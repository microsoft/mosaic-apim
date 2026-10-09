from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    Gateway,
    McpEndpoint,
    McpPublication,
    ModelEndpoint,
    ModelProvider,
    Publication,
    PublishedResource,
    PublishedResourceKind,
    deterministic_id,
    new_id,
)
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedApi, ObservedBackend, ObservedMcpServer
from mosaic_api.repositories import (
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import EnvironmentFindingsService, EnvironmentService
from mosaic_api.services.directory import Actor
from mosaic_api.services.environment_findings import normalize_ai_account_host

DEV_URL = "https://dev.openai.azure.com/"


class FailingObservedGatewayRepository(InMemoryGatewayRepository):
    async def list_observed(
        self, model_type: type[Any], tenant_id: str, gateway_id: str, entity_type: str
    ) -> list[Any]:
        if gateway_id == "gateway-broken":
            raise RuntimeError("inventory read failed")
        return await super().list_observed(model_type, tenant_id, gateway_id, entity_type)


@pytest.fixture
def findings_client(settings: Settings) -> Iterator[TestClient]:
    gateways = InMemoryGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    mcp = InMemoryMcpEndpointRepository()
    environments = InMemoryEnvironmentRepository(gateways, endpoints, mcp)
    environment_service = EnvironmentService(
        environments,
        gateway_repository=gateways,
        endpoint_repository=endpoints,
        mcp_repository=mcp,
    )
    app: FastAPI = create_app(settings)
    with TestClient(app) as client:
        app.state.gateway_repository = gateways
        app.state.model_endpoint_repository = endpoints
        app.state.mcp_endpoint_repository = mcp
        app.state.environment_repository = environments
        app.state.environment_service = environment_service
        app.state.environment_findings_service = EnvironmentFindingsService(
            environment_service=environment_service,
            gateway_repository=gateways,
            endpoint_repository=endpoints,
            mcp_repository=mcp,
        )
        client.gateways = gateways  # type: ignore[attr-defined]
        client.endpoints = endpoints  # type: ignore[attr-defined]
        client.mcp = mcp  # type: ignore[attr-defined]
        yield client


def test_normalize_ai_account_host() -> None:
    assert normalize_ai_account_host("Contoso-Dev.openai.azure.com") == "contoso-dev"
    assert normalize_ai_account_host("contoso-dev.cognitiveservices.azure.com:443") == "contoso-dev"
    assert (
        normalize_ai_account_host("https://contoso-dev.services.ai.azure.com/path")
        == "contoso-dev"
    )
    assert normalize_ai_account_host("api.openai.com") is None
    assert normalize_ai_account_host("openai.azure.com") is None


async def test_blocked_publication_for_applied_but_not_draft(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-prod", "Prod gateway", "production")
    await _model_endpoint(endpoints, "endpoint-dev", "Dev endpoint", DEV_URL, "development")
    await _publication(
        gateways,
        "publication-applied",
        "gateway-prod",
        "endpoint-dev",
        "Chat",
        "chat",
        applied=True,
    )
    await _publication(
        gateways,
        "publication-draft",
        "gateway-prod",
        "endpoint-dev",
        "Draft",
        "draft",
        applied=False,
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["kind"] for item in items] == ["blockedPublication"]
    assert items[0]["confidence"] == "certain"
    assert items[0]["id"] == deterministic_id(
        "environmentFinding",
        "tenant-test",
        "gateway-prod",
        "blockedPublication",
        "publication-applied",
        "endpoint-dev",
    )


async def test_backend_exact_and_cross_domain_matches(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-prod", "Prod gateway", "production")
    await _model_endpoint(
        endpoints,
        "endpoint-exact",
        "Exact endpoint",
        "https://exact-dev.openai.azure.com/",
        "development",
    )
    await _model_endpoint(
        endpoints,
        "endpoint-account",
        "Account endpoint",
        "https://account-dev.cognitiveservices.azure.com/",
        "development",
    )
    await gateways.replace_observed(
        "tenant-test",
        "gateway-prod",
        [
            ObservedBackend(
                id="backend-exact",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="exact",
                url="https://exact-dev.openai.azure.com/",
            ),
            ObservedBackend(
                id="backend-account",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="account",
                url="https://account-dev.openai.azure.com/",
            ),
        ],
        "snapshot",
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(item["subject"]["id"], item["confidence"]) for item in items] == [
        ("backend-account", "medium"),
        ("backend-exact", "high"),
    ]


async def test_allowed_and_warning_pairs_do_not_report(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-dev", "Dev gateway", "development")
    await _model_endpoint(
        endpoints,
        "endpoint-dev",
        "Dev endpoint",
        "https://dev.openai.azure.com/",
        "development",
    )
    await _model_endpoint(
        endpoints,
        "endpoint-unclassified",
        "Unclassified endpoint",
        "https://unclassified.openai.azure.com/",
        None,
    )
    await gateways.replace_observed(
        "tenant-test",
        "gateway-dev",
        [
            ObservedBackend(
                id="backend-allowed",
                tenant_id="tenant-test",
                gateway_id="gateway-dev",
                snapshot_id="snapshot",
                name="allowed",
                url="https://dev.openai.azure.com/",
            ),
            ObservedBackend(
                id="backend-warning",
                tenant_id="tenant-test",
                gateway_id="gateway-dev",
                snapshot_id="snapshot",
                name="warning",
                url="https://unclassified.openai.azure.com/",
            ),
        ],
        "snapshot",
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


async def test_own_published_api_is_not_duplicated(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-prod", "Prod gateway", "production")
    await _model_endpoint(endpoints, "endpoint-dev", "Dev endpoint", DEV_URL, "development")
    await _publication(
        gateways,
        "publication-applied",
        "gateway-prod",
        "endpoint-dev",
        "Chat",
        "chat",
        applied=True,
    )
    await gateways.replace_observed(
        "tenant-test",
        "gateway-prod",
        [
            ObservedApi(
                id="api-chat",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="chat",
                display_name="Chat",
                path="chat",
                service_url="https://dev.openai.azure.com/",
            )
        ],
        "snapshot",
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    assert [item["kind"] for item in response.json()["items"]] == ["blockedPublication"]


async def test_mcp_canonical_url_matching(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    mcp = findings_client.mcp  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-prod", "Prod gateway", "production")
    await _mcp_endpoint(mcp, "mcp-dev", "Dev MCP", "https://MCP.example.com/mcp/", "development")
    await gateways.replace_observed(
        "tenant-test",
        "gateway-prod",
        [
            ObservedMcpServer(
                id="mcp-server",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="mcp-api",
                display_name="MCP API",
                path="mcp",
                service_url="https://mcp.example.com/mcp",
            )
        ],
        "snapshot",
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    assert item["kind"] == "mcpServerCrossesEnvironments"
    assert item["confidence"] == "high"
    assert item["subject"]["apiName"] == "mcp-api"
    assert item["target"]["resourceKind"] == "mcpEndpoint"


async def test_owned_mcp_publication_reports_blocked_once(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    mcp = findings_client.mcp  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-prod", "Prod gateway", "production")
    await _model_endpoint(endpoints, "endpoint-dev", "Dev model endpoint", DEV_URL, "development")
    await _mcp_endpoint(mcp, "mcp-dev", "Dev MCP", "https://mcp.example.com/mcp", "development")
    await _mcp_publication(
        gateways,
        "mcp-publication-applied",
        "gateway-prod",
        "mcp-dev",
        "Docs MCP",
        "docs-mcp",
        applied=True,
    )
    await gateways.replace_observed(
        "tenant-test",
        "gateway-prod",
        [
            ObservedApi(
                id="api-docs-mcp",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="docs-mcp",
                display_name="Docs MCP",
                path="docs",
                service_url="https://mcp.example.com/mcp",
            ),
            ObservedApi(
                id="api-docs-mcp-prm",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="docs-mcp-prm",
                display_name="Docs MCP metadata",
                path="docs-prm",
                service_url=DEV_URL,
            ),
            ObservedMcpServer(
                id="mcp-server",
                tenant_id="tenant-test",
                gateway_id="gateway-prod",
                snapshot_id="snapshot",
                name="docs-mcp",
                display_name="Docs MCP",
                path="docs",
                service_url="https://mcp.example.com/mcp",
            ),
        ],
        "snapshot",
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["kind"] for item in items] == [
        "apiCrossesEnvironments",
        "blockedPublication",
    ]
    api_finding = next(item for item in items if item["kind"] == "apiCrossesEnvironments")
    assert api_finding["subject"]["apiName"] == "docs-mcp-prm"
    assert api_finding["target"]["resourceKind"] == "modelEndpoint"
    blocked = next(item for item in items if item["kind"] == "blockedPublication")
    assert blocked["target"]["resourceKind"] == "mcpEndpoint"
    assert blocked["target"]["resourceName"] == "Dev MCP"
    assert blocked["subject"]["name"] == "Docs MCP"


async def test_gateway_filter_and_unknown_gateway(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-a", "A gateway", "production")
    await _gateway(gateways, "gateway-b", "B gateway", "production")
    await _model_endpoint(endpoints, "endpoint-dev", "Dev endpoint", DEV_URL, "development")
    await _publication(
        gateways, "publication-a", "gateway-a", "endpoint-dev", "A", "a", applied=True
    )
    await _publication(
        gateways, "publication-b", "gateway-b", "endpoint-dev", "B", "b", applied=True
    )

    filtered = findings_client.get("/api/v1/environment-findings?gatewayId=gateway-b")
    missing = findings_client.get("/api/v1/environment-findings?gatewayId=unknown")

    assert filtered.status_code == 200, filtered.text
    assert [item["gatewayId"] for item in filtered.json()["items"]] == ["gateway-b"]
    assert missing.status_code == 404


async def test_ordering_and_limitations_present(findings_client: TestClient) -> None:
    gateways = findings_client.gateways  # type: ignore[attr-defined]
    endpoints = findings_client.endpoints  # type: ignore[attr-defined]
    await _gateway(gateways, "gateway-z", "Z gateway", "production")
    await _gateway(gateways, "gateway-a", "A gateway", "production")
    await _model_endpoint(endpoints, "endpoint-dev", "Dev endpoint", DEV_URL, "development")
    await _publication(
        gateways, "publication-z", "gateway-z", "endpoint-dev", "Zed", "zed", applied=True
    )
    await _publication(
        gateways,
        "publication-a",
        "gateway-a",
        "endpoint-dev",
        "Alpha",
        "alpha",
        applied=True,
    )

    response = findings_client.get("/api/v1/environment-findings")

    assert response.status_code == 200, response.text
    body = response.json()
    assert [(item["gatewayName"], item["subject"]["name"]) for item in body["items"]] == [
        ("A gateway", "Alpha"),
        ("Z gateway", "Zed"),
    ]
    assert body["limitations"] == [
        "Backends referenced only from policy, such as a set-backend-service base URL or a "
        "named value, aren't inspected."
    ]
    assert body["generatedAt"]


async def test_inventory_failure_skips_gateway_with_limitation(settings: Settings) -> None:
    gateways = FailingObservedGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    mcp = InMemoryMcpEndpointRepository()
    environments = InMemoryEnvironmentRepository(gateways, endpoints, mcp)
    environment_service = EnvironmentService(
        environments,
        gateway_repository=gateways,
        endpoint_repository=endpoints,
        mcp_repository=mcp,
    )
    service = EnvironmentFindingsService(
        environment_service=environment_service,
        gateway_repository=gateways,
        endpoint_repository=endpoints,
        mcp_repository=mcp,
    )
    await _gateway(gateways, "gateway-broken", "Broken gateway", "production")
    await _gateway(gateways, "gateway-good", "Good gateway", "production")
    await _model_endpoint(endpoints, "endpoint-dev", "Dev endpoint", DEV_URL, "development")
    await gateways.replace_observed(
        "tenant-test",
        "gateway-good",
        [
            ObservedBackend(
                id="backend-good",
                tenant_id="tenant-test",
                gateway_id="gateway-good",
                snapshot_id="snapshot",
                name="good",
                url="https://dev.openai.azure.com/",
            )
        ],
        "snapshot",
    )

    result = await service.list_findings(Actor(object_id="admin", tenant_id="tenant-test"))

    assert [item.gateway_id for item in result.items] == ["gateway-good"]
    assert any("Broken gateway" in limitation for limitation in result.limitations)


def test_environment_findings_is_admin_only(settings: Settings) -> None:
    portal_settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=settings.tenant_id,
        local_roles=["User"],
    )
    with TestClient(create_app(portal_settings)) as client:
        response = client.get("/api/v1/environment-findings")

    assert response.status_code == 403


async def _gateway(
    repository: InMemoryGatewayRepository, gateway_id: str, name: str, environment: str | None
) -> None:
    await repository.save_gateway(
        Gateway(
            id=gateway_id,
            tenant_id="tenant-test",
            name=name,
            azure_resource_id=(
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
                f"/providers/Microsoft.ApiManagement/service/{gateway_id}"
            ),
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name=gateway_id,
            environment=environment,
        ),
        _audit("gateway", gateway_id),
    )


async def _model_endpoint(
    repository: InMemoryModelEndpointRepository,
    endpoint_id: str,
    name: str,
    url: str,
    environment: str | None,
) -> None:
    await repository.save_endpoint(
        ModelEndpoint(
            id=endpoint_id,
            tenant_id="tenant-test",
            name=name,
            provider=ModelProvider.AZURE_OPENAI,
            endpoint=url,
            environment=environment,
        ),
        _audit("modelEndpoint", endpoint_id),
    )


async def _mcp_endpoint(
    repository: InMemoryMcpEndpointRepository,
    endpoint_id: str,
    name: str,
    url: str,
    environment: str | None,
) -> None:
    await repository.save_endpoint(
        McpEndpoint(
            id=endpoint_id,
            tenant_id="tenant-test",
            name=name,
            endpoint=url,
            environment=environment,
        ),
        _audit("mcpEndpoint", endpoint_id),
    )


async def _publication(
    repository: InMemoryGatewayRepository,
    publication_id: str,
    gateway_id: str,
    endpoint_id: str,
    display_name: str,
    api_name: str,
    *,
    applied: bool,
) -> None:
    await repository.save_publication(
        Publication(
            id=publication_id,
            tenant_id="tenant-test",
            gateway_id=gateway_id,
            model_endpoint_id=endpoint_id,
            deployment_name="gpt-4o",
            provider=ModelProvider.AZURE_OPENAI,
            display_name=display_name,
            api_name=api_name,
            api_path=api_name,
            backend_name=api_name,
            fragment_name=api_name,
            product_name=api_name,
            subscription_name=api_name,
            shape_version="v1",
            api_shape=ApiShape.AZURE_OPENAI,
            resources=(
                [
                    PublishedResource(
                        kind=PublishedResourceKind.API,
                        name=api_name,
                        resource_id=f"apis/{api_name}",
                        created_by_mosaic=True,
                    )
                ]
                if applied
                else []
            ),
        ),
        _audit("publication", publication_id),
    )


async def _mcp_publication(
    repository: InMemoryGatewayRepository,
    publication_id: str,
    gateway_id: str,
    endpoint_id: str,
    display_name: str,
    api_name: str,
    *,
    applied: bool,
) -> None:
    await repository.save_mcp_publication(
        McpPublication(
            id=publication_id,
            tenant_id="tenant-test",
            gateway_id=gateway_id,
            mcp_endpoint_id=endpoint_id,
            display_name=display_name,
            api_name=api_name,
            api_path=api_name,
            backend_name=api_name,
            fragment_name=api_name,
            metadata_api_name=f"{api_name}-prm",
            mcp_server_id=f"mcp-server-{api_name}",
            resources=(
                [
                    PublishedResource(
                        kind=PublishedResourceKind.API,
                        name=api_name,
                        resource_id=f"apis/{api_name}",
                        created_by_mosaic=True,
                    )
                ]
                if applied
                else []
            ),
        ),
        _audit("mcpPublication", publication_id),
    )


def _audit(resource_type: str, resource_id: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id="tenant-test",
        action=f"{resource_type}.updated",
        resource_type=resource_type,
        resource_id=resource_id,
        actor_object_id="admin",
    )
