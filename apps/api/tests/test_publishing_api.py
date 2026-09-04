"""The publishing HTTP surface: contracts, status codes, and refusals."""

import time
from typing import Any

import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import build_endpoint_service, build_gateway_service, build_publishing_service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import Settings
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository

DEPLOYMENT = "gpt-4o-prod"
ENFORCEMENT: dict[str, Any] = {
    "counterKeyExpression": "@(context.Subscription.Id)",
    "tokensPerMinute": 10000,
}


@pytest.fixture
def publishing_client(settings: Settings) -> Any:
    fake_apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
    fake_aoai = FakeCognitiveServices()
    gateway_repository = InMemoryGatewayRepository()
    endpoint_repository = InMemoryModelEndpointRepository()
    app: FastAPI = create_app(settings)
    with TestClient(app) as client:
        app.state.gateway_repository = gateway_repository
        app.state.model_endpoint_repository = endpoint_repository
        app.state.gateway_service = build_gateway_service(fake_apim, gateway_repository)
        app.state.model_endpoint_service = build_endpoint_service(
            fake_aoai,
            repository=endpoint_repository,
            gateway_repository=gateway_repository,
        )
        app.state.publishing_service = build_publishing_service(
            fake_apim, gateway_repository, endpoint_repository
        )
        client.fake_apim = fake_apim  # type: ignore[attr-defined]
        yield client


def _await_run(client: TestClient, publication_id: str, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        run = client.get(f"/api/v1/publications/{publication_id}/runs/{run_id}").json()
        if run["status"] != "running":
            return dict(run)
        time.sleep(0.02)
    raise AssertionError("apply did not finish")


def _onboard(client: TestClient, *, manage: bool = True) -> tuple[str, str]:
    gateway = client.post("/api/v1/gateways", json={"azureResourceId": RESOURCE_ID})
    assert gateway.status_code == 201, gateway.text
    gateway_id = gateway.json()["id"]

    started = client.post(f"/api/v1/gateways/{gateway_id}/sync")
    assert started.status_code == 202
    run_id = started.json()["id"]
    for _ in range(200):
        run = client.get(f"/api/v1/gateways/{gateway_id}/sync-runs/{run_id}").json()
        if run["status"] != "running":
            break
        time.sleep(0.02)

    if manage:
        patched = client.patch(
            f"/api/v1/gateways/{gateway_id}", json={"managementMode": "manage"}
        )
        assert patched.status_code == 200, patched.text

    endpoint = client.post("/api/v1/model-endpoints", json={"azureResourceId": AI_RESOURCE_ID})
    assert endpoint.status_code == 201, endpoint.text
    endpoint_id = endpoint.json()["id"]
    started = client.post(f"/api/v1/model-endpoints/{endpoint_id}/sync")
    assert started.status_code == 202
    run_id = started.json()["id"]
    for _ in range(200):
        run = client.get(
            f"/api/v1/model-endpoints/{endpoint_id}/sync-runs/{run_id}"
        ).json()
        if run["status"] != "running":
            break
        time.sleep(0.02)
    return gateway_id, endpoint_id


def _create(client: TestClient, gateway_id: str, endpoint_id: str, **extra: Any) -> str:
    response = client.post(
        "/api/v1/publications",
        json={
            "gatewayId": gateway_id,
            "modelEndpointId": endpoint_id,
            "deploymentName": DEPLOYMENT,
            "enforcement": ENFORCEMENT,
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_publishable_models_are_offered_for_a_gateway(publishing_client: TestClient) -> None:
    gateway_id, _ = _onboard(publishing_client)

    response = publishing_client.get(f"/api/v1/gateways/{gateway_id}/publishable-models")

    assert response.status_code == 200
    names = [item["deploymentName"] for item in response.json()]
    assert DEPLOYMENT in names
    chosen = next(item for item in response.json() if item["deploymentName"] == DEPLOYMENT)
    assert chosen["suggestedApiPath"].startswith("mosaic/")
    assert chosen["publicationId"] is None


def test_publish_plan_apply_round_trip(publishing_client: TestClient) -> None:
    gateway_id, endpoint_id = _onboard(publishing_client)
    publication_id = _create(publishing_client, gateway_id, endpoint_id)

    plan = publishing_client.post(f"/api/v1/publications/{publication_id}/plan")
    assert plan.status_code == 200, plan.text
    body = plan.json()
    assert body["steps"]
    assert "policyXml" not in body
    assert all("<" not in facet["summary"] for facet in body["facets"])

    applied = publishing_client.post(
        f"/api/v1/publications/{publication_id}/apply", params={"plan": body["id"]}
    )
    assert applied.status_code == 202, applied.text
    run = _await_run(publishing_client, publication_id, applied.json()["id"])

    assert run["status"] == "succeeded"
    publication = publishing_client.get(f"/api/v1/publications/{publication_id}").json()
    assert publication["status"] == "published"
    assert publication["resources"]

    runs = publishing_client.get(f"/api/v1/publications/{publication_id}/runs")
    assert runs.status_code == 200
    assert [item["id"] for item in runs.json()] == [run["id"]]


def test_publications_can_be_filtered_by_gateway(publishing_client: TestClient) -> None:
    gateway_id, endpoint_id = _onboard(publishing_client)
    publication_id = _create(publishing_client, gateway_id, endpoint_id)

    listed = publishing_client.get("/api/v1/publications", params={"gateway": gateway_id})
    empty = publishing_client.get("/api/v1/publications", params={"gateway": "gateway_other"})

    assert [item["id"] for item in listed.json()] == [publication_id]
    assert empty.json() == []


def test_publishing_into_an_observed_gateway_is_refused(publishing_client: TestClient) -> None:
    gateway_id, endpoint_id = _onboard(publishing_client, manage=False)
    publication_id = _create(publishing_client, gateway_id, endpoint_id)

    response = publishing_client.post(f"/api/v1/publications/{publication_id}/plan")

    assert response.status_code == 409
    assert "observe mode" in response.json()["message"]


def test_an_invalid_api_name_is_rejected(publishing_client: TestClient) -> None:
    gateway_id, endpoint_id = _onboard(publishing_client)

    response = publishing_client.post(
        "/api/v1/publications",
        json={
            "gatewayId": gateway_id,
            "modelEndpointId": endpoint_id,
            "deploymentName": DEPLOYMENT,
            "enforcement": ENFORCEMENT,
            "apiName": "not valid!",
        },
    )

    assert response.status_code == 422


def test_unknown_publication_returns_not_found(publishing_client: TestClient) -> None:
    response = publishing_client.get("/api/v1/publications/publication_missing")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_unpublish_removes_what_was_published(publishing_client: TestClient) -> None:
    gateway_id, endpoint_id = _onboard(publishing_client)
    publication_id = _create(publishing_client, gateway_id, endpoint_id)
    plan = publishing_client.post(f"/api/v1/publications/{publication_id}/plan").json()
    applied = publishing_client.post(
        f"/api/v1/publications/{publication_id}/apply", params={"plan": plan["id"]}
    )
    _await_run(publishing_client, publication_id, applied.json()["id"])

    blocked = publishing_client.delete(f"/api/v1/publications/{publication_id}")
    assert blocked.status_code == 409

    removed = publishing_client.post(f"/api/v1/publications/{publication_id}/unpublish")
    assert removed.status_code == 202
    run = _await_run(publishing_client, publication_id, removed.json()["id"])
    assert run["status"] == "succeeded"

    deleted = publishing_client.delete(f"/api/v1/publications/{publication_id}")
    assert deleted.status_code == 204


def test_publishing_into_an_unknown_gateway_returns_not_found(
    publishing_client: TestClient,
) -> None:
    _, endpoint_id = _onboard(publishing_client)

    response = publishing_client.post(
        "/api/v1/publications",
        json={
            "gatewayId": "gateway_missing",
            "modelEndpointId": endpoint_id,
            "deploymentName": DEPLOYMENT,
            "enforcement": ENFORCEMENT,
        },
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
