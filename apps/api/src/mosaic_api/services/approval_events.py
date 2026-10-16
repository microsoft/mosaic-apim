"""Atomic grant-event facts and fresh, fail-closed human audience expansion."""

from mosaic_api.domain import AuditEvent, Entitlement, PrincipalKind
from mosaic_api.errors import DirectoryDisabledError, DirectoryError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationEvent,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
)
from mosaic_api.repositories.base import DirectoryRepository
from mosaic_api.services.notification_events import build_notification_event

MAX_GRANT_NOTIFICATION_RECIPIENTS = 10_000
_HUMAN_KINDS = frozenset({PrincipalKind.USER, PrincipalKind.AGENT_USER})


def grant_event(
    entitlement: Entitlement,
    audit_event: AuditEvent,
    event_type: NotificationEventType,
    *,
    user_object_id: str | None = None,
    subject_object_id: str | None = None,
    request_id: str | None = None,
) -> NotificationEvent:
    return build_notification_event(
        tenant_id=entitlement.tenant_id,
        event_type=event_type,
        source_id=entitlement.id,
        source_revision=audit_event.id,
        occurred_at=audit_event.created_at,
        origin="entitlements",
        correlation_id=request_id,
        subject=NotificationSubject(
            entitlement_id=entitlement.id,
            resource=entitlement.resource,
            cost_center_id=entitlement.cost_center_id,
            request_id=request_id,
            grant_subject=entitlement.subject,
            grant_subject_object_id=subject_object_id or user_object_id,
        ),
        audience=NotificationAudience(
            user_object_ids=[user_object_id] if user_object_id else [],
            include_administrators=True,
        ),
        data=NotificationTemplateData(requires_plan_apply=True),
    )


class GrantNotificationRecipientResolver:
    """Fresh candidates only; delivery still requires current per-recipient authorization.

    The immutable source event retains its former grant subject when the grant is deleted.
    Entra grant membership is transitive; enterprise-app administrator assignment is not.
    """

    def __init__(
        self,
        directory_repository: DirectoryRepository,
        *,
        directory_lookup: DirectoryLookup | None = None,
    ) -> None:
        self._directory = directory_repository
        self._lookup = directory_lookup

    async def expand(self, event: NotificationEvent) -> list[str]:
        subject = event.subject.grant_subject
        if subject is None or subject.kind == "application":
            return []
        if subject.kind == "user":
            if event.subject.grant_subject_object_id:
                return [event.subject.grant_subject_object_id]
            principal = await self._directory.get_principal(event.tenant_id, subject.id)
            return [principal.object_id] if principal and principal.kind in _HUMAN_KINDS else []
        if subject.kind == "group":
            group = await self._directory.get_group(event.tenant_id, subject.id)
            if group is None:
                raise DirectoryError(
                    "The former MOSAIC group's current membership cannot be verified."
                )
            memberships = await self._directory.list_memberships(
                event.tenant_id, group_id=subject.id
            )
            if len(memberships) > MAX_GRANT_NOTIFICATION_RECIPIENTS:
                raise DirectoryError(
                    "The grant's complete membership exceeds the notification bound."
                )
            users: set[str] = set()
            for membership in memberships:
                principal = await self._directory.get_principal(
                    event.tenant_id, membership.principal_id
                )
                if principal is None:
                    raise DirectoryError(
                        "A MOSAIC group member's directory identity cannot be verified."
                    )
                if principal.kind in _HUMAN_KINDS:
                    users.add(principal.object_id.casefold())
            return sorted(users)
        if subject.kind != "securityGroup":
            return []
        if self._lookup is None:
            raise DirectoryDisabledError(
                "Enable Microsoft Graph directory lookup to notify security-group grant members."
            )
        group_object_id = event.subject.grant_subject_object_id
        if not group_object_id:
            principal = await self._directory.get_principal(event.tenant_id, subject.id)
            if principal and principal.kind == PrincipalKind.SECURITY_GROUP:
                group_object_id = principal.object_id
        if not group_object_id:
            raise DirectoryError("The former security group's Entra identity cannot be verified.")
        page = await self._lookup.group_members(
            group_object_id, limit=MAX_GRANT_NOTIFICATION_RECIPIENTS
        )
        if (
            page.truncated
            or page.group_object_id.casefold() != group_object_id.casefold()
            or len(page.members) > MAX_GRANT_NOTIFICATION_RECIPIENTS
        ):
            raise DirectoryError(
                "The complete security-group membership could not be verified. "
                "No partial personal notification audience will be used."
            )
        return sorted(
            {member.object_id.casefold() for member in page.members if member.kind in _HUMAN_KINDS}
        )
