import os
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import wraps
from typing import Any

import httpx
import uvicorn
from azure.core.credentials_async import AsyncTokenCredential
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.applications import Starlette

from federal_runtime.catalog import DISCLAIMER, PROFILES
from federal_runtime.data import DataError, Snapshot
from federal_runtime.model import MODEL_TIMEOUT, BriefGateway, managed_identity, settings_from_env
from federal_runtime.shared import (
    ModelCallError,
    ModelSettings,
    RequestLog,
    configure_logging,
    tool_call,
)
from federal_runtime.tools import AgencyTools


def _logged(name: str, function: Callable) -> Callable:
    @wraps(function)
    async def invoke(*args: Any, **kwargs: Any) -> Any:
        with tool_call(name):
            try:
                return await function(*args, **kwargs)
            except (DataError, ModelCallError) as error:
                raise ToolError(str(error)) from None

    return invoke


def build_server(profile: str, gateway: BriefGateway | None = None) -> FastMCP:
    definition = PROFILES[profile]
    tools = AgencyTools(Snapshot(profile), gateway)
    server = FastMCP(
        definition.name,
        instructions=f"{definition.purpose} {DISCLAIMER}",
        stateless_http=True,
        json_response=True,
        streamable_http_path="/mcp",
        # EasyAuth validates the public HTTPS host, audience, issuer and allowed caller.
        # Requests arrive through APIM, so localhost DNS-rebinding rules cannot apply.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    for name in definition.tools:
        server.add_tool(
            _logged(name, getattr(tools, name)),
            name=name,
            annotations=ToolAnnotations(
                readOnlyHint=True,
                destructiveHint=False,
                idempotentHint=name != definition.model_tool,
                openWorldHint=name == definition.model_tool,
            ),
        )
    return server


@dataclass(frozen=True)
class Settings:
    profile: str
    environment: str = "Development"
    model: ModelSettings | None = None
    port: int = 8000
    client_id: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        profile = env.get("FEDERAL_MCP_PROFILE", "")
        if profile not in PROFILES:
            raise ValueError("Set FEDERAL_MCP_PROFILE to one of the eight documented logical keys.")
        environment = env.get("FEDERAL_ENVIRONMENT", "Development")
        if environment not in {"Development", "Staging", "Production"}:
            raise ValueError("FEDERAL_ENVIRONMENT must be Development, Staging or Production.")
        port = env.get("PORT", "8000")
        if not port.isdecimal() or not 1 <= int(port) <= 65535:
            raise ValueError("PORT must be a TCP port.")
        verified = env.get("FEDERAL_BACKEND_AUTH_VERIFIED", "false").strip().lower()
        if verified not in {"true", "false"}:
            raise ValueError("FEDERAL_BACKEND_AUTH_VERIFIED must be true or false.")
        model = None
        if PROFILES[profile].model_tool and verified == "true":
            if not env.get("AZURE_CLIENT_ID", "").strip():
                raise ValueError("Set AZURE_CLIENT_ID to the MCP app's user-assigned identity.")
            model = settings_from_env(env)
            if environment != "Production" and model.cost_center == "FAA-OPS":
                raise ValueError("FAA-OPS has no non-Production model-caller grants.")
        return cls(profile, environment, model, int(port), env.get("AZURE_CLIENT_ID") or None)


def create_app(
    settings: Settings,
    *,
    credential: AsyncTokenCredential | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Starlette:
    gateway = None
    if settings.model is not None:
        gateway = BriefGateway(
            settings.model,
            credential or managed_identity({"AZURE_CLIENT_ID": settings.client_id or ""}),
            httpx.AsyncClient(transport=transport, timeout=MODEL_TIMEOUT, follow_redirects=False),
        )
    server = build_server(settings.profile, gateway)
    app = server.streamable_http_app()
    run_sessions = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        try:
            async with run_sessions(app):
                yield
        finally:
            if gateway is not None:
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


if __name__ == "__main__":
    main()
