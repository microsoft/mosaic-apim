"""Model use through MCP servers in Analytics, the exports, the chargeback, and the portal.

Built on ``test_cost``'s estate. The Ticket tools MCP server calls models as the Ticket assistant,
an application with its own grant on the chat model under the Customer Support cost center. Alice,
Bob and Dana call Ticket tools, and the assistant calls chat while serving them. Dana holds no grant
on any model. The assistant also makes calls of its own, and two whose reference MOSAIC can't use.

Chat costs $2.50 and $10 per million prompt and completion tokens. See ADR 0025.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest
from azure.cosmos.aio import CosmosClient
from loganalytics_double import GatewayCall
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import (
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    PrincipalKind,
    general_cost_center_id,
)
from mosaic_api.repositories.cosmos_usage import CosmosUsageRollupRepository
from mosaic_api.services.analytics.cost_report import split_cost
from test_cost import (
    ALICE,
    BOB,
    CAROL,
    NOW,
    TENANT,
    Harness,
    _audit,
    _principal,
    _roll_up,
    _traced,
    harness,
    seed,
    settings,
)

__all__ = ["harness", "settings"]

ASSISTANT = "assistant-oid"
DANA = "dana-oid"
SUPPORT = "costCenter_support"
GENERAL = general_cost_center_id(TENANT)
MCP_SECONDS = 60
# Each MCP call's own reference, which the gateway passes to the MCP server.
ALICE_17 = "aaaabbbb-cccc-dddd-eeee-ffff00000017"
ALICE_18 = "aaaabbbb-cccc-dddd-eeee-ffff00000018"
BOB_18 = "aaaabbbb-cccc-dddd-eeee-ffff0000b018"
DANA_18 = "aaaabbbb-cccc-dddd-eeee-ffff0000d018"
NO_SUCH_CALL = "aaaabbbb-cccc-dddd-eeee-ffff00009999"

# What each person's model use through Ticket tools cost, unrounded.
ALICE_COST = (12_345 * 2.5 + 6_789 * 10 + 20_001 * 2.5 + 3_333 * 10) / 1e6
BOB_COST = (7_777 * 2.5 + 1_111 * 10) / 1e6
DANA_COST = (4_000 * 2.5 + 400 * 10) / 1e6
# The assistant's own calls: one of its own, and two whose reference it couldn't use.
OWN_COST = (50_000 * 2.5 + 5_000 * 10 + 2 * 1_000 * 2.5) / 1e6


def _at(day: int, hour: int, seconds: int = 0) -> datetime:
    return datetime(2026, 3, day, hour, tzinfo=UTC) + timedelta(seconds=seconds)


def _mcp(grant: str, member: str, reference: str, at: datetime) -> GatewayCall:
    return GatewayCall(
        time=at,
        api="tickets",
        grant=grant,
        member=member,
        client_app="vscode",
        reference=reference,
        model_caller=ASSISTANT,
        total_time_ms=MCP_SECONDS * 1000,
    )


def _model(at: datetime, prompt: int, completion: int, reference: str = "") -> GatewayCall:
    return GatewayCall(
        time=at,
        api="chat",
        grant="k-assistant",
        client_app="ticket-assistant-app",
        reference=reference,
        prompt_tokens=prompt,
        completion_tokens=completion,
        deployment="chat",
        model="gpt-4o",
    )


async def seed_on_behalf(harness: Harness) -> None:
    await seed(harness)
    state = harness.state
    await state.cost_center_repository.create_cost_center(
        CostCenter(id=SUPPORT, tenant_id=TENANT, name="Customer Support", code="SUP"),
        _audit("costCenter"),
    )
    assistant = await _principal(
        harness, ASSISTANT, PrincipalKind.SERVICE_PRINCIPAL, "Ticket assistant"
    )
    dana = await _principal(harness, DANA, PrincipalKind.USER, "Dana")
    grants = [
        ("grant-assistant", "application", assistant, "modelApi", "model-api-chat", "k-assistant"),
        ("grant-bob-tickets", "user", f"principal-{BOB}", "mcpServer", "mcp-tickets", "k-bob-t"),
        ("grant-dana-tickets", "user", dana, "mcpServer", "mcp-tickets", "k-dana-t"),
    ]
    for grant_id, kind, subject, resource_kind, resource_id, key in grants:
        await state.entitlement_repository.save_entitlement(
            Entitlement(
                id=grant_id,
                tenant_id=TENANT,
                created_at=NOW - timedelta(days=90),
                subject=EntitlementSubject.model_validate({"kind": kind, "id": subject}),
                resource=EntitlementResource.model_validate(
                    {"kind": resource_kind, "id": resource_id}
                ),
                binding=_traced(key),
                cost_center_id=SUPPORT if grant_id == "grant-assistant" else "",
            ),
            _audit("entitlement"),
        )
    harness.logs.calls = [
        *harness.logs.calls,
        _mcp("k-alice-tickets", ALICE, ALICE_17, _at(17, 10)),
        _model(_at(17, 10, 30), 12_345, 6_789, ALICE_17),
        _mcp("k-alice-tickets", ALICE, ALICE_18, _at(18, 10)),
        _model(_at(18, 10, 20), 20_001, 3_333, ALICE_18),
        _mcp("k-bob-t", BOB, BOB_18, _at(18, 11)),
        _model(_at(18, 11, 10), 7_777, 1_111, BOB_18),
        _mcp("k-dana-t", DANA, DANA_18, _at(18, 14)),
        _model(_at(18, 14, 5), 4_000, 400, DANA_18),
        # The assistant's own call, and two whose reference names no MCP call it can use.
        _model(_at(18, 12), 50_000, 5_000),
        _model(_at(18, 13), 1_000, 0, "!"),
        _model(_at(18, 13, 5), 1_000, 0, NO_SUCH_CALL),
    ]


async def rolled_up(harness: Harness) -> Harness:
    await seed_on_behalf(harness)
    await _roll_up(harness)
    return harness


def _forget_on_behalf_summaries(harness: Harness) -> None:
    """Drop the on-behalf summaries, as if no model call had been made for anyone."""

    summaries = harness.state.usage_rollup_repository._summaries
    for key, summary in list(summaries.items()):
        if summary.dimension in {"onBehalf", "onBehalfUnresolved"}:
            del summaries[key]


def _forget_on_behalf_facts(harness: Harness) -> None:
    facts = harness.state.usage_rollup_repository._facts
    for key, fact in list(facts.items()):
        if fact.on_behalf:
            facts[key] = fact.model_copy(update={"on_behalf": []})


def _without(report: dict[str, Any], *fields: str) -> dict[str, Any]:
    return {key: value for key, value in report.items() if key not in {"generatedAt", *fields}}


def _csv(harness: Harness, **params: Any) -> list[dict[str, str]]:
    response = harness.client.get("/api/v1/analytics/export", params=params)
    assert response.status_code == 200, response.text
    return list(csv.DictReader(io.StringIO(response.text.lstrip("\ufeff"))))


# -- Analytics ------------------------------------------------------------------------------


async def test_consumers_list_each_persons_model_use_through_mcp_servers(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/analytics/consumers", range="30d")

    rows = {row["personLabel"]: row for row in report["onBehalf"]}
    assert set(rows) == {"Alice", "Bob", "Dana"}
    alice = rows["Alice"]
    assert (alice["mcpLabel"], alice["mcpApiName"], alice["mcpServerId"]) == (
        "Ticket tools",
        "tickets",
        "mcp-tickets",
    )
    assert (alice["applicationLabel"], alice["applicationObjectId"]) == (
        "Ticket assistant",
        ASSISTANT,
    )
    assert (alice["personObjectId"], alice["personPrincipalKind"]) == (ALICE, "user")
    assert alice["gatewayName"] == "Production gateway"
    assert (alice["requests"], alice["totalTokens"]) == (2, 19_134 + 23_334)
    assert alice["cost"] == pytest.approx(round(ALICE_COST, 4))
    assert (rows["Bob"]["requests"], rows["Bob"]["totalTokens"]) == (1, 8_888)
    assert rows["Bob"]["cost"] == pytest.approx(round(BOB_COST, 4))
    assert rows["Dana"]["cost"] == pytest.approx(DANA_COST)
    # Shares are of every linked call, like every other share on the page.
    assert alice["requestShare"] == pytest.approx(round(2 / report["linkedRequests"], 4))
    # The busiest first, by tokens, as every other table on the page.
    assert [row["personLabel"] for row in report["onBehalf"]] == ["Alice", "Bob", "Dana"]
    assert report["onBehalfUnresolved"] == [
        {"reason": "malformed", "requests": 1, "totalTokens": 1_000},
        {"reason": "missing", "requests": 1, "totalTokens": 1_000},
    ]


async def test_a_reason_mosaic_doesnt_know_counts_as_unknown(harness: Harness) -> None:
    await rolled_up(harness)
    summaries = harness.state.usage_rollup_repository._summaries
    [unresolved] = [
        summary
        for summary in summaries.values()
        if summary.dimension == "onBehalfUnresolved" and summary.period == "day"
    ]
    unresolved.entries.append(
        unresolved.entries[0].model_copy(update={"key": "trace:k-assistant|from-the-future"})
    )

    report = harness.get("/api/v1/analytics/consumers", range="30d")

    assert [(row["reason"], row["requests"]) for row in report["onBehalfUnresolved"]] == [
        ("malformed", 1),
        ("missing", 1),
        ("unknown", 1),
    ]


async def test_model_use_through_mcp_servers_changes_no_other_figure(harness: Harness) -> None:
    await rolled_up(harness)
    reports = {
        path: harness.get(f"/api/v1/analytics/{path}", range="30d")
        for path in ("consumers", "overview", "cost", "models")
    }
    # Still the application's own calls: its row, its grant and its client app count all seven.
    consumers = reports["consumers"]
    [assistant] = [row for row in consumers["applications"] if row["label"] == "Ticket assistant"]
    assert assistant["requests"] == 7
    [grant] = [row for row in consumers["grants"] if row["entitlementId"] == "grant-assistant"]
    assert (grant["requests"], grant["costCenterCode"]) == (7, "SUP")
    assert {row["label"] for row in consumers["people"]}.isdisjoint({"Ticket assistant"})

    _forget_on_behalf_summaries(harness)

    for path, before in reports.items():
        after = harness.get(f"/api/v1/analytics/{path}", range="30d")
        assert _without(after, "onBehalf", "onBehalfUnresolved") == _without(
            before, "onBehalf", "onBehalfUnresolved"
        ), path
    assert harness.get("/api/v1/analytics/consumers", range="30d")["onBehalf"] == []


async def test_the_filters_narrow_it_by_the_applications_grant(harness: Harness) -> None:
    await rolled_up(harness)

    def people(**params: Any) -> set[str]:
        report = harness.get("/api/v1/analytics/consumers", range="30d", **params)
        return {row["personLabel"] for row in report["onBehalf"]}

    # The application's grant is charged to Customer Support, so its calls follow that.
    assert people(costCenterId=SUPPORT) == {"Alice", "Bob", "Dana"}
    assert people(costCenterId=GENERAL) == set()
    filtered = harness.get("/api/v1/analytics/consumers", range="30d", costCenterId=SUPPORT)
    assert [row["reason"] for row in filtered["onBehalfUnresolved"]] == ["malformed", "missing"]
    # Its grant is on the chat model, so the model's filter keeps the rows and the server's doesn't.
    assert people(resourceId="model-api-chat") == {"Alice", "Bob", "Dana"}
    assert people(resourceId="mcp-tickets") == set()
    assert people(subjectKind="application") == {"Alice", "Bob", "Dana"}
    assert people(subjectKind="user") == set()


async def test_the_table_exports_as_csv(harness: Harness) -> None:
    await rolled_up(harness)

    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "onBehalf", "range": "30d"}
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        'attachment; filename="mosaic-onBehalf-20260217-20260318.csv"'
    )
    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    assert (reader.fieldnames or [])[:8] == [
        "Person",
        "Person detail",
        "Person object ID",
        "MCP server",
        "MCP API name",
        "Gateway",
        "Application",
        "Application object ID",
    ]
    rows = {row["Person"]: row for row in reader}
    assert set(rows) == {"Alice", "Bob", "Dana"}
    alice = rows["Alice"]
    assert (alice["MCP server"], alice["Application"], alice["Requests"]) == (
        "Ticket tools",
        "Ticket assistant",
        "2",
    )
    assert float(alice["Cost (USD)"]) == pytest.approx(round(ALICE_COST, 4))


# -- The chargeback -------------------------------------------------------------------------


async def test_the_chargeback_splits_an_applications_grant_by_person(harness: Harness) -> None:
    await rolled_up(harness)

    response = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )

    reader = csv.DictReader(io.StringIO(response.text.lstrip("\ufeff")))
    header = reader.fieldnames or []
    at = header.index("Cost center name")
    assert header[at + 1 : at + 3] == ["On behalf of", "On behalf of object ID"]
    rows = [row for row in reader if row["Charged to"] == "Ticket assistant"]
    split = {row["On behalf of"]: row for row in rows}
    assert set(split) == {"", "Alice", "Bob", "Dana"}
    for row in rows:
        # Still charged to the application, under its grant's cost center.
        assert (row["Kind"], row["Object ID"]) == ("application", ASSISTANT)
        assert (row["Cost center"], row["Cost center name"]) == ("SUP", "Customer Support")
        assert (row["Month"], row["Model"], row["Deployment"]) == ("2026-03", "gpt-4o", "chat")
    assert (split["Alice"]["On behalf of object ID"], split["Alice"]["Requests"]) == (ALICE, "2")
    assert split["Alice"]["Total tokens"] == str(19_134 + 23_334)
    assert split["Bob"]["On behalf of object ID"] == BOB
    assert (split["Dana"]["Requests"], split["Dana"]["Cost (USD)"]) == ("1", "0.014")
    # The rest is the application's own use, with the on-behalf columns left empty.
    own = split[""]
    assert (own["On behalf of object ID"], own["Requests"], own["Total tokens"]) == (
        "",
        "3",
        "57000",
    )
    assert all(row["Priced"] == "yes" for row in rows)
    # Rows no MCP server's application made for anyone aren't split.
    others = _csv(harness, view="chargeback", range="30d")
    assert all(
        row["On behalf of"] == "" for row in others if row["Charged to"] != "Ticket assistant"
    )


async def test_the_split_rows_add_up_exactly_to_the_unsplit_row(harness: Harness) -> None:
    await rolled_up(harness)
    split = _csv(harness, view="chargeback", range="30d")
    _forget_on_behalf_summaries(harness)
    whole = _csv(harness, view="chargeback", range="30d")

    identity = ("Month", "Charged to", "Object ID", "Cost center", "Model", "Deployment")
    sums: dict[tuple[str, ...], list[Decimal]] = defaultdict(
        lambda: [Decimal(0), Decimal(0), Decimal(0), Decimal(0), Decimal(0)]
    )
    for row in split:
        total = sums[tuple(row[name] for name in identity)]
        for index, name in enumerate(
            ("Requests", "Prompt tokens", "Completion tokens", "Total tokens", "Cost (USD)")
        ):
            total[index] += Decimal(row[name] or "0")
    assert len(split) == len(whole) + 3
    for row in whole:
        assert (row["On behalf of"], row["On behalf of object ID"]) == ("", "")
        expected = [
            Decimal(row[name] or "0")
            for name in (
                "Requests",
                "Prompt tokens",
                "Completion tokens",
                "Total tokens",
                "Cost (USD)",
            )
        ]
        assert sums[tuple(row[name] for name in identity)] == expected, row["Charged to"]
    [assistant] = [row for row in whole if row["Charged to"] == "Ticket assistant"]
    assert (assistant["Requests"], assistant["Total tokens"]) == ("7", "112756")
    expected_total = round(ALICE_COST + BOB_COST + DANA_COST + OWN_COST, 4)
    assert Decimal(assistant["Cost (USD)"]) == Decimal(str(expected_total))


async def test_the_cost_center_filter_keeps_the_split(harness: Harness) -> None:
    await rolled_up(harness)

    rows = _csv(harness, view="chargeback", range="30d", costCenterId=SUPPORT)

    assert {row["Charged to"] for row in rows} == {"Ticket assistant"}
    assert {row["On behalf of"] for row in rows} == {"", "Alice", "Bob", "Dana"}


async def test_a_refused_call_counts_for_the_person_with_no_tokens(harness: Harness) -> None:
    await seed_on_behalf(harness)
    # The application's token limit refused one of the model calls it made for Alice.
    harness.logs.calls.append(
        replace(
            _model(_at(18, 10, 40), 5_000, 500, ALICE_18),
            response_code=429,
            backend_code=0,
        )
    )
    await _roll_up(harness)

    consumers = harness.get("/api/v1/analytics/consumers", range="30d")
    [alice] = [row for row in consumers["onBehalf"] if row["personLabel"] == "Alice"]
    assert (alice["requests"], alice["throttled"]) == (3, 1)
    assert alice["totalTokens"] == 19_134 + 23_334
    assert alice["cost"] == pytest.approx(round(ALICE_COST, 4))
    [mine] = harness.get("/api/v1/me/usage", period="30d")["onBehalf"]
    assert (mine["requests"], mine["totalTokens"]) == (3, 19_134 + 23_334)
    split = _csv(harness, view="chargeback", range="30d")
    _forget_on_behalf_summaries(harness)
    [whole] = [
        row
        for row in _csv(harness, view="chargeback", range="30d")
        if row["Charged to"] == "Ticket assistant"
    ]
    parts = [row for row in split if row["Charged to"] == "Ticket assistant"]
    for column in ("Requests", "Total tokens", "Cost (USD)"):
        assert sum(Decimal(row[column] or "0") for row in parts) == Decimal(whole[column])
    assert whole["Requests"] == "8"


@pytest.mark.parametrize(
    ("total", "amounts"),
    [
        (0.3926, [0.18, 0.182085, 0.0305525]),
        (0.0001, [0.00005, 0.00005]),
        (0.0003, [0.0001, 0.0001, 0.0001]),
        (1.0, [1 / 3, 1 / 3, 1 / 3]),
        (0.0, [0.0, 0.0]),
        (2.5, [None, 1.0, 1.5]),
        (0.75, [0.0, 0.0]),
    ],
)
def test_a_split_cost_adds_up_exactly(total: float, amounts: list[float | None]) -> None:
    shares = split_cost(total, amounts)

    assert sum(Decimal(str(share)) for share in shares if share is not None) == Decimal(
        str(total)
    )
    for amount, share in zip(amounts, shares, strict=True):
        if amount is None:
            assert share is None
        else:
            assert share is not None and share >= 0
    weighed = sum(amount or 0 for amount in amounts)
    if weighed:
        for amount, share in zip(amounts, shares, strict=True):
            if amount is not None and share is not None:
                assert abs(share - total * amount / weighed) <= 0.0001 + 1e-12


def test_a_split_of_no_cost_stays_unpriced() -> None:
    assert split_cost(None, [0.1, None]) == [None, None]


# -- The portal ------------------------------------------------------------------------------


async def test_a_person_sees_their_own_model_use_through_mcp_servers(harness: Harness) -> None:
    await rolled_up(harness)

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    assert (row["mcpServer"]["kind"], row["mcpServer"]["id"], row["mcpServer"]["displayName"]) == (
        "mcpServer",
        "mcp-tickets",
        "Ticket tools",
    )
    resource = row["resource"]
    assert (resource["kind"], resource["id"], resource["displayName"]) == (
        "modelApi",
        "model-api-chat",
        "Chat",
    )
    assert (resource["gatewayName"], resource["environment"]) == (
        "Production gateway",
        "production",
    )
    assert row["model"] == "gpt-4o"
    assert (row["requests"], row["promptTokens"], row["completionTokens"]) == (
        2,
        12_345 + 20_001,
        6_789 + 3_333,
    )
    assert row["totalTokens"] == 19_134 + 23_334
    # Priced through the model's deployment, as the person's own model rows are.
    assert row["estimatedCost"] == pytest.approx(round(ALICE_COST, 4))
    assert row["costNote"] is None
    # Charged to the cost center of the application's grant, not one of hers.
    assert row["costCenter"] == {"id": SUPPORT, "name": "Customer Support", "code": "SUP"}
    assert row["lastUsedAt"].startswith("2026-03-18T10")


async def test_a_person_sees_nobody_elses_model_use(harness: Harness) -> None:
    await rolled_up(harness)

    # Bob's model call was made the same day, through the same application's grant, as one of
    # Alice's and as the application's own calls: the same fact holds them all.
    harness.sign_in(BOB, ["User"], frozenset())
    [bob] = harness.get("/api/v1/me/usage", period="30d")["onBehalf"]
    assert (bob["requests"], bob["totalTokens"]) == (1, 8_888)
    assert bob["estimatedCost"] == pytest.approx(round(BOB_COST, 4))

    harness.sign_in(ALICE, ["User"])
    [alice] = harness.get("/api/v1/me/usage", period="30d")["onBehalf"]
    assert (alice["requests"], alice["totalTokens"]) == (2, 19_134 + 23_334)

    harness.sign_in(CAROL, ["User"])
    assert harness.get("/api/v1/me/usage", period="30d")["onBehalf"] == []

    # The application's own calls are its own: it's never someone it called for.
    harness.sign_in(ASSISTANT, ["User"], frozenset())
    assert harness.get("/api/v1/me/usage", period="30d")["onBehalf"] == []


async def test_someone_with_no_model_grant_sees_their_use_priced(harness: Harness) -> None:
    await rolled_up(harness)
    harness.sign_in(DANA, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    assert row["estimatedCost"] == pytest.approx(DANA_COST)
    assert row["costCenter"]["code"] == "SUP"
    # None of it is hers to pay, so her own figures carry none of it.
    assert report["totals"]["totalTokens"] == 0
    assert report["totals"]["estimatedCost"] is None


async def test_the_rest_of_the_report_is_unchanged(harness: Harness) -> None:
    await rolled_up(harness)
    before = harness.get("/api/v1/me/usage", period="30d")

    _forget_on_behalf_facts(harness)

    after = harness.get("/api/v1/me/usage", period="30d")
    assert after["onBehalf"] == []
    assert _without(after, "onBehalf") == _without(before, "onBehalf")


async def test_the_figures_stay_current_when_the_mcp_grant_is_gone(harness: Harness) -> None:
    await rolled_up(harness)
    entitlements = harness.state.entitlement_repository
    grant = await entitlements.get_entitlement(TENANT, "grant-dana-tickets")
    assert grant is not None
    await entitlements.delete_entitlement(grant, _audit("entitlement"))
    harness.sign_in(DANA, ["User"], frozenset())

    report = harness.get("/api/v1/me/usage", period="30d")

    [row] = report["onBehalf"]
    assert row["requests"] == 1
    # The section's gateway counts, so the report says how current it is.
    assert report["freshness"]["gateways"] == 1
    assert report["freshness"]["status"] != "notLinked"


# -- The repositories ------------------------------------------------------------------------


async def test_facts_are_found_by_the_person_they_were_made_for(harness: Harness) -> None:
    await rolled_up(harness)
    rollups = harness.state.usage_rollup_repository

    async def found(person: str) -> list[tuple[str, str]]:
        facts = await rollups.list_facts(
            TENANT, start_day="2026-03-01", end_day="2026-03-31", on_behalf_object_id=person
        )
        return sorted((fact.day, fact.link_key) for fact in facts)

    # Only the application's facts that hold calls made for Alice: none of her own.
    assert await found(ALICE) == [("2026-03-17", "k-assistant"), ("2026-03-18", "k-assistant")]
    assert await found(ALICE.upper()) == await found(ALICE)
    assert await found(BOB) == [("2026-03-18", "k-assistant")]
    assert await found(CAROL) == []
    assert await found("") == []


class _Queries:
    """A Cosmos container that records the queries it's asked, and answers with no items."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, list[dict[str, Any]]]] = []

    def get_database_client(self, _name: str) -> _Queries:
        return self

    def get_container_client(self, _name: str) -> _Queries:
        return self

    def query_items(
        self, *, query: str, parameters: list[dict[str, Any]], partition_key: str
    ) -> Any:
        self.asked.append((query, parameters))

        async def none() -> Any:
            for item in ():
                yield item

        return none()


async def test_cosmos_finds_facts_by_any_entry_for_the_person() -> None:
    cosmos = _Queries()
    repository = CosmosUsageRollupRepository(cast(CosmosClient, cosmos), "mosaic", "rollups")

    await repository.list_facts(
        TENANT, start_day="2026-03-01", end_day="2026-03-31", on_behalf_object_id="Alice-OID"
    )
    assert await repository.list_facts(
        TENANT, start_day="2026-03-01", end_day="2026-03-31", on_behalf_object_id=""
    ) == []

    [(query, parameters)] = cosmos.asked
    assert 'ARRAY_CONTAINS(c.onBehalf, {"objectId": @onBehalfObjectId}, true)' in query
    assert {"name": "@onBehalfObjectId", "value": "alice-oid"} in parameters
