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
    PortalAccessRequest,
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

    async def my_entitlements(
        self, actor: Actor, *, include_disabled: bool = False
    ) -> list[ResolvedEntitlement]:
        return await self._entitlements.resolve_for_object_id(
            actor, actor.object_id, include_disabled=include_disabled
        )

    async def my_access_requests(self, actor: Actor) -> list[PortalAccessRequest]:
        # Scoped here rather than trusting a caller-supplied filter: this is the only thing
        # standing between one portal user and another's requests.
        requests = await self._entitlements.list_access_requests(
            actor, requester_object_id=actor.object_id
        )
        visible = await self._visible_resource_keys(actor)
        summaries = await self._entitlements.resource_summaries(
            actor.tenant_id,
            [item.resource for item in requests],
            snapshots=[item.resource_snapshot for item in requests],
            requested_environments=[item.requested_environment for item in requests],
            visible=visible,
        )
        return [
            PortalAccessRequest.model_validate(
                {**item.model_dump(by_alias=False), "resource_summary": summaries[index]}
            )
            for index, item in enumerate(requests)
        ]

    async def create_access_request(
        self, actor: Actor, request: AccessRequestCreate
    ) -> AccessRequest:
        return await self._entitlements.create_catalog_access_request(actor, request)

    async def withdraw_access_request(self, actor: Actor, request_id: str) -> AccessRequest:
        return await self._entitlements.withdraw_access_request(actor, request_id)

    async def _visible_resource_keys(self, actor: Actor) -> dict[tuple[str, str, str], bool]:
        visible: dict[tuple[str, str, str], bool] = {}
        for item in await self._entitlements.resolve_for_object_id(actor, actor.object_id):
            resource = item.entitlement.resource
            visible[(str(resource.kind), resource.id, resource.scope_id or "")] = True
        gateway_ids = {
            gateway.id for gateway in await self._gateways.list_gateways(actor.tenant_id)
        }
        for model_api in await self._gateways.list_model_apis(actor.tenant_id):
            if (
                model_api.visibility == CatalogVisibility.CATALOG
                and model_api.gateway_id in gateway_ids
            ):
                visible[("modelApi", model_api.id, "")] = True
        for mcp_server in await self._gateways.list_mcp_servers(actor.tenant_id):
            if (
                mcp_server.visibility == CatalogVisibility.CATALOG
                and mcp_server.gateway_id in gateway_ids
            ):
                visible[("mcpServer", mcp_server.id, "")] = True
        return visible

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
            gateway.id: gateway
            for gateway in await self._gateways.list_gateways(actor.tenant_id)
        }

        entries: list[CatalogEntry] = []
        for model_api in await self._gateways.list_model_apis(actor.tenant_id):
            if model_api.visibility != CatalogVisibility.CATALOG:
                continue
            gateway = gateways.get(model_api.gateway_id)
            if gateway is None:
                continue
            entries.append(
                CatalogEntry(
                    kind=CatalogEntryKind.MODEL_API,
                    id=model_api.id,
                    display_name=model_api.display_name,
                    summary=model_api.summary,
                    gateway_id=model_api.gateway_id,
                    gateway_name=gateway.name,
                    environment=gateway.environment,
                    entitled=("modelApi", model_api.id) in entitled,
                    request_state=open_requests.get(("modelApi", model_api.id)),
                )
            )
        for mcp_server in await self._gateways.list_mcp_servers(actor.tenant_id):
            if mcp_server.visibility != CatalogVisibility.CATALOG:
                continue
            gateway = gateways.get(mcp_server.gateway_id)
            if gateway is None:
                continue
            entries.append(
                CatalogEntry(
                    kind=CatalogEntryKind.MCP_SERVER,
                    id=mcp_server.id,
                    display_name=mcp_server.display_name,
                    summary=mcp_server.summary,
                    gateway_id=mcp_server.gateway_id,
                    gateway_name=gateway.name,
                    environment=gateway.environment,
                    entitled=("mcpServer", mcp_server.id) in entitled,
                    request_state=open_requests.get(("mcpServer", mcp_server.id)),
                )
            )
        entries.sort(key=lambda item: (item.display_name.casefold(), item.id))
        return entries
