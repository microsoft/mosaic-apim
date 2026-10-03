"""The end-user portal's read model.

Everything here answers a question about *the caller*, never about anyone else. The object ID comes
from the validated token, never from a query parameter, so there is no route on which one portal
user can enumerate another's grants. ADR 0008 separated the portal's identity from the
administrator console's; this keeps their data surfaces separate too.

The portal never queries API Management. Cosmos is the source of truth for entitlement, as ADR 0009
records, so a grant that has not yet been realised in APIM still shows here — described as exactly
that rather than silently omitted. A grant's runtime state is reported by its status alone: API
Management's error text for a failed apply stays on the administrator routes, for the reasons
:func:`~mosaic_api.services.model_access.portal_runtime` gives.

Grants and requests are named the way the catalog names the resource, and the names are resolved
here rather than joined against the catalog in the browser: the catalog lists only the model APIs,
MCP servers, and pool models currently published to it, while a grant or a request can be for one
an administrator has since made private or unpublished, or for a product or model deployment, which
it never lists.

A pool model is shown as a model, never as part of a pool: users see its name, its gateway's
environment, the API it's called with, and its capacity badge, but not the pool, its endpoints,
their regions, or its deployments (ADR 0024).
"""

from typing import Literal

from mosaic_api.deployment_capacity import CapacityType
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestCreate,
    AccessRequestState,
    CatalogEntry,
    CatalogEntryKind,
    CatalogVisibility,
    PortalAccessRequest,
    PortalProfile,
    PortalResolvedEntitlement,
    PublicationStatus,
    ResolvedEntitlement,
)
from mosaic_api.model_pools import ModelPool, PoolModel, capacity_label
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.repositories import (
    DirectoryRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService, GovernedRecords
from mosaic_api.services.model_access import portal_entitlement
from mosaic_api.services.pool_access import pool_model_listed, pool_model_offered

# What inventory says about each endpoint's deployments, by endpoint ID and then by casefolded
# deployment name, read once for one catalog. None for an endpoint that's no longer registered.
_Inventory = dict[str, dict[str, ObservedModelDeployment] | None]


class PortalService:
    def __init__(
        self,
        entitlements: EntitlementService,
        *,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._directory = directory_repository
        self._gateways = gateway_repository
        # Only a pool model's capacity badge reads inventory. Without it, the catalog shows none.
        self._endpoints = endpoint_repository

    async def profile(self, actor: Actor, *, roles: list[str], is_admin: bool) -> PortalProfile:
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        entitlements = await self._resolved(actor)
        pending = [
            item for item in await self._requests(actor) if item.state == AccessRequestState.PENDING
        ]
        book = await self._entitlements.cost_center_book(actor.tenant_id)
        return PortalProfile(
            object_id=actor.object_id,
            tenant_id=actor.tenant_id,
            roles=sorted(roles),
            is_admin=is_admin,
            principal_id=principal.id if principal else None,
            display_label=principal.label if principal else None,
            entitlement_count=len(entitlements),
            pending_request_count=len(pending),
            groups_overage=actor.groups_overage,
            default_cost_center=book.ref(book.default_for(principal)),
        )

    async def _resolved(
        self,
        actor: Actor,
        *,
        include_disabled: bool = False,
        records: GovernedRecords | None = None,
    ) -> list[ResolvedEntitlement]:
        return await self._entitlements.resolve_for_object_id(
            actor,
            actor.object_id,
            group_object_ids=actor.group_ids,
            include_disabled=include_disabled,
            records=records,
        )

    async def _requests(self, actor: Actor) -> list[AccessRequest]:
        # Scoped here rather than trusting a caller-supplied filter: this is the only thing
        # standing between one portal user and another's requests.
        return await self._entitlements.list_access_requests(
            actor, requester_object_id=actor.object_id
        )

    async def _portal_requests(
        self, actor: Actor, requests: list[AccessRequest]
    ) -> list[PortalAccessRequest]:
        """Name each request and summarise its resource, as the portal shows it.

        The name follows the catalog's naming rules. The summary shows live gateway and
        environment details only for a resource the caller can still see, and otherwise what the
        request recorded when it was made. Both share one read of each kind of record.
        """

        if not requests:
            return []
        records = self._entitlements.governed_records(actor.tenant_id)
        resources = [item.resource for item in requests]
        names = await self._entitlements.resource_display_names(actor, resources, records=records)
        book = await self._entitlements.cost_center_book(actor.tenant_id)
        summaries = await self._entitlements.resource_summaries(
            actor.tenant_id,
            resources,
            snapshots=[item.resource_snapshot for item in requests],
            requested_environments=[item.requested_environment for item in requests],
            visible=await self._visible_resource_keys(actor, records),
            records=records,
        )
        return [
            PortalAccessRequest.model_validate(
                {
                    **dict(item),
                    "resource_display_name": name,
                    "resource_summary": summary,
                    "cost_center": book.ref(item.cost_center_id),
                }
            )
            for item, name, summary in zip(requests, names, summaries, strict=True)
        ]

    async def my_entitlements(
        self, actor: Actor, *, include_disabled: bool = False
    ) -> list[PortalResolvedEntitlement]:
        records = self._entitlements.governed_records(actor.tenant_id)
        resolved = await self._resolved(actor, include_disabled=include_disabled, records=records)
        names = await self._entitlements.resource_display_names(
            actor, [item.entitlement.resource for item in resolved], records=records
        )
        return [
            PortalResolvedEntitlement.model_validate(
                {
                    **dict(item),
                    "entitlement": portal_entitlement(item.entitlement),
                    "resource_display_name": name,
                }
            )
            for item, name in zip(resolved, names, strict=True)
        ]

    async def my_access_requests(self, actor: Actor) -> list[PortalAccessRequest]:
        return await self._portal_requests(actor, await self._requests(actor))

    async def create_access_request(
        self, actor: Actor, request: AccessRequestCreate
    ) -> PortalAccessRequest:
        created = await self._entitlements.create_catalog_access_request(actor, request)
        return (await self._portal_requests(actor, [created]))[0]

    async def withdraw_access_request(self, actor: Actor, request_id: str) -> PortalAccessRequest:
        withdrawn = await self._entitlements.withdraw_access_request(actor, request_id)
        return (await self._portal_requests(actor, [withdrawn]))[0]

    async def _visible_resource_keys(
        self, actor: Actor, records: GovernedRecords
    ) -> dict[tuple[str, str, str], bool]:
        visible: dict[tuple[str, str, str], bool] = {}
        for item in await self._resolved(actor, records=records):
            resource = item.entitlement.resource
            visible[(str(resource.kind), resource.id, resource.scope_id or "")] = True
        gateways = await records.gateways()
        for model_api in (await records.model_apis()).values():
            if (
                model_api.visibility == CatalogVisibility.CATALOG
                and model_api.gateway_id in gateways
            ):
                visible[("modelApi", model_api.id, "")] = True
        for mcp_server in (await records.mcp_servers()).values():
            if (
                mcp_server.visibility == CatalogVisibility.CATALOG
                and mcp_server.gateway_id in gateways
            ):
                visible[("mcpServer", mcp_server.id, "")] = True
        for pool in (await records.model_pools()).values():
            if pool.gateway_id not in gateways:
                continue
            for model in pool.models:
                if pool_model_listed(pool, model.id):
                    visible[("poolModel", model.id, pool.id)] = True
        return visible

    async def catalog(self, actor: Actor) -> list[CatalogEntry]:
        """Everything an administrator published to the catalog, annotated for this caller.

        A resource an administrator marked ``private`` is omitted entirely rather than shown as
        unavailable, because the point of hiding it is that end users do not know it exists.

        A model API or MCP server MOSAIC publishes is omitted too while its API isn't in API
        Management: before its first apply, after an unpublish, or once its publication is gone.
        The catalog is what a person can ask for and then use, and until an administrator
        publishes it again a request could never give them a working model. That matches how a
        resource whose gateway was removed is left out. A caller who already holds a grant, or has
        asked, still sees it on My access or My requests, marked as no longer available, and
        publishing it again brings it back under the same ID with its grants and catalog settings.

        A pool model is an entry under the same rules: only while an administrator lists it and
        its pool, the pool governs access, and its gateway serves the model.
        """

        records = self._entitlements.governed_records(actor.tenant_id)
        entitled_under: dict[tuple[str, str], set[str]] = {}
        for item in await self._resolved(actor, records=records):
            key = (str(item.entitlement.resource.kind), item.entitlement.resource.id)
            entitled_under.setdefault(key, set()).add(item.entitlement.cost_center_id)
        entitled = set(entitled_under)
        # Only open requests describe the caller's current position. A denied or withdrawn request
        # from last month should not stop them asking again, so it is not surfaced as state here.
        pending = [
            item for item in await self._requests(actor) if item.state == AccessRequestState.PENDING
        ]
        open_requests: dict[tuple[str, str], AccessRequestState] = {
            (str(item.resource.kind), item.resource.id): item.state for item in pending
        }
        requested_under: dict[tuple[str, str], set[str]] = {}
        for request in pending:
            if request.cost_center_id:
                key = (str(request.resource.kind), request.resource.id)
                requested_under.setdefault(key, set()).add(request.cost_center_id)
        gateways = await records.gateways()

        entries: list[CatalogEntry] = []
        for model_api in (await records.model_apis()).values():
            if model_api.visibility != CatalogVisibility.CATALOG:
                continue
            gateway = gateways.get(model_api.gateway_id)
            if gateway is None or not await records.model_api_offered(model_api):
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
                    entitled_cost_center_ids=sorted(
                        entitled_under.get(("modelApi", model_api.id), set())
                    ),
                    requested_cost_center_ids=sorted(
                        requested_under.get(("modelApi", model_api.id), set())
                    ),
                )
            )
        for mcp_server in (await records.mcp_servers()).values():
            if mcp_server.visibility != CatalogVisibility.CATALOG:
                continue
            gateway = gateways.get(mcp_server.gateway_id)
            if gateway is None or not await records.mcp_server_offered(mcp_server):
                continue
            enforced = False
            if mcp_server.publication_id is not None:
                publication = (await records.mcp_publications())[mcp_server.publication_id]
                enforced = (
                    publication.status == PublicationStatus.PUBLISHED
                    and publication.access_state == "applied"
                )
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
                    entitled_cost_center_ids=sorted(
                        entitled_under.get(("mcpServer", mcp_server.id), set())
                    ),
                    requested_cost_center_ids=sorted(
                        requested_under.get(("mcpServer", mcp_server.id), set())
                    ),
                    enforced=enforced,
                )
            )
        inventory: _Inventory = {}
        for pool in (await records.model_pools()).values():
            gateway = gateways.get(pool.gateway_id)
            if gateway is None:
                continue
            for model in pool.models:
                if not pool_model_listed(pool, model.id) or not pool_model_offered(
                    pool, model.id
                ):
                    continue
                key = ("poolModel", model.id)
                entries.append(
                    CatalogEntry(
                        kind=CatalogEntryKind.POOL_MODEL,
                        id=model.id,
                        scope_id=pool.id,
                        display_name=model.display_name,
                        # Never the pool's description: it may name the regions or endpoints
                        # behind the model, which users aren't shown.
                        summary=None,
                        gateway_id=pool.gateway_id,
                        gateway_name=gateway.name,
                        environment=gateway.environment,
                        entitled=key in entitled,
                        request_state=open_requests.get(key),
                        entitled_cost_center_ids=sorted(entitled_under.get(key, set())),
                        requested_cost_center_ids=sorted(requested_under.get(key, set())),
                        api_style=pool.api_shape,
                        capacity=await self._capacity(actor.tenant_id, pool, model, inventory),
                    )
                )
        entries.sort(key=lambda item: (item.display_name.casefold(), item.id))
        return entries

    async def _capacity(
        self, tenant_id: str, pool: ModelPool, model: PoolModel, inventory: _Inventory
    ) -> Literal["provisioned", "payAsYouGo", "provisionedWithOverflow"] | None:
        """The capacity badge a pool model carries, read from inventory as the console reads it.

        None when the pool hides capacity, and when MOSAIC can't tell what one of the model's
        active deployments is, such as one that's no longer observed: no badge rather than a wrong
        one.
        """

        if not pool.show_capacity or self._endpoints is None:
            return None
        kinds: set[CapacityType] = set()
        for member in model.active_members():
            if member.model_endpoint_id not in inventory:
                inventory[member.model_endpoint_id] = await self._deployments(
                    tenant_id, member.model_endpoint_id
                )
            deployments = inventory[member.model_endpoint_id]
            deployment = (
                deployments.get(member.deployment_name.casefold())
                if deployments is not None
                else None
            )
            kinds.add(deployment.capacity_type if deployment is not None else CapacityType.UNKNOWN)
        label = capacity_label(kinds)
        return None if label == "unknown" else label

    async def _deployments(
        self, tenant_id: str, endpoint_id: str
    ) -> dict[str, ObservedModelDeployment] | None:
        assert self._endpoints is not None
        if await self._endpoints.get_endpoint(tenant_id, endpoint_id) is None:
            return None
        observed = await self._endpoints.list_observed_for_endpoint(
            ObservedModelDeployment, tenant_id, endpoint_id, "observedModelDeployment"
        )
        return {item.deployment_name.casefold(): item for item in observed}
