from fastapi.testclient import TestClient


def test_group_principal_membership_lifecycle(client: TestClient) -> None:
    principal_response = client.post(
        "/api/v1/principals",
        json={
            "objectId": "11111111-1111-1111-1111-111111111111",
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
        "objectId": "22222222-2222-2222-2222-222222222222",
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
            "objectId": "33333333-3333-3333-3333-333333333333",
            "kind": "user",
        },
    ).json()

    response = client.patch(
        f"/api/v1/principals/{principal['id']}",
        json={"kind": None},
    )

    assert response.status_code == 422
