"""Five manual Production workflows: published MCP evidence, then one real governed completion."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from federal_runtime.data import MAX_RESULT_BYTES
from federal_runtime.model import MODEL_TIMEOUT, BriefGateway, managed_identity, settings_from_env
from federal_runtime.shared import COST_CENTER_HEADER, ModelSettings, configure_logging


@dataclass(frozen=True)
class Workload:
    name: str
    cost_center: str
    deployment: str
    mcp_profile: str
    tool: str
    arguments: dict[str, Any]
    focus: str


WORKLOADS = {
    "nasa-brief": Workload(
        "NASA Mission Briefing",
        "NASA-MISSION",
        "gpt-5.6-terra",
        "nasa-mission-archive",
        "search_missions",
        {"query": "Artemis", "limit": 3},
        "Create a cited historical Artemis I briefing using only the supplied NASA media metadata.",
    ),
    "faa-airspace": Workload(
        "FAA Airspace Briefing",
        "FAA-OPS",
        "gpt-5.6-luna",
        "faa-airport-reference",
        "airport_profile",
        {"airport_id": "IAD"},
        "Create a clearly non-operational historical airport-reference briefing. "
        "No flight planning, navigation, NOTAM or safety-critical advice.",
    ),
    "faa-standards": Workload(
        "FAA Flight Standards Review",
        "FAA-RND",
        "gpt-5.6-luna",
        "faa-regulatory-reference",
        "lookup_citation",
        {"section": "107.31"},
        "Draft a cited educational review checklist for the selected dated Title 14 reference. "
        "Not legal advice, flight planning or operational/safety instructions.",
    ),
    "doe-research": Workload(
        "DOE Lab Research",
        "DOE-LABS",
        "gpt-5.1",
        "doe-lab-research",
        "search_research",
        {"query": "solar", "limit": 3},
        "Draft a cited research-discovery summary. Bibliographic metadata only: "
        "do not claim to have read papers or infer experimental results.",
    ),
    "usda-field": Workload(
        "USDA Field Insights",
        "USDA-DATA",
        "gpt-5.6-luna",
        "usda-crop-insights",
        "crop_summary",
        {"crop": "corn", "year": 2025},
        "Summarize the dated national crop estimates with units and citations. "
        "No farm-specific conclusions, forecast, benefit or eligibility decision.",
    ),
}


class WorkloadError(Exception):
    """A safe workload failure. Never echo SDK/HTTP exception messages."""


@dataclass(frozen=True)
class Settings:
    profile: str
    mcp_url: str
    model: ModelSettings
    client_id: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        profile = env.get("FEDERAL_WORKLOAD_PROFILE", "")
        if profile not in WORKLOADS:
            raise ValueError("Set FEDERAL_WORKLOAD_PROFILE to a documented manual job key.")
        if env.get("FEDERAL_ENVIRONMENT") != "Production":
            raise ValueError("Federal workload jobs may run only in Production.")
        if not env.get("AZURE_CLIENT_ID", "").strip():
            raise ValueError("Set AZURE_CLIENT_ID to this job's own user-assigned identity.")
        model = settings_from_env(env)
        workload = WORKLOADS[profile]
        if model.cost_center != workload.cost_center or model.deployment != workload.deployment:
            raise ValueError(
                "Workload model deployment/cost center does not match its approved profile."
            )
        mcp_url = env.get("MOSAIC_MCP_GATEWAY_URL", "")
        parts = urlsplit(mcp_url)
        model_parts = urlsplit(model.endpoint)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.netloc != model_parts.netloc
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.path.rstrip("/").endswith("/mcp")
        ):
            raise ValueError(
                "MOSAIC_MCP_GATEWAY_URL must be the same Production APIM host's published /mcp URL."
            )
        return cls(profile, mcp_url, model, env.get("AZURE_CLIENT_ID") or None)


class BoundedStream(httpx.AsyncByteStream):
    def __init__(self, response: httpx.Response) -> None:
        self.response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self.response.aiter_bytes():
            total += len(chunk)
            if total > 64_000:
                raise WorkloadError("MCP gateway response exceeded the bounded payload limit.")
            yield chunk

    async def aclose(self) -> None:
        await self.response.aclose()


class BoundedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.inner = inner or httpx.AsyncHTTPTransport(retries=0)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.inner.handle_async_request(request)
        if response.status_code not in {200, 202}:
            await response.aclose()
            raise WorkloadError(f"MCP gateway refused the call (HTTP {response.status_code}).")
        headers = dict(response.headers)
        # Bound decoded bytes, including when a gateway compresses its JSON/SSE response.
        headers.pop("content-encoding", None)
        headers.pop("content-length", None)
        return httpx.Response(
            response.status_code,
            headers=headers,
            stream=BoundedStream(response),
            extensions=response.extensions,
        )

    async def aclose(self) -> None:
        await self.inner.aclose()


def _evidence(result: Any, expected_profile: str) -> dict[str, Any]:
    if result.isError:
        raise WorkloadError("Published MCP tool returned an error; no model call was made.")
    value = result.structuredContent
    if value is None:
        text = [block.text for block in result.content if block.type == "text"]
        if len(text) != 1 or len(text[0].encode("utf-8")) > MAX_RESULT_BYTES:
            raise WorkloadError("Published MCP result is missing or oversized.")
        try:
            value = json.loads(text[0])
        except ValueError:
            raise WorkloadError(
                "Published MCP result was not structured public evidence."
            ) from None
    if not isinstance(value, dict) or len(json.dumps(value).encode("utf-8")) > MAX_RESULT_BYTES:
        raise WorkloadError("Published MCP result is invalid or oversized.")
    provenance = value.get("provenance")
    if (
        value.get("profile") != expected_profile
        or not isinstance(provenance, dict)
        or provenance.get("mode") != "snapshot"
        or not provenance.get("sources")
        or not provenance.get("release")
        or not provenance.get("retrieved_at")
        or not (value.get("record") or value.get("records"))
    ):
        raise WorkloadError("Published MCP result has no matching public snapshot provenance.")
    return value


async def run_workload(
    settings: Settings,
    *,
    credential: AsyncTokenCredential | None = None,
    mcp_transport: httpx.AsyncBaseTransport | None = None,
    model_transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    workload = WORKLOADS[settings.profile]
    identity = credential or managed_identity({"AZURE_CLIENT_ID": settings.client_id or ""})
    gateway = BriefGateway(
        settings.model,
        identity,
        httpx.AsyncClient(transport=model_transport, timeout=MODEL_TIMEOUT, follow_redirects=False),
    )
    try:
        async with asyncio.timeout(90):
            token = (await identity.get_token(settings.model.runtime_scope)).token
            if not token:
                raise WorkloadError("Managed identity returned an empty runtime token.")
            headers = {
                "Authorization": f"Bearer {token}",
                COST_CENTER_HEADER: workload.cost_center,
            }
            async with (
                httpx.AsyncClient(
                    headers=headers,
                    timeout=httpx.Timeout(30.0, connect=10.0),
                    follow_redirects=False,
                    transport=BoundedTransport(mcp_transport),
                ) as http,
                streamable_http_client(
                    settings.mcp_url, http_client=http, terminate_on_close=False
                ) as (read, write, _),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                available = {tool.name for tool in (await session.list_tools()).tools}
                if workload.tool not in available:
                    raise WorkloadError(
                        "Expected read-only tool is not published by this MCP gateway."
                    )
                result = await session.call_tool(workload.tool, workload.arguments)
                evidence = _evidence(result, workload.mcp_profile)
            prompt = json.dumps({"focus": workload.focus, "evidence": evidence}, ensure_ascii=False)
            # A job calls as itself. It must not manufacture a person's attribution header.
            answer = await gateway.ask(prompt)
            return {
                "outcome": "ok",
                "profile": settings.profile,
                "mode": evidence["provenance"]["mode"],
                "model_answer_chars": len(answer),
                "mcp_tool_calls": 1,
                "model_calls": 1,
            }
    except Exception:
        # MCP task groups and transport errors may include response bodies, URLs or tokens.
        # Never print them, nor produce a success-shaped placeholder.
        raise WorkloadError(
            "Workload failed; no success or fabricated usage is reported."
        ) from None
    finally:
        await gateway.aclose()


def main() -> int:
    configure_logging()
    try:
        settings = Settings.from_env(os.environ)
        result = asyncio.run(run_workload(settings))
    except (ValueError, WorkloadError):
        print(json.dumps({"outcome": "error", "reason": "configuration-or-runtime-failure"}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
