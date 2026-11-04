from datetime import datetime
from uuid import uuid4

from mosaic_api.cost_center_directory import (
    CostCenterDirectoryOperation,
    CostCenterGroupReservation,
    DirectoryOperationState,
    cost_center_group_reservation_id,
    cost_center_provisioning_operation_id,
)
from mosaic_api.cost_centers import CostCenter, CostCenterSettings
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cost_center_writes import (
    COST_CENTER_CHANGED,
    OPERATION_CHANGED,
    directory_operation_limit,
    group_reservation,
    operation_result_replayed,
    require_binding_change,
    require_operation_start,
    require_operation_update,
    require_reservation_owner,
    validate_audit_tenant,
    validated_cost_center,
    validated_operation,
)
from mosaic_api.repositories.memory import InMemoryDirectoryRepository

SETTINGS_CHANGED = "The cost center settings changed; reload them and try again"


class InMemoryCostCenterRepository:
    """Cost centers for local runs and tests. Every read returns a copy."""

    def __init__(
        self,
        directory_repository: InMemoryDirectoryRepository | None = None,
    ) -> None:
        self._directory = directory_repository
        self.cost_centers: dict[tuple[str, str], CostCenter] = {}
        self.group_reservations: dict[tuple[str, str], CostCenterGroupReservation] = {}
        self.directory_operations: dict[tuple[str, str], CostCenterDirectoryOperation] = {}
        self.settings: dict[str, CostCenterSettings] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def list_cost_centers(self, tenant_id: str) -> list[CostCenter]:
        return sorted(
            (
                item.model_copy(deep=True)
                for (tenant, _), item in self.cost_centers.items()
                if tenant == tenant_id
            ),
            key=lambda item: (item.name.casefold(), item.code.casefold(), item.id),
        )

    async def get_cost_center(self, tenant_id: str, cost_center_id: str) -> CostCenter | None:
        item = self.cost_centers.get((tenant_id, cost_center_id))
        return None if item is None else item.model_copy(deep=True)

    async def create_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        cost_center = validated_cost_center(cost_center)
        validate_audit_tenant(cost_center, audit_event)
        key = (cost_center.tenant_id, cost_center.id)
        if key in self.cost_centers:
            raise ConflictError("A cost center with this ID already exists")
        reservation = self._binding_reservation(None, cost_center)
        if reservation is not None:
            self._require_new_audit(audit_event)
        stored = cost_center.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.cost_centers[key] = stored
        self._store_reservation(reservation)
        self.audit_events[audit_event.id] = audit_event.model_copy(deep=True)
        return stored.model_copy(deep=True)

    async def save_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        cost_center = validated_cost_center(cost_center)
        validate_audit_tenant(cost_center, audit_event)
        key = (cost_center.tenant_id, cost_center.id)
        current = self.cost_centers.get(key)
        if current is None or current.etag != cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        reservation = self._binding_reservation(current, cost_center)
        if reservation is not None:
            self._require_new_audit(audit_event)
        stored = cost_center.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.cost_centers[key] = stored
        self._store_reservation(reservation)
        self.audit_events[audit_event.id] = audit_event.model_copy(deep=True)
        return stored.model_copy(deep=True)

    def _require_new_audit(self, audit_event: AuditEvent) -> None:
        if audit_event.id in self.audit_events:
            raise ConflictError("The audit event already exists")

    def _binding_reservation(
        self, previous: CostCenter | None, updated: CostCenter
    ) -> CostCenterGroupReservation | None:
        provisioning = self.directory_operations.get(
            (
                updated.tenant_id,
                cost_center_provisioning_operation_id(updated.tenant_id, updated.id),
            )
        )
        require_binding_change(previous, updated, provisioning)
        if updated.entra_binding is None:
            return None
        requested = group_reservation(updated)
        current = self.group_reservations.get((requested.tenant_id, requested.id))
        if current is not None:
            require_reservation_owner(requested, current)
            return current
        return requested

    def _store_reservation(self, reservation: CostCenterGroupReservation | None) -> None:
        if reservation is not None:
            self.group_reservations[(reservation.tenant_id, reservation.id)] = (
                reservation.model_copy(update={"etag": uuid4().hex}, deep=True)
            )

    async def get_group_reservation(
        self, tenant_id: str, directory_tenant_id: str, group_object_id: str
    ) -> CostCenterGroupReservation | None:
        reservation_id = cost_center_group_reservation_id(
            tenant_id, directory_tenant_id, group_object_id
        )
        item = self.group_reservations.get((tenant_id, reservation_id))
        return None if item is None else item.model_copy(deep=True)

    async def reserve_group(
        self, reservation: CostCenterGroupReservation, audit_event: AuditEvent
    ) -> CostCenterGroupReservation:
        reservation = CostCenterGroupReservation.model_validate(
            {**reservation.model_dump(by_alias=False), "etag": reservation.etag}
        )
        validate_audit_tenant(reservation, audit_event)
        key = (reservation.tenant_id, reservation.id)
        current = self.group_reservations.get(key)
        if current is not None:
            require_reservation_owner(reservation, current)
            return current.model_copy(deep=True)
        self._require_new_audit(audit_event)
        stored = reservation.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.group_reservations[key] = stored
        self.audit_events[audit_event.id] = audit_event.model_copy(deep=True)
        return stored.model_copy(deep=True)

    async def get_directory_operation(
        self, tenant_id: str, operation_id: str
    ) -> CostCenterDirectoryOperation | None:
        item = self.directory_operations.get((tenant_id, operation_id))
        return None if item is None else item.model_copy(deep=True)

    async def list_directory_operations(
        self,
        tenant_id: str,
        cost_center_id: str,
        *,
        limit: int = 50,
        member_object_id: str | None = None,
        unsettled_only: bool = False,
    ) -> list[CostCenterDirectoryOperation]:
        directory_operation_limit(limit)
        return sorted(
            (
                item.model_copy(deep=True)
                for (tenant, _), item in self.directory_operations.items()
                if tenant == tenant_id
                and item.cost_center_id == cost_center_id
                and (member_object_id is None or item.member_object_id == member_object_id)
                and (
                    not unsettled_only
                    or item.state
                    in {DirectoryOperationState.PENDING, DirectoryOperationState.UNKNOWN}
                )
            ),
            key=lambda item: (item.updated_at, item.id),
            reverse=True,
        )[:limit]

    async def create_directory_operation(
        self, operation: CostCenterDirectoryOperation, audit_event: AuditEvent
    ) -> CostCenterDirectoryOperation:
        operation = validated_operation(operation)
        validate_audit_tenant(operation, audit_event)
        key = (operation.tenant_id, operation.id)
        if key in self.directory_operations:
            raise ConflictError("The directory intent already exists; read its recorded outcome")
        center_key = (operation.tenant_id, operation.cost_center_id)
        current = self.cost_centers.get(center_key)
        require_operation_start(operation, current)
        self._require_new_audit(audit_event)
        stored = operation.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.directory_operations[key] = stored
        if current is not None:
            self.cost_centers[center_key] = current.model_copy(
                update={"etag": uuid4().hex}, deep=True
            )
        self.audit_events[audit_event.id] = audit_event.model_copy(deep=True)
        return stored.model_copy(deep=True)

    async def save_directory_operation(
        self, operation: CostCenterDirectoryOperation, audit_event: AuditEvent
    ) -> CostCenterDirectoryOperation:
        operation = validated_operation(operation)
        validate_audit_tenant(operation, audit_event)
        key = (operation.tenant_id, operation.id)
        current = self.directory_operations.get(key)
        if current is not None and operation_result_replayed(current, operation):
            return current.model_copy(deep=True)
        if current is None or not operation.etag or current.etag != operation.etag:
            raise ConflictError(OPERATION_CHANGED)
        require_operation_update(current, operation)
        center_key = (operation.tenant_id, operation.cost_center_id)
        center = self.cost_centers.get(center_key)
        if operation.state == DirectoryOperationState.PENDING:
            require_operation_start(operation, center)
        self._require_new_audit(audit_event)
        stored = operation.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.directory_operations[key] = stored
        if operation.state == DirectoryOperationState.PENDING and center is not None:
            self.cost_centers[center_key] = center.model_copy(
                update={"etag": uuid4().hex}, deep=True
            )
        self.audit_events[audit_event.id] = audit_event.model_copy(deep=True)
        return stored.model_copy(deep=True)

    async def member_removal_recorded(
        self,
        tenant_id: str,
        cost_center_id: str,
        principal_id: str,
        *,
        not_before: datetime | None = None,
    ) -> bool:
        return any(
            event.tenant_id == tenant_id
            and event.action == "costCenter.memberRemoved"
            and event.resource_type == "costCenter"
            and event.resource_id == cost_center_id
            and event.details.get("principalId") == principal_id
            and event.details.get("listed") is True
            and (not_before is None or event.created_at >= not_before)
            for event in self.audit_events.values()
        )

    async def delete_cost_center(self, cost_center: CostCenter, audit_event: AuditEvent) -> None:
        validate_audit_tenant(cost_center, audit_event)
        key = (cost_center.tenant_id, cost_center.id)
        current = self.cost_centers.get(key)
        if (
            current is None
            or current.etag != cost_center.etag
            or current.group_id != cost_center.group_id
        ):
            raise ConflictError(COST_CENTER_CHANGED)
        del self.cost_centers[key]
        self.audit_events[audit_event.id] = audit_event

    async def get_settings(self, tenant_id: str) -> CostCenterSettings | None:
        item = self.settings.get(tenant_id)
        return None if item is None else item.model_copy(deep=True)

    async def save_settings(
        self, settings: CostCenterSettings, audit_event: AuditEvent
    ) -> CostCenterSettings:
        current = self.settings.get(settings.tenant_id)
        if (current is None) != (settings.etag is None) or (
            current is not None and current.etag != settings.etag
        ):
            raise ConflictError(SETTINGS_CHANGED)
        stored = settings.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.settings[settings.tenant_id] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)
