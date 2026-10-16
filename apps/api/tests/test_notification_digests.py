"""Real engine, durable memory/Cosmos digests and fake transports: no cloud calls."""

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import (
    CategoryNotificationPreference,
    ChannelCadence,
    DigestSchedule,
    NotificationAudience,
    NotificationCategory,
    NotificationChannelSettingsInput,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationPreferencesInput,
    NotificationRouteInput,
    TeamsDestination,
    notification_category,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.scheduling import WeeklyTimeWindow
from mosaic_api.services.notification_digests import (
    DIGEST_ORIGIN,
    DigestNotificationDispatcher,
    NotificationDigestCoordinator,
)
from mosaic_api.services.notification_preferences import NotificationPreferenceService
from mosaic_api.services.notifications import (
    NotificationDispatchResult,
    NotificationPartialAudienceError,
    NotificationService,
)
from test_notification_preferences import Directory
from test_notification_repository import NOW, Clock, event
from test_notification_repository import store as store


class Visibility:
    def __init__(self) -> None:
        self.denied: set[str] = set()

    async def __call__(self, source: NotificationEvent, _object_id: str, _persona: str) -> bool:
        return source.id not in self.denied


class Transport:
    supports_idempotency = True

    def __init__(self) -> None:
        self.sent: list[tuple[NotificationDelivery, list[NotificationEvent]]] = []

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        self.sent.append((delivery, list(events)))
        return NotificationDispatchResult("providerAccepted", delivery.provider_operation_id)


def source(
    identity: str = "one",
    occurred_at: datetime = NOW,
    event_type: NotificationEventType = NotificationEventType.REQUEST_APPROVED,
) -> NotificationEvent:
    return event(identity).model_copy(
        update={
            "occurred_at": occurred_at,
            "created_at": occurred_at,
            "updated_at": occurred_at,
            "event_type": event_type,
            "category": notification_category(event_type),
        }
    )


def preferences(
    schedules: list[DigestSchedule] | None = None, *, time_zone: str = "UTC"
) -> NotificationPreferencesInput:
    schedules = schedules or [DigestSchedule(period="daily")]
    return NotificationPreferencesInput(
        time_zone=time_zone,
        categories=[
            CategoryNotificationPreference(
                category=category,
                email=ChannelCadence(
                    mode="digest", digest_periods=[schedule.period for schedule in schedules]
                ),
            )
            for category in NotificationCategory
        ],
        digest_schedules=schedules,
    )


async def setup(
    repository: NotificationPersistence,
    clock: Clock,
    *,
    schedules: list[DigestSchedule] | None = None,
    time_zone: str = "UTC",
) -> tuple[
    NotificationPreferenceService,
    NotificationDigestCoordinator,
    NotificationService,
    Transport,
    Visibility,
    Directory,
]:
    directory = Directory(clock)
    visibility = Visibility()
    prefs = NotificationPreferenceService(
        repository, directory=directory, authorize_recipient=visibility, clock=clock
    )
    await prefs.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    await prefs.save_preferences(
        "tenant", "user", "portal", preferences(schedules, time_zone=time_zone)
    )
    coordinator = NotificationDigestCoordinator(
        repository, prefs, tenant_id="tenant", clock=clock, hosting_verified=True
    )
    transport = Transport()
    engine = NotificationService(
        repository,
        audience_resolver=coordinator,
        delivery_authorizer=prefs,
        dispatchers={"email": DigestNotificationDispatcher(coordinator, transport)},
        clock=clock,
    )
    return prefs, coordinator, engine, transport, visibility, directory


@pytest.mark.parametrize(
    "event_type",
    [
        NotificationEventType.REQUEST_APPROVED,
        NotificationEventType.GRANT_REVOKED,
        NotificationEventType.APPLY_FAILED,
        NotificationEventType.INTEGRATION_FAILED,
    ],
)
async def test_generic_categories_aggregate_and_actually_deliver_only_after_completed_local_period(
    store: tuple[NotificationPersistence, Clock], event_type: NotificationEventType
) -> None:
    repository, clock = store
    _, digests, engine, transport, _, _ = await setup(repository, clock)
    original = source(event_type=event_type)
    await engine.publish(original)
    await engine.run_once("tenant")
    assert not transport.sent
    clock.now = datetime(2026, 10, 17, 8, 59, tzinfo=UTC)
    await digests.reconcile()
    await engine.run_once("tenant")
    assert not transport.sent
    clock.now += timedelta(minutes=1)
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    delivery, originals = transport.sent[0]
    assert [item.id for item in originals] == [original.id]
    assert delivery.digest_window_id
    [synthetic] = [
        item
        for item in [
            await repository.get_event("tenant", identifier) for identifier in delivery.event_ids
        ]
        if item
    ]
    assert synthetic.origin == DIGEST_ORIGIN
    record = await digests.repository.get_window("tenant", delivery.digest_window_id)
    assert record and record.window.end_at <= record.window.due_at <= clock.now
    assert record.ttl >= 86400
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    assert (await digests.readiness_checks())[0].state == "verified"


async def test_partial_admin_outage_preserves_inbox_and_private_digest_registration_and_delivery(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, digests, engine, transport, _, directory = await setup(repository, clock)
    directory.complete = False
    original = source().model_copy(
        update={
            "audience": NotificationAudience(user_object_ids=["user"], include_administrators=True),
        }
    )
    await engine.publish(original)
    with pytest.raises(NotificationPartialAudienceError):
        await digests.resolve(original)
    await engine.run_once("tenant")
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error
    [stream_id] = await digests.repository.list_streams("tenant", now=clock())
    stored = await digests.repository.get_stream("tenant", stream_id)
    assert stored and [item.event_id for item in stored[1].pending] == [original.id]
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    delivery, originals = transport.sent[0]
    assert delivery.recipient_reference == "user" and delivery.persona == "portal"
    assert [item.id for item in originals] == [original.id]
    [pending] = [
        item
        for item in await repository.list_outbox_states("tenant", failed_only=False)
        if item.event_id == original.id
    ]
    assert pending.state == "retrying" and pending.next_attempt_at
    assert (await repository.get_event("tenant", original.id)).fanout_completed_at is None
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error
    directory.complete = True
    clock.now = pending.next_attempt_at
    await engine.run_once("tenant")
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    for object_id in directory.admins:
        assert (await engine.list_inbox("tenant", object_id, "admin")).unread_count == 1
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error is None


async def test_restart_catchup_coalesces_missed_days_and_duplicate_fanout_never_resends(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, _, _ = await setup(repository, clock)
    originals = [source("one"), source("two", NOW + timedelta(days=1))]
    for item in originals:
        await engine.publish(item)
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 21, 9, tzinfo=UTC)
    restarted = NotificationDigestCoordinator(repository, prefs, tenant_id="tenant", clock=clock)
    await asyncio.gather(restarted.reconcile(), digests.reconcile())
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    delivery, payload = transport.sent[0]
    assert {item.id for item in payload} == {item.id for item in originals}
    record = await restarted.repository.get_window("tenant", delivery.digest_window_id or "")
    assert record and record.window.catch_up
    assert record.window.start_at == datetime(2026, 10, 16, tzinfo=UTC)
    for item in originals:
        await restarted.resolve(item)
    await restarted.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1


@pytest.mark.parametrize("after_commit", [False, True])
async def test_window_publish_and_pending_advance_replay_atomically_after_crash(
    store: tuple[NotificationPersistence, Clock], after_commit: bool
) -> None:
    repository, clock = store
    prefs, _, engine, transport, _, _ = await setup(repository, clock)
    await engine.publish(source())
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)

    class Crash:
        armed = True

        def __getattr__(self, name: str) -> Any:
            return getattr(repository, name)

        async def commit_source_event(self, event: NotificationEvent, **kwargs: Any) -> bool:
            if self.armed and not after_commit:
                self.armed = False
                raise ConflictError("Simulated crash before atomic digest publication")
            result = await repository.commit_source_event(event, **kwargs)
            if self.armed:
                self.armed = False
                raise ConflictError("Simulated lost acknowledgement after atomic publication")
            return result

    digest = NotificationDigestCoordinator(
        cast(NotificationPersistence, Crash()), prefs, tenant_id="tenant", clock=clock
    )
    await digest.reconcile()
    await digest.reconcile()
    await engine.run_once("tenant")
    await digest.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    assert len(transport.sent[0][1]) == 1


async def test_send_time_visibility_removal_filters_private_events_and_optout_stops_delivery(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, visibility, _ = await setup(repository, clock)
    first, second = source("first"), source("second")
    await engine.publish(first)
    await engine.publish(second)
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    visibility.denied.add(first.id)
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    assert [item.id for item in transport.sent[0][1]] == [second.id]
    third = source("third", clock.now)
    await engine.publish(third)
    await engine.run_once("tenant")
    clock.now += timedelta(days=1)
    await digests.reconcile()
    await prefs.save_preferences("tenant", "user", "portal", NotificationPreferencesInput())
    await engine.run_once("tenant")
    assert len(transport.sent) == 1


async def test_schedule_edit_rehomes_pending_refs_and_does_not_send_at_old_time(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, _, _ = await setup(repository, clock)
    await engine.publish(source())
    await engine.run_once("tenant")
    await prefs.save_preferences(
        "tenant",
        "user",
        "portal",
        preferences([DigestSchedule(period="daily", delivery_time="11:00")]),
    )
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    await digests.reconcile()
    await engine.run_once("tenant")
    assert not transport.sent
    clock.now += timedelta(hours=2)
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1


@pytest.mark.parametrize(
    ("occurred", "due", "hours"),
    [
        (datetime(2026, 3, 8, 16, tzinfo=UTC), datetime(2026, 3, 9, 6, 30, tzinfo=UTC), 23),
        (datetime(2026, 11, 1, 16, tzinfo=UTC), datetime(2026, 11, 2, 7, 30, tzinfo=UTC), 25),
    ],
)
async def test_dst_local_calendar_windows_are_not_fixed_24_hour_intervals(
    store: tuple[NotificationPersistence, Clock], occurred: datetime, due: datetime, hours: int
) -> None:
    repository, clock = store
    clock.now = occurred
    _, digests, engine, transport, _, _ = await setup(
        repository,
        clock,
        schedules=[DigestSchedule(period="daily", delivery_time="02:30")],
        time_zone="America/New_York",
    )
    await engine.publish(source(occurred_at=occurred))
    await engine.run_once("tenant")
    clock.now = due
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    record = await digests.repository.get_window(
        "tenant", transport.sent[0][0].digest_window_id or ""
    )
    assert record
    assert record.window.end_at - record.window.start_at == timedelta(hours=hours)


async def test_shared_digest_never_collects_private_user_events_or_uses_a_fallback_destination(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, _, _, _ = await setup(repository, clock)
    await prefs.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(teams_channel_enabled=True)
    )
    destination = TeamsDestination(
        id="shared-destination",
        tenant_id="tenant",
        name="Fictional team",
        enabled=True,
        allowed_categories=[NotificationCategory.REQUEST_LIFECYCLE],
        credential_reference_id="opaque-reference",
    )
    await repository.save_teams_destination(
        destination,
        AuditEvent(
            id="audit-team",
            tenant_id="tenant",
            action="create",
            resource_type="teamsDestination",
            resource_id=destination.id,
            actor_object_id="actor",
        ),
    )
    await prefs.save_route(
        "tenant",
        "actor",
        NotificationRouteInput(
            name="Shared team digest",
            enabled=True,
            audience="teamsDestination",
            teams_destination_id=destination.id,
            categories=[NotificationCategory.REQUEST_LIFECYCLE],
            channel="teamsChannel",
            cadence=ChannelCadence(mode="digest", digest_periods=["daily"]),
            digest_schedules=[DigestSchedule(period="daily")],
        ),
    )
    private, shared = (
        source("private"),
        source("shared").model_copy(
            update={"audience": NotificationAudience(include_administrators=True)}
        ),
    )
    teams = Transport()
    shared_engine = NotificationService(
        repository,
        audience_resolver=digests,
        delivery_authorizer=prefs,
        dispatchers={"teamsChannel": DigestNotificationDispatcher(digests, teams)},
        clock=clock,
    )
    await engine.publish(private)
    await engine.publish(shared)
    await shared_engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    await shared_engine.run_once("tenant")
    assert len(teams.sent) == 1
    assert [item.id for item in teams.sent[0][1]] == [shared.id]
    assert teams.sent[0][0].persona is None
    assert teams.sent[0][0].recipient_reference == destination.id


async def test_bounded_batches_deliver_every_event_once_without_unbounded_window_refs(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, digests, engine, transport, _, _ = await setup(repository, clock)
    originals = [source(f"event-{number:03}") for number in range(105)]
    for original in originals:
        await engine.publish(original)
    for _ in range(5):
        await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    for _ in range(5):
        await engine.run_once("tenant")
    payloads = [event.id for _, events in transport.sent for event in events]
    assert len(transport.sent) == 2
    assert len(payloads) == len(set(payloads)) == 105
    assert {item.id for item in originals} == set(payloads)
    assert all(len(events) <= 100 for _, events in transport.sent)


async def test_multiple_explicit_periods_produce_one_delivery_each_without_daily_reaggregation(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, digests, engine, transport, _, _ = await setup(
        repository,
        clock,
        schedules=[
            DigestSchedule(period="daily"),
            DigestSchedule(period="weekly"),
            DigestSchedule(period="monthly"),
        ],
    )
    await engine.publish(source())
    await engine.run_once("tenant")
    for due in [
        datetime(2026, 10, 17, 9, tzinfo=UTC),
        datetime(2026, 10, 19, 9, tzinfo=UTC),
        datetime(2026, 11, 1, 9, tzinfo=UTC),
    ]:
        clock.now = due
        await digests.reconcile()
        await engine.run_once("tenant")
    assert len(transport.sent) == 3
    periods = set()
    for delivery, originals in transport.sent:
        record = await digests.repository.get_window("tenant", delivery.digest_window_id or "")
        assert record and len(originals) == 1
        periods.add(record.window.period)
    assert periods == {"daily", "weekly", "monthly"}


async def test_queued_digest_early_clock_and_quiet_hours_defer_without_provider_attempts(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, _, _ = await setup(repository, clock)
    await engine.publish(source())
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    clock.now -= timedelta(minutes=1)
    await engine.run_once("tenant")
    assert not transport.sent
    clock.now += timedelta(minutes=1)
    opted = preferences().model_copy(
        update={
            "quiet_hours": WeeklyTimeWindow(days_of_week=[5], start_time="08:00", end_time="18:00")
        }
    )
    await prefs.save_preferences("tenant", "user", "portal", opted)
    await engine.run_once("tenant")
    digest_deliveries = [
        job for job in await repository.list_deliveries("tenant") if job.digest_window_id
    ]
    assert len(digest_deliveries) == 1
    assert digest_deliveries[0].attempts == 0
    assert digest_deliveries[0].next_attempt_at == datetime(2026, 10, 17, 18, tzinfo=UTC)
    clock.now = datetime(2026, 10, 17, 18, tzinfo=UTC)
    await engine.run_once("tenant")
    assert len(transport.sent) == 1


async def test_actual_admin_removal_after_window_publication_suppresses_queued_digest(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, _, directory = await setup(repository, clock)
    await prefs.save_preferences("tenant", "admin-one", "admin", preferences())
    await prefs.save_route(
        "tenant",
        "actor",
        NotificationRouteInput(
            name="Selected admins",
            enabled=True,
            audience="selectedAdmins",
            administrator_object_ids=["admin-one"],
            categories=[NotificationCategory.REQUEST_LIFECYCLE],
            channel="email",
            cadence=ChannelCadence(mode="digest", digest_periods=["daily"]),
            digest_schedules=[DigestSchedule(period="daily")],
        ),
    )
    original = source().model_copy(
        update={"audience": NotificationAudience(include_administrators=True)}
    )
    await engine.publish(original)
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    directory.admins.remove("admin-one")
    await engine.run_once("tenant")
    assert not transport.sent
    assert any(
        job.state == "suppressed" and job.digest_window_id
        for job in await repository.list_deliveries("tenant")
    )


async def test_dispatcher_rechecks_authorization_again_after_engine_authorization(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, digests, engine, transport, visibility, _ = await setup(repository, clock)
    original = source()
    await engine.publish(original)
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    original_validator = digests.validate_window

    async def revoke_after_check(
        delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> str | None:
        result = await original_validator(delivery, events)
        visibility.denied.add(original.id)
        return result

    digests.preferences.digest_window_validator = revoke_after_check
    await engine.run_once("tenant")
    assert not transport.sent


async def test_prebuilt_usage_summaries_are_not_registered_for_generic_aggregation(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, digests, _, _, _, _ = await setup(repository, clock)
    prepared = source(event_type=NotificationEventType.USAGE_SUMMARY).model_copy(
        update={
            "origin": "usageProducer:portal",
            "data": source().data.model_copy(update={"reason_code": "digest:daily"}),
        }
    )
    await digests.resolve(prepared)
    assert await digests.repository.list_streams("tenant") == []


async def test_missing_retained_source_keeps_pending_reference_reports_error_and_recovers(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, _, _ = await setup(repository, clock)
    original = source()
    await engine.publish(original)
    await engine.run_once("tenant")

    class MissingSource:
        unavailable = True

        def __getattr__(self, name: str) -> Any:
            return getattr(repository, name)

        async def get_event(self, tenant_id: str, event_id: str) -> NotificationEvent | None:
            if self.unavailable and event_id == original.id:
                return None
            return await repository.get_event(tenant_id, event_id)

    proxy = MissingSource()
    delayed = NotificationDigestCoordinator(
        cast(NotificationPersistence, proxy),
        prefs,
        tenant_id="tenant",
        clock=clock,
        hosting_verified=True,
    )
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await delayed.reconcile()
    assert (await delayed.readiness_checks())[0].state == "needsAction"
    [stream_id] = await digests.repository.list_streams("tenant", now=clock())
    stored = await digests.repository.get_stream("tenant", stream_id)
    assert stored and [item.event_id for item in stored[1].pending] == [original.id]
    assert not transport.sent
    proxy.unavailable = False
    await delayed.reconcile()
    await engine.run_once("tenant")
    assert (await delayed.readiness_checks())[0].state == "verified"
    assert len(transport.sent) == 1
    assert [item.id for item in transport.sent[0][1]] == [original.id]


async def test_earlier_schedule_edit_rehomes_and_delivers_before_old_schedule_without_early_window(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    prefs, digests, engine, transport, _, _ = await setup(repository, clock)
    await engine.publish(source())
    await engine.run_once("tenant")
    await prefs.save_preferences(
        "tenant",
        "user",
        "portal",
        preferences([DigestSchedule(period="daily", delivery_time="07:00")]),
    )
    clock.now = datetime(2026, 10, 17, 6, 59, tzinfo=UTC)
    await digests.reconcile()
    await digests.reconcile()
    await engine.run_once("tenant")
    assert not transport.sent
    clock.now += timedelta(minutes=1)
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1


async def test_non_summary_usage_producer_events_still_get_generic_completed_period_digests(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, digests, engine, transport, _, _ = await setup(repository, clock)
    original = source(event_type=NotificationEventType.QUOTA_REACHED).model_copy(
        update={"origin": "usageProducer:portal"}
    )
    await engine.publish(original)
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    await engine.run_once("tenant")
    assert len(transport.sent) == 1
    assert [item.id for item in transport.sent[0][1]] == [original.id]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("recipient_reference", "different-person"),
        ("persona", "admin"),
        ("channel", "teamsChannel"),
        ("route_ids", ["invented-route"]),
        ("digest_window_id", "different-window"),
        ("tenant_id", "different-tenant"),
    ],
)
async def test_durable_window_rejects_changed_recipient_route_channel_persona_or_tenant(
    store: tuple[NotificationPersistence, Clock],
    field: str,
    value: Any,
) -> None:
    repository, clock = store
    _, digests, engine, transport, _, _ = await setup(repository, clock)
    await engine.publish(source())
    await engine.run_once("tenant")
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await digests.reconcile()
    await engine.run_once("tenant")
    delivery = transport.sent[0][0]
    synthetic = await repository.get_event("tenant", delivery.event_ids[0])
    assert synthetic
    assert await digests.validate_window(delivery.model_copy(update={field: value}), [synthetic])
