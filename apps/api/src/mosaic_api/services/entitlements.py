"""Entitlement and access-request orchestration.

Cosmos is the source of truth for who may use what and under which limits. This service never
reads API Management to answer that question; it reads observed state only to *infer* the APIM
product or subscription that realizes a grant, because gateway telemetry is keyed on the
subscription and a grant with no binding cannot be joined to a usage row.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import structlog
from pydantic import ValidationError as SchemaValidationError

from mosaic_api.cost_centers import CostCenter, CostCenterBook, may_charge
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestResourceSnapshot,
    AccessRequestState,
    AdminAccessRequestListItem,
    AuditEvent,
    BindingSource,
    CatalogVisibility,
    Entitlement,
    EntitlementBinding,
    EntitlementCreate,
    EntitlementResource,
    EntitlementSubject,
    EntitlementSubjectKind,
    EntitlementUpdate,
    Gateway,
    GrantOverlapReport,
    GrantPath,
    GrantRevocation,
    McpPublication,
    McpServer,
    ModelApi,
    Principal,
    PrincipalCreate,
    PrincipalKind,
    Publication,
    ResolvedEntitlement,
    ResourceSummary,
    deterministic_id,
    entitlement_id,
    grant_precedence_key,
    new_id,
    subject_kind_for,
    utc_now,
)
from mosaic_api.errors import ConflictError, DirectoryError, NotFoundError, ValidationError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.observed import (
    ObservedApimUser,
    ObservedModelDeployment,
    ObservedProduct,
    ObservedSubscription,
)
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.directory import Actor, DirectoryService
from mosaic_api.services.mcp_access import (
    decorate_mcp_entitlement,
    entitlement_mcp_publication,
    mcp_grant_needs_retention,
    mcp_server_offered,
)
from mosaic_api.services.model_access import (
    cost_center_intent,
    decorate_entitlement,
    entitlement_publication,
    managed_grant_needs_retention,
    model_api_offered,
    publication_lock,
)

logger = structlog.get_logger()


@dataclass(frozen=True)
class ResourceDescriptor:
    """A governed resource resolved to something a human can read."""

    kind: str
    id: str
    display_name: str
    gateway_id: str | None = None
    product_names: tuple[str, ...] = ()
    gateway_name: str | None = None
    environment: str | None = None


class GovernedRecords:
    """Gateway, model API, MCP server and publication records, each kind read at most once.

    Naming, summarising, and scoping one list of grants or requests all need the same records.
    Sharing one of these across those lookups keeps a response to one read per kind however many
    lookups it makes, and reading on first use keeps an empty list free. It lives for one response
    only, so it never serves a later response a stale record.
    """

    def __init__(self, repository: GatewayRepository, tenant_id: str) -> None:
        self._repository = repository
        self._tenant_id = tenant_id
        self._gateways: dict[str, Gateway] | None = None
        self._model_apis: dict[str, ModelApi] | None = None
        self._mcp_servers: dict[str, McpServer] | None = None
        self._publications: dict[str, Publication] | None = None
        self._mcp_publications: dict[str, McpPublication] | None = None

    async def gateways(self) -> dict[str, Gateway]:
        if self._gateways is None:
            self._gateways = {
                item.id: item for item in await self._repository.list_gateways(self._tenant_id)
            }
        return self._gateways

    async def model_apis(self) -> dict[str, ModelApi]:
        if self._model_apis is None:
            self._model_apis = {
                item.id: item for item in await self._repository.list_model_apis(self._tenant_id)
            }
        return self._model_apis

    async def mcp_servers(self) -> dict[str, McpServer]:
        if self._mcp_servers is None:
            self._mcp_servers = {
                item.id: item for item in await self._repository.list_mcp_servers(self._tenant_id)
            }
        return self._mcp_servers

    async def publications(self) -> dict[str, Publication]:
        if self._publications is None:
            self._publications = {
                item.id: item
                for item in await self._repository.list_publications(self._tenant_id)
            }
        return self._publications

    async def mcp_publications(self) -> dict[str, McpPublication]:
        if self._mcp_publications is None:
            self._mcp_publications = {
                item.id: item
                for item in await self._repository.list_mcp_publications(self._tenant_id)
            }
        return self._mcp_publications

    async def model_api_offered(self, model_api: ModelApi) -> bool:
        """See :func:`model_api_offered`. Reads publications only for a published model API."""

        if model_api.publication_id is None:
            return True
        return model_api_offered(
            model_api, (await self.publications()).get(model_api.publication_id)
        )

    async def mcp_server_offered(self, server: McpServer) -> bool:
        """See :func:`mcp_server_offered`. Reads publications only for a published MCP server."""

        if server.publication_id is None:
            return True
        return mcp_server_offered(
            server, (await self.mcp_publications()).get(server.publication_id)
        )


def _subscription_owner(subscription: ObservedSubscription) -> str | None:
    """The APIM user name that owns a subscription.

    API Management reports ``ownerId`` as a resource path ending in ``/users/{name}``, while an
    ``ObservedApimUser`` is keyed on the bare name, so the two are only comparable after the path
    is reduced.
    """

    if subscription.owner_label:
        return subscription.owner_label
    if subscription.owner_id:
        return subscription.owner_id.rsplit("/", 1)[-1]
    return None


def _resource_key(resource: EntitlementResource) -> tuple[str, str, str]:
    return (str(resource.kind), resource.id, resource.scope_id or "")


def _grant_key(entitlement: Entitlement) -> tuple[str, str, str, str]:
    """A resource under one cost center: each has one effective grant per caller."""

    return (*_resource_key(entitlement.resource), entitlement.cost_center_id)


def _outranks(item: ResolvedEntitlement, current: ResolvedEntitlement) -> bool:
    """Whether ``item`` decides access to its resource ahead of ``current``.

    An enabled grant beats a disabled one. Then a direct grant beats any group grant, and a
    security-group grant beats a MOSAIC group grant. Among security-group grants the most generous
    wins, and among MOSAIC group grants the lowest ID, so the choice never depends on read order.
    """

    if item.entitlement.enabled != current.entitlement.enabled:
        return item.entitlement.enabled
    if item.via == GrantPath.DIRECT:
        return current.via != GrantPath.DIRECT
    if item.via == GrantPath.SECURITY_GROUP:
        if current.via == GrantPath.GROUP:
            return True
        if current.via == GrantPath.SECURITY_GROUP:
            return grant_precedence_key(
                item.entitlement.enforcement, item.entitlement.id
            ) < grant_precedence_key(current.entitlement.enforcement, current.entitlement.id)
        return False
    return current.via == GrantPath.GROUP and item.entitlement.id < current.entitlement.id


def _environment_confirmation_value(environment: str | None) -> str:
    return environment if environment is not None else "unclassified"


def _subject_for(principal: Principal) -> EntitlementSubject:
    return EntitlementSubject(kind=subject_kind_for(principal.kind), id=principal.id)


def _principal_label(principal: Principal) -> str:
    return principal.label or principal.object_id


def _approved_with_grant(access_request: AccessRequest) -> bool:
    return (
        access_request.state == AccessRequestState.APPROVED
        and access_request.granted_entitlement_id is not None
    )


def _existing_grant_conflict(existing: Entitlement) -> ConflictError:
    # Refused rather than linked. The administrator confirmed limits for a new grant, and linking
    # would silently discard them. Linking could also report a disabled grant as access.
    return ConflictError(
        "The requester already has a direct grant for this resource under this cost center. Deny "
        "this request, or change the existing grant instead.",
        details={
            "entitlementId": existing.id,
            "enabled": existing.enabled,
            "costCenterId": existing.cost_center_id,
        },
    )


def _reject_mcp_token_limits(resource: EntitlementResource, enforcement: Any) -> None:
    if (
        resource.kind == "mcpServer"
        and enforcement is not None
        and enforcement.tokens is not None
    ):
        raise ValidationError(
            "MCP servers are limited by calls, not tokens. Remove the token limits."
        )


class EntitlementService:
    def __init__(
        self,
        repository: EntitlementRepository,
        *,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
        directory_lookup: DirectoryLookup | None = None,
        cost_center_repository: CostCenterRepository | None = None,
    ) -> None:
        self._repository = repository
        self._directory = directory_repository
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._directory_lookup = directory_lookup
        self._cost_centers = cost_center_repository

    def governed_records(self, tenant_id: str) -> GovernedRecords:
        """Records to share across the lookups that serve one response."""

        return GovernedRecords(self._gateways, tenant_id)

    async def cost_center_book(self, tenant_id: str) -> CostCenterBook:
        """Every cost center and the tenant's settings, read once for one operation."""

        return await load_book(self._cost_centers, tenant_id)

    # ------------------------------------------------------------------ cost centers

    async def _group_principal_ids(
        self, actor: Actor, principal: Principal, cost_center: CostCenter
    ) -> list[str]:
        """The security groups a cost center lists that Microsoft Graph says the principal is in.

        Empty when the cost center lists no group, when directory lookup is off, or when Graph
        can't answer: then only a direct membership or a default lets the principal charge it.
        """

        if principal.kind == PrincipalKind.SECURITY_GROUP or self._directory_lookup is None:
            return []
        listed = cost_center.member_ids()
        groups = [
            item
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP and item.id in listed
        ]
        if not groups:
            return []
        try:
            found = {
                object_id.casefold()
                for object_id in await self._directory_lookup.member_groups(
                    principal.object_id, [item.object_id for item in groups]
                )
            }
        except DirectoryError as error:
            logger.warning(
                "cost_center_membership_lookup_failed",
                tenant_id=actor.tenant_id,
                error_code=error.code,
            )
            return []
        return [item.id for item in groups if item.object_id.casefold() in found]

    async def _require_may_charge(
        self,
        actor: Actor,
        subject: EntitlementSubject,
        cost_center: CostCenter,
        book: CostCenterBook,
        *,
        group_principal_ids: list[str] | None = None,
    ) -> None:
        """Refuse a grant to a subject that may not charge its cost center.

        MOSAIC groups aren't principals and never reach the gateway, so they're not checked.
        """

        if subject.kind == EntitlementSubjectKind.GROUP:
            return
        principal = await self._directory.get_principal(actor.tenant_id, subject.id)
        if principal is None:
            return
        groups = group_principal_ids
        if groups is None and not may_charge(
            cost_center, principal=principal, settings=book.settings
        ):
            groups = await self._group_principal_ids(actor, principal, cost_center)
        if may_charge(
            cost_center,
            principal=principal,
            settings=book.settings,
            group_principal_ids=groups or [],
        ):
            return
        raise ValidationError(
            f"{_principal_label(principal)} can't charge {cost_center.name} ({cost_center.code}). "
            "Add them, or a security group they're in, as a member of the cost center first.",
            details={
                "reason": "notACostCenterMember",
                "costCenterId": cost_center.id,
                "subjectId": subject.id,
            },
        )

    def _require_cost_center(self, book: CostCenterBook, cost_center_id: str) -> CostCenter:
        cost_center = book.get(cost_center_id)
        if cost_center is None:
            raise ValidationError(
                "No cost center has that ID", details={"costCenterId": cost_center_id}
            )
        return cost_center

    @staticmethod
    def _audit(
        actor: Actor,
        action: str,
        resource_type: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_object_id=actor.object_id,
            details=details or {},
        )

    # ------------------------------------------------------------------ validation

    async def _validate_subject(self, actor: Actor, subject: EntitlementSubject) -> None:
        if subject.kind == "group":
            if not await self._directory.get_group(actor.tenant_id, subject.id):
                raise ValidationError(
                    "The group named by this entitlement does not exist",
                    details={"subjectId": subject.id},
                )
            return
        principal = await self._directory.get_principal(actor.tenant_id, subject.id)
        if not principal:
            raise ValidationError(
                "The principal named by this entitlement does not exist",
                details={"subjectId": subject.id},
            )
        expected = subject_kind_for(principal.kind)
        if subject.kind != expected:
            raise ValidationError(
                f"This principal is a {principal.kind}, so the entitlement subject must be "
                f"{expected!r}",
                details={"subjectId": subject.id, "principalKind": str(principal.kind)},
            )
        if subject.kind == EntitlementSubjectKind.SECURITY_GROUP and (
            principal.kind != PrincipalKind.SECURITY_GROUP
        ):
            raise ValidationError(
                "A securityGroup entitlement subject must name a securityGroup principal",
                details={"subjectId": subject.id, "principalKind": str(principal.kind)},
            )

    async def _describe_resource(
        self, actor: Actor, resource: EntitlementResource
    ) -> ResourceDescriptor:
        """Resolve a resource reference, failing when it names something MOSAIC does not govern."""

        if resource.kind == "modelApi":
            model_api = await self._gateways.get_model_api(actor.tenant_id, resource.id)
            if model_api:
                return ResourceDescriptor(
                    kind="modelApi",
                    id=model_api.id,
                    display_name=model_api.display_name,
                    gateway_id=model_api.gateway_id,
                    product_names=tuple(model_api.product_names),
                )
        elif resource.kind == "mcpServer":
            mcp_server = await self._gateways.get_mcp_server(actor.tenant_id, resource.id)
            if mcp_server:
                return ResourceDescriptor(
                    kind="mcpServer",
                    id=mcp_server.id,
                    display_name=mcp_server.display_name,
                    gateway_id=mcp_server.gateway_id,
                    product_names=tuple(mcp_server.product_names),
                )
        elif resource.kind == "product":
            scope_id = resource.scope_id or ""
            products = await self._gateways.list_observed(
                ObservedProduct, actor.tenant_id, scope_id, "observedProduct"
            )
            product = next((item for item in products if item.id == resource.id), None)
            if product:
                return ResourceDescriptor(
                    kind="product",
                    id=product.id,
                    display_name=product.display_name,
                    gateway_id=scope_id,
                    product_names=(product.name,),
                )
        elif resource.kind == "modelDeployment":
            scope_id = resource.scope_id or ""
            deployments = await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, actor.tenant_id, scope_id, "observedModelDeployment"
            )
            deployment = next((item for item in deployments if item.id == resource.id), None)
            if deployment:
                return ResourceDescriptor(
                    kind="modelDeployment",
                    id=deployment.id,
                    display_name=deployment.deployment_name,
                )

        raise ValidationError(
            "MOSAIC does not govern the resource named by this entitlement",
            details={"resourceKind": str(resource.kind), "resourceId": resource.id},
        )

    async def _catalog_visible_resource(
        self, tenant_id: str, resource: EntitlementResource
    ) -> ResourceSummary | None:
        """The summary of a model API or MCP server the catalog shows or would show.

        None unless the resource exists, an administrator made it discoverable, and its gateway is
        registered. It may still be unavailable: see :class:`ResourceSummary`.
        """

        if resource.kind not in {"modelApi", "mcpServer"}:
            return None
        summary = await self.resource_summary(tenant_id, resource)
        if summary.gateway_name is None:
            return None
        if resource.kind == "modelApi":
            model_api = await self._gateways.get_model_api(tenant_id, resource.id)
            if model_api is None or model_api.visibility != CatalogVisibility.CATALOG:
                return None
        elif resource.kind == "mcpServer":
            mcp_server = await self._gateways.get_mcp_server(tenant_id, resource.id)
            if mcp_server is None or mcp_server.visibility != CatalogVisibility.CATALOG:
                return None
        return summary

    @staticmethod
    def _snapshot_from_summary(summary: ResourceSummary) -> AccessRequestResourceSnapshot:
        return AccessRequestResourceSnapshot(
            display_name=summary.display_name,
            gateway_id=summary.gateway_id,
            gateway_name=summary.gateway_name,
        )

    @staticmethod
    def _snapshot_summary(
        resource: EntitlementResource,
        *,
        snapshot: AccessRequestResourceSnapshot | None,
        requested_environment: str | None,
    ) -> ResourceSummary:
        return ResourceSummary(
            kind=resource.kind,
            id=resource.id,
            scope_id=resource.scope_id,
            display_name=snapshot.display_name if snapshot else None,
            gateway_id=snapshot.gateway_id if snapshot else None,
            gateway_name=snapshot.gateway_name if snapshot else None,
            environment=requested_environment,
            available=False,
        )

    async def resource_summary(
        self,
        tenant_id: str,
        resource: EntitlementResource,
        *,
        snapshot: AccessRequestResourceSnapshot | None = None,
        requested_environment: str | None = None,
        visible: bool | None = None,
    ) -> ResourceSummary:
        """Build a human-safe summary for one entitlement resource.

        ``visible=False`` is the portal privacy path: live data is deliberately ignored and only
        the request snapshot may be shown. ``visible=None`` is the administrative path and may show
        any live governed resource.
        """

        if visible is False:
            return self._snapshot_summary(
                resource, snapshot=snapshot, requested_environment=requested_environment
            )
        return (
            await self.resource_summaries(
                tenant_id,
                [resource],
                snapshots=[snapshot],
                requested_environments=[requested_environment],
                visible=visible,
            )
        )[0]

    async def resource_summaries(
        self,
        tenant_id: str,
        resources: list[EntitlementResource],
        *,
        snapshots: list[AccessRequestResourceSnapshot | None] | None = None,
        requested_environments: list[str | None] | None = None,
        visible: bool | dict[tuple[str, str, str], bool] | None = None,
        records: GovernedRecords | None = None,
    ) -> list[ResourceSummary]:
        """Build summaries for several resources with shared repository reads.

        Each list is read once, and only for the kinds present, so an empty list reads nothing.
        Pass ``records`` to share those reads with other lookups that serve the same response.
        """

        if not resources:
            return []
        if records is None:
            records = self.governed_records(tenant_id)
        snapshots = snapshots or [None] * len(resources)
        requested_environments = requested_environments or [None] * len(resources)
        kinds = {str(resource.kind) for resource in resources}
        gateways = await records.gateways() if kinds - {"modelDeployment"} else {}
        endpoints = (
            {endpoint.id: endpoint for endpoint in await self._endpoints.list_endpoints(tenant_id)}
            if "modelDeployment" in kinds
            else {}
        )
        model_apis = await records.model_apis() if "modelApi" in kinds else {}
        mcp_servers = await records.mcp_servers() if "mcpServer" in kinds else {}
        product_scopes = {
            resource.scope_id or ""
            for resource in resources
            if resource.kind == "product" and resource.scope_id
        }
        deployment_scopes = {
            resource.scope_id or ""
            for resource in resources
            if resource.kind == "modelDeployment" and resource.scope_id
        }
        products_by_scope: dict[str, dict[str, ObservedProduct]] = {}
        for scope_id in product_scopes:
            products_by_scope[scope_id] = {
                item.id: item
                for item in await self._gateways.list_observed(
                    ObservedProduct, tenant_id, scope_id, "observedProduct"
                )
            }
        deployments_by_scope: dict[str, dict[str, ObservedModelDeployment]] = {}
        for scope_id in deployment_scopes:
            deployments_by_scope[scope_id] = {
                item.id: item
                for item in await self._endpoints.list_observed_for_endpoint(
                    ObservedModelDeployment,
                    tenant_id,
                    scope_id,
                    "observedModelDeployment",
                )
            }

        result: list[ResourceSummary] = []
        for index, resource in enumerate(resources):
            resource_visible = visible
            if isinstance(visible, dict):
                resource_visible = visible.get(_resource_key(resource), False)
            if resource_visible is False:
                result.append(
                    self._snapshot_summary(
                        resource,
                        snapshot=snapshots[index],
                        requested_environment=requested_environments[index],
                    )
                )
                continue

            summary: ResourceSummary | None = None
            if resource.kind == "modelApi":
                model_api = model_apis.get(resource.id)
                gateway = gateways.get(model_api.gateway_id) if model_api else None
                if model_api and gateway:
                    summary = ResourceSummary(
                        kind=resource.kind,
                        id=resource.id,
                        scope_id=resource.scope_id,
                        display_name=model_api.display_name,
                        gateway_id=gateway.id,
                        gateway_name=gateway.name,
                        environment=gateway.environment,
                        available=await records.model_api_offered(model_api),
                    )
                elif model_api:
                    summary = ResourceSummary(
                        kind=resource.kind,
                        id=resource.id,
                        scope_id=resource.scope_id,
                        display_name=model_api.display_name,
                        gateway_id=model_api.gateway_id,
                        available=False,
                    )
            elif resource.kind == "mcpServer":
                mcp_server = mcp_servers.get(resource.id)
                gateway = gateways.get(mcp_server.gateway_id) if mcp_server else None
                if mcp_server and gateway:
                    summary = ResourceSummary(
                        kind=resource.kind,
                        id=resource.id,
                        scope_id=resource.scope_id,
                        display_name=mcp_server.display_name,
                        gateway_id=gateway.id,
                        gateway_name=gateway.name,
                        environment=gateway.environment,
                        available=await records.mcp_server_offered(mcp_server),
                    )
                elif mcp_server:
                    summary = ResourceSummary(
                        kind=resource.kind,
                        id=resource.id,
                        scope_id=resource.scope_id,
                        display_name=mcp_server.display_name,
                        gateway_id=mcp_server.gateway_id,
                        available=False,
                    )
            elif resource.kind == "product":
                scope_id = resource.scope_id or ""
                product = products_by_scope.get(scope_id, {}).get(resource.id)
                gateway = gateways.get(scope_id)
                if product and gateway:
                    summary = ResourceSummary(
                        kind=resource.kind,
                        id=resource.id,
                        scope_id=resource.scope_id,
                        display_name=product.display_name,
                        gateway_id=gateway.id,
                        gateway_name=gateway.name,
                        environment=gateway.environment,
                        available=True,
                    )
            elif resource.kind == "modelDeployment":
                scope_id = resource.scope_id or ""
                deployment = deployments_by_scope.get(scope_id, {}).get(resource.id)
                endpoint = endpoints.get(scope_id)
                if deployment and endpoint:
                    summary = ResourceSummary(
                        kind=resource.kind,
                        id=resource.id,
                        scope_id=resource.scope_id,
                        display_name=f"{deployment.deployment_name} on {endpoint.name}",
                        gateway_id=None,
                        gateway_name=None,
                        environment=endpoint.environment,
                        available=True,
                    )

            if summary is None:
                summary = self._snapshot_summary(
                    resource,
                    snapshot=snapshots[index],
                    requested_environment=requested_environments[index],
                )
            result.append(summary)
        return result

    async def resource_display_names(
        self,
        actor: Actor,
        resources: Sequence[EntitlementResource],
        *,
        records: GovernedRecords | None = None,
    ) -> list[str | None]:
        """Name many resources at once, in the order given, for a list a person will read.

        Each name comes from the record :meth:`_describe_resource` reads for the same reference,
        so it is the name the catalog and the administrator console show. Reads are batched: one
        per desired-state kind, and one per gateway or model endpoint for observed resources,
        however many references there are. Pass ``records`` to share the desired-state reads with
        other lookups that serve the same response. A resource that no longer exists, or whose
        name is blank, is ``None`` rather than an error, because a grant or request can outlive
        the resource it names.

        Only the resources passed in are named, so a caller that passes its own grants and
        requests learns nothing about anything else.
        """

        keys = [_resource_key(resource) for resource in resources]
        kinds = {kind for kind, _, _ in keys}
        if records is None:
            records = self.governed_records(actor.tenant_id)
        # Desired-state records carry their own gateway, so a scope never distinguishes them.
        desired: dict[tuple[str, str], str] = {}
        if "modelApi" in kinds:
            for model_api in (await records.model_apis()).values():
                desired[("modelApi", model_api.id)] = model_api.display_name
        if "mcpServer" in kinds:
            for mcp_server in (await records.mcp_servers()).values():
                desired[("mcpServer", mcp_server.id)] = mcp_server.display_name

        observed: dict[tuple[str, str, str], str] = {}
        for scope_id in {scope for kind, _, scope in keys if kind == "product" and scope}:
            for product in await self._gateways.list_observed(
                ObservedProduct, actor.tenant_id, scope_id, "observedProduct"
            ):
                observed[("product", product.id, scope_id)] = product.display_name
        for scope_id in {scope for kind, _, scope in keys if kind == "modelDeployment" and scope}:
            for deployment in await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, actor.tenant_id, scope_id, "observedModelDeployment"
            ):
                observed[("modelDeployment", deployment.id, scope_id)] = deployment.deployment_name

        names: list[str | None] = []
        for kind, resource_id, scope_id in keys:
            if kind in {"modelApi", "mcpServer"}:
                name = desired.get((kind, resource_id))
            else:
                name = observed.get((kind, resource_id, scope_id))
            names.append(name if name and name.strip() else None)
        return names

    # ------------------------------------------------------------------ binding

    async def infer_binding(
        self, actor: Actor, descriptor: ResourceDescriptor, subject: EntitlementSubject
    ) -> EntitlementBinding | None:
        """Match a grant to the APIM subscription its usage will be logged against.

        Inference is best effort and deliberately conservative: it only claims a subscription when
        exactly one candidate matches, because attributing a user's consumption to the wrong
        subscription is worse than reporting that MOSAIC could not determine it.
        """

        if not descriptor.gateway_id or subject.kind == "group":
            return None
        principal = await self._directory.get_principal(actor.tenant_id, subject.id)
        if not principal:
            return None

        apim_user = await self._find_apim_user(actor, descriptor.gateway_id, principal)
        if not apim_user:
            return None

        subscriptions = await self._gateways.list_observed(
            ObservedSubscription, actor.tenant_id, descriptor.gateway_id, "observedSubscription"
        )
        owned = [
            item for item in subscriptions if _subscription_owner(item) == apim_user.name
        ]
        candidates = [
            item
            for item in owned
            if item.scope_kind == "allApis"
            or (item.scope_kind == "product" and item.scope_name in descriptor.product_names)
        ]
        if len(candidates) != 1:
            return None
        match = candidates[0]
        return EntitlementBinding(
            gateway_id=descriptor.gateway_id,
            apim_product_name=match.scope_name if match.scope_kind == "product" else None,
            apim_subscription_name=match.name,
            source=BindingSource.INFERRED,
            bound_at=utc_now(),
        )

    async def _find_apim_user(
        self, actor: Actor, gateway_id: str, principal: Principal
    ) -> ObservedApimUser | None:
        users = await self._gateways.list_observed(
            ObservedApimUser, actor.tenant_id, gateway_id, "observedApimUser"
        )
        return next(
            (user for user in users if user.entra_object_id == principal.object_id),
            None,
        )

    # ------------------------------------------------------------------ CRUD

    async def _decorate(
        self, entitlement: Entitlement, book: CostCenterBook | None = None
    ) -> Entitlement:
        publication = await entitlement_publication(self._gateways, entitlement)
        mcp_publication = await entitlement_mcp_publication(self._gateways, entitlement)
        principal = (
            await self._directory.get_principal(entitlement.tenant_id, entitlement.subject.id)
            if entitlement.subject.kind != "group"
            else None
        )
        if (publication is not None or mcp_publication is not None) and book is None:
            book = await self.cost_center_book(entitlement.tenant_id)
        intent = cost_center_intent(entitlement, principal, book)
        locked = bool(
            publication
            and await self._gateways.get_publication_lock(
                entitlement.tenant_id, publication.id
            )
        )
        mcp_locked = bool(
            mcp_publication
            and await self._gateways.get_publication_lock(
                entitlement.tenant_id, mcp_publication.id
            )
        )
        if entitlement.resource.kind == "mcpServer":
            return decorate_mcp_entitlement(
                entitlement, mcp_publication, principal, locked=mcp_locked, cost_center=intent
            )
        return decorate_entitlement(
            entitlement, publication, principal, locked=locked, cost_center=intent
        )

    @asynccontextmanager
    async def _mutation(self, entitlement: Entitlement) -> AsyncIterator[None]:
        publication = await entitlement_publication(self._gateways, entitlement)
        mcp_publication = await entitlement_mcp_publication(self._gateways, entitlement)
        target = publication or mcp_publication
        if target is None:
            yield
        else:
            async with publication_lock(self._gateways, entitlement.tenant_id, target.id):
                yield

    async def list_entitlements(
        self,
        actor: Actor,
        *,
        subject_id: str | None = None,
        resource_id: str | None = None,
        cost_center_id: str | None = None,
    ) -> list[Entitlement]:
        records = await self._repository.list_entitlements(
            actor.tenant_id, subject_id=subject_id, resource_id=resource_id
        )
        if cost_center_id is not None:
            records = [item for item in records if item.cost_center_id == cost_center_id]
        book = await self.cost_center_book(actor.tenant_id) if records else None
        return [await self._decorate(record, book) for record in records]

    async def get_entitlement(self, actor: Actor, entitlement_ref: str) -> Entitlement:
        entitlement = await self._repository.get_entitlement(actor.tenant_id, entitlement_ref)
        if not entitlement:
            raise NotFoundError("Entitlement was not found", details={"id": entitlement_ref})
        return await self._decorate(entitlement)

    async def _subject_default(
        self, actor: Actor, subject: EntitlementSubject, book: CostCenterBook
    ) -> str:
        """The cost center a grant made without one is charged to: its subject's default."""

        if subject.kind in {EntitlementSubjectKind.GROUP, EntitlementSubjectKind.SECURITY_GROUP}:
            return book.tenant_default_id
        principal = await self._directory.get_principal(actor.tenant_id, subject.id)
        return book.default_for(principal)

    async def _prepare_entitlement(
        self,
        actor: Actor,
        request: EntitlementCreate,
        descriptor: ResourceDescriptor | None = None,
        *,
        book: CostCenterBook | None = None,
        group_principal_ids: list[str] | None = None,
    ) -> Entitlement:
        """Validate a grant and build its record without writing it.

        Shared by direct creation and by access-request approval, so an approved request's grant
        passes exactly the same subject, resource, cost center, and binding checks as one created
        directly.
        """

        if request.binding and request.binding.source == BindingSource.ORCHESTRATED:
            raise ValidationError("Orchestrated bindings are server-managed")
        await self._validate_subject(actor, request.subject)
        if descriptor is None:
            descriptor = await self._describe_resource(actor, request.resource)
        _reject_mcp_token_limits(request.resource, request.enforcement)
        if book is None:
            book = await self.cost_center_book(actor.tenant_id)
        cost_center_id = request.cost_center_id or await self._subject_default(
            actor, request.subject, book
        )
        cost_center = self._require_cost_center(book, cost_center_id)
        await self._require_may_charge(
            actor,
            request.subject,
            cost_center,
            book,
            group_principal_ids=group_principal_ids,
        )
        binding = request.binding
        if binding is None:
            binding = await self.infer_binding(actor, descriptor, request.subject)
        return Entitlement(
            id=entitlement_id(
                actor.tenant_id, request.subject, request.resource, cost_center.id
            ),
            tenant_id=actor.tenant_id,
            subject=request.subject,
            resource=request.resource,
            cost_center_id=cost_center.id,
            enabled=request.enabled,
            enforcement=request.enforcement,
            binding=binding,
            notes=request.notes,
        )

    async def create_entitlement(self, actor: Actor, request: EntitlementCreate) -> Entitlement:
        record = await self._prepare_entitlement(actor, request)
        async with self._mutation(record):
            await self._validate_subject(actor, request.subject)
            saved = await self._repository.create_entitlement(
                record,
                self._audit(
                    actor,
                    "entitlement.created",
                    "entitlement",
                    record.id,
                    {"costCenterId": record.cost_center_id},
                ),
            )
        saved = await self._revoke_if_ineligible(actor, saved)
        logger.info(
            "entitlement_created",
            entitlement_id=record.id,
            subject_kind=str(request.subject.kind),
            resource_kind=str(request.resource.kind),
            cost_center_id=record.cost_center_id,
            bound=record.binding is not None,
            tenant_id=actor.tenant_id,
        )
        return await self._decorate(saved)

    async def update_entitlement(
        self, actor: Actor, entitlement_ref: str, request: EntitlementUpdate
    ) -> Entitlement:
        entitlement = await self.get_entitlement(actor, entitlement_ref)
        async with self._mutation(entitlement):
            saved = await self._update_entitlement(actor, entitlement_ref, request)
        if entitlement.revocation is not None and saved.revocation is None:
            saved = await self._revoke_if_ineligible(actor, saved)
        return await self._decorate(saved)

    async def _update_entitlement(
        self, actor: Actor, entitlement_ref: str, request: EntitlementUpdate
    ) -> Entitlement:
        entitlement = await self.get_entitlement(actor, entitlement_ref)
        changes = request.model_dump(exclude_unset=True)
        publication = await entitlement_publication(self._gateways, entitlement)
        mcp_publication = await entitlement_mcp_publication(self._gateways, entitlement)
        if "enforcement" in changes:
            _reject_mcp_token_limits(entitlement.resource, request.enforcement)
        if "binding" in changes and (
            (entitlement.binding and entitlement.binding.source == BindingSource.ORCHESTRATED)
            or (
                publication is not None
                and managed_grant_needs_retention(publication, entitlement.id)
            )
            or (
                mcp_publication is not None
                and mcp_grant_needs_retention(mcp_publication, entitlement.id)
            )
            or (request.binding and request.binding.source == BindingSource.ORCHESTRATED)
        ):
            raise ConflictError(
                "This grant's runtime binding is server-managed and cannot be cleared or replaced"
            )
        # ``enabled`` is not nullable on the stored record, so an explicit null means "leave it
        # alone" rather than a validation crash. ``enforcement``, ``binding``, and ``notes`` are
        # nullable and keep their clear-on-null behaviour.
        if changes.get("enabled") is None:
            changes.pop("enabled", None)
        if changes.get("enabled") is True and entitlement.revocation is not None:
            # Revoked when its subject left the cost center: it can come back only once the
            # subject may charge the cost center again.
            book = await self.cost_center_book(actor.tenant_id)
            await self._require_may_charge(
                actor,
                entitlement.subject,
                self._require_cost_center(book, entitlement.cost_center_id),
                book,
            )
            changes["revocation"] = None
        updated = Entitlement.model_validate(
            {
                **entitlement.model_dump(by_alias=False),
                **changes,
                "etag": entitlement.etag,
                "updated_at": utc_now(),
                "runtime": None,
            }
        )
        return await self._repository.save_entitlement(
            updated,
            self._audit(actor, "entitlement.updated", "entitlement", updated.id),
        )

    async def _revoke_if_ineligible(
        self,
        actor: Actor,
        saved: Entitlement,
        *,
        group_principal_ids: list[str] | None = None,
    ) -> Entitlement:
        """Check again, after writing a grant, that its subject may still charge its cost center.

        Removing a member writes the cost center first and then revokes the member's grants. A
        grant written while that runs was either checked against the new member list here, or
        already saved when the removal listed the member's grants, so it can't outlive the
        membership it relied on.
        """

        if saved.subject.kind == EntitlementSubjectKind.GROUP or saved.revocation is not None:
            return saved
        book = await self.cost_center_book(actor.tenant_id)
        cost_center = book.get(saved.cost_center_id)
        if cost_center is not None:
            try:
                await self._require_may_charge(
                    actor,
                    saved.subject,
                    cost_center,
                    book,
                    group_principal_ids=group_principal_ids,
                )
                return saved
            except ValidationError:
                pass
        logger.warning(
            "entitlement_revoked_after_cost_center_change",
            entitlement_id=saved.id,
            cost_center_id=saved.cost_center_id,
            tenant_id=actor.tenant_id,
        )
        return await self.revoke_for_cost_center(actor, saved.id, saved.cost_center_id)

    async def recheck_cost_center(self, actor: Actor, cost_center_id: str) -> list[str]:
        """Revoke direct grants under a cost center whose subjects may no longer charge it.

        Run after a security group leaves the cost center, since people may have charged it only
        through that group. Microsoft Graph says which listed groups each subject is still in.
        Without it, a grant keeps only the groups its approved request recorded from the
        requester's token, and a grant nothing proves eligible is revoked: this fails closed.
        """

        recorded: dict[str, list[str]] = {}
        if self._directory_lookup is None:
            recorded = {
                item.granted_entitlement_id: item.cost_center_group_ids
                for item in await self._repository.list_access_requests(
                    actor.tenant_id, state=str(AccessRequestState.APPROVED)
                )
                if item.granted_entitlement_id
            }
        revoked: list[str] = []
        for grant in await self._repository.list_entitlements(actor.tenant_id):
            if (
                grant.cost_center_id != cost_center_id
                or grant.revocation is not None
                or grant.subject.kind
                not in {EntitlementSubjectKind.USER, EntitlementSubjectKind.APPLICATION}
            ):
                continue
            result = await self._revoke_if_ineligible(
                actor,
                grant,
                group_principal_ids=(
                    None if self._directory_lookup is not None else recorded.get(grant.id, [])
                ),
            )
            if result.revocation is not None:
                revoked.append(grant.id)
        return revoked

    async def revoke_for_cost_center(
        self, actor: Actor, entitlement_ref: str, cost_center_id: str
    ) -> Entitlement:
        """Turn a grant off because its subject left its cost center.

        Saved as desired state under the grant's publication lock, like any grant change. The
        next apply of its model removes its access and deletes its key.
        """

        entitlement = await self.get_entitlement(actor, entitlement_ref)
        async with self._mutation(entitlement):
            current = await self._repository.get_entitlement(actor.tenant_id, entitlement_ref)
            if current is None:
                raise NotFoundError("Entitlement was not found", details={"id": entitlement_ref})
            if current.revocation is not None:
                return await self._decorate(current)
            updated = current.model_copy(
                update={
                    "enabled": False,
                    "revocation": GrantRevocation(
                        cost_center_id=cost_center_id, revoked_by=actor.object_id
                    ),
                    "updated_at": utc_now(),
                    "runtime": None,
                }
            )
            saved = await self._repository.save_entitlement(
                updated,
                self._audit(
                    actor,
                    "entitlement.revoked",
                    "entitlement",
                    current.id,
                    {"reason": "costCenterMembership", "costCenterId": cost_center_id},
                ),
            )
        return await self._decorate(saved)

    async def delete_entitlement(self, actor: Actor, entitlement_ref: str) -> None:
        entitlement = await self.get_entitlement(actor, entitlement_ref)
        async with self._mutation(entitlement):
            entitlement = await self.get_entitlement(actor, entitlement_ref)
            publication = await entitlement_publication(self._gateways, entitlement)
            if publication and managed_grant_needs_retention(publication, entitlement.id):
                raise ConflictError(
                    "This grant still has managed or uncertain runtime access. Disable and apply "
                    "it to revoke access; retain the disabled grant until its publication is "
                    "unpublished so its subscription is not orphaned.",
                    details={"publicationId": publication.id},
                )
            mcp_publication = await entitlement_mcp_publication(self._gateways, entitlement)
            if mcp_publication and mcp_grant_needs_retention(
                mcp_publication, entitlement.id
            ):
                raise ConflictError(
                    "This MCP grant is still applied or its runtime state is uncertain. Disable "
                    "the grant and apply the MCP server's access before deleting it.",
                    details={"publicationId": mcp_publication.id},
                )
            await self._repository.delete_entitlement(
                entitlement,
                self._audit(actor, "entitlement.deleted", "entitlement", entitlement_ref),
            )

    # ------------------------------------------------------------------ resolution

    async def resolve_for_object_id(
        self,
        actor: Actor,
        object_id: str,
        *,
        group_object_ids: frozenset[str] | None = None,
        include_disabled: bool = False,
        records: GovernedRecords | None = None,
    ) -> list[ResolvedEntitlement]:
        """Effective access for an Entra object ID: one grant per resource.

        A principal MOSAIC has never seen has no grants of their own, which is an empty list rather
        than an error: the portal renders that as "nothing has been granted to you yet". They can
        still reach grants made to the security groups in ``group_object_ids``. Disabled grants
        are left out unless ``include_disabled`` is set, as :meth:`resolve_for_principal` explains.
        """

        principal = await self._directory.find_principal_by_object_id(actor.tenant_id, object_id)
        if principal:
            return await self.resolve_for_principal(
                actor,
                principal.id,
                group_object_ids=group_object_ids,
                only_effective=True,
                include_disabled=include_disabled,
                records=records,
            )
        if not group_object_ids:
            return []
        groups = {
            item.id: item
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP
            and item.object_id.casefold() in group_object_ids
        }
        book = await self.cost_center_book(actor.tenant_id)
        winners: dict[tuple[str, str, str, str], ResolvedEntitlement] = {}
        for entitlement in await self._repository.list_entitlements(actor.tenant_id):
            if (
                entitlement.subject.kind != EntitlementSubjectKind.SECURITY_GROUP
                or entitlement.subject.id not in groups
                or not (entitlement.enabled or include_disabled)
            ):
                continue
            item = ResolvedEntitlement(
                entitlement=await self._decorate(entitlement, book),
                via=GrantPath.SECURITY_GROUP,
                via_group_id=groups[entitlement.subject.id].id,
                via_group_name=_principal_label(groups[entitlement.subject.id]),
                effective=entitlement.enabled,
            )
            key = _grant_key(entitlement)
            current = winners.get(key)
            if current is None or _outranks(item, current):
                winners[key] = item
        return await self._with_resource_summaries(
            actor, sorted(winners.values(), key=lambda item: item.entitlement.id), records, book
        )

    async def resolve_for_principal(
        self,
        actor: Actor,
        principal_id: str,
        *,
        group_object_ids: frozenset[str] | None = None,
        only_effective: bool = False,
        include_disabled: bool = False,
        records: GovernedRecords | None = None,
    ) -> list[ResolvedEntitlement]:
        """Every grant that reaches a principal, each marked with whether it decides their access.

        A grant reaches a principal directly, through a MOSAIC group they belong to, or through a
        security group they belong to. One grant per resource and cost center is effective: a
        direct grant wins, then a security-group grant over a MOSAIC group grant, then the most
        generous of several security-group grants. The others are marked with the grant that
        shadows them. Grants under different cost centers don't compete: the caller picks one with
        the cost-center header.

        ``only_effective`` keeps just the grant that decides each resource. Disabled grants never
        decide access; with ``include_disabled`` a resource that only disabled grants cover keeps
        one of them, not effective, so a report can show that the grant exists but is off.
        """

        principal = await self._directory.get_principal(actor.tenant_id, principal_id)
        if principal is None:
            raise NotFoundError("Principal was not found", details={"id": principal_id})
        book = await self.cost_center_book(actor.tenant_id)
        reached: list[ResolvedEntitlement] = []
        for entitlement in await self._repository.list_entitlements(
            actor.tenant_id, subject_id=principal_id
        ):
            if entitlement.subject.kind == subject_kind_for(principal.kind):
                reached.append(
                    ResolvedEntitlement(
                        entitlement=await self._decorate(entitlement, book),
                        via=GrantPath.DIRECT,
                    )
                )

        memberships = await self._directory.list_memberships(
            actor.tenant_id, principal_id=principal_id
        )
        for membership in memberships:
            group = await self._directory.get_group(actor.tenant_id, membership.group_id)
            for entitlement in await self._repository.list_entitlements(
                actor.tenant_id, subject_id=membership.group_id
            ):
                if entitlement.subject.kind != "group":
                    continue
                if not entitlement.enabled and not include_disabled:
                    continue
                reached.append(
                    ResolvedEntitlement(
                        entitlement=entitlement,
                        via=GrantPath.GROUP,
                        via_group_id=membership.group_id,
                        via_group_name=group.name if group else None,
                    )
                )
        security_group_principals = [
            item
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP
        ]
        security_groups_by_principal_id = {item.id: item for item in security_group_principals}
        group_grants = [
            item
            for item in await self._repository.list_entitlements(actor.tenant_id)
            if item.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
            and item.subject.id in security_groups_by_principal_id
        ]
        if principal.kind != PrincipalKind.SECURITY_GROUP:
            matched_group_object_ids = group_object_ids
            if matched_group_object_ids is None and group_grants:
                if self._directory_lookup is None:
                    logger.warning(
                        "security_group_resolution_skipped",
                        tenant_id=actor.tenant_id,
                        principal_id=principal_id,
                        reason="directory_lookup_not_configured",
                    )
                    matched_group_object_ids = frozenset()
                else:
                    try:
                        matched_group_object_ids = frozenset(
                            object_id.casefold()
                            for object_id in await self._directory_lookup.member_groups(
                                principal.object_id,
                                [
                                    security_groups_by_principal_id[
                                        entitlement.subject.id
                                    ].object_id
                                    for entitlement in group_grants
                                ],
                            )
                        )
                    except DirectoryError as error:
                        logger.warning(
                            "security_group_resolution_failed",
                            tenant_id=actor.tenant_id,
                            principal_id=principal_id,
                            error_code=error.code,
                        )
                        matched_group_object_ids = frozenset()
            matched_group_object_ids = matched_group_object_ids or frozenset()
            for entitlement in group_grants:
                security_group = security_groups_by_principal_id[entitlement.subject.id]
                if security_group.object_id.casefold() not in matched_group_object_ids:
                    continue
                reached.append(
                    ResolvedEntitlement(
                        entitlement=await self._decorate(entitlement, book),
                        via=GrantPath.SECURITY_GROUP,
                        via_group_id=security_group.id,
                        via_group_name=_principal_label(security_group),
                    )
                )

        winners: dict[tuple[str, str, str, str], ResolvedEntitlement] = {}
        for item in reached:
            if not item.entitlement.enabled and not include_disabled:
                continue
            key = _grant_key(item.entitlement)
            current = winners.get(key)
            if current is None or _outranks(item, current):
                winners[key] = item

        resolved: list[ResolvedEntitlement] = []
        for item in reached:
            winner = winners.get(_grant_key(item.entitlement))
            decides = winner is not None and winner.entitlement.id == item.entitlement.id
            if only_effective and not decides:
                continue
            effective = item.entitlement.enabled and decides
            resolved.append(
                item.model_copy(
                    update={
                        "effective": effective,
                        "shadowed_by": None
                        if effective or not item.entitlement.enabled or winner is None
                        else winner.entitlement.id,
                    }
                )
            )
        return await self._with_resource_summaries(
            actor, sorted(resolved, key=lambda item: item.entitlement.id), records, book
        )

    async def _with_resource_summaries(
        self,
        actor: Actor,
        items: list[ResolvedEntitlement],
        records: GovernedRecords | None,
        book: CostCenterBook | None = None,
    ) -> list[ResolvedEntitlement]:
        summaries = await self.resource_summaries(
            actor.tenant_id,
            [item.entitlement.resource for item in items],
            visible=True,
            records=records,
        )
        if book is None and items:
            book = await self.cost_center_book(actor.tenant_id)
        return [
            item.model_copy(
                update={
                    "resource_summary": summaries[index],
                    "cost_center": book.ref(item.entitlement.cost_center_id) if book else None,
                }
            )
            for index, item in enumerate(items)
        ]

    async def list_overlaps(
        self, actor: Actor, resource_id: str | None = None
    ) -> GrantOverlapReport:
        from mosaic_api.services.overlaps import GrantOverlapService

        return await GrantOverlapService(
            self._repository,
            directory_repository=self._directory,
            gateway_repository=self._gateways,
            directory_lookup=self._directory_lookup,
        ).list_overlaps(actor, resource_id=resource_id)

    # ------------------------------------------------------------------ access requests

    async def list_access_requests(
        self,
        actor: Actor,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
    ) -> list[AccessRequest]:
        return await self._repository.list_access_requests(
            actor.tenant_id, requester_object_id=requester_object_id, state=state
        )

    async def list_access_request_items(
        self,
        actor: Actor,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
    ) -> list[AdminAccessRequestListItem]:
        requests = await self.list_access_requests(
            actor, requester_object_id=requester_object_id, state=state
        )
        summaries = await self.resource_summaries(
            actor.tenant_id,
            [item.resource for item in requests],
            snapshots=[item.resource_snapshot for item in requests],
            requested_environments=[item.requested_environment for item in requests],
        )
        book = await self.cost_center_book(actor.tenant_id) if requests else None
        return [
            AdminAccessRequestListItem.model_validate(
                {
                    **item.model_dump(by_alias=False),
                    "resource_summary": summaries[index],
                    "cost_center": book.ref(item.cost_center_id) if book else None,
                }
            )
            for index, item in enumerate(requests)
        ]

    async def caller_group_principal_ids(self, actor: Actor) -> list[str]:
        """The security groups MOSAIC records that the caller's own token says they're in."""

        if not actor.group_ids:
            return []
        return [
            item.id
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP
            and item.object_id.casefold() in actor.group_ids
        ]

    async def get_access_request(self, actor: Actor, request_id: str) -> AccessRequest:
        access_request = await self._repository.get_access_request(actor.tenant_id, request_id)
        if not access_request:
            raise NotFoundError("Access request was not found", details={"id": request_id})
        return access_request

    async def create_access_request(
        self,
        actor: Actor,
        request: AccessRequestCreate,
        *,
        summary: ResourceSummary | None = None,
    ) -> AccessRequest:
        descriptor = await self._describe_resource(actor, request.resource)
        if summary is None:
            summary = await self.resource_summary(actor.tenant_id, request.resource)
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        book = await self.cost_center_book(actor.tenant_id)
        cost_center_id = request.cost_center_id or book.default_for(principal)
        cost_center = book.get(cost_center_id)
        groups = await self.caller_group_principal_ids(actor)
        # Only the caller's own token decides which groups they're in, so a person can name only
        # a cost center they may charge.
        if cost_center is None or not may_charge(
            cost_center,
            principal=principal,
            settings=book.settings,
            group_principal_ids=groups,
        ):
            raise ValidationError(
                "You can't charge that cost center. Choose one of yours.",
                details={"reason": "notACostCenterMember", "costCenterId": cost_center_id},
            )
        if principal is not None:
            existing = await self._repository.get_entitlement(
                actor.tenant_id,
                entitlement_id(
                    actor.tenant_id, _subject_for(principal), request.resource, cost_center.id
                ),
            )
            if existing is not None and existing.enabled:
                raise ConflictError(
                    f"You already have access to this under {cost_center.name}.",
                    details={"reason": "alreadyGranted", "costCenterId": cost_center.id},
                )
        default_id = book.default_for(principal)
        open_requests = [
            item
            for item in await self._repository.list_access_requests(
                actor.tenant_id, requester_object_id=actor.object_id, state="pending"
            )
            if _resource_key(item.resource) == _resource_key(request.resource)
            and (item.cost_center_id or default_id) == cost_center.id
        ]
        if open_requests:
            raise ConflictError(
                "You already have an open request for this resource under this cost center",
                details={"id": open_requests[0].id, "costCenterId": cost_center.id},
            )
        record = AccessRequest(
            id=deterministic_id(
                "accessRequest",
                actor.tenant_id,
                actor.object_id,
                descriptor.id,
                cost_center.id,
                utc_now().isoformat(),
            ),
            tenant_id=actor.tenant_id,
            requester_object_id=actor.object_id,
            requester_principal_id=principal.id if principal else None,
            resource=request.resource,
            cost_center_id=cost_center.id,
            cost_center_group_ids=sorted(set(groups) & cost_center.member_ids()),
            justification=request.justification,
            requested_environment=summary.environment,
            resource_snapshot=self._snapshot_from_summary(summary),
        )
        return await self._repository.create_access_request(
            record,
            self._audit(
                actor,
                "accessRequest.created",
                "accessRequest",
                record.id,
                {
                    "requestedEnvironment": record.requested_environment,
                    "costCenterId": cost_center.id,
                },
            ),
        )

    async def create_catalog_access_request(
        self, actor: Actor, request: AccessRequestCreate
    ) -> AccessRequest:
        # Only what the caller's catalog shows may be requested, and a private, unknown, product,
        # or deployment resource all get the same answer, so a guessed ID confirms nothing.
        summary = await self._catalog_visible_resource(actor.tenant_id, request.resource)
        if summary is None:
            raise NotFoundError("That resource isn't in your catalog.")
        # A discoverable model or MCP server whose API isn't in API Management right now: the
        # catalog leaves it out, and a request made from a page loaded earlier gets the reason.
        if not summary.available:
            raise ConflictError(
                f"{summary.display_name or 'This resource'} isn't published right now, so MOSAIC "
                "can't take access requests for it. Try again once an administrator publishes it.",
                details={
                    "reason": "notPublished",
                    "resourceKind": str(request.resource.kind),
                    "resourceId": request.resource.id,
                },
            )
        return await self.create_access_request(actor, request, summary=summary)

    async def decide_access_request(
        self,
        actor: Actor,
        request_id: str,
        *,
        state: AccessRequestState,
        note: str | None = None,
    ) -> AccessRequest:
        """Close a pending request without granting anything: deny or withdraw it.

        Approval goes through :meth:`approve_access_request` instead. That is the only path that
        writes the grant together with the decision, so an approved request cannot exist
        without its grant.
        """

        if state not in {AccessRequestState.DENIED, AccessRequestState.WITHDRAWN}:
            raise ValueError("Only a denial or withdrawal closes a request without a grant")
        access_request = await self.get_access_request(actor, request_id)
        if access_request.state != AccessRequestState.PENDING:
            raise ConflictError(
                f"This request was already {access_request.state}",
                details={"id": request_id, "state": str(access_request.state)},
            )
        updated = AccessRequest.model_validate(
            {
                **access_request.model_dump(by_alias=False),
                "state": state,
                "decided_by_object_id": actor.object_id,
                "decided_at": utc_now(),
                "decision_note": note,
                "etag": access_request.etag,
                "updated_at": utc_now(),
            }
        )
        return await self._repository.save_access_request(
            updated,
            self._audit(actor, f"accessRequest.{state}", "accessRequest", request_id),
        )

    async def approve_access_request(
        self, actor: Actor, request_id: str, approval: AccessRequestApproval
    ) -> AccessRequest:
        """Approve a pending request by creating the requester's grant and linking it.

        The grant is desired state. API Management is unchanged until its model plan is reviewed
        and applied. The grant and the decision are written in one atomic repository call, so
        neither exists without the other. A requester MOSAIC has never registered becomes a
        ``user`` principal first. That registration is idempotent and grants nothing on its
        own, so it is safe to leave in place if the approval then fails.

        Approving an already approved request returns it unchanged. That makes a retry after an
        ambiguous failure safe, and the first decision's limits stand.
        """

        access_request = await self.get_access_request(actor, request_id)
        if _approved_with_grant(access_request):
            return access_request
        self._require_pending(access_request)
        descriptor = await self._describe_resource(actor, access_request.resource)
        await self._require_environment_confirmation(actor, access_request, approval)

        principal = await self._requester_principal(actor, access_request.requester_object_id)
        book = await self.cost_center_book(actor.tenant_id)
        cost_center = self._require_cost_center(
            book,
            approval.cost_center_id
            or access_request.cost_center_id
            or book.default_for(principal),
        )
        if principal is not None:
            existing = await self._repository.get_entitlement(
                actor.tenant_id,
                entitlement_id(
                    actor.tenant_id,
                    _subject_for(principal),
                    access_request.resource,
                    cost_center.id,
                ),
            )
            if existing is not None:
                raise _existing_grant_conflict(existing)
        principal_created = principal is None
        if principal is None:
            principal = await self._register_requester(actor, access_request.requester_object_id)

        # The requester's token showed which of the cost center's groups they were in when they
        # asked. Without Microsoft Graph to check again, that's the only evidence there is, and it
        # counts only for the cost center they chose and only while it still lists those groups.
        trusted_groups = (
            sorted(set(access_request.cost_center_group_ids) & cost_center.member_ids())
            if self._directory_lookup is None and cost_center.id == access_request.cost_center_id
            else None
        )
        subject = _subject_for(principal)
        entitlement = await self._prepare_entitlement(
            actor,
            EntitlementCreate(
                subject=subject,
                resource=access_request.resource,
                cost_center_id=cost_center.id,
                enforcement=approval.enforcement,
            ),
            descriptor,
            book=book,
            group_principal_ids=trusted_groups,
        )
        decided_at = utc_now()
        approved = AccessRequest.model_validate(
            {
                **access_request.model_dump(by_alias=False),
                "state": AccessRequestState.APPROVED,
                "requester_principal_id": principal.id,
                "decided_by_object_id": actor.object_id,
                "decided_at": decided_at,
                "decision_note": approval.note,
                "granted_entitlement_id": entitlement.id,
                "etag": access_request.etag,
                "updated_at": decided_at,
            }
        )
        audit_events = [
            self._audit(
                actor,
                "entitlement.created",
                "entitlement",
                entitlement.id,
                {"accessRequestId": request_id, "costCenterId": entitlement.cost_center_id},
            ),
            self._audit(
                actor,
                "accessRequest.approved",
                "accessRequest",
                request_id,
                {
                    "grantedEntitlementId": entitlement.id,
                    "principalId": principal.id,
                    "principalCreated": principal_created,
                    "costCenterId": entitlement.cost_center_id,
                    "requestedCostCenterId": access_request.cost_center_id,
                },
            ),
        ]
        try:
            async with self._mutation(entitlement):
                await self._validate_subject(actor, subject)
                saved = await self._repository.approve_access_request(
                    approved, entitlement, audit_events
                )
        except ConflictError:
            # Nothing was written. Work out why, so a concurrent approval converges on its
            # result instead of failing, and any other conflict is reported precisely.
            current = await self.get_access_request(actor, request_id)
            if _approved_with_grant(current):
                return current
            self._require_pending(current)
            existing = await self._repository.get_entitlement(actor.tenant_id, entitlement.id)
            if existing is not None:
                raise _existing_grant_conflict(existing) from None
            raise
        await self._revoke_if_ineligible(actor, entitlement, group_principal_ids=trusted_groups)
        logger.info(
            "access_request_approved",
            access_request_id=request_id,
            entitlement_id=entitlement.id,
            principal_id=principal.id,
            principal_created=principal_created,
            resource_kind=str(access_request.resource.kind),
            limited=approval.enforcement is not None,
            tenant_id=actor.tenant_id,
        )
        return saved

    async def _require_environment_confirmation(
        self,
        actor: Actor,
        access_request: AccessRequest,
        approval: AccessRequestApproval,
    ) -> None:
        summary = await self.resource_summary(actor.tenant_id, access_request.resource)
        current_environment = summary.environment
        requested_environment = access_request.requested_environment
        confirmed = approval.confirmed_environment
        if confirmed is not None and confirmed != _environment_confirmation_value(
            current_environment
        ):
            raise ConflictError(
                "The resource's environment changed. Confirm the current environment to approve.",
                details={
                    "reason": "environmentChanged",
                    "requestedEnvironment": requested_environment,
                    "currentEnvironment": current_environment,
                },
            )
        if current_environment != requested_environment and confirmed is None:
            raise ConflictError(
                "The resource's environment changed. Confirm the current environment to approve.",
                details={
                    "reason": "environmentChanged",
                    "requestedEnvironment": requested_environment,
                    "currentEnvironment": current_environment,
                },
            )

    @staticmethod
    def _require_pending(access_request: AccessRequest) -> None:
        if access_request.state == AccessRequestState.PENDING:
            return
        if access_request.state == AccessRequestState.APPROVED:
            raise ConflictError(
                "This request was approved before approval created grants, so no grant is linked "
                "to it. Grant access from Entitlements, or ask the requester to submit a new "
                "request.",
                details={"id": access_request.id, "state": str(access_request.state)},
            )
        raise ConflictError(
            f"This request was already {access_request.state}",
            details={"id": access_request.id, "state": str(access_request.state)},
        )

    async def _requester_principal(self, actor: Actor, object_id: str) -> Principal | None:
        principal = await self._directory.find_principal_by_object_id(actor.tenant_id, object_id)
        if principal is None:
            # Principal IDs derive from the case-folded object ID. That also finds a principal
            # an administrator registered with different letter case, which would otherwise
            # collide with the one registration would create.
            principal = await self._directory.get_principal(
                actor.tenant_id, deterministic_id("principal", actor.tenant_id, object_id)
            )
        return principal

    async def _register_requester(self, actor: Actor, object_id: str) -> Principal:
        try:
            request = PrincipalCreate(object_id=object_id, kind=PrincipalKind.USER)
        except SchemaValidationError as exc:
            raise ValidationError(
                "The requester's Entra object ID cannot be registered as a principal",
                details={"objectId": object_id},
            ) from exc
        try:
            return await DirectoryService(
                self._directory, cost_center_repository=self._cost_centers
            ).create_principal(actor, request)
        except ConflictError:
            # A concurrent approval for the same person registered them first.
            principal = await self._requester_principal(actor, object_id)
            if principal is None:
                raise
            return principal

    async def withdraw_access_request(self, actor: Actor, request_id: str) -> AccessRequest:
        access_request = await self.get_access_request(actor, request_id)
        if access_request.requester_object_id != actor.object_id:
            raise NotFoundError("Access request was not found", details={"id": request_id})
        return await self.decide_access_request(
            actor, request_id, state=AccessRequestState.WITHDRAWN
        )
