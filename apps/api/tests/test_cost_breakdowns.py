"""Calls the model never served cost nothing, and every cost breakdown adds up to the total.

Built on ``test_cost``'s estate. On 18 March the gateway refuses some of Alice's chat calls under
her token limit, so the LLM log holds its estimate of their prompts, and the chat deployment
throttles one more itself. The LLM log also names no model for one call the model served on each
deployment: priced, provisioned, and unpriced, and on an API MOSAIC only adopted, whose deployment
it doesn't know. See ADR 0019.
"""

from __future__ import annotations

import csv
import io
from dataclasses import replace
from typing import Any

import pytest
from loganalytics_double import GatewayCall
from mosaic_api.domain import ModelApi, general_cost_center_id
from test_cost import (
    ALICE,
    BOB,
    CHAT_COST,
    FEBRUARY_IDLE,
    GATEWAY,
    MARCH_SO_FAR,
    TENANT,
    Harness,
    _audit,
    _call,
    _roll_up,
    harness,
    seed,
    settings,
)

__all__ = ["harness", "settings"]

GENERAL = general_cost_center_id(TENANT)
# Where the gateway log says a token limit refused a call before it reached the model.
TOKEN_LIMIT = "token-limit-process-request-handler"
# The chat call the LLM log named no model for: 200,000 prompt tokens at $2.50 a million and
# 50,000 completion tokens at $10.
UNNAMED_CHAT_COST = 0.5 + 0.5
# Alice's ten days, the unknown key's call, and her unnamed call.
CHAT_TOKENS = 10 * 1_100_000 + 100_000 + 250_000
# Bob's three calls, Carol's one, and Bob's unnamed call.
RESERVED_TOKENS = 4 * 100_000 + 50_000
# The bot's two calls and its unnamed one, which nothing prices, and the adopted API's call.
MYSTERY_TOKENS = 3 * 2_000
LEGACY_TOKENS = 4_000
TOKENS = CHAT_TOKENS + RESERVED_TOKENS + MYSTERY_TOKENS + LEGACY_TOKENS
CALLED = CHAT_COST + UNNAMED_CHAT_COST + MARCH_SO_FAR
TOTAL = CALLED + FEBRUARY_IDLE


def _logged(
    api: str, prompt: int, completion: int, *, hour: int = 10, **logged: Any
) -> GatewayCall:
    """A call on 18 March, with ``logged`` as the gateway and LLM logs record it."""

    call = _call(18, api, prompt, completion)
    return replace(call, time=call.time.replace(hour=hour), **logged)


async def refused_and_unnamed(harness: Harness) -> Harness:
    await seed(harness)
    await harness.state.gateway_repository.save_model_api(
        ModelApi(
            id="model-api-legacy",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            api_name="legacy",
            display_name="Legacy",
            path="legacy",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("model-api"),
    )
    alice = {"grant": "k-alice", "member": ALICE}
    refused = {"backend_code": 0, "model": None, "last_error_source": TOKEN_LIMIT}
    harness.logs.calls.extend(
        [
            # Twice over her tokens a minute, and once over her token quota.
            _logged("chat", 11, 0, response_code=429, **refused, **alice),
            _logged("chat", 11, 0, response_code=429, **refused, **alice),
            _logged("chat", 11, 0, response_code=403, **refused, **alice),
            # The deployment's own 429, which the gateway passed on.
            _logged(
                "chat",
                11,
                0,
                response_code=429,
                backend_code=429,
                model=None,
                last_error_source="forward-request",
                **alice,
            ),
            # Served, but the LLM log named no model.
            _logged("chat", 200_000, 50_000, hour=12, model=None, **alice),
            _logged("reserved", 40_000, 10_000, hour=12, model=None, grant="k-bob", member=BOB),
            _logged(
                "mystery", 1_000, 1_000, hour=12, model=None, traced=False, subscription="sub-bot"
            ),
            _logged("legacy", 3_000, 1_000, hour=12, model=None, traced=False),
        ]
    )
    await _roll_up(harness)
    return harness


async def test_calls_the_model_never_served_add_requests_but_no_tokens_or_cost(
    harness: Harness,
) -> None:
    await refused_and_unnamed(harness)

    overview = harness.get("/api/v1/analytics/overview", range="30d")

    kpis = overview["kpis"]
    # The gateway's two 429s and the deployment's one, and the quota refusal, are all counted.
    assert (kpis["throttled"], kpis["quotaRefused"], kpis["backendThrottled"]) == (3, 1, 1)
    assert kpis["totalTokens"] == TOKENS
    assert kpis["cost"] == pytest.approx(TOTAL)
    mix = harness.get("/api/v1/analytics/reliability", range="30d")["statusMix"]
    assert (mix["throttled"], mix["quotaRefused"], mix["backendThrottled"]) == (3, 1, 1)
    cost = harness.get("/api/v1/analytics/cost", range="30d")
    apis = {row["label"]: (row["totalTokens"], row["cost"]) for row in cost["apis"]}
    assert apis["Chat"] == (CHAT_TOKENS, pytest.approx(CHAT_COST + UNNAMED_CHAT_COST))
    # None of the refusals' estimated tokens are priced, or left out as having no price.
    summary = cost["cost"]
    assert (summary["pricedTokens"], summary["unpricedTokens"]) == (
        CHAT_TOKENS + RESERVED_TOKENS,
        MYSTERY_TOKENS + LEGACY_TOKENS,
    )

    portal = harness.get("/api/v1/me/usage", period="30d")

    chat = next(row for row in portal["byResource"] if row["entitlementId"] == "grant-alice")
    assert (chat["requests"], chat["throttled"], chat["quotaRefused"]) == (15, 3, 1)
    assert chat["totalTokens"] == 10 * 1_100_000 + 250_000
    assert chat["estimatedCost"] == pytest.approx(35.0 + UNNAMED_CHAT_COST)


async def test_every_cost_breakdown_adds_up_to_the_total(harness: Harness) -> None:
    await refused_and_unnamed(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d")

    assert report["cost"]["total"] == pytest.approx(TOTAL)
    # Reserved capacity nobody called belongs to no model, API, or grant.
    for rows in (report["models"], report["apis"]):
        assert sum(row["cost"] or 0 for row in rows) == pytest.approx(CALLED)
        assert sum(row["totalTokens"] for row in rows) == TOKENS
    models = {row["label"]: (row["totalTokens"], row["cost"]) for row in report["models"]}
    # A call the LLM log named no model for counts under the model MOSAIC knows its deployment
    # serves, and the adopted API's, whose deployment MOSAIC doesn't know, under Unknown model.
    assert models == {
        "gpt-4o": (CHAT_TOKENS + RESERVED_TOKENS + 4_000, pytest.approx(CALLED)),
        "contoso-llm": (2_000, None),
        "Unknown model": (LEGACY_TOKENS, None),
    }
    # Grants carry every call but the unknown key's and the adopted API's.
    unattributed = harness.get("/api/v1/analytics/unattributed", range="30d")["cost"]["total"]
    assert unattributed == pytest.approx(0.25)
    centers = sum(row["cost"] or 0 for row in report["costCenters"])
    assert centers + unattributed == pytest.approx(CALLED)
    export = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
    assert sum(float(row["Cost (USD)"] or 0) for row in rows) == pytest.approx(TOTAL, abs=1e-3)

    # The other views that break usage down by model name the same models.
    overview = harness.get("/api/v1/analytics/overview", range="30d")
    top = {row["label"]: row["totalTokens"] for row in overview["topModels"]}
    assert top == {label: tokens for label, (tokens, _) in models.items()}
    tab = harness.get("/api/v1/analytics/models", range="30d")
    assert {row["model"]: row["totalTokens"] for row in tab["models"]} == top
    served = {row["apiName"]: row["models"] for row in tab["apis"]}
    assert (served["mystery"], served["legacy"]) == (["contoso-llm", "gpt-4o"], ["Unknown model"])


async def test_a_cost_centers_breakdowns_add_up_to_its_total(harness: Harness) -> None:
    await refused_and_unnamed(harness)

    report = harness.get("/api/v1/analytics/cost", range="30d", costCenterId=GENERAL)

    # Every grant charges General: Alice's chat, the provisioned deployment's, and the bot's.
    total = report["cost"]["total"]
    assert total == pytest.approx(35.0 + UNNAMED_CHAT_COST + MARCH_SO_FAR)
    for rows in (report["models"], report["apis"], report["costCenters"]):
        assert sum(row["cost"] or 0 for row in rows) == pytest.approx(total)
    tokens = sum(row["totalTokens"] for row in report["apis"])
    assert sum(row["totalTokens"] for row in report["models"]) == tokens
    # A grant's calls count under the model MOSAIC knows its API's deployment serves.
    assert {row["label"] for row in report["models"]} == {"gpt-4o", "contoso-llm"}
