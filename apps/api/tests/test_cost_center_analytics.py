"""Spend and usage by cost center: analytics, the chargeback file, budgets, and the portal.

Built on ``test_cost``'s estate. Bob's grant and the Analysts group's grant on the provisioned
deployment are charged to Research; everything else stays on General. See ADR 0022.
"""

from __future__ import annotations

import csv
import io
import json

import pytest
from mosaic_api.cost_centers import CostCenter, CostCenterLimit, PooledQuota
from mosaic_api.domain import EntitlementResource, general_cost_center_id
from test_cost import (
    ALICE,
    ANALYSTS,
    BOB,
    CAROL,
    FEBRUARY_IDLE,
    MARCH_SO_FAR,
    TENANT,
    Harness,
    _audit,
    _roll_up,
    harness,
    seed,
    settings,
)

__all__ = ["harness", "settings"]

RESEARCH = "costCenter_research"
GENERAL = general_cost_center_id(TENANT)


async def _charge_research(harness: Harness) -> None:
    await seed(harness)
    await harness.state.cost_center_repository.create_cost_center(
        CostCenter(
            id=RESEARCH,
            tenant_id=TENANT,
            name="Research",
            code="RES",
            limits=[
                CostCenterLimit(
                    resource=EntitlementResource(kind="modelApi", id="model-api-reserved"),
                    pool=PooledQuota(monthly_tokens=1_000_000),
                )
            ],
        ),
        _audit("costCenter"),
    )
    entitlements = harness.state.entitlement_repository
    for grant_id in ("grant-bob", "grant-analysts"):
        current = await entitlements.get_entitlement(TENANT, grant_id)
        assert current is not None
        await entitlements.save_entitlement(
            current.model_copy(update={"cost_center_id": RESEARCH}), _audit("entitlement")
        )
    await _roll_up(harness)


async def test_the_filter_counts_only_the_cost_centers_grants(harness: Harness) -> None:
    await _charge_research(harness)

    research = harness.get("/api/v1/analytics/overview", range="30d", costCenterId=RESEARCH)

    # Bob's three calls and Carol's one through Analysts; idle February capacity is no one's.
    assert research["kpis"]["requests"] == 4
    assert research["kpis"]["cost"] == pytest.approx(MARCH_SO_FAR)
    assert any("cost center filter" in note for note in research["notes"])
    general = harness.get("/api/v1/analytics/overview", range="30d", costCenterId=GENERAL)
    # Alice's chat, the bot's mystery calls (unpriced) and her MCP calls; not the unknown key.
    assert general["kpis"]["cost"] == pytest.approx(35.0)
    everyone = harness.get("/api/v1/analytics/overview", range="30d")
    assert everyone["kpis"]["cost"] == pytest.approx(35.25 + MARCH_SO_FAR + FEBRUARY_IDLE)
    response = harness.client.get(
        "/api/v1/analytics/overview", params={"range": "30d", "costCenterId": "missing"}
    )
    assert response.status_code == 404


async def test_consumers_and_cost_list_each_cost_center(harness: Harness) -> None:
    await _charge_research(harness)

    consumers = harness.get("/api/v1/analytics/consumers", range="30d")
    rows = {row["code"]: row for row in consumers["costCenters"]}
    assert rows["RES"]["requests"] == 4
    assert rows["RES"]["grants"] == 2
    assert rows["RES"]["cost"] == pytest.approx(MARCH_SO_FAR)
    assert rows["general"]["cost"] == pytest.approx(35.0)
    grants = {row["entitlementId"]: row for row in consumers["grants"]}
    assert grants["grant-bob"]["costCenterCode"] == "RES"
    assert grants["grant-alice"]["costCenterCode"] == "general"

    cost = harness.get("/api/v1/analytics/cost", range="30d")
    labels = {row["label"]: row["cost"] for row in cost["costCenters"]}
    assert labels["Research"] == pytest.approx(MARCH_SO_FAR)
    assert labels["General"] == pytest.approx(35.0)


async def test_the_chargeback_names_each_rows_cost_center(harness: Harness) -> None:
    await _charge_research(harness)

    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )

    assert response.status_code == 200, response.text
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    header = reader.fieldnames or []
    at = header.index("Object ID")
    assert header[at + 1 : at + 3] == ["Cost center", "Cost center name"]
    centers = {
        (row["Month"], row["Charged to"]): (row["Cost center"], row["Cost center name"])
        for row in reader
    }
    assert centers[("2026-03", "Bob")] == ("RES", "Research")
    assert centers[("2026-03", "Analysts")] == ("RES", "Research")
    assert centers[("2026-03", "Alice")] == ("general", "General")
    # Calls no grant matched, and reserved capacity nobody called, belong to no cost center.
    assert centers[("2026-03", "Unattributed calls")] == ("", "")
    assert centers[("2026-02", "Reserved capacity with no calls")] == ("", "")

    filtered = harness.client.get(
        "/api/v1/analytics/export",
        params={"view": "chargeback", "range": "30d", "costCenterId": RESEARCH},
    )
    rows = csv.DictReader(io.StringIO(filtered.text.lstrip("\ufeff")))
    charged = {row["Charged to"] for row in rows}
    assert charged == {"Bob", "Analysts"}


async def test_a_cost_centers_spend_is_ready_for_budget_checks(harness: Harness) -> None:
    await _charge_research(harness)

    spend = await harness.state.analytics_service.cost_center_spend(TENANT, RESEARCH)

    assert spend is not None
    assert spend.month_to_date == pytest.approx(MARCH_SO_FAR)


async def test_a_member_sees_their_cost_centers_total_and_no_one_elses_use(
    harness: Harness,
) -> None:
    await _charge_research(harness)
    harness.sign_in(CAROL, ["User"], frozenset({ANALYSTS}))

    report = harness.get("/api/v1/me/usage", period="30d")

    [research] = report["costCenters"]
    assert research["costCenter"]["code"] == "RES"
    # Everyone's calls under Research this month: Bob's three and Carol's one.
    assert research["requests"] == 4
    assert research["totalTokens"] == 400_000
    [resource] = research["resources"]
    assert resource["resource"]["id"] == "model-api-reserved"
    assert resource["poolTokens"] == 1_000_000
    assert resource["utilization"] == pytest.approx(0.4)
    # Only totals: nothing names, counts or splits the people behind them.
    text = json.dumps(research)
    for identity in (BOB, "Bob", ALICE, "Alice", "grant-bob", "principal-"):
        assert identity not in text
    assert set(research) == {"costCenter", "monthStart", "requests", "totalTokens", "resources"}

    # Bob holds no grant under General, so he sees only Research.
    harness.sign_in(BOB, ["User"], frozenset())
    bob = harness.get("/api/v1/me/usage", period="30d")
    assert [item["costCenter"]["code"] for item in bob["costCenters"]] == ["RES"]


async def test_someone_whose_grant_is_off_no_longer_sees_its_cost_centers_total(
    harness: Harness,
) -> None:
    await _charge_research(harness)
    entitlements = harness.state.entitlement_repository
    current = await entitlements.get_entitlement(TENANT, "grant-bob")
    assert current is not None
    await entitlements.save_entitlement(
        current.model_copy(update={"enabled": False}), _audit("entitlement")
    )
    harness.sign_in(BOB, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    assert report["costCenters"] == []
