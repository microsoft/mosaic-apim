from collections.abc import Iterator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import Settings
from mosaic_api.domain import (
    ApiShape,
    Gateway,
    McpEndpoint,
    McpPublication,
    ModelEndpoint,
    ModelProvider,
    Publication,
    PublishedResource,
    PublishedResourceKind,
)
from mosaic_api.main import create_app
from mosaic_api.repositories import (
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import EnvironmentService


@pytest.fixture
def environment_client(settings: Settings) -> Iterator[TestClient]:
    gateways = InMemoryGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    mcp = InMemoryMcpEndpointRepository()
    environments = InMemoryEnvironmentRepository(gateways, endpoints, mcp)
    app: FastAPI = create_app(settings)
    with TestClient(app) as client:
        app.state.gateway_repository = gateways
        app.state.model_endpoint_repository = endpoints
        app.state.mcp_endpoint_repository = mcp
        app.state.environment_repository = environments
        app.state.environment_service = EnvironmentService(
            environments,
            gateway_repository=gateways,
            endpoint_repository=endpoints,
            mcp_repository=mcp,
        )
        client.gateways = gateways  # type: ignore[attr-defined]
        client.endpoints = endpoints  # type: ignore[attr-defined]
        client.mcp = mcp  # type: ignore[attr-defined]
        yield client


def test_environment_catalog_returns_seeds_before_first_write(
    environment_client: TestClient,
) -> None:
    response = environment_client.get("/api/v1/environment-catalog")

    assert response.status_code == 200
    body = response.json()
    assert [item["key"] for item in body["environments"]] == [
        "development",
        "test",
        "qc",
        "staging",
        "production",
        "sandbox",
    ]
    assert body["updatedAt"] is None
    assert len(body["compatibility"]) == 49


def test_create_update_delete_and_settings_happy_path(environment_client: TestClient) -> None:
    created = environment_client.post(
        "/api/v1/environment-catalog/environments",
        json={"key": "perf", "displayName": "Performance", "color": "success"},
    )
    assert created.status_code == 201, created.text
    assert any(item["key"] == "perf" for item in created.json()["environments"])

    updated = environment_client.patch(
        "/api/v1/environment-catalog/environments/perf",
        json={"displayName": "Perf", "aliases": ["load"]},
    )
    assert updated.status_code == 200, updated.text
    assert next(item for item in updated.json()["environments"] if item["key"] == "perf")[
        "aliases"
    ] == ["load"]

    settings = environment_client.patch(
        "/api/v1/environment-catalog/settings", json={"requireClassification": True}
    )
    assert settings.status_code == 200
    assert settings.json()["requireClassification"] is True

    deleted = environment_client.delete("/api/v1/environment-catalog/environments/perf")
    assert deleted.status_code == 200, deleted.text
    assert all(item["key"] != "perf" for item in deleted.json()["environments"])


def test_environment_refusals_and_portal_shape(environment_client: TestClient) -> None:
    built_in = environment_client.delete("/api/v1/environment-catalog/environments/production")
    assert built_in.status_code == 409
    assert built_in.json()["details"]["reason"] == "builtInEnvironment"

    invalid = environment_client.post(
        "/api/v1/environment-catalog/environments",
        json={"key": "unclassified", "displayName": "Nope", "color": "brand"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["details"]["reason"] == "invalidEnvironment"

    portal = environment_client.get("/api/v1/portal/environments")
    assert portal.status_code == 200
    assert set(portal.json()[0]) == {
        "key",
        "displayName",
        "description",
        "color",
        "production",
        "order",
    }


async def test_delete_refuses_environment_in_use(environment_client: TestClient) -> None:
    created = environment_client.post(
        "/api/v1/environment-catalog/environments",
        json={"key": "perf", "displayName": "Performance", "color": "success"},
    )
    assert created.status_code == 201, created.text
    await environment_client.gateways.save_gateway(  # type: ignore[attr-defined]
        Gateway(
            id="gateway-perf",
            tenant_id="tenant-test",
            name="Perf gateway",
            azure_resource_id=(
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
                "/providers/Microsoft.ApiManagement/service/apim"
            ),
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim",
            environment="perf",
        ),
        _audit("gateway"),
    )

    refused = environment_client.delete("/api/v1/environment-catalog/environments/perf")

    assert refused.status_code == 409
    details = refused.json()["details"]
    assert details["reason"] == "environmentInUse"
    assert details["usage"]["gateways"] == 1
    assert details["resources"][0]["resourceKind"] == "gateway"


async def test_catalog_edit_refuses_blocking_applied_publication(
    environment_client: TestClient,
) -> None:
    gateways = environment_client.gateways  # type: ignore[attr-defined]
    endpoints = environment_client.endpoints  # type: ignore[attr-defined]
    await gateways.save_gateway(
        Gateway(
            id="gateway-prod",
            tenant_id="tenant-test",
            name="Prod gateway",
            azure_resource_id="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim",
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim",
            environment="staging",
        ),
        _audit("gateway"),
    )
    await endpoints.save_endpoint(
        ModelEndpoint(
            id="endpoint-test",
            tenant_id="tenant-test",
            name="Test endpoint",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://aoai.example.com/",
            environment="test",
        ),
        _audit("endpoint"),
    )
    await gateways.save_publication(
        Publication(
            id="publication-1",
            tenant_id="tenant-test",
            gateway_id="gateway-prod",
            model_endpoint_id="endpoint-test",
            deployment_name="gpt-4o",
            provider=ModelProvider.AZURE_OPENAI,
            display_name="Chat",
            api_name="chat",
            api_path="chat",
            backend_name="chat",
            fragment_name="chat",
            product_name="chat",
            subscription_name="chat",
            shape_version="v1",
            api_shape=ApiShape.AZURE_OPENAI,
            resources=[
                PublishedResource(
                    kind=PublishedResourceKind.API,
                    name="chat",
                    resource_id="apis/chat",
                    created_by_mosaic=True,
                )
            ],
        ),
        _audit("publication"),
    )
    make_compatible = environment_client.patch(
        "/api/v1/environment-catalog/environments/staging",
        json={"acceptsEndpointsFrom": ["test"]},
    )
    assert make_compatible.status_code == 200, make_compatible.text

    blocked = environment_client.patch(
        "/api/v1/environment-catalog/environments/staging",
        json={"acceptsEndpointsFrom": []},
    )

    assert blocked.status_code == 409
    body = blocked.json()["details"]
    assert body["reason"] == "publicationsBlocked"
    assert body["publications"][0]["publicationId"] == "publication-1"
    assert body["suggestedAssignments"] == []


async def test_draft_publication_does_not_block_catalog_edits(
    environment_client: TestClient,
) -> None:
    gateways = environment_client.gateways  # type: ignore[attr-defined]
    endpoints = environment_client.endpoints  # type: ignore[attr-defined]
    await gateways.save_gateway(
        Gateway(
            id="gateway-draft",
            tenant_id="tenant-test",
            name="Draft gateway",
            azure_resource_id=(
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
                "/providers/Microsoft.ApiManagement/service/apim-draft"
            ),
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim-draft",
            environment="production",
        ),
        _audit("gateway"),
    )
    await endpoints.save_endpoint(
        ModelEndpoint(
            id="endpoint-draft",
            tenant_id="tenant-test",
            name="Draft endpoint",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://draft.example.com/",
            environment=None,
        ),
        _audit("endpoint"),
    )
    await gateways.save_publication(
        Publication(
            id="publication-draft",
            tenant_id="tenant-test",
            gateway_id="gateway-draft",
            model_endpoint_id="endpoint-draft",
            deployment_name="gpt-4o",
            provider=ModelProvider.AZURE_OPENAI,
            display_name="Draft",
            api_name="draft",
            api_path="draft",
            backend_name="draft",
            fragment_name="draft",
            product_name="draft",
            subscription_name="draft",
            shape_version="v1",
            api_shape=ApiShape.AZURE_OPENAI,
        ),
        _audit("publication"),
    )

    response = environment_client.patch(
        "/api/v1/environment-catalog/settings", json={"requireClassification": True}
    )

    assert response.status_code == 200, response.text


async def test_applied_mcp_publication_blocks_require_classification(
    environment_client: TestClient,
) -> None:
    gateways = environment_client.gateways  # type: ignore[attr-defined]
    mcp = environment_client.mcp  # type: ignore[attr-defined]
    await gateways.save_gateway(
        Gateway(
            id="gateway-mcp",
            tenant_id="tenant-test",
            name="MCP gateway",
            azure_resource_id=(
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
                "/providers/Microsoft.ApiManagement/service/apim-mcp"
            ),
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim-mcp",
            environment="development",
        ),
        _audit("gateway"),
    )
    await mcp.save_endpoint(
        McpEndpoint(
            id="mcp-unclassified",
            tenant_id="tenant-test",
            name="Unclassified MCP",
            endpoint="https://mcp.example.com/mcp",
        ),
        _audit("mcpEndpoint"),
    )
    await gateways.save_mcp_publication(
        McpPublication(
            id="mcp-publication",
            tenant_id="tenant-test",
            gateway_id="gateway-mcp",
            mcp_endpoint_id="mcp-unclassified",
            display_name="Docs MCP",
            api_name="docs-mcp",
            api_path="docs",
            backend_name="docs-mcp",
            fragment_name="docs-mcp",
            metadata_api_name="docs-mcp-prm",
            mcp_server_id="mcp-server-docs",
            resources=[
                PublishedResource(
                    kind=PublishedResourceKind.API,
                    name="docs-mcp",
                    resource_id="apis/docs-mcp",
                    created_by_mosaic=True,
                )
            ],
        ),
        _audit("mcpPublication"),
    )

    response = environment_client.patch(
        "/api/v1/environment-catalog/settings", json={"requireClassification": True}
    )

    assert response.status_code == 409
    details = response.json()["details"]
    assert details["reason"] == "publicationsBlocked"
    assert details["publications"][0]["kind"] == "mcp"
    assert details["publications"][0]["mcpEndpointName"] == "Unclassified MCP"


def _audit(resource: str):
    from mosaic_api.domain import AuditEvent, new_id

    return AuditEvent(
        id=new_id("audit"),
        tenant_id="tenant-test",
        action=f"{resource}.updated",
        resource_type=resource,
        resource_id=resource,
        actor_object_id="admin",
    )
