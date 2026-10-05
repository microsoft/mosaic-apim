"""Each cost share comes from costs before rounding, so shares add up even when cost is tiny.

Built on ``test_cost``'s estate, with three small calls on 18 March and nothing else. The
deployment that test_cost reserves capacity on is pay-as-you-go GPT-4o mini here, so it costs
nothing while nobody calls it, and Bob's grant is charged to Research. Alice's chat call costs
$0.0006, Bob's $0.00066, and Carol's, through the Analysts group, $0.00018: $0.00144 in all, which
the Cost tab shows as $0.0014.

Dividing by the rounded total made cost by model, by API, and by deployment add up to 102.9%.
Dividing callers' and cost centers' rounded costs by it made those add up to about 107%.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from mosaic_api.cost_centers import CostCenter
from test_cost import (
    ALICE,
    BOB,
    CAROL,
    ENDPOINT,
    TENANT,
    Harness,
    _audit,
    _call,
    _deployment,
    _roll_up,
    harness,
    seed,
    settings,
)

__all__ = ["harness", "settings"]

RESEARCH = "costCenter_research"
BREAKDOWNS = ("models", "apis", "deployments", "consumers", "costCenters")


async def under_a_cent(harness: Harness) -> Harness:
    await seed(harness)
    await harness.state.model_endpoint_repository.replace_observed_for_endpoint(
        TENANT,
        ENDPOINT,
        [
            _deployment("chat", "gpt-4o", "2024-11-20", "GlobalStandard", 450),
            _deployment("reserved", "gpt-4o-mini", "2024-07-18", "GlobalStandard", 10),
            _deployment("mystery", "contoso-llm", "1", "GlobalStandard", 1),
        ],
        "snapshot",
    )
    await harness.state.cost_center_repository.create_cost_center(
        CostCenter(id=RESEARCH, tenant_id=TENANT, name="Research", code="RES"),
        _audit("costCenter"),
    )
    entitlements = harness.state.entitlement_repository
    bob = await entitlements.get_entitlement(TENANT, "grant-bob")
    assert bob is not None
    await entitlements.save_entitlement(
        bob.model_copy(update={"cost_center_id": RESEARCH}), _audit("entitlement")
    )
    mini = {"model": "gpt-4o-mini"}
    harness.logs.calls = [
        # GPT-4o at $2.50 and $10 per million tokens, and GPT-4o mini at $0.15 and $0.60.
        _call(18, "chat", 120, 30, grant="k-alice", member=ALICE),
        replace(_call(18, "reserved", 2_000, 600, grant="k-bob", member=BOB), **mini),
        replace(_call(18, "reserved", 400, 200, grant="k-analysts", member=CAROL), **mini),
    ]
    await _roll_up(harness)
    return harness


def _shown(rows: list[dict[str, Any]], key: str = "label") -> dict[str, tuple[float, float]]:
    """Each row's cost and share, as the Cost tab shows them."""

    return {row[key]: (row["cost"], row["costShare"]) for row in rows}


async def test_each_share_is_of_the_total_before_rounding(harness: Harness) -> None:
    await under_a_cent(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d")

    assert report["cost"]["total"] == 0.0014
    # GPT-4o's $0.0006 and GPT-4o mini's $0.00084 are 5/12 and 7/12 of $0.00144. Of $0.0014,
    # they were 42.9% and 60%.
    assert _shown(report["models"]) == {"gpt-4o": (0.0006, 0.4167), "gpt-4o-mini": (0.0008, 0.5833)}
    assert _shown(report["apis"]) == {"Chat": (0.0006, 0.4167), "Reserved": (0.0008, 0.5833)}
    assert _shown(report["deployments"], "deploymentName") == {
        "chat": (0.0006, 0.4167),
        "reserved": (0.0008, 0.5833),
    }


async def test_a_callers_share_and_a_cost_centers_are_of_their_cost_before_rounding(
    harness: Harness,
) -> None:
    await under_a_cent(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d")

    # Bob's $0.00066 shows as $0.0007 and Carol's $0.00018 as $0.0002, but each share is of what
    # the calls cost: 11/24 and 1/8 of $0.00144.
    assert _shown(report["consumers"]) == {
        "Alice": (0.0006, 0.4167),
        "Bob": (0.0007, 0.4583),
        "Carol": (0.0002, 0.125),
    }
    # General carries Alice's grant and the Analysts group's: $0.00078.
    assert _shown(report["costCenters"]) == {
        "General": (0.0008, 0.5417),
        "Research": (0.0007, 0.4583),
    }


async def test_every_breakdown_adds_up_to_the_whole_to_within_rounding(harness: Harness) -> None:
    await under_a_cent(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d")

    # Every call here is a grant's and nothing is reserved, so each breakdown covers the whole
    # total. Each share is rounded on its own, to a hundredth of a percent.
    for name in BREAKDOWNS:
        shares = [row["costShare"] for row in report[name]]
        assert sum(shares) == pytest.approx(1, abs=0.00005 * len(shares)), name
