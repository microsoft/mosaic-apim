"""A model pool's usage and cost: calls placed on the members that served them, then priced.

Built on ``test_cost``'s harness. One pool, Northwind Fleet, offers one model, Contoso Chat
(gpt-4o), from three member deployments in three regions:

- ``chat-east``, pay-as-you-go in East US 2 at $2.50 and $10 per million tokens;
- ``chat-sweden``, pay-as-you-go in Sweden Central at $3.025 and $12.10;
- ``chat-ptu``, 10 provisioned PTUs in East US from 1 March, at $240 a day.

API Management picks the member, so the attribution trace can't name it. The gateway log can, by
the backend that served the call, the host and deployment its backend URL called, or a host or
deployment only one member has. One of Grace's calls went somewhere none of these name, so it's
left out of cost and reported, never priced at a guess. See ADR 0024.

Some tests add Dispatch tools, an MCP server that calls the model as its own application, the
Dispatch assistant, while it serves Ada and Hedy. Each person's share is priced at the member that
served it, and the portal names only the model. See ADR 0025.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from loganalytics_double import GatewayCall
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import (
    ApiShape,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    McpServer,
    ModelAccessSettings,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    PrincipalKind,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
)
from mosaic_api.model_pools import (
    ModelPool,
    PoolMember,
    PoolModel,
    backend_pool_name,
    member_backend_name,
    model_pool_id,
    pool_model_id,
)
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.services.analytics.cost import POOL_UNPLACED
from mosaic_api.services.telemetry import governed_apis
from mosaic_api.services.usage import (
    POOL_PART_UNPRICED_NOTE,
    POOL_UNPRICED_NOTE,
    UNKNOWN_POOL_MODEL,
)
from mosaic_api.usage_telemetry import PoolMembers, RolledUpApi, RolledUpMember
from test_cost import (
    GATEWAY,
    NOW,
    RESOURCE_ID,
    TENANT,
    Harness,
    _audit,
    _principal,
    _roll_up,
    harness,
    settings,
)

__all__ = ["harness", "settings"]

POOL_API = "northwind-fleet"
POOL_ID = model_pool_id(TENANT, GATEWAY, POOL_API)
MODEL_ID = pool_model_id(POOL_ID, "contoso-chat")
EAST = "endpoint-east"
SWEDEN = "endpoint-sweden"
RESERVED = "endpoint-reserved"
ADA = "ada-oid"
GRACE = "grace-oid"
LINUS = "linus-oid"
RESEARCH = "costCenter_research"
# Dispatch tools, whose application calls the model, and each MCP call's own reference.
DISPATCH = "mcp-dispatch"
ASSISTANT = "dispatch-assistant-oid"
HEDY = "hedy-oid"
OPERATIONS = "costCenter_operations"
ADA_DISPATCH = "aaaabbbb-cccc-dddd-eeee-ffff0000ada1"
HEDY_DISPATCH = "aaaabbbb-cccc-dddd-eeee-ffff0000bed1"

# Each member: its endpoint's name, region and host, and its deployment's name, SKU, capacity
# and creation day.
ENDPOINTS = {
    EAST: (
        "Contoso East",
        "eastus2",
        "contoso-east.openai.azure.com",
        "chat-east",
        "GlobalStandard",
        450,
        datetime(2026, 2, 1, tzinfo=UTC),
    ),
    SWEDEN: (
        "Contoso Sweden",
        "swedencentral",
        "contoso-sweden.openai.azure.com",
        "chat-sweden",
        "Standard",
        100,
        datetime(2026, 2, 1, tzinfo=UTC),
    ),
    RESERVED: (
        "Contoso Reserved",
        "eastus",
        "contoso-ptu.openai.azure.com",
        "chat-ptu",
        "GlobalProvisionedManaged",
        10,
        datetime(2026, 3, 1, tzinfo=UTC),
    ),
}
EAST_BACKEND = member_backend_name(POOL_API, MODEL_ID, EAST, "chat-east")
SWEDEN_BACKEND = member_backend_name(POOL_API, MODEL_ID, SWEDEN, "chat-sweden")
PTU_URL = "https://contoso-ptu.openai.azure.com/openai/v1/chat/completions"
SWEDEN_URL = "https://contoso-sweden.openai.azure.com/openai/deployments/chat-sweden/chat/completions"
ELSEWHERE_URL = "https://elsewhere.example.com/openai/deployments/gpt-4o/chat/completions"

# Ada's ten days on East at $3.50 a day, and 100,000 prompt tokens placed by deployment alone.
EAST_COST = 10 * 3.5 + 0.25
# Grace's one call on Sweden: a million prompt and 100,000 completion tokens.
SWEDEN_COST = 3.025 + 1.21
# The reserved deployment from 1 March: 18 days at $240. Ada sent a quarter of its 400,000 tokens.
RESERVED_COST = 18 * 240.0
ADA_COST = EAST_COST + RESERVED_COST / 4
GRACE_COST = SWEDEN_COST + RESERVED_COST * 3 / 4
TOTAL = EAST_COST + SWEDEN_COST + RESERVED_COST
# The Dispatch assistant's calls: Ada's share on East, Hedy's on Sweden, and one of its own on East.
ADA_BEHALF_COST = (200_000 * 2.5 + 20_000 * 10) / 1e6
HEDY_BEHALF_COST = (100_000 * 3.025 + 10_000 * 12.1) / 1e6
ASSISTANT_OWN_COST = (100_000 * 2.5 + 10_000 * 10) / 1e6


def _pool_model(endpoint_ids: tuple[str, ...] = (EAST, SWEDEN, RESERVED)) -> PoolModel:
    return PoolModel(
        id=MODEL_ID,
        public_name="contoso-chat",
        display_name="Contoso Chat",
        model_name="gpt-4o",
        model_format="OpenAI",
        backend_pool_name=backend_pool_name(POOL_API, MODEL_ID, "contoso-chat"),
        members=[
            PoolMember(
                model_endpoint_id=endpoint_id,
                deployment_name=ENDPOINTS[endpoint_id][3],
                backend_name=member_backend_name(
                    POOL_API, MODEL_ID, endpoint_id, ENDPOINTS[endpoint_id][3]
                ),
            )
            for endpoint_id in endpoint_ids
        ],
    )


def _pool(*, applied: bool = True, api_name: str = POOL_API) -> ModelPool:
    pool_id = model_pool_id(TENANT, GATEWAY, api_name)
    return ModelPool(
        id=pool_id,
        tenant_id=TENANT,
        gateway_id=GATEWAY,
        display_name="Northwind Fleet",
        api_name=api_name,
        api_path=api_name,
        fragment_name=f"{api_name}-policy",
        product_name=api_name,
        subscription_name=f"{api_name}-key",
        api_shape=ApiShape.AZURE_OPENAI,
        vendor="OpenAI",
        models=[_pool_model()],
        status=PublicationStatus.PUBLISHED if applied else PublicationStatus.DRAFT,
        resources=(
            [
                PublishedResource(
                    kind=PublishedResourceKind.API,
                    name=api_name,
                    resource_id=f"{RESOURCE_ID}/apis/{api_name}",
                    created_by_mosaic=True,
                )
            ]
            if applied
            else []
        ),
        governed_access=ModelAccessSettings(),
        access_state="applied" if applied else "pending",
        applied_model_ids=[MODEL_ID] if applied else [],
    )


async def _grant(
    harness: Harness,
    grant_id: str,
    principal_id: str,
    key: str,
    *,
    kind: str = "user",
    resource: EntitlementResource | None = None,
    cost_center_id: str = "",
) -> None:
    await harness.state.entitlement_repository.save_entitlement(
        Entitlement(
            id=grant_id,
            tenant_id=TENANT,
            created_at=NOW - timedelta(days=90),
            subject=EntitlementSubject.model_validate({"kind": kind, "id": principal_id}),
            resource=resource
            or EntitlementResource(kind="poolModel", id=MODEL_ID, scope_id=POOL_ID),
            binding=EntitlementBinding(
                gateway_id=GATEWAY, attribution_key=key, source=BindingSource.ORCHESTRATED
            ),
            cost_center_id=cost_center_id,
        ),
        _audit("entitlement"),
    )


def _call(
    day: int, key: str, member: str, prompt: int, completion: int, *, hour: int = 10, **backend: Any
) -> GatewayCall:
    return GatewayCall(
        time=datetime(2026, 3, day, hour, tzinfo=UTC),
        api=POOL_API,
        prompt_tokens=prompt,
        completion_tokens=completion,
        model="gpt-4o",
        grant=key,
        member=member,
        **backend,
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
    for endpoint_id, (name, region, host, deployment, sku, capacity, created) in ENDPOINTS.items():
        await state.model_endpoint_repository.save_endpoint(
            ModelEndpoint(
                id=endpoint_id,
                tenant_id=TENANT,
                name=name,
                provider=ModelProvider.AZURE_OPENAI,
                endpoint=f"https://{host}",
                environment="production",
                capabilities=ModelEndpointCapabilities(location=region),
            ),
            _audit("endpoint"),
        )
        await state.model_endpoint_repository.replace_observed_for_endpoint(
            TENANT,
            endpoint_id,
            [
                ObservedModelDeployment(
                    id=f"obs-{deployment}",
                    tenant_id=TENANT,
                    endpoint_id=endpoint_id,
                    snapshot_id="snapshot",
                    deployment_name=deployment,
                    model_name="gpt-4o",
                    model_version="2024-11-20",
                    model_format="OpenAI",
                    sku_name=sku,
                    sku_capacity=capacity,
                    deployed_at=created,
                )
            ],
            "snapshot",
        )
    await state.gateway_repository.save_model_pool(_pool(), _audit("pool"))

    ada = await _principal(harness, ADA, PrincipalKind.USER, "Ada")
    grace = await _principal(harness, GRACE, PrincipalKind.USER, "Grace")
    linus = await _principal(harness, LINUS, PrincipalKind.USER, "Linus")
    await _grant(harness, "grant-ada", ada, "k-ada")
    await _grant(harness, "grant-grace", grace, "k-grace")
    await _grant(harness, "grant-linus", linus, "k-linus")

    calls = [
        # Named by the backend that served them.
        *(
            _call(
                day,
                "k-ada",
                ADA,
                1_000_000,
                100_000,
                deployment="chat-east",
                backend_id=EAST_BACKEND,
            )
            for day in range(9, 19)
        ),
        # No backend at all, but only one member has a deployment called chat-east.
        _call(18, "k-ada", ADA, 100_000, 0, hour=12, deployment="chat-east"),
        # The v1 route names no deployment, but only one member's backend calls this host.
        _call(18, "k-ada", ADA, 80_000, 20_000, deployment="gpt-4o", backend_url=PTU_URL),
        # The host and deployment of the backend URL.
        _call(17, "k-grace", GRACE, 1_000_000, 100_000, hour=9, backend_url=SWEDEN_URL),
        *(
            _call(day, "k-grace", GRACE, 80_000, 20_000, deployment="gpt-4o", backend_url=PTU_URL)
            for day in (16, 17, 18)
        ),
        # A backend MOSAIC didn't write, calling a host no member has: it's left unplaced.
        _call(
            18,
            "k-grace",
            GRACE,
            50_000,
            50_000,
            hour=11,
            deployment="gpt-4o",
            backend_id="legacy-backend",
            backend_url=ELSEWHERE_URL,
        ),
    ]
    harness.logs.calls = calls


async def rolled_up(harness: Harness, *extra: GatewayCall) -> Harness:
    await seed(harness)
    harness.logs.calls.extend(extra)
    await _roll_up(harness)
    return harness


def _at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 3, 18, hour, minute, second, tzinfo=UTC)


def _dispatch(at: datetime, key: str, member: str, reference: str) -> GatewayCall:
    return GatewayCall(
        time=at,
        api="dispatch",
        grant=key,
        member=member,
        client_app="vscode",
        reference=reference,
        model_caller=ASSISTANT,
        total_time_ms=60_000,
    )


def _assistant(
    at: datetime,
    prompt: int,
    completion: int,
    deployment: str,
    backend: str,
    reference: str = "",
) -> GatewayCall:
    return GatewayCall(
        time=at,
        api=POOL_API,
        grant="k-assistant",
        client_app="dispatch-assistant-app",
        reference=reference,
        prompt_tokens=prompt,
        completion_tokens=completion,
        deployment=deployment,
        model="gpt-4o",
        backend_id=backend,
    )


async def on_behalf(harness: Harness) -> Harness:
    """The pool, and the Dispatch assistant's calls to its model for Ada, Hedy, and itself."""

    await seed(harness)
    state = harness.state
    await state.cost_center_repository.create_cost_center(
        CostCenter(id=OPERATIONS, tenant_id=TENANT, name="Operations", code="OPS"),
        _audit("costCenter"),
    )
    await state.gateway_repository.save_mcp_server(
        McpServer(
            id=DISPATCH,
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            api_name="dispatch",
            display_name="Dispatch tools",
            path="dispatch",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("mcp"),
    )
    assistant = await _principal(
        harness, ASSISTANT, PrincipalKind.SERVICE_PRINCIPAL, "Dispatch assistant"
    )
    hedy = await _principal(harness, HEDY, PrincipalKind.USER, "Hedy")
    # The assistant's grant is on the pool's model; Ada's and Hedy's are on the MCP server only.
    await _grant(
        harness,
        "grant-assistant",
        assistant,
        "k-assistant",
        kind="application",
        cost_center_id=OPERATIONS,
    )
    dispatch = EntitlementResource(kind="mcpServer", id=DISPATCH)
    await _grant(
        harness, "grant-ada-dispatch", f"principal-{ADA}", "k-ada-dispatch", resource=dispatch
    )
    await _grant(harness, "grant-hedy-dispatch", hedy, "k-hedy-dispatch", resource=dispatch)
    harness.logs.calls.extend(
        [
            _dispatch(_at(11), "k-ada-dispatch", ADA, ADA_DISPATCH),
            _assistant(_at(11, 0, 30), 200_000, 20_000, "chat-east", EAST_BACKEND, ADA_DISPATCH),
            _dispatch(_at(11, 30), "k-hedy-dispatch", HEDY, HEDY_DISPATCH),
            _assistant(
                _at(11, 30, 20), 100_000, 10_000, "chat-sweden", SWEDEN_BACKEND, HEDY_DISPATCH
            ),
            # The assistant's own call, made for nobody.
            _assistant(_at(12, 30), 100_000, 10_000, "chat-east", EAST_BACKEND),
        ]
    )
    await _roll_up(harness)
    return harness


def _chargeback(harness: Harness) -> list[dict[str, str]]:
    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))


async def test_the_overview_prices_each_call_at_the_member_that_served_it(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/overview", range="30d")

    assert report["kpis"]["cost"] == pytest.approx(TOTAL)
    cost = report["cost"]
    assert cost["total"] == pytest.approx(TOTAL)
    assert cost["reserved"] == pytest.approx(RESERVED_COST)
    # Grace's call that no member is known to have served is left out and said so, never zero.
    assert (cost["unpricedTokens"], cost["unpricedRequests"], cost["unpricedItems"]) == (
        100_000,
        1,
        1,
    )
    [left_out] = cost["unpriced"]
    assert (left_out["kind"], left_out["label"], left_out["detail"]) == (
        "api",
        "Northwind Fleet",
        "Production gateway",
    )
    assert (left_out["reason"], left_out["message"]) == (
        POOL_UNPLACED.reason,
        POOL_UNPLACED.message,
    )
    assert (left_out["requests"], left_out["totalTokens"]) == (1, 100_000)
    callers = {row["label"]: row["cost"] for row in report["topCallers"]}
    assert callers["Ada"] == pytest.approx(ADA_COST)
    assert callers["Grace"] == pytest.approx(GRACE_COST)


async def test_each_member_deployment_carries_its_own_cost(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/models", range="30d")

    deployments = {row["deploymentName"]: row["cost"] for row in report["deployments"]}
    assert deployments == pytest.approx(
        {"chat-east": EAST_COST, "chat-sweden": SWEDEN_COST, "chat-ptu": RESERVED_COST}
    )
    apis = {row["apiName"]: row["cost"] for row in report["apis"]}
    assert apis[POOL_API] == pytest.approx(TOTAL)
    [gateway] = report["gateways"]
    assert gateway["cost"] == pytest.approx(TOTAL)
    assert report["cost"]["total"] == pytest.approx(TOTAL)


async def test_the_cost_report_prices_each_member_by_its_region_and_capacity(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d")

    rows = {row["deploymentName"]: row for row in report["deployments"]}
    east = rows["chat-east"]
    assert (east["pricing"], east["inputPerMillion"], east["outputPerMillion"]) == (
        "tokens",
        2.5,
        10.0,
    )
    # A regional deployment in Sweden costs more than a global one.
    sweden = rows["chat-sweden"]
    assert (sweden["pricing"], sweden["inputPerMillion"], sweden["outputPerMillion"]) == (
        "tokens",
        3.025,
        12.1,
    )
    assert sweden["cost"] == pytest.approx(SWEDEN_COST)
    reserved = rows["chat-ptu"]
    assert (reserved["pricing"], reserved["capacity"], reserved["ptuHourly"]) == (
        "provisioned",
        10,
        1.0,
    )
    assert reserved["monthCost"] == pytest.approx(240.0 * 31)
    assert reserved["cost"] == pytest.approx(RESERVED_COST)
    # Created on 1 March, so the window's February days cost nothing, idle or not.
    assert not reserved["idleCost"]
    consumers = {row["label"]: row["cost"] for row in report["consumers"]}
    assert consumers["Ada"] == pytest.approx(ADA_COST)
    assert consumers["Grace"] == pytest.approx(GRACE_COST)


async def test_grants_and_the_chargeback_file_reconcile(harness: Harness) -> None:
    await rolled_up(harness)

    consumers = harness.get("/api/v1/analytics/consumers", range="30d")

    people = {row["label"]: row["cost"] for row in consumers["people"]}
    assert people["Ada"] == pytest.approx(ADA_COST)
    assert people["Grace"] == pytest.approx(GRACE_COST)
    grants = {row["entitlementId"]: row["cost"] for row in consumers["grants"]}
    assert grants["grant-ada"] == pytest.approx(ADA_COST)
    assert grants["grant-grace"] == pytest.approx(GRACE_COST)
    assert consumers["cost"]["unpricedTokens"] == 100_000

    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )
    assert response.status_code == 200, response.text
    rows = list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))
    charged = {
        (row["Charged to"], row["Model"], row["Deployment"]): (row["Cost (USD)"], row["Priced"])
        for row in rows
    }
    assert float(charged[("Ada", "gpt-4o", "chat-east")][0]) == pytest.approx(EAST_COST)
    assert float(charged[("Ada", "gpt-4o", "chat-ptu")][0]) == pytest.approx(RESERVED_COST / 4)
    assert float(charged[("Grace", "gpt-4o", "chat-sweden")][0]) == pytest.approx(SWEDEN_COST)
    assert float(charged[("Grace", "gpt-4o", "chat-ptu")][0]) == pytest.approx(
        RESERVED_COST * 3 / 4
    )
    # The unplaced call is charged to Grace, but at no price, and without a deployment to name.
    assert charged[("Grace", "Unknown model", "")] == ("", "no")
    assert sum(float(row["Cost (USD)"] or 0) for row in rows) == pytest.approx(TOTAL, abs=1e-3)


async def test_a_members_calls_stay_priced_after_it_leaves_the_pool(harness: Harness) -> None:
    await rolled_up(harness)
    await harness.state.gateway_repository.save_model_pool(
        _pool().model_copy(update={"models": [_pool_model((EAST, RESERVED))]}), _audit("pool")
    )
    harness.now = NOW + timedelta(hours=1)
    await _roll_up(harness)

    report = harness.get("/api/v1/analytics/models", range="30d")

    deployments = {row["deploymentName"]: row["cost"] for row in report["deployments"]}
    assert deployments["chat-sweden"] == pytest.approx(SWEDEN_COST)
    harness.sign_in(GRACE, ["User"], frozenset())
    [row] = harness.get("/api/v1/me/usage", period="30d")["byResource"]
    assert row["estimatedCost"] == pytest.approx(GRACE_COST)


async def test_a_person_sees_their_cost_of_the_model_and_nothing_of_the_pool(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    harness.sign_in(ADA, ["User"], frozenset())
    ada = harness.get("/api/v1/me/usage", period="30d")
    [row] = ada["byResource"]
    assert row["entitlementId"] == "grant-ada"
    assert (row["resource"]["kind"], row["model"]) == ("poolModel", "gpt-4o")
    assert row["estimatedCost"] == pytest.approx(ADA_COST)
    assert row["costNote"] is None
    assert ada["totals"]["estimatedCost"] == pytest.approx(ADA_COST)

    harness.sign_in(GRACE, ["User"], frozenset())
    grace = harness.get("/api/v1/me/usage", period="30d")
    [row] = grace["byResource"]
    assert row["estimatedCost"] == pytest.approx(GRACE_COST)
    assert row["costNote"] == POOL_PART_UNPRICED_NOTE

    # Linus may call the model but hasn't, which costs nothing rather than an unknown amount.
    harness.sign_in(LINUS, ["User"], frozenset())
    linus = harness.get("/api/v1/me/usage", period="30d")
    [row] = linus["byResource"]
    assert row["estimatedCost"] == 0.0
    assert row["costNote"] is None

    for report in (ada, grace, linus):
        _assert_hides_the_pool(report)


async def test_calls_no_member_is_known_to_have_served_are_never_priced(
    harness: Harness,
) -> None:
    await rolled_up(
        harness,
        _call(
            18,
            "k-linus",
            LINUS,
            30_000,
            20_000,
            hour=13,
            deployment="gpt-4o",
            backend_url=ELSEWHERE_URL,
        ),
    )
    harness.sign_in(LINUS, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["byResource"]
    assert row["estimatedCost"] is None
    assert row["costNote"] == POOL_UNPRICED_NOTE
    assert report["totals"]["estimatedCost"] is None
    assert report["totals"]["costExcludedResources"] == 1
    _assert_hides_the_pool(report)


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _assert_hides_the_pool(report: Any) -> None:
    """The portal never names the pool, its members, their endpoints, or where they run.

    Its IDs name nothing, so they may appear, alone or in the keys built from them.
    """

    hidden = [
        "northwind",
        "contoso east",
        "contoso sweden",
        "contoso reserved",
        "openai.azure.com",
        "example.com",
        "eastus",
        "swedencentral",
        "chat-east",
        "chat-sweden",
        "chat-ptu",
        "legacy-backend",
        *(endpoint_id.casefold() for endpoint_id in ENDPOINTS),
    ]
    for text in _strings(report):
        folded = text.casefold()
        assert not any(word in folded for word in hidden), text
        for opaque in (POOL_ID, MODEL_ID, "poolModel"):
            folded = folded.replace(opaque.casefold(), "")
        assert "pool" not in folded, text


async def test_the_unpriced_list_names_a_pools_unplaced_calls(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/pricing/unpriced")

    # Every member's deployment has a price, so the pool's unplaced calls are all that's left.
    [row] = report["rows"]
    assert (row["kind"], row["key"], row["label"], row["gatewayName"]) == (
        "api",
        f"{GATEWAY}/{POOL_API}",
        "Northwind Fleet",
        "Production gateway",
    )
    assert (row["reason"], row["requests"], row["totalTokens"]) == (
        "unknownDeployment",
        1,
        100_000,
    )


async def test_a_cost_center_and_its_budget_carry_a_pool_grants_cost(harness: Harness) -> None:
    await seed(harness)
    await harness.state.cost_center_repository.create_cost_center(
        CostCenter(id=RESEARCH, tenant_id=TENANT, name="Research", code="RES", limits=[]),
        _audit("costCenter"),
    )
    entitlements = harness.state.entitlement_repository
    grace = await entitlements.get_entitlement(TENANT, "grant-grace")
    assert grace is not None
    await entitlements.save_entitlement(
        grace.model_copy(update={"cost_center_id": RESEARCH}), _audit("entitlement")
    )
    await _roll_up(harness)

    research = harness.get("/api/v1/analytics/overview", range="30d", costCenterId=RESEARCH)

    assert research["kpis"]["cost"] == pytest.approx(GRACE_COST)
    assert research["cost"]["unpricedTokens"] == 100_000
    analytics = harness.state.analytics_service
    spend = await analytics.cost_center_spend(TENANT, RESEARCH)
    assert spend.month_to_date == pytest.approx(GRACE_COST)
    budgets = await analytics.budget_spend(TENANT, [RESEARCH])
    assert budgets.cost_centers[RESEARCH].month_to_date == pytest.approx(GRACE_COST)


async def test_only_a_pool_whose_api_is_on_the_gateway_is_rolled_up(harness: Harness) -> None:
    await seed(harness)
    await harness.state.gateway_repository.save_model_pool(
        _pool(applied=False, api_name="draft-fleet"), _audit("pool")
    )

    governed = await governed_apis(
        harness.state.gateway_repository,
        TENANT,
        endpoint_repository=harness.state.model_endpoint_repository,
    )

    [pool] = [api for api in governed[GATEWAY] if api.kind == "pool"]
    assert (pool.api_name, pool.resource_id, pool.display_name) == (
        POOL_API,
        POOL_ID,
        "Northwind Fleet",
    )
    hosts = {member.deployment_name: member.host for member in pool.members}
    assert hosts == {
        "chat-east": "contoso-east.openai.azure.com",
        "chat-sweden": "contoso-sweden.openai.azure.com",
        "chat-ptu": "contoso-ptu.openai.azure.com",
    }
    assert {member.backend_name for member in pool.members} == {
        member.backend_name.casefold() for member in _pool_model().members
    }


def _fleet() -> PoolMembers:
    def member(endpoint_id: str, deployment: str, backend: str, host: str) -> RolledUpMember:
        return RolledUpMember(
            model_endpoint_id=endpoint_id,
            deployment_name=deployment,
            backend_name=backend,
            host=host,
        )

    return PoolMembers.of(
        RolledUpApi(
            api_name="fleet",
            resource_id="modelPool_fleet",
            kind="pool",
            display_name="Fleet",
            members=[
                member("east", "chat", "fleet-chat-east", "east.example.com"),
                member("west", "Chat", "Fleet-Chat-West", "west.example.com"),
                member("shared", "chat-a", "fleet-chat-a", "shared.example.com"),
                member("shared", "chat-b", "fleet-chat-b", "shared.example.com"),
                member("unknown", "chat-c", "fleet-chat-c", ""),
            ],
            first_seen_at=NOW,
        )
    )


@pytest.mark.parametrize(
    ("backend", "host", "deployment", "expected"),
    [
        # The backend MOSAIC wrote for one member names it, whatever else the call says.
        ("FLEET-CHAT-WEST", "east.example.com", "chat-a", "west/Chat"),
        # Then the host and deployment of the backend URL.
        ("legacy", "shared.example.com", "CHAT-B", "shared/chat-b"),
        # A host only one member's backend calls, whatever deployment the call names.
        ("", "east.example.com", "gpt-4o", "east/chat"),
        ("", "east.example.com", "chat-a", "east/chat"),
        # A host several members share needs the deployment too.
        ("", "shared.example.com", "gpt-4o", None),
        # With no known host, a deployment only one member has.
        ("", "", "chat-c", "unknown/chat-c"),
        ("", "elsewhere.example.com", "chat-a", "shared/chat-a"),
        # Two members have a deployment called chat, so it names neither.
        ("", "", "chat", None),
        ("", "", "", None),
    ],
)
def test_a_call_is_placed_on_a_member_only_when_its_log_says_which(
    backend: str, host: str, deployment: str, expected: str | None
) -> None:
    assert _fleet().member_for(backend, host, deployment) == expected


# -- Model use through MCP servers -----------------------------------------------------------


async def test_each_persons_share_of_an_applications_pool_calls_is_priced_where_it_ran(
    harness: Harness,
) -> None:
    await on_behalf(harness)

    report = harness.get("/api/v1/analytics/consumers", range="30d")

    rows = {row["personLabel"]: row for row in report["onBehalf"]}
    assert set(rows) == {"Ada", "Hedy"}
    ada = rows["Ada"]
    assert (ada["mcpLabel"], ada["mcpServerId"], ada["applicationLabel"]) == (
        "Dispatch tools",
        DISPATCH,
        "Dispatch assistant",
    )
    assert (ada["requests"], ada["totalTokens"]) == (1, 220_000)
    # Ada's share ran in East US 2, and Hedy's in Sweden Central, which costs more.
    assert ada["cost"] == pytest.approx(ADA_BEHALF_COST)
    assert rows["Hedy"]["cost"] == pytest.approx(HEDY_BEHALF_COST)
    # Ada's own calls are still hers alone, and the application's grant carries all of its own.
    people = {row["label"]: row["cost"] for row in report["people"]}
    assert people["Ada"] == pytest.approx(ADA_COST)
    grants = {row["entitlementId"]: row["cost"] for row in report["grants"]}
    assert grants["grant-assistant"] == pytest.approx(
        ADA_BEHALF_COST + HEDY_BEHALF_COST + ASSISTANT_OWN_COST
    )


async def test_the_chargeback_splits_a_pool_grant_by_person_and_member(harness: Harness) -> None:
    await on_behalf(harness)

    split = _chargeback(harness)

    mine = [row for row in split if row["Charged to"] == "Dispatch assistant"]
    for row in mine:
        assert (row["Kind"], row["Object ID"], row["Model"]) == ("application", ASSISTANT, "gpt-4o")
        assert (row["Cost center"], row["Cost center name"]) == ("OPS", "Operations")
        assert row["Priced"] == "yes"
    parts = {(row["Deployment"], row["On behalf of"]): row for row in mine}
    assert set(parts) == {("chat-east", ""), ("chat-east", "Ada"), ("chat-sweden", "Hedy")}
    own = parts["chat-east", ""]
    assert (own["On behalf of object ID"], own["Requests"], own["Total tokens"]) == (
        "",
        "1",
        "110000",
    )
    assert float(own["Cost (USD)"]) == pytest.approx(ASSISTANT_OWN_COST)
    ada = parts["chat-east", "Ada"]
    assert (ada["On behalf of object ID"], ada["Requests"], ada["Total tokens"]) == (
        ADA,
        "1",
        "220000",
    )
    assert float(ada["Cost (USD)"]) == pytest.approx(ADA_BEHALF_COST)
    hedy = parts["chat-sweden", "Hedy"]
    assert (hedy["On behalf of object ID"], hedy["Total tokens"]) == (HEDY, "110000")
    assert float(hedy["Cost (USD)"]) == pytest.approx(HEDY_BEHALF_COST)
    assert all(row["On behalf of"] == "" for row in split if row not in mine)

    # The parts add up exactly to the rows the file had before anyone's share was split out.
    summaries = harness.state.usage_rollup_repository._summaries
    for key, summary in list(summaries.items()):
        if summary.dimension in {"onBehalf", "onBehalfUnresolved"}:
            del summaries[key]
    whole = _chargeback(harness)
    assert len(split) == len(whole) + 1
    identity = ("Month", "Charged to", "Object ID", "Cost center", "Model", "Deployment")
    columns = ("Requests", "Prompt tokens", "Completion tokens", "Total tokens", "Cost (USD)")
    sums: dict[tuple[str, ...], list[Decimal]] = defaultdict(
        lambda: [Decimal(0) for _ in columns]
    )
    for row in split:
        total = sums[tuple(row[name] for name in identity)]
        for index, name in enumerate(columns):
            total[index] += Decimal(row[name] or "0")
    for row in whole:
        assert sums[tuple(row[name] for name in identity)] == [
            Decimal(row[name] or "0") for name in columns
        ], row["Charged to"]


async def test_a_person_sees_their_share_by_the_models_name_and_nothing_of_the_pool(
    harness: Harness,
) -> None:
    await on_behalf(harness)
    harness.sign_in(ADA, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    assert (row["mcpServer"]["id"], row["mcpServer"]["displayName"]) == (
        DISPATCH,
        "Dispatch tools",
    )
    resource = row["resource"]
    assert (resource["kind"], resource["id"], resource["scopeId"]) == (
        "poolModel",
        MODEL_ID,
        POOL_ID,
    )
    assert (resource["displayName"], resource["available"]) == ("Contoso Chat", True)
    assert row["model"] == "gpt-4o"
    assert (row["requests"], row["totalTokens"]) == (1, 220_000)
    # Priced at the member that served it, as her own calls to the model are.
    assert row["estimatedCost"] == pytest.approx(ADA_BEHALF_COST)
    assert row["costNote"] is None
    # Charged to the application's grant's cost center, so none of it is in her own figures.
    assert row["costCenter"] == {"id": OPERATIONS, "name": "Operations", "code": "OPS"}
    assert report["totals"]["estimatedCost"] == pytest.approx(ADA_COST)
    _assert_hides_the_pool(report)


async def test_someone_with_no_grant_on_the_pool_sees_their_share_priced(
    harness: Harness,
) -> None:
    await on_behalf(harness)
    harness.sign_in(HEDY, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    assert (row["resource"]["displayName"], row["model"]) == ("Contoso Chat", "gpt-4o")
    # Priced in Sweden Central, where it ran, though Hedy may call none of the pool's models.
    assert row["estimatedCost"] == pytest.approx(HEDY_BEHALF_COST)
    assert row["costNote"] is None
    assert report["totals"]["totalTokens"] == 0
    assert report["totals"]["estimatedCost"] is None
    _assert_hides_the_pool(report)


async def test_a_share_of_a_pool_model_thats_gone_stays_priced_and_unnamed(
    harness: Harness,
) -> None:
    await on_behalf(harness)
    await harness.state.gateway_repository.save_model_pool(
        _pool().model_copy(update={"models": []}), _audit("pool")
    )
    harness.sign_in(HEDY, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    resource = row["resource"]
    assert (resource["displayName"], resource["available"]) == (UNKNOWN_POOL_MODEL, False)
    assert row["model"] is None
    assert row["estimatedCost"] == pytest.approx(HEDY_BEHALF_COST)
    _assert_hides_the_pool(report)


async def test_a_share_mosaic_cant_follow_to_a_grant_never_names_the_pool(
    harness: Harness,
) -> None:
    await on_behalf(harness)
    records = harness.state.usage_rollup_repository._records
    for key, record in list(records.items()):
        if record.entitlement_id == "grant-assistant":
            del records[key]
    harness.sign_in(HEDY, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    # Without the application's grant, MOSAIC can't say which of the pool's models it called.
    assert (row["resource"]["kind"], row["resource"]["displayName"]) == (
        "poolModel",
        UNKNOWN_POOL_MODEL,
    )
    assert row["estimatedCost"] == pytest.approx(HEDY_BEHALF_COST)
    _assert_hides_the_pool(report)
