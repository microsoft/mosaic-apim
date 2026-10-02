import time
from collections.abc import Iterator
from typing import Any

import pytest
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import build_gateway_service, build_mcp_publishing_service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    McpEndpoint,
    McpEndpointStatus,
    McpInventorySummary,
    Principal,
    PrincipalKind,
)
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryMcpEndpointRepository

TENANT_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
def mcp_publishing_client(settings: Settings) -> Iterator[TestClient]:
    fake_apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
    gateway_repository = InMemoryGatewayRepository()
    mcp_repository = InMemoryMcpEndpointRepository()
    app: FastAPI = create_app(
        settings.model_copy(
            update={
                "tenant_id": TENANT_ID,
                "model_runtime_client_id": "22222222-2222-2222-2222-222222222222",
            }
        )
    )
    with TestClient(app) as client:
        app.state.gateway_repository = gateway_repository
        app.state.mcp_endpoint_repository = mcp_repository
        app.state.gateway_service = build_gateway_service(fake_apim, gateway_repository)
        app.state.mcp_publishing_service = build_mcp_publishing_service(
            fake_apim,
            gateway_repository,
            mcp_repository,
            directory_repository=app.state.repository,
            entitlement_repository=app.state.entitlement_repository,
        )
        client.fake_apim = fake_apim  # type: ignore[attr-defined]
        client.mcp_repository = mcp_repository  # type: ignore[attr-defined]
        yield client


def _onboard_gateway(client: TestClient) -> str:
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
    patched = client.patch(f"/api/v1/gateways/{gateway_id}", json={"managementMode": "manage"})
    assert patched.status_code == 200, patched.text
    return str(gateway_id)


def _seed_endpoint(client: TestClient) -> str:
    endpoint = McpEndpoint(
        id="mcp-endpoint-orders",
        tenant_id=TENANT_ID,
        name="Orders MCP",
        endpoint="https://mcp.contoso.test/mcp",
        status=McpEndpointStatus.CONNECTED,
        inventory=McpInventorySummary(tools=2),
    )
    client.mcp_repository.endpoints[endpoint.id] = endpoint  # type: ignore[attr-defined]
    return endpoint.id


def _await_run(client: TestClient, publication_id: str, run_id: str) -> dict[str, Any]:
    for _ in range(200):
        run = client.get(f"/api/v1/mcp-publications/{publication_id}/runs/{run_id}").json()
        if run["status"] != "running":
            return dict(run)
        time.sleep(0.02)
    raise AssertionError("MCP apply did not finish")


def test_mcp_publication_http_round_trip(mcp_publishing_client: TestClient) -> None:
    gateway_id = _onboard_gateway(mcp_publishing_client)
    endpoint_id = _seed_endpoint(mcp_publishing_client)

    capability = mcp_publishing_client.get(f"/api/v1/gateways/{gateway_id}/mcp-publishing")
    assert capability.status_code == 200
    assert capability.json()["supported"] is True

    created = mcp_publishing_client.post(
        "/api/v1/mcp-publications",
        json={"gatewayId": gateway_id, "mcpEndpointId": endpoint_id},
    )
    assert created.status_code == 201, created.text
    publication_id = created.json()["id"]
    assert created.json()["mcpServerId"]

    listed = mcp_publishing_client.get("/api/v1/mcp-publications", params={"gateway": gateway_id})
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [publication_id]

    patched = mcp_publishing_client.patch(
        f"/api/v1/mcp-publications/{publication_id}", json={"displayName": "Orders tools"}
    )
    assert patched.status_code == 200
    assert patched.json()["displayName"] == "Orders tools"

    plan = mcp_publishing_client.post(f"/api/v1/mcp-publications/{publication_id}/plan")
    assert plan.status_code == 200, plan.text
    assert plan.json()["target"] == "mcp"
    assert plan.json()["mcpAccessSnapshot"]["audience"] == "22222222-2222-2222-2222-222222222222"
    by_id = mcp_publishing_client.get(f"/api/v1/mcp-publish-plans/{plan.json()['id']}")
    assert by_id.status_code == 200

    applied = mcp_publishing_client.post(
        f"/api/v1/mcp-publications/{publication_id}/apply",
        params={"plan": plan.json()["id"]},
    )
    assert applied.status_code == 202, applied.text
    run = _await_run(mcp_publishing_client, publication_id, applied.json()["id"])
    assert run["status"] == "succeeded"

    runs = mcp_publishing_client.get(f"/api/v1/mcp-publications/{publication_id}/runs")
    assert runs.status_code == 200
    assert [item["id"] for item in runs.json()] == [run["id"]]
    lock = mcp_publishing_client.get(f"/api/v1/mcp-publications/{publication_id}/lock")
    assert lock.status_code == 200
    assert lock.json() == {"publicationId": publication_id, "ownerId": None}

    unreviewed = mcp_publishing_client.post(
        f"/api/v1/mcp-publications/{publication_id}/unpublish"
    )
    assert unreviewed.status_code == 409
    assert unreviewed.json()["details"]["reason"] == "planRequired"
    review = mcp_publishing_client.post(
        f"/api/v1/mcp-publications/{publication_id}/unpublish-plan"
    )
    assert review.status_code == 200, review.text
    assert review.json()["target"] == "mcp"
    assert review.json()["operation"] == "unpublish"
    assert {step["action"] for step in review.json()["steps"]} == {"delete"}
    unpublished = mcp_publishing_client.post(
        f"/api/v1/mcp-publications/{publication_id}/unpublish",
        params={"plan": review.json()["id"]},
    )
    assert unpublished.status_code == 202
    unpublish_run = _await_run(mcp_publishing_client, publication_id, unpublished.json()["id"])
    assert unpublish_run["status"] == "succeeded"
    after = mcp_publishing_client.get(f"/api/v1/mcp-publications/{publication_id}").json()
    assert after["unpublishedAt"] is not None

    deleted = mcp_publishing_client.delete(f"/api/v1/mcp-publications/{publication_id}")
    assert deleted.status_code == 204


def test_mcp_model_caller_http_round_trip(mcp_publishing_client: TestClient) -> None:
    gateway_id = _onboard_gateway(mcp_publishing_client)
    endpoint_id = _seed_endpoint(mcp_publishing_client)
    created = mcp_publishing_client.post(
        "/api/v1/mcp-publications",
        json={"gatewayId": gateway_id, "mcpEndpointId": endpoint_id},
    )
    publication_id = created.json()["id"]
    app = Principal(
        id="principal-search-app",
        tenant_id=TENANT_ID,
        object_id="aaaabbbb-cccc-dddd-eeee-ffff00001111",
        kind=PrincipalKind.SERVICE_PRINCIPAL,
        label="Contoso Search App",
    )
    directory = mcp_publishing_client.app.state.repository  # type: ignore[attr-defined]
    directory.principals[app.id] = app
    route = f"/api/v1/mcp-publications/{publication_id}/model-caller"

    named = mcp_publishing_client.put(route, json={"principalId": app.id})
    assert named.status_code == 200, named.text
    assert named.json()["modelCallerId"] == app.id
    plan = mcp_publishing_client.post(f"/api/v1/mcp-publications/{publication_id}/plan")
    assert plan.json()["mcpAccessSnapshot"]["modelCaller"] == {
        "principalId": app.id,
        "objectId": app.object_id,
        "displayName": "Contoso Search App",
    }

    assert mcp_publishing_client.put(route, json={}).status_code == 422
    assert mcp_publishing_client.put(route, json={"principalId": "missing"}).status_code == 404
    cleared = mcp_publishing_client.delete(route)
    assert cleared.status_code == 200
    assert "modelCallerId" not in cleared.json()
    replanned = mcp_publishing_client.post(f"/api/v1/mcp-publications/{publication_id}/plan")
    assert "modelCaller" not in replanned.json()["mcpAccessSnapshot"]
    unknown = "/api/v1/mcp-publications/unknown/model-caller"
    assert mcp_publishing_client.put(unknown, json={"principalId": app.id}).status_code == 404
    assert mcp_publishing_client.delete(unknown).status_code == 404


def test_mcp_publishing_routes_are_admin_only() -> None:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT_ID,
        local_roles=["User"],
        model_runtime_client_id="22222222-2222-2222-2222-222222222222",
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/v1/mcp-publications").status_code == 403
        assert client.post("/api/v1/mcp-publications", json={}).status_code == 403
        assert client.get("/api/v1/mcp-publish-plans/unknown").status_code == 403


def test_mcp_publishing_unknown_ids_return_not_found(
    mcp_publishing_client: TestClient,
) -> None:
    gateway_id = _onboard_gateway(mcp_publishing_client)

    assert (
        mcp_publishing_client.get("/api/v1/gateways/unknown/mcp-publishing").status_code
        == 404
    )
    assert mcp_publishing_client.get("/api/v1/mcp-publications/unknown").status_code == 404
    assert (
        mcp_publishing_client.patch(
            "/api/v1/mcp-publications/unknown", json={"displayName": "Nope"}
        ).status_code
        == 404
    )
    assert mcp_publishing_client.delete("/api/v1/mcp-publications/unknown").status_code == 404
    assert mcp_publishing_client.post("/api/v1/mcp-publications/unknown/plan").status_code == 404
    assert mcp_publishing_client.get("/api/v1/mcp-publish-plans/unknown").status_code == 404
    assert (
        mcp_publishing_client.post("/api/v1/mcp-publications/unknown/apply").status_code
        == 404
    )
    assert mcp_publishing_client.get("/api/v1/mcp-publications/unknown/runs").status_code == 404
    assert (
        mcp_publishing_client.get("/api/v1/mcp-publications/unknown/runs/unknown").status_code
        == 404
    )
    assert (
        mcp_publishing_client.get(f"/api/v1/gateways/{gateway_id}/mcp-publishing").json()[
            "gatewayId"
        ]
        == gateway_id
    )
