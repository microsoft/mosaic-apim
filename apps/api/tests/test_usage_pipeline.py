"""The usage pipeline end to end: gateway logs, rollups, a person's usage, and analytics.

A gateway's calls are described as raw log rows, the rollup job reads them through a Log Analytics
double that applies what MOSAIC's KQL does, and every figure is then read back through the API.
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
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    Group,
    GroupMembership,
    McpServer,
    ModelApi,
    ModelEndpoint,
    ModelProvider,
    Principal,
    PrincipalKind,
    Publication,
    TokenEnforcement,
    new_id,
)
from mosaic_api.integrations.loganalytics import LogQueryAccessError
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.services.analytics import AnalyticsService
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
# The local authenticator signs every request in as this object.
ALICE = "local-admin"
CAROL = "carol-oid"
BOT = "bot-oid"
BOT_APP = "bot-app-id"
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


class _Authenticator:
    def __init__(self, roles: list[str], groups: frozenset[str] = frozenset()) -> None:
        self._context = AuthContext(
            object_id=ALICE, tenant_id=TENANT, roles=frozenset(roles), group_ids=groups
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
        self.sign_in(["User", "Admin"])
        self.logs = FakeLogs(clock=lambda: self.now)
        self.job = UsageRollupService(
            self.state.usage_rollup_repository,
            gateway_repository=self.state.gateway_repository,
            entitlement_repository=self.state.entitlement_repository,
            directory_repository=self.state.repository,
            logs=self.logs,
            telemetry=None,
            tenant_id=TENANT,
            backfill_max_days=30,
            clock=lambda: self.now,
        )
        self.state.usage_rollup_service = self.job
        self.state.analytics_service = AnalyticsService(
            self.state.usage_rollup_repository,
            gateway_repository=self.state.gateway_repository,
            entitlement_repository=self.state.entitlement_repository,
            directory_repository=self.state.repository,
            environment_repository=self.state.environment_repository,
            endpoint_repository=self.state.model_endpoint_repository,
            rollups=self.job,
            configured=True,
            clock=lambda: self.now,
        )
        self.state.usage_service = UsageService(
            self.state.portal_service,
            source=RollupUsageSource(
                self.state.usage_rollup_repository, clock=lambda: self.now
            ),
            gateway_repository=self.state.gateway_repository,
            endpoint_repository=self.state.model_endpoint_repository,
            environment_repository=self.state.environment_repository,
            clock=lambda: self.now,
        )

    def sign_in(self, roles: list[str]) -> None:
        """Sign Alice in with these app roles. Her token names the Analysts security group."""

        self.state.authenticator = _Authenticator(roles, frozenset({ANALYSTS}))

    async def roll_up(self) -> None:
        """Run cycles until the first backfill has read every day it covers."""

        for _ in range(10):
            states = await self.job.run_cycle()
            if all(state.backfill_status != "running" for state in states):
                return
        raise AssertionError("The backfill never finished")

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


async def _principal(harness: Harness, object_id: str, kind: PrincipalKind, label: str,
                     detail: str | None = None) -> Principal:
    return await harness.state.repository.create_principal(
        Principal(
            id=f"principal-{object_id}",
            tenant_id=TENANT,
            object_id=object_id,
            kind=kind,
            label=label,
            detail=detail,
        ),
        _audit("principal"),
    )


async def _grant(
    harness: Harness,
    grant_id: str,
    subject: EntitlementSubject,
    resource: EntitlementResource,
    binding: EntitlementBinding | None,
    *,
    enforcement: EntitlementEnforcement | None = None,
    created_days_ago: int = 60,
) -> Entitlement:
    return await harness.state.entitlement_repository.save_entitlement(
        Entitlement(
            id=grant_id,
            tenant_id=TENANT,
            created_at=NOW - timedelta(days=created_days_ago),
            subject=subject,
            resource=resource,
            enforcement=enforcement,
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
        id="endpoint-aoai",
        tenant_id=TENANT,
        name="Contoso Azure OpenAI",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://aoai.example.openai.azure.com",
        environment="production",
    )
    await state.model_endpoint_repository.save_endpoint(endpoint, _audit("endpoint"))
    await state.model_endpoint_repository.replace_observed_for_endpoint(
        TENANT,
        endpoint.id,
        [
            ObservedModelDeployment(
                id="chat-prod",
                tenant_id=TENANT,
                endpoint_id=endpoint.id,
                snapshot_id="snapshot",
                deployment_name="chat-prod",
                model_name="gpt-4o",
                model_format="OpenAI",
                sku_name="Standard",
                sku_capacity=10,
            ),
            ObservedModelDeployment(
                id="mini-prod",
                tenant_id=TENANT,
                endpoint_id=endpoint.id,
                snapshot_id="snapshot",
                deployment_name="mini-prod",
                model_name="gpt-4o-mini",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=50,
            ),
        ],
        "snapshot",
    )
    for api_name, display_name, deployment in (("chat", "Chat", "chat-prod"),
                                                ("mini", "Summaries", "mini-prod")):
        publication = Publication(
            id=f"publication-{api_name}",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            model_endpoint_id=endpoint.id,
            deployment_name=deployment,
            provider=ModelProvider.AZURE_OPENAI,
            display_name=display_name,
            api_name=api_name,
            api_path=api_name,
            backend_name=api_name,
            fragment_name=api_name,
            product_name=api_name,
            subscription_name=api_name,
            shape_version="v1",
            api_shape=ApiShape.AZURE_OPENAI,
        )
        await state.gateway_repository.save_publication(publication, _audit("publication"))
        await state.gateway_repository.save_model_api(
            ModelApi(
                id=f"model-api-{api_name}",
                tenant_id=TENANT,
                gateway_id=GATEWAY,
                api_name=api_name,
                display_name=display_name,
                path=api_name,
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

    alice = await _principal(harness, ALICE, PrincipalKind.USER, "Alice Admin", "alice@contoso")
    carol = await _principal(harness, CAROL, PrincipalKind.USER, "Carol Analyst")
    bob = await _principal(harness, "bob-oid", PrincipalKind.USER, "Bob Builder")
    bot = await _principal(harness, BOT, PrincipalKind.SERVICE_PRINCIPAL, "Support bot", BOT_APP)
    analysts = await _principal(harness, ANALYSTS, PrincipalKind.SECURITY_GROUP, "Analysts")
    # A MOSAIC group is desired state only: the gateway never enforces its grants.
    team = await state.repository.create_group(
        Group(id="group-team", tenant_id=TENANT, name="Platform team"), _audit("group")
    )
    for member in (alice, carol):
        await state.repository.create_membership(
            GroupMembership(
                id=f"membership-{member.id}",
                tenant_id=TENANT,
                group_id=team.id,
                principal_id=member.id,
            ),
            team,
            member,
            _audit("membership"),
        )

    chat = EntitlementResource(kind="modelApi", id="model-api-chat")
    mini = EntitlementResource(kind="modelApi", id="model-api-mini")
    tickets = EntitlementResource(kind="mcpServer", id="mcp-tickets")
    await _grant(
        harness,
        "grant-alice",
        EntitlementSubject(kind="user", id=alice.id),
        chat,
        # Alice signs in with a token, so her grant's key is never used.
        EntitlementBinding(
            gateway_id=GATEWAY,
            attribution_key="k-alice",
            apim_subscription_name="sub-alice",
            source=BindingSource.ORCHESTRATED,
        ),
        enforcement=EntitlementEnforcement(
            tokens=TokenEnforcement(
                counter_key_expression="@(context.Subscription.Id)",
                tokens_per_minute=2_000,
                token_quota=20_000,
                token_quota_period="Monthly",
            )
        ),
    )
    await _grant(
        harness,
        "grant-bot",
        EntitlementSubject(kind="application", id=bot.id),
        chat,
        EntitlementBinding(gateway_id=GATEWAY, apim_subscription_name="sub-bot"),
    )
    await _grant(
        harness,
        "grant-analysts",
        EntitlementSubject(kind="securityGroup", id=analysts.id),
        mini,
        _traced("k-analysts", per_member=True),
    )
    await _grant(
        harness, "grant-team", EntitlementSubject(kind="group", id=team.id), tickets, None
    )
    await _grant(
        harness, "grant-alice-tickets", EntitlementSubject(kind="user", id=alice.id), tickets,
        _traced("k-alice-tickets"),
    )
    await _grant(
        harness, "grant-bob", EntitlementSubject(kind="user", id=bob.id), tickets,
        _traced("k-bob"),
    )

    calls: list[GatewayCall] = []
    for age in range(10):
        day = NOW.replace(hour=0, minute=0) - timedelta(days=age)
        for index in range(10):
            calls.append(
                GatewayCall(
                    time=day.replace(hour=10, minute=5, second=index),
                    api="chat",
                    grant="k-alice",
                    member=ALICE,
                    client_app="cli-app",
                    prompt_tokens=100,
                    completion_tokens=50,
                    deployment="chat-prod",
                    model="gpt-4o",
                    total_time_ms=300,
                )
            )
        for index in range(5):
            calls.append(
                GatewayCall(
                    time=day.replace(hour=11, second=index),
                    api="chat",
                    traced=False,
                    subscription="sub-bot",
                    prompt_tokens=200,
                    completion_tokens=100,
                    deployment="chat-prod",
                    model="gpt-4o",
                    total_time_ms=1_500,
                )
            )
    yesterday = NOW.replace(hour=0, minute=0) - timedelta(days=1)
    for member, count in ((ALICE, 3), (CAROL, 4)):
        for index in range(count):
            calls.append(
                GatewayCall(
                    time=yesterday.replace(hour=12, second=index),
                    api="mini",
                    grant="k-analysts",
                    member=member,
                    prompt_tokens=10,
                    completion_tokens=10,
                    deployment="mini-prod",
                    model="gpt-4o-mini",
                )
            )
    calls.append(
        GatewayCall(
            time=yesterday.replace(hour=13),
            api="chat",
            grant="k-alice",
            member=ALICE,
            response_code=429,
            backend_code=0,
        )
    )
    for index in range(2):
        calls.append(
            GatewayCall(
                time=yesterday.replace(hour=14, second=index),
                api="chat",
                traced=False,
                response_code=403,
                backend_code=0,
                denial_reason="no-grant",
                denied_caller="dave-oid",
                client_app="dave-app",
            )
        )
    for index in range(3):
        calls.append(
            GatewayCall(
                time=yesterday.replace(hour=9, second=index),
                api="chat",
                traced=False,
                subscription="unknown-sub",
                prompt_tokens=5,
                completion_tokens=5,
                deployment="chat-prod",
                model="gpt-4o",
            )
        )
    for index in range(2):
        calls.append(
            GatewayCall(
                time=(yesterday - timedelta(days=1)).replace(hour=8, second=index),
                api="tickets",
                grant="k-alice-tickets",
                member=ALICE,
            )
        )
    # A call to an API MOSAIC doesn't govern is never read.
    calls.append(GatewayCall(time=yesterday.replace(hour=8), api="payroll", subscription="x"))
    harness.logs.calls = calls


# Over the last 30 days: Alice's 100 calls and 1 throttled, the bot's 50, the analysts' 7, two
# refusals, three calls on an unknown key, and Alice's two MCP calls.
REQUESTS = 101 + 50 + 7 + 2 + 3 + 2
TOKENS = 100 * 150 + 50 * 300 + 7 * 20 + 3 * 10


async def rolled_up(harness: Harness) -> Harness:
    await seed(harness)
    await harness.roll_up()
    return harness


async def _month_requests(harness: Harness) -> int:
    month = await harness.state.usage_rollup_repository.list_summaries(
        TENANT,
        period="month",
        start=TODAY.replace(day=1).isoformat(),
        end=TODAY.replace(day=1).isoformat(),
        dimensions=["total"],
        gateway_ids=[GATEWAY],
    )
    return sum(entry.metrics.requests for summary in month for entry in summary.entries)


async def test_rollup_reads_every_governed_call_once(harness: Harness) -> None:
    await rolled_up(harness)

    states = await harness.job.list_states(TENANT)
    assert len(states) == 1
    state = states[0]
    assert state.last_error is None
    assert state.backfill_status == "done"
    assert state.data_available_from == (TODAY - timedelta(days=29)).isoformat()
    assert state.queried_through == NOW
    assert {api.api_name for api in state.apis} == {"chat", "mini", "tickets"}

    summaries = await harness.state.usage_rollup_repository.list_summaries(
        TENANT,
        period="day",
        start=(TODAY - timedelta(days=29)).isoformat(),
        end=TODAY.isoformat(),
        dimensions=["total"],
        gateway_ids=[GATEWAY],
    )
    requests = sum(entry.metrics.requests for summary in summaries for entry in summary.entries)
    tokens = sum(entry.metrics.total_tokens for summary in summaries for entry in summary.entries)
    assert (requests, tokens) == (REQUESTS, TOKENS)

    # Nothing changed, so a second cycle writes nothing.
    rerun = await harness.job.run_cycle()
    assert rerun[0].last_written == 0
    assert await _month_requests(harness) == REQUESTS


async def test_long_ranges_are_read_in_smaller_queries(harness: Harness) -> None:
    await seed(harness)
    # Log Analytics refuses any result larger than six hours of these calls.
    harness.logs.max_hours = 6
    await harness.roll_up()

    report = harness.get("/api/v1/analytics/overview", range="30d")
    assert (report["kpis"]["requests"], report["kpis"]["totalTokens"]) == (REQUESTS, TOKENS)
    assert harness.logs.answered
    assert max(end - start for start, end in harness.logs.answered) <= timedelta(hours=6)


async def test_my_usage_is_measured_from_the_gateway(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/me/usage", period="30d")
    assert report["dataSource"] == "logAnalytics"
    assert report["freshness"]["status"] == "current"
    rows = {row["entitlementId"]: row for row in report["byResource"]}
    assert set(rows) == {"grant-alice", "grant-alice-tickets", "grant-analysts"}

    chat = rows["grant-alice"]
    assert (chat["attribution"], chat["linkedBy"]) == ("measured", "gatewayLog")
    assert (chat["requests"], chat["totalTokens"], chat["throttled"]) == (101, 15_000, 1)
    assert chat["peakMinuteTokens"] == 1_500
    [quota] = chat["quotas"]
    assert (quota["used"], quota["utilization"]) == (15_000, 0.75)

    group = rows["grant-analysts"]
    assert (group["via"], group["viaGroupName"]) == ("securityGroup", "Analysts")
    # Alice sees her own calls through the group's grant, not Carol's.
    assert (group["requests"], group["totalTokens"]) == (3, 60)

    tickets = rows["grant-alice-tickets"]
    assert (tickets["requests"], tickets["totalTokens"]) == (2, None)

    assert (report["totals"]["requests"], report["totals"]["totalTokens"]) == (106, 15_060)
    # Over the last 24 hours Alice made today's ten calls at ten o'clock.
    recent = {
        point["hour"][11:13]: point["requests"]
        for point in report["recentHours"]
        if point["requests"]
    }
    assert recent == {"10": 10}


async def test_my_usage_leaves_unmeasured_grants_out_of_environment_figures(
    harness: Harness,
) -> None:
    await rolled_up(harness)
    await harness.state.gateway_repository.save_mcp_server(
        McpServer(
            id="mcp-wiki",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            api_name="wiki",
            display_name="Wiki tools",
            path="wiki",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("mcp"),
    )
    # A grant on an API MOSAIC adopted but didn't publish has no binding to measure it by.
    alice = await harness.state.repository.find_principal_by_object_id(TENANT, ALICE)
    assert alice is not None
    await _grant(
        harness,
        "grant-alice-wiki",
        EntitlementSubject(kind="user", id=alice.id),
        EntitlementResource(kind="mcpServer", id="mcp-wiki"),
        None,
    )

    report = harness.get("/api/v1/me/usage", period="30d")

    wiki = next(row for row in report["byResource"] if row["entitlementId"] == "grant-alice-wiki")
    assert (wiki["attribution"], wiki["requests"]) == ("unattributed", None)
    [production] = report["byEnvironment"]
    assert (production["resources"], production["unmeasuredResources"]) == (4, 1)
    assert production["requests"] == report["totals"]["requests"] == 106


async def test_overview_counts_every_call(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/overview", range="30d")
    assert report["dataSource"] == "logAnalytics"
    assert report["notes"] == []
    kpis = report["kpis"]
    assert (kpis["requests"], kpis["totalTokens"]) == (REQUESTS, TOKENS)
    assert (kpis["denied"], kpis["throttled"], kpis["unattributedRequests"]) == (2, 1, 3)
    assert (kpis["activeCallers"], kpis["activeGrants"], kpis["activeApis"]) == (3, 4, 3)
    # MOSAIC has no figures from before the first day it read, so there's nothing to compare.
    assert report["previous"] is None
    assert sum(point["requests"] for point in report["trend"]) == REQUESTS
    callers = [(row["label"], row["requests"]) for row in report["topCallers"]]
    assert callers == [("Alice Admin", 106), ("Support bot", 50), ("Carol Analyst", 4)]
    assert [row["label"] for row in report["topModels"]] == ["gpt-4o", "gpt-4o-mini"]

    week = harness.get("/api/v1/analytics/overview", range="7d")
    assert week["kpis"]["requests"] == 7 * 15 + 13 + 2
    assert week["previous"]["requests"] == 3 * 15

    day = harness.get("/api/v1/analytics/overview", range="24h")
    assert (day["kpis"]["requests"], day["kpis"]["totalTokens"]) == (15, 3_000)
    assert day["previous"]["requests"] == 28
    hours = {
        point["start"][11:13]: point["requests"] for point in day["trend"] if point["requests"]
    }
    assert hours == {"10": 10, "11": 5}
    assert len(day["notes"]) == 1


async def test_consumers_name_people_applications_and_groups(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/consumers", range="30d")
    people = {
        row["label"]: (row["requests"], row["totalTokens"], row["grants"])
        for row in report["people"]
    }
    assert people == {"Alice Admin": (106, 15_060, 3), "Carol Analyst": (4, 80, 1)}
    assert {row["principalKind"] for row in report["people"]} == {"user"}
    [bot] = report["applications"]
    assert (bot["label"], bot["detail"], bot["requests"]) == ("Support bot", BOT_APP, 50)
    assert bot["principalKind"] == "servicePrincipal"
    [group] = report["groups"]
    assert (group["label"], group["members"], group["requests"], group["totalTokens"]) == (
        "Analysts",
        2,
        7,
        140,
    )
    assert group["principalKind"] == "securityGroup"
    grants = {row["entitlementId"]: row for row in report["grants"]}
    assert set(grants) == {
        "grant-alice",
        "grant-bot",
        "grant-analysts",
        "grant-alice-tickets",
        "grant-bob",
    }
    assert grants["grant-bob"]["requests"] == 0
    assert grants["grant-bot"]["keyRequests"] == 50
    assert grants["grant-analysts"]["callers"] == 2
    # Grant rows say what their subject is, the same way the consumer rows do.
    assert (
        grants["grant-alice"]["subjectPrincipalKind"],
        grants["grant-bot"]["subjectPrincipalKind"],
        grants["grant-analysts"]["subjectPrincipalKind"],
    ) == ("user", "servicePrincipal", "securityGroup")
    assert (report["linkedRequests"], report["unidentifiedRequests"]) == (160, 0)
    [client] = report["clientApps"]
    assert (client["clientAppId"], client["requests"]) == ("cli-app", 100)

    only_groups = harness.get(
        "/api/v1/analytics/consumers", range="30d", subjectKind="securityGroup"
    )
    assert [row["entitlementId"] for row in only_groups["grants"]] == ["grant-analysts"]
    people = {
        row["label"]: (row["requests"], row["totalTokens"]) for row in only_groups["people"]
    }
    assert people == {"Alice Admin": (3, 60), "Carol Analyst": (4, 80)}
    assert only_groups["applications"] == []

    # A MOSAIC group grant is never enforced at the gateway, so no call can be made through it.
    mosaic_groups = harness.get("/api/v1/analytics/consumers", range="30d", subjectKind="group")
    assert (mosaic_groups["grants"], mosaic_groups["people"]) == ([], [])


async def test_models_follow_calls_to_their_deployments(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/models", range="30d")
    models = {row["model"]: row["requests"] for row in report["models"]}
    assert models == {"gpt-4o": 153, "gpt-4o-mini": 7}
    deployments = {row["deploymentName"]: row for row in report["deployments"]}
    chat = deployments["chat-prod"]
    assert (chat["capacityTokensPerMinute"], chat["peakMinuteTokens"], chat["utilization"]) == (
        10_000,
        1_500,
        0.15,
    )
    mini = deployments["mini-prod"]
    assert (mini["modelName"], mini["capacityTokensPerMinute"], mini["peakMinuteTokens"]) == (
        "gpt-4o-mini",
        50_000,
        140,
    )
    apis = {row["apiName"]: row for row in report["apis"]}
    assert apis["chat"]["models"] == ["gpt-4o"]
    assert (apis["tickets"]["kind"], apis["tickets"]["models"]) == ("mcp", [])


async def test_reliability_explains_refusals(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/reliability", range="30d")
    mix = report["statusMix"]
    assert (mix["requests"], mix["ok"], mix["throttled"], mix["denied"]) == (REQUESTS, 162, 1, 2)
    # Latency covers the calls the gateway admitted; it answers refusals itself.
    assert sum(bucket["count"] for bucket in report["latency"]["buckets"]) == REQUESTS - 2
    [denial] = report["denials"]
    assert (denial["reason"], denial["reasonLabel"], denial["callerObjectId"]) == (
        "no-grant",
        "No grant for this caller",
        "dave-oid",
    )
    assert (denial["clientAppId"], denial["requests"]) == ("dave-app", 2)
    # Reliability doesn't read models, so it doesn't claim any.
    assert all(row["models"] is None for row in report["apis"])


async def test_an_application_signing_in_as_itself_names_its_client_app(
    harness: Harness,
) -> None:
    # MOSAIC keeps no app IDs for managed identities or service principals.
    await seed(harness)
    claims = await _principal(
        harness, "claims-oid", PrincipalKind.MANAGED_IDENTITY, "Claims function"
    )
    await _principal(harness, "script-oid", PrincipalKind.SERVICE_PRINCIPAL, "Nightly script")
    await _grant(
        harness,
        "grant-claims",
        EntitlementSubject(kind="application", id=claims.id),
        EntitlementResource(kind="modelApi", id="model-api-chat"),
        _traced("k-claims"),
    )
    yesterday = NOW.replace(hour=0, minute=0) - timedelta(days=1)
    calls = [
        GatewayCall(
            time=yesterday.replace(hour=15, second=index),
            api="chat",
            grant="k-claims",
            client_app="claims-app",
            prompt_tokens=10,
            completion_tokens=10,
            deployment="chat-prod",
            model="gpt-4o",
        )
        for index in range(4)
    ]
    for caller, client_app in (("script-oid", "script-app"), (CAROL, "carol-app")):
        calls.append(
            GatewayCall(
                time=yesterday.replace(hour=16),
                api="chat",
                traced=False,
                response_code=403,
                backend_code=0,
                denial_reason="no-grant",
                denied_caller=caller,
                client_app=client_app,
            )
        )
    harness.logs.calls = [*harness.logs.calls, *calls]
    await harness.roll_up()

    for window in ("30d", "12m"):
        clients = harness.get("/api/v1/analytics/consumers", range=window)["clientApps"]
        names = {row["clientAppId"]: (row["label"], row["principalId"]) for row in clients}
        assert names == {
            "cli-app": ("Unknown application", None),
            "claims-app": ("Claims function", claims.id),
        }
    denials = harness.get("/api/v1/analytics/reliability", range="30d")["denials"]
    labels = {row["clientAppId"]: row["clientAppLabel"] for row in denials}
    # A person's token names whichever client they signed in through, so it names no app ID.
    assert labels == {
        "dave-app": "Unknown application",
        "script-app": "Nightly script",
        "carol-app": "Unknown application",
    }


async def test_limits_show_how_close_grants_are(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/limits", range="30d")
    [row] = report["rows"]
    assert row["entitlementId"] == "grant-alice"
    assert (row["subjectKind"], row["subjectPrincipalKind"]) == ("user", "user")
    quota, rate = row["limits"]
    assert (quota["kind"], quota["used"], quota["utilization"]) == ("quota", 15_000, 0.75)
    assert (rate["kind"], rate["used"], rate["limit"]) == ("rateLimit", 1_500, 2_000)
    # The gateway stops calls at a limit, so a throttled call, not the busiest admitted minute,
    # shows that Alice reached hers yesterday.
    assert (row["status"], row["throttled"], report["near"], report["reached"]) == (
        "reached",
        1,
        0,
        1,
    )

    # A thousand more tokens today takes Alice to 80% of her monthly quota. The model deployment
    # throttling her too is its capacity, not her limit.
    for backend_code, tokens in ((200, 1_000), (429, 0)):
        harness.logs.calls.append(
            GatewayCall(
                time=NOW - timedelta(minutes=20),
                api="chat",
                grant="k-alice",
                member=ALICE,
                response_code=backend_code,
                backend_code=backend_code,
                prompt_tokens=tokens * 6 // 10,
                completion_tokens=tokens * 4 // 10,
                deployment="chat-prod",
                model="gpt-4o",
            )
        )
    await harness.job.run_cycle()
    today = TODAY.isoformat()
    report = harness.get("/api/v1/analytics/limits", range="custom", start=today, end=today)
    [row] = report["rows"]
    assert (row["limits"][0]["used"], row["throttled"]) == (16_000, 0)
    assert (row["status"], report["near"], report["reached"]) == ("near", 1, 0)


async def test_hygiene_finds_grants_to_tidy(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/hygiene", range="30d")
    assert report["judgedGrants"] == 5
    assert [row["entitlementId"] for row in report["unusedGrants"]] == ["grant-bob"]
    assert [(row["entitlementId"], row["tokenRequests"]) for row in report["unusedKeys"]] == [
        ("grant-alice", 101)
    ]
    assert [(row["entitlementId"], row["reason"]) for row in report["untrackedGrants"]] == [
        ("grant-team", "mosaicGroup")
    ]
    # A MOSAIC group is not an Entra object, so it has no principal kind.
    assert report["untrackedGrants"][0]["subjectPrincipalKind"] is None
    assert [row["callerObjectId"] for row in report["deniedCallers"]] == ["dave-oid"]
    assert "Access hygiene always covers the last 30 days" not in " ".join(report["notes"])

    # Any other range still judges the last 30 days, and the report says so.
    week = harness.get("/api/v1/analytics/hygiene", range="7d")
    assert week["window"]["range"] == "30d"
    assert [row["entitlementId"] for row in week["unusedGrants"]] == ["grant-bob"]
    assert (
        "Access hygiene always covers the last 30 days, whatever range is chosen." in week["notes"]
    )


async def test_unattributed_calls_name_the_unknown_key(harness: Harness) -> None:
    await rolled_up(harness)
    # Someone calls Summaries with the publication's own key, which is no one caller's.
    harness.logs.calls.append(
        GatewayCall(
            time=NOW - timedelta(minutes=20),
            api="mini",
            traced=False,
            subscription="mini",
            prompt_tokens=4,
            completion_tokens=4,
            deployment="mini-prod",
            model="gpt-4o-mini",
        )
    )
    await harness.job.run_cycle()

    report = harness.get("/api/v1/analytics/unattributed", range="30d")
    rows = [
        (row["subscription"], row["reason"], row["requests"], row["totalTokens"])
        for row in report["rows"]
    ]
    assert rows == [("unknown-sub", "unknownSubscription", 3, 30), ("mini", "sharedKey", 1, 8)]
    assert (report["requests"], report["admittedRequests"]) == (4, REQUESTS - 1)


async def test_export_is_a_spreadsheet_safe_csv(harness: Harness) -> None:
    await rolled_up(harness)
    carol = await harness.state.repository.get_principal(TENANT, f"principal-{CAROL}")
    await harness.state.repository.save_principal(
        carol.model_copy(update={"detail": "=1+2"}), _audit("principal")
    )

    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "people", "range": "30d"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/csv; charset=utf-8"
    assert response.headers["content-disposition"].startswith('attachment; filename="')
    assert "no-store" in response.headers["cache-control"]
    body = response.content.decode("utf-8")
    assert body.startswith("\ufeff")
    rows = list(csv.reader(io.StringIO(body[1:])))
    assert rows[0][:5] == ["Name", "Detail", "Object ID", "Kind", "Principal kind"]
    [carol_row] = [row for row in rows if row[0] == "Carol Analyst"]
    # A spreadsheet would run a cell that starts with "=", so the export quotes it.
    assert carol_row[1] == "'=1+2"
    assert carol_row[3:5] == ["person", "user"]

    unknown = harness.client.get("/api/v1/analytics/export", params={"view": "everything"})
    assert unknown.status_code == 422


def test_a_console_on_another_origin_can_read_the_export_file_name(settings: Settings) -> None:
    console = "http://localhost:5173"
    app = create_app(settings.model_copy(update={"cors_origins": [console]}))
    with TestClient(app) as client:
        response = client.get(
            "/api/v1/analytics/export",
            params={"view": "people", "range": "30d"},
            headers={"Origin": console},
        )
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="mosaic-people-')
    # Browsers hide the header from a cross-origin script unless the API exposes it.
    assert response.headers["access-control-expose-headers"] == "Content-Disposition"


async def test_analytics_need_the_admin_role(harness: Harness) -> None:
    await rolled_up(harness)
    harness.sign_in(["User"])

    assert harness.client.get("/api/v1/analytics/overview").status_code == 403
    assert harness.client.get("/api/v1/analytics/export?view=people").status_code == 403
    assert harness.client.post("/api/v1/analytics/refresh").status_code == 403
    assert harness.client.get(f"/api/v1/gateways/{GATEWAY}/telemetry").status_code == 403
    # A person's own usage needs only the portal role.
    assert harness.get("/api/v1/me/usage")["totals"]["requests"] == 106


async def test_refreshes_are_rate_limited(harness: Harness) -> None:
    await rolled_up(harness)

    assert harness.client.post("/api/v1/analytics/refresh").status_code == 202
    assert harness.client.post("/api/v1/analytics/refresh").status_code == 429
    gateway = f"/api/v1/gateways/{GATEWAY}/telemetry/refresh"
    assert harness.client.post(gateway).status_code == 202
    assert harness.client.post(gateway).status_code == 429
    harness.now += timedelta(minutes=2)
    assert harness.client.post("/api/v1/analytics/refresh").status_code == 202
    assert harness.client.post(gateway).status_code == 202


async def test_backfills_are_validated_and_audited(harness: Harness) -> None:
    await rolled_up(harness)
    path = f"/api/v1/gateways/{GATEWAY}/telemetry/backfill"

    assert harness.client.post(path, json={"days": 0}).status_code == 422
    assert harness.client.post(path, json={"days": 731}).status_code == 422
    missing = harness.client.post("/api/v1/gateways/missing/telemetry/backfill", json={})
    assert missing.status_code == 404

    response = harness.client.post(path, json={"days": 60})
    assert response.status_code == 202
    body = response.json()
    assert body["backfillStatus"] == "running"
    assert body["backfillFrom"] == (TODAY - timedelta(days=59)).isoformat()
    [event] = [
        event
        for event in harness.state.entitlement_repository.audit_events.values()
        if event.action == "gateway.telemetryBackfillRequested"
    ]
    assert (event.resource_id, event.details["days"]) == (GATEWAY, 60)


async def test_reading_a_day_again_never_lowers_it(harness: Harness) -> None:
    await rolled_up(harness)

    # Log Analytics keeps logs for a limited time. Once a day's logs are gone, reading the day
    # again finds nothing, which mustn't erase what MOSAIC already knows.
    oldest = TODAY - timedelta(days=9)
    harness.logs.calls = [call for call in harness.logs.calls if call.time.date() != oldest]
    response = harness.client.post(
        f"/api/v1/gateways/{GATEWAY}/telemetry/backfill", json={"days": 30}
    )
    assert response.status_code == 202
    await harness.roll_up()

    report = harness.get("/api/v1/analytics/overview", range="30d")
    assert report["kpis"]["requests"] == REQUESTS
    assert await _month_requests(harness) == REQUESTS


async def test_a_month_is_never_folded_lower(harness: Harness) -> None:
    await rolled_up(harness)
    repository = harness.state.usage_rollup_repository

    # A day's figures expire before its month is folded again.
    expired = await repository.list_summaries(
        TENANT,
        period="day",
        start=(TODAY - timedelta(days=8)).isoformat(),
        end=(TODAY - timedelta(days=8)).isoformat(),
        dimensions=["total", "api"],
        gateway_ids=[GATEWAY],
    )
    assert expired
    await repository.delete_rollups(TENANT, [summary.id for summary in expired])
    harness.logs.calls.append(
        GatewayCall(time=NOW - timedelta(minutes=5), api="chat", grant="k-alice", member=ALICE)
    )
    await harness.job.run_cycle()

    # The month keeps the fifteen calls of the expired day rather than dropping them.
    assert await _month_requests(harness) == REQUESTS


async def test_a_gateway_mosaic_cannot_read_says_why(harness: Harness) -> None:
    await seed(harness)
    harness.logs.failures[RESOURCE_ID] = LogQueryAccessError(
        "MOSAIC's identity can't read this gateway's logs"
    )
    [state] = await harness.job.run_cycle()
    assert state.last_error

    status = harness.get("/api/v1/analytics/status")
    [gateway] = status["gateways"]
    assert gateway["status"] == "failing"
    assert gateway["lastError"] == state.last_error
    overview = harness.get("/api/v1/analytics/overview")
    assert overview["kpis"]["requests"] == 0
    assert overview["notes"]

    del harness.logs.failures[RESOURCE_ID]
    await harness.roll_up()
    status = harness.get("/api/v1/analytics/status")
    assert status["gateways"][0]["status"] == "current"
    assert harness.get("/api/v1/analytics/overview")["kpis"]["requests"] == REQUESTS


def test_analytics_say_when_telemetry_is_off() -> None:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["User", "Admin"],
    )
    with TestClient(create_app(settings)) as client:
        overview = client.get("/api/v1/analytics/overview")
        assert overview.status_code == 200
        body = overview.json()
        assert body["dataSource"] == "notConfigured"
        assert body["kpis"]["requests"] == 0
        assert any("MOSAIC_USAGE_SOURCE" in note for note in body["notes"])
        assert client.post("/api/v1/analytics/refresh").status_code == 409
