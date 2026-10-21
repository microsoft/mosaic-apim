"""App-only Graph sendMail, exercised without a live mailbox."""

import json
from typing import Any, cast

import httpx
import jwt
import pytest
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from mosaic_api.integrations.email import EmailMessage, GraphEmailClient

ENDPOINT = "https://graph.microsoft.com"
TENANT = "11111111-1111-4111-8111-111111111111"
PRINCIPAL = "22222222-2222-4222-8222-222222222222"
MAILBOX = "33333333-3333-4333-8333-333333333333"
SENDER = "notifications@example.test"
MESSAGE = EmailMessage(
    subject="Budget reached",
    plain_text="Research reached 80% of its budget.",
    html="<p>Research reached 80% of its budget.</p>",
    to=["owner@example.test", "finance@example.test"],
)


class Credential:
    def __init__(self, *, fail: bool = False, **claims: Any) -> None:
        self.scopes: list[str] = []
        self.fail = fail
        self.claims = {
            "aud": "00000003-0000-0000-c000-000000000000",
            "tid": TENANT,
            "oid": PRINCIPAL,
            "idtyp": "app",
            **claims,
        }

    async def get_token(self, *scopes: str, **_kwargs: Any) -> AccessToken:
        self.scopes.extend(scopes)
        if self.fail:
            raise ClientAuthenticationError("private credential failure")
        token = jwt.encode(self.claims, "fictional-test-key-at-least-32-bytes", algorithm="HS256")
        return AccessToken(token, 4_102_444_800)

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
) -> tuple[GraphEmailClient, Transport, Credential]:
    transport = Transport(response)
    identity = credential or Credential()
    client = GraphEmailClient(
        cast(AsyncTokenCredential, identity),
        tenant_id=TENANT,
        principal_id=PRINCIPAL,
        client=httpx.AsyncClient(transport=httpx.MockTransport(transport.handler)),
    )
    return client, transport, identity


async def test_a_send_is_one_post_and_empty_202_is_acceptance_not_delivery() -> None:
    client, transport, credential = _client(httpx.Response(202))
    result = await client.send(MAILBOX, MESSAGE, operation_id="correlation")
    assert result.accepted and result.operation_id == "correlation"
    [request] = transport.requests
    assert request.method == "POST"
    assert str(request.url) == f"{ENDPOINT}/v1.0/users/{MAILBOX}/sendMail"
    assert request.headers["Authorization"].startswith("Bearer ")
    assert request.headers["client-request-id"] == "correlation"
    assert "Operation-Id" not in request.headers
    assert credential.scopes == [ENDPOINT + "/.default"]
    assert json.loads(request.content) == {
        "message": {
            "subject": MESSAGE.subject,
            "body": {"contentType": "HTML", "content": MESSAGE.html},
            "toRecipients": [{"emailAddress": {"address": address}} for address in MESSAGE.to],
        },
        "saveToSentItems": True,
    }


async def test_roleless_exchange_rbac_identity_can_check_without_sending() -> None:
    client, transport, _ = _client(httpx.Response(202))
    result = await client.check_access()
    assert result.authenticated and result.identity_object_id == PRINCIPAL
    assert transport.requests == []


@pytest.mark.parametrize(
    "claims",
    [
        {"tid": "other-tenant"},
        {"scp": "Mail.Send"},
        {"idtyp": "user"},
        {"oid": "other-identity"},
        {"aud": "https://management.azure.com"},
    ],
)
async def test_wrong_tenant_identity_audience_and_delegated_tokens_never_send(
    claims: dict[str, str],
) -> None:
    client, transport, _ = _client(httpx.Response(202), Credential(**claims))
    result = await client.send(MAILBOX, MESSAGE, operation_id="test")
    assert not result.accepted and result.error_code == "graphWrongIdentity"
    assert transport.requests == []


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://attacker.example.test",
        "http://graph.microsoft.com",
        "https://graph.microsoft.com/other",
        "https://graph.microsoft.com?secret=1",
        "https://user@graph.microsoft.com",
        "https://graph.microsoft.com:8443",
    ],
)
def test_untrusted_origins_are_rejected_before_credential_use(endpoint: str) -> None:
    with pytest.raises(ValueError):
        GraphEmailClient(
            cast(AsyncTokenCredential, Credential()), tenant_id=TENANT, endpoint=endpoint
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        ENDPOINT,
        "https://graph.microsoft.us",
        "https://dod-graph.microsoft.us",
        "https://microsoftgraph.chinacloudapi.cn",
    ],
)
async def test_supported_clouds_request_their_own_graph_scope(endpoint: str) -> None:
    credential = Credential(aud=endpoint)
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(202)))
    client = GraphEmailClient(
        cast(AsyncTokenCredential, credential), tenant_id=TENANT, endpoint=endpoint, client=http
    )
    assert (await client.send(MAILBOX, MESSAGE, operation_id="test")).accepted
    assert credential.scopes == [endpoint + "/.default"]
    await http.aclose()


@pytest.mark.parametrize(
    ("status", "retryable", "unknown"),
    [
        (400, False, False),
        (401, False, False),
        (403, False, False),
        (404, False, False),
        (409, False, False),
        (429, True, False),
        (408, True, True),
        (503, True, True),
    ],
)
async def test_refusals_are_sanitized_and_classified(
    status: int,
    retryable: bool,
    unknown: bool,
) -> None:
    client, transport, _ = _client(
        httpx.Response(
            status,
            json={"error": {"code": "ErrorAccessDenied", "message": "sensitive upstream data"}},
            headers={"Retry-After": "120", "request-id": PRINCIPAL},
        )
    )
    result = await client.send(MAILBOX, MESSAGE, operation_id="test")
    assert not result.accepted and result.retryable is retryable
    assert result.outcome_unknown is unknown
    assert result.request_id == PRINCIPAL and result.retry_after_seconds == 120
    assert "sensitive upstream data" not in (result.error or "")
    assert len(transport.requests) == 1


async def test_redirects_never_forward_a_token_or_count_as_acceptance() -> None:
    client, transport, _ = _client(
        httpx.Response(302, headers={"location": "https://attacker.example.test"})
    )
    assert not (await client.send(MAILBOX, MESSAGE, operation_id="test")).accepted
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    ("error", "unknown"),
    [(httpx.ConnectError("sensitive host"), False), (httpx.ReadTimeout("private"), True)],
)
async def test_transport_failures_remain_retryable_without_exposing_details(
    error: Exception,
    unknown: bool,
) -> None:
    client, _, _ = _client(error)
    result = await client.send(MAILBOX, MESSAGE, operation_id="test")
    assert not result.accepted and result.retryable
    assert result.outcome_unknown is unknown
    assert "sensitive" not in (result.error or "") and "private" not in (result.error or "")


async def test_no_token_is_a_visible_failure_without_a_provider_request() -> None:
    client, transport, _ = _client(httpx.Response(202), Credential(fail=True))
    result = await client.send(MAILBOX, MESSAGE, operation_id="test")
    assert result.error_code == "graphIdentityUnavailable"
    assert result.retryable
    assert transport.requests == []


async def test_missing_recipients_or_invalid_mailbox_never_send() -> None:
    client, transport, credential = _client(httpx.Response(202))
    assert not (await client.send("address@example.test", MESSAGE, operation_id="test")).accepted
    assert not (
        await client.send(MAILBOX, EmailMessage("s", "text", "", []), operation_id="test")
    ).accepted
    assert transport.requests == [] and credential.scopes == []
