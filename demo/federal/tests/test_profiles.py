import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from federal_runtime.catalog import PROFILES
from federal_runtime.data import MAX_RESULT_BYTES, DataError, Snapshot
from federal_runtime.server import build_server

pytestmark = pytest.mark.anyio

READ_CALLS: dict[str, list[tuple[str, dict[str, Any]]]] = {
    "doe-energy-data": [
        ("state_energy_profile", {"state": "AL"}),
        ("compare_generation_mix", {"state_a": "CA", "state_b": "TX"}),
        ("source_details", {}),
    ],
    "doe-lab-research": [
        ("search_research", {"query": "solar"}),
        ("get_record", {"record_id": "3376627"}),
    ],
    "faa-airport-reference": [
        ("find_airport", {"query": "Dulles"}),
        ("airport_profile", {"airport_id": "KIAD"}),
        ("source_cycle", {}),
    ],
    "faa-regulatory-reference": [
        ("lookup_citation", {"section": "107.31"}),
        ("find_guidance", {"query": "waivers"}),
    ],
    "usda-crop-insights": [
        ("crop_summary", {"crop": "corn"}),
        ("compare_yields", {"crop": "corn"}),
        ("source_release", {}),
    ],
    "usda-conservation-guide": [
        ("lookup_practice", {"practice_code": "340"}),
        ("find_practices", {"query": "cover"}),
    ],
    "nasa-mission-archive": [
        ("search_missions", {"query": "Artemis"}),
        ("mission_sources", {"record_id": "KSC-20211022-PH-KLS01_0002"}),
    ],
    "nasa-earthdata-explorer": [
        ("search_collections", {"query": "MODIS"}),
        ("describe_collection", {"collection_id": "C2565093311-NSIDC_CPRD"}),
        (
            "compare_coverage",
            {"collection_ids": ["C2565093311-NSIDC_CPRD", "C3044685923-NSIDC_CPRD"]},
        ),
    ],
}


@pytest.mark.parametrize("profile", PROFILES)
async def test_every_profile_exact_tools_schema_annotations_and_provenance(profile: str) -> None:
    server = build_server(profile)
    definition = PROFILES[profile]
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        tools = (await client.list_tools()).tools
        assert {tool.name for tool in tools} == set(definition.tools)
        for tool in tools:
            assert tool.description
            assert tool.inputSchema["type"] == "object"
            assert "ctx" not in tool.inputSchema.get("properties", {})
            assert "self" not in tool.inputSchema.get("properties", {})
            assert tool.annotations.readOnlyHint is True
            assert tool.annotations.destructiveHint is False
            assert tool.annotations.idempotentHint is (tool.name != definition.model_tool)
        for name, arguments in READ_CALLS[profile]:
            result = await client.call_tool(name, arguments)
            assert not result.isError, result
            payload = result.structuredContent
            assert payload["profile"] == profile
            assert payload["provenance"]["mode"] == "snapshot"
            assert payload["provenance"]["release"]
            assert datetime.fromisoformat(payload["provenance"]["retrieved_at"])
            assert payload["disclaimer"].startswith("Unofficial")
            assert len(json.dumps(payload).encode()) <= MAX_RESULT_BYTES


@pytest.mark.parametrize("profile", PROFILES)
def test_snapshot_contains_actual_public_provenance_and_record_versions(profile: str) -> None:
    snapshot = Snapshot(profile)
    assert len(snapshot.records) >= 3
    for source in snapshot.provenance["sources"]:
        assert urlsplit(source["url"]).scheme == "https"
        assert len(source.get("sha256", source.get("excerpt_sha256", ""))) == 64
    for record in snapshot.records:
        assert record["id"]
        assert urlsplit(record["source_url"]).scheme == "https"
        assert "synthetic" not in record
    ids = [record["id"] for record in snapshot.records]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    ("profile", "tool", "arguments"),
    [
        ("doe-energy-data", "state_energy_profile", {"state": "not-a-state"}),
        ("doe-energy-data", "state_energy_profile", {"state": "VA"}),
        ("doe-energy-data", "state_energy_profile", {"state": "AL", "year": 2026}),
        ("doe-energy-data", "compare_generation_mix", {"state_a": "AL", "state_b": "al"}),
        ("doe-lab-research", "search_research", {"query": " "}),
        ("doe-lab-research", "search_research", {"query": "solar", "limit": 6}),
        ("doe-lab-research", "get_record", {"record_id": "x" * 121}),
        ("faa-airport-reference", "airport_profile", {"airport_id": "NOT-A-REAL-AIRPORT"}),
        ("faa-airport-reference", "find_airport", {"query": "x" * 81}),
        ("faa-regulatory-reference", "lookup_citation", {"section": "https://attacker.test"}),
        ("faa-regulatory-reference", "lookup_citation", {"section": "999.99"}),
        ("usda-crop-insights", "crop_summary", {"crop": "invented"}),
        ("usda-crop-insights", "compare_yields", {"crop": "soybeans"}),
        ("usda-crop-insights", "compare_yields", {"crop": "corn", "first_year": 2025}),
        ("usda-conservation-guide", "lookup_practice", {"practice_code": "abc"}),
        ("usda-conservation-guide", "find_practices", {"query": ""}),
        ("nasa-mission-archive", "mission_sources", {"record_id": "not-present"}),
        ("nasa-mission-archive", "search_missions", {"query": "Artemis", "limit": 0}),
        ("nasa-earthdata-explorer", "describe_collection", {"collection_id": "unknown"}),
        ("nasa-earthdata-explorer", "compare_coverage", {"collection_ids": ["one"]}),
        ("nasa-earthdata-explorer", "compare_coverage", {"collection_ids": ["one", "one"]}),
    ],
)
async def test_invalid_missing_or_out_of_subset_input_is_an_error(
    profile: str, tool: str, arguments: dict
) -> None:
    async with create_connected_server_and_client_session(
        build_server(profile)._mcp_server
    ) as client:
        result = await client.call_tool(tool, arguments)
        assert result.isError


@pytest.mark.parametrize(
    "profile", [p for p, definition in PROFILES.items() if definition.model_tool]
)
async def test_summary_requires_protection_gate_and_distinct_application_model_grant(profile: str):
    snapshot = Snapshot(profile)
    async with create_connected_server_and_client_session(
        build_server(profile)._mcp_server
    ) as client:
        result = await client.call_tool(
            PROFILES[profile].model_tool,
            {"record_ids": [snapshot.records[0]["id"]], "focus": "Brief evidence"},
        )
    assert result.isError
    assert "disabled until protected-backend verification" in result.content[0].text


async def test_published_statistics_and_comparisons_are_not_invented():
    async with create_connected_server_and_client_session(
        build_server("doe-energy-data")._mcp_server
    ) as client:
        result = await client.call_tool("state_energy_profile", {"state": "AL"})
        record = result.structuredContent["record"]
        assert record["generation_mwh"] == 142_921_216
        assert record["mix_mwh"]["Natural gas"] == 64_598_751
        assert record["mix_percent"]["Natural gas"] == round(64_598_751 / 142_921_216 * 100, 3)
    async with create_connected_server_and_client_session(
        build_server("usda-crop-insights")._mcp_server
    ) as client:
        result = await client.call_tool("compare_yields", {"crop": "corn"})
        assert result.structuredContent["change_bushels_per_acre"] == 7.2
        assert result.structuredContent["records"][0]["harvested_million_acres"] is None


def test_oversized_results_fail_instead_of_truncating_without_notice():
    with pytest.raises(DataError, match="bounded response"):
        Snapshot("doe-energy-data").envelope(oversized="x" * MAX_RESULT_BYTES)
