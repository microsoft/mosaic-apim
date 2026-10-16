import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from mosaic_api.domain import EnvironmentVerdict, VerdictLevel
from mosaic_api.notifications import (
    NotificationEvent,
    NotificationEventType,
    NotificationWorkerState,
)
from mosaic_api.repositories.cosmos import CosmosRepositoryBase
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository
from mosaic_api.repositories.notification_store import Mutation
from mosaic_api.services.environment_findings import (
    EnvironmentFinding,
    EnvironmentFindingList,
    EnvironmentFindingSubject,
    EnvironmentFindingTarget,
)
from mosaic_api.services.environment_notification_sources import EnvironmentNotificationSource
from mosaic_api.services.notification_events import notification_record_id

NOW = datetime(2026, 10, 16, tzinfo=UTC)


class InterruptedRepository(InMemoryNotificationRepository):
    def __init__(self) -> None:
        super().__init__(clock=lambda: NOW)
        self.attempts: list[NotificationEvent] = []
        self.fail_at: int | None = None
        self.after_commit = False

    async def _commit(self, tenant: str, mutations: Sequence[Mutation]) -> None:
        events = [
            replacement
            for _, replacement in mutations
            if isinstance(replacement, NotificationEvent)
        ]
        if events:
            assert len(events) == 1 and len(mutations) == 2
            assert any(
                isinstance(replacement, NotificationWorkerState) for _, replacement in mutations
            )
            self.attempts.extend(event.model_copy(deep=True) for event in events)
        interrupt = bool(events) and len(self.attempts) == self.fail_at
        if interrupt and not self.after_commit:
            raise RuntimeError("checkpoint transaction interrupted")
        await super()._commit(tenant, mutations)
        if interrupt:
            raise RuntimeError("checkpoint acknowledgement interrupted")


def finding(identifier: str = "finding") -> EnvironmentFinding:
    return EnvironmentFinding(
        id=identifier,
        kind="backendCrossesEnvironments",
        confidence="high",
        gateway_id="gateway",
        gateway_name="Gateway",
        gateway_environment="development",
        subject=EnvironmentFindingSubject(kind="backend", id="backend", name="Backend"),
        target=EnvironmentFindingTarget(
            resource_id="endpoint",
            resource_name="Endpoint",
            environment="production",
            resource_kind="modelEndpoint",
        ),
        verdict=EnvironmentVerdict(
            level=VerdictLevel.BLOCKED,
            reason="Development cannot front production",
            gateway_environment="development",
            endpoint_environment="production",
        ),
        evidence="A backend references the production endpoint",
        message="A development gateway routes to a production endpoint",
    )


@pytest.mark.parametrize("recovering", [False, True], ids=["findings", "recoveries"])
@pytest.mark.parametrize("after_commit", [False, True], ids=["aborted", "lost-ack"])
async def test_checkpoint_failure_preserves_committed_facts_when_scan_timestamp_advances(
    recovering: bool, after_commit: bool
) -> None:
    repository = InterruptedRepository()
    findings = [finding("first"), finding("second")]
    scan = AsyncMock(
        return_value=EnvironmentFindingList(
            items=findings,
            generated_at=NOW - timedelta(minutes=1),
        )
    )
    source = EnvironmentNotificationSource(repository, scan, tenant_id="tenant")
    if recovering:
        await source.reconcile()
    initial_ids = set(repository.events)
    scan.return_value = EnvironmentFindingList(
        items=[] if recovering else findings,
        generated_at=NOW,
    )
    first_attempt = len(repository.attempts)
    repository.fail_at = first_attempt + 2
    repository.after_commit = after_commit
    with pytest.raises(RuntimeError, match="checkpoint"):
        await source.reconcile()
    originals = [
        event.model_copy(deep=True)
        for key, event in repository.events.items()
        if key not in initial_ids
    ]
    assert len(originals) == 1 + int(after_commit)
    state = await repository.get_worker_state("tenant", "environmentFindings")
    assert state is not None and state.checkpoint is not None
    persisted = json.loads(state.checkpoint)["findings"]
    if recovering:
        assert set(persisted) == (set() if after_commit else {"second"})
    else:
        assert set(persisted) == ({"first", "second"} if after_commit else {"first"})

    later = NOW + timedelta(minutes=1)
    scan.return_value.generated_at = later
    await EnvironmentNotificationSource(repository, scan, tenant_id="tenant").reconcile()
    await source.reconcile()
    for original in originals:
        assert await repository.get_event("tenant", original.id) == original
        assert original.occurred_at == NOW and original.created_at == NOW
    transitions = [event for key, event in repository.events.items() if key not in initial_ids]
    assert len(transitions) == 2
    assert transitions[1].occurred_at == (NOW if after_commit else later)
    assert transitions[1].created_at == transitions[1].occurred_at
    assert all(
        event.event_type
        == (
            NotificationEventType.OPERATION_RECOVERED
            if recovering
            else NotificationEventType.READINESS_FINDING
        )
        for event in transitions
    )
    state = await repository.get_worker_state("tenant", "environmentFindings")
    assert state is not None and state.last_succeeded_at == (NOW if after_commit else later)


@pytest.mark.parametrize("recovering", [False, True], ids=["findings", "recoveries"])
async def test_changed_scan_only_transitions_durably_committed_findings(
    recovering: bool,
) -> None:
    repository = InterruptedRepository()
    findings = [finding("first"), finding("second")]
    scan = AsyncMock(
        return_value=EnvironmentFindingList(items=findings, generated_at=NOW - timedelta(minutes=1))
    )
    source = EnvironmentNotificationSource(repository, scan, tenant_id="tenant")
    if recovering:
        await source.reconcile()
    scan.return_value = EnvironmentFindingList(
        items=[] if recovering else findings, generated_at=NOW
    )
    repository.fail_at = len(repository.attempts) + 2
    with pytest.raises(RuntimeError, match="checkpoint"):
        await source.reconcile()
    first = repository.attempts[-2]
    assert await repository.get_event("tenant", first.id) is not None
    assert await repository.get_event("tenant", repository.attempts[-1].id) is None

    scan.return_value = EnvironmentFindingList(
        items=findings if recovering else [],
        generated_at=NOW + timedelta(minutes=1),
    )
    await EnvironmentNotificationSource(repository, scan, tenant_id="tenant").reconcile()
    newest = repository.attempts[-1]
    assert newest.source_id == "first"
    assert newest.event_type == (
        NotificationEventType.READINESS_FINDING
        if recovering
        else NotificationEventType.OPERATION_RECOVERED
    )
    assert newest.occurred_at == NOW + timedelta(minutes=1)
    assert (await repository.get_event("tenant", first.id)) is not None
    assert len(repository.events) == (4 if recovering else 2)


async def test_incomplete_scan_never_claims_recovery_and_recurring_finding_is_new() -> None:
    repository = InterruptedRepository()
    scan = AsyncMock(return_value=EnvironmentFindingList(items=[finding()], generated_at=NOW))
    source = EnvironmentNotificationSource(repository, scan, tenant_id="tenant")
    await source.reconcile()
    first = next(iter(repository.events.values()))
    scan.return_value = EnvironmentFindingList(
        items=[],
        generated_at=NOW + timedelta(minutes=1),
        limitations=["Policy-only backends were not inspected"],
    )
    await source.reconcile()
    assert len(repository.events) == 1
    scan.return_value.limitations = []
    await source.reconcile()
    assert list(repository.events.values())[-1].event_type == "operation.recovered"
    scan.return_value.items = [finding()]
    await source.reconcile()
    assert list(repository.events.values())[-1].id != first.id
    assert len(repository.events) == 3


async def test_legacy_checkpoint_remains_readable() -> None:
    repository = InterruptedRepository()

    def legacy(record: NotificationWorkerState) -> NotificationWorkerState:
        return record.model_copy(
            update={"checkpoint": '{"generation":4,"findings":{"old":"revision"}}'}
        )

    await repository.update_worker_state("tenant", "environmentFindings", legacy)
    scan = AsyncMock(return_value=EnvironmentFindingList(items=[], generated_at=NOW))
    await EnvironmentNotificationSource(repository, scan, tenant_id="tenant").reconcile()
    recovery = next(iter(repository.events.values()))
    assert recovery.event_type == NotificationEventType.OPERATION_RECOVERED
    assert recovery.source_revision == "5:revision"
    state = await repository.get_worker_state("tenant", "environmentFindings")
    assert state is not None and state.checkpoint is not None
    assert json.loads(state.checkpoint) == {"generation": 5, "findings": {}}


class SizeBoundedRepository(InMemoryNotificationRepository):
    max_item_bytes = 0
    max_transaction_bytes = 0
    max_findings = 0

    async def _commit(self, tenant: str, mutations: Sequence[Mutation]) -> None:
        transaction_bytes = 0
        for _, replacement in mutations:
            if replacement is None:
                continue
            document = CosmosRepositoryBase._document(replacement)
            size = len(json.dumps(document).encode("utf-8"))
            assert size < 2 * 1024 * 1024
            self.max_item_bytes = max(self.max_item_bytes, size)
            transaction_bytes += size
            if isinstance(replacement, NotificationWorkerState) and replacement.checkpoint:
                count = len(json.loads(replacement.checkpoint)["findings"])
                assert count <= 2000
                self.max_findings = max(self.max_findings, count)
        assert transaction_bytes < 2 * 1024 * 1024
        self.max_transaction_bytes = max(self.max_transaction_bytes, transaction_bytes)
        await super()._commit(tenant, mutations)


async def test_full_replacement_scan_stays_within_serialized_cosmos_byte_limits() -> None:
    repository = SizeBoundedRepository(clock=lambda: NOW)
    old = {
        f"old-{index:04d}-".ljust(256, "x"): notification_record_id(
            "environmentRevision", str(index)
        )
        for index in range(2000)
    }

    def seed(record: NotificationWorkerState) -> NotificationWorkerState:
        return record.model_copy(
            update={"checkpoint": json.dumps({"generation": 7, "findings": old})}
        )

    await repository.update_worker_state("tenant", "environmentFindings", seed)
    findings = [finding(f"new-{index:04d}-".ljust(256, "x")) for index in range(2000)]
    scan = AsyncMock(return_value=EnvironmentFindingList(items=findings, generated_at=NOW))
    await EnvironmentNotificationSource(repository, scan, tenant_id="tenant").reconcile()
    assert len(repository.events) == 4000
    assert (
        sum(
            event.event_type == NotificationEventType.READINESS_FINDING
            for event in repository.events.values()
        )
        == 2000
    )
    state = await repository.get_worker_state("tenant", "environmentFindings")
    assert state is not None and state.checkpoint is not None
    assert set(json.loads(state.checkpoint)["findings"]) == {item.id for item in findings}
    assert repository.max_findings == 2000
    assert repository.max_item_bytes < 2 * 1024 * 1024
    assert repository.max_transaction_bytes < 2 * 1024 * 1024
