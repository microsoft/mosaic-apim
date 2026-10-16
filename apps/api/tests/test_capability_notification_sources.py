from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from mosaic_api.notifications import CapabilityCheck, NotificationWorkerState
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.capability_notification_sources import CapabilityNotificationSource

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def setup_source() -> tuple[CapabilityNotificationSource, AsyncMock, AsyncMock]:
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    records: dict[str, NotificationWorkerState] = {}

    async def get_state(tenant: str, name: str) -> NotificationWorkerState | None:
        return records.get(name)

    async def update_state(tenant: str, name: str, change: object) -> NotificationWorkerState:
        assert callable(change)
        state: NotificationWorkerState = change(
            records.get(name)
            or NotificationWorkerState(
                id=name,
                tenant_id=tenant,
                worker=name,
            )
        )
        records[name] = state
        return state

    repository.get_worker_state.side_effect = get_state
    repository.update_worker_state.side_effect = update_state
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
    return (
        CapabilityNotificationSource(
            repository,
            probe,
            tenant_id="tenant",
            clock=lambda: NOW,
        ),
        repository,
        probe,
    )


async def test_failed_event_replays_same_identity_after_publication_failure() -> None:
    source, repository, _ = setup_source()
    repository.append_event.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError):
        await source.reconcile()
    first_id = repository.append_event.await_args.args[0].id
    repository.append_event.side_effect = None
    await source.reconcile()
    assert repository.append_event.await_args.args[0].id == first_id


async def test_recovery_requires_fresh_verified_evidence_and_a_prior_issue() -> None:
    source, repository, probe = setup_source()
    await source.reconcile()
    probe.return_value = [
        CapabilityCheck(
            feature="gatewayTelemetry:rollups",
            scope="gateway",
            state="verified",
            checked_at=NOW - timedelta(hours=1),
            evidence=["Historical result"],
        )
    ]
    await source.reconcile()
    assert repository.append_event.await_count == 1
    probe.return_value = [
        CapabilityCheck(
            feature="gatewayTelemetry:rollups",
            scope="gateway",
            state="verified",
            checked_at=NOW,
            evidence=["Current rollup evidence"],
        )
    ]
    await source.reconcile()
    assert repository.append_event.await_args.args[0].event_type == "integration.recovered"
    await source.reconcile()
    assert repository.append_event.await_count == 2


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
    repository.append_event.assert_not_awaited()
