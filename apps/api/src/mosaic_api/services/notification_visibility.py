"""Current source authorization for personal notification fan-out and send claims."""

from collections.abc import Awaitable, Callable

from mosaic_api.config import AuthMode, Settings
from mosaic_api.domain import utc_now
from mosaic_api.errors import DirectoryError
from mosaic_api.integrations.graph.notifications import GraphNotificationDirectory
from mosaic_api.notifications import NotificationEvent, NotificationPersona
from mosaic_api.repositories.base import DirectoryRepository, EntitlementRepository


def usage_role_authorizer(
    settings: Settings,
    administrator_directory: GraphNotificationDirectory | None,
    portal_directory: GraphNotificationDirectory | None,
) -> Callable[[NotificationEvent, str, NotificationPersona], Awaitable[bool]]:
    async def verify(
        event: NotificationEvent,
        object_id: str,
        persona: NotificationPersona,
    ) -> bool:
        if event.tenant_id.casefold() != settings.tenant_id.casefold():
            return False
        required = (
            (settings.required_role,)
            if persona == "admin"
            else (settings.required_role, settings.portal_role)
        )
        if settings.auth_mode is AuthMode.LOCAL:
            return object_id == "local-admin" and any(
                role in settings.local_roles for role in required
            )
        directories = (
            (administrator_directory,)
            if persona == "admin"
            else (administrator_directory, portal_directory)
        )
        unavailable: DirectoryError | None = None
        for directory in directories:
            if directory is None:
                continue
            try:
                discovery = await directory.selected_administrators(event.tenant_id, [object_id])
            except DirectoryError as error:
                unavailable = error
                continue
            now = utc_now()
            if (
                discovery.complete
                and discovery.fresh_until
                and discovery.fresh_until > now
                and discovery.checked_at <= now
                and any(
                    contact.object_id.casefold() == object_id.casefold() and contact.lookup_complete
                    for contact in discovery.administrators
                )
            ):
                return True
        if unavailable is not None:
            raise unavailable
        return False

    return verify


class NotificationVisibility:
    def __init__(
        self,
        entitlements: EntitlementRepository,
        directory: DirectoryRepository,
        *,
        grant_recipients: Callable[[NotificationEvent], Awaitable[list[str]]],
        usage_authorizer: Callable[[NotificationEvent, str, NotificationPersona], Awaitable[bool]]
        | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._directory = directory
        self._grant_recipients = grant_recipients
        self._usage_authorizer = usage_authorizer

    async def authorize(
        self,
        event: NotificationEvent,
        object_id: str,
        persona: NotificationPersona,
    ) -> bool:
        if event.origin.startswith("usageProducer:"):
            return bool(
                self._usage_authorizer and await self._usage_authorizer(event, object_id, persona)
            )
        # Actual administrator role verification belongs to the recipient directory port.
        if persona == "admin":
            return True
        subject = event.subject
        if subject.request_id:
            request = await self._entitlements.get_access_request(
                event.tenant_id,
                subject.request_id,
            )
            return bool(
                request
                and request.tenant_id == event.tenant_id
                and request.requester_object_id.casefold() == object_id.casefold()
            )
        if str(event.event_type).startswith("grant.") and subject.grant_subject is not None:
            # Revocation still reaches a former holder after grant/principal removal.
            return object_id.casefold() in {
                recipient.casefold() for recipient in await self._grant_recipients(event)
            }
        if subject.entitlement_id:
            grant = await self._entitlements.get_entitlement(
                event.tenant_id,
                subject.entitlement_id,
            )
            if (
                grant is None
                or grant.tenant_id != event.tenant_id
                or not grant.enabled
                or (subject.resource is not None and subject.resource != grant.resource)
                or (subject.grant_subject is not None and subject.grant_subject != grant.subject)
            ):
                return False
            if grant.subject.kind in {"group", "securityGroup"} and subject.grant_subject_object_id:
                return object_id.casefold() in {
                    recipient.casefold() for recipient in await self._grant_recipients(event)
                }
            if grant.subject.kind != "user":
                return False
            principal = await self._directory.get_principal(event.tenant_id, grant.subject.id)
            return bool(
                principal
                and principal.kind in {"user", "agentUser"}
                and principal.object_id.casefold() == object_id.casefold()
                and (
                    subject.grant_subject_object_id is None
                    or subject.grant_subject_object_id.casefold() == principal.object_id.casefold()
                )
            )
        return False
