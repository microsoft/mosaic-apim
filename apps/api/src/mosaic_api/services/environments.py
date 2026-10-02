from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from mosaic_api.domain import (
    AuditEvent,
    Entitlement,
    EntitlementResourceKind,
    Gateway,
    McpEndpoint,
    ModelEndpoint,
    new_id,
    utc_now,
)
from mosaic_api.environments import (
    BlockedPublication,
    EnvironmentAssignmentOutcome,
    EnvironmentAssignmentRequest,
    EnvironmentAssignmentResult,
    EnvironmentCatalog,
    EnvironmentCatalogView,
    EnvironmentCreate,
    EnvironmentDefinition,
    EnvironmentResourceKind,
    EnvironmentSettingsUpdate,
    EnvironmentSuggestion,
    EnvironmentSuggestionList,
    EnvironmentUpdate,
    EnvironmentUsage,
    EnvironmentWithUsage,
    PortalEnvironment,
    VerdictLevel,
    compatibility_matrix,
    is_production,
    permits,
    publications_blocked_error,
    sorted_environments,
    suggest_environment,
    validate_catalog,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.observed import ObservedModelDeployment, ObservedProduct
from mosaic_api.repositories import (
    DirectoryRepository,
    EntitlementRepository,
    EnvironmentRepository,
    GatewayRepository,
    McpEndpointRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import ENVIRONMENT_BUSY_MESSAGE, environment_guard

ResourceOverride = Mapping[tuple[str, str], str | None]
AssignmentResource = Gateway | ModelEndpoint | McpEndpoint
ResourceKind = EnvironmentResourceKind
PublicationFilter = Callable[[str, ResourceKind, str], bool]
ASSIGNMENT_SCOPE_ATTEMPTS = 3
PUBLICATION_BUSY_MESSAGE = (
    "A publication affected by this change is being applied. Try again when it finishes."
)


@dataclass(frozen=True)
class AssignmentRecord:
    kind: ResourceKind
    resource: AssignmentResource
    target: str | None
    previous: str | None


@dataclass(frozen=True)
class AssignmentScopes:
    """What an assignment batch must hold: the resources it changes and their publications."""

    gateway_ids: frozenset[str]
    endpoint_ids: frozenset[str]
    publication_ids: frozenset[str]

    def within(self, other: "AssignmentScopes") -> bool:
        return (
            self.gateway_ids <= other.gateway_ids
            and self.endpoint_ids <= other.endpoint_ids
            and self.publication_ids <= other.publication_ids
        )


@dataclass(frozen=True)
class AffectedGrant:
    entitlement: Entitlement
    moved_kind: ResourceKind
    moved_id: str
    moved_name: str
    from_environment: str | None
    to_environment: str | None
    resource_name: str | None


async def load_environment_catalog(
    repository: EnvironmentRepository | None, tenant_id: str
) -> EnvironmentCatalog:
    """The tenant's catalog, or the built-in seeds until an administrator first saves one."""

    if repository is not None:
        catalog = await repository.get_environment_catalog(tenant_id)
        if catalog is not None:
            return catalog
    return EnvironmentCatalog.new(tenant_id)


class EnvironmentService:
    def __init__(
        self,
        repository: EnvironmentRepository,
        *,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
        mcp_repository: McpEndpointRepository,
        entitlement_repository: EntitlementRepository | None = None,
        directory_repository: DirectoryRepository | None = None,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._mcp = mcp_repository
        self._entitlements = entitlement_repository
        self._directory = directory_repository

    @staticmethod
    def _audit_event(
        actor: Actor,
        action: str,
        catalog: EnvironmentCatalog,
        *,
        resource_type: str = "environmentCatalog",
        resource_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id or catalog.id,
            actor_object_id=actor.object_id,
            details=details or {},
        )

    async def catalog(self, actor: Actor) -> EnvironmentCatalog:
        return await self.catalog_for_tenant(actor.tenant_id)

    async def catalog_for_tenant(self, tenant_id: str) -> EnvironmentCatalog:
        return await load_environment_catalog(self._repository, tenant_id)

    async def _catalog_for_tenant(self, tenant_id: str) -> EnvironmentCatalog:
        return await self.catalog_for_tenant(tenant_id)

    async def view(self, actor: Actor) -> EnvironmentCatalogView:
        catalog = await self.catalog(actor)
        return await self._view(catalog, saved=catalog.etag is not None)

    async def _view(self, catalog: EnvironmentCatalog, *, saved: bool) -> EnvironmentCatalogView:
        counts = await self._repository.count_resources_by_environment(catalog.tenant_id)
        environments = [
            EnvironmentWithUsage.model_validate(
                {
                    **environment.model_dump(),
                    "usage": counts.get(environment.key, EnvironmentUsage()),
                }
            )
            for environment in sorted_environments(catalog)
        ]
        return EnvironmentCatalogView(
            environments=environments,
            require_classification=catalog.require_classification,
            unclassified=counts.get(None, EnvironmentUsage()),
            compatibility=compatibility_matrix(catalog),
            updated_at=catalog.updated_at if saved else None,
        )

    async def portal_environments(self, actor: Actor) -> list[PortalEnvironment]:
        catalog = await self.catalog(actor)
        return [
            PortalEnvironment(
                key=environment.key,
                display_name=environment.display_name,
                description=environment.description,
                color=environment.color,
                production=environment.production,
                order=environment.order,
            )
            for environment in sorted_environments(catalog)
        ]

    async def suggestions(self, actor: Actor) -> EnvironmentSuggestionList:
        catalog = await self.catalog(actor)
        items: list[EnvironmentSuggestion] = []
        for gateway in await self._gateways.list_gateways(actor.tenant_id):
            if gateway.environment is None:
                items.append(self._suggestion(catalog, "gateway", gateway))
        for endpoint in await self._endpoints.list_endpoints(actor.tenant_id):
            if endpoint.environment is None:
                items.append(self._suggestion(catalog, "modelEndpoint", endpoint))
        for mcp_endpoint in await self._mcp.list_endpoints(actor.tenant_id):
            if mcp_endpoint.environment is None:
                items.append(self._suggestion(catalog, "mcpEndpoint", mcp_endpoint))
        kind_order = {"gateway": 0, "modelEndpoint": 1, "mcpEndpoint": 2}
        items.sort(
            key=lambda item: (
                kind_order[item.resource_kind],
                item.resource_name.casefold(),
            )
        )
        return EnvironmentSuggestionList(items=items)

    def _suggestion(
        self, catalog: EnvironmentCatalog, kind: ResourceKind, resource: AssignmentResource
    ) -> EnvironmentSuggestion:
        azure_value = (
            resource.azure_environment_tag
            if isinstance(resource, Gateway | ModelEndpoint)
            else None
        )
        if azure_value is not None:
            suggested = suggest_environment(catalog, azure_value)
            if suggested is not None:
                return EnvironmentSuggestion(
                    resource_kind=kind,
                    resource_id=resource.id,
                    resource_name=resource.name,
                    suggested_environment=suggested.key,
                    source="azureTag",
                    evidence=f'Azure tag environment = "{azure_value}"',
                )
        legacy = resource.environment_label
        suggested = suggest_environment(catalog, legacy)
        if suggested is not None:
            return EnvironmentSuggestion(
                resource_kind=kind,
                resource_id=resource.id,
                resource_name=resource.name,
                suggested_environment=suggested.key,
                source="legacyLabel",
                evidence=f'Legacy label "{legacy}"',
            )
        return EnvironmentSuggestion(
            resource_kind=kind,
            resource_id=resource.id,
            resource_name=resource.name,
        )

    async def assign_environments(
        self, actor: Actor, request: EnvironmentAssignmentRequest
    ) -> EnvironmentAssignmentResult:
        if not 1 <= len(request.assignments) <= 200:
            raise ValidationError(
                "Assignments must include 1 to 200 resources",
                details={"reason": "invalidAssignment"},
            )
        seen: set[tuple[str, str]] = set()
        for assignment in request.assignments:
            key = (assignment.resource_kind, assignment.resource_id)
            if key in seen:
                raise ValidationError(
                    "Each resource can be assigned at most once",
                    details={"reason": "invalidAssignment"},
                )
            seen.add(key)

        preliminary = await self._load_assignment_records(actor.tenant_id, request)
        self._validate_assignment_keys(await self.catalog(actor), request)
        for _ in range(ASSIGNMENT_SCOPE_ATTEMPTS):
            scopes = await self._assignment_scopes(actor.tenant_id, preliminary)
            try:
                async with environment_guard(
                    self._gateways,
                    actor.tenant_id,
                    environments=True,
                    gateway_ids=scopes.gateway_ids,
                    endpoint_ids=scopes.endpoint_ids,
                    publication_ids=scopes.publication_ids,
                ):
                    # The scopes were chosen from a read made before they were held. Re-read under
                    # them, and start over if a change that landed in between widened what this
                    # batch touches, so every publication it could affect is locked.
                    records = await self._load_assignment_records(actor.tenant_id, request)
                    if not (await self._assignment_scopes(actor.tenant_id, records)).within(
                        scopes
                    ):
                        preliminary = records
                        continue
                    catalog = await self.catalog(actor)
                    self._validate_assignment_keys(catalog, request)
                    return await self._assign_locked(
                        actor, catalog, records, request.acknowledge_grants
                    )
            except ConflictError as exc:
                if exc.details.get("id") in scopes.publication_ids:
                    raise ConflictError(PUBLICATION_BUSY_MESSAGE) from exc
                raise
        raise ConflictError(ENVIRONMENT_BUSY_MESSAGE)

    async def _assignment_scopes(
        self, tenant_id: str, records: Sequence[AssignmentRecord]
    ) -> AssignmentScopes:
        changing = [record for record in records if record.previous != record.target]
        gateway_ids = frozenset(
            record.resource.id for record in changing if record.kind == "gateway"
        )
        endpoint_ids = frozenset(
            record.resource.id for record in changing if record.kind == "modelEndpoint"
        )
        mcp_endpoint_ids = frozenset(
            record.resource.id for record in changing if record.kind == "mcpEndpoint"
        )
        # Drafts are included: a draft left blocked is allowed, but only while its lock keeps a
        # concurrent apply from turning it into an applied publication mid-change.
        model_publication_ids = (
            frozenset(
                publication.id
                for publication in await self._gateways.list_publications(tenant_id)
                if publication.gateway_id in gateway_ids
                or publication.model_endpoint_id in endpoint_ids
            )
            if gateway_ids or endpoint_ids
            else frozenset()
        )
        mcp_publication_ids = (
            frozenset(
                publication.id
                for publication in await self._gateways.list_mcp_publications(tenant_id)
                if publication.gateway_id in gateway_ids
                or publication.mcp_endpoint_id in mcp_endpoint_ids
            )
            if gateway_ids or mcp_endpoint_ids
            else frozenset()
        )
        pool_ids = (
            frozenset(
                pool.id
                for pool in await self._gateways.list_model_pools(tenant_id)
                if pool.gateway_id in gateway_ids
                or not pool.member_endpoint_ids().isdisjoint(endpoint_ids)
            )
            if gateway_ids or endpoint_ids
            else frozenset()
        )
        return AssignmentScopes(
            gateway_ids, endpoint_ids, model_publication_ids | mcp_publication_ids | pool_ids
        )

    def _validate_assignment_keys(
        self, catalog: EnvironmentCatalog, request: EnvironmentAssignmentRequest
    ) -> None:
        known = {environment.key for environment in catalog.environments}
        for assignment in request.assignments:
            if assignment.environment is not None and assignment.environment not in known:
                raise ValidationError(
                    "The requested environment is not defined",
                    details={
                        "reason": "unknownEnvironment",
                        "environment": assignment.environment,
                    },
                )

    async def _load_assignment_records(
        self, tenant_id: str, request: EnvironmentAssignmentRequest
    ) -> list[AssignmentRecord]:
        records: list[AssignmentRecord] = []
        for assignment in request.assignments:
            resource: AssignmentResource | None
            if assignment.resource_kind == "gateway":
                resource = await self._gateways.get_gateway(tenant_id, assignment.resource_id)
            elif assignment.resource_kind == "modelEndpoint":
                resource = await self._endpoints.get_endpoint(tenant_id, assignment.resource_id)
            else:
                resource = await self._mcp.get_endpoint(tenant_id, assignment.resource_id)
            if resource is None:
                raise NotFoundError(
                    "Resource was not found",
                    details={
                        "reason": "resourceNotFound",
                        "resourceKind": assignment.resource_kind,
                        "resourceId": assignment.resource_id,
                    },
                )
            records.append(
                AssignmentRecord(
                    kind=assignment.resource_kind,
                    resource=resource,
                    target=assignment.environment,
                    previous=resource.environment,
                )
            )
        return records

    async def _assign_locked(
        self,
        actor: Actor,
        catalog: EnvironmentCatalog,
        records: list[AssignmentRecord],
        acknowledge_grants: bool,
    ) -> EnvironmentAssignmentResult:
        changes = [record for record in records if record.previous != record.target]
        overrides: dict[tuple[str, str], str | None] = {
            (record.kind, record.resource.id): record.target for record in changes
        }
        assigned_gateways = {record.resource.id for record in records if record.kind == "gateway"}
        assigned_model_endpoints = {
            record.resource.id for record in records if record.kind == "modelEndpoint"
        }
        assigned_mcp_endpoints = {
            record.resource.id for record in records if record.kind == "mcpEndpoint"
        }
        blocked = await self.blocked_publications(
            actor.tenant_id,
            catalog=catalog,
            overrides=overrides,
            publication_filter=lambda gateway_id, endpoint_kind, endpoint_id: (
                gateway_id in assigned_gateways
                or (endpoint_kind == "modelEndpoint" and endpoint_id in assigned_model_endpoints)
                or (endpoint_kind == "mcpEndpoint" and endpoint_id in assigned_mcp_endpoints)
            ),
        )
        if blocked:
            raise publications_blocked_error(
                blocked,
                suggested_assignments=self._suggested_assignments(blocked, changes),
            )

        affected_grants = await self._affected_grants(actor.tenant_id, catalog, changes)
        crossing = [
            grant
            for grant in affected_grants
            if is_production(catalog, grant.from_environment)
            != is_production(catalog, grant.to_environment)
        ]
        if crossing and not acknowledge_grants:
            raise ConflictError(
                "Existing grants follow this resource into the new environment. "
                "Confirm to continue.",
                details=await self._grants_acknowledgment_details(actor.tenant_id, crossing),
            )

        warnings = await self._assignment_warnings(actor.tenant_id, catalog, records)
        groups = await self._assignment_groups(actor.tenant_id, changes)
        for group in groups:
            if len(group) > 99:
                raise ConflictError(
                    "This linked assignment group is too large for one transactional write",
                    details={
                        "reason": "linkedGroupTooLarge",
                        "operations": len(group),
                        "limit": 99,
                    },
                )

        outcomes = {
            (record.kind, record.resource.id): EnvironmentAssignmentOutcome(
                resource_kind=record.kind,
                resource_id=record.resource.id,
                resource_name=record.resource.name,
                previous_environment=record.previous,
                environment=record.target,
                status="unchanged",
            )
            for record in records
        }
        grants_by_resource: dict[tuple[str, str], list[AffectedGrant]] = {}
        for grant in affected_grants:
            grants_by_resource.setdefault((grant.moved_kind, grant.moved_id), []).append(grant)
        grants_carried = len(affected_grants)
        for group in groups:
            resources = [
                record.resource.model_copy(
                    update={"environment": record.target, "updated_at": utc_now()}
                )
                for record in group
            ]
            try:
                await self._repository.save_environment_assignments(
                    resources,
                    self._assignment_audit_event(actor, catalog, group, grants_by_resource),
                )
            except ConflictError as exc:
                for record in group:
                    outcomes[(record.kind, record.resource.id)] = outcomes[
                        (record.kind, record.resource.id)
                    ].model_copy(update={"status": "failed", "message": exc.message})
                continue
            for record in group:
                outcomes[(record.kind, record.resource.id)] = outcomes[
                    (record.kind, record.resource.id)
                ].model_copy(update={"status": "applied", "message": None})

        return EnvironmentAssignmentResult(
            results=[outcomes[(record.kind, record.resource.id)] for record in records],
            grants_carried=grants_carried,
            warnings=warnings,
        )

    def _suggested_assignments(
        self, blocked: list[BlockedPublication], changes: list[AssignmentRecord]
    ) -> list[dict[str, object]]:
        endpoint_targets = {
            record.resource.id: record.target
            for record in changes
            if record.kind == "modelEndpoint"
        }
        mcp_endpoint_targets = {
            record.resource.id: record.target for record in changes if record.kind == "mcpEndpoint"
        }
        gateway_targets = {
            record.resource.id: record.target
            for record in changes
            if record.kind == "gateway"
        }
        suggestions: list[dict[str, object]] = []
        seen: set[tuple[str, str]] = set()
        for publication in blocked:
            if publication.gateway_id not in gateway_targets:
                continue
            gateway_target = gateway_targets[publication.gateway_id]
            if publication.kind == "mcp":
                endpoint_id = publication.mcp_endpoint_id
                endpoint_kind: ResourceKind = "mcpEndpoint"
                endpoint_targets_for_kind = mcp_endpoint_targets
            else:
                endpoint_id = publication.model_endpoint_id
                endpoint_kind = "modelEndpoint"
                endpoint_targets_for_kind = endpoint_targets
            if endpoint_id is None or (endpoint_kind, endpoint_id) in seen:
                continue
            if endpoint_targets_for_kind.get(endpoint_id) == gateway_target:
                continue
            suggestions.append(
                {
                    "resourceKind": endpoint_kind,
                    "resourceId": endpoint_id,
                    "environment": gateway_target,
                }
            )
            seen.add((endpoint_kind, endpoint_id))
        return suggestions

    async def _affected_grants(
        self,
        tenant_id: str,
        catalog: EnvironmentCatalog,
        changes: list[AssignmentRecord],
    ) -> list[AffectedGrant]:
        if self._entitlements is None or not changes:
            return []
        change_by_gateway = {
            record.resource.id: record for record in changes if record.kind == "gateway"
        }
        change_by_endpoint = {
            record.resource.id: record for record in changes if record.kind == "modelEndpoint"
        }
        # Read each collection once rather than once per grant: this runs under the environments
        # lease, which must stay short.
        model_apis = (
            {item.id: item for item in await self._gateways.list_model_apis(tenant_id)}
            if change_by_gateway
            else {}
        )
        mcp_servers = (
            {item.id: item for item in await self._gateways.list_mcp_servers(tenant_id)}
            if change_by_gateway
            else {}
        )
        product_names: dict[tuple[str, str], str] = {}
        for gateway_id in change_by_gateway:
            for product in await self._gateways.list_observed(
                ObservedProduct, tenant_id, gateway_id, "observedProduct"
            ):
                product_names[(gateway_id, product.id)] = product.display_name
        deployment_names: dict[tuple[str, str], str] = {}
        for endpoint_id in change_by_endpoint:
            for deployment in await self._endpoints.list_observed_for_endpoint(
                ObservedModelDeployment, tenant_id, endpoint_id, "observedModelDeployment"
            ):
                deployment_names[(endpoint_id, deployment.id)] = deployment.deployment_name
        affected: list[AffectedGrant] = []
        for entitlement in await self._entitlements.list_entitlements(tenant_id):
            if not entitlement.enabled:
                continue
            resource = entitlement.resource
            scope_id = resource.scope_id or ""
            moved: AssignmentRecord | None = None
            resource_name: str | None = None
            if resource.kind == EntitlementResourceKind.MODEL_API:
                model_api = model_apis.get(resource.id)
                if model_api is not None:
                    moved = change_by_gateway.get(model_api.gateway_id)
                    resource_name = model_api.display_name
            elif resource.kind == EntitlementResourceKind.MCP_SERVER:
                server = mcp_servers.get(resource.id)
                if server is not None:
                    moved = change_by_gateway.get(server.gateway_id)
                    resource_name = server.display_name
            elif resource.kind == EntitlementResourceKind.PRODUCT:
                moved = change_by_gateway.get(scope_id)
                resource_name = product_names.get((scope_id, resource.id))
            elif resource.kind == EntitlementResourceKind.MODEL_DEPLOYMENT:
                moved = change_by_endpoint.get(scope_id)
                deployment_name = deployment_names.get((scope_id, resource.id))
                if moved is not None and deployment_name is not None:
                    resource_name = f"{deployment_name} on {moved.resource.name}"
            if moved is None:
                continue
            affected.append(
                AffectedGrant(
                    entitlement=entitlement,
                    moved_kind=moved.kind,
                    moved_id=moved.resource.id,
                    moved_name=moved.resource.name,
                    from_environment=moved.previous,
                    to_environment=moved.target,
                    resource_name=resource_name,
                )
            )
        return affected

    async def _grants_acknowledgment_details(
        self, tenant_id: str, grants: list[AffectedGrant]
    ) -> dict[str, object]:
        principal_ids = {grant.entitlement.subject.id for grant in grants}
        return {
            "reason": "grantsAcknowledgmentRequired",
            "grants": [
                {
                    "entitlementId": grant.entitlement.id,
                    "subject": grant.entitlement.subject.model_dump(mode="json", by_alias=True),
                    "subjectLabel": await self._subject_label(
                        tenant_id, grant.entitlement.subject.kind, grant.entitlement.subject.id
                    ),
                    "resource": grant.entitlement.resource.model_dump(
                        mode="json", by_alias=True
                    ),
                    "resourceName": grant.resource_name,
                    "movedResource": {
                        "resourceKind": grant.moved_kind,
                        "resourceId": grant.moved_id,
                        "resourceName": grant.moved_name,
                    },
                    "fromEnvironment": grant.from_environment,
                    "toEnvironment": grant.to_environment,
                }
                for grant in grants[:50]
            ],
            "grantCount": len(grants),
            "principalCount": len(principal_ids),
            "truncated": len(grants) > 50,
        }

    async def _subject_label(self, tenant_id: str, kind: str, subject_id: str) -> str | None:
        if self._directory is None:
            return None
        if kind == "group":
            group = await self._directory.get_group(tenant_id, subject_id)
            return group.name if group else None
        principal = await self._directory.get_principal(tenant_id, subject_id)
        return principal.label if principal else None

    async def _assignment_warnings(
        self, tenant_id: str, catalog: EnvironmentCatalog, records: list[AssignmentRecord]
    ) -> list[str]:
        gateway_targets = {
            record.resource.id: record.target
            for record in records
            if record.kind == "gateway"
        }
        endpoint_targets = {
            record.resource.id: record.target
            for record in records
            if record.kind == "modelEndpoint"
        }
        mcp_endpoint_targets = {
            record.resource.id: record.target for record in records if record.kind == "mcpEndpoint"
        }
        assigned_gateway_ids = set(gateway_targets)
        assigned_endpoint_ids = set(endpoint_targets)
        assigned_mcp_endpoint_ids = set(mcp_endpoint_targets)
        gateways = {item.id: item for item in await self._gateways.list_gateways(tenant_id)}
        endpoints = {item.id: item for item in await self._endpoints.list_endpoints(tenant_id)}
        mcp_endpoints = {item.id: item for item in await self._mcp.list_endpoints(tenant_id)}
        warnings: list[str] = []
        for publication in await self._gateways.list_publications(tenant_id):
            if (
                publication.gateway_id not in assigned_gateway_ids
                and publication.model_endpoint_id not in assigned_endpoint_ids
            ):
                continue
            gateway = gateways.get(publication.gateway_id)
            endpoint = endpoints.get(publication.model_endpoint_id)
            if gateway is None or endpoint is None:
                continue
            gateway_env = gateway_targets.get(gateway.id, gateway.environment)
            endpoint_env = endpoint_targets.get(endpoint.id, endpoint.environment)
            verdict = permits(catalog, gateway_env, endpoint_env)
            if verdict.level == VerdictLevel.WARNING and verdict.reason not in warnings:
                warnings.append(verdict.reason)
        for mcp_publication in await self._gateways.list_mcp_publications(tenant_id):
            if (
                mcp_publication.gateway_id not in assigned_gateway_ids
                and mcp_publication.mcp_endpoint_id not in assigned_mcp_endpoint_ids
            ):
                continue
            gateway = gateways.get(mcp_publication.gateway_id)
            mcp_endpoint = mcp_endpoints.get(mcp_publication.mcp_endpoint_id)
            if gateway is None or mcp_endpoint is None:
                continue
            gateway_env = gateway_targets.get(gateway.id, gateway.environment)
            endpoint_env = mcp_endpoint_targets.get(mcp_endpoint.id, mcp_endpoint.environment)
            verdict = permits(catalog, gateway_env, endpoint_env)
            if verdict.level == VerdictLevel.WARNING and verdict.reason not in warnings:
                warnings.append(verdict.reason)
        for pool in await self._gateways.list_model_pools(tenant_id):
            gateway = gateways.get(pool.gateway_id)
            if gateway is None:
                continue
            gateway_env = gateway_targets.get(gateway.id, gateway.environment)
            for endpoint_id in sorted(pool.member_endpoint_ids()):
                if (
                    pool.gateway_id not in assigned_gateway_ids
                    and endpoint_id not in assigned_endpoint_ids
                ):
                    continue
                endpoint = endpoints.get(endpoint_id)
                if endpoint is None:
                    continue
                endpoint_env = endpoint_targets.get(endpoint.id, endpoint.environment)
                verdict = permits(catalog, gateway_env, endpoint_env)
                if verdict.level == VerdictLevel.WARNING and verdict.reason not in warnings:
                    warnings.append(verdict.reason)
        return warnings

    async def _assignment_groups(
        self, tenant_id: str, changes: list[AssignmentRecord]
    ) -> list[list[AssignmentRecord]]:
        by_key = {(record.kind, record.resource.id): record for record in changes}
        parent: dict[tuple[str, str], tuple[str, str]] = {key: key for key in by_key}

        def find(key: tuple[str, str]) -> tuple[str, str]:
            while parent[key] != key:
                parent[key] = parent[parent[key]]
                key = parent[key]
            return key

        def union(left: tuple[str, str], right: tuple[str, str]) -> None:
            root_left = find(left)
            root_right = find(right)
            if root_left != root_right:
                parent[root_right] = root_left

        for publication in await self._gateways.list_publications(tenant_id):
            if not publication.may_own_gateway_state():
                continue
            left = ("gateway", publication.gateway_id)
            right = ("modelEndpoint", publication.model_endpoint_id)
            if left in by_key and right in by_key:
                union(left, right)
        for mcp_publication in await self._gateways.list_mcp_publications(tenant_id):
            if not mcp_publication.may_own_gateway_state():
                continue
            left = ("gateway", mcp_publication.gateway_id)
            right = ("mcpEndpoint", mcp_publication.mcp_endpoint_id)
            if left in by_key and right in by_key:
                union(left, right)
        for pool in await self._gateways.list_model_pools(tenant_id):
            if not pool.may_own_gateway_state():
                continue
            left = ("gateway", pool.gateway_id)
            for endpoint_id in pool.live_endpoint_ids():
                right = ("modelEndpoint", endpoint_id)
                if left in by_key and right in by_key:
                    union(left, right)
        groups: dict[tuple[str, str], list[AssignmentRecord]] = {}
        for key, record in by_key.items():
            groups.setdefault(find(key), []).append(record)
        return list(groups.values())

    def _assignment_audit_event(
        self,
        actor: Actor,
        catalog: EnvironmentCatalog,
        group: Sequence[AssignmentRecord],
        grants_by_resource: Mapping[tuple[str, str], list[AffectedGrant]],
    ) -> AuditEvent:
        carried = [
            grant.entitlement.id
            for record in group
            for grant in grants_by_resource.get((record.kind, record.resource.id), [])
        ]
        return self._audit_event(
            actor,
            "environment.assigned",
            catalog,
            resource_type=(group[0].kind if len(group) == 1 else "environmentAssignment"),
            resource_id=(group[0].resource.id if len(group) == 1 else new_id("envassign")),
            details={
                "resources": [
                    {
                        "resourceKind": record.kind,
                        "resourceId": record.resource.id,
                        "resourceName": record.resource.name,
                        "before": record.previous,
                        "after": record.target,
                    }
                    for record in group
                ],
                "grantsCarried": carried[:50],
                "grantCount": len(carried),
                "truncated": len(carried) > 50,
            },
        )

    async def create_environment(
        self, actor: Actor, request: EnvironmentCreate
    ) -> EnvironmentCatalogView:
        async def mutate(catalog: EnvironmentCatalog) -> tuple[EnvironmentCatalog, str]:
            order = request.order
            if order is None:
                order = max((item.order for item in catalog.environments), default=0) + 10
            environment = self._clean_definition(
                EnvironmentDefinition(
                    **request.model_dump(exclude={"order"}, by_alias=False),
                    order=order,
                    built_in=False,
                )
            )
            updated = catalog.model_copy(
                update={
                    "environments": [*catalog.environments, environment],
                    "updated_at": utc_now(),
                }
            )
            validate_catalog(updated.environments)
            return updated, "environment.created"

        return await self._mutate(actor, mutate)

    async def update_environment(
        self, actor: Actor, key: str, request: EnvironmentUpdate
    ) -> EnvironmentCatalogView:
        async def mutate(catalog: EnvironmentCatalog) -> tuple[EnvironmentCatalog, str]:
            environments = list(catalog.environments)
            index = self._index(environments, key)
            changes = request.model_dump(exclude_unset=True, by_alias=False)
            updated_environment = self._clean_definition(
                environments[index].model_copy(update=changes)
            )
            environments[index] = updated_environment
            updated = catalog.model_copy(
                update={"environments": environments, "updated_at": utc_now()}
            )
            validate_catalog(updated.environments)
            return updated, "environment.updated"

        return await self._mutate(actor, mutate)

    async def delete_environment(self, actor: Actor, key: str) -> EnvironmentCatalogView:
        async def mutate(catalog: EnvironmentCatalog) -> tuple[EnvironmentCatalog, str]:
            environments = list(catalog.environments)
            index = self._index(environments, key)
            environment = environments[index]
            if environment.built_in:
                raise ConflictError(
                    "Built-in environments cannot be deleted",
                    details={"reason": "builtInEnvironment", "environment": key},
                )
            await self._refuse_environment_in_use(actor.tenant_id, catalog, key)
            environments.pop(index)
            updated = catalog.model_copy(
                update={"environments": environments, "updated_at": utc_now()}
            )
            validate_catalog(updated.environments)
            return updated, "environment.deleted"

        return await self._mutate(actor, mutate)

    async def update_settings(
        self, actor: Actor, request: EnvironmentSettingsUpdate
    ) -> EnvironmentCatalogView:
        async def mutate(catalog: EnvironmentCatalog) -> tuple[EnvironmentCatalog, str]:
            updated = catalog.model_copy(
                update={
                    "require_classification": request.require_classification,
                    "updated_at": utc_now(),
                }
            )
            validate_catalog(updated.environments)
            return updated, "environment.settingsUpdated"

        return await self._mutate(actor, mutate)

    async def _mutate(
        self,
        actor: Actor,
        mutate: Callable[[EnvironmentCatalog], Awaitable[tuple[EnvironmentCatalog, str]]],
    ) -> EnvironmentCatalogView:
        async with environment_guard(self._gateways, actor.tenant_id, environments=True):
            for attempt in range(3):
                catalog = await self.catalog(actor)
                proposed, action = await mutate(catalog)
                current_draft_blocks = {
                    item.publication_id
                    for item in await self.blocked_publications(
                        actor.tenant_id, catalog=catalog, include_drafts=True
                    )
                }
                publication_ids = {
                    item.publication_id
                    for item in await self.blocked_publications(
                        actor.tenant_id, catalog=proposed, include_drafts=True
                    )
                } - current_draft_blocks
                blocked: list[BlockedPublication] = []
                saved: EnvironmentCatalog | None = None
                try:
                    # Lock every publication the edit would newly block, drafts included, and only
                    # then judge which are applied. An apply holds its publication lock for its
                    # whole run, so under these locks no draft can become applied mid-edit.
                    async with environment_guard(
                        self._gateways,
                        actor.tenant_id,
                        environments=False,
                        publication_ids=publication_ids,
                    ):
                        blocked = await self.blocked_publications(
                            actor.tenant_id, catalog=proposed
                        )
                        if not blocked:
                            saved = await self._repository.save_environment_catalog(
                                proposed, self._audit_event(actor, action, proposed)
                            )
                except ConflictError as exc:
                    if exc.details.get("id") in publication_ids:
                        raise ConflictError(PUBLICATION_BUSY_MESSAGE) from exc
                    if attempt == 2:
                        raise
                    continue
                if blocked:
                    raise publications_blocked_error(blocked)
                if saved is not None:
                    return await self._view(saved, saved=True)
        raise ConflictError("The environment catalog changed; reload it and try again")

    async def blocked_publications(
        self,
        tenant_id: str,
        *,
        catalog: EnvironmentCatalog,
        overrides: ResourceOverride | None = None,
        publication_filter: PublicationFilter | None = None,
        include_drafts: bool = False,
    ) -> list[BlockedPublication]:
        overrides = overrides or {}
        blocked: list[BlockedPublication] = []
        gateways = {item.id: item for item in await self._gateways.list_gateways(tenant_id)}
        endpoints = {item.id: item for item in await self._endpoints.list_endpoints(tenant_id)}
        mcp_endpoints = {item.id: item for item in await self._mcp.list_endpoints(tenant_id)}
        for publication in await self._gateways.list_publications(tenant_id):
            if publication_filter and not publication_filter(
                publication.gateway_id, "modelEndpoint", publication.model_endpoint_id
            ):
                continue
            if not include_drafts and not publication.may_own_gateway_state():
                continue
            gateway = gateways.get(publication.gateway_id)
            endpoint = endpoints.get(publication.model_endpoint_id)
            if not gateway or not endpoint:
                continue
            gateway_environment = overrides.get(("gateway", gateway.id), gateway.environment)
            endpoint_environment = overrides.get(
                ("modelEndpoint", endpoint.id), endpoint.environment
            )
            verdict = permits(catalog, gateway_environment, endpoint_environment)
            if verdict.level == VerdictLevel.BLOCKED:
                blocked.append(
                    BlockedPublication(
                        publication_id=publication.id,
                        display_name=publication.display_name,
                        status=str(publication.status),
                        gateway_id=gateway.id,
                        gateway_name=gateway.name,
                        gateway_environment=gateway_environment,
                        model_endpoint_id=endpoint.id,
                        model_endpoint_name=endpoint.name,
                        endpoint_environment=endpoint_environment,
                        deployment_name=publication.deployment_name,
                        verdict=verdict,
                    )
                )
        for mcp_publication in await self._gateways.list_mcp_publications(tenant_id):
            if publication_filter and not publication_filter(
                mcp_publication.gateway_id,
                "mcpEndpoint",
                mcp_publication.mcp_endpoint_id,
            ):
                continue
            if not include_drafts and not mcp_publication.may_own_gateway_state():
                continue
            gateway = gateways.get(mcp_publication.gateway_id)
            mcp_endpoint = mcp_endpoints.get(mcp_publication.mcp_endpoint_id)
            if not gateway or not mcp_endpoint:
                continue
            gateway_environment = overrides.get(("gateway", gateway.id), gateway.environment)
            endpoint_environment = overrides.get(
                ("mcpEndpoint", mcp_endpoint.id), mcp_endpoint.environment
            )
            verdict = permits(catalog, gateway_environment, endpoint_environment)
            if verdict.level == VerdictLevel.BLOCKED:
                blocked.append(
                    BlockedPublication(
                        kind="mcp",
                        publication_id=mcp_publication.id,
                        display_name=mcp_publication.display_name,
                        status=str(mcp_publication.status),
                        gateway_id=gateway.id,
                        gateway_name=gateway.name,
                        gateway_environment=gateway_environment,
                        mcp_endpoint_id=mcp_endpoint.id,
                        mcp_endpoint_name=mcp_endpoint.name,
                        endpoint_environment=endpoint_environment,
                        verdict=verdict,
                    )
                )
        for pool in await self._gateways.list_model_pools(tenant_id):
            if not include_drafts and not pool.may_own_gateway_state():
                continue
            gateway = gateways.get(pool.gateway_id)
            if not gateway:
                continue
            gateway_environment = overrides.get(("gateway", gateway.id), gateway.environment)
            # A draft member can't route anything, so only the members the gateway may hold count,
            # unless the caller is gathering drafts to lock.
            endpoint_ids = (
                pool.member_endpoint_ids() if include_drafts else pool.live_endpoint_ids()
            )
            for endpoint_id in sorted(endpoint_ids):
                if publication_filter and not publication_filter(
                    pool.gateway_id, "modelEndpoint", endpoint_id
                ):
                    continue
                endpoint = endpoints.get(endpoint_id)
                if not endpoint:
                    continue
                endpoint_environment = overrides.get(
                    ("modelEndpoint", endpoint.id), endpoint.environment
                )
                verdict = permits(catalog, gateway_environment, endpoint_environment)
                if verdict.level == VerdictLevel.BLOCKED:
                    blocked.append(
                        BlockedPublication(
                            kind="pool",
                            publication_id=pool.id,
                            display_name=pool.display_name,
                            status=str(pool.status),
                            gateway_id=gateway.id,
                            gateway_name=gateway.name,
                            gateway_environment=gateway_environment,
                            model_endpoint_id=endpoint.id,
                            model_endpoint_name=endpoint.name,
                            endpoint_environment=endpoint_environment,
                            deployment_name=", ".join(pool.deployments_on(endpoint.id)) or None,
                            verdict=verdict,
                        )
                    )
        return blocked

    @staticmethod
    def _clean_definition(environment: EnvironmentDefinition) -> EnvironmentDefinition:
        return environment.model_copy(
            update={
                "display_name": environment.display_name.strip(),
                "description": (
                    environment.description.strip()
                    if isinstance(environment.description, str)
                    else None
                ),
                "aliases": [alias.strip().casefold() for alias in environment.aliases],
                "accepts_endpoints_from": list(environment.accepts_endpoints_from),
            }
        )

    @staticmethod
    def _index(environments: list[EnvironmentDefinition], key: str) -> int:
        for index, environment in enumerate(environments):
            if environment.key == key:
                return index
        raise NotFoundError("Environment was not found", details={"environment": key})

    async def _refuse_environment_in_use(
        self, tenant_id: str, catalog: EnvironmentCatalog, key: str
    ) -> None:
        usage_counts = await self._repository.count_resources_by_environment(tenant_id)
        usage = usage_counts.get(key, EnvironmentUsage())
        resources: list[dict[str, object]] = []
        for gateway in await self._gateways.list_gateways(tenant_id):
            if gateway.environment == key and len(resources) < 50:
                resources.append(
                    {
                        "resourceKind": "gateway",
                        "resourceId": gateway.id,
                        "resourceName": gateway.name,
                    }
                )
        for endpoint in await self._endpoints.list_endpoints(tenant_id):
            if endpoint.environment == key and len(resources) < 50:
                resources.append(
                    {
                        "resourceKind": "modelEndpoint",
                        "resourceId": endpoint.id,
                        "resourceName": endpoint.name,
                    }
                )
        for mcp_endpoint in await self._mcp.list_endpoints(tenant_id):
            if mcp_endpoint.environment == key and len(resources) < 50:
                resources.append(
                    {
                        "resourceKind": "mcpEndpoint",
                        "resourceId": mcp_endpoint.id,
                        "resourceName": mcp_endpoint.name,
                    }
                )
        referenced_by = [
            environment.key
            for environment in catalog.environments
            if key in environment.accepts_endpoints_from
        ]
        if (
            usage.gateways
            or usage.model_endpoints
            or usage.mcp_endpoints
            or referenced_by
        ):
            raise ConflictError(
                "This environment is still in use",
                details={
                    "reason": "environmentInUse",
                    "environment": key,
                    "usage": usage.model_dump(mode="json", by_alias=True),
                    "resources": resources,
                    "referencedBy": referenced_by,
                },
            )
