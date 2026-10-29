import io
import json
import logging

import httpx
import pytest

from federal_runtime.model import BriefGateway, managed_identity, settings_from_env
from federal_runtime.server import Settings
from federal_runtime.shared import ON_BEHALF_HEADER, ModelCallError, configure_logging
from support import FAKE_TOKEN, SCOPE, FakeCredential, model_env

pytestmark = pytest.mark.anyio


def answer(text: str = "Cited public snapshot brief.") -> dict:
    return {"choices": [{"message": {"content": text}}]}


async def test_model_calls_are_mi_authenticated_bounded_and_attribution_is_request_local():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=answer())

    credential = FakeCredential()
    gateway = BriefGateway(
        settings_from_env(model_env()),
        credential,
        httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    for reference in ("opaque/one+not-a-guid", "opaque-two", None):
        assert await gateway.ask("Public evidence", on_behalf_of=reference)
    await gateway.aclose()
    assert credential.closed
    assert credential.scopes == [(SCOPE,)] * 3
    assert [r.headers.get(ON_BEHALF_HEADER) for r in requests] == [
        "opaque/one+not-a-guid",
        "opaque-two",
        None,
    ]
    for request in requests:
        assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
        assert request.headers["x-mosaic-cost-center"] == "FAA-RND"
        assert request.url.params["api-version"] == "2024-10-21"
        payload = json.loads(request.content)
        assert payload["max_completion_tokens"] == 256
        assert payload["reasoning_effort"] == "none"
        assert payload["stream"] is False
        assert payload["messages"][1]["content"] == "Public evidence"


@pytest.mark.parametrize("status", [301, 401, 403, 429, 500, 503])
async def test_model_gateway_denials_are_errors_and_no_upstream_details_escape(status: int):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(status, text=f"private prompt {FAKE_TOKEN} private-answer")

    gateway = BriefGateway(
        settings_from_env(model_env()),
        FakeCredential(),
        httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(ModelCallError) as error:
        await gateway.ask("Public evidence")
    assert str(status) in str(error.value)
    assert "private" not in str(error.value)
    assert len(requests) == 1
    await gateway.aclose()


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not JSON"),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json=answer("")),
        httpx.Response(200, json=answer("x" * 4001)),
        httpx.Response(200, content=b"x" * 64001),
    ],
)
async def test_invalid_or_oversized_model_results_never_become_success(response):
    gateway = BriefGateway(
        settings_from_env(model_env()),
        FakeCredential(),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)),
    )
    with pytest.raises(ModelCallError):
        await gateway.ask("Public evidence")
    await gateway.aclose()


@pytest.mark.parametrize("credential", [FakeCredential(token=""), FakeCredential(fail=True)])
async def test_no_anonymous_fallback_when_mi_token_is_missing(credential):
    requests = []
    gateway = BriefGateway(
        settings_from_env(model_env()),
        credential,
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: requests.append(request))),
    )
    with pytest.raises(ModelCallError):
        await gateway.ask("Public evidence")
    assert requests == []
    await gateway.aclose()


@pytest.mark.parametrize("question", ["", " ", "x" * 12001])
async def test_model_input_is_bounded_before_identity_or_network(question):
    credential = FakeCredential()
    gateway = BriefGateway(settings_from_env(model_env()), credential, httpx.AsyncClient())
    with pytest.raises(ModelCallError):
        await gateway.ask(question)
    assert credential.scopes == []
    await gateway.aclose()


async def test_model_network_errors_are_safe():
    def fail(request):
        raise httpx.ReadTimeout("private endpoint and prompt")

    gateway = BriefGateway(
        settings_from_env(model_env()),
        FakeCredential(),
        httpx.AsyncClient(transport=httpx.MockTransport(fail)),
    )
    with pytest.raises(ModelCallError, match="timed out") as error:
        await gateway.ask("Public evidence")
    assert "private" not in str(error.value)
    await gateway.aclose()


def test_logging_reuses_existing_safe_filter_and_withholds_sdk_secrets():
    stream = io.StringIO()
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    try:
        configure_logging(stream)
        logging.getLogger("mcp.server").warning("private sessionID token prompt answer")
        logging.getLogger("httpx").warning("https://private.endpoint private token")
        value = stream.getvalue()
        assert "message withheld" in value
        for secret in ("sessionID", "token", "prompt", "answer", "private.endpoint"):
            assert secret not in value
    finally:
        root.handlers[:], root.level = old_handlers, old_level


@pytest.mark.parametrize(
    "changes",
    [
        {"MOSAIC_MODEL_ENDPOINT": "http://insecure.test"},
        {"MOSAIC_MODEL_ENDPOINT": "https://account.openai.azure.com"},
        {"MOSAIC_MODEL_ENDPOINT": "https://user:pass@gateway.test"},
        {"MOSAIC_RUNTIME_SCOPE": "https://cognitiveservices.azure.com/.default"},
        {"MOSAIC_MODEL_MAX_TOKENS": "257"},
        {"MOSAIC_MODEL_MAX_TOKENS": "0"},
        {"MOSAIC_COST_CENTER": ""},
        {"MOSAIC_MODEL_TOKEN_PARAMETER": "unbounded"},
    ],
)
def test_invalid_model_configuration_rejected(changes):
    with pytest.raises(ValueError):
        settings_from_env(model_env(**changes))


def test_protection_verification_is_explicit_and_read_only_profiles_need_no_model_identity():
    settings = Settings.from_env({"FEDERAL_MCP_PROFILE": "doe-lab-research"})
    assert settings.model is None
    settings = Settings.from_env({"FEDERAL_MCP_PROFILE": "doe-energy-data"})
    assert settings.model is None
    with pytest.raises(ValueError):
        Settings.from_env(
            {"FEDERAL_MCP_PROFILE": "doe-lab-research", "FEDERAL_BACKEND_AUTH_VERIFIED": "true"}
        )


def test_managed_identity_always_selects_the_deployments_user_assigned_client_id(monkeypatch):
    seen = []
    sentinel = object()

    def credential(**kwargs):
        seen.append(kwargs)
        return sentinel

    monkeypatch.setattr("federal_runtime.model.ManagedIdentityCredential", credential)
    client_id = model_env()["AZURE_CLIENT_ID"]
    assert managed_identity({"AZURE_CLIENT_ID": client_id}) is sentinel
    assert seen == [{"client_id": client_id}]
    with pytest.raises(ValueError, match="AZURE_CLIENT_ID"):
        managed_identity({})


def test_verified_summary_configuration_requires_its_precreated_user_assigned_identity():
    env = {
        **model_env(),
        "FEDERAL_MCP_PROFILE": "doe-lab-research",
        "FEDERAL_BACKEND_AUTH_VERIFIED": "true",
    }
    assert Settings.from_env(env).client_id == env["AZURE_CLIENT_ID"]
    env.pop("AZURE_CLIENT_ID")
    with pytest.raises(ValueError, match="AZURE_CLIENT_ID"):
        Settings.from_env(env)
    with pytest.raises(ValueError, match="FAA-OPS"):
        Settings.from_env(
            {
                **model_env(MOSAIC_COST_CENTER="FAA-OPS"),
                "FEDERAL_MCP_PROFILE": "faa-regulatory-reference",
                "FEDERAL_ENVIRONMENT": "Staging",
                "FEDERAL_BACKEND_AUTH_VERIFIED": "true",
            }
        )
