"""The Communication Services email client, against a fake transport. See ADR 0023."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from mosaic_api.integrations.email import (
    EMAIL_API_VERSION,
    AcsEmailClient,
    EmailMessage,
    email_scope,
)

ENDPOINT = "https://contoso-mosaic.communication.azure.com"
SENDER = "DoNotReply@contoso.com"
MESSAGE = EmailMessage(
    subject="Budget reached",
    plain_text="Research reached 80% of its budget.",
    html="<p>Research reached 80% of its budget.</p>",
    to=["owner@contoso.com", "finance@fabrikam.com"],
)


class Credential:
    def __init__(self, *, fail: bool = False) -> None:
        self.scopes: list[str] = []
        self.fail = fail

    async def get_token(self, *scopes: str, **_kwargs: Any) -> AccessToken:
        self.scopes.extend(scopes)
        if self.fail:
            raise ClientAuthenticationError("no identity")
        return AccessToken("communication-token", 4_102_444_800)

    async def close(self) -> None:
        return None


class Transport:
    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _client(
    response: httpx.Response | Exception, credential: Credential | None = None
) -> tuple[AcsEmailClient, Transport, Credential]:
    transport = Transport(response)
    identity = credential or Credential()
    client = AcsEmailClient(
        cast(AsyncTokenCredential, identity),
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)),
    )
    return client, transport, identity


async def test_a_send_is_one_post_with_the_identitys_token_and_the_operation_id() -> None:
    client, transport, credential = _client(
        httpx.Response(202, json={"id": "op-1", "status": "Running"})
    )

    result = await client.send(ENDPOINT, SENDER, MESSAGE, operation_id="op-1")

    assert result.accepted
    assert result.operation_id == "op-1"
    [request] = transport.requests
    assert request.method == "POST"
    assert str(request.url).startswith(f"{ENDPOINT}/emails:send?")
    assert request.url.params["api-version"] == EMAIL_API_VERSION
    assert request.headers["Authorization"] == "Bearer communication-token"
    assert request.headers["Operation-Id"] == "op-1"
    assert credential.scopes == ["https://communication.azure.com/.default"]
    body = json.loads(request.content)
    assert body == {
        "senderAddress": SENDER,
        "recipients": {
            "to": [{"address": "owner@contoso.com"}, {"address": "finance@fabrikam.com"}]
        },
        "content": {
            "subject": "Budget reached",
            "plainText": "Research reached 80% of its budget.",
            "html": "<p>Research reached 80% of its budget.</p>",
        },
        "userEngagementTrackingDisabled": True,
    }


async def test_an_operation_communication_services_already_has_isnt_sent_again() -> None:
    client, _, _ = _client(httpx.Response(409, json={"error": {"code": "Conflict"}}))

    result = await client.send(ENDPOINT, SENDER, MESSAGE, operation_id="op-1")

    assert result.accepted
    assert result.status_code == 409


async def test_a_refusal_is_returned_with_azures_reason() -> None:
    client, _, _ = _client(
        httpx.Response(
            400,
            json={"error": {"code": "InvalidSenderUserName", "message": "The sender is unknown."}},
        )
    )

    result = await client.send(ENDPOINT, SENDER, MESSAGE, operation_id="op-1")

    assert not result.accepted
    assert result.status_code == 400
    assert result.error == "InvalidSenderUserName The sender is unknown."


async def test_no_answer_is_a_refusal_not_an_exception() -> None:
    client, _, _ = _client(httpx.ConnectError("unreachable"))

    result = await client.send(ENDPOINT, SENDER, MESSAGE, operation_id="op-1")

    assert not result.accepted
    assert result.error is not None and "ConnectError" in result.error


async def test_no_token_is_a_refusal_and_sends_nothing() -> None:
    client, transport, _ = _client(httpx.Response(202), Credential(fail=True))

    result = await client.send(ENDPOINT, SENDER, MESSAGE, operation_id="op-1")

    assert not result.accepted
    assert transport.requests == []


@pytest.mark.parametrize(
    "endpoint",
    ["https://attacker.example.com", "http://contoso.communication.azure.com"],
)
async def test_the_token_never_goes_anywhere_but_communication_services(endpoint: str) -> None:
    client, transport, credential = _client(httpx.Response(202))

    result = await client.send(endpoint, SENDER, MESSAGE, operation_id="op-1")

    assert not result.accepted
    assert transport.requests == []
    assert credential.scopes == []


def test_azure_government_endpoints_get_their_own_scope() -> None:
    assert email_scope("https://contoso.communication.azure.us") == (
        "https://communication.azure.us/.default"
    )
    assert email_scope(ENDPOINT) == "https://communication.azure.com/.default"


async def test_an_email_without_recipients_isnt_sent() -> None:
    client, transport, _ = _client(httpx.Response(202))

    result = await client.send(
        ENDPOINT,
        SENDER,
        EmailMessage(subject="s", plain_text="t", html="h", to=[]),
        operation_id="op-1",
    )

    assert not result.accepted
    assert transport.requests == []
