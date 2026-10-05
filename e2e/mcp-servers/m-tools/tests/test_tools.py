from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone

import pytest
from mcp import ClientSession
from mcp.server.fastmcp.exceptions import ToolError
from mcp.shared.memory import create_connected_server_and_client_session

from m_tools.server import add_numbers, build_server, format_utc

pytestmark = pytest.mark.anyio


@asynccontextmanager
async def connected() -> AsyncIterator[ClientSession]:
    server = build_server()
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        yield client


async def call(client: ClientSession, name: str, arguments: dict[str, object]) -> tuple[bool, str]:
    result = await client.call_tool(name, arguments)
    text = "".join(block.text for block in result.content if block.type == "text")
    return result.isError, text


async def test_lists_the_three_tools_with_their_schemas_and_annotations() -> None:
    async with connected() as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}

    assert set(tools) == {"echo", "utc_now", "add"}
    assert tools["echo"].inputSchema["required"] == ["text"]
    assert tools["echo"].inputSchema["properties"]["text"]["type"] == "string"
    assert tools["utc_now"].inputSchema["properties"] == {}
    assert tools["add"].inputSchema["required"] == ["a", "b"]
    for name in ("a", "b"):
        number = tools["add"].inputSchema["properties"][name]["anyOf"]
        assert {"type": "integer"} in number and {"type": "number"} in number
    for tool in tools.values():
        assert tool.description
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.openWorldHint is False
    assert tools["echo"].annotations.idempotentHint is True
    assert tools["add"].annotations.idempotentHint is True
    assert tools["utc_now"].annotations.idempotentHint is False


@pytest.mark.parametrize("text", ["mosaic", "", "  spaced  ", "line one\nline two", "é ✓ 漢字"])
async def test_echo_returns_the_text_unchanged(text: str) -> None:
    async with connected() as client:
        assert await call(client, "echo", {"text": text}) == (False, text)


async def test_echo_needs_its_text() -> None:
    async with connected() as client:
        is_error, _ = await call(client, "echo", {})
    assert is_error


async def test_utc_now_returns_the_current_utc_time() -> None:
    before = datetime.now(UTC).replace(microsecond=0)
    async with connected() as client:
        is_error, text = await call(client, "utc_now", {})
    after = datetime.now(UTC)

    assert not is_error
    assert text.endswith("Z")
    moment = datetime.fromisoformat(text)
    assert moment.tzinfo is not None and moment.utcoffset() == timedelta(0)
    assert before <= moment <= after


def test_format_utc_writes_iso_8601_in_utc_to_the_second() -> None:
    eastern = timezone(timedelta(hours=-4))
    assert format_utc(datetime(2026, 10, 5, 14, 6, 46, 818000, tzinfo=eastern)) == (
        "2026-10-05T18:06:46Z"
    )
    assert format_utc(datetime(2026, 1, 1, tzinfo=UTC)) == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (2, 3, "5"),
        (-7, 2, "-5"),
        (1.5, 2, "3.5"),
        (0.1, 0.2, "0.30000000000000004"),
        (10**20, 1, "100000000000000000001"),
    ],
)
async def test_add_returns_the_sum(a: float, b: float, expected: str) -> None:
    async with connected() as client:
        assert await call(client, "add", {"a": a, "b": b}) == (False, expected)


@pytest.mark.parametrize(
    "arguments",
    [{"a": True, "b": 1}, {"a": 1}, {"a": "two", "b": 3}, {"a": [1], "b": 2}, {"a": None, "b": 2}],
)
async def test_add_refuses_anything_but_two_numbers(arguments: dict[str, object]) -> None:
    async with connected() as client:
        is_error, _ = await call(client, "add", arguments)
    assert is_error


async def test_add_refuses_a_sum_that_isnt_finite() -> None:
    async with connected() as client:
        is_error, text = await call(client, "add", {"a": 1e308, "b": 1e308})
    assert is_error
    assert "finite" in text


def test_add_numbers_keeps_integers_integral() -> None:
    assert add_numbers(2, 3) == 5
    assert isinstance(add_numbers(2, 3), int)
    assert add_numbers(2.5, 0.5) == 3.0


@pytest.mark.parametrize(("a", "b"), [(True, 1), (1, False), (float("inf"), 1), (float("nan"), 0)])
def test_add_numbers_refuses_booleans_and_non_finite_numbers(a: float, b: float) -> None:
    with pytest.raises(ToolError):
        add_numbers(a, b)


def test_add_numbers_refuses_a_sum_too_large_for_a_float() -> None:
    with pytest.raises(ToolError, match="too large"):
        add_numbers(10**400, 1.0)
