import base64
import json
from typing import Any

import httpx
import pytest
from fastapi import HTTPException
from mosaic_api.auth import EntraAuthenticator, token_display_claims
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings


def _token(kid: str) -> str:
    header = base64.urlsafe_b64encode(json.dumps({"alg": "RS256", "kid": kid}).encode()).rstrip(
        b"="
    )
    return f"{header.decode()}.e30.c2ln"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        (
            {"name": "  Jordan   Rowan ", "preferred_username": " Jordan.Rowan@example.invalid "},
            ("Jordan Rowan", "Jordan.Rowan@example.invalid"),
        ),
        (
            {
                "name": ["not a name"],
                "preferred_username": None,
                "upn": "UPN",
                "unique_name": "old",
            },
            (None, "UPN"),
        ),
        (
            {"preferred_username": " \n ", "upn": 12, "unique_name": " legacy-user "},
            (None, "legacy-user"),
        ),
        ({"name": "\t", "email": "contact@example.invalid"}, (None, None)),
        (
            {"name": " Jo\u0301rdan\u202e\n Rowan\x00 ", "preferred_username": "\u2066USER\u2069"},
            ("J\u00f3rdan Rowan", "USER"),
        ),
        ({"name": "n" * 201, "preferred_username": "u" * 201}, ("n" * 200, "u" * 200)),
        (
            {"name": "\u5f20 \u4f1f\u200d", "preferred_username": False},
            ("\u5f20 \u4f1f\u200d", None),
        ),
        ({}, (None, None)),
    ],
)
def test_optional_display_claims_are_normalized_without_inventing_identity(
    claims: dict[str, Any], expected: tuple[str | None, str | None]
) -> None:
    assert token_display_claims(claims) == expected


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
