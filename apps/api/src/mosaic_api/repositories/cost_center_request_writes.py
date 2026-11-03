from collections.abc import Sequence

from mosaic_api.cost_center_requests import CostCenterRequest
from mosaic_api.domain import AccessRequestState, AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import NotificationEvent

REQUEST_CHANGED = "The cost center request changed; reload it and try again"
_REQUEST_FIELDS = (
    "id",
    "tenant_id",
    "created_at",
    "requester_object_id",
    "name",
    "code",
    "description",
    "justification",
    "suggested_group_object_id",
)


def validate_request_write(
    request: CostCenterRequest,
    audit_event: AuditEvent,
    events: Sequence[NotificationEvent],
) -> CostCenterRequest:
    validated = CostCenterRequest.model_validate(
        {**request.model_dump(by_alias=False), "etag": request.etag}
    )
    if audit_event.tenant_id != request.tenant_id:
        raise ValueError("The request and its audit event must belong to the same tenant")
    record_ids = [request.id, audit_event.id, *(event.id for event in events)]
    if len(set(record_ids)) != len(record_ids):
        raise ValueError("A request, its audit, and each event need distinct record IDs")
    if any(
        event.tenant_id != request.tenant_id
        or event.source_id != request.id
        or event.subject.request_id != request.id
        or event.subject.request_kind != "costCenter"
        for event in events
    ):
        raise ValueError("Notification events must identify this cost center request")
    return validated


def require_request_change(previous: CostCenterRequest, updated: CostCenterRequest) -> None:
    if any(getattr(previous, field) != getattr(updated, field) for field in _REQUEST_FIELDS):
        raise ConflictError("The submitted request cannot be rewritten during its decision")
    if previous.approval is not None and previous.approval != updated.approval:
        raise ConflictError("Resume the recorded approval; its reviewed configuration is fixed")
    if previous.state != AccessRequestState.PENDING and previous.state != updated.state:
        raise ConflictError("This cost center request has already been decided")
    if previous.state != AccessRequestState.PENDING and previous.approval != updated.approval:
        raise ConflictError("A decided request cannot start another approval")
    if previous.state != AccessRequestState.PENDING and (
        previous.decided_at != updated.decided_at
        or previous.decided_by_object_id != updated.decided_by_object_id
        or previous.decision_note != updated.decision_note
    ):
        raise ConflictError("A recorded request decision cannot be changed")
    if previous.requester_membership_state == "succeeded" and (
        updated.requester_membership_state != "succeeded"
    ):
        raise ConflictError("The initial requester-add operation has already completed")
