"""Smoke checks for the deployed Phase 11 MCP test servers.

Standard library only, so it runs with any Python 3.12 or later:

    python smoke.py tools https://<m-tools-host>/mcp
    python smoke.py sse https://<m-tools-sse-host>/sse
    python smoke.py protected https://<m-protected-host>/runtime/webhooks/mcp
    python smoke.py agent https://<m-agent-host>/mcp

deploy.py runs all four against the deployment's outputs. The checks speak MCP the way MOSAIC's
own client does: streamable HTTP, protocol revision 2025-11-25 offered, and redirects never
followed. They never send a credential and never call ask_model, so they spend no model quota.
They print check names, tool names and HTTP statuses, never a URL, header value or session ID.
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.parse import urlsplit, urlunsplit

OFFERED_PROTOCOL_VERSION = "2025-11-25"
# The revisions MOSAIC accepts, and the statuses its client takes to mean "not streamable HTTP".
MOSAIC_PROTOCOL_VERSIONS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})
UNSUPPORTED_TRANSPORT_STATUSES = frozenset({400, 404, 405})
ACCEPT = "application/json, text/event-stream"
TOOLS = ("echo", "utc_now", "add")
AGENT_TOOLS = ("ask_model",)
MAX_BYTES = 1024 * 1024
TIMEOUT_SECONDS = 60.0
_ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class SmokeFailure(Exception):
    pass


@dataclass(frozen=True)
class Reply:
    status: int
    headers: Message
    body: bytes

    @property
    def content_type(self) -> str:
        return (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        # MOSAIC never follows a redirect, so a server that needs one fails here as it would there.
        return None


_OPENER = urllib.request.build_opener(_RefuseRedirects)


def checked_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in _LOCAL_HOSTS):
        if parts.username or parts.password or parts.fragment:
            raise SmokeFailure("The URL must not carry credentials or a fragment.")
        return url
    raise SmokeFailure("The URL must use https, or http to this machine.")


def send(
    method: str, url: str, *, headers: Mapping[str, str] | None = None, body: bytes | None = None
) -> Reply:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            return Reply(response.status, response.headers, response.read(MAX_BYTES))
    except urllib.error.HTTPError as error:
        with error:
            return Reply(error.code, error.headers, error.read(MAX_BYTES))
    except (urllib.error.URLError, TimeoutError) as error:
        raise SmokeFailure(f"The server couldn't be reached ({type(error).__name__}).") from None


def sse_data(text: str) -> Iterator[str]:
    """The data of each event in a server-sent event stream."""

    data: list[str] = []
    for line in [*text.splitlines(), ""]:
        if not line:
            if data:
                yield "\n".join(data)
                data = []
            continue
        name, _, value = line.partition(":")
        if name == "data":
            data.append(value.removeprefix(" "))


def rpc_result(reply: Reply, request_id: int) -> dict[str, Any]:
    if reply.content_type == "text/event-stream":
        candidates = [json.loads(data) for data in sse_data(reply.body.decode("utf-8"))]
    else:
        candidates = [json.loads(reply.body)]
    for message in candidates:
        if isinstance(message, dict) and message.get("id") == request_id:
            error = message.get("error")
            if isinstance(error, dict):
                raise SmokeFailure(f"The server answered with JSON-RPC error {error.get('code')}.")
            result = message.get("result")
            if isinstance(result, dict):
                return result
    raise SmokeFailure("The server didn't answer the request.")


class Session:
    """One MCP conversation over streamable HTTP, as MOSAIC's client holds it."""

    def __init__(self, url: str, *, headers: Mapping[str, str] | None = None) -> None:
        self.url = checked_url(url)
        self.headers = dict(headers or {})
        self.session_id: str | None = None
        self.protocol_version: str | None = None
        self._next_id = 0

    def post(self, payload: dict[str, Any]) -> Reply:
        headers = {"Accept": ACCEPT, "Content-Type": "application/json", **self.headers}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.protocol_version and self.protocol_version >= "2025-06-18":
            headers["MCP-Protocol-Version"] = self.protocol_version
        return send("POST", self.url, headers=headers, body=json.dumps(payload).encode("utf-8"))

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        reply = self.post(payload)
        if reply.status != 200:
            raise SmokeFailure(f"{method} returned HTTP {reply.status}.")
        if method == "initialize":
            self.session_id = reply.headers.get("Mcp-Session-Id")
        return rpc_result(reply, self._next_id)

    def initialize(self) -> str:
        result = self.request("initialize", _initialize_payload()["params"])
        version = result.get("protocolVersion")
        if version not in MOSAIC_PROTOCOL_VERSIONS:
            raise SmokeFailure("The server negotiated a protocol revision MOSAIC doesn't accept.")
        if "tools" not in (result.get("capabilities") or {}):
            raise SmokeFailure("The server doesn't offer tools.")
        self.protocol_version = str(version)
        reply = self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        if reply.status not in {200, 202, 204}:
            raise SmokeFailure(f"notifications/initialized returned HTTP {reply.status}.")
        return self.protocol_version

    def tool_names(self) -> list[str]:
        tools = self.request("tools/list", {}).get("tools") or []
        return [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]

    def call_text(self, name: str, arguments: dict[str, Any]) -> str:
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise SmokeFailure(f"{name} returned a tool error.")
        texts = [
            block.get("text") for block in result.get("content") or [] if isinstance(block, dict)
        ]
        return "".join(text for text in texts if isinstance(text, str))

    def close(self) -> None:
        if not self.session_id:
            return
        headers = {"Mcp-Session-Id": self.session_id, **self.headers}
        try:
            send("DELETE", self.url, headers=headers)
        except SmokeFailure:
            pass
        self.session_id = None


Report = Callable[[str], None]


def _expect_tools(session: Session, expected: tuple[str, ...], say: Report) -> None:
    names = session.tool_names()
    missing = [name for name in expected if name not in names]
    if missing:
        raise SmokeFailure(f"tools/list is missing {', '.join(missing)}.")
    say(f"tools/list names {', '.join(expected)}")


def check_tools(url: str, say: Report) -> None:
    session = Session(url)
    try:
        say(f"initialize negotiated {session.initialize()}")
        _expect_tools(session, TOOLS, say)
        if session.call_text("echo", {"text": "mosaic smoke"}) != "mosaic smoke":
            raise SmokeFailure("echo didn't return its text.")
        say("echo returned its text")
        if session.call_text("add", {"a": 2, "b": 3}) != "5":
            raise SmokeFailure("add(2, 3) didn't return 5.")
        say("add(2, 3) returned 5")
        if not _ISO_UTC.fullmatch(session.call_text("utc_now", {})):
            raise SmokeFailure("utc_now didn't return an ISO 8601 UTC time.")
        say("utc_now returned an ISO 8601 UTC time")
    finally:
        session.close()


def check_agent(url: str, say: Report) -> None:
    session = Session(url)
    try:
        say(f"initialize negotiated {session.initialize()}")
        _expect_tools(session, AGENT_TOOLS, say)
    finally:
        session.close()


def _initialize_payload() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": OFFERED_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mosaic-smoke", "version": "1.0.0"},
        },
    }


def _initialize_status(url: str, headers: Mapping[str, str] | None = None) -> int:
    return Session(url, headers=headers).post(_initialize_payload()).status


def check_sse_only(url: str, say: Report) -> None:
    for label, target in (("its SSE URL", url), ("/mcp", _sibling(url, "/mcp"))):
        status = _initialize_status(target)
        if status not in UNSUPPORTED_TRANSPORT_STATUSES:
            raise SmokeFailure(
                f"A streamable HTTP initialize to {label} returned HTTP {status}, which MOSAIC "
                "wouldn't read as an unsupported transport."
            )
        say(f"a streamable HTTP initialize to {label} returned HTTP {status}")
    if not _opens_sse_stream(checked_url(url)):
        raise SmokeFailure("GET didn't open an SSE stream that names a message endpoint.")
    say("GET opened an SSE stream that names a message endpoint")


def _sibling(url: str, path: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _opens_sse_stream(url: str) -> bool:
    request = urllib.request.Request(url, method="GET", headers={"Accept": "text/event-stream"})
    try:
        with _OPENER.open(request, timeout=TIMEOUT_SECONDS) as response:
            if not (response.headers.get("Content-Type") or "").startswith("text/event-stream"):
                return False
            for _ in range(20):
                line = response.readline(MAX_BYTES).decode("utf-8").strip()
                if line == "event: endpoint":
                    return True
    except (urllib.error.URLError, TimeoutError):
        return False
    return False


def check_protected(url: str, say: Report) -> None:
    for label, headers in (
        ("without a token", {}),
        ("with a token that isn't one", {"Authorization": "Bearer not-a-token"}),
    ):
        status = _initialize_status(url, headers)
        if status != 401:
            raise SmokeFailure(f"An initialize {label} returned HTTP {status}, not 401.")
        say(f"an initialize {label} returned HTTP 401")


CHECKS: dict[str, Callable[[str, Report], None]] = {
    "tools": check_tools,
    "sse": check_sse_only,
    "protected": check_protected,
    "agent": check_agent,
}


def run(name: str, url: str, say: Report = print) -> bool:
    """Run one server's checks, reporting each step. True when every check passed."""

    try:
        CHECKS[name](url, lambda line: say(f"PASS {name}: {line}"))
    except SmokeFailure as failure:
        say(f"FAIL {name}: {failure}")
        return False
    except (ValueError, UnicodeDecodeError):
        say(f"FAIL {name}: the server answered with something that isn't MCP.")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-check one deployed MCP test server.")
    parser.add_argument("server", choices=sorted(CHECKS))
    parser.add_argument("url", help="The server's MCP URL, or the SSE-only server's /sse URL.")
    arguments = parser.parse_args(argv)
    return 0 if run(arguments.server, arguments.url) else 1


if __name__ == "__main__":
    sys.exit(main())
