"""Model endpoint registration, access verification, and model discovery.

Read-only against Azure by construction, exactly like :mod:`mosaic_api.services.gateways`. This
service registers an endpoint, verifies MOSAIC's own control-plane access, reports whether each
registered gateway's managed identity can call it at runtime, and mirrors the models it observes
into Cosmos. It writes nothing to Azure AI and nothing to API Management.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
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
    Publication,
    SubscriptionScanIssue,
    SubscriptionScanStatus,
    SuggestionSource,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.environments import EnvironmentCatalog, azure_environment_tag, suggest_environment
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.aoai import (
    CognitiveServicesClient,
    ModelInventoryCollector,
    RuntimeAccessCheck,
    SubscriptionScanner,
    run_endpoint_preflight,
    verify_gateway_runtime_access,
)
from mosaic_api.integrations.apim import classify_url
from mosaic_api.integrations.rbac import permits
from mosaic_api.observed import (
    AiBackendKind,
    ObservedApi,
    ObservedAvailableModel,
    ObservedBackend,
    ObservedModelDeployment,
)
from mosaic_api.repositories import (
    EnvironmentRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import load_environment_catalog
from mosaic_api.services.model_access import (
    ENVIRONMENTS_SCOPE,
    endpoint_mutation_scope,
    publication_lock,
    scope_lease,
)

logger = structlog.get_logger()

EndpointClientFactory = Callable[[CognitiveServicesResourceId], CognitiveServicesClient]
IdentityResolver = Callable[[], Awaitable[str | None]]

STALE_RUN_MESSAGE = "The API restarted while this sync was running; its result is unknown."

IDENTITY_PLACEHOLDER = "<mosaic-managed-identity-object-id>"
SUBSCRIPTION_PLACEHOLDER = "<subscription-id>"
UNEXPECTED_LIST_FAILURE = "The request failed unexpectedly; the API log has the details."
PARTIAL_SCAN_MESSAGE = (
    "MOSAIC can read only some resources in this subscription, so any Azure AI resources it "
    "cannot read are not suggested here. Endpoints can still be registered by resource ID."
)


def _validate_environment_key(catalog_keys: set[str], value: str) -> None:
    if value not in catalog_keys:
        raise ValidationError(
            f"Environment {value!r} is not defined in Settings → Environments.",
            details={"reason": "unknownEnvironment", "environment": value},
        )

# A subscription-wide account list returns only the accounts this action allows the caller to read,
# so holding it at the subscription itself is what makes that list complete.
_ACCOUNT_READ_ACTION = "Microsoft.CognitiveServices/accounts/read"

# The same shape the resource ID parsers accept. A candidate scope is interpolated into a command an
# operator will run, so anything else is left out rather than quoted into it.
_SUBSCRIPTION_ID_PATTERN = re.compile(r"[0-9a-fA-F-]{36}")

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


def _reader_at_subscription(subscription_id: str, principal_id: str | None) -> AccessRemediation:
    """Reader for MOSAIC's identity at one subscription's scope, which is what the scan needs."""

    scope = f"/subscriptions/{subscription_id}"
    assignee = principal_id or IDENTITY_PLACEHOLDER
    return AccessRemediation(
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
    )


def _list_failure_message(reason: str) -> str:
    """A :class:`DomainError` summary as a sentence. MOSAIC wrote it, so it is safe to return.

    Callers pass the error's ``summary`` rather than its message, because an upstream error's
    message can also say what Azure said, and a scan never returns Azure's text.
    """

    reason = reason.strip().rstrip(".")
    return f"{reason}." if reason else UNEXPECTED_LIST_FAILURE


def _registered_resource(endpoint: ModelEndpoint) -> CognitiveServicesResourceId | None:
    if not endpoint.azure_resource_id:
        return None
    try:
        return CognitiveServicesResourceId.parse(endpoint.azure_resource_id)
    except ValueError:
        return None


def _overlap_message(
    requested: CognitiveServicesResourceId, existing: CognitiveServicesResourceId, name: str
) -> str:
    """Why a resource on an account another registration already covers is refused."""

    twice = "so registering both would list every deployment twice."
    if requested.project_name is None:
        return (
            f"MOSAIC already lists this resource's models through {name}, a Foundry project on "
            f"it. A Foundry project's models are deployed on its parent resource, {twice}"
        )
    if existing.project_name is None:
        return (
            f"MOSAIC already lists this project's models through {name}, its parent resource. "
            f"A Foundry project's models are deployed on its parent resource, {twice}"
        )
    return (
        f"MOSAIC already lists this project's models through {name}, another project on the "
        f"same resource. Foundry projects share their parent resource's deployments, {twice}"
    )


@dataclass(frozen=True)
class _SubscriptionScan:
    status: SubscriptionScanStatus
    scanned: int = 0
    issues: list[SubscriptionScanIssue] = field(default_factory=list)
    partial: list[SubscriptionScanIssue] = field(default_factory=list)
    message: str | None = None
    remediation: list[AccessRemediation] = field(default_factory=list)


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
        bootstrap_subscription_id: str | None = None,
        environment_repository: EnvironmentRepository | None = None,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._client_factory = client_factory
        self._scanner = scanner
        self._principal_id = principal_id
        self._identity_resolver = identity_resolver
        self._identity_resolved = principal_id is not None
        self._bootstrap_subscription_id = bootstrap_subscription_id
        # None only in tests that don't exercise environments; the built-in seeds apply then.
        self._environments = environment_repository
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
        covering = await self._covering_endpoint(actor.tenant_id, resource)
        if covering is not None:
            covering_resource, endpoint_covering = covering
            raise ConflictError(
                _overlap_message(resource, covering_resource, endpoint_covering.name),
                details={"id": endpoint_covering.id, "name": endpoint_covering.name},
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
        if request.environment is not None:
            async with scope_lease(self._gateways, actor.tenant_id, ENVIRONMENTS_SCOPE):
                catalog = await load_environment_catalog(self._environments, actor.tenant_id)
                _validate_environment_key(
                    {environment.key for environment in catalog.environments},
                    request.environment,
                )
                endpoint = endpoint.model_copy(update={"environment": request.environment})
                return await self._repository.create_endpoint(
                    endpoint, self._audit(actor, "modelEndpoint.registered", endpoint.id)
                )
        return await self._repository.create_endpoint(
            endpoint, self._audit(actor, "modelEndpoint.registered", endpoint.id)
        )

    async def _covering_endpoint(
        self, tenant_id: str, resource: CognitiveServicesResourceId
    ) -> tuple[CognitiveServicesResourceId, ModelEndpoint] | None:
        """The registration that already lists this resource's deployments, if any.

        Deployments live on the account, so an account and every project on it all list the same
        models. Any registration therefore covers its whole account.
        """

        account = resource.account_scope.casefold()
        for endpoint in await self._repository.list_endpoints(tenant_id):
            registered = _registered_resource(endpoint)
            if registered is not None and registered.account_scope.casefold() == account:
                return registered, endpoint
        return None

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
        if request.environment is not None:
            async with scope_lease(self._gateways, actor.tenant_id, ENVIRONMENTS_SCOPE):
                catalog = await load_environment_catalog(self._environments, actor.tenant_id)
                _validate_environment_key(
                    {environment.key for environment in catalog.environments},
                    request.environment,
                )
                endpoint = endpoint.model_copy(update={"environment": request.environment})
                await self._repository.save_credential(
                    credential, self._audit(actor, "credentialReference.recorded", credential.id)
                )
                return await self._repository.create_endpoint(
                    endpoint, self._audit(actor, "modelEndpoint.registered", endpoint.id)
                )
        await self._repository.save_credential(
            credential, self._audit(actor, "credentialReference.recorded", credential.id)
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
        """Forget an endpoint, refusing while a publication from it may own anything in APIM.

        A publication that may own gateway state blocks removal: once the endpoint is gone it can
        no longer be planned, applied or unpublished, while the API it created keeps serving. One
        that owns nothing (a never-applied draft, or one already unpublished or rolled back) could
        never be planned again either, so it is deleted with the endpoint rather than left behind.
        Each publication is locked while it is checked and removed, so an apply cannot start
        between the check and the delete.
        """

        async with AsyncExitStack() as stack:
            await stack.enter_async_context(
                scope_lease(self._gateways, actor.tenant_id, endpoint_mutation_scope(endpoint_id))
            )
            endpoint = await self.get_endpoint(actor, endpoint_id)
            publications = [
                publication
                for publication in await self._gateways.list_publications(actor.tenant_id)
                if publication.model_endpoint_id == endpoint.id
            ]
            self._refuse_while_published(
                endpoint, [item for item in publications if item.may_own_gateway_state()]
            )
            forgettable: list[Publication] = []
            blocking: list[Publication] = []
            for publication in publications:
                try:
                    await stack.enter_async_context(
                        publication_lock(self._gateways, actor.tenant_id, publication.id)
                    )
                except ConflictError:
                    # Another run or mutation holds it, so it may be about to own something.
                    blocking.append(publication)
                    continue
                current = await self._gateways.get_publication(actor.tenant_id, publication.id)
                if current is None:
                    continue
                if current.may_own_gateway_state():
                    blocking.append(current)
                else:
                    forgettable.append(current)
            self._refuse_while_published(endpoint, blocking)
            for publication in forgettable:
                await self._gateways.delete_publication(
                    publication,
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=actor.tenant_id,
                        action="publication.removed",
                        resource_type="publication",
                        resource_id=publication.id,
                        actor_object_id=actor.object_id,
                        details={"reason": "modelEndpoint.removed", "modelEndpointId": endpoint.id},
                    ),
                )
            await self._repository.delete_endpoint(
                endpoint, self._audit(actor, "modelEndpoint.removed", endpoint.id)
            )

    @staticmethod
    def _refuse_while_published(endpoint: ModelEndpoint, blocking: list[Publication]) -> None:
        if not blocking:
            return
        one = len(blocking) == 1
        raise ConflictError(
            f"Unpublish the {'model' if one else 'models'} published from {endpoint.name} before "
            f"removing it. {'Its API' if one else 'Their APIs'} would keep serving traffic in API "
            "Management with nothing in MOSAIC to change or remove "
            f"{'it' if one else 'them'}.",
            details={
                "id": endpoint.id,
                "name": endpoint.name,
                "publications": [
                    {
                        "id": publication.id,
                        "displayName": publication.display_name,
                        "status": str(publication.status),
                        "gatewayId": publication.gateway_id,
                    }
                    for publication in blocking
                ],
            },
        )

    async def preflight(self, actor: Actor, endpoint_id: str) -> ModelEndpoint:
        endpoint = await self.get_endpoint(actor, endpoint_id)
        checked = await self._apply_preflight(endpoint)
        recorded = await self._repository.record_endpoint_state(checked)
        if recorded is None:
            raise NotFoundError("Model endpoint was not found", details={"id": endpoint_id})
        return recorded

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
        provider = provider_for(result.capabilities.kind, result.endpoint_url)
        runtime = await self._runtime_access(
            client, endpoint.tenant_id, result.capabilities, provider
        )
        update: dict[str, object] = {
            "access": result.access,
            "capabilities": result.capabilities,
            "runtime_access": runtime,
            "status": result.status,
            "provider": provider,
            # A failed read leaves the tag as last seen rather than claiming it was removed.
            "azure_environment_tag": (
                azure_environment_tag(result.tags)
                if result.tags is not None
                else endpoint.azure_environment_tag
            ),
            "updated_at": utc_now(),
        }
        if result.endpoint_url:
            # ``model_copy`` does not validate, so the URL is coerced here rather than storing a
            # bare string in a field typed as a URL.
            update["endpoint"] = AnyHttpUrl(result.endpoint_url)
        return endpoint.model_copy(update=update)

    async def _runtime_access(
        self,
        client: CognitiveServicesClient,
        tenant_id: str,
        capabilities: ModelEndpointCapabilities,
        provider: ModelProvider,
    ) -> list[GatewayRuntimeAccess]:
        """Report, for every registered gateway, whether it could call this endpoint.

        A failure here degrades one gateway's row rather than the whole preflight: not knowing
        whether a gateway can call an endpoint is a much smaller problem than losing the endpoint's
        access result entirely. Gateways share one check, so each role definition is read once.
        """

        gateways: list[Gateway] = await self._gateways.list_gateways(tenant_id)
        check = RuntimeAccessCheck(client)
        results: list[GatewayRuntimeAccess] = []
        for gateway in gateways:
            try:
                results.append(
                    await verify_gateway_runtime_access(
                        client,
                        gateway,
                        kind=capabilities.kind,
                        provider=provider,
                        capabilities=capabilities,
                        check=check,
                    )
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

        removed = await self._repository.replace_observed_for_endpoint(
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
        items = await self._repository.list_observed_for_endpoint(
            ObservedModelDeployment, actor.tenant_id, endpoint_id, "observedModelDeployment"
        )
        return sorted(items, key=lambda item: item.deployment_name.casefold())

    async def list_available_models(
        self, actor: Actor, endpoint_id: str
    ) -> list[ObservedAvailableModel]:
        await self.get_endpoint(actor, endpoint_id)
        items = await self._repository.list_observed_for_endpoint(
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
            client, actor.tenant_id, endpoint.capabilities, endpoint.provider
        )
        await self._repository.record_endpoint_state(
            endpoint.model_copy(update={"runtime_access": results, "updated_at": utc_now()})
        )
        return results

    async def suggestions(self, actor: Actor) -> ModelEndpointSuggestionView:
        """Endpoints worth registering, from every source MOSAIC can reach.

        The three sources cost very different amounts of privilege. Backends already observed in a
        registered gateway need none at all, so they are gathered first and are always available.
        The subscription scan needs Reader at subscription scope and degrades per subscription,
        including a subscription it could list but can read only in part.
        """

        registered = await self._repository.list_endpoints(actor.tenant_id)
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        # Keyed on the account, because every registration covers its whole account: a Foundry
        # project's deployments live on its parent resource, so suggesting that resource once the
        # project is registered would offer the same models a second time.
        by_account: dict[str, ModelEndpoint] = {}
        for endpoint in registered:
            resource = _registered_resource(endpoint)
            if resource is not None:
                by_account.setdefault(resource.account_scope.casefold(), endpoint)
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

        # Read once: gateways are both a source of suggestions and, when the scan can see nothing,
        # the subscriptions worth naming in its remediation.
        gateways = await self._gateways.list_gateways(actor.tenant_id)
        for suggestion in await self._gateway_backend_suggestions(actor, gateways, by_host):
            add(suggestion)

        if self._scanner is None:
            return ModelEndpointSuggestionView(
                suggestions=suggestions, scan_status=SubscriptionScanStatus.NOT_CONFIGURED
            )

        scan = await self._scan_subscriptions(catalog, by_account, add, gateways)
        return ModelEndpointSuggestionView(
            suggestions=suggestions,
            scan_issues=scan.issues,
            partial_scans=scan.partial,
            subscriptions_scanned=scan.scanned,
            scan_status=scan.status,
            scan_message=scan.message,
            scan_remediation=scan.remediation,
        )

    async def _gateway_backend_suggestions(
        self,
        actor: Actor,
        gateways: list[Gateway],
        by_host: dict[str, ModelEndpoint],
    ) -> list[ModelEndpointSuggestion]:
        """Offer the AI hosts MOSAIC already saw a registered gateway routing to.

        This reuses inventory that is already in Cosmos, so it needs no Azure permission beyond
        what gateway onboarding already required. A hostname cannot be reversed into a resource ID,
        so the suggestion carries the URL and an administrator completes the identification.
        """

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
        catalog: EnvironmentCatalog,
        by_account: dict[str, ModelEndpoint],
        add: Callable[[ModelEndpointSuggestion], None],
        gateways: list[Gateway],
    ) -> _SubscriptionScan:
        """Enumerate Azure AI accounts across visible subscriptions.

        Each subscription is independent: one that MOSAIC cannot read records what to grant and is
        skipped, so a single missing role assignment never blanks the whole suggestion list. A scan
        with no subscription to read at all says so, rather than returning an empty list that is
        indistinguishable from a clean result.

        A subscription that lists successfully is not necessarily read in full: ARM leaves out
        what MOSAIC cannot read and still answers 200. Such a subscription still counts as scanned
        and still yields suggestions, but it is recorded as partial with the same Reader remediation
        so the result is never presented as complete.
        """

        assert self._scanner is not None
        try:
            listed = await self._scanner.list_subscriptions()
        except DomainError as error:
            logger.warning("endpoint_subscription_list_failed", reason=error.message)
            return await self._unscanned(
                SubscriptionScanStatus.LIST_FAILED,
                gateways,
                message=_list_failure_message(error.summary),
            )
        except Exception:
            # Unlike a DomainError's summary, arbitrary exception text can carry upstream or
            # credential detail, so it is logged and never returned.
            logger.exception("endpoint_subscription_list_failed")
            return await self._unscanned(
                SubscriptionScanStatus.LIST_FAILED, gateways, message=UNEXPECTED_LIST_FAILURE
            )

        subscriptions: list[tuple[str, str | None]] = []
        for subscription in listed:
            subscription_id = subscription.get("subscriptionId")
            if not isinstance(subscription_id, str) or not subscription_id:
                continue
            display_name = subscription.get("displayName")
            subscriptions.append(
                (subscription_id, display_name if isinstance(display_name, str) else None)
            )
        if not subscriptions:
            return await self._unscanned(SubscriptionScanStatus.NO_VISIBLE_SUBSCRIPTIONS, gateways)

        scanned = 0
        issues: list[SubscriptionScanIssue] = []
        partial: list[SubscriptionScanIssue] = []
        principal_id = await self._resolve_principal_id()
        for subscription_id, display_name in subscriptions:
            try:
                accounts = await self._scanner.list_accounts(subscription_id)
            except DomainError as error:
                issues.append(
                    SubscriptionScanIssue(
                        subscription_id=subscription_id,
                        display_name=display_name,
                        message=(
                            "MOSAIC could not list Azure AI resources in this subscription, so "
                            "any endpoints it holds are not suggested here. Endpoints can still "
                            f"be registered by resource ID. ({error.summary})"
                        ),
                        remediation=_reader_at_subscription(subscription_id, principal_id),
                    )
                )
                continue

            scanned += 1
            for account in accounts:
                suggestion = self._account_suggestion(catalog, account, by_account)
                if suggestion is not None:
                    add(suggestion)
            if await self._reads_only_part_of(subscription_id):
                partial.append(
                    SubscriptionScanIssue(
                        subscription_id=subscription_id,
                        display_name=display_name,
                        message=PARTIAL_SCAN_MESSAGE,
                        remediation=_reader_at_subscription(subscription_id, principal_id),
                    )
                )
        return _SubscriptionScan(
            status=SubscriptionScanStatus.SCANNED, scanned=scanned, issues=issues, partial=partial
        )

    async def _reads_only_part_of(self, subscription_id: str) -> bool:
        """Does MOSAIC know that it cannot read every Azure AI account in this subscription?

        Only the permissions MOSAIC holds at the subscription itself can answer that, since the
        account list looks the same whether or not ARM filtered it. When they cannot be read, the
        answer is no: MOSAIC reports a partial scan only when it knows the scan was partial.
        """

        assert self._scanner is not None
        try:
            permissions = await self._scanner.subscription_permissions(subscription_id)
        except Exception:
            logger.exception(
                "endpoint_subscription_permissions_failed", subscription_id=subscription_id
            )
            return False
        if permissions is None:
            return False
        return not permits(permissions, _ACCOUNT_READ_ACTION)

    async def _unscanned(
        self,
        status: SubscriptionScanStatus,
        gateways: list[Gateway],
        *,
        message: str | None = None,
    ) -> _SubscriptionScan:
        """A scan with nothing to read, and the Reader assignments that would give it something.

        No single subscription is at fault, so Reader is offered on every subscription MOSAIC has a
        reason to look in: the one it was deployed into, then each registered gateway's. With none
        known, a placeholder scope keeps the command's shape for an operator to complete.
        """

        principal_id = await self._resolve_principal_id()
        candidates = [
            self._bootstrap_subscription_id,
            *(gateway.subscription_id for gateway in gateways),
        ]
        remediation: list[AccessRemediation] = []
        seen: set[str] = set()
        for candidate in candidates:
            subscription_id = (candidate or "").strip()
            if not _SUBSCRIPTION_ID_PATTERN.fullmatch(subscription_id):
                continue
            key = subscription_id.casefold()
            if key in seen:
                continue
            seen.add(key)
            remediation.append(_reader_at_subscription(subscription_id, principal_id))
        if not remediation:
            remediation.append(_reader_at_subscription(SUBSCRIPTION_PLACEHOLDER, principal_id))
        return _SubscriptionScan(status=status, message=message, remediation=remediation)

    @staticmethod
    def _account_suggestion(
        catalog: EnvironmentCatalog,
        account: dict[str, object],
        by_account: dict[str, ModelEndpoint],
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
        existing = by_account.get(resource.account_scope.casefold())
        tags = account.get("tags")
        tag = azure_environment_tag(tags if isinstance(tags, dict) else None)
        suggested = suggest_environment(catalog, tag)
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
            azure_environment_tag=tag,
            suggested_environment=suggested.key if suggested is not None else None,
            already_registered=existing is not None,
            model_endpoint_id=existing.id if existing else None,
            reason=f"Found in subscription {resource.subscription_id}.",
        )
