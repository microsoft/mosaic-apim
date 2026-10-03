"""Model calls an MCP server's application made for the people who called it. See ADR 0025."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from loganalytics_double import GatewayCall
from mosaic_api.domain import (
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementSubject,
    McpServer,
)
from mosaic_api.usage_telemetry import UsageFact, UsageMetrics, UsageOnBehalf, iso_day
from test_usage_rollup import (
    ALICE,
    APP,
    BOB,
    CAROL,
    GATEWAY,
    NOW,
    TENANT,
    TODAY,
    RollupHarness,
    _audit,
    build_harness,
)

SEARCH = "mcp-search"
REFERENCE = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
OTHER_REFERENCE = "aaaabbbb-cccc-dddd-eeee-ffff00002222"
APP_LINK = "trace:k-app"
TEN = NOW.replace(hour=10, minute=0, second=0, microsecond=0)


@pytest.fixture
def harness() -> RollupHarness:
    return build_harness()


async def _seed(harness: RollupHarness) -> None:
    await harness.seed()
    await harness.gateways.save_mcp_server(
        McpServer(
            id="mcp-server-search",
            tenant_id=TENANT,
            gateway_id=GATEWAY,
            api_name=SEARCH,
            display_name="Contoso Search",
            path="mcp/search",
            publication_id="mcp-publication-search",
        ),
        _audit("mcp-server"),
    )
    # The application's own model grant, which its token's calls match and its trace names.
    await harness.save_grant("grant-app-token", "application", "principal-app-oid", "k-app", None)
    for entitlement_id, kind, subject, key, per_member in (
        ("grant-mcp-alice", "user", "principal-alice-oid", "k-mcp-alice", False),
        ("grant-mcp-analysts", "securityGroup", "principal-analysts-oid", "k-mcp-analysts", True),
    ):
        await harness.entitlements.save_entitlement(
            Entitlement(
                id=entitlement_id,
                tenant_id=TENANT,
                subject=EntitlementSubject(kind=kind, id=subject),
                resource=EntitlementResource(kind="mcpServer", id="mcp-server-search"),
                binding=EntitlementBinding(
                    gateway_id=GATEWAY,
                    attribution_key=key,
                    attribution_per_member=per_member,
                    source=BindingSource.ORCHESTRATED,
                ),
            ),
            _audit("entitlement"),
        )


def _mcp_call(
    *,
    reference: str = REFERENCE,
    grant: str = "k-mcp-alice",
    member: str = "",
    model_caller: str = APP,
    at: datetime = TEN,
    total_time_ms: int = 60_000,
) -> GatewayCall:
    return GatewayCall(
        time=at,
        api=SEARCH,
        grant=grant,
        member=member,
        client_app="vscode",
        reference=reference,
        model_caller=model_caller,
        total_time_ms=total_time_ms,
    )


def _model_call(
    *,
    reference: str = REFERENCE,
    grant: str = "k-app",
    member: str = "",
    at: datetime = TEN + timedelta(seconds=30),
) -> GatewayCall:
    return GatewayCall(
        time=at,
        api="chat",
        grant=grant,
        member=member,
        client_app=APP,
        reference=reference,
        prompt_tokens=100,
        completion_tokens=50,
        deployment="chat-prod",
        model="gpt-4o",
    )


async def _facts(harness: RollupHarness, day: str = iso_day(TODAY)) -> dict[str, UsageFact]:
    facts = await harness.rollups.list_facts(TENANT, start_day=day, end_day=day)
    return {fact.link_key: fact for fact in facts}


async def _entries(
    harness: RollupHarness, dimension: str, day: str = iso_day(TODAY)
) -> dict[str, int]:
    summaries = await harness.rollups.list_summaries(
        TENANT, period="day", start=day, end=day, dimensions=[dimension], gateway_ids=[GATEWAY]
    )
    return {
        entry.key: entry.metrics.requests for summary in summaries for entry in summary.entries
    }


async def test_a_model_call_counts_for_the_person_whose_mcp_call_it_served(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    harness.logs.calls = [_mcp_call(), _model_call(), _model_call(at=TEN + timedelta(seconds=45))]

    await harness.run_until_done()

    facts = await _facts(harness)
    app = facts["k-app"]
    # Still the application's calls, on its own grant.
    assert (app.caller_object_id, app.entitlement_id, app.metrics.requests) == (
        APP,
        "grant-app-token",
        2,
    )
    [share] = app.on_behalf
    assert (share.object_id, share.mcp_api, share.mcp_key) == (ALICE, SEARCH, "k-mcp-alice")
    assert (share.metrics.requests, share.metrics.total_tokens) == (2, 300)
    assert [hour.hour for hour in share.hours] == [10]
    assert await _entries(harness, "onBehalf") == {f"{APP_LINK}|{APP}|{ALICE}|{SEARCH}": 2}
    assert await _entries(harness, "grantCaller") == {
        f"{APP_LINK}|{APP}": 2,
        f"trace:k-mcp-alice|{ALICE}": 1,
    }
    # The MCP call's own row names its reference, but no other call.
    assert facts["k-mcp-alice"].on_behalf == []
    assert await _entries(harness, "onBehalfUnresolved") == {}


async def test_a_security_group_member_who_called_the_mcp_server_is_the_person(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    harness.logs.calls = [_mcp_call(grant="k-mcp-analysts", member=CAROL), _model_call()]

    await harness.run_until_done()

    [share] = (await _facts(harness))["k-app"].on_behalf
    assert (share.object_id, share.mcp_key) == (CAROL, "k-mcp-analysts")


async def test_a_reference_counts_only_for_the_application_the_mcp_server_names(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    harness.logs.calls = [
        _mcp_call(),
        # A person who learned the reference and called the model directly.
        _model_call(grant="k-bob"),
        # The application, naming an MCP call to a server that calls models as someone else.
        _mcp_call(reference=OTHER_REFERENCE, model_caller=BOB),
        _model_call(reference=OTHER_REFERENCE),
    ]

    await harness.run_until_done()

    facts = await _facts(harness)
    assert facts["k-bob"].on_behalf == []
    assert facts["k-app"].on_behalf == []
    assert await _entries(harness, "onBehalf") == {}
    assert await _entries(harness, "onBehalfUnresolved") == {
        "trace:k-bob|caller": 1,
        f"{APP_LINK}|caller": 1,
    }


async def test_references_that_cant_be_used_are_counted_by_reason(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    harness.logs.calls = [
        _mcp_call(),
        # The MCP call ran for a minute; this model call came long after it ended.
        _model_call(at=TEN + timedelta(minutes=7)),
        _model_call(reference=OTHER_REFERENCE),
        _model_call(reference="!"),
        # An MCP server with no model caller passes no reference, so one naming it is unknown.
        _mcp_call(reference="", model_caller=""),
        _model_call(reference="aaaabbbb-cccc-dddd-eeee-ffff00003333"),
        # An MCP call whose grant MOSAIC doesn't know yet.
        _mcp_call(reference="aaaabbbb-cccc-dddd-eeee-ffff00004444", grant="k-mcp-unknown"),
        _model_call(reference="aaaabbbb-cccc-dddd-eeee-ffff00004444"),
    ]

    await harness.run_until_done()

    assert (await _facts(harness))["k-app"].on_behalf == []
    assert await _entries(harness, "onBehalfUnresolved") == {
        f"{APP_LINK}|late": 1,
        f"{APP_LINK}|missing": 2,
        f"{APP_LINK}|malformed": 1,
        f"{APP_LINK}|unknown": 1,
    }
    # The calls themselves stay the application's.
    assert (await _entries(harness, "grantCaller"))[f"{APP_LINK}|{APP}"] == 5


async def test_a_model_call_within_the_allowance_of_its_mcp_call_still_counts(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    harness.logs.calls = [
        _mcp_call(),
        # The MCP call ended a minute after it began, and the allowance is five minutes.
        _model_call(at=TEN + timedelta(minutes=5, seconds=59)),
        _model_call(at=TEN - timedelta(minutes=4)),
    ]

    await harness.run_until_done()

    [share] = (await _facts(harness))["k-app"].on_behalf
    assert share.metrics.requests == 2


async def test_an_mcp_call_logged_after_midnight_still_counts_for_yesterdays_model_call(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    midnight = NOW.replace(hour=0, minute=0, second=0, microsecond=0)
    harness.logs.calls = [
        _mcp_call(at=midnight + timedelta(seconds=10)),
        _model_call(at=midnight - timedelta(seconds=30)),
    ]

    await harness.run_until_done()

    yesterday = iso_day(TODAY - timedelta(days=1))
    [share] = (await _facts(harness, yesterday))["k-app"].on_behalf
    assert share.object_id == ALICE
    assert await _entries(harness, "onBehalf", yesterday) == {
        f"{APP_LINK}|{APP}|{ALICE}|{SEARCH}": 1
    }


async def test_a_fact_without_calls_made_for_others_stores_no_on_behalf_field(
    harness: RollupHarness,
) -> None:
    await _seed(harness)
    harness.logs.calls = [_mcp_call(), _model_call()]

    await harness.run_until_done()

    facts = await _facts(harness)
    assert "onBehalf" not in facts["k-mcp-alice"].model_dump(mode="json", by_alias=True)
    assert facts["k-app"].model_dump(mode="json", by_alias=True)["onBehalf"] == [
        UsageOnBehalf(
            object_id=ALICE,
            mcp_api=SEARCH,
            mcp_key="k-mcp-alice",
            metrics=facts["k-app"].on_behalf[0].metrics,
            hours=facts["k-app"].on_behalf[0].hours,
        ).model_dump(mode="json", by_alias=True)
    ]
    assert isinstance(facts["k-app"].on_behalf[0].metrics, UsageMetrics)
