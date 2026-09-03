"""Gateway registration, observation, and inventory synchronisation.

This service is read-only against Azure by construction. It registers a gateway, verifies MOSAIC's
access, and mirrors what it observes into Cosmos. It never writes to API Management: enrollment and
policy authoring arrive in a later phase, and until then reporting write capability is the most this
layer does with it.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

import structlog

from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    CapabilitySupport,
    Gateway,
    GatewayCreate,
    GatewayInventorySummary,
    GatewayStatus,
    GatewaySuggestion,
    GatewaySyncRun,
    GatewaySyncStatus,
    GatewayUpdate,
    ManagementMode,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.apim import ApimClient, InventoryCollector, run_preflight
from mosaic_api.integrations.apim.policy_semantics import analyze_policy, summarize_facets
from mosaic_api.observed import (
    GatewayPolicyView,
    ObservedApi,
    ObservedApimGroup,
    ObservedApimUser,
    ObservedBackend,
    ObservedNamedValue,
    ObservedOperation,
    ObservedPolicyDocument,
    ObservedPolicyFragment,
    ObservedProduct,
    ObservedSubscription,
    PolicyScope,
    ScopedPolicyView,
)
from mosaic_api.repositories import GatewayRepository
from mosaic_api.services.directory import Actor

logger = structlog.get_logger()

ClientFactory = Callable[[ApimResourceId], ApimClient]
IdentityResolver = Callable[[], Awaitable[str | None]]

STALE_RUN_MESSAGE = "The API restarted while this sync was running; its result is unknown."
BOOTSTRAP_ACTOR = "system:bootstrap"
BOOTSTRAP_TIMEOUT_SECONDS = 60.0


class GatewayService:
    def __init__(
        self,
        repository: GatewayRepository,
        *,
        client_factory: ClientFactory,
        principal_id: str | None = None,
        identity_resolver: IdentityResolver | None = None,
        bootstrap_resource_id: str | None = None,
    ) -> None:
        self._repository = repository
        self._client_factory = client_factory
        self._principal_id = principal_id
        self._identity_resolver = identity_resolver
        self._identity_resolved = principal_id is not None
        self._bootstrap_resource_id = bootstrap_resource_id
        self._active: set[str] = set()
        self._tasks: set[asyncio.Task[None]] = set()

    async def aclose(self) -> None:
        """Cancel and drain background work before the clients it uses are closed."""

        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._active.clear()

    async def _resolve_principal_id(self) -> str | None:
        if self._identity_resolved or self._identity_resolver is None:
            return self._principal_id
        self._identity_resolved = True
        try:
            self._principal_id = await self._identity_resolver()
        except Exception:
            logger.warning("gateway_identity_lookup_failed")
            self._principal_id = None
        return self._principal_id

    @staticmethod
    def _audit(actor: Actor, action: str, resource_id: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type="gateway",
            resource_id=resource_id,
            actor_object_id=actor.object_id,
        )

    async def list_gateways(self, actor: Actor) -> list[Gateway]:
        return await self._repository.list_gateways(actor.tenant_id)

    async def get_gateway(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self._repository.get_gateway(actor.tenant_id, gateway_id)
        if not gateway:
            raise NotFoundError("Gateway was not found", details={"id": gateway_id})
        return gateway

    async def register(self, actor: Actor, request: GatewayCreate) -> Gateway:
        resource = ApimResourceId.parse(request.azure_resource_id)
        existing = await self._repository.find_gateway_by_resource_id(
            actor.tenant_id, resource.canonical
        )
        if existing:
            raise ConflictError(
                "This API Management service is already registered with MOSAIC",
                details={"id": existing.id, "name": existing.name},
            )

        gateway = Gateway(
            id=deterministic_id("gateway", actor.tenant_id, resource.dedupe_key),
            tenant_id=actor.tenant_id,
            name=(request.name or resource.service_name).strip(),
            provider=request.provider,
            azure_resource_id=resource.canonical,
            subscription_id=resource.subscription_id,
            resource_group=resource.resource_group,
            service_name=resource.service_name,
            environment_label=request.environment_label,
        )
        gateway = await self._apply_preflight(gateway)
        return await self._repository.create_gateway(
            gateway, self._audit(actor, "gateway.registered", gateway.id)
        )

    async def update(self, actor: Actor, gateway_id: str, request: GatewayUpdate) -> Gateway:
        gateway = await self.get_gateway(actor, gateway_id)
        changes = request.model_dump(exclude_unset=True, by_alias=False)
        mode = changes.get("management_mode")
        if mode is not None and mode != ManagementMode.OBSERVE:
            raise ValidationError(
                "MOSAIC does not write to API Management yet, so gateways stay in observe mode.",
                details={"managementMode": str(mode)},
            )
        if "name" in changes and changes["name"] is not None:
            changes["name"] = str(changes["name"]).strip()
        updated = Gateway.model_validate(
            {
                **gateway.model_dump(by_alias=False),
                **changes,
                "etag": gateway.etag,
                "updated_at": utc_now(),
            }
        )
        return await self._repository.save_gateway(
            updated, self._audit(actor, "gateway.updated", updated.id)
        )

    async def delete(self, actor: Actor, gateway_id: str) -> None:
        gateway = await self.get_gateway(actor, gateway_id)
        await self._repository.delete_gateway(
            gateway, self._audit(actor, "gateway.removed", gateway.id)
        )

    async def preflight(self, actor: Actor, gateway_id: str) -> Gateway:
        gateway = await self.get_gateway(actor, gateway_id)
        checked = await self._apply_preflight(gateway)
        return await self._repository.record_gateway_state(checked)

    async def _apply_preflight(self, gateway: Gateway) -> Gateway:
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        result = await run_preflight(client, principal_id=await self._resolve_principal_id())
        capabilities = result.capabilities
        if gateway.capabilities.ai_gateway_policies == CapabilitySupport.AVAILABLE:
            capabilities = capabilities.model_copy(
                update={"ai_gateway_policies": CapabilitySupport.AVAILABLE}
            )
        return gateway.model_copy(
            update={
                "access": result.access,
                "capabilities": capabilities,
                "status": result.status,
                "updated_at": utc_now(),
            }
        )

    async def start_sync(self, actor: Actor, gateway_id: str) -> GatewaySyncRun:
        gateway = await self.get_gateway(actor, gateway_id)
        if not gateway.access.can_read:
            raise ConflictError(
                "MOSAIC cannot read this gateway yet. Grant it access and re-run the check.",
                details={"status": str(gateway.status)},
            )
        # Claim the gateway synchronously. An asyncio.Lock cannot guard admission here because the
        # run has to be persisted before the task starts, and that await lets a second request slip
        # between checking the lock and acquiring it.
        if gateway_id in self._active:
            raise ConflictError(
                "A sync is already running for this gateway",
                details={"gatewayId": gateway_id},
            )
        self._active.add(gateway_id)
        run = GatewaySyncRun(
            id=new_id("syncrun"),
            tenant_id=actor.tenant_id,
            gateway_id=gateway_id,
            actor_object_id=actor.object_id,
        )
        try:
            await self._repository.save_sync_run(run)
        except Exception:
            self._active.discard(gateway_id)
            raise
        task = asyncio.create_task(self._run_sync(gateway, run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def sync_now(self, actor: Actor, gateway_id: str) -> GatewaySyncRun:
        """Run a sync to completion. Used by tests and bootstrap, not by request handlers."""

        run = await self.start_sync(actor, gateway_id)
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        completed = await self._repository.get_sync_run(actor.tenant_id, run.id)
        return completed or run

    async def _run_sync(self, gateway: Gateway, run: GatewaySyncRun) -> None:
        started = utc_now()
        try:
            await self._collect_and_persist(gateway, run, started)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("gateway_sync_failed", gateway_id=gateway.id)
            await self._record_failure(gateway, run, started, str(error))
        finally:
            self._active.discard(gateway.id)

    async def _collect_and_persist(
        self, gateway: Gateway, run: GatewaySyncRun, started: datetime
    ) -> None:
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        collector = InventoryCollector(client, tenant_id=gateway.tenant_id, gateway_id=gateway.id)
        snapshot = await collector.collect()

        # Re-read rather than writing back the copy captured when the sync started. An administrator
        # may have removed or edited the gateway while ARM was being read, and an unconditional
        # upsert of the stale document would resurrect it or silently revert their change.
        current = await self._repository.get_gateway(gateway.tenant_id, gateway.id)
        if current is None:
            logger.info("gateway_sync_discarded", gateway_id=gateway.id, reason="removed")
            return

        removed = await self._repository.replace_observed(
            gateway.tenant_id,
            gateway.id,
            snapshot.entities(),
            snapshot.snapshot_id,
            snapshot.incomplete_types,
        )
        summary = snapshot.summary()
        status = GatewaySyncStatus.PARTIAL if snapshot.errors else GatewaySyncStatus.SUCCEEDED
        await self._finish_run(
            run, status, started, errors=snapshot.errors, counts=summary, removed=removed
        )
        capabilities = current.capabilities.model_copy(
            update={
                "ai_gateway_policies": (
                    CapabilitySupport.AVAILABLE
                    if snapshot.ai_policy_observed
                    else current.capabilities.ai_gateway_policies
                )
            }
        )
        await self._repository.record_gateway_state(
            current.model_copy(
                update={
                    "inventory": summary,
                    "capabilities": capabilities,
                    "last_synced_at": utc_now(),
                    "last_sync_error": "; ".join(snapshot.errors) or None,
                    "status": (
                        GatewayStatus.DEGRADED if snapshot.errors else GatewayStatus.CONNECTED
                    ),
                    "updated_at": utc_now(),
                }
            )
        )

    async def _record_failure(
        self, gateway: Gateway, run: GatewaySyncRun, started: datetime, reason: str
    ) -> None:
        """Best-effort bookkeeping for a failed sync; must never raise back into the task."""

        try:
            await self._finish_run(run, GatewaySyncStatus.FAILED, started, errors=[reason])
            current = await self._repository.get_gateway(gateway.tenant_id, gateway.id)
            if current is None:
                return
            await self._repository.record_gateway_state(
                current.model_copy(
                    update={
                        "status": GatewayStatus.DEGRADED,
                        "last_sync_error": reason,
                        "updated_at": utc_now(),
                    }
                )
            )
        except Exception:
            logger.exception("gateway_sync_failure_not_recorded", gateway_id=gateway.id)

    async def _finish_run(
        self,
        run: GatewaySyncRun,
        status: GatewaySyncStatus,
        started: datetime,
        *,
        errors: list[str],
        counts: GatewayInventorySummary | None = None,
        removed: int = 0,
    ) -> None:
        completed = utc_now()
        await self._repository.save_sync_run(
            run.model_copy(
                update={
                    "status": status,
                    "completed_at": completed,
                    "duration_ms": int((completed - started).total_seconds() * 1000),
                    "counts": counts or GatewayInventorySummary(),
                    "removed": removed,
                    "errors": errors,
                    "updated_at": completed,
                }
            )
        )

    async def get_sync_run(self, actor: Actor, run_id: str) -> GatewaySyncRun:
        run = await self._repository.get_sync_run(actor.tenant_id, run_id)
        if not run:
            raise NotFoundError("Sync run was not found", details={"id": run_id})
        return run

    async def list_sync_runs(self, actor: Actor, gateway_id: str) -> list[GatewaySyncRun]:
        await self.get_gateway(actor, gateway_id)
        return await self._repository.list_sync_runs(actor.tenant_id, gateway_id)

    async def reap_stale_sync_runs(self, tenant_id: str) -> int:
        """Mark runs orphaned by a restart as failed instead of leaving them pending forever."""

        stale = await self._repository.list_unfinished_sync_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.gateway_id in active:
                continue
            completed = utc_now()
            await self._repository.save_sync_run(
                run.model_copy(
                    update={
                        "status": GatewaySyncStatus.FAILED,
                        "completed_at": completed,
                        "errors": [*run.errors, STALE_RUN_MESSAGE],
                        "updated_at": completed,
                    }
                )
            )
            reaped += 1
        return reaped

    async def list_apis(self, actor: Actor, gateway_id: str) -> list[ObservedApi]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedApi, actor.tenant_id, gateway_id, "observedApi"
        )
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def list_operations(
        self, actor: Actor, gateway_id: str, api_name: str | None = None
    ) -> list[ObservedOperation]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedOperation, actor.tenant_id, gateway_id, "observedOperation"
        )
        if api_name:
            items = [item for item in items if item.api_name == api_name]
        return sorted(items, key=lambda item: (item.api_name, item.url_template, item.method))

    async def list_products(self, actor: Actor, gateway_id: str) -> list[ObservedProduct]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedProduct, actor.tenant_id, gateway_id, "observedProduct"
        )
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def list_subscriptions(
        self, actor: Actor, gateway_id: str
    ) -> list[ObservedSubscription]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedSubscription, actor.tenant_id, gateway_id, "observedSubscription"
        )
        return sorted(items, key=lambda item: (item.display_name or item.name).casefold())

    async def list_users(self, actor: Actor, gateway_id: str) -> list[ObservedApimUser]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedApimUser, actor.tenant_id, gateway_id, "observedApimUser"
        )
        return sorted(items, key=lambda item: (item.email or item.name).casefold())

    async def list_groups(self, actor: Actor, gateway_id: str) -> list[ObservedApimGroup]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedApimGroup, actor.tenant_id, gateway_id, "observedApimGroup"
        )
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def list_backends(self, actor: Actor, gateway_id: str) -> list[ObservedBackend]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedBackend, actor.tenant_id, gateway_id, "observedBackend"
        )
        return sorted(items, key=lambda item: item.name.casefold())

    async def list_named_values(self, actor: Actor, gateway_id: str) -> list[ObservedNamedValue]:
        await self.get_gateway(actor, gateway_id)
        items = await self._repository.list_observed(
            ObservedNamedValue, actor.tenant_id, gateway_id, "observedNamedValue"
        )
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def policy_view(self, actor: Actor, gateway_id: str) -> GatewayPolicyView:
        await self.get_gateway(actor, gateway_id)
        documents = await self._repository.list_observed(
            ObservedPolicyDocument, actor.tenant_id, gateway_id, "observedPolicyDocument"
        )
        fragments = await self._repository.list_observed(
            ObservedPolicyFragment, actor.tenant_id, gateway_id, "observedPolicyFragment"
        )
        recognized = 0
        unrecognized = 0
        managed = 0
        for document in documents:
            counts = summarize_facets(document.facets)
            recognized += counts[0]
            unrecognized += counts[1]
            managed += counts[2]
        for fragment in fragments:
            counts = summarize_facets(fragment.facets)
            recognized += counts[0]
            unrecognized += counts[1]
            if fragment.managed_by_mosaic:
                managed += 1
        scope_order = {
            PolicyScope.GLOBAL.value: 0,
            PolicyScope.PRODUCT.value: 1,
            PolicyScope.API.value: 2,
            PolicyScope.OPERATION.value: 3,
        }
        documents.sort(key=lambda item: (scope_order.get(str(item.scope), 9), item.scope_label))
        fragments.sort(key=lambda item: item.name.casefold())
        return GatewayPolicyView(
            documents=documents,
            fragments=fragments,
            recognized_count=recognized,
            unrecognized_count=unrecognized,
            mosaic_managed_count=managed,
        )

    async def operation_policy(
        self, actor: Actor, gateway_id: str, api_name: str, operation_name: str
    ) -> ScopedPolicyView:
        gateway = await self.get_gateway(actor, gateway_id)
        resource = ApimResourceId.parse(gateway.azure_resource_id)
        client = self._client_factory(resource)
        xml = await client.get_operation_policy(api_name, operation_name)
        label = f"Operation: {api_name}/{operation_name}"
        if not xml:
            return ScopedPolicyView(
                scope=PolicyScope.OPERATION,
                scope_id=f"{api_name}/{operation_name}",
                scope_label=label,
                exists=False,
            )
        analysis = analyze_policy(xml)
        return ScopedPolicyView(
            scope=PolicyScope.OPERATION,
            scope_id=f"{api_name}/{operation_name}",
            scope_label=label,
            exists=True,
            content_sha256=analysis.content_sha256,
            facets=analysis.facets,
            unrecognized_elements=sorted(set(analysis.unrecognized_elements)),
        )

    async def suggestions(self, actor: Actor) -> list[GatewaySuggestion]:
        """Surface the APIM that MOSAIC's own deployment created, if it is not onboarded yet."""

        if not self._bootstrap_resource_id:
            return []
        try:
            resource = ApimResourceId.parse(self._bootstrap_resource_id)
        except ValueError:
            return []
        existing = await self._repository.find_gateway_by_resource_id(
            actor.tenant_id, resource.canonical
        )
        return [
            GatewaySuggestion(
                azure_resource_id=resource.canonical,
                service_name=resource.service_name,
                resource_group=resource.resource_group,
                subscription_id=resource.subscription_id,
                already_registered=existing is not None,
                gateway_id=existing.id if existing else None,
                reason="Deployed alongside MOSAIC in this environment.",
            )
        ]

    async def ensure_bootstrap_gateway(self, tenant_id: str) -> Gateway | None:
        """Register the APIM deployed with MOSAIC so the first administrator sees it onboarded.

        This runs in the API rather than an ``azd`` hook because a hook would need a token for
        MOSAIC's own API, which would mean pre-authorising the Azure CLI against the registration.
        Seeding here reuses the managed identity that is already trusted, and stays idempotent.
        """

        if not self._bootstrap_resource_id:
            return None
        try:
            resource = ApimResourceId.parse(self._bootstrap_resource_id)
        except ValueError:
            logger.warning(
                "gateway_bootstrap_invalid_resource_id", value=self._bootstrap_resource_id
            )
            return None
        actor = Actor(object_id=BOOTSTRAP_ACTOR, tenant_id=tenant_id)
        existing = await self._repository.find_gateway_by_resource_id(
            tenant_id, resource.canonical
        )
        if existing:
            return existing
        try:
            gateway = await self.register(
                actor, GatewayCreate(azure_resource_id=resource.canonical)
            )
        except ConflictError:
            return await self._repository.find_gateway_by_resource_id(
                tenant_id, resource.canonical
            )
        logger.info(
            "gateway_bootstrap_registered",
            gateway_id=gateway.id,
            status=str(gateway.status),
            can_read=gateway.access.can_read,
        )
        return gateway

    def schedule_bootstrap(self, tenant_id: str) -> None:
        """Seed in the background so a slow or unreachable gateway cannot delay startup."""

        async def run() -> None:
            try:
                await asyncio.wait_for(
                    self.ensure_bootstrap_gateway(tenant_id), timeout=BOOTSTRAP_TIMEOUT_SECONDS
                )
            except TimeoutError:
                logger.warning("gateway_bootstrap_timed_out")
            except Exception:
                logger.exception("gateway_bootstrap_failed")

        task = asyncio.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
