"""The deployment kit's smoke checks, run against M-tools on this machine."""

from collections.abc import Iterator
from types import ModuleType

import pytest

from m_tools.server import create_app
from support import serve


@pytest.fixture(scope="module")
def tools_origin() -> Iterator[str]:
    with serve(create_app()) as url:
        yield url


@pytest.fixture(scope="module")
def sse_origin() -> Iterator[str]:
    with serve(create_app("sse")) as url:
        yield url


def run(smoke: ModuleType, name: str, url: str) -> tuple[bool, list[str]]:
    lines: list[str] = []
    passed = smoke.run(name, url, lines.append)
    return passed, lines


def test_the_tools_checks_pass_against_m_tools(smoke: ModuleType, tools_origin: str) -> None:
    passed, lines = run(smoke, "tools", f"{tools_origin}/mcp")
    assert passed, lines
    assert lines == [
        "PASS tools: initialize negotiated 2025-11-25",
        "PASS tools: tools/list names echo, utc_now, add",
        "PASS tools: echo returned its text",
        "PASS tools: add(2, 3) returned 5",
        "PASS tools: utc_now returned an ISO 8601 UTC time",
    ]


def test_the_sse_checks_pass_against_the_sse_only_variant(
    smoke: ModuleType, sse_origin: str
) -> None:
    passed, lines = run(smoke, "sse", f"{sse_origin}/sse")
    assert passed, lines
    assert lines == [
        "PASS sse: a streamable HTTP initialize to its SSE URL returned HTTP 405",
        "PASS sse: a streamable HTTP initialize to /mcp returned HTTP 404",
        "PASS sse: GET opened an SSE stream that names a message endpoint",
    ]


def test_the_tools_checks_fail_against_the_sse_only_variant(
    smoke: ModuleType, sse_origin: str
) -> None:
    passed, lines = run(smoke, "tools", f"{sse_origin}/sse")
    assert not passed
    assert lines == ["FAIL tools: initialize returned HTTP 405."]


def test_the_sse_checks_fail_against_a_streamable_server(
    smoke: ModuleType, tools_origin: str
) -> None:
    passed, lines = run(smoke, "sse", f"{tools_origin}/mcp")
    assert not passed
    assert lines[-1].startswith("FAIL sse: A streamable HTTP initialize to its SSE URL returned")


def test_the_protected_checks_fail_against_a_server_that_takes_no_token(
    smoke: ModuleType, tools_origin: str
) -> None:
    passed, lines = run(smoke, "protected", f"{tools_origin}/mcp")
    assert not passed
    assert lines == ["FAIL protected: An initialize without a token returned HTTP 200, not 401."]


def test_the_agent_checks_fail_without_ask_model(smoke: ModuleType, tools_origin: str) -> None:
    passed, lines = run(smoke, "agent", f"{tools_origin}/mcp")
    assert not passed
    assert lines[-1] == "FAIL agent: tools/list is missing ask_model."


def test_the_checks_never_print_the_server_s_address(smoke: ModuleType, tools_origin: str) -> None:
    _, lines = run(smoke, "tools", f"{tools_origin}/mcp")
    port = tools_origin.rsplit(":", 1)[1]
    assert not any("127.0.0.1" in line or port in line for line in lines)


@pytest.mark.parametrize(
    "url",
    [
        "http://m-tools.example.test/mcp",
        "https://user:secret@m-tools.example.test/mcp",
        "https://m-tools.example.test/mcp#fragment",
        "ftp://m-tools.example.test/mcp",
    ],
)
def test_the_checks_refuse_cleartext_remote_and_credential_urls(
    smoke: ModuleType, url: str
) -> None:
    passed, lines = run(smoke, "tools", url)
    assert not passed
    assert lines[0].startswith("FAIL tools: The URL must")


def test_sse_data_joins_each_event_s_data_lines(smoke: ModuleType) -> None:
    stream = ': comment\nevent: message\ndata: {"a":\ndata: 1}\n\ndata: second\n'
    assert list(smoke.sse_data(stream)) == ['{"a":\n1}', "second"]
