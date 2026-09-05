"""The end-user portal's read model.

Everything here answers a question about *the caller*, never about anyone else. The object ID comes
from the validated token, never from a query parameter, so there is no route on which one portal
user can enumerate another's grants. ADR 0008 separated the portal's identity from the
administrator console's; this keeps their data surfaces separate too.

The portal never queries API Management. Cosmos is the source of truth for entitlement, as ADR 0009
records, so a grant that has not yet been realised in APIM still shows here — described as exactly
that rather than silently omitted.
"""

from mosaic_api.domain import (
    AccessRequest,
    AccessRequestCreate,
    AccessRequestState,
    CatalogEntry,
    CatalogEntryKind,
    CatalogVisibility,
    PortalProfile,
    ResolvedEntitlement,
)
from mosaic_api.repositories import DirectoryRepository, GatewayRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService


class PortalService:
    def __init__(
        self,
        entitlements: EntitlementService,
        *,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
    ) -> None:
        self._entitlements = entitlements
        self._directory = directory_repository
        self._gateways = gateway_repository

    async def profile(self, actor: Actor, *, roles: list[str], is_admin: bool) -> PortalProfile:
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        entitlements = await self.my_entitlements(actor)
        pending = [
            item
            for item in await self.my_access_requests(actor)
            if item.state == AccessRequestState.PENDING
        ]
        return PortalProfile(
            object_id=actor.object_id,
            tenant_id=actor.tenant_id,
            roles=sorted(roles),
            is_admin=is_admin,
            principal_id=principal.id if principal else None,
            display_label=principal.label if principal else None,
            entitlement_count=len(entitlements),
            pending_request_count=len(pending),
        )

    async def my_entitlements(self, actor: Actor) -> list[ResolvedEntitlement]:
        return await self._entitlements.resolve_for_object_id(actor, actor.object_id)

    async def my_access_requests(self, actor: Actor) -> list[AccessRequest]:
        # Scoped here rather than trusting a caller-supplied filter: this is the only thing
        # standing between one portal user and another's requests.
        return await self._entitlements.list_access_requests(
            actor, requester_object_id=actor.object_id
        )

    async def create_access_request(
        self, actor: Actor, request: AccessRequestCreate
    ) -> AccessRequest:
        return await self._entitlements.create_access_request(actor, request)

    async def withdraw_access_request(self, actor: Actor, request_id: str) -> AccessRequest:
        return await self._entitlements.withdraw_access_request(actor, request_id)

    async def catalog(self, actor: Actor) -> list[CatalogEntry]:
        """Everything an administrator published to the catalog, annotated for this caller.

        A resource an administrator marked ``private`` is omitted entirely rather than shown as
        unavailable, because the point of hiding it is that end users do not know it exists.
        """

        entitled = {
            (str(item.entitlement.resource.kind), item.entitlement.resource.id)
            for item in await self.my_entitlements(actor)
        }
        # Only open requests describe the caller's current position. A denied or withdrawn request
        # from last month should not stop them asking again, so it is not surfaced as state here.
        open_requests: dict[tuple[str, str], AccessRequestState] = {
            (str(item.resource.kind), item.resource.id): item.state
            for item in await self.my_access_requests(actor)
            if item.state == AccessRequestState.PENDING
        }
        gateways = {
            gateway.id: gateway.name
            for gateway in await self._gateways.list_gateways(actor.tenant_id)
        }

        entries: list[CatalogEntry] = []
        for model_api in await self._gateways.list_model_apis(actor.tenant_id):
            if model_api.visibility != CatalogVisibility.CATALOG:
                continue
            entries.append(
                CatalogEntry(
                    kind=CatalogEntryKind.MODEL_API,
                    id=model_api.id,
                    display_name=model_api.display_name,
                    summary=model_api.summary,
                    gateway_id=model_api.gateway_id,
                    gateway_name=gateways.get(model_api.gateway_id),
                    entitled=("modelApi", model_api.id) in entitled,
                    request_state=open_requests.get(("modelApi", model_api.id)),
                )
            )
        for mcp_server in await self._gateways.list_mcp_servers(actor.tenant_id):
            if mcp_server.visibility != CatalogVisibility.CATALOG:
                continue
            entries.append(
                CatalogEntry(
                    kind=CatalogEntryKind.MCP_SERVER,
                    id=mcp_server.id,
                    display_name=mcp_server.display_name,
                    summary=mcp_server.summary,
                    gateway_id=mcp_server.gateway_id,
                    gateway_name=gateways.get(mcp_server.gateway_id),
                    entitled=("mcpServer", mcp_server.id) in entitled,
                    request_state=open_requests.get(("mcpServer", mcp_server.id)),
                )
            )
        entries.sort(key=lambda item: (item.display_name.casefold(), item.id))
        return entries
