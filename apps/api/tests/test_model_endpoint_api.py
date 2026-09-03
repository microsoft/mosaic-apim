"""HTTP contract for model endpoint onboarding."""

from aoai_double import AI_RESOURCE_ID, PARTIAL_PERMISSIONS
from fastapi.testclient import TestClient
from mosaic_api.domain import READER_ROLE_ID


def _register(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"azureResourceId": AI_RESOURCE_ID}
    payload.update(overrides)
    response = client.post("/api/v1/model-endpoints", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestModelEndpointApi:
    def test_registers_and_lists(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client, name="Contoso models")

        assert created["status"] == "connected"
        assert created["provider"] == "azureOpenAi"
        assert created["access"]["canRead"] is True

        listed = endpoint_client.get("/api/v1/model-endpoints")
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()] == ["Contoso models"]

    def test_rejects_a_non_cognitive_services_resource_id(
        self, endpoint_client: TestClient
    ) -> None:
        response = endpoint_client.post(
            "/api/v1/model-endpoints",
            json={"azureResourceId": "/subscriptions/x/resourceGroups/y"},
        )
        assert response.status_code == 422

    def test_duplicate_registration_conflicts(self, endpoint_client: TestClient) -> None:
        _register(endpoint_client)
        response = endpoint_client.post(
            "/api/v1/model-endpoints", json={"azureResourceId": AI_RESOURCE_ID}
        )
        assert response.status_code == 409

    def test_sync_then_read_deployments(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client)
        endpoint_id = created["id"]

        sync = endpoint_client.post(f"/api/v1/model-endpoints/{endpoint_id}/sync")
        assert sync.status_code == 202

        runs = endpoint_client.get(f"/api/v1/model-endpoints/{endpoint_id}/sync-runs")
        assert runs.status_code == 200
        assert len(runs.json()) == 1

        deployments = endpoint_client.get(
            f"/api/v1/model-endpoints/{endpoint_id}/deployments"
        )
        assert deployments.status_code == 200
        names = [item["deploymentName"] for item in deployments.json()]
        assert "gpt-4o-prod" in names

    def test_available_models_endpoint(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client)
        endpoint_id = created["id"]
        endpoint_client.post(f"/api/v1/model-endpoints/{endpoint_id}/sync")

        response = endpoint_client.get(
            f"/api/v1/model-endpoints/{endpoint_id}/available-models"
        )
        assert response.status_code == 200
        assert {item["modelName"] for item in response.json()} == {"gpt-4o", "gpt-35-turbo"}

    def test_preflight_refreshes_access(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client)
        response = endpoint_client.post(
            f"/api/v1/model-endpoints/{created['id']}/preflight"
        )
        assert response.status_code == 200
        assert response.json()["access"]["canRead"] is True

    def test_delete_removes_the_endpoint(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client)
        assert (
            endpoint_client.delete(f"/api/v1/model-endpoints/{created['id']}").status_code
            == 204
        )
        assert (
            endpoint_client.get(f"/api/v1/model-endpoints/{created['id']}").status_code == 404
        )

    def test_unknown_endpoint_returns_404(self, endpoint_client: TestClient) -> None:
        assert endpoint_client.get("/api/v1/model-endpoints/missing").status_code == 404

    def test_suggestions_include_the_scanned_account(
        self, endpoint_client: TestClient
    ) -> None:
        response = endpoint_client.get("/api/v1/model-endpoints/suggested")
        assert response.status_code == 200
        body = response.json()
        assert body["subscriptionsScanned"] == 1
        assert any(
            item["source"] == "subscriptionScan" and item["accountName"] == "contoso-aoai"
            for item in body["suggestions"]
        )

    def test_runtime_access_endpoint(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client)
        response = endpoint_client.get(
            f"/api/v1/model-endpoints/{created['id']}/runtime-access"
        )
        assert response.status_code == 200
        # No gateways registered in this fixture, so there is nothing to report.
        assert response.json() == []

    def test_rename_is_persisted(self, endpoint_client: TestClient) -> None:
        created = _register(endpoint_client)
        response = endpoint_client.patch(
            f"/api/v1/model-endpoints/{created['id']}", json={"name": "Renamed"}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Renamed"

    def test_null_name_is_rejected_not_a_server_error(
        self, endpoint_client: TestClient
    ) -> None:
        created = _register(endpoint_client)
        response = endpoint_client.patch(
            f"/api/v1/model-endpoints/{created['id']}", json={"name": None}
        )
        # An explicit null would otherwise reach the entity's required field and surface as a 500.
        assert response.status_code == 422


class TestRemediationIsActionable:
    def test_unauthorized_endpoint_returns_a_runnable_command(
        self,
        settings,
        fake_aoai,
        endpoint_repository,
        gateway_repository,
    ) -> None:
        from conftest import build_endpoint_service
        from fastapi.testclient import TestClient as Client
        from mosaic_api.main import create_app

        fake_aoai.permissions = PARTIAL_PERMISSIONS
        app = create_app(settings)
        with Client(app) as client:
            app.state.model_endpoint_repository = endpoint_repository
            app.state.model_endpoint_service = build_endpoint_service(
                fake_aoai,
                repository=endpoint_repository,
                gateway_repository=gateway_repository,
            )
            created = client.post(
                "/api/v1/model-endpoints", json={"azureResourceId": AI_RESOURCE_ID}
            ).json()

        assert created["status"] == "unauthorized"
        remediation = created["access"]["remediation"]
        assert remediation["roleName"] == "Reader"
        assert remediation["roleDefinitionId"] == READER_ROLE_ID
        assert remediation["command"].startswith("az role assignment create")
        assert remediation["scope"] == AI_RESOURCE_ID
        # The narrower alternative is offered, never applied.
        assert remediation["customRoleDefinition"]["properties"]["permissions"][0][
            "dataActions"
        ] == []
