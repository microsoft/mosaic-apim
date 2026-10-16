from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import NotificationWorkerState
from mosaic_api.repositories.base import GatewayRepository
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.scheduled_features import ScheduledFeatureWorker

NOW = datetime(2026, 10, 16, tzinfo=UTC)


def worker() -> tuple[ScheduledFeatureWorker, AsyncMock, AsyncMock, AsyncMock]:
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    repository.get_worker_state.return_value = None
    coordination = AsyncMock(spec=GatewayRepository)
    action = AsyncMock()
    return (
        ScheduledFeatureWorker(
            repository,
            coordination,
            action,
            tenant_id="tenant",
            name="sources",
            interval_seconds=30,
            clock=lambda: NOW,
        ),
        repository,
        coordination,
        action,
    )


async def test_worker_only_reports_success_after_processing_and_releases_lease() -> None:
    service, repository, coordination, action = worker()
    await service.run_once()
    action.assert_awaited_once()
    coordination.release_scope_lease.assert_awaited_once()
    change = repository.update_worker_state.await_args.args[2]
    state = change(NotificationWorkerState(id="state", tenant_id="tenant", worker="sources"))
    assert state.heartbeat_at == NOW and state.last_succeeded_at == NOW
    assert state.last_error is None


async def test_processing_failure_is_recorded_without_copying_secret_exception() -> None:
    service, repository, coordination, action = worker()
    action.side_effect = RuntimeError("secret-webhook-token")
    with pytest.raises(RuntimeError):
        await service.run_once()
    change = repository.update_worker_state.await_args.args[2]
    state = change(NotificationWorkerState(id="state", tenant_id="tenant", worker="sources"))
    assert state.last_error and "secret-webhook-token" not in state.last_error
    assert state.last_succeeded_at is None
    coordination.release_scope_lease.assert_awaited_once()


async def test_another_worker_lease_does_not_report_a_failure_or_run_action() -> None:
    service, repository, coordination, action = worker()
    coordination.acquire_scope_lease.side_effect = ConflictError("busy")
    await service.run_once()
    action.assert_not_awaited()
    repository.update_worker_state.assert_not_awaited()
    coordination.release_scope_lease.assert_not_awaited()
