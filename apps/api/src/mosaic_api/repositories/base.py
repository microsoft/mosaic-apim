from typing import Protocol

from mosaic_api.domain import (
    AuditEvent,
    Gateway,
    GatewaySyncRun,
    Group,
    GroupMembership,
    Principal,
)
from mosaic_api.observed import ObservedEntity


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
