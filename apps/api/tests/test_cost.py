"""Cost from end to end: gateway logs, rollups, the price list, analytics, and a person's usage.

The estate is small and its numbers round: a pay-as-you-go deployment at $2.50 and $10 per
million tokens, a provisioned one of 10 PTUs at $1 a PTU an hour, which is $240 a day, and a
deployment of a model nothing prices.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loganalytics_double import FakeLogs, GatewayCall
from mosaic_api.auth import AuthContext
from mosaic_api.config import (
    AuthMode,
    Environment,
    RepositoryBackend,
    Settings,
    UsageSourceMode,
)
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    McpServer,
    ModelApi,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    Principal,
    PrincipalKind,
    Publication,
    new_id,
)
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.services.analytics import AnalyticsService
from mosaic_api.services.pricing import PricingService
from mosaic_api.services.usage import RollupUsageSource, UsageService
from mosaic_api.services.usage_rollup import UsageRollupService

TENANT = "tenant-test"
NOW = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)
TODAY = NOW.date()
GATEWAY = "gateway-prod"
RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
    "/providers/Microsoft.ApiManagement/service/gateway-prod"
)
ENDPOINT = "endpoint-aoai"
ALICE = "alice-oid"
BOB = "bob-oid"
CAROL = "carol-oid"
BOT = "bot-oid"
ANALYSTS = "analysts-oid"
CHAT = f"{ENDPOINT}/chat"
RESERVED = f"{ENDPOINT}/reserved"
MYSTERY = f"{ENDPOINT}/mystery"

# March 2026 has 31 days, and the 18th is the 18th of them.
DAILY = 10 * 1.0 * 24
MARCH_SO_FAR = DAILY * 18
# Alice's ten days of chat at $3.50 a day, and an unknown key's 100,000 prompt tokens.
CHAT_COST = 10 * 3.5 + 0.25
# The 30 days end on 18 March, so they start on 17 February: twelve February days with no calls.
FEBRUARY_IDLE = DAILY * 12


def _audit(kind: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type=kind,
        resource_id=kind,
        actor_object_id="tester",
    )


class _Authenticator:
    def __init__(self, object_id: str, roles: list[str], groups: frozenset[str]) -> None:
        self._context = AuthContext(
            object_id=object_id, tenant_id=TENANT, roles=frozenset(roles), group_ids=groups
        )

    async def authenticate(self, _request: object) -> AuthContext:
        return self._context

    async def close(self) -> None:
        return None


class Harness:
    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.state = client.app.state
        self.now = NOW
        self.logs = FakeLogs(clock=lambda: self.now)
        self.sign_in(ALICE, ["User", "Admin"])
        self.job = UsageRollupService(
            self.state.usage_rollup_repository,
            gateway_repository=self.state.gateway_repository,
            entitlement_repository=self.state.entitlement_repository,
            directory_repository=self.state.repository,
            logs=self.logs,
            telemetry=None,
            tenant_id=TENANT,
            backfill_max_days=40,
            clock=lambda: self.now,
        )
        self.pricing = PricingService(
            self.state.pricing_repository,
            endpoint_repository=self.state.model_endpoint_repository,
            gateway_repository=self.state.gateway_repository,
            rollup_repository=self.state.usage_rollup_repository,
            clock=lambda: self.now,
        )
        self.state.pricing_service = self.pricing
        self.state.usage_rollup_service = self.job
        self.state.analytics_service = AnalyticsService(
            self.state.usage_rollup_repository,
            gateway_repository=self.state.gateway_repository,
            entitlement_repository=self.state.entitlement_repository,
            directory_repository=self.state.repository,
            environment_repository=self.state.environment_repository,
            endpoint_repository=self.state.model_endpoint_repository,
            rollups=self.job,
            pricing=self.pricing,
            configured=True,
            clock=lambda: self.now,
        )
        self.state.usage_service = UsageService(
            self.state.portal_service,
            source=RollupUsageSource(self.state.usage_rollup_repository, clock=lambda: self.now),
            gateway_repository=self.state.gateway_repository,
            endpoint_repository=self.state.model_endpoint_repository,
            environment_repository=self.state.environment_repository,
            pricing=self.pricing,
            clock=lambda: self.now,
        )

    def sign_in(
        self, object_id: str, roles: list[str], groups: frozenset[str] = frozenset({ANALYSTS})
    ) -> None:
        self.state.authenticator = _Authenticator(object_id, roles, groups)

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(path, params=params)
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["User", "Admin"],
        usage_source=UsageSourceMode.ROLLUPS,
        usage_rollup_enabled=False,
    )


@pytest.fixture
def harness(settings: Settings) -> Iterator[Harness]:
    with TestClient(create_app(settings)) as client:
        yield Harness(client)


def _deployment(name: str, model: str, version: str, sku: str, capacity: int) -> Any:
    return ObservedModelDeployment(
        id=f"obs-{name}",
        tenant_id=TENANT,
        endpoint_id=ENDPOINT,
        snapshot_id="snapshot",
        deployment_name=name,
        model_name=model,
        model_version=version,
        model_format="OpenAI",
        sku_name=sku,
        sku_capacity=capacity,
        deployed_at=datetime(2026, 2, 1, tzinfo=UTC),
    )


async def _principal(harness: Harness, object_id: str, kind: PrincipalKind, label: str) -> str:
    principal = await harness.state.repository.create_principal(
        Principal(
            id=f"principal-{object_id}",
            tenant_id=TENANT,
            object_id=object_id,
            kind=kind,
            label=label,
        ),
        _audit("principal"),
    )
    return str(principal.id)


async def _grant(
    harness: Harness,
    grant_id: str,
    subject: EntitlementSubject,
    resource_id: str,
    binding: EntitlementBinding,
    *,
    kind: str = "modelApi",
) -> None:
    await harness.state.entitlement_repository.save_entitlement(
        Entitlement(
            id=grant_id,
            tenant_id=TENANT,
            created_at=NOW - timedelta(days=90),
            subject=subject,
            resource=EntitlementResource.model_validate({"kind": kind, "id": resource_id}),
            binding=binding,
        ),
        _audit("entitlement"),
    )


def _traced(key: str, *, per_member: bool = False) -> EntitlementBinding:
    return EntitlementBinding(
        gateway_id=GATEWAY,
        attribution_key=key,
        attribution_per_member=per_member,
        source=BindingSource.ORCHESTRATED,
    )


def _call(day: int, api: str, prompt: int, completion: int, **trace: Any) -> GatewayCall:
    return GatewayCall(
        time=datetime(2026, 3, day, 10, tzinfo=UTC),
        api=api,
        prompt_tokens=prompt,
        completion_tokens=completion,
        deployment=api,
        model="gpt-4o",
        **trace,
    )


async def seed(harness: Harness) -> None:
    state = harness.state
    await state.gateway_repository.save_gateway(
        Gateway(
            id=GATEWAY,
            tenant_id=TENANT,
            name="Production gateway",
            azure_resource_id=RESOURCE_ID,
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="gateway-prod",
            environment="production",
        ),
        _audit("gateway"),
    )
    endpoint = ModelEndpoint(
        id=ENDPOINT,
        tenant_id=TENANT,
        name="Contoso Azure OpenAI",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://contoso.openai.azure.com",
        environment="production",
        capabilities=ModelEndpointCapabilities(location="eastus2"),
    )
    await state.model_endpoint_repository.save_endpoint(endpoint, _audit("endpoint"))
    await state.model_endpoint_repository.replace_observed_for_endpoint(
        TENANT,
        ENDPOINT,
        [
            _deployment("chat", "gpt-4o", "2024-11-20", "GlobalStandard", 450),
            _deployment("reserved", "gpt-4o", "2024-11-20", "GlobalProvisionedManaged", 10),
            _deployment("mystery", "contoso-llm", "1", "GlobalStandard", 1),
        ],
        "snapshot",
    )
    for name in ("chat", "reserved", "mystery"):
        publication = Publication(
            id=f"publication-{name}",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            model_endpoint_id=ENDPOINT,
            deployment_name=name,
            provider=ModelProvider.AZURE_OPENAI,
            display_name=name.title(),
            api_name=name,
            api_path=name,
            backend_name=name,
            fragment_name=name,
            product_name=name,
            subscription_name=name,
            shape_version="v1",
            api_shape=ApiShape.AZURE_OPENAI,
        )
        await state.gateway_repository.save_publication(publication, _audit("publication"))
        await state.gateway_repository.save_model_api(
            ModelApi(
                id=f"model-api-{name}",
                tenant_id=TENANT,
                gateway_id=GATEWAY,
                api_name=name,
                display_name=name.title(),
                path=name,
                publication_id=publication.id,
            ),
            _audit("model-api"),
        )
    await state.gateway_repository.save_mcp_server(
        McpServer(
            id="mcp-tickets",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            api_name="tickets",
            display_name="Ticket tools",
            path="tickets",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("mcp"),
    )

    alice = await _principal(harness, ALICE, PrincipalKind.USER, "Alice")
    bob = await _principal(harness, BOB, PrincipalKind.USER, "Bob")
    await _principal(harness, CAROL, PrincipalKind.USER, "Carol")
    bot = await _principal(harness, BOT, PrincipalKind.SERVICE_PRINCIPAL, "Support bot")
    analysts = await _principal(harness, ANALYSTS, PrincipalKind.SECURITY_GROUP, "Analysts")

    await _grant(
        harness, "grant-alice", EntitlementSubject(kind="user", id=alice), "model-api-chat",
        _traced("k-alice"),
    )
    await _grant(
        harness, "grant-bob", EntitlementSubject(kind="user", id=bob), "model-api-reserved",
        _traced("k-bob"),
    )
    await _grant(
        harness,
        "grant-analysts",
        EntitlementSubject(kind="securityGroup", id=analysts),
        "model-api-reserved",
        _traced("k-analysts", per_member=True),
    )
    await _grant(
        harness,
        "grant-bot",
        EntitlementSubject(kind="application", id=bot),
        "model-api-mystery",
        EntitlementBinding(gateway_id=GATEWAY, apim_subscription_name="sub-bot"),
    )
    await _grant(
        harness,
        "grant-alice-tickets",
        EntitlementSubject(kind="user", id=alice),
        "mcp-tickets",
        _traced("k-alice-tickets"),
        kind="mcpServer",
    )

    calls: list[GatewayCall] = []
    for day in range(9, 19):
        calls.append(
            _call(day, "chat", 1_000_000, 100_000, grant="k-alice", member=ALICE)
        )
    for day in (16, 17, 18):
        calls.append(_call(day, "reserved", 80_000, 20_000, grant="k-bob", member=BOB))
    calls.append(_call(17, "reserved", 80_000, 20_000, grant="k-analysts", member=CAROL))
    calls.extend(
        _call(18, "mystery", 1_000, 1_000, traced=False, subscription="sub-bot") for _ in range(2)
    )
    calls.append(_call(18, "chat", 100_000, 0, traced=False, subscription="unknown-sub"))
    calls.extend(
        GatewayCall(
            time=datetime(2026, 3, 18, 9, tzinfo=UTC),
            api="tickets",
            grant="k-alice-tickets",
            member=ALICE,
        )
        for _ in range(2)
    )
    harness.logs.calls = calls


async def _roll_up(harness: Harness) -> None:
    for _ in range(10):
        states = await harness.job.run_cycle()
        if all(state.backfill_status != "running" for state in states):
            return
    raise AssertionError("The backfill never finished")


async def rolled_up(harness: Harness) -> Harness:
    await seed(harness)
    await _roll_up(harness)
    return harness


async def _deploy_reserved_on(harness: Harness, day: datetime) -> None:
    """Redeploy the provisioned deployment as if Azure had created it on ``day``."""

    await harness.state.model_endpoint_repository.replace_observed_for_endpoint(
        TENANT,
        ENDPOINT,
        [
            _deployment("chat", "gpt-4o", "2024-11-20", "GlobalStandard", 450),
            _deployment(
                "reserved", "gpt-4o", "2024-11-20", "GlobalProvisionedManaged", 10
            ).model_copy(update={"deployed_at": day}),
            _deployment("mystery", "contoso-llm", "1", "GlobalStandard", 1),
        ],
        "snapshot",
    )


async def test_overview_prices_the_window_and_projects_the_month(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/overview", range="30d")

    total = CHAT_COST + MARCH_SO_FAR + FEBRUARY_IDLE
    assert report["kpis"]["cost"] == pytest.approx(total)
    cost = report["cost"]
    assert cost["total"] == pytest.approx(total)
    assert cost["reserved"] == pytest.approx(MARCH_SO_FAR + FEBRUARY_IDLE)
    # The mystery model has no price, so its tokens are left out and counted, never zero.
    assert (cost["unpricedTokens"], cost["unpricedRequests"], cost["unpricedItems"]) == (
        4_000,
        2,
        1,
    )
    [left_out] = cost["unpriced"]
    assert (left_out["label"], left_out["reason"]) == ("mystery", "noPrice")
    assert any("cached prompt tokens" in note for note in cost["notes"])

    spend = report["spend"]
    month_to_date = CHAT_COST + MARCH_SO_FAR
    # The figures run to half past three on the 18th: 17 days and 15.5 hours of March.
    elapsed = 17 + 15.5 / 24
    assert (spend["monthStart"], spend["daysInMonth"]) == ("2026-03-01", 31)
    assert spend["daysElapsed"] == pytest.approx(round(elapsed, 2))
    assert spend["monthToDate"] == pytest.approx(month_to_date)
    assert spend["reserved"] == pytest.approx(MARCH_SO_FAR)
    # Pay-as-you-go spend goes on at its pace so far; reserved capacity costs a known amount.
    assert spend["forecast"] == pytest.approx(
        CHAT_COST * 31 / elapsed + MARCH_SO_FAR * 31 / 18, abs=1e-3
    )
    assert spend["projected"] is True

    # Each day's trend point carries that day's cost, including reserved capacity nobody called.
    by_day = {point["start"][:10]: point["cost"] for point in report["trend"]}
    assert by_day["2026-02-20"] == pytest.approx(DAILY)
    assert sum(value or 0 for value in by_day.values()) == pytest.approx(total)

    models = {row["label"]: row["cost"] for row in report["topModels"]}
    assert models["gpt-4o"] == pytest.approx(CHAT_COST + MARCH_SO_FAR)
    callers = {row["label"]: row["cost"] for row in report["topCallers"]}
    assert callers["Alice"] == pytest.approx(35.0)
    assert callers["Bob"] == pytest.approx(MARCH_SO_FAR * 0.75)
    assert callers["Carol"] == pytest.approx(MARCH_SO_FAR * 0.25)
    apis = {row["label"]: row["cost"] for row in report["topApis"]}
    assert apis["Mystery"] is None
    assert apis["Ticket tools"] is None


async def test_the_forecast_waits_for_a_day_of_figures(harness: Harness) -> None:
    await rolled_up(harness)
    # On 1 April MOSAIC's figures still end on 18 March, so April has none yet.
    harness.now = datetime(2026, 4, 1, 5, tzinfo=UTC)

    spend = harness.get("/api/v1/analytics/overview", range="7d")["spend"]

    assert (spend["monthStart"], spend["daysInMonth"], spend["daysElapsed"]) == (
        "2026-04-01",
        30,
        0.0,
    )
    # April's first day of reserved capacity has no calls to share it among, but it's still spend.
    assert spend["monthToDate"] == pytest.approx(DAILY)
    assert spend["forecast"] is None


async def test_the_last_day_carries_no_cost(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/overview", range="24h")

    assert report["kpis"]["cost"] is None
    assert report["cost"]["total"] is None
    assert any("by the hour" in note for note in report["cost"]["notes"])
    # The month's spend doesn't depend on the range.
    assert report["spend"]["monthToDate"] == pytest.approx(CHAT_COST + MARCH_SO_FAR)


async def test_consumers_carry_their_share_of_reserved_capacity(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/consumers", range="30d")

    people = {row["label"]: row["cost"] for row in report["people"]}
    assert people == pytest.approx(
        {"Alice": 35.0, "Bob": MARCH_SO_FAR * 0.75, "Carol": MARCH_SO_FAR * 0.25}
    )
    [bot] = report["applications"]
    assert bot["cost"] is None
    [group] = report["groups"]
    assert group["cost"] == pytest.approx(MARCH_SO_FAR * 0.25)
    grants = {row["entitlementId"]: row["cost"] for row in report["grants"]}
    assert grants["grant-bob"] == pytest.approx(MARCH_SO_FAR * 0.75)
    assert grants["grant-alice-tickets"] is None
    # The linked calls cost what their grants did; the bot's are left out.
    assert report["cost"]["total"] == pytest.approx(35.0 + MARCH_SO_FAR)
    assert report["cost"]["unpricedTokens"] == 4_000


async def test_filtering_by_gateway_never_inflates_a_share(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/consumers", range="30d", gatewayId=GATEWAY)

    people = {row["label"]: row["cost"] for row in report["people"]}
    assert people["Bob"] == pytest.approx(MARCH_SO_FAR * 0.75)


async def test_models_and_deployments_carry_cost(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/models", range="30d")

    deployments = {row["deploymentName"]: row["cost"] for row in report["deployments"]}
    assert deployments["chat"] == pytest.approx(CHAT_COST)
    assert deployments["reserved"] == pytest.approx(MARCH_SO_FAR)
    assert deployments["mystery"] is None
    apis = {row["apiName"]: row["cost"] for row in report["apis"]}
    assert apis["chat"] == pytest.approx(CHAT_COST)
    assert apis["tickets"] is None
    [gateway] = report["gateways"]
    assert gateway["cost"] == pytest.approx(CHAT_COST + MARCH_SO_FAR)
    assert report["cost"]["total"] == pytest.approx(CHAT_COST + MARCH_SO_FAR + FEBRUARY_IDLE)


async def test_the_cost_report_explains_each_deployment(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d")

    rows = {row["deploymentName"]: row for row in report["deployments"]}
    chat = rows["chat"]
    assert (chat["pricing"], chat["priceOrigin"], chat["cloud"]) == ("tokens", "seed", "commercial")
    assert (chat["inputPerMillion"], chat["outputPerMillion"]) == (2.5, 10.0)
    reserved = rows["reserved"]
    assert (reserved["pricing"], reserved["capacity"], reserved["ptuHourly"]) == (
        "provisioned",
        10,
        1.0,
    )
    assert reserved["monthCost"] == pytest.approx(DAILY * 31)
    assert reserved["idleCost"] == pytest.approx(FEBRUARY_IDLE)
    assert reserved["cost"] == pytest.approx(MARCH_SO_FAR + FEBRUARY_IDLE)
    # 400,000 prompt-equivalent and 80,000 output tokens weighed four to one, against ten PTUs of
    # gpt-4o at 2,500 tokens a minute over the 29 days and 15.5 hours of the window so far.
    minutes = 29 * 24 * 60 + 15.5 * 60
    expected = (320_000 + 4 * 80_000) / (10 * 2_500 * minutes)
    assert reserved["utilization"] == pytest.approx(expected, abs=1e-4)
    mystery = rows["mystery"]
    assert (mystery["pricing"], mystery["unpricedReason"], mystery["cost"]) == (
        "unpriced",
        "noPrice",
        None,
    )
    assert report["spend"]["monthToDate"] == pytest.approx(CHAT_COST + MARCH_SO_FAR)
    consumers = {row["label"]: row["cost"] for row in report["consumers"]}
    assert consumers["Bob"] == pytest.approx(MARCH_SO_FAR * 0.75)
    # Callers only: the Analysts group's calls are Carol's, so they're counted once.
    assert consumers["Carol"] == pytest.approx(MARCH_SO_FAR * 0.25)
    assert "Analysts" not in consumers
    assert sum(row["costShare"] or 0 for row in report["consumers"]) <= 1 + 1e-6
    assert report["models"][0]["label"] == "gpt-4o"
    assert report["priced"] is True


async def test_a_deployment_created_mid_month_costs_the_same_by_day_or_by_month(
    harness: Harness,
) -> None:
    await seed(harness)
    await _deploy_reserved_on(harness, datetime(2026, 3, 10, tzinfo=UTC))
    await _roll_up(harness)
    # Reserved from 10 March: nine days so far, and 22 in the whole month.
    so_far = DAILY * 9

    for window in ("30d", "12m"):
        report = harness.get("/api/v1/analytics/consumers", range=window)
        people = {row["label"]: row["cost"] for row in report["people"]}
        assert people["Bob"] == pytest.approx(so_far * 0.75), window
        assert people["Carol"] == pytest.approx(so_far * 0.25), window
        # Calls after the deployment existed are never left out as before it.
        assert report["cost"]["unpricedTokens"] == 4_000, window
        models = harness.get("/api/v1/analytics/models", range=window)
        deployments = {row["deploymentName"]: row["cost"] for row in models["deployments"]}
        assert deployments["reserved"] == pytest.approx(so_far), window

    spend = harness.get("/api/v1/analytics/overview", range="30d")["spend"]
    elapsed = 17 + 15.5 / 24
    # The reserved part is the month it's billed for, not this month's pace stretched to 31 days.
    assert spend["forecast"] == pytest.approx(CHAT_COST * 31 / elapsed + DAILY * 22, abs=1e-3)


async def test_a_month_total_that_lags_its_days_never_overcharges_a_share(
    harness: Harness,
) -> None:
    await rolled_up(harness)
    # A cycle that failed after writing its days leaves the month's total behind them.
    summaries = harness.state.usage_rollup_repository._summaries
    for item_key, summary in list(summaries.items()):
        if summary.period == "month" and summary.dimension == "deployment":
            del summaries[item_key]

    report = harness.get("/api/v1/analytics/consumers", range="30d")

    people = {row["label"]: row["cost"] for row in report["people"]}
    assert people["Bob"] == pytest.approx(MARCH_SO_FAR * 0.75)
    assert people["Carol"] == pytest.approx(MARCH_SO_FAR * 0.25)
    harness.sign_in(CAROL, ["User"])
    carol = harness.get("/api/v1/me/usage", period="30d")
    [row] = carol["byResource"]
    assert row["estimatedCost"] == pytest.approx(MARCH_SO_FAR * 0.25)


async def test_an_older_month_total_that_lags_its_days_never_overcharges_a_share(
    harness: Harness,
) -> None:
    await rolled_up(harness)
    # A backfill cycle that failed after writing 16 March left March's total without that day.
    summaries = harness.state.usage_rollup_repository._summaries
    for summary in summaries.values():
        if (summary.period, summary.dimension, summary.period_start) == (
            "month",
            "deployment",
            "2026-03-01",
        ):
            for entry in summary.entries:
                if entry.key.casefold() == RESERVED.casefold():
                    entry.metrics.total_tokens -= 100_000
    harness.now = datetime(2026, 5, 18, 15, 30, tzinfo=UTC)
    march = DAILY * 31

    report = harness.get(
        "/api/v1/analytics/consumers", range="custom", start="2026-03-01", end="2026-03-31"
    )

    people = {row["label"]: row["cost"] for row in report["people"]}
    assert people["Bob"] == pytest.approx(march * 0.75)
    assert people["Carol"] == pytest.approx(march * 0.25)
    harness.sign_in(BOB, ["User"], frozenset())
    bob = harness.get("/api/v1/me/usage", period="90d")
    [row] = bob["byResource"]
    assert row["estimatedCost"] == pytest.approx(march * 0.75)


async def test_unattributed_calls_carry_cost(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/unattributed", range="30d")

    rows = {row["subscription"]: row["cost"] for row in report["rows"]}
    assert rows["unknown-sub"] == pytest.approx(0.25)
    assert report["cost"]["total"] == pytest.approx(0.25)


async def test_chargeback_export_charges_each_grant_subject_by_month_and_model(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        'attachment; filename="mosaic-chargeback-20260217-20260318.csv"'
    )
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    charged = {
        (row["Month"], row["Charged to"], row["Model"]): (
            row["Kind"],
            row["Cost (USD)"],
            row["Priced"],
        )
        for row in rows
    }
    assert charged[("2026-03", "Alice", "gpt-4o")] == ("person", "35.0", "yes")
    assert float(charged[("2026-03", "Bob", "gpt-4o")][1]) == pytest.approx(MARCH_SO_FAR * 0.75)
    analysts = charged[("2026-03", "Analysts", "gpt-4o")]
    assert analysts[0] == "group"
    assert float(analysts[1]) == pytest.approx(MARCH_SO_FAR * 0.25)
    assert charged[("2026-03", "Support bot", "contoso-llm")] == ("application", "", "no")
    assert charged[("2026-03", "Unattributed calls", "gpt-4o")][1] == "0.25"
    idle = charged[("2026-02", "Reserved capacity with no calls", "gpt-4o")]
    assert float(idle[1]) == pytest.approx(FEBRUARY_IDLE)
    february = next(row for row in rows if row["Month"] == "2026-02")
    assert (february["From"], february["To"]) == ("2026-02-17", "2026-02-28")
    # The whole file reconciles with the window's total.
    total = sum(float(row["Cost (USD)"] or 0) for row in rows)
    assert total == pytest.approx(CHAT_COST + MARCH_SO_FAR + FEBRUARY_IDLE, abs=1e-3)


async def test_cost_exports_carry_a_cost_column(harness: Harness) -> None:
    await rolled_up(harness)

    people = harness.client.get(
        "/api/v1/analytics/export", params={"view": "people", "range": "30d"}
    )
    assert "Cost (USD)" in people.text.splitlines()[0]
    deployments = harness.client.get(
        "/api/v1/analytics/export", params={"view": "costDeployments", "range": "30d"}
    )
    assert deployments.status_code == 200
    header = deployments.text.lstrip("\ufeff").splitlines()[0]
    assert header.startswith("Deployment,Endpoint,Model")


async def test_a_person_sees_only_their_own_cost(harness: Harness) -> None:
    await rolled_up(harness)

    alice = harness.get("/api/v1/me/usage", period="30d")
    rows = {row["entitlementId"]: row for row in alice["byResource"]}
    assert rows["grant-alice"]["estimatedCost"] == pytest.approx(35.0)
    # Alice is in Analysts, but made none of its calls, so she shares none of its cost.
    assert rows["grant-analysts"]["estimatedCost"] == 0.0
    assert rows["grant-alice-tickets"]["estimatedCost"] is None
    assert "billed by their own service" in rows["grant-alice-tickets"]["costNote"]
    assert alice["totals"]["estimatedCost"] == pytest.approx(35.0)
    assert alice["totals"]["costExcludedResources"] == 1
    assert any("list prices" in note for note in alice["notes"])

    harness.sign_in(CAROL, ["User"])
    carol = harness.get("/api/v1/me/usage", period="30d")
    rows = {row["entitlementId"]: row for row in carol["byResource"]}
    assert set(rows) == {"grant-analysts"}
    assert rows["grant-analysts"]["estimatedCost"] == pytest.approx(MARCH_SO_FAR * 0.25)
    day = next(point for point in carol["timeline"] if point["date"] == "2026-03-17")
    assert day["estimatedCost"] == pytest.approx(MARCH_SO_FAR * 0.25)
    assert any("provisioned deployment" in note for note in carol["notes"])


async def test_an_unpriced_grant_says_why(harness: Harness) -> None:
    await rolled_up(harness)
    bot = await harness.state.repository.find_principal_by_object_id(TENANT, BOT)
    assert bot is not None
    harness.sign_in(BOT, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["byResource"]
    assert row["estimatedCost"] is None
    assert "No price for contoso-llm" in row["costNote"]
    assert report["totals"]["estimatedCost"] is None
    assert report["totals"]["costExcludedResources"] == 1


async def test_usage_nothing_prices_is_never_shown_as_free(harness: Harness) -> None:
    await rolled_up(harness)
    # From 14 March, a price with no output rate, which can't price calls that return tokens.
    response = harness.client.post(
        "/api/v1/pricing/prices",
        json={
            "cloud": "commercial",
            "publisher": "OpenAI",
            "model": "gpt-4o",
            "version": "2024-11-20",
            "deploymentType": "GlobalStandard",
            "inputPerMillion": 2.0,
            "effectiveFrom": "2026-03-14",
            "sourceUrl": "https://contoso.example/agreement",
            "note": "Input rate only",
        },
    )
    assert response.status_code == 201, response.text

    alice = harness.get("/api/v1/me/usage", period="30d")

    chat = next(row for row in alice["byResource"] if row["entitlementId"] == "grant-alice")
    # Five days the seed prices, and five nothing can, which are left out and said so.
    assert chat["estimatedCost"] == pytest.approx(5 * 3.5)
    assert chat["costNote"].startswith("Part of this usage has no price.")
    assert "no output price" in chat["costNote"]
    days = {point["date"]: point["estimatedCost"] for point in alice["timeline"]
            if point["entitlementId"] == "grant-alice"}
    assert days["2026-03-13"] == pytest.approx(3.5)
    assert days["2026-03-14"] is None
    assert alice["totals"]["costExcludedResources"] == 2

    # With the same price from 9 March too, nothing prices any of her calls, so there's no cost.
    earlier = harness.client.post(
        "/api/v1/pricing/prices",
        json={
            "cloud": "commercial",
            "publisher": "OpenAI",
            "model": "gpt-4o",
            "version": "2024-11-20",
            "deploymentType": "GlobalStandard",
            "inputPerMillion": 2.0,
            "effectiveFrom": "2026-03-09",
            "sourceUrl": "https://contoso.example/agreement",
            "note": "Input rate only, from earlier",
        },
    )
    assert earlier.status_code == 201, earlier.text
    alice = harness.get("/api/v1/me/usage", period="30d")
    chat = next(row for row in alice["byResource"] if row["entitlementId"] == "grant-alice")
    assert chat["estimatedCost"] is None
    assert chat["costNote"] == "The price lists no output price for this model."
    # Her uncalled Analysts grant is priced at nothing; the total says what it leaves out.
    assert alice["totals"]["estimatedCost"] == 0.0
    assert alice["totals"]["costExcludedResources"] == 2


async def test_an_administrator_price_changes_cost_from_its_date(harness: Harness) -> None:
    await rolled_up(harness)

    response = harness.client.post(
        "/api/v1/pricing/prices",
        json={
            "cloud": "commercial",
            "publisher": "OpenAI",
            "model": "gpt-4o",
            "version": "2024-11-20",
            "deploymentType": "GlobalStandard",
            "inputPerMillion": 1.25,
            "outputPerMillion": 5.0,
            "effectiveFrom": "2026-03-14",
            "sourceUrl": "https://contoso.example/agreement",
            "note": "Negotiated rate from 14 March",
        },
    )
    assert response.status_code == 201, response.text

    report = harness.get("/api/v1/analytics/consumers", range="30d")

    people = {row["label"]: row["cost"] for row in report["people"]}
    # Five days at $3.50 before the change, then five at half that.
    assert people["Alice"] == pytest.approx(5 * 3.5 + 5 * 1.75)
