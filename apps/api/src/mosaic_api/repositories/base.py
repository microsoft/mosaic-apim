from collections.abc import Callable, Sequence
from typing import Protocol

from mosaic_api.budgets import Budget, BudgetState, EmailSettings, GateState
from mosaic_api.cost_centers import CostCenter, CostCenterSettings
from mosaic_api.domain import (
    AccessRequest,
    AuditEvent,
    CredentialReference,
    Entitlement,
    Gateway,
    GatewaySyncRun,
    Group,
    GroupMembership,
    McpEndpoint,
    McpEndpointSyncRun,
    McpPublication,
    McpServer,
    ModelApi,
    ModelEndpoint,
    ModelEndpointSyncRun,
    Principal,
    Publication,
    PublishPlan,
    PublishRun,
)
from mosaic_api.environments import EnvironmentCatalog, EnvironmentUsage
from mosaic_api.model_pools import ModelPool
from mosaic_api.observed import ObservedEndpointEntity, ObservedEntity
from mosaic_api.pricing import EndpointPricing, PriceVersion
from mosaic_api.usage_telemetry import (
    AttributionRecord,
    SummaryDimension,
    SummaryPeriod,
    UsageFact,
    UsageRollupState,
    UsageSummary,
)


class DirectoryRepository(Protocol):
    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def list_principals(self, tenant_id: str) -> list[Principal]: ...

    async def get_principal(self, tenant_id: str, principal_id: str) -> Principal | None: ...

    async def find_principal_by_object_id(
        self, tenant_id: str, object_id: str
    ) -> Principal | None: ...

    async def save_principal(self, principal: Principal, audit_event: AuditEvent) -> Principal: ...

    async def create_principal(
        self, principal: Principal, audit_event: AuditEvent
    ) -> Principal: ...

    async def delete_principal(self, principal: Principal, audit_event: AuditEvent) -> None: ...

    async def list_groups(self, tenant_id: str) -> list[Group]: ...

    async def get_group(self, tenant_id: str, group_id: str) -> Group | None: ...

    async def find_group_by_name(self, tenant_id: str, name: str) -> Group | None: ...

    async def save_group(self, group: Group, audit_event: AuditEvent) -> Group: ...

    async def create_group(self, group: Group, audit_event: AuditEvent) -> Group: ...

    async def delete_group(self, group: Group, audit_event: AuditEvent) -> None: ...

    async def list_memberships(
        self, tenant_id: str, *, group_id: str | None = None, principal_id: str | None = None
    ) -> list[GroupMembership]: ...

    async def get_membership(
        self, tenant_id: str, group_id: str, principal_id: str
    ) -> GroupMembership | None: ...

    async def create_membership(
        self,
        membership: GroupMembership,
        group: Group,
        principal: Principal,
        audit_event: AuditEvent,
    ) -> GroupMembership: ...

    async def delete_membership(
        self, membership: GroupMembership, audit_event: AuditEvent
    ) -> None: ...


class EnvironmentRepository(Protocol):
    """Persistence for tenant environment catalogs and resource usage counts."""

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def get_environment_catalog(self, tenant_id: str) -> EnvironmentCatalog | None: ...

    async def save_environment_catalog(
        self, catalog: EnvironmentCatalog, audit_event: AuditEvent
    ) -> EnvironmentCatalog: ...

    async def count_resources_by_environment(
        self, tenant_id: str
    ) -> dict[str | None, EnvironmentUsage]: ...

    async def save_environment_assignments(
        self,
        resources: Sequence[Gateway | ModelEndpoint | McpEndpoint],
        audit_event: AuditEvent,
    ) -> list[Gateway | ModelEndpoint | McpEndpoint]: ...


class GatewayRepository(Protocol):
    """Persistence for registered gateways and the state MOSAIC observed in them."""

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def list_gateways(self, tenant_id: str) -> list[Gateway]: ...

    async def get_gateway(self, tenant_id: str, gateway_id: str) -> Gateway | None: ...

    async def find_gateway_by_resource_id(
        self, tenant_id: str, azure_resource_id: str
    ) -> Gateway | None: ...

    async def create_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> Gateway: ...

    async def save_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> Gateway: ...

    async def record_gateway_state(self, gateway: Gateway) -> Gateway | None:
        """Merge observation results, preserving authored fields; return None if deleted."""
        ...

    async def delete_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> None: ...

    async def save_sync_run(self, run: GatewaySyncRun) -> GatewaySyncRun: ...

    async def get_sync_run(self, tenant_id: str, run_id: str) -> GatewaySyncRun | None: ...

    async def list_sync_runs(
        self, tenant_id: str, gateway_id: str, *, limit: int = 20
    ) -> list[GatewaySyncRun]: ...

    async def list_unfinished_sync_runs(self, tenant_id: str) -> list[GatewaySyncRun]: ...

    async def replace_observed(
        self,
        tenant_id: str,
        gateway_id: str,
        entities: list[ObservedEntity],
        snapshot_id: str,
        incomplete_types: set[str] | None = None,
    ) -> int:
        """Upsert the snapshot and remove documents that were not part of it. Returns removals.

        ``incomplete_types`` names entity types MOSAIC could not read in this pass; they are exempt
        from the sweep so a failed read is never mistaken for a deletion.
        """
        ...

    async def list_observed[T: ObservedEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        gateway_id: str,
        entity_type: str,
    ) -> list[T]: ...

    async def delete_observed_for_gateway(self, tenant_id: str, gateway_id: str) -> int: ...

    async def list_model_apis(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[ModelApi]: ...

    async def get_model_api(self, tenant_id: str, model_api_id: str) -> ModelApi | None: ...

    async def save_model_api(self, model_api: ModelApi, audit_event: AuditEvent) -> ModelApi:
        """Upsert, because re-importing an already-adopted API must refresh it, not duplicate it."""
        ...

    async def delete_model_api(self, model_api: ModelApi, audit_event: AuditEvent) -> None: ...

    async def list_mcp_servers(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[McpServer]: ...

    async def get_mcp_server(self, tenant_id: str, mcp_server_id: str) -> McpServer | None: ...

    async def save_mcp_server(
        self, mcp_server: McpServer, audit_event: AuditEvent
    ) -> McpServer: ...

    async def delete_mcp_server(self, mcp_server: McpServer, audit_event: AuditEvent) -> None: ...

    async def list_publications(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[Publication]: ...

    async def get_publication(
        self, tenant_id: str, publication_id: str
    ) -> Publication | None: ...

    async def save_publication(
        self, publication: Publication, audit_event: AuditEvent
    ) -> Publication:
        """Upsert, because the publication ID is deterministic per gateway/endpoint/deployment."""
        ...

    async def record_publication_state(self, publication: Publication) -> Publication:
        """Persist apply progress without emitting a second administrator audit event."""
        ...

    async def delete_publication(
        self, publication: Publication, audit_event: AuditEvent
    ) -> None: ...

    async def list_mcp_publications(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[McpPublication]: ...

    async def get_mcp_publication(
        self, tenant_id: str, publication_id: str
    ) -> McpPublication | None: ...

    async def save_mcp_publication(
        self, publication: McpPublication, audit_event: AuditEvent
    ) -> McpPublication:
        """Upsert, because the publication ID is deterministic per gateway and MCP endpoint."""
        ...

    async def record_mcp_publication_state(self, publication: McpPublication) -> McpPublication:
        """Persist apply progress without emitting a second administrator audit event."""
        ...

    async def delete_mcp_publication(
        self, publication: McpPublication, audit_event: AuditEvent
    ) -> None: ...

    async def list_model_pools(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[ModelPool]: ...

    async def get_model_pool(self, tenant_id: str, pool_id: str) -> ModelPool | None: ...

    async def save_model_pool(self, pool: ModelPool, audit_event: AuditEvent) -> ModelPool:
        """Upsert, because a pool's ID is deterministic per gateway and API name."""
        ...

    async def record_model_pool_state(self, pool: ModelPool) -> ModelPool:
        """Persist apply progress without emitting a second administrator audit event."""
        ...

    async def delete_model_pool(self, pool: ModelPool, audit_event: AuditEvent) -> None: ...

    async def save_publish_plan(self, plan: PublishPlan) -> PublishPlan: ...

    async def get_publish_plan(self, tenant_id: str, plan_id: str) -> PublishPlan | None: ...

    async def save_publish_run(self, run: PublishRun) -> PublishRun: ...

    async def get_publish_run(self, tenant_id: str, run_id: str) -> PublishRun | None: ...

    async def list_publish_runs(
        self, tenant_id: str, publication_id: str, *, limit: int = 20
    ) -> list[PublishRun]: ...

    async def list_unfinished_publish_runs(self, tenant_id: str) -> list[PublishRun]: ...

    async def acquire_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        """Conditionally acquire a durable, non-expiring publication write lock."""
        ...

    async def get_publication_lock(
        self, tenant_id: str, publication_id: str
    ) -> str | None: ...

    async def release_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        """Release only the named owner's lock, with a storage precondition."""
        ...

    async def acquire_scope_lease(
        self, tenant_id: str, scope: str, owner_id: str, *, lease_seconds: float
    ) -> None:
        """Acquire an expiring lease on a scope that guards only MOSAIC's own records.

        Unlike a publication lock, a lease expires. It is for short critical sections that make
        no Azure Resource Manager calls, so an expired lease can't let an old writer resume
        against API Management. Their writes are conditional, so a stalled holder can't overwrite
        a newer one either. Raises ``ConflictError`` while another owner holds an unexpired lease.
        """
        ...

    async def release_scope_lease(self, tenant_id: str, scope: str, owner_id: str) -> None:
        """Release the lease if this owner still holds it; otherwise leave the current holder."""
        ...


class EndpointStateRepository(Protocol):
    """The endpoint-scoped observed store, shared by model endpoints and MCP endpoints.

    Keyed on ``endpointId``, so a sweep for one registered endpoint can never reach another's
    documents — nor the gateway inventory, which is keyed on ``gatewayId``.
    """

    async def replace_observed_for_endpoint(
        self,
        tenant_id: str,
        endpoint_id: str,
        entities: list[ObservedEndpointEntity],
        snapshot_id: str,
        incomplete_types: set[str] | None = None,
    ) -> int:
        """Upsert the snapshot and remove documents that were not part of it. Returns removals.

        ``incomplete_types`` names entity types MOSAIC could not read in this pass; they are exempt
        from the sweep so a failed read is never mistaken for a deletion.
        """
        ...

    async def list_observed_for_endpoint[T: ObservedEndpointEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        endpoint_id: str,
        entity_type: str,
    ) -> list[T]: ...

    async def delete_observed_for_endpoint(self, tenant_id: str, endpoint_id: str) -> int: ...


class ModelEndpointRepository(EndpointStateRepository, Protocol):
    """Persistence for registered model endpoints and the models MOSAIC observed on them.

    Structurally parallel to :class:`GatewayRepository`, but keyed on ``endpointId`` rather than
    ``gatewayId``. The two observed shapes are deliberately separate; see
    :class:`~mosaic_api.observed.ObservedEndpointEntity`.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def list_endpoints(self, tenant_id: str) -> list[ModelEndpoint]: ...

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> ModelEndpoint | None: ...

    async def find_endpoint_by_resource_id(
        self, tenant_id: str, azure_resource_id: str
    ) -> ModelEndpoint | None: ...

    async def find_endpoint_by_url(self, tenant_id: str, endpoint: str) -> ModelEndpoint | None: ...

    async def create_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint: ...

    async def save_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint: ...

    async def record_endpoint_state(self, endpoint: ModelEndpoint) -> ModelEndpoint | None:
        """Merge observation results, preserving authored fields; return None if deleted."""
        ...

    async def delete_endpoint(self, endpoint: ModelEndpoint, audit_event: AuditEvent) -> None: ...

    async def get_credential(
        self, tenant_id: str, credential_id: str
    ) -> CredentialReference | None: ...

    async def save_credential(
        self, credential: CredentialReference, audit_event: AuditEvent
    ) -> CredentialReference: ...

    async def save_endpoint_sync_run(self, run: ModelEndpointSyncRun) -> ModelEndpointSyncRun: ...

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> ModelEndpointSyncRun | None: ...

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[ModelEndpointSyncRun]: ...

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[ModelEndpointSyncRun]: ...


class McpEndpointRepository(EndpointStateRepository, Protocol):
    """Persistence for registered MCP servers and the tools MOSAIC observed on them.

    The same shape as :class:`ModelEndpointRepository` minus the Azure resource lookup: an MCP
    server is identified by URL, because it need not be an Azure resource at all.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def list_endpoints(self, tenant_id: str) -> list[McpEndpoint]: ...

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> McpEndpoint | None: ...

    async def find_endpoint_by_url(self, tenant_id: str, endpoint: str) -> McpEndpoint | None: ...

    async def create_endpoint(
        self, endpoint: McpEndpoint, audit_event: AuditEvent
    ) -> McpEndpoint: ...

    async def save_endpoint(
        self, endpoint: McpEndpoint, audit_event: AuditEvent
    ) -> McpEndpoint: ...

    async def record_endpoint_state(self, endpoint: McpEndpoint) -> McpEndpoint | None:
        """Merge observation results, preserving authored fields; return None if deleted."""
        ...

    async def delete_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> None: ...

    async def get_credential(
        self, tenant_id: str, credential_id: str
    ) -> CredentialReference | None: ...

    async def save_credential(
        self, credential: CredentialReference, audit_event: AuditEvent
    ) -> CredentialReference: ...

    async def save_endpoint_sync_run(self, run: McpEndpointSyncRun) -> McpEndpointSyncRun: ...

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> McpEndpointSyncRun | None: ...

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[McpEndpointSyncRun]: ...

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[McpEndpointSyncRun]: ...


class EntitlementRepository(Protocol):
    """Persistence for entitlements and access requests.

    Both live in ``desired-state`` alongside the directory and gateway records they reference, so
    a grant and its audit event commit in one transactional batch on the same partition.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def record_audit(self, event: AuditEvent) -> None:
        """Durably record an audit event without mutating any grant."""
        ...

    async def list_entitlements(
        self,
        tenant_id: str,
        *,
        subject_id: str | None = None,
        resource_id: str | None = None,
    ) -> list[Entitlement]: ...

    async def get_entitlement(self, tenant_id: str, entitlement_id: str) -> Entitlement | None: ...

    async def create_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement: ...

    async def save_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement: ...

    async def delete_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> None: ...

    async def list_access_requests(
        self,
        tenant_id: str,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
    ) -> list[AccessRequest]: ...

    async def get_access_request(self, tenant_id: str, request_id: str) -> AccessRequest | None: ...

    async def create_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest: ...

    async def save_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest: ...

    async def approve_access_request(
        self,
        access_request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
    ) -> AccessRequest:
        """Create a grant and record the approval that links it, atomically.

        Both writes commit or neither does. An approved request therefore always has its grant,
        and a grant never belongs to a request that is still pending. If the request was decided
        after it was read, or the grant already exists, this raises ``ConflictError`` and writes
        nothing.
        """
        ...


class UsageRollupRepository(Protocol):
    """Rolled-up gateway telemetry, kept in its own ``usage-rollups`` container. See ADR 0019.

    Every item is partitioned by tenant. Facts and summaries carry a ``bucket`` naming the day or
    month they were rolled up from, so the rollup job can replace one gateway's bucket whole:
    list what the bucket holds, upsert only what changed, and delete what disappeared.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def get_rollup_state(
        self, tenant_id: str, gateway_id: str
    ) -> UsageRollupState | None: ...

    async def list_rollup_states(self, tenant_id: str) -> list[UsageRollupState]: ...

    async def update_rollup_state(
        self,
        tenant_id: str,
        gateway_id: str,
        change: Callable[[UsageRollupState], UsageRollupState],
    ) -> UsageRollupState:
        """Apply ``change`` to a gateway's state as currently saved, or to a new one, and save it.

        The rollup job, administrators' requests, and enabling telemetry each write their own
        fields, and a rollup takes a while. So the save succeeds only if nobody saved the state
        since it was read. Otherwise the state is read again and ``change`` applied again. The
        state's identity fields always stay the gateway's. ``change`` can raise to save nothing.
        """
        ...

    async def list_attribution_records(self, tenant_id: str) -> list[AttributionRecord]: ...

    async def save_attribution_records(self, records: Sequence[AttributionRecord]) -> None: ...

    async def list_facts(
        self,
        tenant_id: str,
        *,
        start_day: str,
        end_day: str,
        link_keys: Sequence[str] | None = None,
        gateway_ids: Sequence[str] | None = None,
    ) -> list[UsageFact]:
        """Facts whose day falls in the inclusive range, optionally only for some link keys."""
        ...

    async def list_summaries(
        self,
        tenant_id: str,
        *,
        period: SummaryPeriod,
        start: str,
        end: str,
        dimensions: Sequence[SummaryDimension],
        gateway_ids: Sequence[str] | None = None,
    ) -> list[UsageSummary]:
        """Summaries whose period starts in the inclusive range."""
        ...

    async def list_bucket_hashes(
        self, tenant_id: str, gateway_id: str, bucket: str
    ) -> dict[str, str]:
        """Every fact and summary ID in one gateway's bucket, with its content hash."""
        ...

    async def upsert_rollups(self, items: Sequence[UsageFact | UsageSummary]) -> None: ...

    async def delete_rollups(self, tenant_id: str, item_ids: Sequence[str]) -> None:
        """Delete facts or summaries; an item that is already gone is not an error."""
        ...


class PricingRepository(Protocol):
    """Administrator-authored prices and pricing facts, kept in ``desired-state``. See ADR 0020.

    Price versions are only ever added, so no two writers can overwrite each other's. An
    endpoint's pricing facts are saved over the version that was read, and each save commits with
    its audit event.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def list_price_versions(self, tenant_id: str) -> list[PriceVersion]: ...

    async def create_price_version(
        self, version: PriceVersion, audit_event: AuditEvent
    ) -> PriceVersion: ...

    async def list_endpoint_pricing(self, tenant_id: str) -> list[EndpointPricing]: ...

    async def get_endpoint_pricing(
        self, tenant_id: str, endpoint_id: str
    ) -> EndpointPricing | None: ...

    async def save_endpoint_pricing(
        self, settings: EndpointPricing, audit_event: AuditEvent
    ) -> EndpointPricing:
        """Create the settings, or replace the version that was read.

        ``settings.etag`` is None for settings that don't exist yet. Raises ``ConflictError`` when
        someone else saved or created them since they were read.
        """
        ...


class CostCenterRepository(Protocol):
    """Cost centers and the tenant's cost-center settings, kept in ``desired-state``. ADR 0022.

    Each write commits with its audit event. A cost center and the settings are saved only over
    the version that was read, so two administrators editing at once can't undo each other.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def list_cost_centers(self, tenant_id: str) -> list[CostCenter]: ...

    async def get_cost_center(
        self, tenant_id: str, cost_center_id: str
    ) -> CostCenter | None: ...

    async def create_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        """Raises ``ConflictError`` when a cost center with this ID already exists."""
        ...

    async def save_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        """Replace the version that was read; raises ``ConflictError`` if it changed since."""
        ...

    async def delete_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> None: ...

    async def get_settings(self, tenant_id: str) -> CostCenterSettings | None: ...

    async def save_settings(
        self, settings: CostCenterSettings, audit_event: AuditEvent
    ) -> CostCenterSettings:
        """Create the settings, or replace the version that was read."""
        ...


class BudgetRepository(Protocol):
    """Budgets and email settings in ``desired-state``, and where each stands in ``usage-rollups``.

    A budget or the email settings are saved with their audit event, and only over the version
    that was read. A budget's state and a gateway's blocked-list state are operational: the
    evaluator saves them through ``update_*_state``, which applies a change to the state as
    currently saved and writes it only if nobody saved since. See ADR 0023.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def record_audit(self, event: AuditEvent) -> None: ...

    async def list_budgets(self, tenant_id: str) -> list[Budget]: ...

    async def get_budget(self, tenant_id: str, budget_id: str) -> Budget | None: ...

    async def save_budget(self, budget: Budget, audit_event: AuditEvent) -> Budget:
        """Create a budget whose ``etag`` is None, or replace the version that was read.

        Raises ``ConflictError`` when someone created or saved it since it was read.
        """
        ...

    async def delete_budget(self, budget: Budget, audit_event: AuditEvent) -> None: ...

    async def get_email_settings(self, tenant_id: str) -> EmailSettings | None: ...

    async def save_email_settings(
        self, settings: EmailSettings, audit_event: AuditEvent
    ) -> EmailSettings:
        """Create the settings, or replace the version that was read."""
        ...

    async def list_budget_states(self, tenant_id: str) -> list[BudgetState]: ...

    async def get_budget_state(self, tenant_id: str, budget_id: str) -> BudgetState | None: ...

    async def update_budget_state(
        self,
        tenant_id: str,
        budget_id: str,
        change: Callable[[BudgetState], BudgetState],
    ) -> BudgetState:
        """Apply ``change`` to the budget's state as saved, or to a new one, and save it.

        The save succeeds only if nobody saved the state since it was read; otherwise the state
        is read again and ``change`` applied again. ``change`` can raise to save nothing.
        """
        ...

    async def delete_budget_state(
        self, tenant_id: str, budget_id: str, *, etag: str | None = None
    ) -> bool:
        """Remove a budget's state. With ``etag``, only that version: a state saved since, such as
        by a budget set again under the same ID, is kept. Returns whether it was removed."""
        ...

    async def list_gate_states(self, tenant_id: str) -> list[GateState]: ...

    async def update_gate_state(
        self,
        tenant_id: str,
        gateway_id: str,
        change: Callable[[GateState], GateState],
    ) -> GateState:
        """Apply ``change`` to a gateway's blocked-list state as saved, as for a budget's."""
        ...
