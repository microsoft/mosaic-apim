from typing import Protocol

from mosaic_api.domain import (
    AccessRequest,
    AuditEvent,
    CredentialReference,
    Entitlement,
    Gateway,
    GatewaySyncRun,
    Group,
    GroupMembership,
    McpServer,
    ModelApi,
    ModelEndpoint,
    ModelEndpointSyncRun,
    Principal,
)
from mosaic_api.observed import ObservedEntity, ObservedModelEntity


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

    async def record_gateway_state(self, gateway: Gateway) -> Gateway:
        """Persist observation results without emitting an administrator audit event."""
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


class ModelEndpointRepository(Protocol):
    """Persistence for registered model endpoints and the models MOSAIC observed on them.

    Structurally parallel to :class:`GatewayRepository`, but keyed on ``endpointId`` rather than
    ``gatewayId``. The two observed shapes are deliberately separate; see
    :class:`~mosaic_api.observed.ObservedModelEntity`.
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

    async def record_endpoint_state(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        """Persist observation results without emitting an administrator audit event."""
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

    async def replace_observed_models(
        self,
        tenant_id: str,
        endpoint_id: str,
        entities: list[ObservedModelEntity],
        snapshot_id: str,
        incomplete_types: set[str] | None = None,
    ) -> int:
        """Upsert the snapshot and remove documents that were not part of it. Returns removals.

        ``incomplete_types`` names entity types MOSAIC could not read in this pass; they are exempt
        from the sweep so a failed read is never mistaken for a deletion.
        """
        ...

    async def list_observed_models[T: ObservedModelEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        endpoint_id: str,
        entity_type: str,
    ) -> list[T]: ...

    async def delete_observed_for_endpoint(self, tenant_id: str, endpoint_id: str) -> int: ...


class EntitlementRepository(Protocol):
    """Persistence for entitlements and access requests.

    Both live in ``desired-state`` alongside the directory and gateway records they reference, so
    a grant and its audit event commit in one transactional batch on the same partition.
    """

    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

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
