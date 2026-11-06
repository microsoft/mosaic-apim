"""Tests for opt-in monthly billing draft scheduling, lease safety, and restart recovery."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnectionCreate,
    BillingConnectorPayloadPart,
    BillingConnectorStepResult,
    BillingPublishRequest,
    BillingRunTriggerRequest,
    BillingSchedule,
)
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AuditEvent,
    KeyVaultSecretId,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, UpstreamAuthorizationError, UpstreamError
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import token_amount
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
)
from mosaic_api.services.billing_scheduler import (
    SCHEDULE_DISABLED_WARNING,
    SCHEDULE_HOSTING_WARNING,
    BillingPricingScheduler,
    billing_run_lease_scope,
    billing_schedule_lease_scope,
    is_schedule_due,
    resolve_schedule_window,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import PricingService
from pydantic import SecretStr

TENANT = "tenant-billing-scheduling"

VALID_AZURE_CSV = (
    b"MeterCategory,MeterSubCategory,MeterName,ResourceId,ResourceLocation,"
    b"Quantity,UnitOfMeasure,CostInBillingCurrency,BillingCurrency,Date,"
    b"PricingModel,ProductName\n"
    b"Foundry Models,Azure OpenAI,gpt-4o input tokens,"
    b"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
    b"/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east,"
    b"eastus2,2000,1K tokens,3.50,USD,2026-09-10,OnDemand,Azure OpenAI gpt-4o\n"
    b"Foundry Models,Azure OpenAI,gpt-4o output tokens,"
    b"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
    b"/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east,"
    b"eastus2,1000,1K tokens,7.00,USD,2026-09-10,OnDemand,Azure OpenAI gpt-4o\n"
)


class _MutableClock:
    def __init__(self, initial: datetime) -> None:
        self.current = initial

    def __call__(self) -> datetime:
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value

    def advance(self, **kwargs: float) -> datetime:
        self.current = self.current + timedelta(**kwargs)
        return self.current


class _FakeSecretVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(
            f"https://contoso-mgmt.vault.azure.net/secrets/{name}"
        )

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        _ = tags
        self.values[secret.versionless] = value.get_secret_value()

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        return self.values.pop(secret.versionless, None) is not None

    async def read(self, uri: str) -> str:
        parsed = KeyVaultSecretId.parse(uri)
        return self.values[parsed.versionless]


class _ScriptedBillingConnector:
    def __init__(
        self,
        provider: Any = "azure",
        *,
        steps: list[BillingConnectorStepResult | Exception] | None = None,
        delay_seconds: float = 0.0,
        on_call: Any = None,
    ) -> None:
        self.provider = provider
        self.steps: list[BillingConnectorStepResult | Exception] = list(steps or [])
        self.delay_seconds = delay_seconds
        self.on_call = on_call
        self.calls: list[tuple[str, str, str | None, str | None]] = []
        self.seen_secrets: list[str | None] = []

    async def check_access(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectionCheckResult:
        self.seen_secrets.append(secret_value)
        return BillingConnectionCheckResult(
            connection_id=connection.id,
            provider=connection.provider,
            status="ready",
            message="Ready.",
        )

    async def start_or_poll_acquisition(
        self,
        connection: BillingConnection,
        run: BillingAcquisitionRun,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectorStepResult:
        self.seen_secrets.append(secret_value)
        self.calls.append(
            (run.id, run.source_month, run.operation_id, run.operation_location)
        )
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.on_call is not None:
            res = self.on_call(connection, run)
            if asyncio.iscoroutine(res):
                await res
        if not self.steps:
            raise AssertionError("No scripted connector steps remaining")
        item = self.steps.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _actor(tenant_id: str = TENANT) -> Actor:
    return Actor(object_id="admin-sched-1", tenant_id=tenant_id)


def _audit(tenant_id: str = TENANT) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant_id,
        action="test.seed",
        resource_type="seed",
        resource_id="seed",
        actor_object_id="admin-sched-1",
    )


async def _build_scheduling_harness(
    *,
    now: datetime = datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
    scheduled_workers_always_on: bool = True,
    enabled: bool = True,
) -> tuple[
    _MutableClock,
    InMemoryPricingRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    _FakeSecretVault,
    PricingService,
    BillingPricingScheduler,
]:
    clock = _MutableClock(now)
    pricing_repo = InMemoryPricingRepository()
    gateway_repo = InMemoryGatewayRepository()
    endpoint_repo = InMemoryModelEndpointRepository()
    vault = _FakeSecretVault()

    aoai_res_east = (
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        "/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east"
    )
    await endpoint_repo.save_endpoint(
        ModelEndpoint(
            id="endpoint-east",
            tenant_id=TENANT,
            name="Contoso AOAI East",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso-aoai-east.openai.azure.com",
            azure_resource_id=aoai_res_east,
            account_name="contoso-aoai-east",
            capabilities=ModelEndpointCapabilities(location="eastus2"),
        ),
        _audit(),
    )
    await endpoint_repo.replace_observed_for_endpoint(
        TENANT,
        "endpoint-east",
        [
            ObservedModelDeployment(
                id="obs-east-gpt4o",
                tenant_id=TENANT,
                endpoint_id="endpoint-east",
                snapshot_id="snap-1",
                deployment_name="gpt-4o-east",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=100,
            )
        ],
        "snap-1",
    )

    service = PricingService(
        pricing_repo,
        endpoint_repository=endpoint_repo,
        gateway_repository=gateway_repo,
        clock=clock,
        secret_store=vault,
        secret_reader=vault.read,
        scheduled_workers_always_on=scheduled_workers_always_on,
    )
    scheduler = BillingPricingScheduler(
        pricing_repo,
        gateway_repo,
        service,
        tenant_id=TENANT,
        interval_seconds=60.0,
        lease_seconds=120.0,
        enabled=enabled,
        scheduled_workers_always_on=scheduled_workers_always_on,
        clock=clock,
    )
    return (
        clock,
        pricing_repo,
        gateway_repo,
        endpoint_repo,
        vault,
        service,
        scheduler,
    )


async def _seed_running_run(service: PricingService) -> str:
    connection = await service.create_billing_connection(
        _actor(),
        BillingConnectionCreate(
            name="Azure Poll Lease",
            provider="azure",
            pricing_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=BillingSchedule(enabled=False),
        ),
    )
    service.register_billing_connector(
        _ScriptedBillingConnector(
            steps=[BillingConnectorStepResult(status="running", operation_id="poll-op")]
        )
    )
    run = await service.start_billing_run(
        _actor(), connection.id, BillingRunTriggerRequest(source_month="2026-09")
    )
    return run.id


def _completed_step(manifest: str = "winner-manifest") -> BillingConnectorStepResult:
    return BillingConnectorStepResult(
        status="completed",
        source_manifest_id=manifest,
        parts=[BillingConnectorPayloadPart(name=f"{manifest}.csv", content=VALID_AZURE_CSV)],
    )


async def _prepare_scheduled_start(repository: InMemoryPricingRepository, run_id: str) -> str:
    initial = await repository.get_billing_run(TENANT, run_id)
    assert initial is not None and initial.connection_id is not None
    await repository.save_billing_run(initial.model_copy(update={"status": "failed"}), _audit())
    connection = await repository.get_billing_connection(TENANT, initial.connection_id)
    assert connection is not None
    await repository.save_billing_connection(
        connection.model_copy(update={"schedule": BillingSchedule(
            enabled=True, day_of_month=1, time_utc="00:00", time_zone="UTC"
        )}), _audit(),
    )
    return billing_schedule_lease_scope(connection.id, initial.source_month)


# -- 1. Schedule due calculation across time zones, year rollover, and leap years ---------------


def test_schedule_due_calculation_utc_timezones_year_rollover_and_leap_february() -> None:
    schedule_utc = BillingSchedule(
        enabled=True,
        day_of_month=3,
        time_utc="04:00",
        time_zone="UTC",
    )

    # Before scheduled hour on day_of_month -> not due
    due_before, month_before = is_schedule_due(
        schedule_utc, datetime(2026, 10, 3, 3, 59, tzinfo=UTC)
    )
    assert due_before is False
    assert month_before == "2026-09"

    # Exactly at scheduled hour on day_of_month -> due for 2026-09
    due_exact, month_exact = is_schedule_due(
        schedule_utc, datetime(2026, 10, 3, 4, 0, tzinfo=UTC)
    )
    assert due_exact is True
    assert month_exact == "2026-09"

    # Year rollover: January 2026 targets December 2025 (2025-12)
    jan_window = resolve_schedule_window(
        schedule_utc, datetime(2026, 1, 3, 4, 0, tzinfo=UTC)
    )
    assert jan_window.is_due is True
    assert jan_window.source_month == "2025-12"
    assert jan_window.period_start == date(2025, 12, 1)
    assert jan_window.period_end == date(2025, 12, 31)
    assert jan_window.days_in_month == 31

    # Timezone boundary at year rollover:
    # 2026-01-01T03:00Z is still 2025-12-31T22:00 in America/New_York (EST, UTC-5)
    schedule_ny = BillingSchedule(
        enabled=True,
        day_of_month=1,
        time_utc="02:00",
        time_zone="America/New_York",
        last_triggered_source_month="2025-11",
    )
    ny_still_dec = resolve_schedule_window(
        schedule_ny, datetime(2026, 1, 1, 3, 0, tzinfo=UTC)
    )
    assert ny_still_dec.local_date == date(2025, 12, 31)
    assert ny_still_dec.source_month == "2025-11"
    assert ny_still_dec.is_due is False

    # Once New York reaches 2026-01-01T02:00 EST (2026-01-01T07:00Z), 2025-12 is due
    ny_jan_due = resolve_schedule_window(
        schedule_ny, datetime(2026, 1, 1, 7, 0, tzinfo=UTC)
    )
    assert ny_jan_due.local_date == date(2026, 1, 1)
    assert ny_jan_due.source_month == "2025-12"
    assert ny_jan_due.is_due is True

    # Leap-year February (2024): on Feb 29, 2024, February is NOT yet complete
    leap_feb_29 = resolve_schedule_window(
        BillingSchedule(
            enabled=True,
            day_of_month=1,
            time_utc="00:00",
            time_zone="UTC",
            last_triggered_source_month="2024-01",
        ),
        datetime(2024, 2, 29, 23, 30, tzinfo=UTC),
    )
    assert leap_feb_29.source_month == "2024-01"
    assert leap_feb_29.is_due is False

    # In March 2024 (leap year), target month is 2024-02 with 29 days (2024-02-01..2024-02-29)
    leap_march = resolve_schedule_window(
        schedule_utc, datetime(2024, 3, 3, 4, 0, tzinfo=UTC)
    )
    assert leap_march.is_due is True
    assert leap_march.source_month == "2024-02"
    assert leap_march.period_start == date(2024, 2, 1)
    assert leap_march.period_end == date(2024, 2, 29)
    assert leap_march.days_in_month == 29

    # In March 2025 (non-leap year), target month is 2025-02 with 28 days (2025-02-01..2025-02-28)
    non_leap_march = resolve_schedule_window(
        schedule_utc, datetime(2025, 3, 3, 4, 0, tzinfo=UTC)
    )
    assert non_leap_march.is_due is True
    assert non_leap_march.source_month == "2025-02"
    assert non_leap_march.period_start == date(2025, 2, 1)
    assert non_leap_march.period_end == date(2025, 2, 28)
    assert non_leap_march.days_in_month == 28


async def test_tokyo_day_one_schedule_waits_for_utc_month_completion() -> None:
    clock, repository, _gateway, _endpoints, _vault, service, scheduler = (
        await _build_scheduling_harness(now=datetime(2026, 10, 31, 15, 0, tzinfo=UTC))
    )
    schedule = BillingSchedule(
        enabled=True, day_of_month=1, time_utc="00:00", time_zone="Asia/Tokyo",
        last_triggered_source_month="2026-09",
    )
    connection = await service.create_billing_connection(
        _actor(),
        BillingConnectionCreate(
            name="Tokyo schedule", provider="azure", pricing_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=schedule,
        ),
    )
    connector = _ScriptedBillingConnector(steps=[BillingConnectorStepResult(status="running")])
    service.register_billing_connector(connector)
    before = resolve_schedule_window(schedule, clock())
    assert before.source_month == "2026-10"
    assert before.is_time_reached
    assert not before.is_due
    assert (await scheduler.run_once()).triggered_run_ids == []
    clock.set(datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC))
    assert not resolve_schedule_window(schedule, clock()).is_due
    assert (await scheduler.run_once()).triggered_run_ids == []
    saved = await repository.get_billing_connection(TENANT, connection.id)
    assert saved is not None
    assert saved.schedule.last_triggered_source_month == "2026-09"
    assert connector.calls == []

    clock.set(datetime(2026, 11, 1, 0, 0, tzinfo=UTC))
    assert resolve_schedule_window(schedule, clock()).is_due
    tick = await scheduler.run_once()
    assert len(tick.triggered_run_ids) == 1
    assert connector.calls[0][1] == "2026-10"
    assert (await scheduler.run_once()).triggered_run_ids == []


@pytest.mark.parametrize(
    ("winner_kind", "contender_kind"),
    [("interactive", "interactive"), ("interactive", "scheduler"), ("scheduler", "interactive")],
)
async def test_every_poll_entry_point_serializes_before_evidence_replacement(
    monkeypatch: pytest.MonkeyPatch, winner_kind: str, contender_kind: str
) -> None:
    clock, repository, gateway, endpoints, vault, service, scheduler = (
        await _build_scheduling_harness()
    )
    run_id = await _seed_running_run(service)
    winner_connector = _ScriptedBillingConnector(steps=[_completed_step()])
    loser_step = _completed_step("different-extract")
    loser_step.parts[0].content = VALID_AZURE_CSV.replace(b"3.50", b"350.00")
    loser_connector = _ScriptedBillingConnector(steps=[loser_step])
    service.register_billing_connector(winner_connector)
    contender = PricingService(
        repository, endpoint_repository=endpoints, gateway_repository=gateway, clock=clock,
        secret_store=vault, secret_reader=vault.read,
        billing_connectors={"azure": loser_connector},
    )
    contender_scheduler = BillingPricingScheduler(repository, gateway, contender, tenant_id=TENANT)
    evidence_started, finish_evidence = asyncio.Event(), asyncio.Event()
    save_evidence = repository.save_billing_evidence_chunks

    async def pause_evidence(tenant_id: str, target_id: str, chunks: Any) -> None:
        evidence_started.set()
        await finish_evidence.wait()
        await save_evidence(tenant_id, target_id, chunks)

    monkeypatch.setattr(repository, "save_billing_evidence_chunks", pause_evidence)
    winner = asyncio.create_task(
        scheduler.run_once() if winner_kind == "scheduler"
        else service.poll_billing_run(_actor(), run_id)
    )
    try:
        await asyncio.wait_for(evidence_started.wait(), timeout=5)
        if contender_kind == "scheduler":
            tick = await contender_scheduler.run_once()
            assert tick.lease_conflicts == [billing_run_lease_scope(run_id)]
            assert tick.polled_run_ids == []
        else:
            with pytest.raises(ConflictError):
                await contender.poll_billing_run(_actor(), run_id)
        assert loser_connector.calls == []
    finally:
        finish_evidence.set()
        await winner

    persisted = await repository.get_billing_run(TENANT, run_id)
    evidence = await repository.list_billing_evidence_chunks(TENANT, run_id)
    assert persisted is not None and persisted.status == "readyForReview"
    assert persisted.source_manifest_id == "winner-manifest"
    assert persisted.candidates[0].input_per_million == pytest.approx(1.75)
    assert evidence
    assert (await contender.poll_billing_run(_actor(), run_id)).status == "readyForReview"
    assert await repository.list_billing_evidence_chunks(TENANT, run_id) == evidence
    assert loser_connector.calls == []
    assert gateway.scope_leases == {}


async def test_scheduler_rechecks_retry_after_under_the_shared_poll_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, repository, gateway, _endpoints, _vault, service, scheduler = (
        await _build_scheduling_harness()
    )
    run_id = await _seed_running_run(service)
    connector = _ScriptedBillingConnector(steps=[_completed_step()])
    service.register_billing_connector(connector)
    acquire = gateway.acquire_scope_lease

    async def defer_before_acquire(
        tenant_id: str, scope: str, owner: str, *, lease_seconds: float,
    ) -> None:
        run = await repository.get_billing_run(TENANT, run_id)
        assert run is not None
        await repository.save_billing_run(
            run.model_copy(update={"poll_after": clock() + timedelta(minutes=5)}), _audit()
        )
        await acquire(tenant_id, scope, owner, lease_seconds=lease_seconds)

    monkeypatch.setattr(gateway, "acquire_scope_lease", defer_before_acquire)
    tick = await scheduler.run_once()
    assert tick.polled_run_ids == []
    assert connector.calls == []
    assert gateway.scope_leases == {}


@pytest.mark.parametrize("operation_kind", ["interactive", "scheduler-poll", "scheduler-start"])
async def test_heartbeat_keeps_lease_during_provider_wait_longer_than_lease(
    monkeypatch: pytest.MonkeyPatch, operation_kind: str
) -> None:
    clock, repository, gateway, _endpoints, _vault, service, _scheduler = (
        await _build_scheduling_harness()
    )
    run_id = await _seed_running_run(service)
    if operation_kind == "scheduler-start":
        scope = await _prepare_scheduled_start(repository, run_id)
    else:
        scope = billing_run_lease_scope(run_id)
    lease_seconds = 0.3
    entered, finish = asyncio.Event(), asyncio.Event()
    renewals: list[datetime] = []
    renew = gateway.renew_scope_lease

    async def track_renew(
        tenant_id: str, target: str, owner: str, *, lease_seconds: float,
    ) -> None:
        await renew(tenant_id, target, owner, lease_seconds=lease_seconds)
        renewals.append(utc_now())

    monkeypatch.setattr(gateway, "renew_scope_lease", track_renew)

    async def wait_on_provider(_connection: BillingConnection, _run: BillingAcquisitionRun) -> None:
        entered.set()
        await finish.wait()

    connector = _ScriptedBillingConnector(on_call=wait_on_provider, steps=[_completed_step()])
    service.register_billing_connector(connector)
    scheduler = BillingPricingScheduler(
        repository, gateway, service, tenant_id=TENANT, clock=clock, lease_seconds=lease_seconds
    )
    task = asyncio.create_task(
        service.poll_billing_run(_actor(), run_id, lease_seconds=lease_seconds)
        if operation_kind == "interactive" else scheduler.run_once()
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        initial_expiry = gateway.scope_leases[(TENANT, scope)][1]
        await asyncio.sleep(lease_seconds * 2.5)
        assert utc_now() > initial_expiry
        assert gateway.scope_leases[(TENANT, scope)][1] > utc_now()
        assert len(renewals) >= 5
        with pytest.raises(ConflictError):
            await gateway.acquire_scope_lease(TENANT, scope, "contender", lease_seconds=60)
    finally:
        finish.set()
        result = await task
    if operation_kind == "interactive":
        assert result.status == "readyForReview"
    else:
        assert len(result.completed_run_ids) == 1
        assert result.lease_conflicts == []
    assert len(connector.calls) == 1
    assert gateway.scope_leases == {}


@pytest.mark.parametrize("external_cancel", [False, True])
@pytest.mark.parametrize("suppress_provider_cancellation", [False, True])
@pytest.mark.parametrize("operation_kind", ["interactive", "scheduler-poll", "scheduler-start"])
async def test_heartbeat_ownership_loss_or_cancellation_stops_without_publishing_evidence(
    external_cancel: bool,
    suppress_provider_cancellation: bool,
    operation_kind: str,
) -> None:
    clock, repository, gateway, _endpoints, _vault, service, _scheduler = (
        await _build_scheduling_harness()
    )
    run_id = await _seed_running_run(service)
    scope = (
        await _prepare_scheduled_start(repository, run_id)
        if operation_kind == "scheduler-start" else billing_run_lease_scope(run_id)
    )
    original = await repository.list_billing_runs(TENANT)
    original_connections = await repository.list_billing_connections(TENANT)
    entered, provider_cancelled = asyncio.Event(), asyncio.Event()

    async def wait_on_provider(_connection: BillingConnection, _run: BillingAcquisitionRun) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if not suppress_provider_cancellation:
                raise
        finally:
            provider_cancelled.set()

    service.register_billing_connector(
        _ScriptedBillingConnector(on_call=wait_on_provider, steps=[_completed_step()])
    )
    scheduler = BillingPricingScheduler(
        repository, gateway, service, tenant_id=TENANT, clock=clock, lease_seconds=0.15
    )
    task = asyncio.create_task(
        service.poll_billing_run(_actor(), run_id, lease_seconds=0.15)
        if operation_kind == "interactive" else scheduler.run_once()
    )
    await asyncio.wait_for(entered.wait(), timeout=5)
    if external_cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (TENANT, scope) not in gateway.scope_leases
    else:
        gateway.scope_leases[(TENANT, scope)] = ("winner", utc_now() + timedelta(seconds=60))
        if operation_kind == "interactive":
            with pytest.raises(ConflictError):
                await asyncio.wait_for(task, timeout=5)
        else:
            tick = await asyncio.wait_for(task, timeout=5)
            assert tick.lease_conflicts == [scope]
            assert tick.completed_run_ids == []
            assert tick.triggered_run_ids == []
        assert gateway.scope_leases[(TENANT, scope)][0] == "winner"
    assert provider_cancelled.is_set()
    assert await repository.list_billing_runs(TENANT) == original
    assert await repository.list_billing_connections(TENANT) == original_connections
    assert await repository.list_billing_evidence_chunks(TENANT, run_id) == []


# -- 2. Repeated ticks and concurrent replica races never duplicate a month's acquisition ------


async def test_repeated_ticks_and_concurrent_schedulers_never_duplicate_month_acquisition() -> None:
    (
        clock,
        pricing_repo,
        gateway_repo,
        _endpoint_repo,
        _vault,
        service,
        scheduler_a,
    ) = await _build_scheduling_harness(
        now=datetime(2026, 10, 4, 5, 0, tzinfo=UTC),
    )
    actor = _actor()

    connector = _ScriptedBillingConnector(
        provider="azure",
        delay_seconds=0.02,
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(
                        name="azure-2026-09.csv",
                        content=VALID_AZURE_CSV,
                    )
                ],
                source_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
                source_manifest_id="manifest-sep-2026",
                total_read_bytes=len(VALID_AZURE_CSV),
            )
        ],
    )
    service.register_billing_connector(connector)

    conn = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure EA Scheduled",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=BillingSchedule(
                enabled=True,
                day_of_month=4,
                time_utc="04:00",
                time_zone="UTC",
            ),
        ),
    )

    scheduler_b = BillingPricingScheduler(
        pricing_repo,
        gateway_repo,
        service,
        tenant_id=TENANT,
        clock=clock,
    )
    scheduler_c = BillingPricingScheduler(
        pricing_repo,
        gateway_repo,
        service,
        tenant_id=TENANT,
        clock=clock,
    )

    # Three API replicas tick simultaneously against the shared GatewayRepository lease store
    results = await asyncio.gather(
        scheduler_a.run_once(),
        scheduler_b.run_once(),
        scheduler_c.run_once(),
    )

    all_triggered = [
        run_id for tick in results for run_id in tick.triggered_run_ids
    ]
    assert len(all_triggered) == 1
    assert len(connector.calls) == 1

    # Repeated ticks later in the same month do not trigger another acquisition
    clock.advance(hours=6)
    second_tick = await scheduler_a.run_once()
    assert second_tick.triggered_run_ids == []
    assert len(connector.calls) == 1

    runs = await pricing_repo.list_billing_runs(TENANT, connection_id=conn.id)
    assert len(runs) == 1
    assert runs[0].acquisition_kind == "scheduled"
    assert runs[0].source_month == "2026-09"
    assert runs[0].status == "readyForReview"

    updated_conn = await service.get_billing_connection(actor, conn.id)
    assert updated_conn.schedule.last_triggered_source_month == "2026-09"
    assert updated_conn.schedule.last_run_id == runs[0].id
    assert updated_conn.schedule.last_run_at is not None
    assert updated_conn.schedule.last_error is None

    # Critical invariant: scheduled runs NEVER publish automatically
    publications = await pricing_repo.list_billing_publications(TENANT)
    assert publications == []


# -- 3. Restart-safe recovery of an interrupted running acquisition -----------------------------


async def test_restart_recovery_resumes_interrupted_running_acquisition_to_draft() -> None:
    t0 = datetime(2026, 3, 3, 4, 0, tzinfo=UTC)  # Leap-year March 2024? Let's test 2024-03!
    t0 = datetime(2024, 3, 3, 4, 0, tzinfo=UTC)
    (
        clock,
        pricing_repo,
        gateway_repo,
        endpoint_repo,
        vault,
        service,
        scheduler,
    ) = await _build_scheduling_harness(now=t0)
    actor = _actor()

    leap_feb_csv = VALID_AZURE_CSV.replace(b"2026-09-10", b"2024-02-29")
    poll_ready_at = t0 + timedelta(seconds=60)

    connector_before_restart = _ScriptedBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="running",
                source_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
                operation_id="op-leap-feb-2024",
                operation_location=(
                    "https://management.azure.com/subscriptions/"
                    "00000000-0000-0000-0000-000000000001/providers/"
                    "Microsoft.CostManagement/operationResults/op-leap-feb-2024"
                ),
                poll_after=poll_ready_at,
                status_message="Generating 2024-02 Cost Details report...",
            )
        ],
    )
    service.register_billing_connector(connector_before_restart)

    await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure Leap Year Connection",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            credential_secret=SecretStr("azure-sp-secret-value"),
            schedule=BillingSchedule(
                enabled=True,
                day_of_month=3,
                time_utc="04:00",
                time_zone="UTC",
            ),
        ),
    )

    # Tick 1 starts the scheduled acquisition for 2024-02; connector returns status="running"
    tick1 = await scheduler.run_once()
    assert len(tick1.triggered_run_ids) == 1
    run_id = tick1.triggered_run_ids[0]
    persisted_run = await pricing_repo.get_billing_run(TENANT, run_id)
    assert persisted_run is not None
    assert persisted_run.status == "running"
    assert persisted_run.source_month == "2024-02"
    assert persisted_run.operation_id == "op-leap-feb-2024"
    assert persisted_run.operation_location is not None
    assert persisted_run.poll_after == poll_ready_at

    # Tick before poll_after (T0 + 30s) respects Retry-After backoff and does not poll
    clock.set(t0 + timedelta(seconds=30))
    early_tick = await scheduler.run_once()
    assert early_tick.polled_run_ids == []

    # Simulate API process restart at T0 + 65s with new service and scheduler instances
    clock.set(t0 + timedelta(seconds=65))
    connector_after_restart = _ScriptedBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(
                        name="part-1.csv",
                        content=leap_feb_csv,
                    )
                ],
                source_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
                source_manifest_id="manifest-2024-02",
                operation_id="op-leap-feb-2024",
                total_read_bytes=len(leap_feb_csv),
            )
        ],
    )
    restarted_service = PricingService(
        pricing_repo,
        endpoint_repository=endpoint_repo,
        gateway_repository=gateway_repo,
        clock=clock,
        billing_connectors={"azure": connector_after_restart},
        secret_store=vault,
        secret_reader=vault.read,
        scheduled_workers_always_on=True,
    )
    restarted_scheduler = BillingPricingScheduler(
        pricing_repo,
        gateway_repo,
        restarted_service,
        tenant_id=TENANT,
        clock=clock,
    )

    tick_after_restart = await restarted_scheduler.run_once()
    assert tick_after_restart.polled_run_ids == [run_id]
    assert tick_after_restart.completed_run_ids == [run_id]
    assert tick_after_restart.triggered_run_ids == []

    # Verify the resumed call received the persisted operation_id and operation_location
    assert len(connector_after_restart.calls) == 1
    called_run_id, called_month, called_op_id, called_op_loc = (
        connector_after_restart.calls[0]
    )
    assert called_run_id == run_id
    assert called_month == "2024-02"
    assert called_op_id == "op-leap-feb-2024"
    assert called_op_loc is not None and "op-leap-feb-2024" in called_op_loc

    completed_run = await pricing_repo.get_billing_run(TENANT, run_id)
    assert completed_run is not None
    assert completed_run.status == "readyForReview"
    assert completed_run.poll_after is None
    assert len(completed_run.candidates) == 1
    assert completed_run.candidates[0].input_per_million == pytest.approx(1.75)
    assert completed_run.candidates[0].output_per_million == pytest.approx(7.00)


# -- 4. Lease renewal and lease loss mid-run stop cleanly without overwriting newer state -------


async def test_lease_loss_mid_poll_stops_cleanly_without_overwriting_newer_state() -> None:
    t0 = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    (
        _clock,
        pricing_repo,
        gateway_repo,
        _endpoint_repo,
        _vault,
        service,
        scheduler,
    ) = await _build_scheduling_harness(now=t0)
    actor = _actor()

    conn = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure Lease Test",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=BillingSchedule(enabled=False),
        ),
    )

    # Start an initial running run
    initial_connector = _ScriptedBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="running",
                operation_id="op-lease-race",
                operation_location="https://management.azure.com/op-lease-race",
                poll_after=t0,
            )
        ],
    )
    service.register_billing_connector(initial_connector)
    started = await service.start_billing_run(
        actor, conn.id, BillingRunTriggerRequest(source_month="2026-09")
    )
    assert started.status == "running"

    run_scope = billing_run_lease_scope(started.id)

    async def simulate_takeover_during_poll(
        _connection: BillingConnection, run: BillingAcquisitionRun
    ) -> None:
        # Simulate this worker's lease expiring and another worker taking over the lease
        # and completing the run while this worker was waiting on the upstream download.
        gateway_repo.scope_leases[(TENANT, run_scope)] = (
            "other-replica-owner",
            utc_now() + timedelta(seconds=300),
        )
        current = await pricing_repo.get_billing_run(TENANT, run.id)
        assert current is not None
        await pricing_repo.save_billing_run(
            current.model_copy(
                update={
                    "status": "readyForReview",
                    "status_message": "Completed by other replica",
                    "source_manifest_id": "winner-manifest",
                    "completed_at": t0,
                }
            ),
            _audit(),
        )

    stalled_connector = _ScriptedBillingConnector(
        provider="azure",
        on_call=simulate_takeover_during_poll,
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(
                        name="stale.csv",
                        content=VALID_AZURE_CSV,
                    )
                ],
                source_manifest_id="stale-manifest-should-not-win",
            )
        ],
    )
    service.register_billing_connector(stalled_connector)

    tick = await scheduler.run_once()
    assert run_scope in tick.lease_conflicts
    assert tick.completed_run_ids == []

    # The winning replica's state and lease remain intact
    final_run = await pricing_repo.get_billing_run(TENANT, started.id)
    assert final_run is not None
    assert final_run.status == "readyForReview"
    assert final_run.source_manifest_id == "winner-manifest"
    assert final_run.status_message == "Completed by other replica"
    assert gateway_repo.scope_leases[(TENANT, run_scope)][0] == "other-replica-owner"


# -- 5. Failed or empty scheduled refresh preserves approved rates; success stays draft --------


async def test_failed_and_empty_scheduled_refreshes_preserve_active_prices() -> None:
    t_sep = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    (
        clock,
        pricing_repo,
        _gateway_repo,
        _endpoint_repo,
        _vault,
        service,
        scheduler,
    ) = await _build_scheduling_harness(now=t_sep)
    actor = _actor()

    aug_csv = VALID_AZURE_CSV.replace(b"2026-09-10", b"2026-08-15")
    connector = _ScriptedBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(
                        name="aug-2026.csv",
                        content=aug_csv,
                    )
                ],
                source_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
                source_manifest_id="manifest-2026-08",
                total_read_bytes=len(aug_csv),
            )
        ],
    )
    service.register_billing_connector(connector)

    conn = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure Production Billing",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            credential_secret=SecretStr("super-secret-sp-credential"),
            schedule=BillingSchedule(
                enabled=True,
                day_of_month=4,
                time_utc="04:00",
                time_zone="UTC",
            ),
        ),
    )

    # 1. Run and publish August 2026 rates ($1.75 / 1M input, $7.00 / 1M output)
    aug_tick = await scheduler.run_once()
    assert len(aug_tick.triggered_run_ids) == 1
    aug_run_id = aug_tick.triggered_run_ids[0]
    aug_run = await service.get_billing_run(actor, aug_run_id)
    assert aug_run.status == "readyForReview"

    await service.publish_billing_run(
        actor,
        aug_run_id,
        BillingPublishRequest(
            effective_from=date(2026, 9, 5),
            preview_revision=aug_run.preview_revision,
            version=aug_run.version,
        ),
    )

    pricer_before = await service.pricer(TENANT)
    rate_before = pricer_before.rate(
        "endpoint-east/gpt-4o-east",
        date(2026, 10, 5),
    )
    assert rate_before.entry is not None
    assert rate_before.entry.origin == "billing"
    assert rate_before.entry.input_per_million == pytest.approx(1.75)
    assert rate_before.entry.output_per_million == pytest.approx(7.00)
    cost_before_amount = token_amount(rate_before.entry, 1_000_000, 1_000_000)
    assert cost_before_amount == pytest.approx(8.75)

    versions_before = await pricing_repo.list_price_versions(TENANT)
    publications_before = await pricing_repo.list_billing_publications(TENANT)
    assert len(versions_before) == 1
    assert len(publications_before) == 1

    # 2. Advance to October 2026 (target month 2026-09): scheduled run fails with auth error
    clock.set(datetime(2026, 10, 4, 5, 0, tzinfo=UTC))
    connector.steps.append(
        UpstreamAuthorizationError(
            "Cost Management Reader permission denied (token=super-secret-sp-credential)"
        )
    )
    fail_tick = await scheduler.run_once()
    assert len(fail_tick.triggered_run_ids) == 1
    assert fail_tick.failed_run_ids == fail_tick.triggered_run_ids
    failed_run = await service.get_billing_run(actor, fail_tick.triggered_run_ids[0])
    assert failed_run.status == "failed"
    assert failed_run.completed_at == clock()
    assert "super-secret-sp-credential" not in (failed_run.status_message or "")
    assert "[REDACTED]" in (failed_run.status_message or "")

    conn_after_fail = await service.get_billing_connection(actor, conn.id)
    assert conn_after_fail.schedule.last_triggered_source_month == "2026-09"
    assert conn_after_fail.schedule.last_run_id == failed_run.id
    assert conn_after_fail.schedule.last_error == failed_run.status_message
    assert "super-secret-sp-credential" not in (conn_after_fail.schedule.last_error or "")

    # Active pricing and published versions are completely untouched
    assert await pricing_repo.list_price_versions(TENANT) == versions_before
    assert await pricing_repo.list_billing_publications(TENANT) == publications_before

    # 3. Advance to November 2026 (target month 2026-10): scheduled run returns empty dataset
    clock.set(datetime(2026, 11, 4, 5, 0, tzinfo=UTC))
    connector.steps.append(
        BillingConnectorStepResult(
            status="completed",
            parts=[],
            status_message="Azure Cost Management reported NoDataFound for 2026-10.",
            warnings=["Azure Cost Management returned NoDataFound for 2026-10."],
        )
    )
    empty_tick = await scheduler.run_once()
    assert len(empty_tick.triggered_run_ids) == 1
    assert empty_tick.failed_run_ids == empty_tick.triggered_run_ids
    empty_run = await service.get_billing_run(actor, empty_tick.triggered_run_ids[0])
    assert empty_run.status == "failed"
    assert "NoDataFound" in (empty_run.status_message or "")

    conn_after_empty = await service.get_billing_connection(actor, conn.id)
    assert conn_after_empty.schedule.last_triggered_source_month == "2026-10"
    assert conn_after_empty.schedule.last_error is not None
    assert "NoDataFound" in conn_after_empty.schedule.last_error

    # 4. Poll failure on an in-flight scheduled run also records failed status and last_error
    clock.set(datetime(2026, 12, 4, 5, 0, tzinfo=UTC))
    connector.steps.extend(
        [
            BillingConnectorStepResult(
                status="running",
                operation_id="op-2026-11",
                operation_location="https://management.azure.com/op-2026-11",
                poll_after=datetime(2026, 12, 4, 5, 1, tzinfo=UTC),
            ),
            UpstreamError("Download blob storage partition timed out"),
        ]
    )
    start_dec_tick = await scheduler.run_once()
    assert len(start_dec_tick.triggered_run_ids) == 1
    in_flight_id = start_dec_tick.triggered_run_ids[0]
    assert (await service.get_billing_run(actor, in_flight_id)).status == "running"

    clock.set(datetime(2026, 12, 4, 5, 2, tzinfo=UTC))
    poll_fail_tick = await scheduler.run_once()
    assert poll_fail_tick.polled_run_ids == [in_flight_id]
    assert poll_fail_tick.failed_run_ids == [in_flight_id]
    polled_failed_run = await service.get_billing_run(actor, in_flight_id)
    assert polled_failed_run.status == "failed"
    assert "timed out" in (polled_failed_run.status_message or "")

    conn_after_poll_fail = await service.get_billing_connection(actor, conn.id)
    assert conn_after_poll_fail.schedule.last_error == polled_failed_run.status_message

    # Approved rates and Pricer output are still completely unchanged
    pricer_after = await service.pricer(TENANT)
    rate_after = pricer_after.rate(
        "endpoint-east/gpt-4o-east",
        date(2026, 12, 4),
    )
    assert rate_after.entry is not None
    assert rate_after.entry.origin == "billing"
    assert token_amount(rate_after.entry, 1_000_000, 1_000_000) == pytest.approx(
        cost_before_amount
    )
    assert await pricing_repo.list_price_versions(TENANT) == versions_before
    assert await pricing_repo.list_billing_publications(TENANT) == publications_before


# -- 6. Readiness warnings, PricingOverview flags, and FastAPI lifespan wiring -----------------


async def test_scheduler_readiness_and_overview_expose_always_on_warnings() -> None:
    (
        _clock,
        _pricing_repo,
        _gateway_repo,
        _endpoint_repo,
        _vault,
        service,
        scheduler,
    ) = await _build_scheduling_harness(
        scheduled_workers_always_on=False,
        enabled=True,
    )
    actor = _actor()

    # Before any connection enables a schedule: ready=True and no warning
    initial_status = await scheduler.readiness()
    assert initial_status.enabled is True
    assert initial_status.scheduled_workers_always_on is False
    assert initial_status.active_scheduled_connections == 0
    assert initial_status.ready is True
    assert initial_status.warning is None

    initial_overview = await service.overview(actor)
    assert initial_overview.billing_scheduler_always_on is False
    assert initial_overview.scheduled_workers_always_on is False
    assert initial_overview.scheduled_billing_connections == 0
    assert initial_overview.billing_schedule_warning is None

    # Enable schedule on a connection while scheduled_workers_always_on is False -> warns
    await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Scheduled AWS CUR",
            provider="aws",
            pricing_cloud="aws-bedrock",
            aws_role_arn="arn:aws:iam::123456789012:role/MosaicCurReader",
            aws_cur_bucket="contoso-cur-bucket",
            schedule=BillingSchedule(enabled=True, day_of_month=3, time_utc="04:00"),
        ),
    )

    warn_status = await scheduler.readiness()
    assert warn_status.active_scheduled_connections == 1
    assert warn_status.ready is False
    assert warn_status.warning == SCHEDULE_HOSTING_WARNING
    assert SCHEDULE_HOSTING_WARNING in warn_status.warnings

    warn_overview = await service.overview(actor)
    assert warn_overview.scheduled_billing_connections == 1
    assert warn_overview.billing_schedule_warning == SCHEDULE_HOSTING_WARNING


def test_app_wiring_exposes_billing_scheduler_and_settings() -> None:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["Admin", "User"],
        billing_refresh_enabled=True,
        billing_refresh_interval_seconds=180,
        scheduled_workers_always_on=False,
    )
    with TestClient(create_app(settings)) as client:
        scheduler: BillingPricingScheduler = client.app.state.billing_pricing_scheduler
        assert scheduler is not None
        assert scheduler.enabled is True
        assert scheduler.interval_seconds == 180
        assert scheduler.scheduled_workers_always_on is False

        overview_resp = client.get("/api/v1/pricing")
        assert overview_resp.status_code == 200, overview_resp.text
        overview_json = overview_resp.json()
        assert overview_json["billingSchedulerAlwaysOn"] is False
        assert overview_json["scheduledWorkersAlwaysOn"] is False

        sched_resp = client.get("/api/v1/pricing/billing/scheduler")
        assert sched_resp.status_code == 200, sched_resp.text
        sched_json = sched_resp.json()
        assert sched_json["enabled"] is True
        assert sched_json["scheduledWorkersAlwaysOn"] is False
        assert sched_json["activeScheduledConnections"] == 0


async def test_multi_partition_lease_renewal_and_existing_on_demand_run_suppression() -> None:
    t0 = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    (
        _clock,
        pricing_repo,
        gateway_repo,
        _endpoint_repo,
        _vault,
        service,
        scheduler,
    ) = await _build_scheduling_harness(now=t0)
    actor = _actor()

    part1 = (
        b"MeterCategory,MeterSubCategory,MeterName,ResourceId,ResourceLocation,"
        b"Quantity,UnitOfMeasure,CostInBillingCurrency,BillingCurrency,Date,"
        b"PricingModel,ProductName\n"
        b"Foundry Models,Azure OpenAI,gpt-4o input tokens,"
        b"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        b"/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east,"
        b"eastus2,2000,1K tokens,3.50,USD,2026-09-10,OnDemand,Azure OpenAI gpt-4o\n"
    )
    part2 = (
        b"MeterCategory,MeterSubCategory,MeterName,ResourceId,ResourceLocation,"
        b"Quantity,UnitOfMeasure,CostInBillingCurrency,BillingCurrency,Date,"
        b"PricingModel,ProductName\n"
        b"Foundry Models,Azure OpenAI,gpt-4o output tokens,"
        b"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
        b"/providers/Microsoft.CognitiveServices/accounts/contoso-aoai-east,"
        b"eastus2,1000,1K tokens,7.00,USD,2026-09-10,OnDemand,Azure OpenAI gpt-4o\n"
    )

    connector = _ScriptedBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(name="part-0001.csv", content=part1),
                    BillingConnectorPayloadPart(name="part-0002.csv", content=part2),
                ],
                source_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
                source_manifest_id="multi-part-manifest",
                total_read_bytes=len(part1) + len(part2),
            )
        ],
    )
    service.register_billing_connector(connector)

    conn = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure Multi-Partition",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=BillingSchedule(
                enabled=True,
                day_of_month=4,
                time_utc="04:00",
                time_zone="UTC",
            ),
        ),
    )

    renewed_scopes: list[str] = []
    real_renew = scheduler.renew_scope_lease

    async def tracking_renew(scope: str) -> None:
        renewed_scopes.append(scope)
        await real_renew(scope)

    scheduler.renew_scope_lease = tracking_renew  # type: ignore[method-assign]

    tick = await scheduler.run_once()
    assert len(tick.triggered_run_ids) == 1
    expected_scope = billing_schedule_lease_scope(conn.id, "2026-09")
    # Renewed after connector call, per partition, and around chunk persistence
    assert renewed_scopes.count(expected_scope) >= 4
    assert gateway_repo.scope_leases == {}

    # Also test that an existing on-demand non-failed run suppresses scheduled trigger
    conn2 = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure Already Fetched On Demand",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=BillingSchedule(
                enabled=True,
                day_of_month=4,
                time_utc="04:00",
                time_zone="UTC",
            ),
        ),
    )
    connector.steps.append(
        BillingConnectorStepResult(
            status="completed",
            parts=[
                BillingConnectorPayloadPart(
                    name="on-demand.csv", content=VALID_AZURE_CSV
                )
            ],
            source_manifest_id="on-demand-manifest",
        )
    )
    on_demand_run = await service.start_billing_run(
        actor,
        conn2.id,
        BillingRunTriggerRequest(source_month="2026-09"),
    )
    assert on_demand_run.acquisition_kind == "connected"
    assert on_demand_run.status == "readyForReview"

    # Tick now skips conn2 because a non-failed run already exists for (conn2.id, 2026-09)
    calls_before = len(connector.calls)
    tick2 = await scheduler.run_once()
    assert tick2.triggered_run_ids == []
    assert conn2.id in tick2.skipped_connection_ids
    assert len(connector.calls) == calls_before
    assert len(await pricing_repo.list_billing_runs(TENANT, connection_id=conn2.id)) == 1


async def test_non_ai_only_dataset_and_disabled_worker_readiness() -> None:
    t0 = datetime(2026, 10, 4, 5, 0, tzinfo=UTC)
    (
        _clock,
        _pricing_repo,
        _gateway_repo,
        _endpoint_repo,
        _vault,
        service,
        scheduler,
    ) = await _build_scheduling_harness(
        now=t0,
        scheduled_workers_always_on=True,
        enabled=False,
    )
    actor = _actor()

    non_ai_csv = (
        b"MeterCategory,MeterSubCategory,MeterName,ResourceId,ResourceLocation,"
        b"Quantity,UnitOfMeasure,CostInBillingCurrency,BillingCurrency,Date,"
        b"PricingModel,ProductName\n"
        b"Storage,Blob,Hot LRS Data Stored,"
        b"/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-storage"
        b"/providers/Microsoft.Storage/storageAccounts/contosostorage,"
        b"eastus2,100,1 GB,2.00,USD,2026-09-10,OnDemand,Blob Storage\n"
    )
    connector = _ScriptedBillingConnector(
        provider="azure",
        steps=[
            BillingConnectorStepResult(
                status="completed",
                parts=[
                    BillingConnectorPayloadPart(
                        name="storage-only.csv", content=non_ai_csv
                    )
                ],
                source_manifest_id="storage-only-manifest",
            )
        ],
    )
    service.register_billing_connector(connector)

    conn = await service.create_billing_connection(
        actor,
        BillingConnectionCreate(
            name="Azure Non-AI Only",
            provider="azure",
            pricing_cloud="commercial",
            azure_cloud="commercial",
            azure_scope="/subscriptions/00000000-0000-0000-0000-000000000001",
            schedule=BillingSchedule(
                enabled=True,
                day_of_month=4,
                time_utc="04:00",
                time_zone="UTC",
            ),
        ),
    )

    # Readiness reports SCHEDULE_DISABLED_WARNING when enabled=False and a connection is scheduled
    disabled_status = await scheduler.readiness()
    assert disabled_status.enabled is False
    assert disabled_status.ready is False
    assert disabled_status.warning == SCHEDULE_DISABLED_WARNING

    # Direct run_once with non-AI-only rows marks the scheduled run failed with last_error
    tick = await scheduler.run_once()
    assert len(tick.triggered_run_ids) == 1
    assert tick.failed_run_ids == tick.triggered_run_ids
    run_detail = await service.get_billing_run(actor, tick.triggered_run_ids[0])
    assert run_detail.status == "failed"
    assert run_detail.raw_row_count == 1
    assert run_detail.eligible_row_count == 0
    assert run_detail.candidate_count == 0
    assert "No eligible AI model usage rows" in (run_detail.status_message or "")

    updated_conn = await service.get_billing_connection(actor, conn.id)
    assert updated_conn.schedule.last_error == run_detail.status_message
