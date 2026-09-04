import json
import time

from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, SERVICE_NAME, FakeApim
from conftest import build_gateway_service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import Settings
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryGatewayRepository


def _register(client: TestClient, **payload: str) -> dict[str, object]:
    response = client.post(
        "/api/v1/gateways", json={"azureResourceId": RESOURCE_ID, **payload}
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _sync(client: TestClient, gateway_id: str) -> dict[str, object]:
    started = client.post(f"/api/v1/gateways/{gateway_id}/sync")
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    for _ in range(100):
        run = client.get(f"/api/v1/gateways/{gateway_id}/sync-runs/{run_id}").json()
        if run["status"] != "running":
            return dict(run)
        time.sleep(0.02)
    raise AssertionError("sync did not finish")


def test_gateway_registration_and_listing(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client, name="Development gateway")

    assert gateway["serviceName"] == SERVICE_NAME
    assert gateway["managementMode"] == "observe"
    assert gateway["status"] == "connected"
    assert gateway["access"]["canRead"] is True

    listed = gateway_client.get("/api/v1/gateways")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [gateway["id"]]


def test_duplicate_registration_is_rejected(gateway_client: TestClient) -> None:
    _register(gateway_client)

    response = gateway_client.post("/api/v1/gateways", json={"azureResourceId": RESOURCE_ID})

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_invalid_resource_id_is_rejected(gateway_client: TestClient) -> None:
    response = gateway_client.post("/api/v1/gateways", json={"azureResourceId": "nonsense"})

    assert response.status_code == 422


def test_unknown_gateway_returns_not_found(gateway_client: TestClient) -> None:
    response = gateway_client.get("/api/v1/gateways/gateway_missing")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_suggested_gateway_is_offered_then_marked_registered(
    gateway_client: TestClient,
) -> None:
    before = gateway_client.get("/api/v1/gateways/suggested").json()
    assert before[0]["alreadyRegistered"] is False

    gateway = _register(gateway_client)
    after = gateway_client.get("/api/v1/gateways/suggested").json()

    assert after[0]["alreadyRegistered"] is True
    assert after[0]["gatewayId"] == gateway["id"]


def test_preflight_can_be_rerun(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)

    response = gateway_client.post(f"/api/v1/gateways/{gateway['id']}/preflight")

    assert response.status_code == 200
    assert response.json()["access"]["checkedAt"] is not None


def test_management_mode_is_refused_without_verified_write_access(
    gateway_client: TestClient,
) -> None:
    gateway = _register(gateway_client)

    response = gateway_client.patch(
        f"/api/v1/gateways/{gateway['id']}", json={"managementMode": "manage"}
    )

    assert response.status_code == 422
    assert "cannot write" in response.json()["message"]
    assert response.json()["details"]["missingActions"]


def test_management_mode_is_allowed_once_write_access_is_verified(
    settings: Settings,
    gateway_repository: InMemoryGatewayRepository,
) -> None:
    fake = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
    app: FastAPI = create_app(settings)
    with TestClient(app) as client:
        app.state.gateway_repository = gateway_repository
        app.state.gateway_service = build_gateway_service(fake, gateway_repository)
        gateway = _register(client)
        assert gateway["access"]["canWrite"] is True

        response = client.patch(
            f"/api/v1/gateways/{gateway['id']}", json={"managementMode": "manage"}
        )

    assert response.status_code == 200, response.text
    assert response.json()["managementMode"] == "manage"


def test_sync_populates_every_inventory_view(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)
    gateway_id = str(gateway["id"])

    run = _sync(gateway_client, gateway_id)
    assert run["status"] == "succeeded"

    apis = gateway_client.get(f"/api/v1/gateways/{gateway_id}/apis").json()
    operations = gateway_client.get(f"/api/v1/gateways/{gateway_id}/operations").json()
    products = gateway_client.get(f"/api/v1/gateways/{gateway_id}/products").json()
    subscriptions = gateway_client.get(f"/api/v1/gateways/{gateway_id}/subscriptions").json()
    users = gateway_client.get(f"/api/v1/gateways/{gateway_id}/users").json()
    groups = gateway_client.get(f"/api/v1/gateways/{gateway_id}/groups").json()
    backends = gateway_client.get(f"/api/v1/gateways/{gateway_id}/backends").json()
    named_values = gateway_client.get(f"/api/v1/gateways/{gateway_id}/named-values").json()

    assert len(apis) == 2
    assert len(operations) == 2
    assert products[0]["displayName"] == "Gold tier"
    assert subscriptions[0]["scopeName"] == "gold"
    assert users[0]["entraObjectId"] == "11111111-2222-3333-4444-555555555555"
    assert groups[0]["displayName"] == "Developers"
    assert backends[0]["aiKind"] == "azureOpenAi"
    assert named_values[0]["secret"] is True


def test_operations_can_be_filtered_by_api(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)
    gateway_id = str(gateway["id"])
    _sync(gateway_client, gateway_id)

    response = gateway_client.get(
        f"/api/v1/gateways/{gateway_id}/operations", params={"api": "chat-api"}
    )

    assert [item["apiName"] for item in response.json()] == ["chat-api"]


def test_policy_endpoint_returns_plain_language_only(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)
    gateway_id = str(gateway["id"])
    _sync(gateway_client, gateway_id)

    response = gateway_client.get(f"/api/v1/gateways/{gateway_id}/policies")
    body = response.json()
    serialized = json.dumps(body)

    assert response.status_code == 200
    assert "<" not in serialized
    assert "sk-live-not-a-real-key" not in serialized
    assert body["unrecognizedCount"] == 1
    assert body["mosaicManagedCount"] == 1
    assert any(
        "tokens per minute" in facet["summary"]
        for document in body["documents"]
        for facet in document["facets"]
    )


def test_operation_policy_is_available_on_demand(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)
    gateway_id = str(gateway["id"])

    response = gateway_client.get(
        f"/api/v1/gateways/{gateway_id}/apis/chat-api/operations/chat-completions/policy"
    )

    assert response.status_code == 200
    assert response.json()["exists"] is False


def test_sync_runs_are_listed_most_recent_first(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)
    gateway_id = str(gateway["id"])
    _sync(gateway_client, gateway_id)
    _sync(gateway_client, gateway_id)

    runs = gateway_client.get(f"/api/v1/gateways/{gateway_id}/sync-runs").json()

    assert len(runs) == 2
    assert runs[0]["startedAt"] >= runs[1]["startedAt"]


def test_deleting_a_gateway_clears_its_observed_state(gateway_client: TestClient) -> None:
    gateway = _register(gateway_client)
    gateway_id = str(gateway["id"])
    _sync(gateway_client, gateway_id)

    deleted = gateway_client.delete(f"/api/v1/gateways/{gateway_id}")

    assert deleted.status_code == 204
    assert gateway_client.get("/api/v1/gateways").json() == []
    assert gateway_client.get(f"/api/v1/gateways/{gateway_id}/apis").status_code == 404
