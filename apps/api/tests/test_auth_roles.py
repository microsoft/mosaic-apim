"""Authorization boundaries.

Phase 1 moved the app-role check out of ``EntraAuthenticator.authenticate`` so one API can serve
both the administrator console and the end-user portal. These tests are the safety net for that
move: authentication must still fail closed for a token carrying no MOSAIC role, and every
existing administrator route must still refuse a caller who only holds the portal role.

The routes in ``ANY_ROLE_ROUTES`` are the deliberate exception outside the portal: the console asks
one of them which role its caller holds, so it has to answer a portal-only caller too.
"""

import asyncio
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
    require_mosaic_role,
    require_portal_user,
)
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.main import create_app

KEY_ID = "test-key"
PORTAL_PREFIXES = ("/api/v1/portal/", "/api/v1/me/")
# Routes either MOSAIC role may call because all they return is which role the caller holds. Each
# is listed by method and path rather than by prefix, so a new route beside one of them still has
# to refuse a portal-only caller unless it is added here on purpose.
ANY_ROLE_ROUTES = frozenset({("GET", "/api/v1/console/me")})
NO_MOSAIC_ROLE = "A MOSAIC app role is required: Admin, User"

Admin = Annotated[AuthContext, Depends(require_admin)]
PortalUser = Annotated[AuthContext, Depends(require_portal_user)]
AnyMosaicRole = Annotated[AuthContext, Depends(require_mosaic_role)]


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
    assert raised.value.detail == NO_MOSAIC_ROLE


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

    @app.get("/any-role")
    async def any_role(auth: AnyMosaicRole) -> dict[str, list[str]]:
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
    ("roles", "admin_status", "portal_status", "any_role_status"),
    [
        (["Admin"], 200, 200, 200),
        (["User"], 403, 200, 200),
        (["Admin", "User"], 200, 200, 200),
        ([], 403, 403, 403),
        (["SomeOtherApp.Reader"], 403, 403, 403),
    ],
)
def test_role_dependencies_gate_independently(
    roles: list[str], admin_status: int, portal_status: int, any_role_status: int
) -> None:
    with _dependency_app(roles) as client:
        assert client.get("/admin-only").status_code == admin_status
        assert client.get("/portal").status_code == portal_status
        assert client.get("/any-role").status_code == any_role_status


def _admin_routes(app: FastAPI) -> list[tuple[str, str]]:
    """Enumerate the published admin surface from the OpenAPI schema.

    Read from the schema rather than ``app.routes`` so the check keeps covering every route as
    FastAPI changes how included routers are represented internally.

    The portal read model and current-user credential APIs have their own authorization coverage.
    Only their explicit prefixes are exempt, so a new admin route elsewhere is still checked. The
    routes in ``ANY_ROLE_ROUTES`` are exempt one by one and are covered separately below.
    """

    calls: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api/v1") or path.startswith(PORTAL_PREFIXES):
            continue
        concrete = path
        while "{" in concrete:
            start = concrete.index("{")
            end = concrete.index("}", start)
            concrete = f"{concrete[:start]}placeholder{concrete[end + 1 :]}"
        for method in operations:
            if method.upper() in {"HEAD", "OPTIONS", "PARAMETERS"}:
                continue
            if (method.upper(), concrete) in ANY_ROLE_ROUTES:
                continue
            calls.append((method.upper(), concrete))
    return calls


def _local_client(roles: list[str]) -> TestClient:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        local_roles=roles,
    )
    return TestClient(create_app(settings))


@pytest.fixture
def portal_only_client() -> Iterator[TestClient]:
    with _local_client(["User"]) as client:
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


def _portal_routes(app: FastAPI) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith(PORTAL_PREFIXES):
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


def test_every_portal_route_admits_a_portal_only_caller(
    portal_only_client: TestClient,
) -> None:
    """The inverse of the exemption above.

    Excluding the portal prefix from the admin sweep would otherwise hide a portal route that
    accidentally demanded ``Admin``, which would lock every end user out of the thing built for
    them. A 404 or a validation error is fine here; a 403 is not.
    """

    routes = _portal_routes(portal_only_client.app)
    assert routes, "expected the portal surface to be published"
    assert ("GET", "/api/v1/portal/me") in routes
    assert ("GET", "/api/v1/portal/entitlements") in routes
    assert ("GET", "/api/v1/me/entitlements") in routes
    assert ("POST", "/api/v1/me/entitlements/placeholder/keys/reveal") in routes
    forbidden = []
    for method, path in routes:
        response = portal_only_client.request(
            method, path, json={} if method in {"POST", "PUT", "PATCH"} else None
        )
        if response.status_code == 403:
            forbidden.append((method, path, response.text[:120]))
    assert not forbidden, forbidden


def test_every_any_role_route_is_published(portal_only_client: TestClient) -> None:
    """An entry left behind after its route moved would exempt nothing and prove nothing."""

    paths = portal_only_client.app.openapi()["paths"]
    missing = [
        (method, path)
        for method, path in sorted(ANY_ROLE_ROUTES)
        if method.lower() not in paths.get(path, {})
    ]
    assert not missing, missing


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (["Admin"], {"roles": ["Admin"], "isAdmin": True}),
        (["User"], {"roles": ["User"], "isAdmin": False}),
        (["Admin", "User"], {"roles": ["Admin", "User"], "isAdmin": True}),
        (["User", "SomeOtherApp.Reader"], {"roles": ["User"], "isAdmin": False}),
    ],
)
def test_the_console_learns_which_role_its_caller_holds(
    roles: list[str], expected: dict[str, Any]
) -> None:
    with _local_client(roles) as client:
        response = client.get("/api/v1/console/me")
    assert response.status_code == 200
    assert response.json() == expected


@pytest.mark.parametrize("roles", [[], ["SomeOtherApp.Reader"]])
def test_every_any_role_route_refuses_a_caller_without_a_mosaic_role(roles: list[str]) -> None:
    """Local authentication admits whatever roles it is given, so the route itself must refuse."""

    with _local_client(roles) as client:
        refused = [
            (method, path, client.request(method, path)) for method, path in sorted(ANY_ROLE_ROUTES)
        ]
    unexpected = [
        (method, path, response.status_code, response.text[:120])
        for method, path, response in refused
        if response.status_code != 403 or response.json().get("detail") != NO_MOSAIC_ROLE
    ]
    assert not unexpected, unexpected


def test_the_console_route_reads_roles_from_the_entra_access_token(
    signing_key: rsa.RSAPrivateKey,
) -> None:
    """What the console sees for an administrator, a portal user, and someone with no role."""

    settings = _settings()
    authenticator, http_client = _authenticator(settings, signing_key)
    app = create_app(settings)
    try:
        with TestClient(app) as client:
            app.state.authenticator = authenticator
            responses = {
                name: client.get(
                    "/api/v1/console/me",
                    headers={"Authorization": f"Bearer {_token(signing_key, settings, roles)}"},
                )
                for name, roles in (("admin", ["Admin"]), ("user", ["User"]), ("nobody", []))
            }
    finally:
        asyncio.run(http_client.aclose())
    assert responses["admin"].status_code == 200
    assert responses["admin"].json() == {"roles": ["Admin"], "isAdmin": True}
    assert responses["user"].status_code == 200
    assert responses["user"].json() == {"roles": ["User"], "isAdmin": False}
    assert responses["nobody"].status_code == 403
    assert responses["nobody"].json()["detail"] == NO_MOSAIC_ROLE


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
