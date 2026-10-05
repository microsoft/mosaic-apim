"""ask_model's model call, against a mocked gateway."""

import json

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError

from m_agent.gateway import (
    COST_CENTER_HEADER,
    ON_BEHALF_HEADER,
    SYSTEM_PROMPT,
    ModelCallError,
    ModelGateway,
    ModelSettings,
    deny_reason,
    scrub,
)
from support import FAKE_TOKEN, RUNTIME_SCOPE, FakeCredential, FakeGateway, model_settings

pytestmark = pytest.mark.anyio

REFERENCE = "0f8fad5b-d9cb-469f-a165-70867728950e"


def gateway_for(
    fake: FakeGateway,
    settings: ModelSettings | None = None,
    credential: FakeCredential | None = None,
) -> ModelGateway:
    return ModelGateway(
        settings or model_settings(),
        credential or FakeCredential(),
        httpx.AsyncClient(transport=fake.transport, follow_redirects=False),
    )


async def failure(respond: httpx.Response, **settings: object) -> str:
    fake = FakeGateway(lambda request: respond)
    with pytest.raises(ModelCallError) as raised:
        await gateway_for(fake, model_settings(**settings)).ask("Hi?", on_behalf_of=None)
    return str(raised.value)


async def test_the_model_is_called_through_the_gateway_as_the_server_s_application() -> None:
    fake = FakeGateway()
    credential = FakeCredential()

    answer = await gateway_for(fake, credential=credential).ask(
        "What is the capital of France?", on_behalf_of=None
    )

    assert answer == "Paris."
    assert credential.scopes == [(RUNTIME_SCOPE,)]
    [request] = fake.requests
    assert request.method == "POST"
    assert str(request.url) == (
        "https://gateway.example.test/models/chat/openai/deployments/gpt-test/chat/completions"
        "?api-version=2024-10-21"
    )
    assert request.headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert json.loads(request.content) == {
        "model": "gpt-test",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "What is the capital of France?"},
        ],
        "max_tokens": 16,
        "stream": False,
    }


async def test_the_on_behalf_value_is_passed_on_exactly_as_received() -> None:
    fake = FakeGateway()
    await gateway_for(fake).ask("Hi?", on_behalf_of=REFERENCE)
    assert fake.requests[0].headers.get_list(ON_BEHALF_HEADER) == [REFERENCE]


async def test_an_opaque_on_behalf_value_isn_t_parsed_or_changed() -> None:
    fake = FakeGateway()
    await gateway_for(fake).ask("Hi?", on_behalf_of="Not-A-GUID/Mixed.Case")
    assert fake.requests[0].headers[ON_BEHALF_HEADER] == "Not-A-GUID/Mixed.Case"


@pytest.mark.parametrize("missing", [None, ""])
async def test_no_on_behalf_value_means_no_header(missing: str | None) -> None:
    fake = FakeGateway()
    await gateway_for(fake).ask("Hi?", on_behalf_of=missing)
    assert ON_BEHALF_HEADER not in fake.requests[0].headers


async def test_the_cost_center_is_named_only_when_configured() -> None:
    fake = FakeGateway()
    await gateway_for(fake).ask("Hi?", on_behalf_of=None)
    await gateway_for(fake, model_settings(cost_center="research")).ask("Hi?", on_behalf_of=None)
    assert COST_CENTER_HEADER not in fake.requests[0].headers
    assert fake.requests[1].headers[COST_CENTER_HEADER] == "research"


async def test_the_token_parameter_and_budget_are_configurable() -> None:
    fake = FakeGateway()
    settings = model_settings(token_parameter="max_completion_tokens", max_tokens=8)
    await gateway_for(fake, settings).ask("Hi?", on_behalf_of=None)
    body = json.loads(fake.requests[0].content)
    assert body["max_completion_tokens"] == 8
    assert "max_tokens" not in body


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(403, text="Model access denied."),
            "The model call failed with HTTP 403: Model access denied.",
        ),
        (
            httpx.Response(401, json={"statusCode": 401, "message": "Model access denied."}),
            "The model call failed with HTTP 401: Model access denied.",
        ),
        (
            httpx.Response(
                429,
                json={
                    "statusCode": 429,
                    "message": "Rate limit is exceeded. Try again in 12 seconds.",
                },
            ),
            "The model call failed with HTTP 429: Rate limit is exceeded. Try again in 12 seconds.",
        ),
        (
            httpx.Response(
                403,
                text=(
                    "Access denied. Cost center research has used its monthly budget, so its "
                    "calls are refused until an administrator raises the budget or the month ends."
                ),
            ),
            "The model call failed with HTTP 403: Access denied. Cost center research has used its "
            "monthly budget, so its calls are refused until an administrator raises the budget or "
            "the month ends.",
        ),
        (
            httpx.Response(
                404,
                json={
                    "error": {
                        "code": "DeploymentNotFound",
                        "message": "The deployment doesn't exist.",
                    }
                },
            ),
            "The model call failed with HTTP 404: The deployment doesn't exist.",
        ),
        (
            httpx.Response(502, html="<html><body>Bad gateway</body></html>"),
            "The model call failed with HTTP 502.",
        ),
        (httpx.Response(503, content=b""), "The model call failed with HTTP 503."),
    ],
)
async def test_a_refusal_names_the_status_and_mosaic_s_reason(
    response: httpx.Response, expected: str
) -> None:
    assert await failure(response) == expected


async def test_a_reason_keeps_no_identifiers_or_addresses() -> None:
    message = await failure(
        httpx.Response(
            500,
            json={
                "statusCode": 500,
                "message": (
                    "Internal error 0f8fad5b-d9cb-469f-a165-70867728950e for admin@contoso.example "
                    "at https://gateway.example.test/models/chat"
                ),
                "activityId": "6a0d6a3e-7e43-4b9b-9a7b-3c5c4a0e1f2d",
            },
        )
    )
    assert (
        message == "The model call failed with HTTP 500: Internal error [id] for [email] at [url]"
    )


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, text=f"Rejected Authorization: Bearer {FAKE_TOKEN}"),
        httpx.Response(401, text=f"token {FAKE_TOKEN} expired"),
        httpx.Response(400, json={"message": f"bad header {FAKE_TOKEN}"}),
        httpx.Response(400, json={"error": {"message": f"echo: Bearer {FAKE_TOKEN}"}}),
        httpx.Response(
            307, headers={"location": f"https://elsewhere.example.test/?t={FAKE_TOKEN}"}
        ),
    ],
)
async def test_no_error_ever_carries_the_token(response: httpx.Response) -> None:
    message = await failure(response)
    assert FAKE_TOKEN not in message
    assert FAKE_TOKEN.split(".")[1] not in message


async def test_an_echoed_token_never_reaches_the_answer() -> None:
    echoed = {"choices": [{"message": {"content": f"You sent {FAKE_TOKEN}"}}]}
    fake = FakeGateway(lambda request: httpx.Response(200, json=echoed))
    answer = await gateway_for(fake).ask("Hi?", on_behalf_of=None)
    assert FAKE_TOKEN not in answer


async def test_a_gateway_that_can_t_be_reached_is_reported_without_its_address() -> None:
    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            f"Can't connect to {request.url} with {FAKE_TOKEN}", request=request
        )

    with pytest.raises(ModelCallError) as raised:
        await gateway_for(FakeGateway(unreachable)).ask("Hi?", on_behalf_of=None)
    message = str(raised.value)
    assert message == "The model call failed before the gateway answered (ConnectError)."


async def test_a_timeout_is_reported_as_one() -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ModelCallError, match="timed out"):
        await gateway_for(FakeGateway(slow)).ask("Hi?", on_behalf_of=None)


async def test_a_token_failure_says_so_without_the_credential_s_details() -> None:
    credential = FakeCredential(
        error=ClientAuthenticationError(
            "ManagedIdentityCredential failed for client 1234 at 169.254.169.254"
        )
    )
    fake = FakeGateway()
    with pytest.raises(ModelCallError) as raised:
        await gateway_for(fake, credential=credential).ask("Hi?", on_behalf_of=REFERENCE)
    assert str(raised.value) == (
        "The server couldn't get a token for the model gateway (ClientAuthenticationError)."
    )
    assert fake.requests == []


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]},
            "finish reason: length",
        ),
        (
            {"choices": [{"message": {"content": None}, "finish_reason": "content_filter"}]},
            "content_filter",
        ),
        ({"choices": []}, "no answer text."),
        ({"unexpected": True}, "no answer text."),
    ],
)
async def test_an_answer_without_text_is_an_error(payload: object, expected: str) -> None:
    fake = FakeGateway(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(ModelCallError, match=expected):
        await gateway_for(fake).ask("Hi?", on_behalf_of=None)


async def test_an_answer_that_isn_t_json_is_an_error() -> None:
    fake = FakeGateway(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(ModelCallError, match="wasn't JSON"):
        await gateway_for(fake).ask("Hi?", on_behalf_of=None)


async def test_closing_the_gateway_closes_the_credential() -> None:
    credential = FakeCredential()
    await gateway_for(FakeGateway(), credential=credential).aclose()
    assert credential.closed


def test_deny_reason_ignores_a_json_body_without_a_message() -> None:
    assert deny_reason(httpx.Response(403, json={"statusCode": 403})) is None


def test_scrub_shortens_long_text_to_one_line() -> None:
    text = scrub("line one\nline two\t" + "x" * 500)
    assert text is not None
    assert "\n" not in text and "\t" not in text
    assert len(text) == 200
    assert text.endswith("…")
