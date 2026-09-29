from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
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
    RequestEnforcement,
    TokenEnforcement,
    new_id,
)
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment, ObservedProduct
from mosaic_api.services.directory import Actor
from mosaic_api.services.usage import (
    DailyUsage,
    SimulatedUsageSource,
    UsageService,
)

TENANT = "tenant-test"
NOW = datetime(2026, 9, 29, 15, 30, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["User", "Admin"],
    )


@pytest.fixture
def usage_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        _set_usage_service(client)
        yield client


def _set_usage_service(client: TestClient, source: Any | None = None) -> None:
    client.app.state.usage_service = UsageService(
        client.app.state.portal_service,
        source=source
        or SimulatedUsageSource(
            environment_repository=client.app.state.environment_repository
        ),
        gateway_repository=client.app.state.gateway_repository,
        endpoint_repository=client.app.state.model_endpoint_repository,
        environment_repository=client.app.state.environment_repository,
        clock=lambda: NOW,
    )


def _audit(kind: str = "test") -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type=kind,
        resource_id=kind,
        actor_object_id="tester",
    )


class _StubAuthenticator:
    def __init__(self, object_id: str, roles: list[str] | None = None) -> None:
        self._context = AuthContext(
            object_id=object_id, tenant_id=TENANT, roles=frozenset(roles or ["User"])
        )

    async def authenticate(self, _request: object) -> AuthContext:
        return self._context

    async def close(self) -> None:
        return None


async def _principal(client: TestClient, object_id: str = "local-admin") -> Principal:
    return await client.app.state.repository.create_principal(
        Principal(
            id=f"principal-{object_id}",
            tenant_id=TENANT,
            object_id=object_id,
            kind=PrincipalKind.USER,
            label=object_id,
        ),
        _audit("principal"),
    )


async def _seed_resource_catalog(client: TestClient) -> None:
    gateway = Gateway(
        id="gateway-prod",
        tenant_id=TENANT,
        name="Production gateway",
        azure_resource_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
            "/providers/Microsoft.ApiManagement/service/gateway-prod"
        ),
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_group="rg",
        service_name="gateway-prod",
        environment="production",
    )
    await client.app.state.gateway_repository.save_gateway(gateway, _audit("gateway"))
    endpoint = ModelEndpoint(
        id="endpoint-staging",
        tenant_id=TENANT,
        name="Staging AOAI",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://aoai.example.openai.azure.com",
        environment="staging",
    )
    await client.app.state.model_endpoint_repository.save_endpoint(
        endpoint, _audit("endpoint")
    )
    await client.app.state.model_endpoint_repository.replace_observed_for_endpoint(
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
            ),
            ObservedModelDeployment(
                id="custom-deploy",
                tenant_id=TENANT,
                endpoint_id=endpoint.id,
                snapshot_id="snapshot",
                deployment_name="custom-deploy",
                model_name="custom-expensive-model",
            ),
        ],
        "snapshot",
    )
    publication = Publication(
        id="publication-chat",
        tenant_id=TENANT,
        gateway_id=gateway.id,
        model_endpoint_id=endpoint.id,
        deployment_name="chat-prod",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Chat completions",
        api_name="chat",
        api_path="chat",
        backend_name="chat",
        fragment_name="chat",
        product_name="chat",
        subscription_name="chat",
        shape_version="v1",
        api_shape=ApiShape.AZURE_OPENAI,
    )
    await client.app.state.gateway_repository.save_publication(
        publication, _audit("publication")
    )
    await client.app.state.gateway_repository.save_model_api(
        ModelApi(
            id="model-api-known",
            tenant_id=TENANT,
            gateway_id=gateway.id,
            api_name="chat",
            display_name="Chat completions",
            path="chat",
            publication_id=publication.id,
        ),
        _audit("model-api"),
    )
    await client.app.state.gateway_repository.save_model_api(
        ModelApi(
            id="model-api-unknown",
            tenant_id=TENANT,
            gateway_id=gateway.id,
            api_name="legacy",
            display_name="Legacy API",
            path="legacy",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("model-api"),
    )
    await client.app.state.gateway_repository.save_model_api(
        ModelApi(
            id="model-api-weekly",
            tenant_id=TENANT,
            gateway_id=gateway.id,
            api_name="chat-weekly",
            display_name="Weekly chat",
            path="chat-weekly",
            publication_id=publication.id,
        ),
        _audit("model-api"),
    )
    await client.app.state.gateway_repository.save_mcp_server(
        McpServer(
            id="mcp-tickets",
            tenant_id=TENANT,
            gateway_id=gateway.id,
            api_name="tickets",
            display_name="Ticket tools",
            path="tickets",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("mcp"),
    )
    await client.app.state.gateway_repository.replace_observed(
        TENANT,
        gateway.id,
        [
            ObservedProduct(
                id="gold-product",
                tenant_id=TENANT,
                gateway_id=gateway.id,
                snapshot_id="snapshot",
                name="gold",
                display_name="Gold product",
            )
        ],
        "snapshot",
    )


async def _grant(
    client: TestClient,
    principal: Principal,
    grant_id: str,
    resource: EntitlementResource,
    *,
    enforcement: EntitlementEnforcement | None = None,
    enabled: bool = True,
    bound: bool = True,
    created_days_ago: int = 30,
) -> Entitlement:
    entitlement = Entitlement(
        id=grant_id,
        tenant_id=TENANT,
        created_at=NOW - timedelta(days=created_days_ago),
        subject=EntitlementSubject(kind="user", id=principal.id),
        resource=resource,
        enabled=enabled,
        enforcement=enforcement,
        binding=EntitlementBinding(gateway_id="gateway-prod", source=BindingSource.MANUAL)
        if bound
        else None,
    )
    return await client.app.state.entitlement_repository.save_entitlement(
        entitlement, _audit("entitlement")
    )


def _token_enforcement(period: str, limit: int = 50_000) -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            tokens_per_minute=20_000,
            token_quota=limit,
            token_quota_period=period,
        )
    )


def _request_enforcement(period: str, limit: int = 2_000) -> EntitlementEnforcement:
    return EntitlementEnforcement(
        requests=RequestEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            calls=100,
            renewal_period_seconds=60,
            call_quota=limit,
            call_quota_period=period,
        )
    )


async def test_usage_route_periods_and_validation(usage_client: TestClient) -> None:
    await _seed_resource_catalog(usage_client)
    principal = await _principal(usage_client)
    await _grant(
        usage_client,
        principal,
        "grant-known",
        EntitlementResource(kind="modelApi", id="model-api-known"),
    )

    default = usage_client.get("/api/v1/me/usage")
    assert default.status_code == 200, default.text
    assert default.json()["period"] == "30d"
    assert len(default.json()["timeline"]) == 30

    for period, expected_days in [("7d", 7), ("90d", 90)]:
        response = usage_client.get("/api/v1/me/usage", params={"period": period})
        assert response.status_code == 200, response.text
        assert response.json()["period"] == period
        assert len(response.json()["timeline"]) == expected_days

    invalid = usage_client.get("/api/v1/me/usage", params={"period": "1y"})
    assert invalid.status_code == 422


async def test_usage_is_scoped_and_deterministic_per_caller(
    usage_client: TestClient,
) -> None:
    await _seed_resource_catalog(usage_client)
    local = await _principal(usage_client, "local-admin")
    other = await _principal(usage_client, "other-object")
    await _grant(
        usage_client,
        local,
        "grant-local",
        EntitlementResource(kind="modelApi", id="model-api-known"),
    )
    await _grant(
        usage_client,
        other,
        "grant-other",
        EntitlementResource(kind="modelApi", id="model-api-known"),
    )

    first = usage_client.get("/api/v1/me/usage", params={"period": "7d"}).json()
    second = usage_client.get("/api/v1/me/usage", params={"period": "7d"}).json()
    assert first["timeline"] == second["timeline"]
    assert [row["entitlementId"] for row in first["byResource"]] == ["grant-local"]

    usage_client.app.state.authenticator = _StubAuthenticator("other-object")
    other_response = usage_client.get("/api/v1/me/usage", params={"period": "7d"}).json()
    assert [row["entitlementId"] for row in other_response["byResource"]] == ["grant-other"]
    assert other_response["timeline"] != first["timeline"]


async def test_usage_shapes_costs_quotas_and_zero_rules(usage_client: TestClient) -> None:
    await _seed_resource_catalog(usage_client)
    principal = await _principal(usage_client)
    await _grant(
        usage_client,
        principal,
        "daily-tokens",
        EntitlementResource(kind="modelApi", id="model-api-known"),
        enforcement=_token_enforcement("Daily", 40_000),
    )
    await _grant(
        usage_client,
        principal,
        "weekly-requests",
        EntitlementResource(kind="modelApi", id="model-api-weekly"),
        enforcement=_request_enforcement("Weekly", 500),
    )
    await _grant(
        usage_client,
        principal,
        "monthly-product",
        EntitlementResource(kind="product", id="gold-product", scope_id="gateway-prod"),
        enforcement=_request_enforcement("Monthly", 800),
        enabled=False,
    )
    await _grant(
        usage_client,
        principal,
        "yearly-deployment",
        EntitlementResource(kind="modelDeployment", id="chat-prod", scope_id="endpoint-staging"),
        enforcement=_token_enforcement("Yearly", 200_000),
        created_days_ago=3,
    )
    await _grant(
        usage_client,
        principal,
        "hourly-mcp",
        EntitlementResource(kind="mcpServer", id="mcp-tickets"),
        enforcement=_request_enforcement("Hourly", 100),
        bound=False,
    )
    await _grant(
        usage_client,
        principal,
        "unknown-model",
        EntitlementResource(kind="modelApi", id="model-api-unknown"),
    )

    response = usage_client.get("/api/v1/me/usage", params={"period": "7d"})
    assert response.status_code == 200, response.text
    body = response.json()
    rows = {row["entitlementId"]: row for row in body["byResource"]}

    assert rows["daily-tokens"]["estimatedCost"] > 0
    assert rows["monthly-product"]["estimatedCost"] is None
    assert "Products bundle several APIs" in rows["monthly-product"]["costNote"]
    assert rows["unknown-model"]["estimatedCost"] is None
    assert "doesn't know which model" in rows["unknown-model"]["costNote"]
    assert rows["hourly-mcp"]["promptTokens"] is None
    assert rows["hourly-mcp"]["totalTokens"] is None
    assert rows["hourly-mcp"]["attribution"] == "simulated"
    assert rows["hourly-mcp"]["bound"] is False
    assert rows["monthly-product"]["requests"] == 0
    assert body["totals"]["costExcludedResources"] == 3
    assert body["totals"]["estimatedCost"] is not None

    timeline = body["timeline"]
    product_points = [p for p in timeline if p["entitlementId"] == "monthly-product"]
    assert all(point["requests"] == 0 for point in product_points)
    deployment_points = [p for p in timeline if p["entitlementId"] == "yearly-deployment"]
    assert [p["requests"] for p in deployment_points[:3]] == [0, 0, 0]
    assert any((p["totalTokens"] or 0) > 0 for p in deployment_points[3:])

    quota_by_period = {
        quota["period"]: quota
        for row in rows.values()
        for quota in row["quotas"]
        if quota["used"] is not None
    }
    assert quota_by_period["Hourly"]["windowStart"] == "2026-09-29T15:00:00Z"
    assert quota_by_period["Hourly"]["windowEnd"] == "2026-09-29T16:00:00Z"
    assert quota_by_period["Daily"]["windowStart"] == "2026-09-29T00:00:00Z"
    assert quota_by_period["Weekly"]["windowStart"] == "2026-09-28T00:00:00Z"
    assert quota_by_period["Monthly"]["windowStart"] == "2026-09-01T00:00:00Z"
    assert quota_by_period["Yearly"]["windowStart"] == "2026-01-01T00:00:00Z"
    for quota in quota_by_period.values():
        assert quota["used"] <= quota["limit"]
        assert quota["utilization"] <= 1

    daily_today = next(
        point
        for point in timeline
        if point["entitlementId"] == "daily-tokens" and point["date"] == "2026-09-29"
    )
    assert daily_today["totalTokens"] <= rows["daily-tokens"]["quotas"][0]["limit"]


async def test_usage_for_a_day_is_the_same_in_every_period(usage_client: TestClient) -> None:
    await _seed_resource_catalog(usage_client)
    principal = await _principal(usage_client)
    await _grant(
        usage_client,
        principal,
        "monthly-tokens",
        EntitlementResource(kind="modelApi", id="model-api-known"),
        enforcement=_token_enforcement("Monthly", 400_000),
        created_days_ago=120,
    )
    await _grant(
        usage_client,
        principal,
        "weekly-requests",
        EntitlementResource(kind="modelApi", id="model-api-weekly"),
        enforcement=_request_enforcement("Weekly", 300),
        created_days_ago=120,
    )

    reports = {
        period: usage_client.get("/api/v1/me/usage", params={"period": period}).json()
        for period in ("7d", "30d", "90d")
    }

    def points(period: str) -> dict[tuple[str, str], dict[str, Any]]:
        return {
            (point["entitlementId"], point["date"]): point
            for point in reports[period]["timeline"]
        }

    week, month, quarter = points("7d"), points("30d"), points("90d")
    assert week and all(quarter[key] == value for key, value in week.items())
    # 2026-08-31 opens the 30-day report but closes August, whose monthly quota caps the 90-day
    # report's whole month.
    assert ("monthly-tokens", "2026-08-31") in month
    assert all(quarter[key] == value for key, value in month.items())
    august = sum(
        point["totalTokens"]
        for (entitlement_id, day), point in quarter.items()
        if entitlement_id == "monthly-tokens" and day.startswith("2026-08")
    )
    assert 0 < august <= 400_000
    quotas = {
        period: [
            quota for row in report["byResource"] for quota in row["quotas"]
        ]
        for period, report in reports.items()
    }
    assert quotas["7d"] == quotas["30d"] == quotas["90d"]
    assert all(quota["used"] <= quota["limit"] for quota in quotas["7d"])


async def test_usage_shows_disabled_grants_only_where_nothing_enabled_covers_them(
    usage_client: TestClient,
) -> None:
    await _seed_resource_catalog(usage_client)
    principal = await _principal(usage_client)
    directory = usage_client.app.state.repository
    group = await directory.create_group(
        Group(id="group-builders", tenant_id=TENANT, name="Builders"), _audit("group")
    )
    await directory.create_membership(
        GroupMembership(
            id="membership-1", tenant_id=TENANT, group_id=group.id, principal_id=principal.id
        ),
        group,
        principal,
        _audit("membership"),
    )
    known = EntitlementResource(kind="modelApi", id="model-api-known")
    await _grant(usage_client, principal, "direct-disabled", known, enabled=False)
    await usage_client.app.state.entitlement_repository.save_entitlement(
        Entitlement(
            id="group-enabled",
            tenant_id=TENANT,
            created_at=NOW - timedelta(days=30),
            subject=EntitlementSubject(kind="group", id=group.id),
            resource=known,
            enabled=True,
        ),
        _audit("entitlement"),
    )
    await _grant(
        usage_client,
        principal,
        "only-disabled",
        EntitlementResource(kind="modelApi", id="model-api-weekly"),
        enabled=False,
    )

    body = usage_client.get("/api/v1/me/usage", params={"period": "7d"}).json()
    rows = {row["entitlementId"]: row for row in body["byResource"]}

    assert set(rows) == {"group-enabled", "only-disabled"}
    assert rows["group-enabled"]["via"] == "group"
    assert rows["group-enabled"]["viaGroupName"] == "Builders"
    assert rows["group-enabled"]["requests"] > 0
    assert rows["only-disabled"]["enabled"] is False
    assert rows["only-disabled"]["requests"] == 0
    assert rows["only-disabled"]["resourceSummary"]["displayName"] == "Weekly chat"
    access = usage_client.get("/api/v1/portal/entitlements").json()
    assert [item["entitlement"]["id"] for item in access] == ["group-enabled"]


class _FailingUsageSource:
    data_source = "logAnalytics"

    async def daily_usage(
        self,
        _actor: Actor,
        _entitlements: Sequence[object],
        *,
        start: object,
        end: object,
    ) -> Mapping[str, Mapping[object, DailyUsage] | None]:
        raise RuntimeError("telemetry unavailable")


async def test_real_source_failure_does_not_fall_back(settings: Settings) -> None:
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        _set_usage_service(client, _FailingUsageSource())
        await _seed_resource_catalog(client)
        principal = await _principal(client)
        await _grant(
            client,
            principal,
            "grant-known",
            EntitlementResource(kind="modelApi", id="model-api-known"),
        )

        response = client.get("/api/v1/me/usage")

    assert response.status_code == 500
    assert "simulated" not in response.text
