"""ask_model end to end on this machine: MCP over real HTTP in, a mocked gateway out."""

import json
import logging
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from m_agent.gateway import ON_BEHALF_HEADER
from m_agent.logs import LOGGER_NAME
from m_agent.server import create_app
from support import FAKE_TOKEN, FakeCredential, FakeGateway, serve, settings

pytestmark = pytest.mark.anyio

MOSAIC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}
FIRST = "0f8fad5b-d9cb-469f-a165-70867728950e"
SECOND = "7c9e6679-7425-40de-944b-e07fc1f90ae7"


def rpc(response: httpx.Response) -> dict[str, Any]:
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])
    return response.json()


class Running:
    def __init__(self, origin: str, gateway: FakeGateway, credential: FakeCredential) -> None:
        self.origin = origin
        self.gateway = gateway
        self.credential = credential


@pytest.fixture
def running() -> Iterator[Running]:
    gateway, credential = FakeGateway(), FakeCredential()
    app = create_app(settings(), credential=credential, transport=gateway.transport)
    with serve(app) as origin:
        yield Running(origin, gateway, credential)


class RawSession:
    """MCP over plain HTTP, so each request can carry its own headers, as the gateway's do."""

    def __init__(self, client: httpx.Client, url: str) -> None:
        self.client, self.url = client, url
        started = client.post(
            url,
            headers=MOSAIC_HEADERS,
            json=self.message(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "gateway-test", "version": "1.0.0"},
                },
            ),
        )
        self.headers = {**MOSAIC_HEADERS, "Mcp-Session-Id": started.headers["mcp-session-id"]}
        client.post(
            url,
            headers=self.headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )

    @staticmethod
    def message(request_id: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

    def ask(self, request_id: int, question: str, reference: str | None) -> dict[str, Any]:
        headers = dict(self.headers)
        if reference is not None:
            headers[ON_BEHALF_HEADER] = reference
        params = {"name": "ask_model", "arguments": {"question": question}}
        response = self.client.post(
            self.url, headers=headers, json=self.message(request_id, "tools/call", params)
        )
        return rpc(response)["result"]


async def test_an_mcp_client_calls_ask_model_and_gets_the_model_s_answer(running: Running) -> None:
    async with (
        httpx.AsyncClient(headers={ON_BEHALF_HEADER: FIRST}) as http,
        streamable_http_client(f"{running.origin}/mcp", http_client=http) as (read, write, _),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = (await session.list_tools()).tools
        result = await session.call_tool(
            "ask_model", {"question": "What is the capital of France?"}
        )

    assert [tool.name for tool in tools] == ["ask_model"]
    assert tools[0].inputSchema["required"] == ["question"]
    assert tools[0].annotations is not None and tools[0].annotations.openWorldHint is True
    assert not result.isError
    assert result.content[0].text == "Paris."
    [model_call] = running.gateway.requests
    assert model_call.headers[ON_BEHALF_HEADER] == FIRST
    assert (
        json.loads(model_call.content)["messages"][1]["content"] == "What is the capital of France?"
    )


def test_each_tool_call_passes_on_its_own_request_s_value_and_never_invents_one(
    running: Running,
) -> None:
    with httpx.Client() as client:
        session = RawSession(client, f"{running.origin}/mcp")
        session.ask(2, "One?", FIRST)
        session.ask(3, "Two?", SECOND)
        session.ask(4, "Three?", None)

    sent = [request.headers.get_list(ON_BEHALF_HEADER) for request in running.gateway.requests]
    assert sent == [[FIRST], [SECOND], []]


def test_a_refused_model_call_is_a_tool_error_naming_the_status_and_reason() -> None:
    gateway = FakeGateway(lambda request: httpx.Response(403, text="Model access denied."))
    app = create_app(settings(), credential=FakeCredential(), transport=gateway.transport)
    with serve(app) as origin, httpx.Client() as client:
        result = RawSession(client, f"{origin}/mcp").ask(2, "Hi?", FIRST)

    assert result["isError"] is True
    [text] = [block["text"] for block in result["content"]]
    assert text.endswith("The model call failed with HTTP 403: Model access denied.")
    assert FAKE_TOKEN not in json.dumps(result)


def test_a_question_that_s_too_long_is_refused_before_any_model_call(running: Running) -> None:
    with httpx.Client() as client:
        result = RawSession(client, f"{running.origin}/mcp").ask(2, "x" * 501, FIRST)
    assert result["isError"] is True
    assert running.gateway.requests == []


def test_the_server_logs_no_question_answer_reference_or_token(
    running: Running, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with httpx.Client() as client:
        RawSession(client, f"{running.origin}/mcp").ask(2, "A private question?", FIRST)

    own = [entry.getMessage() for entry in caplog.records if entry.name == LOGGER_NAME]
    assert any(text.startswith("model_call status=200 duration_ms=") for text in own)
    assert any(text.startswith("tool=ask_model outcome=ok duration_ms=") for text in own)
    for text in own:
        assert re.fullmatch(
            r"request status=\d{3} duration_ms=\d+"
            r"|tool=ask_model outcome=(ok|error) duration_ms=\d+"
            r"|model_call status=(\d{3}|timeout|unreachable) duration_ms=\d+",
            text,
        ), text
    every_message = "\n".join(entry.getMessage() for entry in caplog.records)
    for private in ("A private question?", "Paris", FIRST, FAKE_TOKEN, "gateway.example.test"):
        assert private not in every_message


def test_stopping_the_server_closes_the_credential() -> None:
    credential = FakeCredential()
    with serve(create_app(settings(), credential=credential, transport=FakeGateway().transport)):
        assert not credential.closed
    assert credential.closed
