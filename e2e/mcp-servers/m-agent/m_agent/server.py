"""M-agent: one MCP tool, ask_model, that answers by calling a governed model through MOSAIC.

Journey M9: a person's tool call leads to a governed model call, made with the server's own
application grant and attributed to that person through x-mosaic-on-behalf-of (ADR 0025).
"""

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated

import httpx
import uvicorn
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import ManagedIdentityCredential
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.applications import Starlette

from m_agent.gateway import (
    MAX_QUESTION_CHARS,
    ON_BEHALF_HEADER,
    ModelCallError,
    ModelGateway,
    ModelSettings,
)
from m_agent.logs import RequestLog, configure_logging, tool_call

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"", "0", "false", "no", "off"})
MODEL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def on_behalf_reference(context: Context) -> str | None:
    """The x-mosaic-on-behalf-of value of the MCP request this tool call arrived on.

    The value is opaque: it's passed on exactly as received, and never parsed or stored.
    """

    request = context.request_context.request
    if request is None:
        return None
    return request.headers.get(ON_BEHALF_HEADER) or None


def build_server(
    gateway: ModelGateway, *, stateless: bool = False, json_response: bool = False
) -> FastMCP:
    server = FastMCP(
        "M-agent",
        instructions="Answers short questions with a governed model, called through MOSAIC.",
        stateless_http=stateless,
        json_response=json_response,
        # The SDK checks the Host header only for a server bound to localhost. This one is reached
        # by its public host name, directly and through the gateway, so the check would refuse
        # every real request.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool(
        title="Ask the model",
        annotations=ToolAnnotations(
            readOnlyHint=True, destructiveHint=False, idempotentHint=False, openWorldHint=True
        ),
    )
    async def ask_model(
        question: Annotated[
            str,
            Field(min_length=1, max_length=MAX_QUESTION_CHARS, description="A short question."),
        ],
        # Bare Context, as the SDK's examples write it: FastMCP finds the parameter to inject the
        # request's context into by that class, and doesn't see it through type arguments.
        ctx: Context,
    ) -> str:
        """Ask a governed model a short question, and return its brief answer."""

        with tool_call("ask_model"):
            try:
                return await gateway.ask(question, on_behalf_of=on_behalf_reference(ctx))
            except ModelCallError as error:
                raise ToolError(str(error)) from None

    return server


def _flag(env: Mapping[str, str], name: str) -> bool:
    value = env.get(name, "").strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false.")


@dataclass(frozen=True)
class Settings:
    model: ModelSettings
    stateless: bool = False
    json_response: bool = False
    port: int = 8000

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        port = env.get("PORT", "8000").strip()
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError("PORT must be a TCP port number.")
        return cls(
            model=ModelSettings.from_env(env),
            stateless=_flag(env, "MCP_STATELESS"),
            json_response=_flag(env, "MCP_JSON_RESPONSE"),
            port=int(port),
        )


def create_app(
    settings: Settings,
    *,
    credential: AsyncTokenCredential | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    """The ASGI app. Tests pass a credential and a transport in place of Azure's."""

    gateway = ModelGateway(
        settings.model,
        # The container app's system-assigned identity: no client ID, so not a user-assigned one.
        credential or ManagedIdentityCredential(),
        httpx.AsyncClient(transport=transport, timeout=MODEL_TIMEOUT, follow_redirects=False),
    )
    server = build_server(
        gateway, stateless=settings.stateless, json_response=settings.json_response
    )
    app = server.streamable_http_app()
    run_sessions = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        async with run_sessions(app):
            try:
                yield
            finally:
                # The credential keeps its connections open until it's closed.
                await gateway.aclose()

    app.router.lifespan_context = lifespan
    app.add_middleware(RequestLog)
    return app


def main() -> None:
    configure_logging()
    settings = Settings.from_env(os.environ)
    uvicorn.run(
        create_app(settings),
        host="0.0.0.0",
        port=settings.port,
        log_config=None,
        access_log=False,
        server_header=False,
    )
