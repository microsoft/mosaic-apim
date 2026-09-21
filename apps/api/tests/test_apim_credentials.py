from typing import cast

import httpx
import pytest
from apim_double import RESOURCE_ID, FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from mosaic_api.domain import ApimResourceId
from mosaic_api.errors import ConflictError, UpstreamAuthorizationError, UpstreamError
from mosaic_api.integrations.apim.client import ArmClient
from mosaic_api.integrations.apim.credentials import ApimCredentialClient

SUBSCRIPTION = "mosaic-grant-test"
API_NAME = "mosaic-chat"
PRIMARY = "fixture-primary-value"
SECONDARY = "fixture-secondary-value"


async def no_sleep(_delay: float) -> None:
    pass


@pytest.mark.parametrize("slot", ["primary", "secondary"])
@pytest.mark.parametrize("relative_scope", [False, True])
async def test_reveal_uses_stable_post_and_returns_only_selected_secret(
    slot: str, relative_scope: bool
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "scope": (
                            f"/apis/{API_NAME}" if relative_scope
                            else f"{RESOURCE_ID}/apis/{API_NAME}"
                        ),
                        "state": "active",
                    }
                },
            )
        return httpx.Response(200, json={"primaryKey": PRIMARY, "secondaryKey": SECONDARY})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arm = ArmClient(cast(AsyncTokenCredential, FakeCredential()), client=http)
        client = ApimCredentialClient(arm, ApimResourceId.parse(RESOURCE_ID))
        key = await client.read_key(
            SUBSCRIPTION, API_NAME, "primary" if slot == "primary" else "secondary"
        )
    assert key.get_secret_value() == (PRIMARY if slot == "primary" else SECONDARY)
    assert PRIMARY not in repr(key) and SECONDARY not in repr(key)
    assert requests[-1].method == "POST"
    assert requests[-1].url.path.endswith(f"/subscriptions/{SUBSCRIPTION}/listSecrets")
    assert requests[-1].url.params["api-version"] == "2024-05-01"
    assert requests[-1].content == b""


@pytest.mark.parametrize(
    "properties",
    [
        {"scope": f"{RESOURCE_ID}/apis/{API_NAME}", "state": "suspended"},
        {"scope": f"{RESOURCE_ID}/apis/another-api", "state": "active"},
        {"scope": "/apis", "state": "active"},
        {"scope": f"{RESOURCE_ID}-other/apis/{API_NAME}", "state": "active"},
        {"scope": f"{RESOURCE_ID}/products/shared", "state": "active"},
        {},
    ],
)
async def test_wrong_scope_or_state_never_calls_list_secrets(
    properties: dict[str, str],
) -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        return httpx.Response(200, json={"properties": properties})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arm = ArmClient(cast(AsyncTokenCredential, FakeCredential()), client=http)
        reader = ApimCredentialClient(arm, ApimResourceId.parse(RESOURCE_ID))
        with pytest.raises(ConflictError):
            await reader.read_key(SUBSCRIPTION, API_NAME, "primary")
    assert requests == ["GET"]


@pytest.mark.parametrize("status", [400, 403, 429, 500])
async def test_upstream_secret_error_body_is_never_exposed(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"properties": {"scope": f"{RESOURCE_ID}/apis/{API_NAME}", "state": "active"}},
            )
        return httpx.Response(
            status,
            json={"error": {"message": PRIMARY, "code": SECONDARY}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arm = ArmClient(
            cast(AsyncTokenCredential, FakeCredential()), client=http, sleep=no_sleep
        )
        reader = ApimCredentialClient(arm, ApimResourceId.parse(RESOURCE_ID))
        with pytest.raises((UpstreamError, UpstreamAuthorizationError)) as raised:
            await reader.read_key(SUBSCRIPTION, API_NAME, "primary")
    rendered = str(raised.value) + repr(raised.value.details)
    assert PRIMARY not in rendered and SECONDARY not in rendered
    if status == 403:
        assert "listSecrets/action" in raised.value.details["missingAction"]


@pytest.mark.parametrize("body", [{}, {"primaryKey": 123}, {"primaryKey": "x" * 257}, []])
async def test_malformed_secret_payload_is_not_returned(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"properties": {"scope": f"{RESOURCE_ID}/apis/{API_NAME}", "state": "active"}},
            )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arm = ArmClient(cast(AsyncTokenCredential, FakeCredential()), client=http)
        reader = ApimCredentialClient(arm, ApimResourceId.parse(RESOURCE_ID))
        with pytest.raises(UpstreamError, match="invalid"):
            await reader.read_key(SUBSCRIPTION, API_NAME, "primary")
