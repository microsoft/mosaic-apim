"""Model endpoint registration, access verification, and model discovery.

Read-only against Azure by construction, exactly like :mod:`mosaic_api.services.gateways`. This
service registers an endpoint, verifies MOSAIC's own control-plane access, reports whether each
registered gateway's managed identity can call it at runtime, and mirrors the models it observes
into Cosmos. It writes nothing to Azure AI and nothing to API Management.
"""

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from urllib.parse import urlparse

import structlog
from pydantic import AnyHttpUrl

from mosaic_api.domain import (
    READER_ROLE_ID,
    READER_ROLE_NAME,
    AccessRemediation,
    AuditEvent,
    CognitiveServicesResourceId,
    CredentialReference,
    EndpointAuthMode,
    Gateway,
    GatewayRuntimeAccess,
    GatewaySyncStatus,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelEndpointCreate,
    ModelEndpointStatus,
    ModelEndpointSuggestion,
    ModelEndpointSuggestionView,
    ModelEndpointSyncRun,
    ModelEndpointUpdate,
    ModelInventorySummary,
    ModelProvider,
    SubscriptionScanIssue,
    SuggestionSource,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.aoai import (
    CognitiveServicesClient,
    ModelInventoryCollector,
    SubscriptionScanner,
    run_endpoint_preflight,
    verify_gateway_runtime_access,
)
from mosaic_api.integrations.apim import classify_url
from mosaic_api.observed import (
    AiBackendKind,
    ObservedApi,
    ObservedAvailableModel,
    ObservedBackend,
    ObservedModelDeployment,
)
from mosaic_api.repositories import GatewayRepository, ModelEndpointRepository
from mosaic_api.services.directory import Actor

logger = structlog.get_logger()

EndpointClientFactory = Callable[[CognitiveServicesResourceId], CognitiveServicesClient]
IdentityResolver = Callable[[], Awaitable[str | None]]

STALE_RUN_MESSAGE = "The API restarted while this sync was running; its result is unknown."

_PROVIDER_BY_HOST_SUFFIX: tuple[tuple[str, ModelProvider], ...] = (
    (".openai.azure.com", ModelProvider.AZURE_OPENAI),
    (".api.cognitive.microsoft.com", ModelProvider.AZURE_OPENAI),
    (".services.ai.azure.com", ModelProvider.AZURE_AI_FOUNDRY),
    (".cognitiveservices.azure.com", ModelProvider.AZURE_AI_FOUNDRY),
)


def provider_for(kind: str | None, endpoint: str | None) -> ModelProvider:
    """Classify an Azure AI resource, preferring its ARM ``kind`` over its hostname."""

    normalized = (kind or "").casefold()
    if normalized == "openai":
        return ModelProvider.AZURE_OPENAI
    if normalized in {"aiservices", "cognitiveservices"}:
        return ModelProvider.AZURE_AI_FOUNDRY
    host = (urlparse(endpoint or "").hostname or "").casefold()
    for suffix, provider in _PROVIDER_BY_HOST_SUFFIX:
        if host.endswith(suffix):
            return provider
    return ModelProvider.AZURE_AI_FOUNDRY


class ModelEndpointService:
    def __init__(
        self,
        repository: ModelEndpointRepository,
        *,
        gateway_repository: GatewayRepository,
        client_factory: EndpointClientFactory,
        scanner: SubscriptionScanner | None = None,
        principal_id: str | None = None,
        identity_resolver: IdentityResolver | None = None,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._client_factory = client_factory
        self._scanner = scanner
        self._principal_id = principal_id
        self._identity_resolver = identity_resolver
        self._identity_resolved = principal_id is not None
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
            logger.warning("endpoint_identity_lookup_failed")
            self._principal_id = None
        return self._principal_id

    @staticmethod
    def _audit(actor: Actor, action: str, resource_id: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type="modelEndpoint",
            resource_id=resource_id,
            actor_object_id=actor.object_id,
        )

    async def list_endpoints(self, actor: Actor) -> list[ModelEndpoint]:
        return await self._repository.list_endpoints(actor.tenant_id)

    async def get_endpoint(self, actor: Actor, endpoint_id: str) -> ModelEndpoint:
        endpoint = await self._repository.get_endpoint(actor.tenant_id, endpoint_id)
        if not endpoint:
            raise NotFoundError("Model endpoint was not found", details={"id": endpoint_id})
        return endpoint

    async def register(self, actor: Actor, request: ModelEndpointCreate) -> ModelEndpoint:
        if request.azure_resource_id:
            return await self._register_azure(actor, request)
        return await self._register_compatible(actor, request)

    async def _register_azure(
        self, actor: Actor, request: ModelEndpointCreate
    ) -> ModelEndpoint:
        assert request.azure_resource_id is not None
        resource = CognitiveServicesResourceId.parse(request.azure_resource_id)
        existing = await self._repository.find_endpoint_by_resource_id(
            actor.tenant_id, resource.canonical
        )
        if existing:
            raise ConflictError(
                "This Azure AI resource is already registered with MOSAIC",
                details={"id": existing.id, "name": existing.name},
            )

        endpoint = ModelEndpoint(
            id=deterministic_id("endpoint", actor.tenant_id, resource.dedupe_key),
            tenant_id=actor.tenant_id,
            name=(request.name or resource.project_name or resource.account_name).strip(),
            provider=request.provider or ModelProvider.AZURE_AI_FOUNDRY,
            # Replaced by the account's real endpoint during preflight. A resource-derived
            # placeholder keeps the record valid if preflight cannot read the account.
            endpoint=f"https://{resource.account_name}.cognitiveservices.azure.com",
            azure_resource_id=resource.canonical,
            subscription_id=resource.subscription_id,
            resource_group=resource.resource_group,
            account_name=resource.account_name,
            project_name=resource.project_name,
            environment_label=request.environment_label,
            auth_mode=EndpointAuthMode.MANAGED_IDENTITY,
        )
        endpoint = await self._apply_preflight(endpoint)
        return await self._repository.create_endpoint(
            endpoint, self._audit(actor, "modelEndpoint.registered", endpoint.id)
        )

    async def _register_compatible(
        self, actor: Actor, request: ModelEndpointCreate
    ) -> ModelEndpoint:
        assert request.endpoint is not None
        assert request.credential_secret_uri is not None
        url = str(request.endpoint).rstrip("/")
        existing = await self._repository.find_endpoint_by_url(actor.tenant_id, url)
        if existing:
            raise ConflictError(
                "This endpoint URL is already registered with MOSAIC",
                details={"id": existing.id, "name": existing.name},
            )
        host = urlparse(url).hostname or url
        credential = CredentialReference(
            id=deterministic_id("credential", actor.tenant_id, url),
            tenant_id=actor.tenant_id,
            name=f"{host} API key",
            secret_uri=request.credential_secret_uri,
        )
        await self._repository.save_credential(
            credential, self._audit(actor, "credentialReference.recorded", credential.id)
        )
        endpoint = ModelEndpoint(
            id=deterministic_id("endpoint", actor.tenant_id, url.casefold()),
            tenant_id=actor.tenant_id,
            name=(request.name or host).strip(),
            provider=ModelProvider.OPENAI_COMPATIBLE,
            endpoint=request.endpoint,
            environment_label=request.environment_label,
            auth_mode=EndpointAuthMode.API_KEY,
            credential_reference_id=credential.id,
            status=ModelEndpointStatus.PENDING,
            capabilities=ModelEndpointCapabilities(
                notes=[
                    "MOSAIC stores only the Key Vault secret URI for this endpoint. The key "
                    "itself is read at discovery time and never persisted.",
                ]
            ),
        )
        return await self._repository.create_endpoint(
            endpoint, self._audit(actor, "modelEndpoint.registered", endpoint.id)
        )

    async def update(
        self, actor: Actor, endpoint_id: str, request: ModelEndpointUpdate
    ) -> ModelEndpoint:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        changes = request.model_dump(exclude_unset=True, by_alias=False)
        secret_uri = changes.pop("credential_secret_uri", None)
        if "name" in changes and changes["name"] is not None:
            changes["name"] = str(changes["name"]).strip()
        if secret_uri is not None:
            if endpoint.auth_mode != EndpointAuthMode.API_KEY:
                raise ValidationError(
                    "This endpoint authenticates with managed identity, so it has no API key.",
                    details={"authMode": str(endpoint.auth_mode)},
                )
            credential_id = endpoint.credential_reference_id or deterministic_id(
                "credential", actor.tenant_id, str(endpoint.endpoint).rstrip("/")
            )
            host = urlparse(str(endpoint.endpoint)).hostname or str(endpoint.endpoint)
            await self._repository.save_credential(
                CredentialReference(
                    id=credential_id,
                    tenant_id=actor.tenant_id,
                    name=f"{host} API key",
                    secret_uri=secret_uri,
                ),
                self._audit(actor, "credentialReference.recorded", credential_id),
            )
            changes["credential_reference_id"] = credential_id
        updated = ModelEndpoint.model_validate(
            {
                **endpoint.model_dump(by_alias=False),
                **changes,
                "etag": endpoint.etag,
                "updated_at": utc_now(),
            }
        )
        return await self._repository.save_endpoint(
            updated, self._audit(actor, "modelEndpoint.updated", updated.id)
        )

    async def delete(self, actor: Actor, endpoint_id: str) -> None:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        await self._repository.delete_endpoint(
            endpoint, self._audit(actor, "modelEndpoint.removed", endpoint.id)
        )

    async def preflight(self, actor: Actor, endpoint_id: str) -> ModelEndpoint:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        checked = await self._apply_preflight(endpoint)
        return await self._repository.record_endpoint_state(checked)

    async def _apply_preflight(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        """Verify MOSAIC's control-plane access, then report each gateway's runtime access."""

        if endpoint.auth_mode == EndpointAuthMode.API_KEY or not endpoint.azure_resource_id:
            # A key-based endpoint has no ARM surface to preflight. Its access is proven or
            # disproven by discovery itself, so it is left pending rather than claimed connected.
            return endpoint
        resource = CognitiveServicesResourceId.parse(endpoint.azure_resource_id)
        client = self._client_factory(resource)
        result = await run_endpoint_preflight(
            client, principal_id=await self._resolve_principal_id()
        )
        runtime = await self._runtime_access(client, endpoint.tenant_id, result.capabilities.kind)
        update: dict[str, object] = {
            "access": result.access,
            "capabilities": result.capabilities,
            "runtime_access": runtime,
            "status": result.status,
            "provider": provider_for(result.capabilities.kind, result.endpoint_url),
            "updated_at": utc_now(),
        }
        if result.endpoint_url:
            # ``model_copy`` does not validate, so the URL is coerced here rather than storing a
            # bare string in a field typed as a URL.
            update["endpoint"] = AnyHttpUrl(result.endpoint_url)
        return endpoint.model_copy(update=update)

    async def _runtime_access(
        self, client: CognitiveServicesClient, tenant_id: str, kind: str | None
    ) -> list[GatewayRuntimeAccess]:
        """Report, for every registered gateway, whether it could call this endpoint.

        A failure here degrades one gateway's row rather than the whole preflight: not knowing
        whether a gateway can call an endpoint is a much smaller problem than losing the endpoint's
        access result entirely.
        """

        gateways: list[Gateway] = await self._gateways.list_gateways(tenant_id)
        results: list[GatewayRuntimeAccess] = []
        for gateway in gateways:
            try:
                results.append(
                    await verify_gateway_runtime_access(client, gateway, kind=kind)
                )
            except Exception:
                logger.warning(
                    "endpoint_runtime_access_failed", gateway_id=gateway.id
                )
        return results

    async def start_sync(self, actor: Actor, endpoint_id: str) -> ModelEndpointSyncRun:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        if (
            endpoint.auth_mode == EndpointAuthMode.MANAGED_IDENTITY
            and not endpoint.access.can_read
        ):
            raise ConflictError(
                "MOSAIC cannot read this endpoint yet. Grant it access and re-run the check.",
                details={"status": str(endpoint.status)},
            )
        # Claim synchronously, for the same reason gateway sync does: the run must be persisted
        # before the task starts, and that await would let a second request slip past a lock.
        if endpoint_id in self._active:
            raise ConflictError(
                "A sync is already running for this endpoint",
                details={"endpointId": endpoint_id},
            )
        self._active.add(endpoint_id)
        run = ModelEndpointSyncRun(
            id=new_id("syncrun"),
            tenant_id=actor.tenant_id,
            endpoint_id=endpoint_id,
            actor_object_id=actor.object_id,
        )
        try:
            await self._repository.save_endpoint_sync_run(run)
        except Exception:
            self._active.discard(endpoint_id)
            raise
        task = asyncio.create_task(self._run_sync(endpoint, run))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return run

    async def sync_now(self, actor: Actor, endpoint_id: str) -> ModelEndpointSyncRun:
        """Run a sync to completion. Used by tests, not by request handlers."""

        run = await self.start_sync(actor, endpoint_id)
        await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        completed = await self._repository.get_endpoint_sync_run(actor.tenant_id, run.id)
        return completed or run

    async def _run_sync(self, endpoint: ModelEndpoint, run: ModelEndpointSyncRun) -> None:
        started = utc_now()
        try:
            await self._collect_and_persist(endpoint, run, started)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("endpoint_sync_failed", endpoint_id=endpoint.id)
            await self._record_failure(endpoint, run, started, str(error))
        finally:
            self._active.discard(endpoint.id)

    async def _collect_and_persist(
        self, endpoint: ModelEndpoint, run: ModelEndpointSyncRun, started: datetime
    ) -> None:
        if not endpoint.azure_resource_id:
            raise ValidationError(
                "MOSAIC can only discover models on Azure AI endpoints in this release.",
                details={"endpointId": endpoint.id},
            )
        resource = CognitiveServicesResourceId.parse(endpoint.azure_resource_id)
        client = self._client_factory(resource)
        collector = ModelInventoryCollector(
            client, tenant_id=endpoint.tenant_id, endpoint_id=endpoint.id
        )
        snapshot = await collector.collect()

        # Re-read rather than writing back the copy captured when the sync started: an
        # administrator may have removed or renamed the endpoint while ARM was being read.
        current = await self._repository.get_endpoint(endpoint.tenant_id, endpoint.id)
        if current is None:
            logger.info("endpoint_sync_discarded", endpoint_id=endpoint.id, reason="removed")
            return

        removed = await self._repository.replace_observed_models(
            endpoint.tenant_id,
            endpoint.id,
            snapshot.entities(),
            snapshot.snapshot_id,
            snapshot.incomplete_types,
        )
        summary = snapshot.summary()
        status = GatewaySyncStatus.PARTIAL if snapshot.errors else GatewaySyncStatus.SUCCEEDED
        await self._finish_run(
            run, status, started, errors=snapshot.errors, counts=summary, removed=removed
        )
        await self._repository.record_endpoint_state(
            current.model_copy(
                update={
                    "inventory": summary,
                    "last_synced_at": utc_now(),
                    "last_sync_error": "; ".join(snapshot.errors) or None,
                    "status": (
                        ModelEndpointStatus.DEGRADED
                        if snapshot.errors
                        else ModelEndpointStatus.CONNECTED
                    ),
                    "updated_at": utc_now(),
                }
            )
        )

    async def _record_failure(
        self,
        endpoint: ModelEndpoint,
        run: ModelEndpointSyncRun,
        started: datetime,
        reason: str,
    ) -> None:
        """Best-effort bookkeeping for a failed sync; must never raise back into the task."""

        try:
            await self._finish_run(run, GatewaySyncStatus.FAILED, started, errors=[reason])
            current = await self._repository.get_endpoint(endpoint.tenant_id, endpoint.id)
            if current is None:
                return
            await self._repository.record_endpoint_state(
                current.model_copy(
                    update={
                        "status": ModelEndpointStatus.DEGRADED,
                        "last_sync_error": reason,
                        "updated_at": utc_now(),
                    }
                )
            )
        except Exception:
            logger.exception("endpoint_sync_failure_not_recorded", endpoint_id=endpoint.id)

    async def _finish_run(
        self,
        run: ModelEndpointSyncRun,
        status: GatewaySyncStatus,
        started: datetime,
        *,
        errors: list[str],
        counts: ModelInventorySummary | None = None,
        removed: int = 0,
    ) -> None:
        completed = utc_now()
        await self._repository.save_endpoint_sync_run(
            run.model_copy(
                update={
                    "status": status,
                    "completed_at": completed,
                    "duration_ms": int((completed - started).total_seconds() * 1000),
                    "counts": counts or ModelInventorySummary(),
                    "removed": removed,
                    "errors": errors,
                    "updated_at": completed,
                }
            )
        )

    async def get_sync_run(self, actor: Actor, run_id: str) -> ModelEndpointSyncRun:
        run = await self._repository.get_endpoint_sync_run(actor.tenant_id, run_id)
        if not run:
            raise NotFoundError("Sync run was not found", details={"id": run_id})
        return run

    async def list_sync_runs(self, actor: Actor, endpoint_id: str) -> list[ModelEndpointSyncRun]:
        await self.get_endpoint(actor, endpoint_id)
        return await self._repository.list_endpoint_sync_runs(actor.tenant_id, endpoint_id)

    async def reap_stale_sync_runs(self, tenant_id: str) -> int:
        """Mark runs orphaned by a restart as failed instead of leaving them pending forever."""

        stale = await self._repository.list_unfinished_endpoint_sync_runs(tenant_id)
        active = set(self._active)
        reaped = 0
        for run in stale:
            if run.endpoint_id in active:
                continue
            completed = utc_now()
            await self._repository.save_endpoint_sync_run(
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

    async def list_deployments(
        self, actor: Actor, endpoint_id: str
    ) -> list[ObservedModelDeployment]:
        await self.get_endpoint(actor, endpoint_id)
        items = await self._repository.list_observed_models(
            ObservedModelDeployment, actor.tenant_id, endpoint_id, "observedModelDeployment"
        )
        return sorted(items, key=lambda item: item.deployment_name.casefold())

    async def list_available_models(
        self, actor: Actor, endpoint_id: str
    ) -> list[ObservedAvailableModel]:
        await self.get_endpoint(actor, endpoint_id)
        items = await self._repository.list_observed_models(
            ObservedAvailableModel, actor.tenant_id, endpoint_id, "observedAvailableModel"
        )
        return sorted(
            items, key=lambda item: (item.model_name.casefold(), item.model_version or "")
        )

    async def runtime_access(
        self, actor: Actor, endpoint_id: str
    ) -> list[GatewayRuntimeAccess]:
        """Re-evaluate gateway runtime access on demand, without a full preflight."""

        endpoint = await self.get_endpoint(actor, endpoint_id)
        if not endpoint.azure_resource_id:
            return []
        resource = CognitiveServicesResourceId.parse(endpoint.azure_resource_id)
        client = self._client_factory(resource)
        results = await self._runtime_access(
            client, actor.tenant_id, endpoint.capabilities.kind
        )
        await self._repository.record_endpoint_state(
            endpoint.model_copy(update={"runtime_access": results, "updated_at": utc_now()})
        )
        return results

    async def suggestions(self, actor: Actor) -> ModelEndpointSuggestionView:
        """Endpoints worth registering, from every source MOSAIC can reach.

        The three sources cost very different amounts of privilege. Backends already observed in a
        registered gateway need none at all, so they are gathered first and are always available.
        The subscription scan needs Reader at subscription scope and degrades per subscription.
        """

        registered = await self._repository.list_endpoints(actor.tenant_id)
        by_resource = {
            (endpoint.azure_resource_id or "").casefold(): endpoint
            for endpoint in registered
            if endpoint.azure_resource_id
        }
        by_host = {
            (urlparse(str(endpoint.endpoint)).hostname or "").casefold(): endpoint
            for endpoint in registered
        }

        suggestions: list[ModelEndpointSuggestion] = []
        seen: set[str] = set()

        def add(suggestion: ModelEndpointSuggestion) -> None:
            key = (
                suggestion.azure_resource_id.casefold()
                if suggestion.azure_resource_id
                else f"host:{(urlparse(str(suggestion.endpoint)).hostname or '').casefold()}"
            )
            if not key or key in seen:
                return
            seen.add(key)
            suggestions.append(suggestion)

        for suggestion in await self._gateway_backend_suggestions(actor, by_resource, by_host):
            add(suggestion)

        scanned = 0
        scan_issues: list[SubscriptionScanIssue] = []
        if self._scanner is not None:
            scanned, scan_issues = await self._scan_subscriptions(by_resource, add)

        return ModelEndpointSuggestionView(
            suggestions=suggestions,
            scan_issues=scan_issues,
            subscriptions_scanned=scanned,
        )

    async def _gateway_backend_suggestions(
        self,
        actor: Actor,
        by_resource: dict[str, ModelEndpoint],
        by_host: dict[str, ModelEndpoint],
    ) -> list[ModelEndpointSuggestion]:
        """Offer the AI hosts MOSAIC already saw a registered gateway routing to.

        This reuses inventory that is already in Cosmos, so it needs no Azure permission beyond
        what gateway onboarding already required. A hostname cannot be reversed into a resource ID,
        so the suggestion carries the URL and an administrator completes the identification.
        """

        gateways = await self._gateways.list_gateways(actor.tenant_id)
        found: list[ModelEndpointSuggestion] = []
        for gateway in gateways:
            try:
                backends = await self._gateways.list_observed(
                    ObservedBackend, actor.tenant_id, gateway.id, "observedBackend"
                )
                apis = await self._gateways.list_observed(
                    ObservedApi, actor.tenant_id, gateway.id, "observedApi"
                )
            except Exception:
                logger.warning("endpoint_suggestion_inventory_failed", gateway_id=gateway.id)
                continue

            urls = [backend.url for backend in backends if backend.url]
            urls.extend(api.service_url for api in apis if api.service_url)
            for url in urls:
                if classify_url(url) == AiBackendKind.NONE:
                    continue
                host = (urlparse(url).hostname or "").casefold()
                if not host:
                    continue
                existing = by_host.get(host)
                found.append(
                    ModelEndpointSuggestion(
                        source=SuggestionSource.GATEWAY_BACKEND,
                        endpoint=f"https://{host}",
                        provider=provider_for(None, url),
                        already_registered=existing is not None,
                        model_endpoint_id=existing.id if existing else None,
                        reason=(
                            f"The gateway {gateway.name} routes traffic to this host, so it is "
                            "already serving models MOSAIC does not govern."
                        ),
                    )
                )
        return found

    async def _scan_subscriptions(
        self,
        by_resource: dict[str, ModelEndpoint],
        add: Callable[[ModelEndpointSuggestion], None],
    ) -> tuple[int, list[SubscriptionScanIssue]]:
        """Enumerate Azure AI accounts across visible subscriptions.

        Each subscription is independent: one that MOSAIC cannot read records what to grant and is
        skipped, so a single missing role assignment never blanks the whole suggestion list.
        """

        assert self._scanner is not None
        issues: list[SubscriptionScanIssue] = []
        try:
            subscriptions = await self._scanner.list_subscriptions()
        except DomainError as error:
            logger.warning("endpoint_subscription_list_failed", reason=error.message)
            return 0, issues

        scanned = 0
        principal_id = await self._resolve_principal_id()
        for subscription in subscriptions:
            subscription_id = subscription.get("subscriptionId")
            if not isinstance(subscription_id, str) or not subscription_id:
                continue
            display_name = subscription.get("displayName")
            try:
                accounts = await self._scanner.list_accounts(subscription_id)
            except DomainError as error:
                scope = f"/subscriptions/{subscription_id}"
                assignee = principal_id or "<mosaic-managed-identity-object-id>"
                issues.append(
                    SubscriptionScanIssue(
                        subscription_id=subscription_id,
                        display_name=(
                            display_name if isinstance(display_name, str) else None
                        ),
                        message=(
                            "MOSAIC could not list Azure AI resources in this subscription, so "
                            "any endpoints it holds are not suggested here. Endpoints can still "
                            f"be registered by resource ID. ({error.message})"
                        ),
                        remediation=AccessRemediation(
                            role_name=READER_ROLE_NAME,
                            role_definition_id=READER_ROLE_ID,
                            scope=scope,
                            principal_id=principal_id,
                            command=(
                                "az role assignment create"
                                f' --assignee-object-id "{assignee}"'
                                " --assignee-principal-type ServicePrincipal"
                                f' --role "{READER_ROLE_NAME}"'
                                f' --scope "{scope}"'
                            ),
                        ),
                    )
                )
                continue

            scanned += 1
            for account in accounts:
                suggestion = self._account_suggestion(account, by_resource)
                if suggestion is not None:
                    add(suggestion)
        return scanned, issues

    @staticmethod
    def _account_suggestion(
        account: dict[str, object], by_resource: dict[str, ModelEndpoint]
    ) -> ModelEndpointSuggestion | None:
        resource_id = account.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            return None
        try:
            resource = CognitiveServicesResourceId.parse(resource_id)
        except ValueError:
            return None
        kind = account.get("kind")
        kind = kind if isinstance(kind, str) else None
        if (kind or "").casefold() not in {"openai", "aiservices"}:
            # Speech, Vision, and the other Cognitive Services kinds host no model deployments.
            return None
        properties = account.get("properties")
        endpoint = None
        if isinstance(properties, dict):
            candidate = properties.get("endpoint")
            endpoint = candidate if isinstance(candidate, str) and candidate else None
        location = account.get("location")
        existing = by_resource.get(resource.canonical.casefold())
        return ModelEndpointSuggestion(
            source=SuggestionSource.SUBSCRIPTION_SCAN,
            endpoint=endpoint,
            azure_resource_id=resource.canonical,
            account_name=resource.account_name,
            resource_group=resource.resource_group,
            subscription_id=resource.subscription_id,
            kind=kind,
            location=location if isinstance(location, str) else None,
            provider=provider_for(kind, endpoint),
            already_registered=existing is not None,
            model_endpoint_id=existing.id if existing else None,
            reason=f"Found in subscription {resource.subscription_id}.",
        )
