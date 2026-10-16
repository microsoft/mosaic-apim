import asyncio
import time

import httpx
import jwt
import pytest
from conftest import install_directory_lookup
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext, EntraAuthenticator, LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import DirectoryObject, PrincipalKind
from mosaic_api.errors import (
    DirectoryError,
    DirectoryForbiddenError,
    DirectoryThrottledError,
)
from mosaic_api.integrations.graph import DirectoryPhoto, FakeDirectoryLookup, GraphDirectoryLookup
from mosaic_api.main import create_app
from photo_double import photo_image

USER_ID = "11111111-1111-1111-1111-111111111111"
AGENT_USER_ID = "22222222-2222-2222-2222-222222222222"
AGENT_ID = "33333333-3333-3333-3333-333333333333"
GROUP_ID = "44444444-4444-4444-4444-444444444444"
NESTED_GROUP_ID = "55555555-5555-5555-5555-555555555555"
MISSING_ID = "66666666-6666-6666-6666-666666666666"


def _lookup() -> FakeDirectoryLookup:
    return FakeDirectoryLookup(
        [
            DirectoryObject(
                object_id=USER_ID,
                kind=PrincipalKind.USER,
                display_name="Ada Lovelace",
                detail="ada@example.com",
            ),
            DirectoryObject(
                object_id=AGENT_USER_ID,
                kind=PrincipalKind.AGENT_USER,
                display_name="Research agent user",
                detail="research-agent@example.com",
                identity_parent_id=AGENT_ID,
            ),
            DirectoryObject(
                object_id=AGENT_ID,
                kind=PrincipalKind.AGENT_IDENTITY,
                display_name="Research agent",
                detail="33333333-3333-3333-3333-333333333333",
                app_id="33333333-3333-3333-3333-333333333333",
                blueprint_id="77777777-7777-7777-7777-777777777777",
            ),
            DirectoryObject(
                object_id=GROUP_ID,
                kind=PrincipalKind.SECURITY_GROUP,
                display_name="Model consumers",
                detail="model-consumers",
            ),
            DirectoryObject(
                object_id=NESTED_GROUP_ID,
                kind=PrincipalKind.SECURITY_GROUP,
                display_name="Nested consumers",
            ),
        ],
        {
            GROUP_ID: [USER_ID, NESTED_GROUP_ID],
            NESTED_GROUP_ID: [AGENT_USER_ID],
        },
    )


def _install_lookup(client: TestClient) -> None:
    install_directory_lookup(client.app, _lookup())


def test_group_principal_membership_lifecycle(client: TestClient) -> None:
    principal_response = client.post(
        "/api/v1/principals",
        json={
            "objectId": USER_ID,
            "kind": "user",
            "label": "Platform administrator",
        },
    )
    assert principal_response.status_code == 201
    principal = principal_response.json()

    group_response = client.post(
        "/api/v1/groups",
        json={"name": "Model administrators", "description": "Initial administrator group"},
    )
    assert group_response.status_code == 201
    group = group_response.json()

    principal_update = client.patch(
        f"/api/v1/principals/{principal['id']}",
        json={"label": "Updated administrator"},
    )
    assert principal_update.status_code == 200
    assert principal_update.json()["label"] == "Updated administrator"

    group_update = client.patch(
        f"/api/v1/groups/{group['id']}",
        json={"description": "Updated administrator group"},
    )
    assert group_update.status_code == 200
    assert group_update.json()["description"] == "Updated administrator group"

    membership_response = client.put(f"/api/v1/groups/{group['id']}/members/{principal['id']}")
    assert membership_response.status_code == 201
    assert membership_response.json()["principalId"] == principal["id"]

    repeated = client.put(f"/api/v1/groups/{group['id']}/members/{principal['id']}")
    assert repeated.status_code == 200
    assert repeated.json()["id"] == membership_response.json()["id"]

    blocked_delete = client.delete(f"/api/v1/principals/{principal['id']}")
    assert blocked_delete.status_code == 409
    assert blocked_delete.json()["code"] == "conflict"

    assert (
        client.delete(f"/api/v1/groups/{group['id']}/members/{principal['id']}").status_code == 204
    )
    assert client.delete(f"/api/v1/principals/{principal['id']}").status_code == 204
    assert client.delete(f"/api/v1/groups/{group['id']}").status_code == 204
    assert len(client.app.state.repository.audit_events) == 8


def test_uniqueness_and_reference_validation(client: TestClient) -> None:
    payload = {
        "objectId": "not-a-guid-but-legacy-sp-id",
        "kind": "servicePrincipal",
    }
    assert client.post("/api/v1/principals", json=payload).status_code == 201
    duplicate = client.post("/api/v1/principals", json=payload)
    assert duplicate.status_code == 409

    group = client.post("/api/v1/groups", json={"name": "Consumers"}).json()
    missing = client.put(f"/api/v1/groups/{group['id']}/members/principal_missing")
    assert missing.status_code == 404


def test_entra_mode_requires_bearer_token() -> None:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        api_client_id="api-client",
    )
    with TestClient(create_app(settings)) as entra_client:
        assert entra_client.get("/healthz").status_code == 200
        response = entra_client.get("/api/v1/groups")
        assert response.status_code == 401
        assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("legacy_value", [None, "false", "true", "invalid"])
def test_entra_directory_lookup_is_always_available(
    monkeypatch: pytest.MonkeyPatch, legacy_value: str | None
) -> None:
    if legacy_value is None:
        monkeypatch.delenv("MOSAIC_ENTRA_DIRECTORY_LOOKUP", raising=False)
    else:
        monkeypatch.setenv("MOSAIC_ENTRA_DIRECTORY_LOOKUP", legacy_value)
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        api_client_id="api-client",
    )
    with TestClient(create_app(settings)) as entra_client:
        assert isinstance(entra_client.app.state.directory_lookup, GraphDirectoryLookup)
        assert isinstance(entra_client.app.state.authenticator, EntraAuthenticator)
        # Authenticate only this status request locally; it must never contact Graph for consent.
        entra_client.app.state.authenticator = LocalAuthenticator(settings.tenant_id, ["Admin"])
        response = entra_client.get("/api/v1/directory/status")
        assert response.status_code == 200
        assert response.json()["lookupEnabled"] is True
        assert response.json()["message"] is None
        assert all(
            permission["granted"] is None for permission in response.json()["permissions"]
        )


def test_policy_preview_validation_returns_422(client: TestClient) -> None:
    response = client.post(
        "/api/v1/policies/preview",
        json={"enforcement": {"counterKeyExpression": '@("group")'}},
    )

    assert response.status_code == 422


def test_principal_update_rejects_null_kind(client: TestClient) -> None:
    principal = client.post(
        "/api/v1/principals",
        json={
            "objectId": USER_ID,
            "kind": "user",
        },
    ).json()

    response = client.patch(
        f"/api/v1/principals/{principal['id']}",
        json={"kind": None},
    )

    assert response.status_code == 422


def test_local_authentication_has_no_live_directory_lookup(client: TestClient) -> None:
    assert client.app.state.directory_lookup is None
    status_response = client.get("/api/v1/directory/status")
    assert status_response.status_code == 200
    assert status_response.json()["lookupEnabled"] is False
    assert status_response.json()["message"] == (
        "Directory lookup requires Entra authentication. Use manual entry instead."
    )

    response = client.get("/api/v1/directory/search", params={"kind": "user", "q": "Ada"})
    assert response.status_code == 409
    assert response.json()["code"] == "directory_disabled"
    assert response.json()["message"] == status_response.json()["message"]


def test_directory_status_reports_granted_and_missing_graph_permissions(
    directory_client: TestClient,
) -> None:
    install_directory_lookup(
        directory_client.app,
        FakeDirectoryLookup(
            [],
            application_permissions=[
                "User.ReadBasic.All",
                "GroupMember.Read.All",
                "AgentIdentity.Read.All",
                "ProfilePhoto.Read.All",
            ],
        ),
    )

    response = directory_client.get(
        "/api/v1/directory/status", params={"includePermissions": "true"}
    )

    assert response.status_code == 200
    permissions = {
        item["permission"]: item for item in response.json()["permissions"]
    }
    assert permissions["User.ReadBasic.All"]["granted"] is True
    assert permissions["ProfilePhoto.Read.All"]["granted"] is True
    assert permissions["Application.Read.All"]["granted"] is False
    assert permissions["Application.Read.All"]["required"] is False
    assert response.json()["permissionStatusMessage"] is None


def test_directory_status_keeps_permissions_visible_when_the_probe_fails(
    directory_client: TestClient,
) -> None:
    install_directory_lookup(
        directory_client.app,
        FakeDirectoryLookup([], unavailable=True),
    )

    response = directory_client.get(
        "/api/v1/directory/status", params={"includePermissions": "true"}
    )

    assert response.status_code == 200
    assert response.json()["permissionStatusMessage"] == (
        "Microsoft Graph directory read failed"
    )
    assert all(
        permission["granted"] is None for permission in response.json()["permissions"]
    )


def test_directory_search_when_lookup_is_on_and_principal_annotation(
    directory_client: TestClient,
) -> None:
    _install_lookup(directory_client)
    principal = directory_client.post(
        "/api/v1/principals",
        json={"objectId": USER_ID.upper(), "kind": "user"},
    ).json()

    response = directory_client.get(
        "/api/v1/directory/search", params={"kind": "user", "q": "ada", "limit": 5}
    )

    assert response.status_code == 200
    assert response.json()[0]["objectId"] == USER_ID
    assert response.json()[0]["principalId"] == principal["id"]


def test_application_lookup_and_registration_preserve_manual_semantics(
    directory_client: TestClient,
) -> None:
    install_directory_lookup(
        directory_client.app,
        FakeDirectoryLookup(
            [
                DirectoryObject(
                    object_id=USER_ID,
                    kind=PrincipalKind.MANAGED_IDENTITY,
                    display_name="Build workload",
                ),
                DirectoryObject(
                    object_id=AGENT_ID,
                    kind=PrincipalKind.AGENT_IDENTITY,
                    display_name="Build agent",
                ),
            ]
        ),
    )
    response = directory_client.get(
        "/api/v1/directory/search", params={"kind": "application", "q": "Build", "limit": 5}
    )
    assert response.status_code == 200
    assert [item["kind"] for item in response.json()] == ["managedIdentity"]
    created = directory_client.post(
        "/api/v1/principals",
        json={"objectId": USER_ID, "kind": "managedIdentity", "label": "Build workload"},
    )
    assert created.status_code == 201
    assert created.json()["directoryVerifiedAt"] is None
    again = directory_client.get(
        "/api/v1/directory/search", params={"kind": "application", "q": "Build"}
    )
    assert again.json()[0]["principalId"] == created.json()["id"]
    assert (
        directory_client.get(
            "/api/v1/directory/search", params={"kind": "application", "q": "Build", "limit": 26}
        ).status_code
        == 422
    )


def test_verify_on_create_success_missing_object_kind_mismatch_and_lowercasing(
    directory_client: TestClient,
) -> None:
    _install_lookup(directory_client)

    created = directory_client.post(
        "/api/v1/principals",
        json={"objectId": AGENT_USER_ID.upper(), "kind": "agentUser"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["objectId"] == AGENT_USER_ID
    assert body["label"] == "Research agent user"
    assert body["detail"] == "research-agent@example.com"
    assert body["identityParentId"] == AGENT_ID
    assert body["directoryVerifiedAt"] is not None

    missing = directory_client.post(
        "/api/v1/principals",
        json={"objectId": MISSING_ID, "kind": "user"},
    )
    assert missing.status_code == 422
    assert "not found in Entra" in missing.json()["message"]

    mismatch = directory_client.post(
        "/api/v1/principals",
        json={"objectId": AGENT_ID, "kind": "user"},
    )
    assert mismatch.status_code == 422
    assert "agent identity" in mismatch.json()["message"]


def test_mosaic_group_membership_refuses_security_groups(directory_client: TestClient) -> None:
    _install_lookup(directory_client)
    group = directory_client.post("/api/v1/groups", json={"name": "MOSAIC local group"}).json()
    principal = directory_client.post(
        "/api/v1/principals", json={"objectId": GROUP_ID, "kind": "securityGroup"}
    ).json()

    response = directory_client.put(f"/api/v1/groups/{group['id']}/members/{principal['id']}")

    assert response.status_code == 422
    assert "can't contain an Entra security group" in response.json()["message"]


def test_members_endpoint_returns_transitive_security_group_members(
    directory_client: TestClient,
) -> None:
    _install_lookup(directory_client)
    existing_user = directory_client.post(
        "/api/v1/principals", json={"objectId": USER_ID, "kind": "user"}
    ).json()
    group = directory_client.post(
        "/api/v1/principals", json={"objectId": GROUP_ID, "kind": "securityGroup"}
    ).json()

    response = directory_client.get(f"/api/v1/principals/{group['id']}/members")

    assert response.status_code == 200
    members = response.json()["members"]
    assert {item["objectId"] for item in members} == {USER_ID, AGENT_USER_ID, NESTED_GROUP_ID}
    assert next(item for item in members if item["objectId"] == USER_ID)["principalId"] == (
        existing_user["id"]
    )


def test_members_endpoint_rejects_non_security_group(directory_client: TestClient) -> None:
    _install_lookup(directory_client)
    principal = directory_client.post(
        "/api/v1/principals", json={"objectId": USER_ID, "kind": "user"}
    ).json()

    response = directory_client.get(f"/api/v1/principals/{principal['id']}/members")

    assert response.status_code == 409


def test_kind_change_rules_allow_same_subject_family(directory_client: TestClient) -> None:
    _install_lookup(directory_client)
    principal = directory_client.post(
        "/api/v1/principals", json={"objectId": AGENT_USER_ID, "kind": "agentUser"}
    ).json()

    response = directory_client.patch(
        f"/api/v1/principals/{principal['id']}",
        json={"kind": "user"},
    )

    assert response.status_code == 422
    assert "agent user" in response.json()["message"]


@pytest.mark.parametrize("format", ["PNG", "JPEG"])
def test_photo_endpoint_returns_authenticated_binary_without_registering_user(
    directory_client: TestClient, format: str
) -> None:
    photo = photo_image(format)
    install_directory_lookup(
        directory_client.app,
        FakeDirectoryLookup(
            [DirectoryObject(object_id=USER_ID, kind=PrincipalKind.USER)],
            photos={USER_ID: photo},
        ),
    )
    response = directory_client.get(f"/api/v1/directory/objects/{USER_ID.upper()}/photo")
    assert response.status_code == 200
    assert response.content == photo.content
    assert response.headers["Content-Type"] == photo.content_type
    assert response.headers["Cache-Control"] == "no-store, private"
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert directory_client.get("/api/v1/principals").json() == []
    assert not directory_client.app.state.repository.audit_events


@pytest.mark.parametrize("object_id", [USER_ID, AGENT_USER_ID, AGENT_ID, GROUP_ID, MISSING_ID])
def test_photo_missing_or_non_person_is_expected_not_found(
    directory_client: TestClient, object_id: str
) -> None:
    lookup = _lookup()
    lookup.photos = {key: photo_image() for key in [AGENT_USER_ID, AGENT_ID, GROUP_ID]}
    install_directory_lookup(directory_client.app, lookup)
    response = directory_client.get(f"/api/v1/directory/objects/{object_id}/photo")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_photo_disabled_does_not_disable_manual_directory_onboarding(client: TestClient) -> None:
    response = client.get(f"/api/v1/directory/objects/{USER_ID}/photo")
    assert response.status_code == 409
    assert response.json()["code"] == "directory_disabled"
    assert client.post(
        "/api/v1/principals", json={"objectId": USER_ID, "kind": "user"}
    ).status_code == 201


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (
            DirectoryForbiddenError("Consent ProfilePhoto.Read.All separately"),
            403,
            "directory_forbidden",
        ),
        (DirectoryError("Invalid photo"), 502, "directory_unavailable"),
        (
            DirectoryThrottledError("Photo read throttled", retry_after=23),
            429,
            "directory_throttled",
        ),
    ],
)
def test_photo_errors_do_not_affect_onboarding(
    directory_client: TestClient,
    error: DirectoryError | DirectoryForbiddenError,
    status: int,
    code: str,
) -> None:
    class PhotoFailure(FakeDirectoryLookup):
        async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
            raise error

    lookup = PhotoFailure(
        [DirectoryObject(object_id=USER_ID, kind=PrincipalKind.USER, display_name="Ada")]
    )
    install_directory_lookup(directory_client.app, lookup)
    response = directory_client.get(f"/api/v1/directory/objects/{USER_ID}/photo")
    assert response.status_code == status
    assert response.json()["code"] == code
    if status == 429:
        assert response.headers["Retry-After"] == "23"
        assert response.json()["details"]["retryAfter"] == 23
    assert directory_client.get(
        "/api/v1/directory/search", params={"kind": "user", "q": "Ada"}
    ).status_code == 200
    created = directory_client.post(
        "/api/v1/principals", json={"objectId": USER_ID, "kind": "user"}
    )
    assert created.status_code == 201
    assert created.json()["directoryVerifiedAt"] is not None
    for kind in ["servicePrincipal", "managedIdentity"]:
        application = directory_client.post(
            "/api/v1/principals", json={"objectId": f"legacy-{kind}", "kind": kind}
        )
        assert application.status_code == 201
        assert application.json()["directoryVerifiedAt"] is None


@pytest.mark.parametrize("object_id", ["not-a-guid", "https:%2F%2Fexternal.example"])
def test_photo_rejects_non_object_id_without_lookup(
    directory_client: TestClient, object_id: str
) -> None:
    class NeverRead(FakeDirectoryLookup):
        async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
            pytest.fail("Invalid object IDs must not reach Graph")

    install_directory_lookup(directory_client.app, NeverRead([]))
    response = directory_client.get(f"/api/v1/directory/objects/{object_id}/photo")
    assert response.status_code in {404, 422}


@pytest.mark.parametrize(
    ("tenant_id", "roles"), [("tenant-test", ["User"]), ("other-tenant", ["Admin"])]
)
def test_photo_admin_and_tenant_gates_precede_lookup(
    directory_client: TestClient, tenant_id: str, roles: list[str]
) -> None:
    class NeverRead(FakeDirectoryLookup):
        async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
            pytest.fail("Unauthorized callers must not reach the photo reader")

    class Caller:
        async def authenticate(self, _request: Request) -> AuthContext:
            return AuthContext(
                object_id=USER_ID, tenant_id=tenant_id, roles=frozenset(roles)
            )

    install_directory_lookup(directory_client.app, NeverRead([]))
    directory_client.app.state.authenticator = Caller()
    response = directory_client.get(f"/api/v1/directory/objects/{USER_ID}/photo")
    assert response.status_code == 403


def test_photo_requires_bearer_auth_before_graph_in_entra_mode() -> None:
    from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
    from mosaic_api.main import create_app

    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        api_client_id="api-client",
    )
    with TestClient(create_app(settings)) as client:
        response = client.get(f"/api/v1/directory/objects/{USER_ID}/photo")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_photo_rejects_signed_foreign_tenant_token_before_lookup(
    directory_client: TestClient,
) -> None:
    class NeverRead(FakeDirectoryLookup):
        async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
            pytest.fail("A foreign-tenant token must not reach the photo reader")

    settings = directory_client.app.state.settings.model_copy(
        update={"auth_mode": AuthMode.ENTRA, "api_client_id": "photo-client"}
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(500))
    )
    authenticator = EntraAuthenticator(settings, client=http_client)
    authenticator._keys = {"photo-key": key.public_key()}
    authenticator._expires_at = time.monotonic() + 3600
    directory_client.app.state.authenticator = authenticator
    directory_client.app.state.settings = settings
    install_directory_lookup(directory_client.app, NeverRead([]))
    now = int(time.time())
    token = jwt.encode(
        {
            "iss": settings.issuer,
            "aud": settings.api_client_id,
            "tid": "foreign-tenant",
            "oid": USER_ID,
            "iat": now,
            "exp": now + 600,
            "roles": ["Admin"],
        },
        key,
        algorithm="RS256",
        headers={"kid": "photo-key"},
    )
    try:
        response = directory_client.get(
            f"/api/v1/directory/objects/{USER_ID}/photo",
            headers={"Authorization": f"Bearer {token}"},
        )
    finally:
        asyncio.run(http_client.aclose())
    assert response.status_code == 401
    assert response.json()["detail"] == "Access token tenant is not allowed"


def test_photo_throttle_header_is_exposed_to_configured_console_origin(
    directory_client: TestClient,
) -> None:
    class Throttled(FakeDirectoryLookup):
        async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
            raise DirectoryThrottledError("Photo throttled", retry_after=12)

    install_directory_lookup(directory_client.app, Throttled([]))
    response = directory_client.get(
        f"/api/v1/directory/objects/{USER_ID}/photo",
        headers={"Origin": "http://localhost:5173"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "12"
    assert "Retry-After" in response.headers["Access-Control-Expose-Headers"]
