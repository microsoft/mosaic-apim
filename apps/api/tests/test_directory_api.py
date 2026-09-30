from conftest import install_directory_lookup
from fastapi.testclient import TestClient
from mosaic_api.domain import DirectoryObject, PrincipalKind
from mosaic_api.integrations.graph import FakeDirectoryLookup

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
    from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
    from mosaic_api.main import create_app

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


def test_directory_search_when_lookup_is_off(client: TestClient) -> None:
    status_response = client.get("/api/v1/directory/status")
    assert status_response.status_code == 200
    assert status_response.json()["lookupEnabled"] is False
    assert status_response.json()["message"] == (
        "Directory search is off, so enter Entra object IDs by hand."
    )

    response = client.get("/api/v1/directory/search", params={"kind": "user", "q": "Ada"})
    assert response.status_code == 409
    assert response.json()["code"] == "directory_disabled"


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
