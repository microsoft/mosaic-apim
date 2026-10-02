from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from loganalytics_double import FakeLogs, GatewayCall
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    ModelApi,
    ModelProvider,
    Principal,
    PrincipalKind,
    Publication,
    new_id,
    utc_now,
)
from mosaic_api.integrations.loganalytics import Row
from mosaic_api.repositories.memory import InMemoryDirectoryRepository
from mosaic_api.repositories.memory_entitlements import InMemoryEntitlementRepository
from mosaic_api.repositories.memory_gateway import InMemoryGatewayRepository
from mosaic_api.repositories.memory_usage import InMemoryUsageRollupRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.usage import usage_freshness
from mosaic_api.services.usage_rollup import (
    BACKFILL_DAYS_PER_CYCLE,
    TooManyRefreshesError,
    UsageRollupService,
    merge_run,
)
from mosaic_api.usage_telemetry import (
    LATENCY_BUCKET_COUNT,
    UsageRollupState,
    iso_day,
    subscription_attribution_key,
    usage_rollup_state_id,
)

TENANT = "tenant-rollup"
NOW = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)
TODAY = NOW.date()
GATEWAY = "gateway-rollup"
RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
    "/providers/Microsoft.ApiManagement/service/gateway-rollup"
)
ALICE = "alice-oid"
BOB = "bob-oid"
CAROL = "carol-oid"
APP = "app-oid"
ANALYSTS = "analysts-oid"


def _audit(kind: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type=kind,
        resource_id=kind,
        actor_object_id="tester",
    )


@dataclass
class RollupHarness:
    now: datetime
    directory: InMemoryDirectoryRepository
    entitlements: InMemoryEntitlementRepository
    gateways: InMemoryGatewayRepository
    rollups: InMemoryUsageRollupRepository
    logs: FakeLogs
    service: UsageRollupService

    async def seed(self) -> None:
        await self.gateways.save_gateway(
            Gateway(
                id=GATEWAY,
                tenant_id=TENANT,
                name="Rollup gateway",
                azure_resource_id=RESOURCE_ID,
                subscription_id="00000000-0000-0000-0000-000000000000",
                resource_group="rg",
                service_name="gateway-rollup",
                environment="test",
            ),
            _audit("gateway"),
        )
        await self.gateways.save_publication(
            Publication(
                id="publication-chat",
                tenant_id=TENANT,
                gateway_id=GATEWAY,
                model_endpoint_id="endpoint-chat",
                deployment_name="chat-prod",
                provider=ModelProvider.AZURE_OPENAI,
                display_name="Chat",
                api_name="chat",
                api_path="chat",
                backend_name="chat",
                fragment_name="chat",
                product_name="chat",
                subscription_name="chat",
                shape_version="v1",
                api_shape=ApiShape.AZURE_OPENAI,
            ),
            _audit("publication"),
        )
        await self.gateways.save_model_api(
            ModelApi(
                id="model-api-chat",
                tenant_id=TENANT,
                gateway_id=GATEWAY,
                api_name="chat",
                display_name="Chat",
                path="chat",
                publication_id="publication-chat",
            ),
            _audit("model-api"),
        )
        for object_id, kind, label in (
            (ALICE, PrincipalKind.USER, "Alice"),
            (BOB, PrincipalKind.USER, "Bob"),
            (CAROL, PrincipalKind.USER, "Carol"),
            (APP, PrincipalKind.SERVICE_PRINCIPAL, "Client app"),
            (ANALYSTS, PrincipalKind.SECURITY_GROUP, "Analysts"),
        ):
            await self.directory.create_principal(
                Principal(
                    id=f"principal-{object_id}",
                    tenant_id=TENANT,
                    object_id=object_id,
                    kind=kind,
                    label=label,
                ),
                _audit("principal"),
            )
        await self.save_grant("grant-alice", "user", "principal-alice-oid", "k-alice", "sub-alice")
        await self.save_grant("grant-bob", "user", "principal-bob-oid", "k-bob", None)
        await self.save_grant("grant-app", "application", "principal-app-oid", None, "sub-app")
        await self.save_grant(
            "grant-analysts",
            "securityGroup",
            "principal-analysts-oid",
            "k-analysts",
            None,
            per_member=True,
        )

    async def save_grant(
        self,
        entitlement_id: str,
        subject_kind: str,
        subject_id: str,
        key: str | None,
        subscription: str | None,
        *,
        per_member: bool = False,
    ) -> Entitlement:
        return await self.entitlements.save_entitlement(
            Entitlement(
                id=entitlement_id,
                tenant_id=TENANT,
                subject=EntitlementSubject(kind=subject_kind, id=subject_id),
                resource=EntitlementResource(kind="modelApi", id="model-api-chat"),
                binding=EntitlementBinding(
                    gateway_id=GATEWAY,
                    attribution_key=key,
                    attribution_per_member=per_member,
                    apim_subscription_name=subscription,
                    source=BindingSource.ORCHESTRATED,
                ),
            ),
            _audit("entitlement"),
        )

    async def run_until_done(self, *, limit: int = 10) -> UsageRollupState:
        states: list[UsageRollupState] = []
        for _ in range(limit):
            states = await self.service.run_cycle()
            if states and states[0].backfill_status != "running":
                return states[0]
        raise AssertionError(f"backfill did not finish: {states}")

    async def total_requests(self, day: str, *, period: str = "day") -> int:
        summaries = await self.rollups.list_summaries(
            TENANT,
            period=period,
            start=day,
            end=day,
            dimensions=["total"],
            gateway_ids=[GATEWAY],
        )
        return sum(entry.metrics.requests for summary in summaries for entry in summary.entries)

    async def summary_entry_requests(self, dimension: str, key: str, day: str) -> int:
        summaries = await self.rollups.list_summaries(
            TENANT,
            period="day",
            start=day,
            end=day,
            dimensions=[dimension],
            gateway_ids=[GATEWAY],
        )
        return sum(
            entry.metrics.requests
            for summary in summaries
            for entry in summary.entries
            if entry.key == key
        )


@pytest.fixture
def harness() -> RollupHarness:
    return build_harness()


def build_harness() -> RollupHarness:
    logs = FakeLogs(clock=lambda: NOW)
    rollups = InMemoryUsageRollupRepository()
    gateways = InMemoryGatewayRepository()
    entitlements = InMemoryEntitlementRepository()
    directory = InMemoryDirectoryRepository()
    service = UsageRollupService(
        rollups,
        gateway_repository=gateways,
        entitlement_repository=entitlements,
        directory_repository=directory,
        logs=logs,
        telemetry=None,
        tenant_id=TENANT,
        backfill_max_days=10,
        clock=lambda: NOW,
        owner_id="rollup-a",
    )
    return RollupHarness(
        now=NOW,
        directory=directory,
        entitlements=entitlements,
        gateways=gateways,
        rollups=rollups,
        logs=logs,
        service=service,
    )


def _call(
    *,
    age: int = 0,
    hour: int = 10,
    minute: int = 0,
    second: int = 0,
    grant: str = "k-alice",
    member: str = ALICE,
    subscription: str = "",
    traced: bool = True,
    response_code: int = 200,
    backend_code: int = 200,
    total_time_ms: int = 120,
    prompt_tokens: int | None = 10,
    completion_tokens: int | None = 5,
    client_app: str = "",
) -> GatewayCall:
    day = NOW.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=age)
    return GatewayCall(
        time=day.replace(hour=hour, minute=minute, second=second),
        api="chat",
        grant=grant,
        member=member,
        subscription=subscription,
        traced=traced,
        response_code=response_code,
        backend_code=backend_code,
        total_time_ms=total_time_ms,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        deployment="chat-prod",
        model="gpt-4o",
        client_app=client_app,
    )


async def test_an_active_lease_makes_another_service_skip_the_gateway(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call()]
    harness.gateways.scope_leases[(TENANT, f"usage-rollup:{GATEWAY}")] = (
        "other-rollup",
        utc_now() + timedelta(minutes=5),
    )

    states = await harness.service.run_cycle()

    assert states == []
    assert harness.logs.queries == []


async def test_an_expired_lease_is_taken_over(harness: RollupHarness) -> None:
    await harness.seed()
    harness.logs.calls = [_call()]
    harness.gateways.scope_leases[(TENANT, f"usage-rollup:{GATEWAY}")] = (
        "other-rollup",
        utc_now() - timedelta(seconds=1),
    )

    state = (await harness.service.run_cycle())[0]

    assert state.last_error is None
    assert state.last_written > 0
    assert (TENANT, f"usage-rollup:{GATEWAY}") not in harness.gateways.scope_leases


async def test_the_lease_is_released_after_a_successful_cycle(harness: RollupHarness) -> None:
    await harness.seed()
    harness.logs.calls = [_call()]

    state = (await harness.service.run_cycle())[0]

    assert state.last_error is None
    assert harness.gateways.scope_leases == {}


async def test_rerunning_an_unchanged_cycle_writes_no_rollups(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(), _call(age=1)]
    await harness.run_until_done()
    upserted = harness.rollups.upserted
    deleted = harness.rollups.deleted

    [state] = await harness.service.run_cycle()

    assert state.last_written == 0
    assert harness.rollups.upserted == upserted
    assert harness.rollups.deleted == deleted


async def test_a_changed_recent_day_rewrites_only_changed_buckets(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(), _call(age=1)]
    await harness.run_until_done()
    before = harness.rollups.upserted
    harness.logs.calls.append(_call(second=1))

    [state] = await harness.service.run_cycle()

    assert state.last_written > 0
    assert state.last_written == harness.rollups.upserted - before
    assert await harness.total_requests(iso_day(TODAY)) == 2
    assert await harness.total_requests(iso_day(TODAY - timedelta(days=1))) == 1


async def test_late_calls_for_yesterday_are_picked_up_by_the_grace_window(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(age=1)]
    await harness.run_until_done()
    harness.logs.calls.append(_call(age=1, second=1))

    await harness.service.run_cycle()

    assert await harness.total_requests(iso_day(TODAY - timedelta(days=1))) == 2


async def test_late_calls_older_than_the_grace_window_wait_for_backfill(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    old_day = TODAY - timedelta(days=3)
    harness.logs.calls = [_call(age=3)]
    await harness.run_until_done()
    harness.logs.calls.append(_call(age=3, second=1))

    await harness.service.run_cycle()

    assert await harness.total_requests(iso_day(old_day)) == 1


async def test_backfill_reads_late_calls_older_than_the_grace_window(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    old_day = TODAY - timedelta(days=3)
    harness.logs.calls = [_call(age=3)]
    await harness.run_until_done()
    harness.logs.calls.append(_call(age=3, second=1))

    await harness.service.request_backfill(Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY, 5)
    await harness.run_until_done()

    assert await harness.total_requests(iso_day(old_day)) == 2


async def test_trace_attribution_wins_over_subscription_attribution(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [
        _call(grant="k-alice", member=ALICE, subscription="sub-app", traced=True)
    ]

    await harness.run_until_done()

    facts = await harness.rollups.list_facts(
        TENANT, start_day=iso_day(TODAY), end_day=iso_day(TODAY)
    )
    assert [(fact.entitlement_id, fact.link, fact.link_key) for fact in facts] == [
        ("grant-alice", "trace", "k-alice")
    ]


async def test_subscription_attribution_is_used_when_trace_is_absent(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(traced=False, subscription="sub-app", member="", grant="")]

    await harness.run_until_done()

    facts = await harness.rollups.list_facts(
        TENANT, start_day=iso_day(TODAY), end_day=iso_day(TODAY)
    )
    assert [
        (fact.entitlement_id, fact.link, fact.link_key, fact.caller_object_id)
        for fact in facts
    ] == [
        (
            "grant-app",
            "subscription",
            subscription_attribution_key(GATEWAY, "sub-app"),
            APP,
        )
    ]


async def test_unknown_subscription_calls_are_reported_as_unattributed(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(traced=False, subscription="mystery", member="", grant="")]

    await harness.run_until_done()

    assert (
        await harness.summary_entry_requests("unattributed", "chat|mystery", iso_day(TODAY)) == 1
    )


async def test_unknown_trace_keys_are_kept_as_trace_facts_for_later_registry_matches(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(grant="k-future", member="future-user")]

    await harness.run_until_done()

    facts = await harness.rollups.list_facts(
        TENANT, start_day=iso_day(TODAY), end_day=iso_day(TODAY)
    )
    assert [
        (fact.link, fact.link_key, fact.entitlement_id, fact.caller_object_id)
        for fact in facts
    ] == [
        ("trace", "k-future", None, "future-user")
    ]


async def test_historical_registry_keys_still_attribute_after_a_binding_changes(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    await harness.service.sync_registry(TENANT, now=NOW - timedelta(days=1))
    await harness.save_grant("grant-alice", "user", "principal-alice-oid", "k-alice-new", None)
    harness.logs.calls = [_call(grant="k-alice", member=ALICE)]

    await harness.run_until_done()

    facts = await harness.rollups.list_facts(
        TENANT, start_day=iso_day(TODAY), end_day=iso_day(TODAY)
    )
    assert [(fact.entitlement_id, fact.link_key, fact.caller_object_id) for fact in facts] == [
        ("grant-alice", "k-alice", ALICE)
    ]


async def test_security_group_grants_are_attributed_to_the_calling_member(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [
        _call(grant="k-analysts", member=ALICE),
        _call(grant="k-analysts", member=CAROL, second=1),
    ]

    await harness.run_until_done()

    facts = await harness.rollups.list_facts(
        TENANT, start_day=iso_day(TODAY), end_day=iso_day(TODAY)
    )
    assert sorted(
        (fact.entitlement_id, fact.caller_object_id, fact.per_member) for fact in facts
    ) == [
        ("grant-analysts", ALICE, True),
        ("grant-analysts", CAROL, True),
    ]


async def test_application_grants_attribute_to_the_application_object(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(traced=False, subscription="sub-app", grant="", member="")]

    await harness.run_until_done()

    facts = await harness.rollups.list_facts(
        TENANT, start_day=iso_day(TODAY), end_day=iso_day(TODAY)
    )
    assert facts[0].caller_object_id == APP
    assert facts[0].entitlement_id == "grant-app"


async def test_backfill_advances_over_bounded_slices_until_done(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(age=age) for age in range(12)]
    harness.service = UsageRollupService(
        harness.rollups,
        gateway_repository=harness.gateways,
        entitlement_repository=harness.entitlements,
        directory_repository=harness.directory,
        logs=harness.logs,
        telemetry=None,
        tenant_id=TENANT,
        backfill_max_days=12,
        clock=lambda: NOW,
        owner_id="rollup-bounded",
    )

    [first] = await harness.service.run_cycle()
    [second] = await harness.service.run_cycle()

    assert first.backfill_status == "running"
    assert first.backfill_next == iso_day(TODAY - timedelta(days=2 + BACKFILL_DAYS_PER_CYCLE))
    assert second.backfill_status == "done"
    assert second.backfill_next is None
    assert second.data_available_from == iso_day(TODAY - timedelta(days=11))


async def test_request_backfill_moves_data_available_from_backward(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(), _call(age=6)]
    harness.service = UsageRollupService(
        harness.rollups,
        gateway_repository=harness.gateways,
        entitlement_repository=harness.entitlements,
        directory_repository=harness.directory,
        logs=harness.logs,
        telemetry=None,
        tenant_id=TENANT,
        backfill_max_days=2,
        clock=lambda: NOW,
        owner_id="rollup-short",
    )
    state = await harness.run_until_done()
    assert state.data_available_from == iso_day(TODAY - timedelta(days=1))

    await harness.service.request_backfill(Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY, 7)
    state = await harness.run_until_done()

    assert state.data_available_from == iso_day(TODAY - timedelta(days=6))
    assert await harness.total_requests(iso_day(TODAY - timedelta(days=6))) == 1


async def test_backfill_never_lowers_an_existing_old_day(harness: RollupHarness) -> None:
    await harness.seed()
    old_day = TODAY - timedelta(days=3)
    harness.logs.calls = [_call(age=3), _call(age=3, second=1)]
    await harness.run_until_done()
    harness.logs.calls = [_call(age=3)]

    await harness.service.request_backfill(Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY, 5)
    state = await harness.run_until_done()

    assert state.last_error is None
    assert await harness.total_requests(iso_day(old_day)) == 2


async def test_a_gateway_whose_state_was_saved_before_its_first_run_still_backfills(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(age=6)]
    # Enabling telemetry and asking for a refresh both save the state before the job has run.
    await harness.service.request_refresh(Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY)

    state = await harness.run_until_done()

    assert state.data_available_from == iso_day(TODAY - timedelta(days=9))
    assert await harness.total_requests(iso_day(TODAY - timedelta(days=6))) == 1


@pytest.mark.parametrize(
    ("states", "gateways", "expected", "updated_at"),
    [
        (
            [
                UsageRollupState(
                    id="s1",
                    tenant_id=TENANT,
                    gateway_id="g1",
                    last_success_at=NOW - timedelta(minutes=10),
                    queried_through=NOW,
                    data_available_from=iso_day(TODAY),
                )
            ],
            1,
            "current",
            NOW - timedelta(minutes=10),
        ),
        (
            [
                UsageRollupState(
                    id="s1",
                    tenant_id=TENANT,
                    gateway_id="g1",
                    last_success_at=NOW - timedelta(hours=2),
                    queried_through=NOW,
                    data_available_from=iso_day(TODAY),
                )
            ],
            1,
            "delayed",
            NOW - timedelta(hours=2),
        ),
        (
            [
                UsageRollupState(
                    id="s1",
                    tenant_id=TENANT,
                    gateway_id="g1",
                    last_error_at=NOW - timedelta(minutes=1),
                    last_error="forbidden",
                )
            ],
            1,
            "failing",
            None,
        ),
        ([], 1, "pending", None),
        ([], 0, "notLinked", None),
    ],
)
def test_usage_freshness_reports_the_expected_status(
    states: list[UsageRollupState],
    gateways: int,
    expected: str,
    updated_at: datetime | None,
) -> None:
    freshness = usage_freshness(
        states, gateways=gateways, now=NOW, interval=timedelta(minutes=15)
    )

    assert freshness.status == expected
    assert freshness.updated_at == updated_at
    assert freshness.interval_minutes == 15


def test_usage_freshness_uses_the_slowest_successful_gateway() -> None:
    fast = UsageRollupState(
        id="fast",
        tenant_id=TENANT,
        gateway_id="fast",
        last_success_at=NOW - timedelta(minutes=5),
        data_available_from=iso_day(TODAY - timedelta(days=10)),
    )
    slow = UsageRollupState(
        id="slow",
        tenant_id=TENANT,
        gateway_id="slow",
        last_success_at=NOW - timedelta(minutes=20),
        data_available_from=iso_day(TODAY - timedelta(days=3)),
    )

    freshness = usage_freshness(
        [fast, slow], gateways=2, now=NOW, interval=timedelta(minutes=15)
    )

    assert freshness.status == "current"
    assert freshness.updated_at == slow.last_success_at
    assert freshness.data_from == iso_day(TODAY - timedelta(days=3))


async def test_month_folding_keeps_calls_on_each_side_of_a_month_boundary() -> None:
    now = datetime(2026, 9, 1, 1, 15, tzinfo=UTC)
    local = harness_with_time(now)
    await local.seed()
    local.logs.calls = [
        GatewayCall(
            time=datetime(2026, 8, 31, 23, 59, tzinfo=UTC),
            api="chat",
            grant="k-alice",
            member=ALICE,
            prompt_tokens=1,
            completion_tokens=1,
            deployment="chat-prod",
            model="gpt-4o",
        ),
        GatewayCall(
            time=datetime(2026, 9, 1, 0, 0, tzinfo=UTC),
            api="chat",
            grant="k-alice",
            member=ALICE,
            prompt_tokens=1,
            completion_tokens=1,
            deployment="chat-prod",
            model="gpt-4o",
        ),
    ]

    await local.run_until_done()

    assert await local.total_requests("2026-08-01", period="month") == 1
    assert await local.total_requests("2026-09-01", period="month") == 1


async def test_query_windows_are_whole_utc_hours(harness: RollupHarness) -> None:
    await harness.seed()
    harness.logs.calls = [_call(hour=15, minute=29)]

    await harness.service.run_cycle()

    assert harness.logs.answered
    for start, end in harness.logs.answered:
        assert start.tzinfo == UTC
        assert end.tzinfo == UTC
        assert (start.minute, start.second, start.microsecond) == (0, 0, 0)
        assert (end.minute, end.second, end.microsecond) == (0, 0, 0)


async def test_queries_split_until_each_part_fits_log_analytics_limits(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(hour=hour) for hour in range(12)]
    harness.logs.max_hours = 6

    await harness.service.run_cycle()

    assert harness.logs.answered
    assert max(end - start for start, end in harness.logs.answered) <= timedelta(hours=6)
    assert await harness.total_requests(iso_day(TODAY)) == 12


async def test_denials_are_counted_by_reason_caller_client_and_api(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [
        GatewayCall(
            time=NOW.replace(hour=10, minute=0, second=0),
            api="chat",
            traced=False,
            response_code=403,
            backend_code=0,
            denial_reason="no-grant",
            denied_caller=BOB,
            client_app="portal",
        )
    ]

    await harness.run_until_done()

    assert (
        await harness.summary_entry_requests(
            "denial", f"no-grant|{BOB}|portal|chat", iso_day(TODAY)
        )
        == 1
    )
    assert await harness.total_requests(iso_day(TODAY)) == 1


async def test_backend_429s_are_separate_from_gateway_throttling(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [
        _call(backend_code=429),
        _call(second=1, response_code=429, backend_code=0),
    ]

    await harness.run_until_done()

    summaries = await harness.rollups.list_summaries(
        TENANT,
        period="day",
        start=iso_day(TODAY),
        end=iso_day(TODAY),
        dimensions=["total"],
        gateway_ids=[GATEWAY],
    )
    [entry] = [entry for summary in summaries for entry in summary.entries if entry.key == ""]
    assert entry.metrics.requests == 2
    assert entry.metrics.backend_throttled == 1
    assert entry.metrics.throttled == 1


async def test_latency_buckets_are_folded_from_gateway_rows(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [
        _call(total_time_ms=50),
        _call(second=1, total_time_ms=250),
        _call(second=2, total_time_ms=60_000),
    ]

    await harness.run_until_done()

    summaries = await harness.rollups.list_summaries(
        TENANT,
        period="day",
        start=iso_day(TODAY),
        end=iso_day(TODAY),
        dimensions=["total"],
        gateway_ids=[GATEWAY],
    )
    [entry] = [entry for summary in summaries for entry in summary.entries if entry.key == ""]
    assert entry.metrics.latency == [1, 0, 1, 0, 0, 0, 0, 0, 0, 1]
    assert len(entry.metrics.latency) == LATENCY_BUCKET_COUNT


async def test_month_refolding_does_not_lower_when_daily_summaries_disappear(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    harness.logs.calls = [_call(age=1), _call(age=2)]
    await harness.run_until_done()
    month = iso_day(TODAY.replace(day=1))
    assert await harness.total_requests(month, period="month") == 2
    day_two = iso_day(TODAY - timedelta(days=2))
    items = await harness.rollups.list_summaries(
        TENANT,
        period="day",
        start=day_two,
        end=day_two,
        dimensions=["total"],
        gateway_ids=[GATEWAY],
    )
    await harness.rollups.delete_rollups(TENANT, [item.id for item in items])

    gateway = await harness.gateways.get_gateway(TENANT, GATEWAY)
    assert gateway is not None
    written = await harness.service._fold_month(gateway, TODAY.replace(day=1))

    assert written == 0
    assert await harness.total_requests(month, period="month") == 2


def harness_with_time(now: datetime) -> RollupHarness:
    logs = FakeLogs(clock=lambda: now)
    rollups = InMemoryUsageRollupRepository()
    gateways = InMemoryGatewayRepository()
    entitlements = InMemoryEntitlementRepository()
    directory = InMemoryDirectoryRepository()
    service = UsageRollupService(
        rollups,
        gateway_repository=gateways,
        entitlement_repository=entitlements,
        directory_repository=directory,
        logs=logs,
        telemetry=None,
        tenant_id=TENANT,
        backfill_max_days=3,
        clock=lambda: now,
        owner_id=f"rollup-{now.isoformat()}",
    )
    return RollupHarness(
        now=now,
        directory=directory,
        entitlements=entitlements,
        gateways=gateways,
        rollups=rollups,
        logs=logs,
        service=service,
    )


async def test_request_backfill_records_an_audit_event(harness: RollupHarness) -> None:
    await harness.seed()

    state = await harness.service.request_backfill(
        Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY, 5
    )

    assert state.backfill_from == iso_day(TODAY - timedelta(days=4))
    [event] = [
        event
        for event in harness.entitlements.audit_events.values()
        if event.action == "gateway.telemetryBackfillRequested"
    ]
    assert event.details == {"days": 5, "from": state.backfill_from}


async def test_requesting_a_one_day_backfill_finishes_immediately(
    harness: RollupHarness,
) -> None:
    await harness.seed()

    state = await harness.service.request_backfill(
        Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY, 1
    )

    assert state.backfill_status == "done"
    assert state.backfill_next is None


async def test_refresh_requests_are_recorded_per_gateway(harness: RollupHarness) -> None:
    await harness.seed()

    await harness.service.request_refresh(Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY)

    state = await harness.rollups.get_rollup_state(TENANT, GATEWAY)
    assert state is not None
    assert state.refresh_requested_at == NOW
    assert state.id == usage_rollup_state_id(TENANT, GATEWAY)


@dataclass
class InterruptedLogs:
    """Gateway logs that run ``action`` during the next query, as a request made mid-run would."""

    logs: FakeLogs
    action: Callable[[], Awaitable[object]] | None = None

    async def query(
        self, resource_id: str, query: str, *, start: datetime, end: datetime
    ) -> list[Row]:
        action, self.action = self.action, None
        if action is not None:
            await action()
        return await self.logs.query(resource_id, query, start=start, end=end)


def _interruptible(harness: RollupHarness) -> InterruptedLogs:
    logs = InterruptedLogs(harness.logs)
    harness.service = UsageRollupService(
        harness.rollups,
        gateway_repository=harness.gateways,
        entitlement_repository=harness.entitlements,
        directory_repository=harness.directory,
        logs=logs,
        telemetry=None,
        tenant_id=TENANT,
        backfill_max_days=10,
        clock=lambda: NOW,
        owner_id="rollup-interrupted",
    )
    return logs


async def test_a_backfill_requested_during_a_run_is_kept(harness: RollupHarness) -> None:
    await harness.seed()
    harness.logs.calls = [_call(), _call(age=20)]
    await harness.run_until_done()
    logs = _interruptible(harness)
    requested: list[UsageRollupState] = []

    async def request_backfill() -> None:
        actor = Actor(object_id=ALICE, tenant_id=TENANT)
        requested.append(await harness.service.request_backfill(actor, GATEWAY, 30))

    logs.action = request_backfill
    [state] = await harness.service.run_cycle()

    assert requested[0].backfill_status == "running"
    assert state.backfill_status == "running"
    assert state.backfill_from == iso_day(TODAY - timedelta(days=29))
    assert state.backfill_next == iso_day(TODAY - timedelta(days=2))
    assert state.last_success_at == NOW
    state = await harness.run_until_done()
    assert state.data_available_from == iso_day(TODAY - timedelta(days=29))
    assert await harness.total_requests(iso_day(TODAY - timedelta(days=20))) == 1


async def test_a_refresh_requested_during_a_run_keeps_its_cooldown(
    harness: RollupHarness,
) -> None:
    await harness.seed()
    await harness.run_until_done()
    logs = _interruptible(harness)
    actor = Actor(object_id=ALICE, tenant_id=TENANT)
    logs.action = lambda: harness.service.request_refresh(actor, GATEWAY)

    [state] = await harness.service.run_cycle()

    assert state.refresh_requested_at == NOW
    with pytest.raises(TooManyRefreshesError):
        await harness.service.request_refresh(actor, GATEWAY)


async def test_a_shorter_backfill_keeps_a_longer_one_going(harness: RollupHarness) -> None:
    await harness.seed()
    harness.logs.calls = [_call(age=9)]
    [first] = await harness.service.run_cycle()
    assert first.backfill_next == iso_day(TODAY - timedelta(days=9))

    state = await harness.service.request_backfill(
        Actor(object_id=ALICE, tenant_id=TENANT), GATEWAY, 3
    )

    assert state.backfill_from == iso_day(TODAY - timedelta(days=9))
    assert state.backfill_next == iso_day(TODAY - timedelta(days=2))
    await harness.run_until_done()
    assert await harness.total_requests(iso_day(TODAY - timedelta(days=9))) == 1


def test_a_run_keeps_the_outcome_of_enabling_telemetry_meanwhile() -> None:
    before = UsageRollupState(
        id="state", tenant_id=TENANT, gateway_id=GATEWAY, instrumented_apis=["chat"]
    )
    run = before.model_copy(update={"instrumented_apis": ["chat", "embeddings"], "last_rows": 4})
    enabled = before.model_copy(
        update={
            "instrumented_apis": ["tools"],
            "diagnostics_error": "MOSAIC couldn't write the diagnostic for chat",
        }
    )

    merged = merge_run(enabled, before=before, run=run)

    assert merged.instrumented_apis == ["embeddings", "tools"]
    assert merged.diagnostics_error == "MOSAIC couldn't write the diagnostic for chat"
    assert merged.last_rows == 4


def test_a_backfill_requested_during_a_first_run_keeps_its_older_days() -> None:
    before = UsageRollupState(id="state", tenant_id=TENANT, gateway_id=GATEWAY)
    run = before.model_copy(
        update={
            "backfill_from": iso_day(TODAY - timedelta(days=89)),
            "backfill_next": iso_day(TODAY - timedelta(days=9)),
            "backfill_status": "running",
        }
    )
    requested = before.model_copy(
        update={
            "backfill_from": iso_day(TODAY - timedelta(days=6)),
            "backfill_next": iso_day(TODAY - timedelta(days=2)),
            "backfill_status": "running",
        }
    )

    merged = merge_run(requested, before=before, run=run)

    assert (merged.backfill_from, merged.backfill_next, merged.backfill_status) == (
        iso_day(TODAY - timedelta(days=89)),
        iso_day(TODAY - timedelta(days=2)),
        "running",
    )
