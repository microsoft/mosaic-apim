from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from mosaic_api.notifications import (
    CapabilityCheck,
    NotificationEvent,
    NotificationEventType,
    NotificationWorkerState,
)
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository
from mosaic_api.repositories.notification_store import Mutation
from mosaic_api.services.capability_notification_sources import CapabilityNotificationSource
from mosaic_api.services.notification_events import notification_record_id

NOW = datetime(2026, 10, 16, tzinfo=UTC)
WORKER = notification_record_id("capabilitySource", "gatewayTelemetry:rollups", "gateway")


class InterruptedRepository(InMemoryNotificationRepository):
    interrupt: NotificationEventType | None = None
    after_commit = False

    async def _commit(self, tenant: str, mutations: Sequence[Mutation]) -> None:
        events = [
            replacement
            for _, replacement in mutations
            if isinstance(replacement, NotificationEvent)
        ]
        if events:
            assert len(mutations) == 2
            assert any(
                isinstance(replacement, NotificationWorkerState) for _, replacement in mutations
            )
        interrupt = any(event.event_type == self.interrupt for event in events)
        if interrupt:
            self.interrupt = None
            if not self.after_commit:
                raise RuntimeError("transaction interrupted")
        await super()._commit(tenant, mutations)
        if interrupt:
            raise RuntimeError("transaction acknowledgement interrupted")


def setup_source() -> tuple[CapabilityNotificationSource, InterruptedRepository, AsyncMock]:
    repository = InterruptedRepository(clock=lambda: NOW)
    probe = AsyncMock(
        return_value=[
            CapabilityCheck(
                feature="gatewayTelemetry:rollups",
                scope="gateway",
                state="failed",
                checked_at=NOW,
            )
        ]
    )
    return restart_source(repository, probe), repository, probe


def restart_source(
    repository: InMemoryNotificationRepository, probe: AsyncMock
) -> CapabilityNotificationSource:
    return CapabilityNotificationSource(
        repository,
        probe,
        tenant_id="tenant",
        clock=lambda: NOW + timedelta(minutes=4),
    )


def verified(checked_at: datetime) -> list[CapabilityCheck]:
    return [
        CapabilityCheck(
            feature="gatewayTelemetry:rollups",
            scope="gateway",
            state="verified",
            checked_at=checked_at,
            evidence=["Current rollup evidence"],
        )
    ]


@pytest.mark.parametrize("after_commit", [False, True])
async def test_failure_and_open_cursor_are_atomic_before_a_newer_verified_probe(
    after_commit: bool,
) -> None:
    source, repository, probe = setup_source()
    repository.interrupt = NotificationEventType.TELEMETRY_UNAVAILABLE
    repository.after_commit = after_commit
    with pytest.raises(RuntimeError, match="interrupted"):
        await source.reconcile()
    state = await repository.get_worker_state("tenant", WORKER)
    assert bool(state and state.last_error) is after_commit
    assert len(repository.events) == int(after_commit)

    probe.return_value = verified(NOW + timedelta(minutes=1))
    await restart_source(repository, probe).reconcile()
    events = list(repository.events.values())
    if after_commit:
        assert [event.event_type for event in events] == [
            NotificationEventType.TELEMETRY_UNAVAILABLE,
            NotificationEventType.INTEGRATION_RECOVERED,
        ]
        assert events[0].occurred_at == NOW
    else:
        # An aborted transaction never leaves an open episode without its failure event.
        assert not events


@pytest.mark.parametrize("after_commit", [False, True])
async def test_recovery_and_close_cursor_are_atomic_when_next_probe_advances(
    after_commit: bool,
) -> None:
    source, repository, probe = setup_source()
    await source.reconcile()
    recovered_at = NOW + timedelta(minutes=1)
    probe.return_value = verified(recovered_at)
    repository.interrupt = NotificationEventType.INTEGRATION_RECOVERED
    repository.after_commit = after_commit
    with pytest.raises(RuntimeError, match="interrupted"):
        await source.reconcile()
    state = await repository.get_worker_state("tenant", WORKER)
    assert state is not None
    assert (state.last_error is None) is after_commit
    assert len(repository.events) == 1 + int(after_commit)

    newer = NOW + timedelta(minutes=2)
    probe.return_value = verified(newer)
    await restart_source(repository, probe).reconcile()
    await source.reconcile()
    recoveries = [
        event
        for event in repository.events.values()
        if event.event_type == NotificationEventType.INTEGRATION_RECOVERED
    ]
    assert len(recoveries) == 1
    assert recoveries[0].occurred_at == (recovered_at if after_commit else newer)
    assert recoveries[0].created_at == recoveries[0].occurred_at
    state = await repository.get_worker_state("tenant", WORKER)
    assert state is not None and state.last_error is None
    assert state.heartbeat_at == recoveries[0].occurred_at


async def test_recovery_requires_fresh_verified_evidence_and_a_prior_issue() -> None:
    source, repository, probe = setup_source()
    await source.reconcile()
    probe.return_value = verified(NOW - timedelta(hours=1))
    await source.reconcile()
    assert len(repository.events) == 1
    probe.return_value = verified(NOW)
    await source.reconcile()
    assert list(repository.events.values())[-1].event_type == "integration.recovered"
    await source.reconcile()
    assert len(repository.events) == 2


async def test_failure_episode_does_not_change_facts_when_failure_reason_changes() -> None:
    source, repository, probe = setup_source()
    await source.reconcile()
    original = next(iter(repository.events.values())).model_copy(deep=True)
    probe.return_value = [
        probe.return_value[0].model_copy(
            update={"state": "degraded", "checked_at": NOW + timedelta(minutes=1)}
        )
    ]
    await source.reconcile()
    assert list(repository.events.values()) == [original]
    probe.return_value = verified(NOW + timedelta(minutes=2))
    await source.reconcile()
    probe.return_value = [
        CapabilityCheck(
            feature="gatewayTelemetry:rollups",
            scope="gateway",
            state="failed",
            checked_at=NOW + timedelta(minutes=3),
        )
    ]
    await source.reconcile()
    failures = [
        event
        for event in repository.events.values()
        if event.event_type == NotificationEventType.TELEMETRY_UNAVAILABLE
    ]
    assert len(failures) == 2
    assert failures[0].id != failures[1].id


async def test_delivery_health_does_not_recursively_generate_health_notifications() -> None:
    source, repository, probe = setup_source()
    probe.return_value = [
        CapabilityCheck(
            feature="notificationWorker",
            scope="outbox",
            state="failed",
            checked_at=NOW,
        )
    ]
    await source.reconcile()
    assert not repository.events
    assert not repository.records
