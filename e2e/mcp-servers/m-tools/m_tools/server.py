"""M-tools: three deterministic MCP tools, served over streamable HTTP at /mcp.

The server takes no authentication, which matches MOSAIC's upstream authentication None. The
gateway is its intended caller, but the server is public.

With MCP_TRANSPORT=sse the same image serves only the deprecated HTTP+SSE transport, at GET /sse
and POST /messages/. That's journey M1's negative case: MOSAIC must refuse to register it.
"""

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field, StrictFloat, StrictInt
from starlette.applications import Starlette

from m_tools.logs import RequestLog, configure_logging, tool_call

Transport = Literal["streamable-http", "sse"]
# Strict, so that true, false and other non-numbers are refused rather than converted.
Number = StrictInt | StrictFloat

_TRANSPORTS: dict[str, Transport] = {"streamable-http": "streamable-http", "sse": "sse"}
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})


def format_utc(moment: datetime) -> str:
    """ISO 8601 in UTC, to the second, such as 2026-10-05T18:06:46Z."""

    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def add_numbers(a: Number, b: Number) -> Number:
    if isinstance(a, bool) or isinstance(b, bool):
        raise ToolError("add takes two numbers, not true or false.")
    try:
        total = a + b
    except OverflowError:
        raise ToolError("The sum is too large.") from None
    if isinstance(total, float) and not math.isfinite(total):
        raise ToolError("add takes finite numbers.")
    return total


def _annotations(*, idempotent: bool) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=idempotent,
        openWorldHint=False,
    )


def build_server(*, stateless: bool = False, json_response: bool = False) -> FastMCP:
    server = FastMCP(
        "M-tools",
        instructions="Deterministic tools for MOSAIC's end-to-end tests.",
        stateless_http=stateless,
        json_response=json_response,
        # The SDK checks the Host header only for a server bound to localhost. This one is reached
        # by its public host name, directly and through the gateway, so the check would refuse
        # every real request.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(title="Echo", annotations=_annotations(idempotent=True))
    def echo(text: Annotated[str, Field(description="The text to return.")]) -> str:
        """Return the text unchanged."""

        with tool_call("echo"):
            return text

    @server.tool(title="Current UTC time", annotations=_annotations(idempotent=False))
    def utc_now() -> str:
        """Return the current UTC time in ISO 8601, to the second, such as 2026-10-05T18:06:46Z."""

        with tool_call("utc_now"):
            return format_utc(datetime.now(UTC))

    @server.tool(title="Add", annotations=_annotations(idempotent=True))
    def add(
        a: Annotated[Number, Field(description="The first number.")],
        b: Annotated[Number, Field(description="The second number.")],
    ) -> Number:
        """Return the sum of two numbers."""

        with tool_call("add"):
            return add_numbers(a, b)

    return server


def create_app(
    transport: Transport = "streamable-http",
    *,
    stateless: bool = False,
    json_response: bool = False,
) -> Starlette:
    server = build_server(stateless=stateless, json_response=json_response)
    app = server.sse_app() if transport == "sse" else server.streamable_http_app()
    app.add_middleware(RequestLog)
    return app


def _flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false.")


@dataclass(frozen=True)
class Settings:
    transport: Transport = "streamable-http"
    stateless: bool = False
    json_response: bool = False
    port: int = 8000

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        name = env.get("MCP_TRANSPORT", "streamable-http").strip().lower()
        if name not in _TRANSPORTS:
            raise ValueError(f"MCP_TRANSPORT must be one of: {', '.join(_TRANSPORTS)}.")
        port = env.get("PORT", "8000").strip()
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("PORT must be a TCP port number.")
        return cls(
            transport=_TRANSPORTS[name],
            stateless=_flag(env, "MCP_STATELESS"),
            json_response=_flag(env, "MCP_JSON_RESPONSE"),
            port=int(port),
        )


def main() -> None:
    configure_logging()
    settings = Settings.from_env(os.environ)
    app = create_app(
        settings.transport, stateless=settings.stateless, json_response=settings.json_response
    )
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
