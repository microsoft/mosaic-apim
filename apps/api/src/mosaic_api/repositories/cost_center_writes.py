"""Shared conditional-write rules for the two cost-center repositories."""

from mosaic_api.cost_center_directory import (
    CostCenterDirectoryOperation,
    CostCenterGroupReservation,
    DirectoryOperationState,
    EntraGroupProvenance,
    cost_center_group_reservation_id,
)
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import AuditEvent, Entity
from mosaic_api.errors import ConflictError

COST_CENTER_CHANGED = "The cost center changed; reload it and try again"
GROUP_RESERVED = "This Entra group is already reserved by another cost center"
OPERATION_CHANGED = "The directory operation changed; reload it and reconcile its outcome"


def validate_audit_tenant(entity: Entity, audit_event: AuditEvent) -> None:
    if entity.tenant_id != audit_event.tenant_id:
        raise ValueError("The audit event and cost-center write must belong to the same tenant")


def validated_cost_center(cost_center: CostCenter) -> CostCenter:
    # model_copy(update=...) deliberately skips Pydantic validation; validate at the write boundary.
    return CostCenter.model_validate(
        {**cost_center.model_dump(by_alias=False), "etag": cost_center.etag}
    )


def validated_operation(operation: CostCenterDirectoryOperation) -> CostCenterDirectoryOperation:
    return CostCenterDirectoryOperation.model_validate(
        {**operation.model_dump(by_alias=False), "etag": operation.etag}
    )


def group_reservation(cost_center: CostCenter) -> CostCenterGroupReservation:
    binding = cost_center.entra_binding
    if binding is None:
        raise ValueError("An Entra group reservation needs a binding")
    return CostCenterGroupReservation(
        id=cost_center_group_reservation_id(
            cost_center.tenant_id, binding.directory_tenant_id, binding.group_object_id
        ),
        tenant_id=cost_center.tenant_id,
        cost_center_id=cost_center.id,
        directory_tenant_id=binding.directory_tenant_id,
        group_object_id=binding.group_object_id,
    )


def require_reservation_owner(
    requested: CostCenterGroupReservation, current: CostCenterGroupReservation
) -> None:
    if (
        current.tenant_id != requested.tenant_id
        or current.identity != requested.identity
        or current.cost_center_id != requested.cost_center_id
    ):
        raise ConflictError(GROUP_RESERVED, details={"costCenterId": current.cost_center_id})


def require_binding_change(
    previous: CostCenter | None,
    updated: CostCenter,
    provisioning: CostCenterDirectoryOperation | None,
) -> None:
    binding = updated.entra_binding
    old_binding = previous.entra_binding if previous is not None else None
    if old_binding is not None:
        if (
            binding is None
            or binding.identity != old_binding.identity
            or binding.provenance != old_binding.provenance
        ):
            raise ConflictError(
                "An Entra binding cannot be released or replaced without an explicit "
                "revocation/reconfiguration workflow"
            )
        return
    if binding is None:
        return
    if provisioning is not None:
        if provisioning.state in {DirectoryOperationState.PENDING, DirectoryOperationState.UNKNOWN}:
            raise ConflictError(
                "Reconcile the group's provisioning operation before binding a group"
            )
        if provisioning.state == DirectoryOperationState.SUCCEEDED and (
            binding.identity
            != (provisioning.directory_tenant_id, provisioning.group_object_id)
            or binding.provenance != EntraGroupProvenance.CREATED
        ):
            raise ConflictError("Bind the group returned by this center's provisioning operation")
    if binding.provenance == EntraGroupProvenance.CREATED and (
        provisioning is None or provisioning.state != DirectoryOperationState.SUCCEEDED
    ):
        raise ConflictError("A MOSAIC-created binding needs its successful provisioning result")


def require_operation_start(
    operation: CostCenterDirectoryOperation, cost_center: CostCenter | None
) -> None:
    if (
        cost_center is None
        or cost_center.tenant_id != operation.tenant_id
        or cost_center.id != operation.cost_center_id
    ):
        raise ConflictError("Save the cost center before recording its directory operation")
    if cost_center.is_general:
        raise ConflictError("General has no managed Entra group")
    if operation.state != DirectoryOperationState.PENDING:
        raise ConflictError("A new directory operation must be pending")
    if operation.kind == "createGroup":
        if cost_center.entra_binding is not None:
            raise ConflictError("This cost center already has an Entra binding")
        if cost_center.group_id is not None or cost_center.members:
            raise ConflictError(
                "Explicitly reconfigure or recreate the legacy local center "
                "before Entra provisioning"
            )
        return
    binding = cost_center.entra_binding
    if (
        binding is None
        or not binding.is_verified
        or binding.identity != (operation.directory_tenant_id, operation.group_object_id)
    ):
        raise ConflictError("Membership writes must target this center's verified Entra group")


def operation_result_replayed(
    current: CostCenterDirectoryOperation, updated: CostCenterDirectoryOperation
) -> bool:
    return (
        updated.state != DirectoryOperationState.PENDING
        and current.intent() == updated.intent()
        and current.result() == updated.result()
    )


def require_operation_update(
    current: CostCenterDirectoryOperation, updated: CostCenterDirectoryOperation
) -> None:
    if current.intent() != updated.intent() or current.created_at != updated.created_at:
        raise ConflictError("A directory operation's recorded intent cannot be changed")
    if current.kind == "createGroup" and current.group_object_id is not None:
        if current.group_object_id != updated.group_object_id:
            raise ConflictError(
                "A provisioning operation's observed group identity cannot be changed"
            )
    transitions = {
        DirectoryOperationState.PENDING: {
            DirectoryOperationState.SUCCEEDED,
            DirectoryOperationState.FAILED,
            DirectoryOperationState.UNKNOWN,
        },
        DirectoryOperationState.UNKNOWN: {
            DirectoryOperationState.SUCCEEDED,
            DirectoryOperationState.FAILED,
            DirectoryOperationState.UNKNOWN,
        },
        DirectoryOperationState.FAILED: {DirectoryOperationState.PENDING},
        DirectoryOperationState.SUCCEEDED: set(),
    }
    if updated.state not in transitions[DirectoryOperationState(current.state)]:
        raise ConflictError("Reconcile an unknown outcome; only a definite failure can be retried")


def directory_operation_limit(limit: int) -> int:
    if not 1 <= limit <= 200:
        raise ValueError("Read between 1 and 200 directory operations at a time")
    return limit
