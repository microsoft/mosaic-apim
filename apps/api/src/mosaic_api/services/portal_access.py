from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import structlog
from pydantic import SecretStr

from mosaic_api.domain import (
    MCP_APPLICATION_ROLE,
    MCP_DELEGATED_SCOPE,
    ApimResourceId,
    AuditEvent,
    ConnectionOperation,
    Entitlement,
    Gateway,
    GrantKey,
    KeyRevealResult,
    McpConnection,
    McpPublication,
    McpServer,
    McpTransportType,
    ModelAccessGrant,
    ModelConnection,
    Principal,
    PrincipalKind,
    Publication,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    grant_precedence_key,
    mcp_resource_metadata_url,
    mcp_server_url,
    model_access_subscription_name,
    new_id,
    subject_kind_for,
)
from mosaic_api.errors import ConflictError, DomainError, NotFoundError
from mosaic_api.integrations.access_policy import governed_operations
from mosaic_api.integrations.apim.model_apis import operations_for
from mosaic_api.repositories import (
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.mcp_access import entitlement_mcp_publication
from mosaic_api.services.model_access import (
    CostCenterIntent,
    cost_center_intent,
    effective_enforcement,
    entitlement_intent_digest,
    grant_key_display_name,
    owns_grant_subscription,
    portal_entitlement,
    portal_runtime,
    publication_lock,
)

logger = structlog.get_logger()

APPLICATION_ROLE = "Models.Invoke.Application"


def _mcp_status_message(status: str) -> str:
    if status == "applied":
        return "The gateway enforces this grant."
    if status == "applying":
        return "An administrator is applying this MCP server's access. Try again after it finishes."
    if status == "failed":
        return (
            "The last apply failed, so the gateway denies every call to this MCP server until an "
            "administrator applies it again."
        )
    if status == "unknown":
        return (
            "The gateway state is uncertain after an interrupted apply. An administrator needs "
            "to recover and apply this MCP server's access."
        )
    if status == "revocationPending":
        return (
            "This grant is disabled in MOSAIC but still applied at the gateway. An administrator "
            "needs to apply this MCP server's access to revoke it."
        )
    if status == "revoked":
        return "This grant is not enforced at the gateway."
    return (
        "This grant isn't applied yet. An administrator needs to plan and apply this MCP "
        "server's access."
    )


class CredentialReader(Protocol):
    async def read_key(
        self, subscription_name: str, api_name: str, slot: Literal["primary", "secondary"]
    ) -> SecretStr: ...


class KeyManager(Protocol):
    """Creates, rotates and deletes a grant's key in API Management. It never reads a key."""

    async def get_subscription(self, name: str) -> dict[str, Any] | None: ...

    async def create(self, name: str, *, display_name: str, api_name: str) -> None: ...

    async def regenerate(self, name: str, slot: Literal["primary", "secondary"]) -> None: ...

    async def delete(self, name: str) -> None: ...


def _owns_key(context: "_AccessContext", name: str) -> bool:
    """Whether the publication records creating this grant's key, at its exact resource ID."""

    resource = ApimResourceId.parse(context.gateway.azure_resource_id)
    expected_id = f"{resource.canonical}/subscriptions/{name}"
    return any(
        item.kind == PublishedResourceKind.SUBSCRIPTION
        and item.name == name
        and item.resource_id.casefold() == expected_id.casefold()
        and item.created_by_mosaic
        for item in context.publication.resources
    )


def _require_key_scope(context: "_AccessContext", live: dict[str, Any]) -> None:
    """Refuse to touch a subscription that isn't scoped to this grant's model API."""

    resource = ApimResourceId.parse(context.gateway.azure_resource_id)
    relative = f"/apis/{context.publication.api_name}"
    properties = live.get("properties") or {}
    scope = str(properties.get("scope", "") if isinstance(properties, dict) else "")
    if scope.rstrip("/").casefold() not in {
        relative.casefold(),
        f"{resource.canonical}{relative}".casefold(),
    }:
        raise ConflictError(
            "This grant's key is no longer scoped to its model in API Management. Ask an "
            "administrator to re-plan its access.",
            details={"reason": "keyScopeChanged"},
        )


@dataclass(frozen=True)
class _AccessContext:
    entitlement: Entitlement
    principal: Principal
    publication: Publication
    gateway: Gateway


@dataclass(frozen=True)
class _McpAccessContext:
    entitlement: Entitlement
    principal: Principal
    server: McpServer
    gateway: Gateway
    publication: McpPublication | None


class PortalAccessService:
    def __init__(
        self,
        entitlements: EntitlementService,
        *,
        repository: EntitlementRepository,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
        credential_factory: Callable[[ApimResourceId], CredentialReader],
        model_runtime_client_id: str | None = None,
        model_client_id: str | None = None,
        key_manager_factory: Callable[[ApimResourceId], KeyManager] | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._repository = repository
        self._directory = directory_repository
        self._gateways = gateway_repository
        self._credential_factory = credential_factory
        self._key_manager_factory = key_manager_factory
        self._runtime_client_id = model_runtime_client_id
        self._model_client_id = model_client_id

    async def list_for_caller(self, actor: Actor) -> list[Entitlement]:
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        direct: list[Entitlement] = []
        # A grant is effective per resource and cost center: a group grant under another cost
        # center than the caller's direct grant is still theirs to select with the header.
        direct_resource_keys: set[tuple[str, str, str, str]] = set()
        if principal is None:
            matches = [
                candidate
                for candidate in await self._directory.list_principals(actor.tenant_id)
                if candidate.object_id.casefold() == actor.object_id.casefold()
            ]
            if len(matches) > 1:
                raise ConflictError(
                    "Multiple principals map to this identity; resolve the duplicates"
                )
            if matches:
                principal = matches[0]
        if principal is not None:
            expected_kind = subject_kind_for(principal.kind)
            direct = [
                entitlement
                for entitlement in await self._entitlements.list_entitlements(
                    actor, subject_id=principal.id
                )
                if entitlement.subject.kind == expected_kind
            ]
            direct_resource_keys = {
                (
                    str(entitlement.resource.kind),
                    entitlement.resource.id,
                    entitlement.resource.scope_id or "",
                    entitlement.cost_center_id,
                )
                for entitlement in direct
                if entitlement.enabled
            }
        security_groups = {
            item.id: item
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP
            and item.object_id.casefold() in actor.group_ids
        }
        group_candidates = [
            entitlement
            for entitlement in await self._entitlements.list_entitlements(actor)
            if entitlement.enabled
            and entitlement.subject.kind == "securityGroup"
            and entitlement.subject.id in security_groups
            and (
                str(entitlement.resource.kind),
                entitlement.resource.id,
                entitlement.resource.scope_id or "",
                entitlement.cost_center_id,
            )
            not in direct_resource_keys
        ]
        effective = set[str]()
        if principal is not None:
            effective = {
                item.entitlement.id
                for item in await self._entitlements.resolve_for_object_id(
                    actor, actor.object_id, group_object_ids=actor.group_ids
                )
            }
        else:
            winners: dict[tuple[str, str, str, str], Entitlement] = {}
            for entitlement in group_candidates:
                key = (
                    str(entitlement.resource.kind),
                    entitlement.resource.id,
                    entitlement.resource.scope_id or "",
                    entitlement.cost_center_id,
                )
                winner = winners.get(key)
                if winner is None or grant_precedence_key(
                    entitlement.enforcement, entitlement.id
                ) < grant_precedence_key(winner.enforcement, winner.id):
                    winners[key] = entitlement
            effective = {item.id for item in winners.values()}
        return [
            portal_entitlement(item)
            for item in sorted(
                [*direct, *[item for item in group_candidates if item.id in effective]],
                key=lambda item: item.id,
            )
        ]

    async def _context(
        self, actor: Actor, entitlement_id: str, *, administrator: bool
    ) -> _AccessContext:
        entitlement = await self._entitlements.get_entitlement(actor, entitlement_id)
        principal = await self._directory.get_principal(
            actor.tenant_id, entitlement.subject.id
        )
        if (
            principal is None
            or entitlement.subject.kind == "group"
            or (
                not administrator
                and principal.kind != PrincipalKind.SECURITY_GROUP
                and principal.object_id.casefold() != actor.object_id.casefold()
            )
            or (
                not administrator
                and principal.kind == PrincipalKind.SECURITY_GROUP
                and principal.object_id.casefold() not in actor.group_ids
            )
        ):
            raise NotFoundError("Entitlement was not found")
        expected_kind = subject_kind_for(principal.kind)
        if entitlement.subject.kind != expected_kind:
            raise ConflictError("The grant no longer matches its principal's identity kind")
        if entitlement.resource.kind != "modelApi":
            raise ConflictError("Connection details are available for published model grants only")
        model_api = await self._gateways.get_model_api(
            actor.tenant_id, entitlement.resource.id
        )
        if model_api is None or model_api.publication_id is None:
            raise ConflictError("This model API is not linked to a MOSAIC publication")
        publication = await self._gateways.get_publication(
            actor.tenant_id, model_api.publication_id
        )
        if (
            publication is None
            or publication.model_api_id != model_api.id
            or publication.gateway_id != model_api.gateway_id
            or publication.api_name != model_api.api_name
        ):
            raise ConflictError("The publication binding changed; re-plan model access")
        if not publication.has_applied_api():
            # Unpublished: the gateway no longer serves the endpoint these details would describe.
            raise ConflictError(
                "This model isn't published in API Management right now, so there's nothing to "
                "connect to",
                details={"reason": "notPublished"},
            )
        gateway = await self._gateways.get_gateway(actor.tenant_id, publication.gateway_id)
        if gateway is None:
            raise ConflictError("The publication's gateway is no longer registered")
        return _AccessContext(entitlement, principal, publication, gateway)

    async def connection(
        self, actor: Actor, entitlement_id: str, *, administrator: bool = False
    ) -> ModelConnection:
        context = await self._context(actor, entitlement_id, administrator=administrator)
        publication = context.publication
        url = context.gateway.capabilities.gateway_url
        if url is None:
            raise ConflictError("Synchronize the gateway to discover its model API endpoint")
        snapshot = publication.applied_access
        audience = snapshot.audience if snapshot else self._runtime_client_id
        current_audience = bool(
            audience and audience.casefold() == (self._runtime_client_id or "").casefold()
        )
        delegated = context.entitlement.subject.kind == "user"
        scope_suffix = "Models.Invoke" if delegated else ".default"
        client_id: str | None = None
        required_app_role: str | None = None
        application_scope: str | None = None
        keys_available = True
        via_group_id: str | None = None
        via_group_name: str | None = None
        if context.principal.kind == PrincipalKind.AGENT_IDENTITY:
            client_id = context.principal.object_id
            required_app_role = APPLICATION_ROLE
        elif context.principal.kind == PrincipalKind.AGENT_USER:
            client_id = context.principal.identity_parent_id
        elif context.entitlement.subject.kind == "application":
            required_app_role = APPLICATION_ROLE
        elif delegated and current_audience:
            client_id = self._model_client_id
        if context.entitlement.subject.kind == "securityGroup":
            scope_suffix = "Models.Invoke"
            client_id = self._model_client_id if current_audience else None
            application_scope = f"api://{audience}/.default" if audience else None
            required_app_role = APPLICATION_ROLE
            keys_available = False
            via_group_id = context.principal.id
            via_group_name = context.principal.label or context.principal.object_id
        operations = (
            governed_operations(publication)
            if publication.governed_access is not None or snapshot is not None
            else operations_for(publication)
        )
        book = await self._entitlements.cost_center_book(actor.tenant_id)
        intent = cost_center_intent(context.entitlement, context.principal, book)
        return ModelConnection(
            entitlement_id=entitlement_id,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            endpoint=f"{str(url).rstrip('/')}/{publication.api_path.strip('/')}",
            deployment_name=publication.deployment_name,
            tenant_id=actor.tenant_id,
            # The route decides, not the caller's role: an administrator reading their own grant
            # through /api/v1/me gets the end-user response.
            runtime=(
                context.entitlement.runtime
                if administrator
                else portal_runtime(context.entitlement.runtime)
            ),
            applied_methods=snapshot.settings if snapshot else None,
            entra_audience=audience,
            entra_scope=f"api://{audience}/{scope_suffix}" if audience else None,
            entra_client_id=client_id,
            api_shape=publication.api_shape,
            operations=[
                ConnectionOperation(
                    name=operation.name, method=operation.method, path=operation.url_template
                )
                for operation in operations
            ],
            publication_limits=(
                snapshot.publication_enforcement if snapshot else publication.enforcement
            ),
            grant_limits=context.entitlement.enforcement,
            principal_kind=context.principal.kind,
            required_app_role=required_app_role,
            entra_application_scope=application_scope,
            keys_available=keys_available,
            via_group_id=via_group_id,
            via_group_name=via_group_name,
            cost_center=book.ref(context.entitlement.cost_center_id),
            key_exists=keys_available
            and owns_grant_subscription(publication, context.entitlement.id),
            keys_allowed_by_cost_center=intent.keys_allowed if intent else False,
        )

    async def _mcp_context(
        self, actor: Actor, entitlement_id: str, *, administrator: bool
    ) -> _McpAccessContext:
        entitlement = await self._entitlements.get_entitlement(actor, entitlement_id)
        principal = await self._directory.get_principal(
            actor.tenant_id, entitlement.subject.id
        )
        if (
            principal is None
            or entitlement.subject.kind == "group"
            or (
                not administrator
                and principal.kind != PrincipalKind.SECURITY_GROUP
                and principal.object_id.casefold() != actor.object_id.casefold()
            )
            or (
                not administrator
                and principal.kind == PrincipalKind.SECURITY_GROUP
                and principal.object_id.casefold() not in actor.group_ids
            )
        ):
            raise NotFoundError("Entitlement was not found")
        expected_kind = subject_kind_for(principal.kind)
        if entitlement.subject.kind != expected_kind:
            raise ConflictError("The grant no longer matches its principal's identity kind")
        if entitlement.resource.kind != "mcpServer":
            raise ConflictError("Connection details are available for MCP server grants only")
        server = await self._gateways.get_mcp_server(actor.tenant_id, entitlement.resource.id)
        if server is None:
            raise ConflictError("This MCP server is no longer registered")
        publication = await entitlement_mcp_publication(self._gateways, entitlement)
        if server.publication_id is not None and publication is None:
            raise ConflictError("The publication binding changed; re-plan MCP access")
        if publication is not None and not publication.has_applied_api():
            # Never applied, or unpublished: there's no MCP API at the URL these details would give.
            raise ConflictError(
                "This MCP server isn't published in API Management right now, so there's nothing "
                "to connect to",
                details={"reason": "notPublished"},
            )
        gateway = await self._gateways.get_gateway(actor.tenant_id, server.gateway_id)
        if gateway is None:
            raise ConflictError("The MCP server's gateway is no longer registered")
        return _McpAccessContext(entitlement, principal, server, gateway, publication)

    async def mcp_connection(
        self, actor: Actor, entitlement_id: str, *, administrator: bool = False
    ) -> McpConnection:
        context = await self._mcp_context(
            actor, entitlement_id, administrator=administrator
        )
        if context.publication is None:
            connection = self._adopted_mcp_connection(actor, context, administrator=administrator)
        else:
            connection = self._published_mcp_connection(
                actor, context, administrator=administrator
            )
        book = await self._entitlements.cost_center_book(actor.tenant_id)
        return connection.model_copy(
            update={"cost_center": book.ref(context.entitlement.cost_center_id)}
        )

    def _adopted_mcp_connection(
        self, actor: Actor, context: _McpAccessContext, *, administrator: bool
    ) -> McpConnection:
        gateway_url = context.gateway.capabilities.gateway_url
        route_name = (
            "sse" if context.server.transport_type == McpTransportType.SSE else "message"
        )
        route = next(
            (item for item in context.server.endpoints if item.name == route_name),
            None,
        )
        template = route.uri_template if route else "/sse" if route_name == "sse" else "/mcp"
        server_url = None
        if gateway_url is not None:
            server_url = (
                f"{str(gateway_url).rstrip('/')}/{context.server.path.strip('/')}"
                f"/{template.strip('/')}"
            )
        return McpConnection(
            entitlement_id=context.entitlement.id,
            mcp_server_id=context.server.id,
            gateway_id=context.server.gateway_id,
            display_name=context.server.display_name,
            tenant_id=actor.tenant_id,
            server_url=server_url,
            transport=context.server.transport_type,
            enforced=False,
            status_message=(
                "MOSAIC records this grant but doesn't enforce it. This MCP server was imported "
                "from the gateway, so its own policy decides who can call it."
            ),
            # The route decides, as it does for model connections: see portal_runtime.
            runtime=(
                context.entitlement.runtime
                if administrator
                else portal_runtime(context.entitlement.runtime)
            ),
            principal_kind=context.principal.kind,
            limits=context.entitlement.enforcement,
        )

    def _published_mcp_connection(
        self, actor: Actor, context: _McpAccessContext, *, administrator: bool
    ) -> McpConnection:
        publication = context.publication
        assert publication is not None
        gateway_url = context.gateway.capabilities.gateway_url
        snapshot = publication.applied_access
        audience = snapshot.audience if snapshot else self._runtime_client_id
        current_audience = bool(
            audience and audience.casefold() == (self._runtime_client_id or "").casefold()
        )
        delegated_scope: str | None = None
        application_scope: str | None = None
        required_app_role: str | None = None
        client_id: str | None = None
        via_group_id: str | None = None
        via_group_name: str | None = None

        if context.entitlement.subject.kind in {"user", "securityGroup"} and audience:
            delegated_scope = f"api://{audience}/{MCP_DELEGATED_SCOPE}"
        if context.entitlement.subject.kind in {"application", "securityGroup"} and audience:
            application_scope = f"api://{audience}/.default"
            required_app_role = MCP_APPLICATION_ROLE
        if context.principal.kind == PrincipalKind.AGENT_IDENTITY:
            client_id = context.principal.object_id
            required_app_role = MCP_APPLICATION_ROLE
        elif context.principal.kind == PrincipalKind.AGENT_USER:
            client_id = context.principal.identity_parent_id
        elif (
            context.entitlement.subject.kind in {"user", "securityGroup"}
            and current_audience
        ):
            client_id = self._model_client_id
        if context.entitlement.subject.kind == "securityGroup":
            via_group_id = context.principal.id
            via_group_name = context.principal.label or context.principal.object_id
            required_app_role = MCP_APPLICATION_ROLE

        runtime = context.entitlement.runtime
        enforced = bool(runtime and runtime.status == "applied")
        return McpConnection(
            entitlement_id=context.entitlement.id,
            mcp_server_id=context.server.id,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            display_name=publication.display_name,
            tenant_id=actor.tenant_id,
            server_url=(
                mcp_server_url(str(gateway_url), publication.api_path)
                if gateway_url is not None
                else None
            ),
            transport=context.server.transport_type,
            enforced=enforced,
            status_message=_mcp_status_message(runtime.status if runtime else "pending"),
            runtime=runtime if administrator else portal_runtime(runtime),
            entra_audience=audience,
            delegated_scope=delegated_scope,
            application_scope=application_scope,
            required_app_role=required_app_role,
            client_id=client_id,
            principal_kind=context.principal.kind,
            via_group_id=via_group_id,
            via_group_name=via_group_name,
            resource_metadata_url=(
                mcp_resource_metadata_url(str(gateway_url), publication.api_path)
                if gateway_url is not None
                else None
            ),
            limits=context.entitlement.enforcement,
        )

    @staticmethod
    def _applied_grant(
        context: _AccessContext,
        intent: CostCenterIntent | None,
        *,
        require_key: bool = True,
    ) -> ModelAccessGrant:
        """The applied direct grant a key belongs to, refusing anything not exactly applied.

        ``require_key`` False is for creating the key, which can't be owned yet.
        """

        entitlement = context.entitlement
        publication = context.publication
        snapshot = publication.applied_access
        if (
            not entitlement.enabled
            or publication.status != PublicationStatus.PUBLISHED
            or publication.access_state != "applied"
            or publication.governed_access is None
            or not publication.governed_access.keys_enabled
            or snapshot is None
            or not snapshot.settings.keys_enabled
            or snapshot.settings != publication.governed_access
            or snapshot.publication_enforcement != publication.enforcement
        ):
            raise ConflictError(
                "Key access requires an enabled grant successfully applied to API Management"
            )
        matches = [
            grant for grant in snapshot.grants if grant.entitlement_id == entitlement.id
        ]
        if len(matches) != 1:
            raise ConflictError("This grant has not been applied to API Management")
        grant = matches[0]
        if grant.is_group_grant or grant.subscription_name is None:
            raise ConflictError(
                "Access granted to a security group uses Entra tokens only; there's no key to "
                "reveal."
            )
        expected_name = model_access_subscription_name(
            publication.tenant_id, publication.id, entitlement.id
        )
        if (
            not grant.enabled
            or intent is None
            or grant.subject != entitlement.subject
            or grant.object_id.casefold() != context.principal.object_id.casefold()
            or grant.enforcement != effective_enforcement(entitlement, intent)
            or grant.intent_digest
            != entitlement_intent_digest(entitlement, context.principal, intent)
            or grant.subscription_name != expected_name
        ):
            raise ConflictError("Apply the pending changes to this grant before using its key")
        if not grant.keys_allowed or not intent.keys_allowed:
            raise ConflictError(
                "Keys are turned off for this grant's cost center. Use a Microsoft Entra token.",
                details={"reason": "costCenterKeysOff"},
            )
        if require_key and not _owns_key(context, expected_name):
            raise ConflictError(
                "This grant has no key yet. Create one first.",
                details={"reason": "noKey"},
            )
        return grant

    async def _key_context(
        self, actor: Actor, entitlement_id: str, *, administrator: bool
    ) -> tuple[_AccessContext, ModelAccessGrant]:
        context = await self._context(actor, entitlement_id, administrator=administrator)
        if await self._gateways.get_publication_lock(
            actor.tenant_id, context.publication.id
        ) is not None:
            raise ConflictError(
                "A publication operation is in progress or needs recovery; key retrieval is blocked"
            )
        return context, self._applied_grant(context, await self._intent(context))

    async def _intent(self, context: _AccessContext) -> CostCenterIntent | None:
        book = await self._entitlements.cost_center_book(context.entitlement.tenant_id)
        return cost_center_intent(context.entitlement, context.principal, book)

    async def _audit(
        self,
        actor: Actor,
        entitlement_id: str,
        slot: Literal["primary", "secondary"],
        outcome: str,
        *,
        subscription_name: str | None = None,
        error_code: str | None = None,
    ) -> None:
        await self._repository.record_audit(
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action=f"credential.reveal.{outcome}",
                resource_type="entitlement",
                resource_id=entitlement_id,
                actor_object_id=actor.object_id,
                details={
                    "slot": slot,
                    "subscriptionName": subscription_name,
                    "errorCode": error_code,
                },
            )
        )

    async def reveal_key(
        self,
        actor: Actor,
        entitlement_id: str,
        slot: Literal["primary", "secondary"],
        *,
        administrator: bool = False,
    ) -> KeyRevealResult:
        await self._audit(actor, entitlement_id, slot, "requested")
        subscription_name: str | None = None
        try:
            context, grant = await self._key_context(
                actor, entitlement_id, administrator=administrator
            )
            subscription_name = grant.subscription_name
            if subscription_name is None:
                raise ConflictError(
                    "Access granted to a security group uses Entra tokens only; there's no key to "
                    "reveal."
                )
            reader = self._credential_factory(
                ApimResourceId.parse(context.gateway.azure_resource_id)
            )
            key = await reader.read_key(subscription_name, context.publication.api_name, slot)

            # A revoke or identity/configuration change can arrive during the ARM round trip.
            latest, latest_grant = await self._key_context(
                actor, entitlement_id, administrator=administrator
            )
            if (
                latest.publication.applied_access != context.publication.applied_access
                or latest_grant != grant
            ):
                raise ConflictError("Model access changed during key retrieval; reload and retry")
            await self._audit(
                actor, entitlement_id, slot, "succeeded", subscription_name=subscription_name
            )
        except DomainError as error:
            await self._audit(
                actor,
                entitlement_id,
                slot,
                "denied" if error.status_code < 500 else "failed",
                subscription_name=subscription_name,
                error_code=error.code,
            )
            logger.warning(
                "credential_reveal_failed",
                tenant_id=actor.tenant_id,
                entitlement_id=entitlement_id,
                error_code=error.code,
            )
            raise
        book = await self._entitlements.cost_center_book(actor.tenant_id)
        return KeyRevealResult(
            entitlement_id=entitlement_id,
            subscription_name=subscription_name,
            slot=slot,
            key=key.get_secret_value(),
            cost_center=book.ref(context.entitlement.cost_center_id),
        )

    # -- keys on request --------------------------------------------------------------------

    async def _key_audit(
        self,
        actor: Actor,
        entitlement_id: str,
        action: Literal["created", "rotated", "deleted"],
        outcome: str,
        *,
        subscription_name: str | None = None,
        slot: Literal["primary", "secondary"] | None = None,
        error_code: str | None = None,
        cost_center_id: str | None = None,
    ) -> None:
        await self._repository.record_audit(
            AuditEvent(
                id=new_id("audit"),
                tenant_id=actor.tenant_id,
                action=f"credential.{action}.{outcome}",
                resource_type="entitlement",
                resource_id=entitlement_id,
                actor_object_id=actor.object_id,
                details={
                    "slot": slot,
                    "subscriptionName": subscription_name,
                    "costCenterId": cost_center_id,
                    "errorCode": error_code,
                },
            )
        )

    def _key_manager(self, context: _AccessContext) -> KeyManager:
        if self._key_manager_factory is None:
            raise ConflictError("This deployment can't manage keys")
        return self._key_manager_factory(ApimResourceId.parse(context.gateway.azure_resource_id))

    async def _record_key(
        self, context: _AccessContext, name: str, *, present: bool
    ) -> None:
        """Record that the publication owns, or no longer owns, a grant's key."""

        current = await self._gateways.get_publication(
            context.publication.tenant_id, context.publication.id
        )
        if current is None:
            raise ConflictError("The publication disappeared while managing this key")
        resource = ApimResourceId.parse(context.gateway.azure_resource_id)
        kept = [
            item
            for item in current.resources
            if not (item.kind == PublishedResourceKind.SUBSCRIPTION and item.name == name)
        ]
        if present:
            kept.append(
                PublishedResource(
                    kind=PublishedResourceKind.SUBSCRIPTION,
                    name=name,
                    resource_id=f"{resource.canonical}/subscriptions/{name}",
                    created_by_mosaic=True,
                )
            )
        await self._gateways.record_publication_state(
            current.model_copy(update={"resources": kept})
        )

    async def _manage_key(
        self,
        actor: Actor,
        entitlement_id: str,
        action: Literal["created", "rotated", "deleted"],
        *,
        administrator: bool,
        slot: Literal["primary", "secondary"] | None = None,
    ) -> GrantKey:
        """Create, rotate or delete a grant's key, under its publication's lock.

        The caller must hold the grant, or use the administrator route. Creating needs an enabled,
        applied direct grant whose cost center allows keys. The subscription's name is fixed by
        the grant, so the gateway's policy already recognizes it and nothing is applied again.
        """

        await self._key_audit(actor, entitlement_id, action, "requested", slot=slot)
        subscription_name: str | None = None
        cost_center_id: str | None = None
        try:
            context = await self._context(actor, entitlement_id, administrator=administrator)
            cost_center_id = context.entitlement.cost_center_id
            manager = self._key_manager(context)
            async with publication_lock(
                self._gateways, actor.tenant_id, context.publication.id
            ):
                # Read again under the lock, which an apply also holds.
                context = await self._context(
                    actor, entitlement_id, administrator=administrator
                )
                name = model_access_subscription_name(
                    context.publication.tenant_id, context.publication.id, context.entitlement.id
                )
                subscription_name = name
                if action == "deleted":
                    if not _owns_key(context, name):
                        raise ConflictError(
                            "This grant has no key to delete.", details={"reason": "noKey"}
                        )
                    live = await manager.get_subscription(name)
                    if live is not None:
                        _require_key_scope(context, live)
                        await manager.delete(name)
                    await self._record_key(context, name, present=False)
                else:
                    intent = await self._intent(context)
                    grant = self._applied_grant(context, intent, require_key=action == "rotated")
                    live = await manager.get_subscription(name)
                    if action == "rotated":
                        if live is None:
                            raise ConflictError(
                                "This grant's key is missing from API Management. Delete it and "
                                "create a new one.",
                                details={"reason": "keyMissing"},
                            )
                        _require_key_scope(context, live)
                        assert slot is not None
                        await manager.regenerate(name, slot)
                    else:
                        if live is not None and _owns_key(context, name):
                            raise ConflictError(
                                "This grant already has a key. Rotate it, or delete it first.",
                                details={"reason": "keyExists"},
                            )
                        if live is not None:
                            raise ConflictError(
                                "A subscription with this grant's key name already exists in API "
                                "Management, and MOSAIC didn't create it.",
                                details={"reason": "keyNotOwned"},
                            )
                        # Recorded before it's created, so a create that fails part way is still
                        # MOSAIC's to delete, and unpublish removes it.
                        await self._record_key(context, name, present=True)
                        assert intent is not None
                        await manager.create(
                            name,
                            display_name=grant_key_display_name(grant.display_name, intent.code),
                            api_name=context.publication.api_name,
                        )
            await self._key_audit(
                actor,
                entitlement_id,
                action,
                "succeeded",
                subscription_name=subscription_name,
                slot=slot,
                cost_center_id=cost_center_id,
            )
        except DomainError as error:
            await self._key_audit(
                actor,
                entitlement_id,
                action,
                "denied" if error.status_code < 500 else "failed",
                subscription_name=subscription_name,
                slot=slot,
                error_code=error.code,
                cost_center_id=cost_center_id,
            )
            logger.warning(
                "credential_key_change_failed",
                tenant_id=actor.tenant_id,
                entitlement_id=entitlement_id,
                action=action,
                error_code=error.code,
            )
            raise
        book = await self._entitlements.cost_center_book(actor.tenant_id)
        return GrantKey(
            entitlement_id=entitlement_id,
            subscription_name=name,
            exists=action != "deleted",
            cost_center=book.ref(cost_center_id),
            rotated=slot if action == "rotated" else None,
        )

    async def create_key(
        self, actor: Actor, entitlement_id: str, *, administrator: bool = False
    ) -> GrantKey:
        return await self._manage_key(
            actor, entitlement_id, "created", administrator=administrator
        )

    async def rotate_key(
        self,
        actor: Actor,
        entitlement_id: str,
        slot: Literal["primary", "secondary"],
        *,
        administrator: bool = False,
    ) -> GrantKey:
        return await self._manage_key(
            actor, entitlement_id, "rotated", administrator=administrator, slot=slot
        )

    async def delete_key(
        self, actor: Actor, entitlement_id: str, *, administrator: bool = False
    ) -> GrantKey:
        return await self._manage_key(
            actor, entitlement_id, "deleted", administrator=administrator
        )
