from datetime import datetime

from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient

from mosaic_api.cost_center_directory import (
    CostCenterDirectoryOperation,
    CostCenterGroupReservation,
    DirectoryOperationState,
    cost_center_group_reservation_id,
    cost_center_provisioning_operation_id,
)
from mosaic_api.cost_centers import CostCenter, CostCenterSettings, cost_center_settings_id
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos import BatchOperation, CosmosRepositoryBase
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

SETTINGS_CHANGED = "The cost center settings changed; reload them and try again"


class CosmosCostCenterRepository(CosmosRepositoryBase):
    """Cost centers and their settings in ``desired-state``, each write with its audit event.

    Every replace and delete carries the ETag that was read, so an administrator saving over a
    newer version gets a conflict rather than silently undoing it.
    """

    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
        *,
        owns_client: bool = True,
    ) -> None:
        super().__init__(
            client,
            database_name,
            desired_state_container,
            audit_events_container,
            owns_client=owns_client,
        )

    async def list_cost_centers(self, tenant_id: str) -> list[CostCenter]:
        items = await self._query(CostCenter, tenant_id, "costCenter")
        return sorted(items, key=lambda item: (item.name.casefold(), item.code.casefold(), item.id))

    async def get_cost_center(self, tenant_id: str, cost_center_id: str) -> CostCenter | None:
        return await self._read(CostCenter, tenant_id, cost_center_id)

    async def create_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        cost_center = validated_cost_center(cost_center)
        validate_audit_tenant(cost_center, audit_event)
        reservation_operations = await self._binding_operations(None, cost_center)
        if reservation_operations:
            await self._execute_batch(
                [
                    ("create", (self._document(cost_center),)),
                    *reservation_operations,
                    ("create", (self._document(audit_event),)),
                ],
                audit_event,
                conflict_message="The cost center or Entra group reservation already exists",
            )
            stored = await self.get_cost_center(cost_center.tenant_id, cost_center.id)
            if stored is None:
                raise ConflictError(COST_CENTER_CHANGED)
            return stored
        await self._mutate(
            cost_center,
            None,
            audit_event,
            "create",
            conflict_message="A cost center with this ID already exists",
        )
        return cost_center

    async def save_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        cost_center = validated_cost_center(cost_center)
        validate_audit_tenant(cost_center, audit_event)
        if not cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        current = await self.get_cost_center(cost_center.tenant_id, cost_center.id)
        if current is None or current.etag != cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        reservation_operations = await self._binding_operations(current, cost_center)
        if reservation_operations:
            await self._execute_batch(
                [
                    self._replace_operation(cost_center),
                    *reservation_operations,
                    ("create", (self._document(audit_event),)),
                ],
                audit_event,
                conflict_message="The cost center or Entra group reservation changed",
            )
            stored = await self.get_cost_center(cost_center.tenant_id, cost_center.id)
            if stored is None:
                raise ConflictError(COST_CENTER_CHANGED)
            return stored
        await self._mutate(
            cost_center, None, audit_event, "replace", conflict_message=COST_CENTER_CHANGED
        )
        return cost_center

    async def _binding_operations(
        self, previous: CostCenter | None, updated: CostCenter
    ) -> list[BatchOperation]:
        if updated.entra_binding is None and (previous is None or previous.entra_binding is None):
            return []
        provisioning = None
        if previous is None or previous.entra_binding is None:
            provisioning = await self.get_directory_operation(
                updated.tenant_id,
                cost_center_provisioning_operation_id(updated.tenant_id, updated.id),
            )
        require_binding_change(previous, updated, provisioning)
        if updated.entra_binding is None:
            return []
        requested = group_reservation(updated)
        current = await self.get_group_reservation(
            requested.tenant_id, requested.directory_tenant_id, requested.group_object_id
        )
        if current is None:
            return [("create", (self._document(requested),))]
        require_reservation_owner(requested, current)
        return [self._replace_operation(current)]

    async def get_group_reservation(
        self, tenant_id: str, directory_tenant_id: str, group_object_id: str
    ) -> CostCenterGroupReservation | None:
        return await self._read(
            CostCenterGroupReservation,
            tenant_id,
            cost_center_group_reservation_id(tenant_id, directory_tenant_id, group_object_id),
        )

    async def reserve_group(
        self, reservation: CostCenterGroupReservation, audit_event: AuditEvent
    ) -> CostCenterGroupReservation:
        reservation = CostCenterGroupReservation.model_validate(
            {**reservation.model_dump(by_alias=False), "etag": reservation.etag}
        )
        validate_audit_tenant(reservation, audit_event)
        try:
            await self._mutate(
                reservation,
                None,
                audit_event,
                "create",
                conflict_message="The Entra group reservation already exists",
            )
        except ConflictError:
            current = await self.get_group_reservation(
                reservation.tenant_id,
                reservation.directory_tenant_id,
                reservation.group_object_id,
            )
            if current is None:
                raise
            require_reservation_owner(reservation, current)
            return current
        stored = await self.get_group_reservation(
            reservation.tenant_id, reservation.directory_tenant_id, reservation.group_object_id
        )
        if stored is None:
            raise ConflictError("The Entra group reservation was not found after saving")
        return stored

    async def get_directory_operation(
        self, tenant_id: str, operation_id: str
    ) -> CostCenterDirectoryOperation | None:
        return await self._read(CostCenterDirectoryOperation, tenant_id, operation_id)

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
        filters = ""
        parameters: list[dict[str, object]] = [
            {"name": "@tenantId", "value": tenant_id},
            {"name": "@entityType", "value": "costCenterDirectoryOperation"},
            {"name": "@costCenterId", "value": cost_center_id},
            {"name": "@limit", "value": limit},
        ]
        if member_object_id is not None:
            filters += "AND c.memberObjectId = @memberObjectId "
            parameters.append({"name": "@memberObjectId", "value": member_object_id})
        if unsettled_only:
            filters += "AND c.state IN ('pending', 'unknown') "
        items = self._desired.query_items(
            query=(
                "SELECT TOP @limit * FROM c WHERE c.tenantId = @tenantId "
                "AND c.entityType = @entityType AND c.costCenterId = @costCenterId "
                f"{filters}ORDER BY c.updatedAt DESC"
            ),
            parameters=parameters,
            partition_key=tenant_id,
        )
        return [self._model(CostCenterDirectoryOperation, item) async for item in items]

    async def create_directory_operation(
        self, operation: CostCenterDirectoryOperation, audit_event: AuditEvent
    ) -> CostCenterDirectoryOperation:
        operation = validated_operation(operation)
        validate_audit_tenant(operation, audit_event)
        current = await self.get_cost_center(operation.tenant_id, operation.cost_center_id)
        require_operation_start(operation, current)
        if current is None:
            raise ConflictError(COST_CENTER_CHANGED)
        await self._execute_batch(
            [
                self._replace_operation(current),
                ("create", (self._document(operation),)),
                ("create", (self._document(audit_event),)),
            ],
            audit_event,
            conflict_message=(
                "The directory intent already exists or its cost center changed; "
                "read the recorded outcome before retrying"
            ),
        )
        stored = await self.get_directory_operation(operation.tenant_id, operation.id)
        if stored is None:
            raise ConflictError(OPERATION_CHANGED)
        return stored

    async def save_directory_operation(
        self, operation: CostCenterDirectoryOperation, audit_event: AuditEvent
    ) -> CostCenterDirectoryOperation:
        operation = validated_operation(operation)
        validate_audit_tenant(operation, audit_event)
        current = await self.get_directory_operation(operation.tenant_id, operation.id)
        if current is not None and operation_result_replayed(current, operation):
            return current
        if current is None or not operation.etag or current.etag != operation.etag:
            raise ConflictError(OPERATION_CHANGED)
        require_operation_update(current, operation)
        operations: list[BatchOperation] = [self._replace_operation(operation)]
        if operation.state == DirectoryOperationState.PENDING:
            center = await self.get_cost_center(operation.tenant_id, operation.cost_center_id)
            require_operation_start(operation, center)
            if center is None:
                raise ConflictError(COST_CENTER_CHANGED)
            operations.append(self._replace_operation(center))
        operations.append(("create", (self._document(audit_event),)))
        await self._execute_batch(operations, audit_event, conflict_message=OPERATION_CHANGED)
        stored = await self.get_directory_operation(operation.tenant_id, operation.id)
        if stored is None:
            raise ConflictError(OPERATION_CHANGED)
        return stored

    async def member_removal_recorded(
        self,
        tenant_id: str,
        cost_center_id: str,
        principal_id: str,
        *,
        not_before: datetime | None = None,
    ) -> bool:
        query = (
            "SELECT TOP 1 * FROM c WHERE c.tenantId = @tenantId "
            "AND c.entityType = 'auditEvent' AND c.action = 'costCenter.memberRemoved' "
            "AND c.resourceType = 'costCenter' AND c.resourceId = @costCenterId "
            "AND c.details.principalId = @principalId AND c.details.listed = true "
            "ORDER BY c.createdAt DESC"
        )
        parameters: list[dict[str, object]] = [
            {"name": "@tenantId", "value": tenant_id},
            {"name": "@costCenterId", "value": cost_center_id},
            {"name": "@principalId", "value": principal_id},
        ]
        # The event can still be in the transactional outbox when audit projection is deferred.
        for container in (self._desired, self._audit):
            async for item in container.query_items(
                query=query, parameters=parameters, partition_key=tenant_id
            ):
                event = self._model(AuditEvent, item)
                if not_before is None or event.created_at >= not_before:
                    return True
        return False

    async def delete_cost_center(self, cost_center: CostCenter, audit_event: AuditEvent) -> None:
        validate_audit_tenant(cost_center, audit_event)
        current = await self.get_cost_center(cost_center.tenant_id, cost_center.id)
        if (
            not cost_center.etag
            or current is None
            or current.etag != cost_center.etag
            or current.group_id != cost_center.group_id
        ):
            raise ConflictError(COST_CENTER_CHANGED)
        await self._mutate(
            cost_center,
            cost_center.id,
            audit_event,
            "delete",
            conflict_message=COST_CENTER_CHANGED,
        )

    async def get_settings(self, tenant_id: str) -> CostCenterSettings | None:
        return await self._read(CostCenterSettings, tenant_id, cost_center_settings_id(tenant_id))

    async def save_settings(
        self, settings: CostCenterSettings, audit_event: AuditEvent
    ) -> CostCenterSettings:
        try:
            await self._mutate(
                settings,
                None,
                audit_event,
                "replace" if settings.etag else "create",
                conflict_message=SETTINGS_CHANGED,
            )
        except exceptions.CosmosResourceExistsError as exc:
            raise ConflictError(SETTINGS_CHANGED) from exc
        return settings
