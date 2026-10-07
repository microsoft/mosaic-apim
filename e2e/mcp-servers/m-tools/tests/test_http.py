"""The local smoke test: M-tools served over real HTTP, called the way MOSAIC and MCP clients do."""

import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from m_tools.server import create_app
from support import serve

pytestmark = pytest.mark.anyio

# What MOSAIC's MCP client sends: apps/api/src/mosaic_api/integrations/mcp/client.py.
MOSAIC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def initialize(version: str = "2025-11-25") -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": version,
            "capabilities": {},
            "clientInfo": {"name": "mosaic", "version": "1.0.0"},
        },
    }


def message(response: httpx.Response) -> dict[str, Any]:
    """The JSON-RPC message in a JSON or SSE response body."""

    if response.headers["content-type"].startswith("text/event-stream"):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        assert len(data) == 1
        return json.loads(data[0])
    return response.json()


@pytest.fixture(scope="module")
def origin() -> Iterator[str]:
    with serve(create_app()) as url:
        yield url


async def test_an_mcp_client_lists_and_calls_every_tool_over_streamable_http(origin: str) -> None:
    async with (
        streamable_http_client(f"{origin}/mcp") as (read, write, _),
        ClientSession(read, write) as session,
    ):
        initialized = await session.initialize()
        names = {tool.name for tool in (await session.list_tools()).tools}
        echo = await session.call_tool("echo", {"text": "through the SDK"})
        added = await session.call_tool("add", {"a": 40, "b": 2})
        now = await session.call_tool("utc_now", {})

    assert initialized.serverInfo.name == "M-tools"
    assert names == {"echo", "utc_now", "add"}
    assert echo.content[0].text == "through the SDK"
    assert added.content[0].text == "42"
    assert added.structuredContent == {"result": 42}
    assert now.content[0].text.endswith("Z")


def test_mosaic_s_handshake_negotiates_the_revision_it_offers(origin: str) -> None:
    with httpx.Client(follow_redirects=False) as client:
        started = client.post(f"{origin}/mcp", headers=MOSAIC_HEADERS, json=initialize())
        session = {"Mcp-Session-Id": started.headers["mcp-session-id"]}
        headers = {**MOSAIC_HEADERS, **session, "MCP-Protocol-Version": "2025-11-25"}
        initialized = client.post(
            f"{origin}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        listed = client.post(
            f"{origin}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        ended = client.delete(f"{origin}/mcp", headers=headers)

    assert started.status_code == 200
    result = message(started)["result"]
    assert result["protocolVersion"] == "2025-11-25"
    assert "tools" in result["capabilities"]
    assert initialized.status_code == 202
    assert listed.status_code == 200
    assert {tool["name"] for tool in message(listed)["result"]["tools"]} == {
        "echo",
        "utc_now",
        "add",
    }
    assert ended.status_code == 200


def test_a_request_without_a_session_is_refused_and_leaves_none(origin: str) -> None:
    # The MCP verifier's pooled-quota proof probes the gateway with this request, because it can't
    # leave anything on the server (scripts/verify_mcp_access.py).
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    with httpx.Client(follow_redirects=False) as client:
        refused = client.post(f"{origin}/mcp", headers=MOSAIC_HEADERS, json=ping)
        assert refused.status_code == 400
        # The SDK names the session it opened for the request, which it has already discarded.
        named = refused.headers.get("mcp-session-id")
        assert named
        headers = {**MOSAIC_HEADERS, "Mcp-Session-Id": named, "MCP-Protocol-Version": "2025-11-25"}
        gone = client.post(f"{origin}/mcp", headers=headers, json=ping)

    assert gone.status_code == 404


@pytest.mark.parametrize("version", ["2025-06-18", "2025-03-26", "2024-11-05"])
def test_an_earlier_revision_mosaic_accepts_is_negotiated_as_offered(
    origin: str, version: str
) -> None:
    response = httpx.post(f"{origin}/mcp", headers=MOSAIC_HEADERS, json=initialize(version))
    assert message(response)["result"]["protocolVersion"] == version


def test_requests_for_the_server_s_public_host_name_are_served(origin: str) -> None:
    # The SDK refuses unknown Host headers on a server it thinks is local. In Azure, every request
    # names the container app's public host.
    response = httpx.post(
        f"{origin}/mcp",
        headers={**MOSAIC_HEADERS, "Host": "m-tools.example.test"},
        json=initialize(),
    )
    assert response.status_code == 200


def test_the_endpoint_is_mcp_with_no_redirect(origin: str) -> None:
    # MOSAIC never follows a redirect, so /mcp itself must answer.
    response = httpx.post(
        f"{origin}/mcp", headers=MOSAIC_HEADERS, json=initialize(), follow_redirects=False
    )
    assert response.status_code == 200


def test_stateless_mode_needs_no_session() -> None:
    with serve(create_app(stateless=True)) as url:
        started = httpx.post(f"{url}/mcp", headers=MOSAIC_HEADERS, json=initialize())
        listed = httpx.post(
            f"{url}/mcp",
            headers=MOSAIC_HEADERS,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert started.status_code == 200
    assert "mcp-session-id" not in started.headers
    assert len(message(listed)["result"]["tools"]) == 3


def test_json_response_mode_answers_with_json() -> None:
    with serve(create_app(json_response=True)) as url:
        response = httpx.post(f"{url}/mcp", headers=MOSAIC_HEADERS, json=initialize())
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["result"]["protocolVersion"] == "2025-11-25"


@pytest.fixture(scope="module")
def sse_origin() -> Iterator[str]:
    with serve(create_app("sse")) as url:
        yield url


@pytest.mark.parametrize(("path", "status"), [("/mcp", 404), ("/sse", 405)])
def test_the_sse_only_server_refuses_streamable_http(
    sse_origin: str, path: str, status: int
) -> None:
    # MOSAIC reads 400, 404 or 405 on initialize as the deprecated HTTP+SSE transport (M1).
    response = httpx.post(f"{sse_origin}{path}", headers=MOSAIC_HEADERS, json=initialize())
    assert response.status_code == status


async def test_the_sse_only_server_works_for_an_sse_client(sse_origin: str) -> None:
    async with (
        sse_client(f"{sse_origin}/sse") as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        echo = await session.call_tool("echo", {"text": "over SSE"})
    assert echo.content[0].text == "over SSE"
