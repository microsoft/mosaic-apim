from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

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
from mosaic_api.services.model_access import entitlement_intent_digest

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
    ) -> None:
        self._entitlements = entitlements
        self._repository = repository
        self._directory = directory_repository
        self._gateways = gateway_repository
        self._credential_factory = credential_factory
        self._runtime_client_id = model_runtime_client_id
        self._model_client_id = model_client_id

    async def list_for_caller(self, actor: Actor) -> list[Entitlement]:
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        direct: list[Entitlement] = []
        direct_resource_keys: set[tuple[str, str, str]] = set()
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
            winners: dict[tuple[str, str, str], Entitlement] = {}
            for entitlement in group_candidates:
                key = (
                    str(entitlement.resource.kind),
                    entitlement.resource.id,
                    entitlement.resource.scope_id or "",
                )
                winner = winners.get(key)
                if winner is None or grant_precedence_key(
                    entitlement.enforcement, entitlement.id
                ) < grant_precedence_key(winner.enforcement, winner.id):
                    winners[key] = entitlement
            effective = {item.id for item in winners.values()}
        return sorted(
            [*direct, *[item for item in group_candidates if item.id in effective]],
            key=lambda item: item.id,
        )

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
        return ModelConnection(
            entitlement_id=entitlement_id,
            publication_id=publication.id,
            gateway_id=publication.gateway_id,
            endpoint=f"{str(url).rstrip('/')}/{publication.api_path.strip('/')}",
            deployment_name=publication.deployment_name,
            tenant_id=actor.tenant_id,
            runtime=context.entitlement.runtime,
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
            return self._adopted_mcp_connection(actor, context)
        return self._published_mcp_connection(actor, context)

    def _adopted_mcp_connection(
        self, actor: Actor, context: _McpAccessContext
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
            runtime=context.entitlement.runtime,
            principal_kind=context.principal.kind,
            limits=context.entitlement.enforcement,
        )

    def _published_mcp_connection(
        self, actor: Actor, context: _McpAccessContext
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
            runtime=runtime,
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
    def _applied_grant(context: _AccessContext) -> ModelAccessGrant:
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
            or grant.subject != entitlement.subject
            or grant.object_id.casefold() != context.principal.object_id.casefold()
            or grant.enforcement != entitlement.enforcement
            or grant.intent_digest != entitlement_intent_digest(entitlement, context.principal)
            or grant.subscription_name != expected_name
        ):
            raise ConflictError("Apply the pending changes to this grant before revealing its key")
        resource = ApimResourceId.parse(context.gateway.azure_resource_id)
        expected_id = f"{resource.canonical}/subscriptions/{expected_name}"
        if not any(
            item.kind == PublishedResourceKind.SUBSCRIPTION
            and item.name == expected_name
            and item.resource_id.casefold() == expected_id.casefold()
            and item.created_by_mosaic
            for item in publication.resources
        ):
            raise ConflictError("MOSAIC has no trusted ownership record for this subscription")
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
        return context, self._applied_grant(context)

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
        return KeyRevealResult(
            entitlement_id=entitlement_id,
            subscription_name=subscription_name,
            slot=slot,
            key=key.get_secret_value(),
        )
