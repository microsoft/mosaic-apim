import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest
from mosaic_api.domain import AuditEvent
from mosaic_api.notifications import (
    AlertEvidence,
    CapabilityCheck,
    CategoryNotificationPreference,
    ChannelCadence,
    DigestSchedule,
    DigestWatermark,
    DigestWindow,
    NotificationAlertIncident,
    NotificationCategory,
    NotificationEvent,
    NotificationEventType,
    NotificationPersona,
    NotificationPreferences,
    NotificationSubject,
    NotificationWorkerState,
)
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.notification_events import notification_preferences_id
from mosaic_api.services.notification_sources import (
    InMemoryNotificationRunSource,
    NotificationSourceService,
)
from mosaic_api.services.usage_notification_source import (
    UsageCounterObservation,
    UsageNotificationSnapshot,
)
from mosaic_api.services.usage_notifications import UsageNotificationProducer

TENANT = "usage-tenant"
USER = "alice"
NOW = datetime(2026, 3, 9, 14, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Identities:
    def __init__(self, persona: NotificationPersona = "portal") -> None:
        self.persona = persona
        self.present = True

    async def subjects(
        self, tenant_id: str, preferences: Sequence[NotificationPreferences]
    ) -> Sequence[tuple[str, NotificationPersona]]:
        return [(USER, self.persona)]

    async def actor(self, tenant_id: str, object_id: str) -> Actor | None:
        return Actor(object_id=object_id, tenant_id=tenant_id) if self.present else None


class Source:
    def __init__(self) -> None:
        self.used = 85
        self.priced = True
        self.coverage = "complete"
        self.revision = "applied-v1"
        self.access = True
        self.windows: list[DigestWindow] = []
        self.on_observe: Callable[[], Awaitable[None]] | None = None

    async def observe(
        self, actor: Actor, *, now: datetime, window: DigestWindow | None = None
    ) -> UsageNotificationSnapshot:
        if self.on_observe:
            callback, self.on_observe = self.on_observe, None
            await callback()
        if window:
            self.windows.append(window)
        return UsageNotificationSnapshot(
            observed_at=now,
            through=now - timedelta(minutes=15),
            freshness_status="current",
            freshness_updated_at=now,
            freshness_interval_minutes=15,
            coverage="complete",
            calls=self.used,
            tokens=self.used * 10,
            cost_usd=0.25 if self.priced else None,
            unpriced_tokens=0 if self.priced else self.used * 10,
            counters=[]
            if window
            else [
                UsageCounterObservation(
                    scope_id="personal-alice-grant",
                    scope_revision=self.revision,
                    metric="requests",
                    period="Monthly",
                    period_start_at=datetime(now.year, now.month, 1, tzinfo=UTC),
                    period_end_at=(
                        datetime(now.year + 1, 1, 1, tzinfo=UTC)
                        if now.month == 12
                        else datetime(now.year, now.month + 1, 1, tzinfo=UTC)
                    ),
                    subject=NotificationSubject(entitlement_id="grant-alice"),
                    evidence=AlertEvidence(
                        observed_at=now,
                        through=now - timedelta(minutes=15),
                        coverage=self.coverage,
                        priced=self.priced,
                        scope_kind="personal",
                        limit=100,
                        used=self.used,
                    ),
                )
            ],
        )

    async def eligible(self, actor: Actor, snapshot: UsageNotificationSnapshot) -> bool:
        return self.access


def preferences(
    *,
    digest: bool = False,
    persona: NotificationPersona = "portal",
    period: str = "daily",
) -> NotificationPreferences:
    return NotificationPreferences(
        id=notification_preferences_id(TENANT, USER, persona),
        tenant_id=TENANT,
        user_object_id=USER,
        persona=persona,
        created_at=datetime(2026, 3, 7, 15, tzinfo=UTC),
        updated_at=datetime(2026, 3, 7, 15, tzinfo=UTC),
        time_zone="America/New_York",
        categories=[
            CategoryNotificationPreference(
                category=NotificationCategory.USAGE_AND_BUDGETS,
                email=ChannelCadence(mode="digest", digest_periods=[period])
                if digest
                else ChannelCadence(),
            )
        ],
        digest_schedules=[DigestSchedule(period=period)] if digest else [],
    )


async def save(
    repository: InMemoryNotificationRepository, record: NotificationPreferences
) -> NotificationPreferences:
    current = await repository.get_preferences(TENANT, record.user_object_id, record.persona)
    record = record.model_copy(update={"etag": current.etag if current else None})
    return await repository.save_preferences(
        record,
        AuditEvent(
            id=f"audit-{record.persona}-{record.revision}",
            tenant_id=TENANT,
            action="notification.preferences",
            resource_type="notificationPreferences",
            resource_id=record.id,
            actor_object_id=USER,
        ),
    )


def events(repository: InMemoryNotificationRepository) -> list[NotificationEvent]:
    return list(repository.events.values())


def incidents(repository: InMemoryNotificationRepository) -> list[NotificationAlertIncident]:
    return [
        item for item in repository.records.values() if isinstance(item, NotificationAlertIncident)
    ]


async def test_default_in_app_alerts_do_not_enable_recurring_or_external_delivery() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    await producer.reconcile()
    assert len(events(repository)) == 1
    event = events(repository)[0]
    assert event.event_type == NotificationEventType.QUOTA_APPROACHING
    assert event.data.threshold_percent == 80
    assert event.origin == "usageProducer:portal"
    assert event.audience.user_object_ids == [USER]
    assert not event.audience.include_administrators
    assert not source.windows
    assert not any(isinstance(item, DigestWatermark) for item in repository.records.values())


async def test_incident_crossings_cooldown_recovery_version_and_utc_rollover() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    await producer.reconcile()
    first = next(item for item in incidents(repository) if item.threshold_percent == 80)
    await producer.reconcile()
    assert len(events(repository)) == 1
    clock.now += timedelta(days=1)
    await producer.reconcile()
    assert len(events(repository)) == 2
    source.used = 105
    clock.now += timedelta(minutes=1)
    await producer.reconcile()
    assert len(events(repository)) == 3
    assert events(repository)[-1].event_type == NotificationEventType.QUOTA_REACHED
    source.used = 20
    await producer.reconcile()
    assert all(item.state == "recovered" for item in incidents(repository))
    source.used = 85
    clock.now += timedelta(minutes=1)
    await producer.reconcile()
    assert len(events(repository)) == 4
    assert incidents(repository)[0].first_observed_at == first.first_observed_at
    source.revision = "applied-v2"
    await producer.reconcile()
    assert len(events(repository)) == 5
    clock.now = datetime(2026, 4, 1, 0, 1, tzinfo=UTC)
    await producer.reconcile()
    assert len(events(repository)) == 6
    assert any(
        item.period_start_at == datetime(2026, 4, 1, tzinfo=UTC) for item in incidents(repository)
    )


@pytest.mark.parametrize(
    "coverage,priced",
    [
        ("incomplete", True),
        ("delayed", True),
        ("unavailable", True),
        ("complete", False),
    ],
)
async def test_insufficient_evidence_neither_crosses_nor_recovers(
    coverage: str, priced: bool
) -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    source.coverage, source.priced = coverage, priced
    await producer.reconcile()
    assert not events(repository)
    assert all(item.state == "unknown" for item in incidents(repository))
    source.coverage, source.priced = "complete", True
    await producer.reconcile()
    source.used, source.coverage, source.priced = 0, coverage, priced
    clock.now += timedelta(days=2)
    await producer.reconcile()
    assert len(events(repository)) == 1
    assert (
        next(item for item in incidents(repository) if item.threshold_percent == 80).state == "open"
    )
    assert all(item.evidence.coverage == coverage for item in incidents(repository))


async def test_restart_coalesces_completed_local_calendar_windows_and_keeps_evidence() -> None:
    clock, source = Clock(), Source()
    source.used, source.priced = 10, False
    repository = InMemoryNotificationRepository(clock=clock)
    await save(repository, preferences(digest=True))
    for _ in range(2):
        await UsageNotificationProducer(
            repository, source, Identities(), tenant_id=TENANT, clock=clock
        ).reconcile()
    summaries = [
        event
        for event in events(repository)
        if event.event_type == NotificationEventType.USAGE_SUMMARY
    ]
    assert len(summaries) == 1
    window = source.windows[0]
    assert window.start_at == datetime(2026, 3, 7, 5, tzinfo=UTC)
    assert window.end_at == datetime(2026, 3, 9, 4, tzinfo=UTC)
    assert window.catch_up
    assert window.end_at - window.start_at == timedelta(hours=47)
    assert summaries[0].data.cost_usd is None
    assert summaries[0].data.reason_code == "digest:daily"
    assert "Catch-up" in (summaries[0].data.summary or "")
    watermarks = [item for item in repository.records.values() if isinstance(item, DigestWatermark)]
    assert len(watermarks) == 1
    assert watermarks[0].last_completed_end_at == window.end_at
    assert watermarks[0].pending_window is None
    assert watermarks[0].next_due_at == datetime(2026, 3, 10, 13, tzinfo=UTC)
    checkpoints = [
        json.loads(item.checkpoint)
        for item in repository.records.values()
        if isinstance(item, NotificationWorkerState) and item.checkpoint
    ]
    assert any(item.get("eventId") == summaries[0].id for item in checkpoints)


class CrashRepository(InMemoryNotificationRepository):
    fail_next = True

    async def commit_source_event(
        self,
        event: NotificationEvent,
        *,
        watermark: DigestWatermark | None = None,
        incident: NotificationAlertIncident | None = None,
        checkpoint: NotificationWorkerState | None = None,
    ) -> bool:
        if watermark is not None and self.fail_next:
            self.fail_next = False
            raise RuntimeError("Simulated process failure before atomic commit")
        return await super().commit_source_event(
            event, watermark=watermark, incident=incident, checkpoint=checkpoint
        )


async def test_due_intent_survives_crash_and_recovery_coalesces_instead_of_flooding() -> None:
    clock, source = Clock(), Source()
    source.used = 10
    repository = CrashRepository(clock=clock)
    await save(repository, preferences(digest=True))
    await UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert not events(repository)
    pending = next(
        item for item in repository.records.values() if isinstance(item, DigestWatermark)
    )
    assert pending.pending_window is not None
    clock.now += timedelta(days=5)
    await UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert len(events(repository)) == 1
    assert source.windows[-1].start_at == source.windows[0].start_at
    assert source.windows[-1].end_at == datetime(2026, 3, 14, 4, tzinfo=UTC)


async def test_latest_preferences_access_and_admin_role_are_rechecked() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    original = await save(repository, preferences(digest=True))

    async def opt_out() -> None:
        await save(
            repository,
            original.model_copy(
                update={
                    "revision": 2,
                    "categories": [
                        CategoryNotificationPreference(
                            category=NotificationCategory.USAGE_AND_BUDGETS, in_app=False
                        )
                    ],
                }
            ),
        )

    source.on_observe = opt_out
    await UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert not events(repository)
    await save(repository, original.model_copy(update={"revision": 3}))
    source.access = False
    await UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert not events(repository)
    source.access = True
    await save(repository, preferences(digest=True, persona="admin"))
    await UsageNotificationProducer(
        repository, source, Identities("admin"), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert not events(repository)


async def test_competing_workers_share_persisted_subject_lease() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    await asyncio.gather(
        *(
            UsageNotificationProducer(
                repository, source, Identities(), tenant_id=TENANT, clock=clock, owner=owner
            ).reconcile()
            for owner in ("worker-one", "worker-two")
        )
    )
    assert len(events(repository)) == 1


async def test_thresholds_and_repeat_interval_are_user_configurable() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    await save(
        repository,
        preferences().model_copy(
            update={
                "thresholds": [50, 90],
                "repeat_cooldown_seconds": 300,
            }
        ),
    )
    producer = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    await producer.reconcile()
    assert [event.data.threshold_percent for event in events(repository)] == [50]
    clock.now += timedelta(seconds=299)
    await producer.reconcile()
    assert len(events(repository)) == 1
    clock.now += timedelta(seconds=1)
    await producer.reconcile()
    assert len(events(repository)) == 2


async def test_explicit_in_app_summary_schedule_does_not_force_external_opt_in() -> None:
    clock, source = Clock(), Source()
    source.used = 10
    repository = InMemoryNotificationRepository(clock=clock)
    record = preferences().model_copy(update={"digest_schedules": [DigestSchedule(period="daily")]})
    await save(repository, record)
    await UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert len(events(repository)) == 1
    assert events(repository)[0].event_type == NotificationEventType.USAGE_SUMMARY
    watermark = next(
        item for item in repository.records.values() if isinstance(item, DigestWatermark)
    )
    assert watermark.channel == "inApp"
    saved = await repository.get_preferences(TENANT, USER, "portal")
    assert saved is not None and saved.categories[0].email.mode == "off"
    assert saved.categories[0].teams.mode == "off"


async def test_source_reconciliation_calls_registered_producer_and_persists_checkpoint() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    reconcile = NotificationSourceService(
        repository,
        InMemoryNotificationRunSource({}),
        tenant_id=TENANT,
        clock=clock,
        additional_sources=(producer.reconcile,),
    )
    await reconcile.reconcile()
    assert len(events(repository)) == 1
    state = await repository.get_worker_state(TENANT, "notificationSources")
    assert state is not None and state.last_succeeded_at == NOW


async def test_expired_source_lease_cannot_publish_from_a_late_observation() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)

    async def expire() -> None:
        clock.now += timedelta(seconds=301)

    source.on_observe = expire
    await UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert not events(repository)
    state = await repository.get_worker_state(TENANT, "usageNotifications")
    assert state is not None and state.backlog == 1 and state.last_error


async def test_each_event_keeps_its_own_retained_evidence_and_rechecks_delivery_visibility() -> (
    None
):
    clock, source = Clock(), Source()
    source.used = 10
    repository = InMemoryNotificationRepository(clock=clock)
    await save(repository, preferences(digest=True))
    producer = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    await producer.reconcile()
    first = events(repository)[0]
    clock.now += timedelta(days=1)
    source.used = 20
    await producer.reconcile()
    assert len(events(repository)) == 2
    payloads = [
        (record, json.loads(record.checkpoint))
        for record in repository.records.values()
        if isinstance(record, NotificationWorkerState) and record.checkpoint
    ]
    first_evidence = next(
        (record, payload) for record, payload in payloads if payload.get("eventId") == first.id
    )
    assert first_evidence[0].ttl == 90 * 86400
    assert first_evidence[1]["snapshot"]["calls"] == 10
    assert await producer.authorize_delivery(first, USER, "portal")
    assert not await producer.authorize_delivery(first, "bob", "portal")
    assert not await producer.authorize_delivery(first, USER, "admin")
    source.access = False
    assert not await producer.authorize_delivery(first, USER, "portal")


class ManyIdentities(Identities):
    async def subjects(
        self, tenant_id: str, preferences: Sequence[NotificationPreferences]
    ) -> Sequence[tuple[str, NotificationPersona]]:
        return [(f"user-{number:03}", "portal") for number in range(103)]


async def test_bounded_subject_scan_resumes_its_persisted_cursor() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository, source, ManyIdentities(), tenant_id=TENANT, clock=clock
    )
    await producer.reconcile()
    assert len(events(repository)) == 100
    state = await repository.get_worker_state(TENANT, "usageNotifications")
    assert state is not None and state.checkpoint and state.backlog == 3
    await UsageNotificationProducer(
        repository, source, ManyIdentities(), tenant_id=TENANT, clock=clock
    ).reconcile()
    assert len(events(repository)) == 103
    state = await repository.get_worker_state(TENANT, "usageNotifications")
    assert state is not None and state.checkpoint is None and state.backlog == 0


async def authorize_self(
    event: NotificationEvent, object_id: str, persona: NotificationPersona
) -> bool:
    return (
        object_id in event.audience.user_object_ids and event.origin == f"usageProducer:{persona}"
    )


def capability(checks: list[CapabilityCheck], feature: str) -> CapabilityCheck:
    return next(check for check in checks if check.feature == feature)


async def test_readiness_without_cycle_explains_unsupported_and_unverified_saved_settings() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    await save(repository, preferences(digest=True))
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    assert not any(check.state == "verified" for check in checks)
    assert capability(checks, "usageLiveRateWarnings").state == "needsAction"
    assert "unsupported" in (capability(checks, "usageLiveRateWarnings").reason or "")
    assert capability(checks, "usageSharedPoolAlerts").state == "needsAction"
    assert capability(checks, "usageFreshness").state == "unknown"
    assert capability(checks, "usageSummaries").state == "unknown"
    assert all(check.remediation and check.remediation.action for check in checks)
    assert not events(repository)
    assert not any(
        isinstance(record, NotificationWorkerState) for record in repository.records.values()
    )


async def test_readiness_evidence_is_exact_owner_tenant_and_persona_scoped() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    counter = capability(checks, "usageQuotaCounter")
    assert counter.state == "verified"
    assert any("85" in fact for fact in counter.evidence)
    assert capability(checks, "usageFreshness").state == "verified"
    scopes: list[tuple[str, str, NotificationPersona]] = [
        (TENANT, "bob", "portal"),
        (TENANT, USER, "admin"),
        ("other-tenant", USER, "portal"),
    ]
    for tenant, owner, persona in scopes:
        other = await producer.readiness_checks(tenant, owner, persona)
        assert capability(other, "usageFreshness").state == "unknown"
        assert not any(check.feature == "usageQuotaCounter" for check in other)
        assert not any("Last observed usage" in fact for check in other for fact in check.evidence)


async def test_readiness_old_observation_cannot_claim_current_counter_or_pricing_functional() -> (
    None
):
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    clock.now += timedelta(hours=3)
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    assert capability(checks, "usageFreshness").state == "degraded"
    assert capability(checks, "usageCoverage").state == "needsAction"
    assert capability(checks, "usageQuotaCounter").state == "needsAction"
    assert capability(checks, "usagePricing").state == "needsAction"
    assert not any(check.state == "verified" for check in checks)


async def test_readiness_undated_legacy_evidence_is_unknown_not_verified() -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    state = await repository.get_worker_state(
        TENANT, producer._evidence_worker(USER, "portal", "latest")
    )
    assert state is not None and state.checkpoint is not None
    payload = json.loads(state.checkpoint)
    payload.pop("freshnessUpdatedAt")
    updated = state.model_copy(update={"checkpoint": json.dumps(payload)})
    await repository.update_worker_state(
        TENANT, producer._evidence_worker(USER, "portal", "latest"), lambda _: updated
    )
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    assert capability(checks, "usageFreshness").state == "unknown"
    assert not any(check.state == "verified" for check in checks)
    assert clock.now == NOW


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("observedAt", NOW.replace(tzinfo=None).isoformat()),
        ("costUsd", float("nan")),
        ("calls", -1),
    ],
)
async def test_readiness_corrupt_saved_snapshot_is_withheld(field: str, value: object) -> None:
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    state = await repository.get_worker_state(
        TENANT, producer._evidence_worker(USER, "portal", "latest")
    )
    assert state is not None and state.checkpoint is not None
    payload = json.loads(state.checkpoint)
    payload[field] = value
    updated = state.model_copy(update={"checkpoint": json.dumps(payload)})
    await repository.update_worker_state(
        TENANT, producer._evidence_worker(USER, "portal", "latest"), lambda _: updated
    )
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    assert capability(checks, "usageFreshness").state == "unknown"
    assert not any(check.feature == "usageQuotaCounter" for check in checks)
    assert not any(check.state == "verified" for check in checks)


async def test_readiness_unpriced_evidence_has_actionable_price_and_counter_blockers() -> None:
    clock, source = Clock(), Source()
    source.priced = False
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    price = capability(checks, "usagePricing")
    assert price.state == "needsAction"
    assert "not a zero-dollar" in (price.reason or "")
    assert price.remediation and "prices" in price.remediation.action
    assert capability(checks, "usageQuotaCounter").state == "needsAction"


async def test_readiness_revoked_visibility_or_missing_live_role_verifier_withholds_figures() -> (
    None
):
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    source.access = False
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    assert capability(checks, "usageFreshness").state == "unknown"
    assert "withheld" in (capability(checks, "usageFreshness").reason or "")
    assert not any(check.feature == "usageQuotaCounter" for check in checks)
    source.access = True
    without_verifier = UsageNotificationProducer(
        repository, source, Identities(), tenant_id=TENANT, clock=clock
    )
    checks = await without_verifier.readiness_checks(TENANT, USER, "portal")
    assert not any(check.state == "verified" for check in checks)
    assert "role verification" in (capability(checks, "usageFreshness").reason or "")


async def test_readiness_subhour_and_hourly_cost_limitations_are_visible_before_first_cycle() -> (
    None
):
    clock, source = Clock(), Source()
    repository = InMemoryNotificationRepository(clock=clock)
    await save(
        repository, preferences(digest=True).model_copy(update={"time_zone": "Asia/Kolkata"})
    )
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    counts = capability(checks, "usageCalendarSummaryCounts")
    assert counts.state == "needsAction" and "subhour" in (counts.reason or "")
    assert counts.remediation and "UTC" in counts.remediation.action
    assert capability(checks, "usageSummaryPricingGrain").state == "needsAction"
    assert not any(check.state == "verified" for check in checks)


async def test_readiness_completed_self_summary_proves_production_not_recipient_delivery() -> None:
    clock, source = Clock(), Source()
    source.used = 10
    repository = InMemoryNotificationRepository(clock=clock)
    await save(repository, preferences(digest=True))
    producer = UsageNotificationProducer(
        repository,
        source,
        Identities(),
        tenant_id=TENANT,
        authorize_recipient=authorize_self,
        clock=clock,
    )
    await producer.reconcile()
    checks = await producer.readiness_checks(TENANT, USER, "portal")
    summary = capability(checks, "usageSummaries")
    assert summary.state == "verified"
    assert "does not prove" in (summary.reason or "")
    assert capability(checks, "usageCalendarSummaryCounts").state == "verified"
    assert capability(checks, "usageSummaryPricingGrain").state == "needsAction"
