"""Pure event builders; callers persist events with the source mutation, never fire-and-forget."""

import hashlib
import json
from datetime import UTC, datetime

from mosaic_api.domain import AccessRequest
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationEvent,
    NotificationEventType,
    NotificationSeverity,
    NotificationSubject,
    NotificationTemplateData,
    notification_category,
)


def notification_record_id(prefix: str, *parts: str) -> str:
    """Length-framed, case-sensitive identities also preserve opaque ETags and period instants."""

    encoded = json.dumps(parts, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()}"


def notification_event_id(
    tenant_id: str, event_type: NotificationEventType | str, source_id: str, source_revision: str
) -> str:
    return notification_record_id(
        "notificationEvent", tenant_id, str(event_type), source_id, source_revision
    )


def notification_preferences_id(tenant_id: str, user_object_id: str, persona: str) -> str:
    return notification_record_id(
        "notificationPreferences", tenant_id.casefold(), user_object_id.casefold(), persona
    )


def build_notification_event(
    *,
    tenant_id: str,
    event_type: NotificationEventType,
    source_id: str,
    source_revision: str,
    occurred_at: datetime,
    origin: str,
    subject: NotificationSubject,
    audience: NotificationAudience,
    data: NotificationTemplateData | None = None,
    correlation_id: str | None = None,
    severity: NotificationSeverity = "information",
) -> NotificationEvent:
    """Reuse the same source revision on retries/reconciliation; occurrence time is not the key."""

    return NotificationEvent(
        id=notification_event_id(tenant_id, event_type, source_id, source_revision),
        tenant_id=tenant_id,
        event_type=event_type,
        category=notification_category(event_type),
        source_id=source_id,
        source_revision=source_revision,
        occurred_at=occurred_at,
        created_at=occurred_at,
        updated_at=occurred_at,
        origin=origin,
        correlation_id=correlation_id,
        severity=severity,
        subject=subject,
        audience=audience,
        data=data or NotificationTemplateData(),
    )


def access_request_event(
    request: AccessRequest,
    *,
    origin: str = "entitlements",
    correlation_id: str | None = None,
) -> NotificationEvent:
    """One deterministic lifecycle event per final request state, with no requester free text."""

    event_type = {
        "pending": NotificationEventType.REQUEST_SUBMITTED,
        "approved": NotificationEventType.REQUEST_APPROVED,
        "denied": NotificationEventType.REQUEST_DENIED,
        "withdrawn": NotificationEventType.REQUEST_WITHDRAWN,
    }[request.state]
    occurred_at = request.decided_at or request.created_at
    return build_notification_event(
        tenant_id=request.tenant_id,
        event_type=event_type,
        source_id=request.id,
        source_revision=f"{request.state}:{occurred_at.astimezone(UTC).isoformat()}",
        occurred_at=occurred_at,
        origin=origin,
        correlation_id=correlation_id,
        subject=NotificationSubject(
            request_id=request.id,
            resource=request.resource,
            entitlement_id=request.granted_entitlement_id,
            cost_center_id=request.cost_center_id,
        ),
        audience=NotificationAudience(
            user_object_ids=[request.requester_object_id], include_administrators=True
        ),
        data=NotificationTemplateData(requires_plan_apply=request.state == "approved"),
    )
