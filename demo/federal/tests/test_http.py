import json
import logging

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from federal_runtime.catalog import PROFILES
from federal_runtime.data import Snapshot
from federal_runtime.model import settings_from_env
from federal_runtime.server import Settings, create_app
from federal_runtime.shared import ON_BEHALF_HEADER
from support import FAKE_TOKEN, FakeCredential, model_env, serve
from test_profiles import READ_CALLS

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("profile", PROFILES)
async def test_sdk_handshake_tool_list_and_read_over_real_streamable_http(profile):
    with serve(create_app(Settings(profile))) as origin:
        async with (
            httpx.AsyncClient() as http,
            streamable_http_client(f"{origin}/mcp", http_client=http) as (read, write, get_session),
            ClientSession(read, write) as session,
        ):
            initialized = await session.initialize()
            assert initialized.protocolVersion
            assert get_session() is None
            assert {tool.name for tool in (await session.list_tools()).tools} == set(
                PROFILES[profile].tools
            )
            tool, arguments = READ_CALLS[profile][0]
            result = await session.call_tool(tool, arguments)
            assert not result.isError
            assert result.structuredContent["provenance"]["mode"] == "snapshot"


@pytest.mark.parametrize(
    "profile", [p for p, definition in PROFILES.items() if definition.model_tool]
)
async def test_all_four_brief_tools_actually_call_model_with_their_own_request_attribution(profile):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "Cited brief."}}]})

    credential = FakeCredential()
    app = create_app(
        Settings(profile, model=settings_from_env(model_env())),
        credential=credential,
        transport=httpx.MockTransport(respond),
    )
    arguments = {
        "record_ids": [Snapshot(profile).records[0]["id"]],
        "focus": "Brief selected public evidence",
    }
    with serve(app) as origin:
        for reference in ("opaque/request+one", "request-two", None):
            headers = {ON_BEHALF_HEADER: reference} if reference else {}
            async with (
                httpx.AsyncClient(headers=headers) as http,
                streamable_http_client(f"{origin}/mcp", http_client=http) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                result = await session.call_tool(PROFILES[profile].model_tool, arguments)
                assert not result.isError, result
                assert result.structuredContent["summary"] == "Cited brief."
                assert result.structuredContent["citations"][0]["source_url"].startswith("https://")
    assert [r.headers.get(ON_BEHALF_HEADER) for r in requests] == [
        "opaque/request+one",
        "request-two",
        None,
    ]
    assert credential.closed
    for request in requests:
        prompt = json.loads(request.content)["messages"][1]["content"]
        assert '"mode": "snapshot"' in prompt
        assert Snapshot(profile).records[0]["id"] in prompt
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"


async def test_brief_refusal_is_an_mcp_tool_error_and_logs_exclude_sensitive_content(caplog):
    caplog.set_level(logging.INFO, logger="m_agent")
    app = create_app(
        Settings("doe-lab-research", model=settings_from_env(model_env())),
        credential=FakeCredential(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(403, text=f"private answer prompt {FAKE_TOKEN}")
        ),
    )
    with serve(app) as origin:
        async with (
            httpx.AsyncClient(headers={ON_BEHALF_HEADER: "secret-reference"}) as http,
            streamable_http_client(f"{origin}/mcp", http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool(
                "summarize_research",
                {"record_ids": ["3376627"], "focus": "private focus wording"},
            )
    assert result.isError
    assert "HTTP 403" in result.content[0].text
    own_logs = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "m_agent"
    )
    for secret in ("private focus", "private answer", FAKE_TOKEN, "secret-reference"):
        assert secret not in own_logs
        assert secret not in str(result)


async def test_invalid_summary_input_cannot_spend_model_tokens():
    requests = []
    app = create_app(
        Settings("doe-lab-research", model=settings_from_env(model_env())),
        credential=FakeCredential(),
        transport=httpx.MockTransport(lambda request: requests.append(request)),
    )
    with serve(app) as origin:
        async with (
            httpx.AsyncClient() as http,
            streamable_http_client(f"{origin}/mcp", http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            for arguments in (
                {"record_ids": [], "focus": "brief"},
                {"record_ids": ["3376627"] * 4, "focus": "brief"},
                {"record_ids": ["3376627"], "focus": "x" * 161},
                {"record_ids": ["not-in-snapshot"], "focus": "brief"},
            ):
                result = await session.call_tool("summarize_research", arguments)
                assert result.isError
    assert requests == []
