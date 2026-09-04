"""Authorization boundaries.

Phase 1 moved the app-role check out of ``EntraAuthenticator.authenticate`` so one API can serve
both the administrator console and the end-user portal. These tests are the safety net for that
move: authentication must still fail closed for a token carrying no MOSAIC role, and every
existing administrator route must still refuse a caller who only holds the portal role.
"""

import time
from collections.abc import Iterator
from typing import Annotated, Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from mosaic_api.auth import (
    AuthContext,
    EntraAuthenticator,
    require_admin,
    require_portal_user,
)
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.main import create_app

KEY_ID = "test-key"

Admin = Annotated[AuthContext, Depends(require_admin)]
PortalUser = Annotated[AuthContext, Depends(require_portal_user)]


def _settings(**overrides: Any) -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        api_client_id="api-client",
        **overrides,
    )


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _authenticator(
    settings: Settings, signing_key: rsa.RSAPrivateKey
) -> tuple[EntraAuthenticator, httpx.AsyncClient]:
    public_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    public_jwk.update({"kid": KEY_ID, "kty": "RSA"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={"issuer": settings.issuer, "jwks_uri": "https://identity.example/keys"},
            )
        return httpx.Response(200, json={"keys": [public_jwk]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EntraAuthenticator(settings, client=client), client


def _token(signing_key: rsa.RSAPrivateKey, settings: Settings, roles: list[str]) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "iss": settings.issuer,
            "aud": settings.api_client_id,
            "tid": settings.tenant_id,
            "oid": "caller-object-id",
            "iat": now,
            "exp": now + 600,
            "roles": roles,
        },
        signing_key,
        algorithm="RS256",
        headers={"kid": KEY_ID},
    )


def _request(token: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (["Admin"], frozenset({"Admin"})),
        (["User"], frozenset({"User"})),
        (["Admin", "User"], frozenset({"Admin", "User"})),
    ],
)
async def test_authentication_admits_any_mosaic_role(
    signing_key: rsa.RSAPrivateKey, roles: list[str], expected: frozenset[str]
) -> None:
    settings = _settings()
    authenticator, client = _authenticator(settings, signing_key)
    try:
        context = await authenticator.authenticate(
            _request(_token(signing_key, settings, roles))
        )
    finally:
        await client.aclose()
    assert context.roles == expected
    assert context.object_id == "caller-object-id"


@pytest.mark.parametrize("roles", [[], ["SomeOtherApp.Reader"]])
async def test_authentication_still_fails_closed_without_a_mosaic_role(
    signing_key: rsa.RSAPrivateKey, roles: list[str]
) -> None:
    settings = _settings()
    authenticator, client = _authenticator(settings, signing_key)
    try:
        with pytest.raises(HTTPException) as raised:
            await authenticator.authenticate(_request(_token(signing_key, settings, roles)))
    finally:
        await client.aclose()
    assert raised.value.status_code == 403


def _dependency_app(roles: list[str]) -> TestClient:
    app = FastAPI()
    app.state.settings = _settings()
    app.state.authenticator = _StubAuthenticator(roles)

    @app.get("/admin-only")
    async def admin_only(auth: Admin) -> dict[str, list[str]]:
        return {"roles": sorted(auth.roles)}

    @app.get("/portal")
    async def portal(auth: PortalUser) -> dict[str, list[str]]:
        return {"roles": sorted(auth.roles)}

    return TestClient(app)


class _StubAuthenticator:
    def __init__(self, roles: list[str]) -> None:
        self._roles = frozenset(roles)

    async def authenticate(self, _request: Request) -> AuthContext:
        return AuthContext(
            object_id="caller-object-id", tenant_id="tenant-test", roles=self._roles
        )

    async def close(self) -> None:
        return None


@pytest.mark.parametrize(
    ("roles", "admin_status", "portal_status"),
    [
        (["Admin"], 200, 200),
        (["User"], 403, 200),
        (["Admin", "User"], 200, 200),
        ([], 403, 403),
    ],
)
def test_role_dependencies_gate_independently(
    roles: list[str], admin_status: int, portal_status: int
) -> None:
    with _dependency_app(roles) as client:
        assert client.get("/admin-only").status_code == admin_status
        assert client.get("/portal").status_code == portal_status


def _admin_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Enumerate the published admin surface from the OpenAPI schema.

    Read from the schema rather than ``app.routes`` so the check keeps covering every route as
    FastAPI changes how included routers are represented internally.
    """

    calls: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1"):
            continue
        concrete = path
        while "{" in concrete:
            start = concrete.index("{")
            end = concrete.index("}", start)
            concrete = f"{concrete[:start]}placeholder{concrete[end + 1 :]}"
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            calls.append((method.upper(), concrete))
    return calls


@pytest.fixture
def portal_only_client() -> Iterator[TestClient]:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        local_roles=["User"],
    )
    with TestClient(create_app(settings)) as client:
        yield client


def test_every_admin_route_refuses_a_portal_only_caller(portal_only_client: TestClient) -> None:
    routes = _admin_routes(portal_only_client.app)
    # Anchored so a future FastAPI change that stops exposing the surface fails loudly instead of
    # passing vacuously on an empty list.
    assert len(routes) >= 50, f"expected the full admin surface, enumerated {len(routes)}"
    unexpected = []
    for method, path in routes:
        response = portal_only_client.request(
            method, path, json={} if method in {"POST", "PUT", "PATCH"} else None
        )
        # Match the message too: a domain 403 such as UpstreamAuthorizationError would otherwise
        # let an ungated route pass this check.
        if response.status_code != 403 or response.json().get("detail") != (
            "The Admin app role is required"
        ):
            unexpected.append((method, path, response.status_code, response.text[:120]))
    assert not unexpected, unexpected


def test_role_names_must_differ() -> None:
    with pytest.raises(ValueError, match="must be different"):
        Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.LOCAL,
            repository_backend=RepositoryBackend.MEMORY,
            tenant_id="tenant-test",
            required_role="User",
            portal_role="User",
        )


def test_admin_routes_admit_an_administrator(client: TestClient) -> None:
    assert client.get("/api/v1/principals").status_code == 200
