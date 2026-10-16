"""Personal preferences, real-admin routing and fail-closed recipient revalidation."""

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, cast
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field

from mosaic_api.domain import AuditEvent, MosaicModel, utc_now
from mosaic_api.errors import (
    ConflictError,
    DirectoryDisabledError,
    DirectoryError,
    DirectoryForbiddenError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.integrations.graph.notifications import (
    AdministratorDiscovery,
    NotificationContact,
    NotificationDirectory,
    usable_mailbox,
)
from mosaic_api.notifications import (
    CapabilityCheck,
    CapabilityRemediation,
    CategoryNotificationPreference,
    ChannelCadence,
    DigestPeriod,
    DigestSchedule,
    EffectiveNotificationRecipient,
    NotificationChannel,
    NotificationChannelSettings,
    NotificationChannelSettingsInput,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationPersona,
    NotificationPreferences,
    NotificationPreferencesInput,
    NotificationRoute,
    NotificationRouteInput,
)
from mosaic_api.repositories.notifications import NotificationRepository
from mosaic_api.services.notification_events import notification_preferences_id
from mosaic_api.services.notifications import (
    NotificationDeferredError,
    NotificationPartialAudienceError,
    NotificationTarget,
)

RecipientAuthorization = Callable[[NotificationEvent, str, NotificationPersona], Awaitable[bool]]
BudgetRecipients = Callable[[str, str], Awaitable[Sequence[str]]]
RecipientExpansion = Callable[[NotificationEvent], Awaitable[Sequence[str]]]


class RecipientVisibility(Protocol):
    async def expand(self, event: NotificationEvent) -> Sequence[str]: ...

    async def authorize(
        self, event: NotificationEvent, object_id: str, persona: NotificationPersona
    ) -> bool: ...

    def for_fanout(
        self, event: NotificationEvent, members: frozenset[str] | DirectoryError
    ) -> RecipientAuthorization: ...


DigestWindowValidator = Callable[
    [NotificationDelivery, Sequence[NotificationEvent]], Awaitable[str | None]
]
_BUDGET_BRIDGE_EMAIL_REASON = (
    "The budget notification bridge owns email delivery; general email routes are suppressed."
)
_DISCOVERY_ERRORS = (DirectoryError, DirectoryDisabledError, DirectoryForbiddenError)


class EffectivePreferenceChannel(MosaicModel):
    category: str
    channel: NotificationChannel
    mode: Literal["off", "immediate", "digest"]
    digest_periods: list[DigestPeriod] = Field(default_factory=list)
    next_delivery_at: datetime | None = None
    available: bool
    reasons: list[str] = Field(default_factory=list)


class PreferencePreview(MosaicModel):
    preferences: NotificationPreferences
    channels: list[EffectivePreferenceChannel]
    checks: list[CapabilityCheck]


class RoutePreview(MosaicModel):
    route_id: str
    recipients: list[EffectiveNotificationRecipient]
    unavailable: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=utc_now)


@dataclass(frozen=True)
class NotificationCandidate:
    target: NotificationTarget
    cadence: ChannelCadence
    time_zone: str = "UTC"
    schedules: tuple[DigestSchedule, ...] = ()
    next_delivery_at: datetime | None = None


@dataclass(frozen=True)
class NotificationCandidateResolution:
    candidates: Sequence[NotificationCandidate]
    unavailable: Sequence[str] = ()


@dataclass(frozen=True)
class NotificationRecipientScope:
    recipient_reference: str
    persona: NotificationPersona | None
    channel: NotificationChannel
    route_ids: Sequence[str] | None = None


class NotificationCandidateResolutionError(DirectoryError):
    def __init__(self, resolution: NotificationCandidateResolution) -> None:
        self.resolution = resolution
        super().__init__(" ".join(dict.fromkeys(resolution.unavailable))[:1000])


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\0".join(parts).encode()).hexdigest()


def _audit(tenant_id: str, actor_id: str, action: str, kind: str, identifier: str) -> AuditEvent:
    return AuditEvent(
        id=str(uuid4()),
        tenant_id=tenant_id,
        action=action,
        resource_type=kind,
        resource_id=identifier,
        actor_object_id=actor_id,
    )


def _category(
    preferences: NotificationPreferences, event: NotificationEvent
) -> CategoryNotificationPreference:
    return next(
        (item for item in preferences.categories if item.category == event.category),
        CategoryNotificationPreference(category=event.category),
    )


def _cadence(personal: ChannelCadence, route: ChannelCadence | None) -> ChannelCadence:
    if route is None:
        return personal
    if "off" in {personal.mode, route.mode}:
        return ChannelCadence()
    if personal.mode == "immediate":
        return route
    if route.mode == "immediate":
        return personal
    periods = [period for period in personal.digest_periods if period in route.digest_periods]
    return ChannelCadence(mode="digest", digest_periods=periods) if periods else ChannelCadence()


def _wall_instant(local: datetime, zone: ZoneInfo) -> datetime:
    # Move nonexistent spring-forward wall times forward, and choose the first fall-back fold.
    for _ in range(181):
        instant = local.replace(tzinfo=zone, fold=0).astimezone(UTC)
        if instant.astimezone(zone).replace(tzinfo=None) == local:
            return instant
        local += timedelta(minutes=1)
    raise ValidationError("The configured delivery time cannot be resolved in this time zone.")


def next_digest_delivery(
    now: datetime,
    time_zone: str,
    schedules: Sequence[DigestSchedule],
    periods: Sequence[DigestPeriod],
) -> datetime | None:
    zone = ZoneInfo(time_zone)
    local_now = now.astimezone(zone)
    candidates = []
    for schedule in schedules:
        if schedule.period not in periods:
            continue
        hour, minute = map(int, schedule.delivery_time.split(":"))
        for offset in range(63):
            day = local_now.date() + timedelta(days=offset)
            if schedule.period == "weekly" and day.weekday() != schedule.day_of_week:
                continue
            if schedule.period == "monthly" and day.day != schedule.day_of_month:
                continue
            candidate = _wall_instant(datetime(day.year, day.month, day.day, hour, minute), zone)
            if candidate > now:
                candidates.append(candidate)
                break
    return min(candidates) if candidates else None


def _quiet(preferences: NotificationPreferences, now: datetime) -> bool:
    window = preferences.quiet_hours
    if window is None:
        return False
    local = now.astimezone(ZoneInfo(preferences.time_zone))
    wall_time = local.strftime("%H:%M")
    if not window.crosses_midnight:
        return (
            local.weekday() in window.days_of_week
            and window.start_time <= wall_time < window.end_time
        )
    return (local.weekday() in window.days_of_week and wall_time >= window.start_time) or (
        (local.weekday() - 1) % 7 in window.days_of_week and wall_time < window.end_time
    )


def _quiet_until(preferences: NotificationPreferences, now: datetime) -> datetime | None:
    window = preferences.quiet_hours
    if window is None or not _quiet(preferences, now):
        return None
    zone = ZoneInfo(preferences.time_zone)
    local = now.astimezone(zone)
    end_day = local.date()
    if window.crosses_midnight and local.strftime("%H:%M") >= window.start_time:
        end_day += timedelta(days=1)
    hour, minute = map(int, window.end_time.split(":"))
    return _wall_instant(datetime(end_day.year, end_day.month, end_day.day, hour, minute), zone)


class NotificationPreferenceService:
    """Also implements the notification worker's audience resolver and delivery authorizer.

    ``authorize_recipient`` must resolve current portal visibility from durable source records;
    an event's candidate user IDs are never authorization evidence. With no callback, portal
    delivery is unavailable. Admins must always be in a fresh, complete actual app-role roster.
    ``recipient_visibility`` additionally shares verified membership within one fan-out pass
    and verifies only the claimed recipient at delivery time.
    """

    def __init__(
        self,
        repository: NotificationRepository,
        *,
        directory: NotificationDirectory | None = None,
        authorize_recipient: RecipientAuthorization | None = None,
        expand_recipients: RecipientExpansion | None = None,
        recipient_visibility: RecipientVisibility | None = None,
        digest_window_validator: DigestWindowValidator | None = None,
        budget_recipients: BudgetRecipients | None = None,
        budget_delivery_migrated: bool = False,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.repository = repository
        self._directory = directory
        self._visibility = recipient_visibility
        self._authorize_recipient = (
            recipient_visibility.authorize if recipient_visibility else authorize_recipient
        )
        self._expand_recipients = (
            recipient_visibility.expand if recipient_visibility else expand_recipients
        )
        self.digest_window_validator = digest_window_validator
        self._budget_recipients = budget_recipients
        self._budget_delivery_migrated = budget_delivery_migrated
        self._clock = clock

    async def get_preferences(
        self, tenant_id: str, object_id: str, persona: NotificationPersona
    ) -> NotificationPreferences:
        current = await self.repository.get_preferences(tenant_id, object_id, persona)
        return current or NotificationPreferences(
            id=notification_preferences_id(tenant_id, object_id, persona),
            tenant_id=tenant_id,
            user_object_id=object_id,
            persona=persona,
        )

    async def save_preferences(
        self,
        tenant_id: str,
        object_id: str,
        persona: NotificationPersona,
        payload: NotificationPreferencesInput,
        *,
        expected_revision: int | None = None,
    ) -> NotificationPreferences:
        current = await self.repository.get_preferences(tenant_id, object_id, persona)
        self._revision(current.revision if current else 1, expected_revision)
        record = NotificationPreferences(
            **payload.model_dump(),
            id=current.id
            if current
            else notification_preferences_id(tenant_id, object_id, persona),
            tenant_id=tenant_id,
            user_object_id=object_id,
            persona=persona,
            revision=current.revision + 1 if current else 2,
            etag=current.etag if current else None,
            created_at=current.created_at if current else self._clock(),
            updated_at=self._clock(),
        )
        return await self.repository.save_preferences(
            record,
            _audit(tenant_id, object_id, "notificationPreferences.saved", "preferences", record.id),
        )

    @staticmethod
    def _revision(current: int, expected: int | None) -> None:
        if expected is not None and expected != current:
            raise ConflictError("Notification settings changed; reload and try again.")

    async def get_settings(self, tenant_id: str) -> NotificationChannelSettings:
        return await self.repository.get_channel_settings(tenant_id) or NotificationChannelSettings(
            id=_stable_id("notificationChannels", tenant_id), tenant_id=tenant_id
        )

    async def save_settings(
        self,
        tenant_id: str,
        actor_id: str,
        payload: NotificationChannelSettingsInput,
        *,
        expected_revision: int | None = None,
    ) -> NotificationChannelSettings:
        current = await self.repository.get_channel_settings(tenant_id)
        self._revision(current.revision if current else 1, expected_revision)
        settings = NotificationChannelSettings(
            **payload.model_dump(),
            id=current.id if current else _stable_id("notificationChannels", tenant_id),
            tenant_id=tenant_id,
            revision=current.revision + 1 if current else 2,
            etag=current.etag if current else None,
            created_at=current.created_at if current else self._clock(),
            updated_at=self._clock(),
        )
        return await self.repository.save_channel_settings(
            settings,
            _audit(
                tenant_id,
                actor_id,
                "notificationChannels.saved",
                "notificationChannels",
                settings.id,
            ),
        )

    async def list_routes(self, tenant_id: str) -> list[NotificationRoute]:
        return await self.repository.list_routes(tenant_id)

    async def get_route(self, tenant_id: str, route_id: str) -> NotificationRoute:
        route = await self.repository.get_route(tenant_id, route_id)
        if route is None:
            raise NotFoundError("Notification route was not found.")
        return route

    async def save_route(
        self,
        tenant_id: str,
        actor_id: str,
        payload: NotificationRouteInput,
        *,
        route_id: str | None = None,
        expected_revision: int | None = None,
    ) -> NotificationRoute:
        current = await self.get_route(tenant_id, route_id) if route_id else None
        self._revision(current.revision if current else 0, expected_revision)
        if payload.migrated_from_budget_id and (
            payload.audience != "budgetRecipients"
            or payload.migrated_from_budget_id != payload.budget_id
        ):
            raise ValidationError(
                "Budget migration may only reference that route's existing budget."
            )
        if payload.enabled and payload.audience in {"selectedAdmins", "allAdmins"}:
            discovery = await self._discover(
                tenant_id,
                payload.administrator_object_ids if payload.audience == "selectedAdmins" else None,
            )
            self._require_discovery(discovery)
            known = {contact.object_id.casefold() for contact in discovery.administrators}
            if any(
                identifier.casefold() not in known
                for identifier in payload.administrator_object_ids
            ):
                raise ValidationError(
                    "Selected recipients must hold the API administrator app role."
                )
        if payload.audience == "budgetRecipients" and any(
            not event.startswith("budget.") for event in payload.event_types
        ):
            raise ValidationError(
                "Legacy explicit budget recipients can only receive budget events."
            )
        route = NotificationRoute(
            **payload.model_dump(),
            id=current.id if current else str(uuid4()),
            tenant_id=tenant_id,
            revision=current.revision + 1 if current else 1,
            updated_by=actor_id,
            etag=current.etag if current else None,
            created_at=current.created_at if current else self._clock(),
            updated_at=self._clock(),
        )
        return await self.repository.save_route(
            route,
            _audit(tenant_id, actor_id, "notificationRoute.saved", "notificationRoute", route.id),
        )

    async def delete_route(
        self, tenant_id: str, actor_id: str, route_id: str, *, expected_revision: int | None = None
    ) -> None:
        route = await self.get_route(tenant_id, route_id)
        self._revision(route.revision, expected_revision)
        await self.repository.delete_route(
            route,
            _audit(tenant_id, actor_id, "notificationRoute.deleted", "notificationRoute", route.id),
        )

    async def administrator_choices(self, tenant_id: str) -> AdministratorDiscovery:
        if self._directory is None:
            return AdministratorDiscovery(reason="Administrator discovery is not configured.")
        return await self._directory.discover_administrators(tenant_id)

    async def _discover(
        self, tenant_id: str, selected: Sequence[str] | None
    ) -> AdministratorDiscovery:
        # The capability is additive: older notification doubles can supply a complete roster.
        selected_lookup = getattr(self._directory, "selected_administrators", None)
        if selected is not None and callable(selected_lookup):
            result = cast(AdministratorDiscovery, await selected_lookup(tenant_id, selected))
        else:
            result = await self.administrator_choices(tenant_id)
        if selected is not None:
            wanted = {identifier.casefold() for identifier in selected}
            result = result.model_copy(
                update={
                    "administrators": [
                        contact
                        for contact in result.administrators
                        if contact.object_id.casefold() in wanted
                    ]
                }
            )
        return result

    def _require_discovery(self, discovery: AdministratorDiscovery) -> None:
        if (
            not discovery.complete
            or discovery.fresh_until is None
            or discovery.fresh_until <= self._clock()
            or discovery.checked_at > self._clock() + timedelta(seconds=30)
        ):
            raise DirectoryError(
                (discovery.reason if not discovery.complete else None)
                or "Actual administrator discovery is incomplete or stale; retry discovery.",
                details={"capability": "notificationAdministrators", "complete": False},
            )

    async def _contact(self, tenant_id: str, object_id: str) -> NotificationContact:
        if self._directory is None:
            return NotificationContact(
                object_id=object_id,
                lookup_complete=False,
                reason="Directory contacts are not configured.",
            )
        return await self._directory.contact(tenant_id, object_id)

    @staticmethod
    def _channel_enabled(
        settings: NotificationChannelSettings, channel: NotificationChannel
    ) -> bool:
        return {
            "inApp": settings.in_app_enabled,
            "email": settings.email_enabled,
            "teamsPersonal": settings.teams_personal_enabled,
            "teamsChannel": settings.teams_channel_enabled,
        }[channel]

    @staticmethod
    def _route_matches(route: NotificationRoute, event: NotificationEvent) -> bool:
        return event.category in route.categories and (
            not route.event_types or event.event_type in route.event_types
        )

    async def _personal_candidate(
        self,
        event: NotificationEvent,
        object_id: str,
        persona: NotificationPersona,
        channel: NotificationChannel,
        settings: NotificationChannelSettings,
        *,
        route: NotificationRoute | None = None,
        actual_admin: bool = False,
        audience_eligible: bool = False,
        authorize_recipient: RecipientAuthorization | None = None,
    ) -> NotificationCandidate:
        preferences = await self.get_preferences(event.tenant_id, object_id, persona)
        category = _category(preferences, event)
        cadence = (
            ChannelCadence(mode="immediate" if category.in_app else "off")
            if channel == "inApp"
            else category.email
            if channel == "email"
            else category.teams
        )
        cadence = _cadence(cadence, route.cadence if route else None)
        reasons = []
        if channel == "email" and event.origin == "budgetNotificationBridge":
            reasons.append(_BUDGET_BRIDGE_EMAIL_REASON)
        if event.event_type == NotificationEventType.USAGE_SUMMARY:
            period = (event.data.reason_code or "").removeprefix("digest:")
            summary_periods = {schedule.period for schedule in preferences.digest_schedules}
            if period not in summary_periods:
                reasons.append("This recurring summary period is not opted in.")
            if channel != "inApp":
                if cadence.mode != "digest" or period not in cadence.digest_periods:
                    reasons.append("This channel did not opt in to this summary period.")
                # The producer already created one durable completed-period summary event.
                if cadence.mode == "digest":
                    cadence = ChannelCadence(mode="immediate")
            if event.occurred_at > self._clock():
                reasons.append("The completed-period summary is not due yet.")
        if not self._channel_enabled(settings, channel):
            reasons.append("The tenant has disabled this channel.")
        if route and not route.enabled:
            reasons.append("The route is disabled.")
        if persona == "admin" and not actual_admin:
            reasons.append("The user no longer holds the API administrator app role.")
        if persona == "portal" and not audience_eligible:
            reasons.append("The event does not address this user.")
        authorizer = authorize_recipient or self._authorize_recipient
        if authorizer is None:
            if persona == "portal":
                raise DirectoryError(
                    "Portal notification resource authorization is not configured."
                )
        elif not await authorizer(event, object_id, persona):
            reasons.append("The recipient no longer has access to this notification's subject.")
        if cadence.mode == "off":
            reasons.append("The personal category/channel preference or route is off.")
        if channel == "email" and cadence.mode != "off" and not reasons:
            contact = await self._contact(event.tenant_id, object_id)
            if not contact.lookup_complete:
                raise DirectoryError(
                    contact.reason or "Directory contact discovery is unavailable."
                )
            if not contact.available or not usable_mailbox(contact.mailbox):
                reasons.append(contact.reason or "No usable directory mailbox is available.")
        if channel == "teamsPersonal" and cadence.mode != "off" and not reasons:
            teams = await self.repository.get_personal_teams_settings(event.tenant_id)
            binding = await self.repository.get_personal_teams_binding(event.tenant_id, object_id)
            if (
                not teams
                or not teams.enabled
                or not binding
                or binding.state != "installed"
                or binding.bot_app_id != teams.bot_app_id
                or binding.cloud != teams.cloud
            ):
                reasons.append(
                    "A current authenticated personal Teams installation is unavailable."
                )
        schedules = preferences.digest_schedules
        time_zone = preferences.time_zone
        if route and route.cadence.mode == "digest":
            schedules = route.digest_schedules
            time_zone = route.time_zone
        next_at = (
            next_digest_delivery(self._clock(), time_zone, schedules, cadence.digest_periods)
            if cadence.mode == "digest"
            else None
        )
        if channel != "inApp" and cadence.mode == "immediate":
            next_at = _quiet_until(preferences, self._clock())
        return NotificationCandidate(
            target=NotificationTarget(
                recipient_reference=object_id,
                channel=channel,
                persona=persona,
                route_ids=(route.id,) if route else (),
                preference_revision=preferences.revision,
                suppression_reason=" ".join(reasons) or None,
                next_attempt_at=next_at if cadence.mode == "immediate" else None,
            ),
            cadence=cadence,
            time_zone=time_zone,
            schedules=tuple(schedules),
            next_delivery_at=next_at,
        )

    async def candidate_resolution(
        self,
        event: NotificationEvent,
        *,
        only_route: NotificationRoute | None = None,
        recipient: NotificationRecipientScope | None = None,
    ) -> NotificationCandidateResolution:
        settings = await self.get_settings(event.tenant_id)
        unavailable: list[str] = []
        admin_unknown = False
        routes = [only_route] if only_route else await self.repository.list_routes(event.tenant_id)
        routes = [route for route in routes if self._route_matches(route, event)]
        admins: set[str] = set()
        direct_persona: NotificationPersona = (
            "admin" if event.origin == "usageProducer:admin" else "portal"
        )
        admin_routes = [
            route for route in routes if route.audience in {"selectedAdmins", "allAdmins"}
        ]
        active_admin_routes = [
            route for route in admin_routes if route.enabled and route.cadence.mode != "off"
        ]
        selected_ids: set[str] = set()
        all_admins_required = False
        if direct_persona == "admin":
            selected_ids.update(event.audience.user_object_ids)
        if event.audience.include_administrators:
            if only_route and only_route.audience == "selectedAdmins":
                selected_ids.update(only_route.administrator_object_ids)
            elif only_route and only_route.audience == "allAdmins":
                all_admins_required = True
            elif any(route.audience == "allAdmins" for route in active_admin_routes):
                all_admins_required = True
            elif active_admin_routes:
                selected_ids.update(
                    identifier
                    for route in active_admin_routes
                    for identifier in route.administrator_object_ids
                )
            elif not admin_routes and only_route is None:
                all_admins_required = True
        if (
            (event.audience.include_administrators or direct_persona == "admin")
            and (only_route is None or only_route.audience in {"selectedAdmins", "allAdmins"})
            and (all_admins_required or selected_ids)
            and (recipient is None or recipient.persona == "admin")
            and (
                recipient is None
                or all_admins_required
                or recipient.recipient_reference.casefold()
                in {identifier.casefold() for identifier in selected_ids}
            )
        ):
            try:
                discovery = await self._discover(
                    event.tenant_id,
                    [recipient.recipient_reference]
                    if recipient is not None
                    else None
                    if all_admins_required
                    else sorted(selected_ids),
                )
                self._require_discovery(discovery)
                admins = {contact.object_id.casefold() for contact in discovery.administrators}
            except _DISCOVERY_ERRORS as exc:
                admin_unknown = True
                unavailable.append(exc.message)
        candidates: list[NotificationCandidate] = []
        fanout_authorizer: RecipientAuthorization | None = None

        async def personal(
            object_id: str,
            persona: NotificationPersona,
            channel: NotificationChannel,
            *,
            route: NotificationRoute | None = None,
            actual_admin: bool = False,
            audience_eligible: bool = False,
        ) -> None:
            if recipient is not None and (
                object_id.casefold() != recipient.recipient_reference.casefold()
                or persona != recipient.persona
                or channel != recipient.channel
            ):
                return
            try:
                candidates.append(
                    await self._personal_candidate(
                        event,
                        object_id,
                        persona,
                        channel,
                        settings,
                        route=route,
                        actual_admin=actual_admin,
                        audience_eligible=audience_eligible,
                        authorize_recipient=fanout_authorizer,
                    )
                )
            except _DISCOVERY_ERRORS as exc:
                unavailable.append(exc.message)

        if only_route is None:
            members: frozenset[str] | DirectoryError
            try:
                expanded = (
                    await self._expand_recipients(event)
                    if self._expand_recipients
                    and not (recipient is not None and self._visibility is not None)
                    else ()
                )
                if any(
                    not isinstance(identifier, str)
                    or not identifier.strip()
                    or len(identifier) > 256
                    for identifier in expanded
                ):
                    raise DirectoryError("Recipient expansion returned invalid user identities.")
                users = {
                    identifier.casefold()
                    for identifier in [*event.audience.user_object_ids, *expanded]
                }
                if len(users) > 10000:
                    raise DirectoryError("Recipient expansion exceeds the bounded fan-out limit.")
                members = frozenset(identifier.casefold() for identifier in expanded)
            except _DISCOVERY_ERRORS as exc:
                unavailable.append(exc.message)
                members = DirectoryError(exc.message)
                users = {identifier.casefold() for identifier in event.audience.user_object_ids}
            if recipient is None and self._visibility is not None:
                fanout_authorizer = self._visibility.for_fanout(event, members)
            if recipient is not None:
                object_id = recipient.recipient_reference.casefold()
                expanded_subject = (
                    self._visibility is not None
                    and direct_persona == "portal"
                    and event.subject.grant_subject is not None
                )
                users = {object_id} if object_id in users or expanded_subject else set()
            for object_id in (
                sorted(users) if not (admin_unknown and direct_persona == "admin") else []
            ):
                for channel in ("inApp", "email", "teamsPersonal"):
                    await personal(
                        object_id,
                        direct_persona,
                        channel,
                        actual_admin=object_id.casefold() in admins,
                        audience_eligible=True,
                    )
            for object_id in sorted(admins) if event.audience.include_administrators else []:
                await personal(object_id, "admin", "inApp", actual_admin=True)
        for route in routes:
            if recipient is not None and (
                (recipient.route_ids is not None and route.id not in recipient.route_ids)
                or route.channel != recipient.channel
            ):
                continue
            if route.audience in {"selectedAdmins", "allAdmins"}:
                if not event.audience.include_administrators or admin_unknown:
                    continue
                route_recipients = (
                    sorted(admins)
                    if route.audience == "allAdmins"
                    else sorted(
                        {identifier.casefold() for identifier in route.administrator_object_ids}
                    )
                )
                for object_id in route_recipients:
                    await personal(
                        object_id,
                        "admin",
                        route.channel,
                        route=route,
                        actual_admin=object_id in admins,
                    )
            elif route.audience == "teamsDestination":
                # A shared route is an administrator audience, never a private-user fallback.
                if not event.audience.include_administrators:
                    continue
                destination = await self.repository.get_teams_destination(
                    event.tenant_id, route.teams_destination_id or ""
                )
                reason = None
                if not self._channel_enabled(settings, route.channel):
                    reason = "The tenant has disabled shared Teams."
                elif not route.enabled or route.cadence.mode == "off":
                    reason = "The shared route is off."
                elif not destination or not destination.enabled:
                    reason = "The shared Teams destination is unavailable."
                elif event.category not in destination.allowed_categories:
                    reason = "The destination does not allow this category."
                candidates.append(
                    NotificationCandidate(
                        target=NotificationTarget(
                            recipient_reference=route.teams_destination_id or "",
                            channel="teamsChannel",
                            route_ids=(route.id,),
                            suppression_reason=reason,
                        ),
                        cadence=route.cadence,
                        time_zone=route.time_zone,
                        schedules=tuple(route.digest_schedules),
                        next_delivery_at=next_digest_delivery(
                            self._clock(),
                            route.time_zone,
                            route.digest_schedules,
                            route.cadence.digest_periods,
                        )
                        if route.cadence.mode == "digest"
                        else None,
                    )
                )
            elif route.audience == "budgetRecipients":
                budget_id = event.audience.budget_id
                if (
                    not event.event_type.startswith("budget.")
                    or budget_id != route.budget_id
                    or event.subject.budget_id != route.budget_id
                ):
                    continue
                if self._budget_recipients is None:
                    if event.origin != "budgetNotificationBridge":
                        unavailable.append("Legacy budget-recipient resolution is not configured.")
                    continue
                try:
                    mailboxes = await self._budget_recipients(event.tenant_id, budget_id or "")
                except _DISCOVERY_ERRORS as exc:
                    unavailable.append(exc.message)
                    continue
                for mailbox in mailboxes:
                    address = usable_mailbox(mailbox)
                    if address is None:
                        continue
                    reason = None
                    if event.origin == "budgetNotificationBridge":
                        reason = _BUDGET_BRIDGE_EMAIL_REASON
                    elif not self._budget_delivery_migrated:
                        reason = "Existing budget email retains ownership; migration is not active."
                    elif not route.enabled or route.cadence.mode == "off":
                        reason = "The budget route is off."
                    elif not settings.email_enabled:
                        reason = "The tenant has disabled email."
                    candidates.append(
                        NotificationCandidate(
                            target=NotificationTarget(
                                recipient_reference=(
                                    f"budget:{budget_id}:{_stable_id(address.casefold())}"
                                ),
                                channel="email",
                                route_ids=(route.id,),
                                suppression_reason=reason,
                            ),
                            cadence=route.cadence,
                            time_zone=route.time_zone,
                            schedules=tuple(route.digest_schedules),
                            next_delivery_at=next_digest_delivery(
                                self._clock(),
                                route.time_zone,
                                route.digest_schedules,
                                route.cadence.digest_periods,
                            )
                            if route.cadence.mode == "digest"
                            else None,
                        )
                    )
        return NotificationCandidateResolution(candidates, tuple(dict.fromkeys(unavailable)))

    async def candidates(
        self, event: NotificationEvent, *, only_route: NotificationRoute | None = None
    ) -> list[NotificationCandidate]:
        resolution = await self.candidate_resolution(event, only_route=only_route)
        if resolution.unavailable:
            raise NotificationCandidateResolutionError(resolution)
        return list(resolution.candidates)

    @staticmethod
    def immediate_targets(
        candidates: Sequence[NotificationCandidate],
    ) -> Sequence[NotificationTarget]:
        targets = [
            candidate.target for candidate in candidates if candidate.cadence.mode != "digest"
        ]
        deduplicated: dict[tuple[str, str, str], NotificationTarget] = {}
        for target in targets:
            key = (
                target.recipient_reference.casefold(),
                target.channel,
                target.persona or "" if target.channel == "inApp" else "",
            )
            current = deduplicated.get(key)
            if current is None:
                deduplicated[key] = target
            elif current.suppression_reason is not None and target.suppression_reason is None:
                deduplicated[key] = target
            elif target.suppression_reason is None:
                deduplicated[key] = replace(
                    current, route_ids=tuple(sorted(set(current.route_ids) | set(target.route_ids)))
                )
        return list(deduplicated.values())

    async def resolve(self, event: NotificationEvent) -> Sequence[NotificationTarget]:
        resolution = await self.candidate_resolution(event)
        targets = self.immediate_targets(resolution.candidates)
        if resolution.unavailable:
            raise NotificationPartialAudienceError(targets, " ".join(resolution.unavailable)[:1000])
        return targets

    async def digest_candidates(self, event: NotificationEvent) -> list[NotificationCandidate]:
        return [
            candidate
            for candidate in await self.candidates(event)
            if candidate.cadence.mode == "digest" and candidate.target.suppression_reason is None
        ]

    async def authorize(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> str | None:
        if not events or any(event.tenant_id != delivery.tenant_id for event in events):
            return "The delivery has missing or cross-tenant source events."
        if set(delivery.event_ids) != {event.id for event in events}:
            return "The delivery's source events do not match."
        if delivery.channel == "email" and any(
            event.origin == "budgetNotificationBridge" for event in events
        ):
            return _BUDGET_BRIDGE_EMAIL_REASON
        if delivery.digest_window_id:
            if self.digest_window_validator is None:
                return "Digest window authorization requires the durable digest scheduler."
            return await self.digest_window_validator(delivery, events)
        for event in events:
            resolution = await self.candidate_resolution(
                event,
                recipient=NotificationRecipientScope(
                    delivery.recipient_reference,
                    delivery.persona,
                    delivery.channel,
                    delivery.route_ids,
                ),
            )
            candidates = [
                candidate
                for candidate in resolution.candidates
                if candidate.target.recipient_reference.casefold()
                == delivery.recipient_reference.casefold()
                and candidate.target.persona == delivery.persona
                and candidate.target.channel == delivery.channel
                and (
                    not candidate.target.route_ids
                    or bool(set(candidate.target.route_ids) & set(delivery.route_ids))
                )
            ]
            eligible = [
                candidate
                for candidate in candidates
                if candidate.target.suppression_reason is None
                and candidate.cadence.mode == "immediate"
            ]
            if not eligible:
                if not candidates and resolution.unavailable:
                    raise DirectoryError(" ".join(resolution.unavailable)[:1000])
                return next(
                    (
                        candidate.target.suppression_reason
                        for candidate in candidates
                        if candidate.target.suppression_reason
                    ),
                    "The route, preference, cadence or recipient authorization changed.",
                )
            if delivery.persona is not None and delivery.channel != "inApp":
                preferences = await self.get_preferences(
                    delivery.tenant_id, delivery.recipient_reference, delivery.persona
                )
                if _quiet(preferences, self._clock()):
                    raise NotificationDeferredError(
                        _quiet_until(preferences, self._clock()) or self._clock(),
                        "External notification delivery is deferred during quiet hours.",
                    )
        return None

    async def mailbox_resolver(self, delivery: NotificationDelivery) -> str | None:
        if delivery.channel != "email":
            return None
        if delivery.recipient_reference.startswith("budget:"):
            if self._budget_recipients is None:
                return None
            for route_id in delivery.route_ids:
                route = await self.repository.get_route(delivery.tenant_id, route_id)
                if not route or not route.enabled or route.audience != "budgetRecipients":
                    continue
                budget_id = route.budget_id or ""
                for mailbox in await self._budget_recipients(delivery.tenant_id, budget_id):
                    address = usable_mailbox(mailbox)
                    if address and delivery.recipient_reference == (
                        f"budget:{budget_id}:{_stable_id(address.casefold())}"
                    ):
                        return address
            return None
        contact = await self._contact(delivery.tenant_id, delivery.recipient_reference)
        if not contact.lookup_complete:
            raise DirectoryError(contact.reason or "Directory contact discovery is unavailable.")
        return usable_mailbox(contact.mailbox) if contact.available else None

    async def preview_route(
        self, tenant_id: str, route_id: str, event: NotificationEvent
    ) -> RoutePreview:
        if event.tenant_id != tenant_id:
            raise ValidationError("A preview event must belong to the authenticated tenant.")
        route = await self.get_route(tenant_id, route_id)
        try:
            candidates = await self.candidates(event, only_route=route)
        except DirectoryError as exc:
            return RoutePreview(route_id=route_id, recipients=[], unavailable=[exc.message])
        return RoutePreview(
            route_id=route_id,
            recipients=[
                EffectiveNotificationRecipient(
                    recipient_reference=candidate.target.recipient_reference,
                    channel=candidate.target.channel,
                    eligible=candidate.target.suppression_reason is None
                    and candidate.cadence.mode != "off",
                    route_ids=list(candidate.target.route_ids),
                    reasons=(
                        [candidate.target.suppression_reason]
                        if candidate.target.suppression_reason
                        else [f"Eligible via {candidate.cadence.mode} cadence."]
                    ),
                    next_delivery_at=candidate.next_delivery_at,
                )
                for candidate in candidates
            ],
        )

    async def readiness_checks(
        self, tenant_id: str, object_id: str | None = None
    ) -> list[CapabilityCheck]:
        discovery = await self.administrator_choices(tenant_id) if object_id is None else None
        contact = await self._contact(tenant_id, object_id) if object_id else None
        now = self._clock()
        if discovery is not None:
            try:
                self._require_discovery(discovery)
                available = True
            except DirectoryError:
                available = False
            return [
                CapabilityCheck(
                    feature="notificationAdministrators",
                    scope="tenant",
                    state="verified" if available else "needsAction",
                    reason=discovery.reason,
                    evidence=[
                        f"Resolved {len(discovery.administrators)} actual app-role administrators."
                    ]
                    if available
                    else [],
                    checked_at=discovery.checked_at,
                    fresh_until=(
                        discovery.fresh_until
                        if discovery.fresh_until is not None
                        and discovery.fresh_until > discovery.checked_at
                        else None
                    ),
                    remediation=None
                    if available
                    else CapabilityRemediation(
                        action="Grant Application.Read.All and User.ReadBasic.All. "
                        "Grant GroupMember.Read.All; nested groups do not inherit app roles.",
                        owner="entraAdmin",
                    ),
                )
            ]
        return [
            CapabilityCheck(
                feature="notificationContact",
                scope="personal",
                state="verified" if contact and contact.available else "needsAction",
                reason=contact.reason if contact else "Directory contact is unavailable.",
                evidence=["A usable directory-backed mailbox was resolved."]
                if contact and contact.available
                else [],
                checked_at=now,
                fresh_until=now + timedelta(minutes=2),
                remediation=None
                if contact and contact.available
                else CapabilityRemediation(
                    action="Provide a usable directory mail value and grant User.ReadBasic.All.",
                    owner="entraAdmin",
                ),
            )
        ]

    async def preview_preferences(
        self, tenant_id: str, object_id: str, persona: NotificationPersona
    ) -> PreferencePreview:
        preferences = await self.get_preferences(tenant_id, object_id, persona)
        settings = await self.get_settings(tenant_id)
        contact = await self._contact(tenant_id, object_id)
        routes = await self.repository.list_routes(tenant_id) if persona == "admin" else []
        administrator_reason = None
        if persona == "admin":
            discovery = await self._discover(tenant_id, [object_id])
            try:
                self._require_discovery(discovery)
                if object_id.casefold() not in {
                    item.object_id.casefold() for item in discovery.administrators
                }:
                    administrator_reason = "The API administrator app role is no longer assigned."
            except DirectoryError as exc:
                administrator_reason = exc.message
        channels = []
        for category in preferences.categories:
            for channel in ("inApp", "email", "teamsPersonal"):
                cadence = (
                    ChannelCadence(mode="immediate" if category.in_app else "off")
                    if channel == "inApp"
                    else category.email
                    if channel == "email"
                    else category.teams
                )
                reasons = []
                if administrator_reason:
                    reasons.append(administrator_reason)
                if not self._channel_enabled(settings, channel):
                    reasons.append("The tenant has disabled this channel.")
                if channel == "email" and not contact.available:
                    reasons.append(contact.reason or "A usable directory mailbox is unavailable.")
                if (
                    persona == "admin"
                    and channel == "email"
                    and not any(
                        route.enabled
                        and route.channel == "email"
                        and category.category in route.categories
                        and route.cadence.mode != "off"
                        and (
                            route.audience == "allAdmins"
                            or (
                                route.audience == "selectedAdmins"
                                and object_id.casefold()
                                in {
                                    identifier.casefold()
                                    for identifier in route.administrator_object_ids
                                }
                            )
                        )
                        for route in routes
                    )
                ):
                    reasons.append(
                        "No enabled administrator route matches; private summaries are independent."
                    )
                if channel == "teamsPersonal":
                    teams = await self.repository.get_personal_teams_settings(tenant_id)
                    binding = await self.repository.get_personal_teams_binding(tenant_id, object_id)
                    if (
                        not teams
                        or not teams.enabled
                        or not binding
                        or binding.state != "installed"
                        or binding.bot_app_id != teams.bot_app_id
                        or binding.cloud != teams.cloud
                    ):
                        reasons.append("An authenticated personal Teams installation is required.")
                channels.append(
                    EffectivePreferenceChannel(
                        category=category.category,
                        channel=channel,
                        mode=cadence.mode,
                        digest_periods=cadence.digest_periods,
                        available=not reasons,
                        reasons=reasons,
                        next_delivery_at=(
                            next_digest_delivery(
                                self._clock(),
                                preferences.time_zone,
                                preferences.digest_schedules,
                                cadence.digest_periods,
                            )
                            if cadence.mode == "digest"
                            else _quiet_until(preferences, self._clock())
                            if channel != "inApp" and cadence.mode == "immediate"
                            else None
                        ),
                    )
                )
        return PreferencePreview(
            preferences=preferences,
            channels=channels,
            checks=await self.readiness_checks(tenant_id, object_id),
        )
