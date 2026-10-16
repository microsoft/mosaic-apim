from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mosaic_api.notifications import NotificationWorkerState
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.environment_notification_sources import EnvironmentNotificationSource

NOW = datetime(2026, 10, 16, tzinfo=UTC)


async def test_environment_replay_is_durable_and_incomplete_scan_never_claims_recovery() -> None:
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    state: NotificationWorkerState | None = None

    async def get_state(tenant: str, name: str) -> NotificationWorkerState | None:
        return state

    async def update_state(tenant: str, name: str, change: object) -> NotificationWorkerState:
        nonlocal state
        assert callable(change)
        state = change(state or NotificationWorkerState(id=name, tenant_id=tenant, worker=name))
        assert state
        return state

    repository.get_worker_state.side_effect = get_state
    repository.update_worker_state.side_effect = update_state
    finding = SimpleNamespace(
        id="finding",
        kind="backendCrossesEnvironments",
        gateway_id="gateway",
        gateway_environment="development",
        subject=SimpleNamespace(id="backend"),
        target=SimpleNamespace(
            resource_id="endpoint",
            environment="production",
            resource_kind="modelEndpoint",
        ),
        verdict=SimpleNamespace(model_dump_json=lambda: '{"level":"blocked"}'),
        confidence="high",
    )
    scan = AsyncMock(
        return_value=SimpleNamespace(items=[finding], generated_at=NOW, limitations=[])
    )
    source = EnvironmentNotificationSource(repository, scan, tenant_id="tenant")
    repository.append_event.side_effect = RuntimeError("storage unavailable")
    with pytest.raises(RuntimeError):
        await source.reconcile()
    first = repository.append_event.await_args.args[0].id
    repository.update_worker_state.assert_not_awaited()
    repository.append_event.side_effect = None
    await source.reconcile()
    assert repository.append_event.await_args.args[0].id == first
    scan.return_value = SimpleNamespace(
        items=[],
        generated_at=NOW,
        limitations=["Policy-only backends were not inspected"],
    )
    await source.reconcile()
    assert repository.append_event.await_count == 2
    scan.return_value.limitations = []
    await source.reconcile()
    assert repository.append_event.await_args.args[0].event_type == "operation.recovered"
    scan.return_value.items = [finding]
    await source.reconcile()
    assert repository.append_event.await_args.args[0].id != first
