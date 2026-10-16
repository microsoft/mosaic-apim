"""Durable generic external digests; prepared usage summaries are intentionally not aggregated."""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from mosaic_api.domain import utc_now
from mosaic_api.errors import ConflictError, DirectoryError, DomainError
from mosaic_api.notifications import (
    CapabilityCheck,
    CapabilityRemediation,
    DigestSchedule,
    DigestWindow,
    NotificationAudience,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
)
from mosaic_api.repositories.notification_digests import (
    MAX_WINDOW_EVENTS,
    DigestDefinition,
    DigestEventReference,
    DigestWindowRecord,
    NotificationDigestRepository,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.notification_events import notification_record_id
from mosaic_api.services.notification_preferences import (
    NotificationCandidate,
    NotificationPreferenceService,
    NotificationRecipientScope,
    _quiet_until,
)
from mosaic_api.services.notifications import (
    NotificationDeferredError,
    NotificationDispatcher,
    NotificationDispatchResult,
    NotificationPartialAudienceError,
    NotificationTarget,
)
from mosaic_api.services.usage_notification_schedule import (
    calendar_boundary,
    completed_window,
    scheduled_delivery,
    wall_instant,
)

DIGEST_ORIGIN = "notificationDigest"
DIGEST_WORKER = "notificationDigests"


def _definition(
    event: NotificationEvent, candidate: NotificationCandidate, schedule: DigestSchedule
) -> DigestDefinition:
    if candidate.target.channel == "inApp":
        raise DirectoryError("In-app notifications are immediate, not external digests.")
    return DigestDefinition(
        recipient_reference=(
            candidate.target.recipient_reference.casefold()
            if candidate.target.persona
            else candidate.target.recipient_reference
        ),
        persona=candidate.target.persona,
        channel=candidate.target.channel,
        category=event.category,
        time_zone=candidate.time_zone,
        schedule=schedule,
    )


def _reference(event: NotificationEvent, candidate: NotificationCandidate) -> DigestEventReference:
    routes = sorted(set(candidate.target.route_ids))
    if len(routes) > 16:
        raise ConflictError("A digest event exceeds its bounded route-reference limit.")
    return DigestEventReference(
        event_id=event.id,
        source_revision=event.source_revision,
        occurred_at=event.occurred_at,
        expires_at=event.created_at + timedelta(seconds=event.ttl),
        route_ids=routes,
    )


def _synthetic_event(tenant_id: str, record: DigestWindowRecord) -> NotificationEvent:
    definition = record.definition
    event_type = next(
        event
        for event in NotificationEventType
        if (
            event.value.split(".")[0]
            == {
                "requestLifecycle": "request",
                "accessLifecycle": "grant",
                "operationalIssues": "operation",
                "usageAndBudgets": "usage",
                "integrationHealth": "integration",
            }[definition.category]
        )
    )
    return NotificationEvent(
        id=notification_record_id("notificationDigestEvent", tenant_id, record.id),
        tenant_id=tenant_id,
        created_at=record.created_at,
        updated_at=record.created_at,
        event_type=event_type,
        category=definition.category,
        source_id=record.id,
        source_revision="1",
        occurred_at=record.window.end_at,
        origin=DIGEST_ORIGIN,
        subject=NotificationSubject(),
        audience=NotificationAudience(
            user_object_ids=[definition.recipient_reference] if definition.persona else [],
            include_administrators=definition.persona is None,
        ),
        data=NotificationTemplateData(
            summary=f"{len(record.references)} notifications from completed local periods.",
            reason_code=f"digest:{record.window.period}",
            period_label=(
                f"{record.window.start_at.isoformat()} — {record.window.end_at.isoformat()}"
            ),
        ),
        ttl=record.ttl,
    )


class NotificationDigestCoordinator:
    """Audience resolver wrapper plus a source reconciliation callback.

    Instantiation connects the preference service's digest-window validator. Both coordinator
    registration and its scheduled reconcile callback must be wired before starting the worker.
    """

    def __init__(
        self,
        repository: NotificationPersistence,
        preferences: NotificationPreferenceService,
        *,
        tenant_id: str,
        clock: Callable[[], datetime] = utc_now,
        owner: str | None = None,
        stream_batch_size: int = 100,
        hosting_verified: bool = False,
    ) -> None:
        if not 1 <= stream_batch_size <= 1000:
            raise ValueError("Digest stream batch size must be between 1 and 1000.")
        self.notifications = repository
        self.repository = NotificationDigestRepository(repository)
        self.preferences = preferences
        self.tenant_id = tenant_id
        self._clock = clock
        self._owner = owner or uuid4().hex
        self._batch_size = stream_batch_size
        self._hosting_verified = hosting_verified
        preferences.digest_window_validator = self.validate_window

    async def resolve(self, event: NotificationEvent) -> Sequence[NotificationTarget]:
        if event.tenant_id != self.tenant_id:
            raise DirectoryError("The digest coordinator is restricted to its configured tenant.")
        if event.origin == DIGEST_ORIGIN:
            record = await self._published_record(event)
            definition = record.definition
            return [
                NotificationTarget(
                    definition.recipient_reference,
                    definition.channel,
                    definition.persona,
                    route_ids=record.route_ids,
                    digest_window_id=record.id,
                )
            ]
        resolution = await self.preferences.candidate_resolution(event)
        immediate = self.preferences.immediate_targets(resolution.candidates)
        if event.event_type == NotificationEventType.USAGE_SUMMARY:
            if resolution.unavailable:
                raise NotificationPartialAudienceError(
                    immediate, " ".join(resolution.unavailable)[:1000]
                )
            return immediate
        delivered_now = {
            (target.recipient_reference.casefold(), target.channel)
            for target in immediate
            if target.suppression_reason is None
        }
        candidates = [
            candidate
            for candidate in resolution.candidates
            if candidate.cadence.mode == "digest" and candidate.target.suppression_reason is None
        ]
        # A person/channel/period gets one digest, even when several routes/personas match.
        chosen: dict[tuple[str, str, str], DigestDefinition] = {}
        for candidate in candidates:
            target = candidate.target
            if (target.recipient_reference.casefold(), target.channel) in delivered_now:
                continue
            for schedule in candidate.schedules:
                if schedule.period not in candidate.cadence.digest_periods:
                    continue
                definition = _definition(event, candidate, schedule)
                key = (definition.recipient_reference, definition.channel, schedule.period)
                selected = chosen.setdefault(key, definition)
                if selected != definition:
                    continue
                reference = _reference(event, candidate)
                if reference.expires_at <= self._clock():
                    raise DirectoryError("An expired source event cannot register a new digest.")
                await self.repository.register(
                    event.tenant_id,
                    definition,
                    reference,
                    now=self._clock(),
                    ttl=event.ttl,
                )
        if resolution.unavailable:
            raise NotificationPartialAudienceError(
                immediate, " ".join(resolution.unavailable)[:1000]
            )
        return immediate

    async def _published_record(self, event: NotificationEvent) -> DigestWindowRecord:
        record = await self.repository.get_window(event.tenant_id, event.source_id)
        if record is None or record.id != event.source_id:
            raise DirectoryError("The digest has no durable completed-window evidence.")
        if await self.notifications.get_event(event.tenant_id, event.id) is None:
            raise DirectoryError("The prepared digest window has not been atomically published.")
        expected = _synthetic_event(event.tenant_id, record)
        excluded = {
            "etag",
            "updated_at",
            "fanout_completed_at",
            "fanout_failed_at",
            "fanout_next_attempt_at",
        }
        if event.model_dump(exclude=excluded) != expected.model_dump(exclude=excluded):
            raise DirectoryError("The digest event does not match its durable window.")
        return record

    @staticmethod
    def _matches(
        event: NotificationEvent,
        candidate: NotificationCandidate,
        definition: DigestDefinition,
        reference: DigestEventReference,
    ) -> bool:
        return any(
            schedule.period in candidate.cadence.digest_periods
            and _definition(event, candidate, schedule) == definition
            and (
                not reference.route_ids
                or bool(set(reference.route_ids) & set(candidate.target.route_ids))
            )
            for schedule in candidate.schedules
        )

    async def _candidates_for(
        self,
        event: NotificationEvent,
        definition: DigestDefinition,
    ) -> list[NotificationCandidate]:
        resolution = await self.preferences.candidate_resolution(
            event,
            recipient=NotificationRecipientScope(
                definition.recipient_reference, definition.persona, definition.channel
            ),
        )
        target_candidates = [
            candidate
            for candidate in resolution.candidates
            if candidate.target.recipient_reference.casefold()
            == definition.recipient_reference.casefold()
            and candidate.target.persona == definition.persona
            and candidate.target.channel == definition.channel
        ]
        if not target_candidates and resolution.unavailable:
            raise DirectoryError(" ".join(resolution.unavailable)[:1000])
        return [
            candidate
            for candidate in target_candidates
            if candidate.cadence.mode == "digest" and candidate.target.suppression_reason is None
        ]

    async def _eligible(self, record: DigestWindowRecord) -> list[NotificationEvent]:
        result = []
        for reference in record.references:
            event = await self.notifications.get_event(self.tenant_id, reference.event_id)
            if event is None and reference.expires_at > self._clock():
                raise DirectoryError("A retained digest source event is temporarily unavailable.")
            if (
                event is None
                or event.origin == DIGEST_ORIGIN
                or event.tenant_id != self.tenant_id
                or event.source_revision != reference.source_revision
                or event.occurred_at != reference.occurred_at
                or reference.expires_at <= self._clock()
                or not record.window.start_at <= event.occurred_at < record.window.end_at
            ):
                continue
            candidates = await self._candidates_for(event, record.definition)
            if any(
                self._matches(event, candidate, record.definition, reference)
                for candidate in candidates
            ):
                result.append(event)
        return result

    async def validate_window(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> str | None:
        if (
            delivery.tenant_id != self.tenant_id
            or len(events) != 1
            or events[0].origin != DIGEST_ORIGIN
            or set(delivery.event_ids) != {events[0].id}
            or events[0].tenant_id != delivery.tenant_id
        ):
            return "The delivery does not identify this tenant's durable digest event."
        record = await self._published_record(events[0])
        definition = record.definition
        if (
            delivery.digest_window_id != record.id
            or (
                delivery.recipient_reference.casefold()
                if definition.persona
                else delivery.recipient_reference
            )
            != definition.recipient_reference
            or delivery.persona != definition.persona
            or delivery.channel != definition.channel
            or set(delivery.route_ids) != set(record.route_ids)
        ):
            return "The digest recipient or route scope does not match its durable window."
        if record.window.end_at > self._clock() or record.window.due_at > self._clock():
            raise NotificationDeferredError(
                max(record.window.end_at, record.window.due_at),
                "The local digest period is not completed and due.",
            )
        previous = completed_window(record.window.due_at, definition.schedule, definition.time_zone)
        if (
            record.window.end_at != previous.end_at
            or record.window.time_zone != definition.time_zone
            or record.window.period != definition.schedule.period
            or scheduled_delivery(
                record.window.due_at, definition.schedule, definition.time_zone, following=False
            )
            != record.window.due_at
        ):
            return "The digest window does not match its completed local calendar schedule."
        if not await self._eligible(record):
            return "No original digest events remain authorized and opted in."
        await self._defer_quiet(definition)
        return None

    async def _defer_quiet(self, definition: DigestDefinition) -> None:
        if definition.persona:
            preferences = await self.preferences.get_preferences(
                self.tenant_id, definition.recipient_reference, definition.persona
            )
            until = _quiet_until(preferences, self._clock())
            if until:
                raise NotificationDeferredError(until, "The digest recipient is in quiet hours.")

    async def expand_delivery(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> list[NotificationEvent]:
        reason = await self.validate_window(delivery, events)
        if reason:
            return []
        record = await self._published_record(events[0])
        return await self._eligible(record)

    async def _reconcile_stream(self, stream_id: str) -> str | None:
        for _ in range(3):
            stored = await self.repository.get_stream(self.tenant_id, stream_id)
            if stored is None:
                return None
            state, stream = stored
            now = self._clock()
            if not stream.pending:
                await self.repository.discard(self.tenant_id, stream_id, set(), now=now, error=None)
                return None
            definition = stream.definition
            due = scheduled_delivery(
                now, definition.schedule, definition.time_zone, following=False
            )
            latest = completed_window(due, definition.schedule, definition.time_zone)
            due_refs = sorted(
                stream.pending,
                key=lambda reference: (reference.occurred_at, reference.event_id),
            )
            if not due_refs:
                return None
            eligible: list[DigestEventReference] = []
            consumed: set[str] = set()
            error = None
            for reference in due_refs:
                event = await self.notifications.get_event(self.tenant_id, reference.event_id)
                if event is None and reference.expires_at > now:
                    raise DirectoryError(
                        "A retained digest source event is temporarily unavailable."
                    )
                if event is None or reference.expires_at <= now:
                    consumed.add(reference.event_id)
                    error = (
                        "Digest source events expired or are unavailable; no content was invented."
                    )
                    continue
                candidates = await self._candidates_for(event, definition)
                if any(self._matches(event, item, definition, reference) for item in candidates):
                    if reference.occurred_at < latest.end_at and len(eligible) < MAX_WINDOW_EVENTS:
                        eligible.append(reference)
                else:
                    # Rehome only the same private/shared target and period, never a fallback.
                    for candidate in candidates:
                        target = candidate.target
                        if (
                            target.recipient_reference.casefold() != definition.recipient_reference
                            or target.persona != definition.persona
                            or target.channel != definition.channel
                        ):
                            continue
                        schedule = next(
                            (
                                item
                                for item in candidate.schedules
                                if item.period == definition.schedule.period
                                and item.period in candidate.cadence.digest_periods
                            ),
                            None,
                        )
                        if schedule:
                            await self.repository.register(
                                self.tenant_id,
                                _definition(event, candidate, schedule),
                                _reference(event, candidate),
                                now=now,
                                ttl=event.ttl,
                            )
                    consumed.add(reference.event_id)
            if not eligible:
                if not consumed:
                    return None
                await self.repository.discard(
                    self.tenant_id, stream_id, consumed, now=now, error=error
                )
                if error:
                    return error
                continue
            zone = ZoneInfo(definition.time_zone)
            first_day = calendar_boundary(
                eligible[0].occurred_at.astimezone(zone).date(), definition.schedule.period
            )
            start = wall_instant(datetime.combine(first_day, datetime.min.time()), zone)
            window = DigestWindow(
                period=definition.schedule.period,
                time_zone=definition.time_zone,
                start_at=start,
                end_at=latest.end_at,
                due_at=due,
                catch_up=start < latest.start_at,
            )
            window_id = notification_record_id(
                "notificationDigestWindow",
                self.tenant_id,
                stream_id,
                window.start_at.isoformat(),
                window.end_at.isoformat(),
                *[reference.model_dump_json() for reference in eligible],
            )
            record = await self.repository.save_window(
                self.tenant_id,
                DigestWindowRecord(
                    id=window_id,
                    definition=definition,
                    window=window,
                    references=eligible,
                    created_at=now,
                    ttl=max(state.ttl or 0, 90 * 86400),
                ),
            )
            await self.repository.advance(
                _synthetic_event(self.tenant_id, record),
                state,
                stream,
                consumed | {reference.event_id for reference in eligible},
            )
            if error:
                return error
        return None

    async def reconcile(self) -> None:
        lease = await self.notifications.claim_lease(
            self.tenant_id, DIGEST_WORKER, self._owner, now=self._clock(), lease_seconds=180
        )
        if lease is None:
            return
        error = None
        cursor = ""
        try:
            state = await self.notifications.get_worker_state(self.tenant_id, DIGEST_WORKER)
            cursor = state.checkpoint if state and state.checkpoint else ""
            streams = await self.repository.list_streams(self.tenant_id, now=self._clock())
            ordered = [item for item in streams if item > cursor] + [
                item for item in streams if item <= cursor
            ]
            for stream_id in ordered[: self._batch_size]:
                try:
                    stream_error = await self._reconcile_stream(stream_id)
                    error = stream_error or error
                except DomainError as exc:
                    error = exc.message
                cursor = stream_id
                lease = await self.notifications.renew_lease(
                    lease, now=self._clock(), lease_seconds=180
                )
            await self.notifications.update_worker_state(
                self.tenant_id,
                DIGEST_WORKER,
                lambda current: current.model_copy(
                    update={
                        "heartbeat_at": self._clock(),
                        "last_succeeded_at": self._clock()
                        if not error
                        else current.last_succeeded_at,
                        "last_error": error,
                        "checkpoint": cursor,
                        "hosting_verified": self._hosting_verified,
                    }
                ),
            )
        finally:
            await self.notifications.release_lease(lease)

    async def readiness_checks(self) -> list[CapabilityCheck]:
        state = await self.notifications.get_worker_state(self.tenant_id, DIGEST_WORKER)
        healthy = bool(
            state
            and state.heartbeat_at
            and state.heartbeat_at > self._clock() - timedelta(minutes=5)
            and state.hosting_verified
            and not state.last_error
        )
        return [
            CapabilityCheck(
                feature="notificationDigests",
                scope="tenant",
                state="verified" if healthy else "needsAction",
                checked_at=self._clock(),
                evidence=["The durable digest reconciler recently ran on a verified awake host."]
                if healthy
                else [],
                reason=state.last_error
                if state and state.last_error
                else (None if healthy else "Durable digest scheduling is not recently verified."),
                remediation=None
                if healthy
                else CapabilityRemediation(
                    action="Run digest reconciliation on an awake host; repair backlog errors."
                ),
            )
        ]


class DigestNotificationDispatcher:
    """Recheck/expand safe original events immediately before the existing transport."""

    def __init__(
        self, coordinator: NotificationDigestCoordinator, fallback: NotificationDispatcher
    ) -> None:
        self.coordinator = coordinator
        self.fallback = fallback
        self.supports_idempotency = fallback.supports_idempotency

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        if not delivery.digest_window_id:
            return await self.fallback.send(delivery, events)
        try:
            original = await self.coordinator.expand_delivery(delivery, events)
        except NotificationDeferredError as exc:
            return NotificationDispatchResult(
                "deferred",
                next_attempt_at=exc.defer_until,
                error_code="digestQuietHours",
                safe_error="Digest delivery is in quiet hours.",
            )
        except DomainError:
            return NotificationDispatchResult(
                "retryableFailure",
                error_code="digestEligibilityUnavailable",
                safe_error="Original digest eligibility could not be verified.",
            )
        if not original:
            return NotificationDispatchResult(
                "suppressed", safe_error="No original digest events remain authorized and opted in."
            )
        return await self.fallback.send(delivery, original)
