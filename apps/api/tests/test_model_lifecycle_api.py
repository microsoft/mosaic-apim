from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from aoai_double import AI_PROJECT_ID, AI_RESOURCE_ID, FakeCognitiveServices
from conftest import build_aoai_arm_client
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.model_lifecycle import DiscoveryRun, LifecycleRecord
from mosaic_api.services import model_lifecycle
from mosaic_api.services.model_lifecycle import ModelLifecycleService

OLD = {"format": "OpenAI", "name": "gpt-4o", "version": "2024-08-06"}
NEW = {"format": "OpenAI", "name": "gpt-4o", "version": "2024-11-20"}


@pytest.fixture
def discovery(endpoint_client: TestClient) -> Iterator[tuple[TestClient, FakeCognitiveServices]]:
    state = endpoint_client.app.state
    fake = FakeCognitiveServices()
    fake.models = [OLD]
    fake.deployments = [
        {
            "name": "existing-chat",
            "properties": {"model": OLD, "provisioningState": "Succeeded"},
        }
    ]
    arm = build_aoai_arm_client(fake)
    state.model_lifecycle_service = ModelLifecycleService(
        state.model_endpoint_repository,
        gateways=state.gateway_repository,
        environments=state.environment_repository,
        cost_centers=state.cost_center_repository,
        client_factory=lambda resource: CognitiveServicesClient(arm, resource),
    )
    yield endpoint_client, fake


def register(client: TestClient, resource: str = AI_RESOURCE_ID) -> str:
    response = client.post("/api/v1/model-endpoints", json={"azureResourceId": resource})
    assert response.status_code == 201, response.text
    return f"/api/v1/model-endpoints/{response.json()['id']}/model-lifecycle"


def test_read_only_diff_retry_context_and_catalog_loss(
    discovery: tuple[TestClient, FakeCognitiveServices],
) -> None:
    client, fake = discovery
    base = register(client)
    fake.requests.clear()
    assert client.get(f"{base}/proposals").json()["lastComplete"] is None
    assert not fake.requests

    def scan(token: str | None = None) -> dict[str, Any]:
        response = client.post(f"{base}/discoveries", json={"requestId": token or str(uuid4())})
        assert response.status_code == 200, response.text
        return response.json()

    token = str(uuid4())
    baseline = scan(token)
    assert baseline["status"] == "complete", baseline
    assert baseline["requestId"] == token
    reads = fake.requests.copy()
    assert scan(token)["id"] == baseline["id"]
    assert fake.requests == reads
    listed = client.get(f"{base}/proposals").json()
    assert listed["latestAttempt"]["requestId"] == token
    assert [(item["change"], item["deploymentStatus"]) for item in listed["items"]] == [
        ("baseline", "observed"),
    ]

    fake.models = [NEW]
    run = scan()
    listed = client.get(f"{base}/proposals").json()
    newest, old = listed["items"]
    assert (newest["change"], newest["deploymentStatus"]) == ("new", "notObserved")
    assert (old["change"], old["deploymentStatus"]) == ("unlisted", "observed")
    proposal = f"{base}/proposals/{newest['id']}"
    detail = client.get(proposal).json()
    assert detail["deployments"] == detail["publications"] == []
    assert detail["contextStatus"] == "notSaved"
    payload = {
        "expectedRevision": 0,
        "evidenceRunId": run["id"],
        "evidenceDigest": detail["evidenceDigest"],
        "environment": "development",
    }
    refused = client.put(f"{proposal}/context", json={**payload, "gatewayId": "missing"})
    assert refused.status_code == 422, refused.text
    saved = client.put(f"{proposal}/context", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["contextStatus"] == "incomplete"
    assert client.put(f"{proposal}/context", json=payload).json()["contextRevision"] == 1

    assert scan()["status"] == "complete"
    assert client.get(proposal).json()["contextStatus"] == "incomplete"
    assert client.put(f"{proposal}/context", json=payload).status_code == 409
    fake.persistent_failures["deployments"] = 404
    partial = scan()
    assert partial["status"] == "partial", partial
    result = client.get(f"{base}/proposals").json()
    assert not result["evidenceCurrent"]
    assert result["lastComplete"]["id"] != partial["id"]
    assert [(item["id"], item["change"]) for item in result["items"]] == [
        (newest["id"], "new"),
        (old["id"], "unlisted"),
    ]
    assert client.get(proposal).json()["contextStatus"] == "outOfDate"
    assert client.put(f"{proposal}/context", json=payload).status_code == 409
    assert set(fake.requests) <= {
        AI_RESOURCE_ID,
        f"{AI_RESOURCE_ID}/models",
        f"{AI_RESOURCE_ID}/deployments",
    }
    assert fake.deployments[0]["name"] == "existing-chat"
    assert not client.app.state.gateway_repository.publications


def test_foundry_uses_parent_and_never_treats_ambiguous_evidence_as_absence(
    discovery: tuple[TestClient, FakeCognitiveServices],
) -> None:
    client, fake = discovery
    base = register(client, AI_PROJECT_ID)
    fake.kind = "AIServices"
    fake.models = [OLD, {**OLD, "source": "a", "publisher": "custom"}]
    response = client.post(f"{base}/discoveries", json={"requestId": str(uuid4())})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["source"]["provider"] == "azureAiFoundry"
    assert result["source"]["accountScope"] == AI_RESOURCE_ID.casefold()
    listed = client.get(f"{base}/proposals").json()
    assert len({item["id"] for item in listed["items"]}) == 2
    assert {item["deploymentStatus"] for item in listed["items"]} == {"unconfirmed"}
    assert any("could not be matched" in line for line in result["findings"])

    fake.models = [OLD]
    fake.deployments[0]["properties"]["model"] = {**OLD, "publisher": "OpenAI"}
    assert client.post(f"{base}/discoveries", json={"requestId": str(uuid4())}).status_code == 200
    reduced = client.get(f"{base}/proposals").json()
    listed_items = [item for item in reduced["items"] if item["listed"]]
    assert len(listed_items) == 1
    assert listed_items[0]["deploymentStatus"] == "unconfirmed"
    assert len(reduced["items"]) == 2  # Do not create a third row from an incomplete join.

    fake.models = [OLD, OLD]
    on_duplicate = client.post(f"{base}/discoveries", json={"requestId": str(uuid4())}).json()
    assert on_duplicate["status"] == "partial"
    last_complete = client.get(f"{base}/proposals").json()["lastComplete"]
    assert last_complete["id"] == reduced["lastComplete"]["id"]


def test_admin_and_tenant_boundary_precede_discovery(
    discovery: tuple[TestClient, FakeCognitiveServices],
) -> None:
    client, fake = discovery
    base = register(client)
    fake.requests.clear()
    client.app.state.authenticator = LocalAuthenticator("tenant-test", roles=("User",))
    assert client.post(f"{base}/discoveries", json={"requestId": str(uuid4())}).status_code == 403
    assert client.get(f"{base}/proposals").status_code == 403
    client.app.state.authenticator = LocalAuthenticator("different-tenant")
    assert client.get(f"{base}/proposals").status_code == 404
    assert client.post(f"{base}/discoveries", json={"requestId": str(uuid4())}).status_code == 404
    assert not fake.requests


def test_uncommitted_rows_stay_hidden_and_interrupted_read_can_be_retried(
    discovery: tuple[TestClient, FakeCognitiveServices], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake = discovery
    base = register(client)
    repo = client.app.state.model_endpoint_repository
    save_lifecycle = repo.save_lifecycle

    async def fail_finalization(records: list[LifecycleRecord], audit: Any = None) -> None:
        if any(
            isinstance(record, DiscoveryRun) and record.status == "complete" for record in records
        ):
            raise ConflictError("Simulated snapshot commit failure")
        await save_lifecycle(records, audit)

    monkeypatch.setattr(repo, "save_lifecycle", fail_finalization)
    token = str(uuid4())
    assert client.post(f"{base}/discoveries", json={"requestId": token}).status_code == 409
    stored = client.get(f"{base}/proposals").json()
    assert stored["items"] == [] and stored["lastComplete"] is None
    assert stored["latestAttempt"]["status"] == "running"
    assert client.post(
        f"{base}/discoveries", json={"requestId": str(uuid4())},
    ).status_code == 409
    monkeypatch.setattr(model_lifecycle, "_TIMEOUT", timedelta(0))
    fake.requests.clear()
    assert client.post(
        f"{base}/discoveries", json={"requestId": token},
    ).json()["status"] == "interrupted"
    assert not fake.requests


def test_unsupported_registration_does_not_read_azure(
    discovery: tuple[TestClient, FakeCognitiveServices], monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake = discovery
    base = register(client)
    repo = client.app.state.model_endpoint_repository
    get_endpoint = repo.get_endpoint

    async def government(tenant_id: str, endpoint_id: str) -> Any:
        endpoint = await get_endpoint(tenant_id, endpoint_id)
        return endpoint.model_copy(update={"azure_cloud": "government"}) if endpoint else None

    monkeypatch.setattr(repo, "get_endpoint", government)
    fake.requests.clear()
    assert client.get(f"{base}/proposals").json()["supported"] is False
    assert client.post(f"{base}/discoveries", json={"requestId": str(uuid4())}).status_code == 422
    assert not fake.requests
