from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings, UsageSourceMode
from mosaic_api.domain import (
    AuditEvent,
    BindingSource,
    DirectoryObject,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementRuntime,
    EntitlementSubject,
    Gateway,
    ModelAccessSettings,
    ModelApi,
    Principal,
    PrincipalKind,
)
from mosaic_api.errors import ValidationError
from mosaic_api.main import create_app
from mosaic_api.services.analytics import AnalyticsFilters
from mosaic_api.services.analytics.export import COLUMNS, cell, filename, table, to_csv
from mosaic_api.services.analytics.limits import HygieneInputs, _status, hygiene_report
from mosaic_api.services.analytics.models import AnalyticsConsumerRow, AnalyticsConsumers
from mosaic_api.services.analytics.rows import (
    Activity,
    Ranked,
    average_backend,
    average_latency,
    denial_label,
    hour_kpis,
    kpis,
    rank,
    share,
)
from mosaic_api.services.analytics.scope import GrantInfo, Scope
from mosaic_api.services.analytics.views import Context, DeploymentInfo, reliability
from mosaic_api.services.analytics.window import Coverage, day_start, resolve_window, split_months
from mosaic_api.services.directory import Actor
from mosaic_api.services.usage import UsageFreshness
from mosaic_api.usage_telemetry import (
    LATENCY_BUCKETS_MS,
    UsageHour,
    UsageMetrics,
    UsageRollupState,
    UsageSummary,
    UsageSummaryEntry,
    latency_percentile,
    usage_rollup_state_id,
)

TENANT = "tenant-test"
GATEWAY = "gateway-prod"
NOW = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)


def _metric(**values: Any) -> UsageMetrics:
    return UsageMetrics(**values)


def _summary(
    dimension: str,
    day: date,
    *entries: UsageSummaryEntry,
    period: str = "day",
) -> UsageSummary:
    return UsageSummary(
        id=f"summary-{period}-{dimension}-{day.isoformat()}",
        tenant_id=TENANT,
        period=period,
        period_start=day.isoformat(),
        bucket=f"{period}:{day.isoformat()}",
        gateway_id=GATEWAY,
        dimension=dimension,
        entries=list(entries),
    )


def _state(
    *,
    first: date,
    through: datetime = NOW,
    gateway_id: str = GATEWAY,
) -> UsageRollupState:
    return UsageRollupState(
        id=usage_rollup_state_id(TENANT, gateway_id),
        tenant_id=TENANT,
        gateway_id=gateway_id,
        last_success_at=through,
        queried_through=through,
        data_available_from=first.isoformat(),
    )


def _audit(kind: str) -> AuditEvent:
    return AuditEvent(
        id=f"audit-{kind}",
        tenant_id=TENANT,
        action="test.seed",
        resource_type=kind,
        resource_id=kind,
        actor_object_id="tester",
    )


class _Authenticator:
    def __init__(self) -> None:
        self._context = AuthContext(
            object_id="admin", tenant_id=TENANT, roles=frozenset({"Admin", "User"})
        )

    async def authenticate(self, _request: object) -> AuthContext:
        return self._context

    async def close(self) -> None:
        return None


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["User", "Admin"],
        usage_source=UsageSourceMode.ROLLUPS,
        usage_rollup_enabled=False,
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.authenticator = _Authenticator()
        yield test_client


@pytest.fixture
def not_configured_client() -> Iterator[TestClient]:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["User", "Admin"],
        usage_source=UsageSourceMode.SIMULATED,
        usage_rollup_enabled=False,
    )
    with TestClient(create_app(settings)) as test_client:
        test_client.app.state.authenticator = _Authenticator()
        yield test_client


def test_24h_window_uses_utc_hours_and_equal_previous_period() -> None:
    local_now = datetime(2026, 3, 18, 11, 45, 59, tzinfo=timezone(timedelta(hours=-4)))

    window = resolve_window(AnalyticsFilters(range="24h"), local_now)

    assert window.granularity == "hour"
    assert window.start == datetime(2026, 3, 17, 16, tzinfo=UTC)
    assert window.end == datetime(2026, 3, 18, 16, tzinfo=UTC)
    assert window.previous_start == datetime(2026, 3, 16, 16, tzinfo=UTC)
    assert window.previous_end == window.start
    assert len(window.buckets) == 24
    assert window.bucket_index(datetime(2026, 3, 18, 15, 59, tzinfo=UTC)) == 23
    assert window.bucket_index(datetime(2026, 3, 18, 16, tzinfo=UTC)) is None


@pytest.mark.parametrize(
    ("range_", "start", "previous_start", "buckets"),
    [
        ("7d", date(2026, 3, 12), datetime(2026, 3, 5, tzinfo=UTC), 7),
        ("30d", date(2026, 2, 17), datetime(2026, 1, 18, tzinfo=UTC), 30),
        ("90d", date(2025, 12, 19), datetime(2025, 9, 20, tzinfo=UTC), 90),
    ],
)
def test_day_ranges_are_whole_utc_days_with_equal_previous_periods(
    range_: str, start: date, previous_start: datetime, buckets: int
) -> None:
    window = resolve_window(AnalyticsFilters(range=range_), NOW)

    assert window.granularity == "day"
    assert window.first_day == start
    assert window.last_day == NOW.date()
    assert window.previous_start == previous_start
    assert window.previous_end == day_start(start)
    assert len(window.buckets) == buckets


def test_12m_window_crosses_year_boundaries_as_calendar_months() -> None:
    window = resolve_window(AnalyticsFilters(range="12m"), NOW)

    assert window.granularity == "month"
    assert window.start == datetime(2025, 4, 1, tzinfo=UTC)
    assert window.end == datetime(2026, 4, 1, tzinfo=UTC)
    assert window.previous_start == datetime(2024, 4, 1, tzinfo=UTC)
    assert window.previous_end == datetime(2025, 4, 1, tzinfo=UTC)
    assert len(window.buckets) == 12


def test_custom_short_range_keeps_leap_day_and_daily_grain() -> None:
    window = resolve_window(
        AnalyticsFilters(range="custom", start=date(2024, 2, 28), end=date(2024, 3, 1)),
        datetime(2024, 3, 5, 9, tzinfo=UTC),
    )

    assert window.granularity == "day"
    assert [bucket.date() for bucket in window.buckets] == [
        date(2024, 2, 28),
        date(2024, 2, 29),
        date(2024, 3, 1),
    ]
    assert window.previous_start == datetime(2024, 2, 25, tzinfo=UTC)


def test_custom_long_range_reads_whole_calendar_months() -> None:
    window = resolve_window(
        AnalyticsFilters(range="custom", start=date(2025, 11, 15), end=date(2026, 3, 18)),
        NOW,
    )

    assert window.granularity == "month"
    assert window.start == datetime(2025, 11, 1, tzinfo=UTC)
    assert window.end == datetime(2026, 4, 1, tzinfo=UTC)
    assert window.previous_start == datetime(2025, 6, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("filters", "message", "details"),
    [
        (
            AnalyticsFilters(range="custom", start=None, end=date(2026, 3, 18)),
            "needs a start date and an end date",
            {},
        ),
        (
            AnalyticsFilters(range="custom", start=date(2026, 3, 20), end=date(2026, 3, 21)),
            "can't start in the future",
            {"start": "2026-03-20"},
        ),
        (
            AnalyticsFilters(range="custom", start=date(2026, 3, 18), end=date(2026, 3, 20)),
            "can't end in the future",
            {"end": "2026-03-20"},
        ),
        (
            AnalyticsFilters(range="custom", start=date(2026, 3, 10), end=date(2026, 3, 9)),
            "start date must be on or before its end date",
            {"start": "2026-03-10", "end": "2026-03-09"},
        ),
        (
            AnalyticsFilters(range="custom", start=date(2020, 1, 1), end=date(2026, 3, 18)),
            "can span at most 1830 days",
            {"days": 2269},
        ),
    ],
)
def test_custom_range_validation_errors_are_specific(
    filters: AnalyticsFilters, message: str, details: dict[str, object]
) -> None:
    with pytest.raises(ValidationError) as error:
        resolve_window(filters, NOW)

    assert message in error.value.message
    assert error.value.details == details


def test_custom_range_a_day_ahead_of_utc_ends_today() -> None:
    # A browser east of UTC is already on the 19th while UTC is still on the 18th.
    window = resolve_window(
        AnalyticsFilters(range="custom", start=date(2026, 3, 17), end=date(2026, 3, 19)), NOW
    )
    assert (window.first_day, window.last_day) == (date(2026, 3, 17), NOW.date())

    today_only = resolve_window(
        AnalyticsFilters(range="custom", start=date(2026, 3, 19), end=date(2026, 3, 19)), NOW
    )
    assert (today_only.first_day, today_only.last_day) == (NOW.date(), NOW.date())


def test_split_months_separates_whole_months_from_partial_edges() -> None:
    months, ranges = split_months(date(2026, 1, 15), date(2026, 5, 10))

    assert months == [date(2026, 2, 1), date(2026, 3, 1), date(2026, 4, 1)]
    assert ranges == [(date(2026, 1, 15), date(2026, 1, 31)), (date(2026, 5, 1), date(2026, 5, 10))]


@pytest.mark.parametrize(
    ("histogram", "percentile", "expected"),
    [
        ([0] * 10, 95, None),
        ([10, *([0] * 9)], 95, 95.0),
        ([0, 0, 0, 0, 20, *([0] * 5)], 95, 1950.0),
        ([0, 0, 0, 0, 0, 0, 0, 0, 0, 3], 95, 60000.0),
    ],
)
def test_latency_percentiles_interpolate_and_handle_empty_or_open_ended_buckets(
    histogram: list[int], percentile: float, expected: float | None
) -> None:
    assert latency_percentile(histogram, percentile) == expected


def test_latency_buckets_in_reliability_cover_only_admitted_calls() -> None:
    metrics = _metric(
        requests=7,
        denied=2,
        total_time_ms=4_500,
        backend_time_ms=3_000,
        latency=[0, 2, 3, *([0] * 7)],
    )
    context = _context(_scope(), coverage=Coverage(first_day=NOW.date(), through=NOW))
    summary = _summary(
        "api",
        NOW.date(),
        UsageSummaryEntry(key="chat", metrics=metrics),
    )

    report = reliability(context, api=[summary], trend_api=[summary], denials=[])

    assert report.latency.average_ms == 900.0
    assert report.latency.average_backend_ms == 600.0
    assert report.latency.buckets[1].count == 2
    assert report.latency.buckets[2].count == 3
    bucket_count = sum(bucket.count for bucket in report.latency.buckets)
    assert bucket_count == metrics.requests - metrics.denied
    assert [bucket.upper_ms for bucket in report.latency.buckets[:-1]] == list(LATENCY_BUCKETS_MS)
    assert report.latency.buckets[-1].upper_ms is None


@pytest.mark.parametrize(
    ("reason", "label"),
    [
        ("unauthenticated", "Not signed in"),
        ("token-invalid", "Invalid or expired token"),
        ("something-new", "Unknown"),
    ],
)
def test_denial_labels_name_known_reasons_and_fallback_unknown(reason: str, label: str) -> None:
    assert denial_label(reason) == label


def test_kpi_helpers_keep_none_for_impossible_rates_and_backend_latency() -> None:
    metrics = _metric(requests=0, total_time_ms=0, backend_time_ms=10, latency=[0] * 10)

    report = kpis(metrics, Activity())

    assert share(1, 0) is None
    assert average_latency(metrics) is None
    assert average_backend(metrics) is None
    assert report.success_rate is None
    assert report.p95_latency_ms is None


def test_hour_kpis_derive_status_from_hourly_counts() -> None:
    point = UsageHour(
        hour=10, requests=10, total_tokens=20, throttled=1, quota=2, denied=3, errors=1
    )

    report = hour_kpis(point, Activity(callers=1, grants=2, apis=3, unattributed=4))

    assert report.ok == 3
    assert report.success_rate == 0.3
    assert report.throttle_rate == 0.3
    assert report.denial_rate == 0.3
    assert report.active_callers == 1


def test_rankings_order_by_tokens_unless_calls_are_asked_for() -> None:
    chat = Ranked("chat", "Chat", None, _metric(requests=10, total_tokens=5_000))
    tools = Ranked("tools", "Ticket tools", None, _metric(requests=40))
    idle = Ranked("idle", "Idle", None, _metric())

    by_tokens = rank([tools, idle, chat], requests=50, tokens=5_000, limit=10)
    by_calls = rank([chat, idle, tools], requests=50, tokens=5_000, limit=10, by="requests")

    # MCP calls carry no tokens, so a tool server busier than a model ranks first only by calls.
    assert [row.key for row in by_tokens] == ["chat", "tools"]
    assert [row.key for row in by_calls] == ["tools", "chat"]
    assert (by_calls[0].request_share, by_calls[0].token_share) == (0.8, 0.0)
    assert len(rank([chat, tools], requests=50, tokens=5_000, limit=1, by="requests")) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("=SUM(A1:A2)", "'=SUM(A1:A2)"),
        ("+cmd", "'+cmd"),
        ("-cmd", "'-cmd"),
        ("@cmd", "'@cmd"),
        ("\tcmd", "'\tcmd"),
        ("\rcmd", "'\rcmd"),
        (["alpha", "beta"], "alpha; beta"),
        (True, "true"),
        (None, ""),
        (42, 42),
        (date(2026, 3, 18), "2026-03-18"),
    ],
)
def test_export_cells_are_spreadsheet_safe(value: object, expected: object) -> None:
    assert cell(value) == expected


def test_export_csv_writes_headers_and_crlf_rows() -> None:
    output = to_csv([("Name", "name"), ("Requests", "requests")], [{"name": "=x", "requests": 2}])

    assert output == "Name,Requests\r\n'=x,2\r\n"
    assert list(csv.reader(io.StringIO(output))) == [["Name", "Requests"], ["'=x", "2"]]


def test_export_filename_and_table_pick_the_view_rows() -> None:
    report = AnalyticsConsumers(
        **_base_report(),
        linked_requests=2,
        linked_tokens=3,
        unidentified_requests=0,
        people=[
            AnalyticsConsumerRow(
                key="alice",
                kind="person",
                label="Alice",
                requests=2,
                total_tokens=3,
                grants=1,
                resources=1,
            )
        ],
        applications=[],
        groups=[],
        grants=[],
        client_apps=[],
        truncated=False,
    )

    assert filename("people", date(2026, 3, 1), date(2026, 3, 31)) == (
        "mosaic-people-20260301-20260331.csv"
    )
    assert table("people", report) == [
        {
            "key": "alice",
            "label": "Alice",
            "detail": None,
            "kind": "person",
            "principal_id": None,
            "principal_kind": None,
            "requests": 2,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 3,
            "throttled": 0,
            "quota_refused": 0,
            "errors": 0,
            "last_seen": None,
            "request_share": None,
            "token_share": None,
            "cost": None,
            "grants": 1,
            "resources": 1,
            "members": None,
        }
    ]


@pytest.mark.parametrize(
    ("utilization", "refused", "expected"),
    [
        (None, 0, "unknown"),
        (0.7999, 0, "ok"),
        (0.8, 0, "near"),
        (0.9999, 0, "near"),
        (1.0, 0, "reached"),
        (0.5, 1, "reached"),
        (None, 1, "reached"),
    ],
)
def test_limit_status_thresholds_are_exact(
    utilization: float | None, refused: int, expected: str
) -> None:
    assert _status(utilization, refused) == expected


@pytest.mark.parametrize(
    ("model_format", "sku_name", "sku_capacity", "expected"),
    [
        ("OpenAI", "Standard", 10, 10_000),
        ("OpenAI", "Global Standard", 450, 450_000),
        ("OpenAI", "DataZoneStandard", 30, 30_000),
        # Provisioned capacity is in throughput units, not tokens.
        ("OpenAI", "GlobalProvisionedManaged", 50, None),
        # Other providers' models are deployed at a capacity of 1 under a regional rate limit.
        ("Microsoft", "GlobalStandard", 1, None),
        ("Mistral AI", "GlobalStandard", 1, None),
        (None, "GlobalStandard", 100, None),
        ("OpenAI", "GlobalStandard", None, None),
    ],
)
def test_only_azure_openai_capacity_counts_tokens(
    model_format: str | None, sku_name: str, sku_capacity: int | None, expected: int | None
) -> None:
    info = DeploymentInfo(
        endpoint_name="Production",
        model_name="model",
        model_format=model_format,
        sku_name=sku_name,
        sku_capacity=sku_capacity,
    )
    assert info.capacity_tokens_per_minute == expected


@pytest.mark.parametrize(
    ("bound_at", "first_day", "through", "expected_judged"),
    [
        (datetime(2026, 2, 17, tzinfo=UTC), date(2026, 2, 17), NOW, 1),
        (datetime(2026, 2, 17, 0, 0, 1, tzinfo=UTC), date(2026, 2, 17), NOW, 0),
        (datetime(2026, 2, 1, tzinfo=UTC), date(2026, 2, 18), NOW, 0),
        (datetime(2026, 2, 1, tzinfo=UTC), date(2026, 2, 17), NOW - timedelta(days=2), 0),
    ],
)
def test_hygiene_only_judges_grants_when_coverage_is_complete_fresh_and_old_enough(
    bound_at: datetime, first_day: date, through: datetime, expected_judged: int
) -> None:
    entitlement = _entitlement("grant-alice", binding=_binding("k-alice", bound_at=bound_at))
    context = _context(
        _scope(entitlements=[entitlement], states=[_state(first=first_day, through=through)]),
        coverage=Coverage(first_day=first_day, through=through),
    )

    report = hygiene_report(
        context,
        HygieneInputs(grants=[], history=[], denials=[]),
        NOW,
        stale_after=timedelta(days=1),
    )

    assert report.judged_grants == expected_judged
    assert [row.entitlement_id for row in report.unused_grants] == (
        ["grant-alice"] if expected_judged else []
    )


@pytest.mark.parametrize(
    ("runtime", "key_requests", "expected"),
    [
        (None, 0, True),
        (EntitlementRuntime(publication_id="pub", status="applied"), 0, True),
        (
            EntitlementRuntime(
                publication_id="pub",
                status="applied",
                applied_methods=ModelAccessSettings(keys_enabled=True),
            ),
            0,
            True,
        ),
        (
            EntitlementRuntime(
                publication_id="pub",
                status="applied",
                applied_methods=ModelAccessSettings(keys_enabled=False),
            ),
            0,
            False,
        ),
        (None, 1, False),
    ],
)
def test_hygiene_unused_keys_require_token_only_calls_while_keys_can_be_enabled(
    runtime: EntitlementRuntime | None, key_requests: int, expected: bool
) -> None:
    entitlement = _entitlement(
        "grant-alice",
        binding=_binding("k-alice", subscription="sub-alice"),
        runtime=runtime,
    )
    context = _context(
        _scope(entitlements=[entitlement], states=[_state(first=date(2026, 2, 17))]),
        coverage=Coverage(first_day=date(2026, 2, 17), through=NOW),
    )
    grants = [
        _summary(
            "grant",
            date(2026, 3, 18),
            UsageSummaryEntry(
                key="trace:k-alice",
                metrics=_metric(requests=3, total_tokens=9, key_requests=key_requests),
            ),
        )
    ]

    report = hygiene_report(
        context,
        HygieneInputs(grants=grants, history=[], denials=[]),
        NOW,
        stale_after=timedelta(days=1),
    )

    assert bool(report.unused_keys) is expected
    if expected:
        assert report.unused_keys[0].subscription_name == "sub-alice"
        assert report.unused_keys[0].token_requests == 3


def test_hygiene_lists_untracked_grants_by_reason() -> None:
    mosaic_group = _entitlement(
        "grant-team",
        subject_kind="group",
        subject_id="group-team",
        binding=None,
    )
    not_applied = _entitlement("grant-not-applied", binding=None)
    no_link = _entitlement(
        "grant-no-link",
        binding=EntitlementBinding(gateway_id=GATEWAY, source=BindingSource.MANUAL),
    )
    context = _context(
        _scope(
            entitlements=[mosaic_group, not_applied, no_link],
            states=[_state(first=date(2026, 2, 17))],
        ),
        coverage=Coverage(first_day=date(2026, 2, 17), through=NOW),
    )

    report = hygiene_report(
        context,
        HygieneInputs(grants=[], history=[], denials=[]),
        NOW,
        stale_after=timedelta(days=1),
    )

    assert [(row.entitlement_id, row.reason) for row in report.untracked_grants] == [
        ("grant-team", "mosaicGroup"),
        ("grant-no-link", "noLink"),
        ("grant-not-applied", "notApplied"),
    ]


def test_caller_names_keep_the_entra_kind_that_analytics_folds_into_people() -> None:
    scope = _scope()
    agent_user = Principal(
        id="principal-scheduler",
        tenant_id=TENANT,
        object_id="Scheduler-OID",
        kind=PrincipalKind.AGENT_USER,
        label="Scheduling Assistant",
    )
    scope.principals_by_object[agent_user.object_id.casefold()] = agent_user
    scope.directory["unrecorded-oid"] = DirectoryObject(
        object_id="unrecorded-oid",
        kind=PrincipalKind.AGENT_USER,
        display_name="Travel Assistant",
    )

    recorded = scope.caller("scheduler-oid")
    unrecorded = scope.caller("unrecorded-oid")
    unknown = scope.caller("stranger-oid")

    assert (recorded.label, recorded.kind, recorded.principal_kind) == (
        "Scheduling Assistant",
        "person",
        PrincipalKind.AGENT_USER,
    )
    assert recorded.principal_id == "principal-scheduler"
    assert (unrecorded.label, unrecorded.kind, unrecorded.principal_kind) == (
        "Travel Assistant",
        "person",
        PrincipalKind.AGENT_USER,
    )
    assert unknown.principal_kind is None
    assert scope.caller("alice-oid").principal_kind == PrincipalKind.USER


async def test_empty_rollups_report_zeroes_and_pending_note(client: TestClient) -> None:
    state = client.app.state
    await state.gateway_repository.save_gateway(
        Gateway(
            id=GATEWAY,
            tenant_id=TENANT,
            name="Production gateway",
            azure_resource_id="/subscriptions/000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/gw",
            subscription_id="000",
            resource_group="rg",
            service_name="gw",
        ),
        _audit("gateway"),
    )
    await state.gateway_repository.save_model_api(
        ModelApi(
            id="model-api-chat",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            api_name="chat",
            display_name="Chat",
            path="chat",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("model-api"),
    )

    response = client.get("/api/v1/analytics/overview")

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["dataSource"] == "logAnalytics"
    assert report["kpis"]["requests"] == 0
    assert report["kpis"]["totalTokens"] == 0
    assert report["previous"] is None
    assert report["freshness"]["status"] == "pending"
    assert report["notes"] == [
        "MOSAIC hasn't rolled up any gateway telemetry yet. Figures appear after the first rollup, "
        "which runs every 15 minutes."
    ]


def test_not_configured_service_reports_not_configured_source(
    not_configured_client: TestClient,
) -> None:
    response = not_configured_client.get("/api/v1/analytics/overview")

    assert response.status_code == 200, response.text
    report = response.json()
    assert report["dataSource"] == "notConfigured"
    assert report["kpis"]["requests"] == 0
    assert report["notes"][0].startswith("This deployment doesn't read gateway telemetry")


def test_export_route_adds_utf8_bom_and_rejects_unknown_views(client: TestClient) -> None:
    ok = client.get("/api/v1/analytics/export", params={"view": "trend"})
    bad = client.get("/api/v1/analytics/export", params={"view": "unknown"})

    assert ok.status_code == 200, ok.text
    assert ok.content.startswith("\ufeff".encode())
    assert ok.text.startswith("\ufeff" + ",".join(header for header, _ in COLUMNS["trend"]))
    assert bad.status_code == 422


def _base_report() -> dict[str, object]:
    window = resolve_window(AnalyticsFilters(range="30d"), NOW).model()
    return {
        "data_source": "logAnalytics",
        "generated_at": NOW,
        "window": window,
        "freshness": UsageFreshness(
            status="current",
            updated_at=NOW,
            data_from=window.breakdown_start.isoformat(),
            gateways=1,
            interval_minutes=15,
        ),
        "notes": [],
    }


def _context(
    scope: Scope,
    *,
    coverage: Coverage | None = None,
    limit: int = 500,
) -> Context:
    window = resolve_window(AnalyticsFilters(range="30d"), NOW)
    return Context(scope=scope, window=window, coverage=coverage, base=_base_report(), limit=limit)


def _scope(
    *,
    entitlements: list[Entitlement] | None = None,
    states: list[UsageRollupState] | None = None,
) -> Scope:
    gateway = Gateway(
        id=GATEWAY,
        tenant_id=TENANT,
        name="Production gateway",
        azure_resource_id="/subscriptions/000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/gw",
        subscription_id="000",
        resource_group="rg",
        service_name="gw",
    )
    principal = Principal(
        id="principal-alice",
        tenant_id=TENANT,
        object_id="alice-oid",
        kind=PrincipalKind.USER,
        label="Alice",
        detail="alice@example.com",
    )
    grant_infos: dict[str, GrantInfo] = {}
    links_by_entitlement: dict[str, set[str]] = {}
    for entitlement in entitlements or []:
        binding = entitlement.binding
        links: set[str] = set()
        if binding and binding.attribution_key:
            links.add(f"trace:{binding.attribution_key.casefold()}")
        if binding and binding.apim_subscription_name:
            links.add(f"subscription:{GATEWAY}/{binding.apim_subscription_name.casefold()}")
        for link in links:
            grant_infos[link] = GrantInfo(
                key=link,
                entitlement_id=entitlement.id,
                subject_kind=entitlement.subject.kind,
                subject_id=entitlement.subject.id,
                subject_object_id=principal.object_id,
                subject_name=principal.label,
                resource_kind=entitlement.resource.kind,
                resource_id=entitlement.resource.id,
                resource_name="Chat",
                gateway_id=GATEWAY,
                publication_id=None,
                per_member=False,
            )
        if links:
            links_by_entitlement[entitlement.id] = links
    return Scope(
        tenant_id=TENANT,
        filters=AnalyticsFilters(),
        all_gateways={GATEWAY: gateway},
        gateways={GATEWAY: gateway},
        gateway_ids=None,
        states={state.gateway_id: state for state in states or []},
        governed={GATEWAY: []},
        apis={},
        allowed_apis=None,
        resource_ids=None,
        environments={},
        grants=grant_infos,
        links_by_entitlement=links_by_entitlement,
        entitlements={entitlement.id: entitlement for entitlement in entitlements or []},
        principals_by_object={principal.object_id: principal},
        principals_by_id={principal.id: principal},
        principals_by_app={},
        groups={},
        subject_names={principal.object_id: principal.label or "Alice"},
    )


def _binding(
    key: str,
    *,
    subscription: str | None = None,
    bound_at: datetime | None = datetime(2026, 2, 1, tzinfo=UTC),
) -> EntitlementBinding:
    return EntitlementBinding(
        gateway_id=GATEWAY,
        attribution_key=key,
        apim_subscription_name=subscription,
        source=BindingSource.MANUAL,
        bound_at=bound_at,
    )


def _entitlement(
    grant_id: str,
    *,
    subject_kind: str = "user",
    subject_id: str = "principal-alice",
    binding: EntitlementBinding | None,
    runtime: EntitlementRuntime | None = None,
) -> Entitlement:
    return Entitlement(
        id=grant_id,
        tenant_id=TENANT,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        subject=EntitlementSubject(kind=subject_kind, id=subject_id),
        resource=EntitlementResource(kind="modelApi", id="model-api-chat"),
        binding=binding,
        runtime=runtime,
    )


def test_actor_fixture_is_available_for_direct_service_calls() -> None:
    actor = Actor(object_id="admin", tenant_id=TENANT)

    assert actor.tenant_id == TENANT
