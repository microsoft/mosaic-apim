"""HTTP contract for MCP server registration."""

from fastapi.testclient import TestClient

URL = "https://mcp.example.com/mcp"


def _register(client: TestClient, **overrides: object) -> dict:
    payload: dict[str, object] = {"endpoint": URL}
    payload.update(overrides)
    response = client.post("/api/v1/mcp-endpoints", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


class TestMcpEndpointApi:
    def test_registers_and_lists(self, mcp_client: TestClient) -> None:
        created = _register(mcp_client, name="Contoso MCP")

        assert created["status"] == "connected"
        assert created["authMode"] == "none"
        assert created["access"]["canDiscover"] is True
        assert created["capabilities"]["protocolVersion"] == "2025-11-25"
        assert created["capabilities"]["transportType"] == "streamable"

        listed = mcp_client.get("/api/v1/mcp-endpoints")
        assert listed.status_code == 200
        assert [item["name"] for item in listed.json()] == ["Contoso MCP"]

    def test_duplicate_registration_conflicts(self, mcp_client: TestClient) -> None:
        _register(mcp_client)
        response = mcp_client.post("/api/v1/mcp-endpoints", json={"endpoint": URL})
        assert response.status_code == 409

    def test_a_private_address_is_rejected(self, mcp_client: TestClient) -> None:
        response = mcp_client.post(
            "/api/v1/mcp-endpoints", json={"endpoint": "https://169.254.169.254/mcp"}
        )
        assert response.status_code == 422

    def test_a_managed_identity_registration_needs_an_audience(
        self, mcp_client: TestClient
    ) -> None:
        response = mcp_client.post(
            "/api/v1/mcp-endpoints", json={"endpoint": URL, "authMode": "managedIdentity"}
        )
        assert response.status_code == 422

    def test_sync_then_read_tools(self, mcp_client: TestClient) -> None:
        endpoint_id = _register(mcp_client)["id"]

        sync = mcp_client.post(f"/api/v1/mcp-endpoints/{endpoint_id}/sync")
        assert sync.status_code == 202

        runs = mcp_client.get(f"/api/v1/mcp-endpoints/{endpoint_id}/sync-runs")
        assert runs.status_code == 200
        assert len(runs.json()) == 1

        tools = mcp_client.get(f"/api/v1/mcp-endpoints/{endpoint_id}/tools")
        assert tools.status_code == 200
        by_name = {item["name"]: item for item in tools.json()}
        assert set(by_name) == {"search_docs", "delete_record"}
        assert by_name["search_docs"]["displayName"] == "Search documents"
        assert by_name["search_docs"]["annotations"]["readOnlyHint"] is True
        # An absent hint stays absent on the wire. A client must be able to tell "the server said
        # nothing" from "the server said false".
        assert by_name["search_docs"]["annotations"]["destructiveHint"] is None
        assert by_name["delete_record"]["annotations"] is None

    def test_a_sync_run_can_be_read_back(self, mcp_client: TestClient) -> None:
        endpoint_id = _register(mcp_client)["id"]
        run_id = mcp_client.post(f"/api/v1/mcp-endpoints/{endpoint_id}/sync").json()["id"]

        run = mcp_client.get(f"/api/v1/mcp-endpoints/{endpoint_id}/sync-runs/{run_id}")
        assert run.status_code == 200
        assert run.json()["endpointId"] == endpoint_id

    def test_preflight_returns_the_refreshed_record(self, mcp_client: TestClient) -> None:
        endpoint_id = _register(mcp_client)["id"]

        response = mcp_client.post(f"/api/v1/mcp-endpoints/{endpoint_id}/preflight")
        assert response.status_code == 200
        assert response.json()["access"]["canDiscover"] is True

    def test_rename_and_remove(self, mcp_client: TestClient) -> None:
        endpoint_id = _register(mcp_client, name="Before")["id"]

        renamed = mcp_client.patch(
            f"/api/v1/mcp-endpoints/{endpoint_id}", json={"name": "After"}
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "After"

        removed = mcp_client.delete(f"/api/v1/mcp-endpoints/{endpoint_id}")
        assert removed.status_code == 204
        assert mcp_client.get(f"/api/v1/mcp-endpoints/{endpoint_id}").status_code == 404

    def test_unknown_endpoint_is_not_found(self, mcp_client: TestClient) -> None:
        assert mcp_client.get("/api/v1/mcp-endpoints/missing").status_code == 404

    def test_a_secret_uri_never_appears_in_a_response(self, mcp_client: TestClient) -> None:
        created = _register(
            mcp_client, credentialSecretUri="https://kv.vault.azure.net/secrets/mcp-token"
        )

        assert created["authMode"] == "apiKey"
        assert "kv.vault.azure.net" not in mcp_client.get("/api/v1/mcp-endpoints").text

    def test_registration_routes_are_distinct_from_gateway_adoption(
        self, mcp_client: TestClient
    ) -> None:
        _register(mcp_client)
        # ``/mcp-servers`` holds servers adopted from a gateway and is untouched by registration.
        adopted = mcp_client.get("/api/v1/mcp-servers")
        assert adopted.status_code == 200
        assert adopted.json() == []
