import json
import logging

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from federal_runtime.server import Settings as ServerSettings
from federal_runtime.server import create_app
from federal_runtime.shared import ModelSettings
from federal_runtime.workload import WORKLOADS, Settings, WorkloadError, run_workload
from support import FAKE_TOKEN, FakeCredential, model_env, serve

pytestmark = pytest.mark.anyio


class GatewayAuth:
    def __init__(self, app, *, refused: int | None = None):
        self.app, self.refused = app, refused
        self.requests = []

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            self.requests.append(headers)
            if self.refused:
                await PlainTextResponse("private deny reason", self.refused)(scope, receive, send)
                return
            assert headers[b"authorization"] == f"Bearer {FAKE_TOKEN}".encode()
            assert b"x-mosaic-on-behalf-of" not in headers
        await self.app(scope, receive, send)


def job_settings(profile: str, mcp_origin: str, model_origin: str) -> Settings:
    definition = WORKLOADS[profile]
    return Settings(
        profile,
        f"{mcp_origin}/mcp",
        ModelSettings(
            endpoint=f"{model_origin}/models",
            deployment=definition.deployment,
            api_version="2024-10-21",
            runtime_scope=model_env()["MOSAIC_RUNTIME_SCOPE"],
            cost_center=definition.cost_center,
            token_parameter="max_completion_tokens",
            max_tokens=256,
        ),
    )


def model_app(requests: list, status: int = 200, *, finish_reason: str = "stop") -> Starlette:
    async def complete(request: Request):
        requests.append({"headers": dict(request.headers), "body": await request.json()})
        if status != 200:
            return PlainTextResponse(f"private prompt {FAKE_TOKEN}", status)
        return JSONResponse(
            {
                "choices": [
                    {
                        "message": {"content": "A real network test completion."},
                        "finish_reason": finish_reason,
                    }
                ]
            }
        )

    return Starlette(routes=[Route("/{path:path}", complete, methods=["POST"])])


@pytest.mark.parametrize("profile", WORKLOADS)
async def test_all_five_workloads_complete_real_mcp_then_model_network_calls(profile, caplog):
    caplog.set_level(logging.INFO, logger="m_agent")
    definition = WORKLOADS[profile]
    model_requests = []
    mcp = GatewayAuth(create_app(ServerSettings(definition.mcp_profile)))
    credential = FakeCredential()
    with serve(mcp) as mcp_origin, serve(model_app(model_requests)) as model_origin:
        result = await run_workload(
            job_settings(profile, mcp_origin, model_origin), credential=credential
        )
    assert result == {
        "outcome": "ok",
        "profile": profile,
        "mode": "snapshot",
        "model_answer_chars": len("A real network test completion."),
        "mcp_tool_calls": 1,
        "model_calls": 1,
    }
    assert credential.closed
    assert len(model_requests) == 1
    assert len(mcp.requests) == 4  # initialize, initialized, tools/list, tools/call; no retries.
    for request in mcp.requests:
        assert request[b"x-mosaic-cost-center"].decode() == definition.cost_center
    request = model_requests[0]
    assert request["headers"]["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert request["headers"]["x-mosaic-cost-center"] == definition.cost_center
    assert "x-mosaic-on-behalf-of" not in request["headers"]
    assert request["body"]["model"] == definition.deployment
    assert request["body"]["max_completion_tokens"] == 256
    assert request["body"]["reasoning_effort"] == "none"
    evidence = json.loads(request["body"]["messages"][1]["content"])["evidence"]
    assert evidence["profile"] == definition.mcp_profile
    own_logs = "\n".join(
        record.getMessage() for record in caplog.records if record.name == "m_agent"
    )
    for secret in (FAKE_TOKEN, "A real network test completion.", definition.focus):
        assert secret not in own_logs
        assert secret not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_mcp_network_denial_never_calls_model_or_reports_success(status):
    model_requests = []
    credential = FakeCredential()
    mcp = GatewayAuth(create_app(ServerSettings("usda-crop-insights")), refused=status)
    with serve(mcp) as mcp_origin, serve(model_app(model_requests)) as model_origin:
        with pytest.raises(WorkloadError) as error:
            await run_workload(
                job_settings("usda-field", mcp_origin, model_origin), credential=credential
            )
    assert model_requests == []
    assert len(mcp.requests) == 1
    assert credential.closed
    assert "private" not in str(error.value)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
async def test_model_network_denial_is_not_a_success_and_is_not_retried(status):
    model_requests = []
    with (
        serve(create_app(ServerSettings("usda-crop-insights"))) as mcp_origin,
        serve(model_app(model_requests, status)) as model_origin,
    ):
        with pytest.raises(WorkloadError):
            await run_workload(
                job_settings("usda-field", mcp_origin, model_origin), credential=FakeCredential()
            )
    assert len(model_requests) == 1


async def test_nonempty_truncated_model_completion_fails_workload_without_retry():
    model_requests = []
    credential = FakeCredential()
    with (
        serve(create_app(ServerSettings("usda-crop-insights"))) as mcp_origin,
        serve(model_app(model_requests, finish_reason="length")) as model_origin,
    ):
        with pytest.raises(WorkloadError) as error:
            await run_workload(
                job_settings("usda-field", mcp_origin, model_origin), credential=credential
            )
    assert str(error.value) == "Workload failed; no success or fabricated usage is reported."
    assert len(model_requests) == 1
    assert model_requests[0]["body"]["max_completion_tokens"] == 256
    assert credential.closed


@pytest.mark.parametrize("mode", ["tool-error", "wrong-profile", "no-provenance", "empty-records"])
async def test_invalid_mcp_evidence_cannot_trigger_an_inference(mode):
    mcp = FastMCP(
        "Invalid evidence",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @mcp.tool()
    async def crop_summary(crop: str, year: int) -> dict:
        if mode == "tool-error":
            raise ToolError("private upstream error")
        return {
            "profile": "wrong" if mode == "wrong-profile" else "usda-crop-insights",
            "provenance": {}
            if mode == "no-provenance"
            else {
                "mode": "snapshot",
                "sources": [{"url": "https://www.nass.usda.gov"}],
                "release": "2025",
                "retrieved_at": "2026-10-29",
            },
            "records": [] if mode == "empty-records" else [{"id": "test"}],
        }

    requests = []
    with serve(mcp.streamable_http_app()) as mcp_origin, serve(model_app(requests)) as model_origin:
        with pytest.raises(WorkloadError):
            await run_workload(
                job_settings("usda-field", mcp_origin, model_origin), credential=FakeCredential()
            )
    assert requests == []


@pytest.mark.parametrize("profile", WORKLOADS)
def test_workload_environment_requires_production_exact_model_center_and_same_gateway(profile):
    definition = WORKLOADS[profile]
    env = model_env(
        MOSAIC_MODEL_DEPLOYMENT=definition.deployment,
        MOSAIC_COST_CENTER=definition.cost_center,
        FEDERAL_WORKLOAD_PROFILE=profile,
        FEDERAL_ENVIRONMENT="Production",
        MOSAIC_MCP_GATEWAY_URL="https://gateway.example.test/tools/agency/mcp",
    )
    assert Settings.from_env(env).profile == profile
    for changes in (
        {"FEDERAL_ENVIRONMENT": "Staging"},
        {"MOSAIC_COST_CENTER": "general"},
        {"MOSAIC_MODEL_DEPLOYMENT": "unapproved-model"},
        {"MOSAIC_MCP_GATEWAY_URL": "https://different-env.example.test/mcp"},
        {"MOSAIC_MCP_GATEWAY_URL": "http://gateway.example.test/mcp"},
        {"MOSAIC_MCP_GATEWAY_URL": "https://gateway.example.test/not-published"},
    ):
        with pytest.raises(ValueError):
            Settings.from_env(env | changes)


def test_job_main_prints_non_success_error_and_exits_nonzero(monkeypatch, capsys):
    from federal_runtime.workload import main

    monkeypatch.delenv("FEDERAL_WORKLOAD_PROFILE", raising=False)
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        assert main() == 1
        output = json.loads(capsys.readouterr().out)
        assert output["outcome"] == "error"
        assert "profile" not in output
        assert "model_calls" not in output
    finally:
        root.handlers[:], root.level = old_handlers, old_level
