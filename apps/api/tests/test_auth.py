import base64
import json

import httpx
import pytest
from fastapi import HTTPException
from mosaic_api.auth import EntraAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings


def _token(kid: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": kid}).encode()).rstrip(
        b"="
    )
    return f"{header.decode()}.e30.c2ln"


@pytest.mark.asyncio
async def test_unknown_key_ids_do_not_trigger_repeated_jwks_refreshes() -> None:
    requests = 0
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        api_client_id="api-client",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.issuer,
                    "jwks_uri": "https://identity.example/keys",
                },
            )
        return httpx.Response(200, json={"keys": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    authenticator = EntraAuthenticator(settings, client=client)
    try:
        for kid in (
            "unknown-one",
            "unknown-two",
            "unknown-one",
            *(f"unknown-{index}" for index in range(300)),
        ):
            with pytest.raises(HTTPException, match="signing key is unknown"):
                await authenticator._key(_token(kid))
        with pytest.raises(HTTPException, match="signing key is invalid"):
            await authenticator._key(_token("x" * 257))
    finally:
        await client.aclose()

    assert requests == 2
    assert len(authenticator._unknown_keys) == 256
