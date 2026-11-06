from collections.abc import Sequence

from mosaic_api.cost_center_membership_requests import MembershipRequest
from mosaic_api.domain import AccessRequestState, AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import NotificationEvent

MEMBERSHIP_CHANGED = "The membership request changed; reload it and try again"
IMMUTABLE = (
    "id",
    "tenant_id",
    "created_at",
    "requester_object_id",
    "cost_center",
    "directory_tenant_id",
    "group_object_id",
    "justification",
)


def validate_membership_write(
    request: MembershipRequest,
    audit: AuditEvent,
    events: Sequence[NotificationEvent],
    previous: MembershipRequest | None = None,
) -> MembershipRequest:
    validated = MembershipRequest.model_validate(
        {
            **request.model_dump(by_alias=False),
            "etag": request.etag,
        }
    )
    if (
        audit.tenant_id != request.tenant_id
        or audit.resource_id != request.id
        or audit.resource_type != "costCenterMembershipRequest"
    ) or any(
        event.tenant_id != request.tenant_id
        or event.source_id != request.id
        or event.subject.request_id != request.id
        or event.subject.request_kind != "costCenterMembership"
        for event in events
    ):
        raise ValueError(
            "Membership request audit and events must identify the same tenant/request"
        )
    ids = [request.id, audit.id, *(event.id for event in events)]
    if len(ids) != len(set(ids)):
        raise ValueError("Request, audit and notification IDs must be distinct")
    if previous is None:
        if request.state != AccessRequestState.PENDING or request.etag:
            raise ValueError("New membership requests must be pending without an ETag")
        if request.requester_object_id != audit.actor_object_id.casefold():
            raise ValueError("New membership requests must belong to the authenticated submitter")
    else:
        if any(getattr(previous, field) != getattr(request, field) for field in IMMUTABLE):
            raise ConflictError("The submitted membership target and requester are immutable")
        if previous.state != AccessRequestState.PENDING and (
            previous.state != request.state
            or previous.decided_at != request.decided_at
            or previous.decided_by_object_id != request.decided_by_object_id
            or previous.decision_note != request.decision_note
        ):
            raise ConflictError("The membership decision is immutable")
        if previous.operation_id and previous.operation_id != request.operation_id:
            raise ConflictError("Recover the linked operation; do not replace its intent")
        if previous.state == AccessRequestState.PENDING and request.state != previous.state:
            if request.decided_by_object_id != audit.actor_object_id:
                raise ValueError("The membership decision must name its audited actor")
            if request.state == AccessRequestState.APPROVED and (
                request.membership_state != "pending" or request.operation_id is not None
            ):
                raise ValueError("Record approval before starting the directory operation")
        if previous.membership_state == "succeeded" and request.membership_state != "succeeded":
            raise ConflictError("Confirmed membership cannot be undone by request recovery")
        if (
            previous.membership_state == "succeeded"
            and previous.operation_id != request.operation_id
        ):
            raise ConflictError("Confirmed membership cannot acquire another directory operation")
    return validated
