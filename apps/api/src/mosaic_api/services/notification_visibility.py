"""Current source authorization for personal notification fan-out and send claims."""

from collections.abc import Awaitable, Callable, Sequence

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
        grant_member: Callable[[NotificationEvent, str], Awaitable[bool]] | None = None,
        usage_authorizer: Callable[[NotificationEvent, str, NotificationPersona], Awaitable[bool]]
        | None = None,
    ) -> None:
        self._entitlements = entitlements
        self._directory = directory
        self._grant_recipients = grant_recipients
        self._grant_member = grant_member
        self._usage_authorizer = usage_authorizer

    async def expand(self, event: NotificationEvent) -> Sequence[str]:
        return await self._grant_recipients(event)

    def for_fanout(
        self,
        event: NotificationEvent,
        members: frozenset[str] | DirectoryError,
    ) -> Callable[[NotificationEvent, str, NotificationPersona], Awaitable[bool]]:
        """Bind verified expansion evidence to this pass, never to the service or event."""

        async def contains(source: NotificationEvent, object_id: str) -> bool:
            if source is not event:
                raise DirectoryError("Membership evidence belongs to a different fan-out pass.")
            if isinstance(members, DirectoryError):
                raise members
            return object_id.casefold() in members

        async def authorize(
            source: NotificationEvent, object_id: str, persona: NotificationPersona
        ) -> bool:
            if source is not event:
                raise DirectoryError("Membership evidence belongs to a different fan-out pass.")
            return await self._authorize(source, object_id, persona, contains)

        return authorize

    async def authorize(
        self,
        event: NotificationEvent,
        object_id: str,
        persona: NotificationPersona,
    ) -> bool:
        membership: bool | None = None

        async def contains(source: NotificationEvent, recipient: str) -> bool:
            nonlocal membership
            if membership is None:
                membership = (
                    await self._grant_member(source, recipient)
                    if self._grant_member is not None
                    else recipient.casefold()
                    in {member.casefold() for member in await self._grant_recipients(source)}
                )
            return membership

        authorized = await self._authorize(event, object_id, persona, contains)
        if (
            authorized
            and persona == "portal"
            and event.subject.grant_subject is not None
            and object_id.casefold()
            not in {recipient.casefold() for recipient in event.audience.user_object_ids}
        ):
            # Subject access alone does not make a non-addressed requester a group candidate.
            return await contains(event, object_id)
        return authorized

    async def _authorize(
        self,
        event: NotificationEvent,
        object_id: str,
        persona: NotificationPersona,
        contains: Callable[[NotificationEvent, str], Awaitable[bool]],
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
            return await contains(event, object_id)
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
                return await contains(event, object_id)
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
