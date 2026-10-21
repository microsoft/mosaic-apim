import asyncio
from collections.abc import Iterator

import pytest
from aoai_double import AI_RESOURCE_ID, FakeDeploymentServices
from conftest import _no_sleep, build_aoai_arm_client
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.aoai.writer import ModelDeploymentWriter
from mosaic_api.services.model_deployments import ModelDeploymentService
from test_model_deployments import configuration


@pytest.fixture
def fake_aoai() -> FakeDeploymentServices:
    return FakeDeploymentServices()


@pytest.fixture
def deployments_client(
    endpoint_client: TestClient,
    fake_aoai: FakeDeploymentServices,
) -> Iterator[TestClient]:
    state = endpoint_client.app.state
    arm = build_aoai_arm_client(fake_aoai)
    service = ModelDeploymentService(
        state.model_endpoint_repository,
        gateway_repository=state.gateway_repository,
        endpoint_service=state.model_endpoint_service,
        client_factory=lambda resource: CognitiveServicesClient(arm, resource),
        writer_factory=lambda resource: ModelDeploymentWriter(arm, resource),
        sleep=_no_sleep,
        poll_attempts=3,
    )
    state.model_deployment_service = service
    yield endpoint_client
    endpoint_client.portal.call(service.aclose)


def test_registration_review_deploy_status_and_inventory(
    deployments_client: TestClient,
    fake_aoai: FakeDeploymentServices,
) -> None:
    client = deployments_client
    registered = client.post("/api/v1/model-endpoints", json={"azureResourceId": AI_RESOURCE_ID})
    assert registered.status_code == 201
    endpoint = registered.json()
    assert endpoint["managementMode"] == "observe"
    base = f"/api/v1/model-endpoints/{endpoint['id']}"
    catalogue = client.get(f"{base}/deployable-models")
    assert catalogue.status_code == 200
    assert catalogue.json()["models"][0]["skus"][0]["quota"]["available"] == 100
    body = configuration().model_dump(mode="json")
    assert client.post(f"{base}/deployment-plans", json=body).status_code == 409
    assert client.patch(base, json={"managementMode": None}).status_code == 422
    assert client.patch(base, json={"managementMode": "manage"}).status_code == 200
    planned = client.post(f"{base}/deployment-plans", json=body)
    assert planned.status_code == 200, planned.text
    plan_id = planned.json()["id"]
    assert not fake_aoai.writes
    applied = client.post(f"{base}/deployment-plans/{plan_id}/apply")
    assert applied.status_code == 202, applied.text
    run_id = applied.json()["id"]
    client.portal.call(asyncio.gather, *tuple(client.app.state.model_deployment_service._tasks))
    run = client.get(f"{base}/deployment-runs/{run_id}").json()
    assert run["status"] == "succeeded"
    assert run["refreshStatus"] == "succeeded"
    assert len(fake_aoai.writes) == 1
    assert client.post(f"{base}/deployment-plans/{plan_id}/apply").json()["id"] == run_id
    assert client.get(f"{base}/deployment-runs").json()[0]["id"] == run_id
    assert any(
        item["deploymentName"] == "new-model" for item in client.get(f"{base}/deployments").json()
    )
    assert client.get(f"{base}/deployment-runs/deploymentrun_unknown").status_code == 404
    assert client.get(f"{base}/deployment-runs/{plan_id}").status_code == 404


@pytest.mark.parametrize(
    "suffix,method",
    [
        ("deployable-models", "GET"),
        ("deployment-access", "GET"),
        ("deployment-runs", "GET"),
        ("deployment-plans", "POST"),
        ("deployment-plans/deploymentplan_x/apply", "POST"),
        ("deployment-runs/deploymentrun_x", "GET"),
        ("deployment-runs/deploymentrun_x/reconcile", "POST"),
        ("deployment-runs/deploymentrun_x/refresh", "POST"),
    ],
)
def test_every_deployment_route_requires_administrator(
    deployments_client: TestClient,
    suffix: str,
    method: str,
) -> None:
    deployments_client.app.state.authenticator = LocalAuthenticator("tenant-test", roles=("User",))
    response = deployments_client.request(
        method,
        f"/api/v1/model-endpoints/endpoint_x/{suffix}",
        json=configuration().model_dump(mode="json") if suffix == "deployment-plans" else {},
    )
    assert response.status_code == 403
