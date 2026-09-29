from collections.abc import AsyncIterator
from typing import cast

import pytest
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import (
    AuditEvent,
    Gateway,
    McpEndpoint,
    ModelEndpoint,
    ModelProvider,
    new_id,
)
from mosaic_api.environments import EnvironmentCatalog
from mosaic_api.errors import ConflictError
from mosaic_api.repositories import (
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.repositories.cosmos_environments import CosmosEnvironmentRepository
from test_model_access_repositories import Cosmos


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id="tenant-test",
        action="environment.updated",
        resource_type="environmentCatalog",
        resource_id="environmentCatalog",
        actor_object_id="admin",
    )


async def test_memory_environment_repository_round_trip_and_etag_conflict() -> None:
    gateways = InMemoryGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    mcp = InMemoryMcpEndpointRepository()
    repository = InMemoryEnvironmentRepository(gateways, endpoints, mcp)
    catalog = EnvironmentCatalog.new("tenant-test")

    saved = await repository.save_environment_catalog(catalog, _audit())
    found = await repository.get_environment_catalog("tenant-test")

    assert found == saved
    with pytest.raises(ConflictError):
        await repository.save_environment_catalog(catalog, _audit())


async def test_memory_environment_repository_counts_grouped_and_unclassified() -> None:
    gateways = InMemoryGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    mcp = InMemoryMcpEndpointRepository()
    repository = InMemoryEnvironmentRepository(gateways, endpoints, mcp)
    await gateways.save_gateway(
        Gateway(
            id="gateway-1",
            tenant_id="tenant-test",
            name="Gateway 1",
            azure_resource_id="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim",
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim",
            environment="production",
        ),
        _audit(),
    )
    await endpoints.save_endpoint(
        ModelEndpoint(
            id="endpoint-1",
            tenant_id="tenant-test",
            name="Endpoint 1",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://aoai.example.com/",
            environment="production",
        ),
        _audit(),
    )
    await mcp.save_endpoint(
        McpEndpoint(
            id="mcp-1",
            tenant_id="tenant-test",
            name="MCP 1",
            endpoint="https://mcp.example.com/",
            environment=None,
        ),
        _audit(),
    )

    counts = await repository.count_resources_by_environment("tenant-test")

    assert counts["production"].gateways == 1
    assert counts["production"].model_endpoints == 1
    assert counts[None].mcp_endpoints == 1


class _GroupedCountContainer:
    """Returns Cosmos GROUP BY rows: a null group and a separate group for missing properties."""

    def __init__(self, rows: dict[str, list[dict[str, object]]]) -> None:
        self._rows = rows

    def query_items(self, *, parameters: list[dict[str, str]], **_kwargs: object) -> object:
        entity_type = next(p["value"] for p in parameters if p["name"] == "@entityType")
        rows = self._rows.get(entity_type, [])

        async def iterate() -> AsyncIterator[dict[str, object]]:
            for row in rows:
                yield row

        return iterate()


class _GroupedCountCosmos:
    def __init__(self, container: _GroupedCountContainer) -> None:
        self._container = container

    def get_database_client(self, _name: str) -> "_GroupedCountCosmos":
        return self

    def get_container_client(self, _name: str) -> _GroupedCountContainer:
        return self._container


async def test_cosmos_counts_add_null_and_missing_environment_groups() -> None:
    container = _GroupedCountContainer(
        {
            "gateway": [
                {"environment": "production", "n": 2},
                {"environment": None, "n": 1},
                # A document saved before environments existed has no property at all.
                {"n": 3},
            ],
            "mcpEndpoint": [{"n": 4}],
        }
    )
    repository = CosmosEnvironmentRepository(
        cast(CosmosClient, _GroupedCountCosmos(container)), "db", "desired", "audit"
    )

    counts = await repository.count_resources_by_environment("tenant-test")

    assert counts["production"].gateways == 2
    assert counts[None].gateways == 4
    assert counts[None].mcp_endpoints == 4
    assert counts[None].model_endpoints == 0


async def test_cosmos_environment_assignment_batch_is_atomic() -> None:
    cosmos = Cosmos()
    repository = CosmosEnvironmentRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit"
    )
    gateway = Gateway(
        id="gateway-1",
        tenant_id="tenant-test",
        name="Gateway 1",
        azure_resource_id="/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim",
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_group="rg",
        service_name="apim",
    )
    endpoint = ModelEndpoint(
        id="endpoint-1",
        tenant_id="tenant-test",
        name="Endpoint 1",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://aoai.example.com/",
    )
    desired = cosmos.containers["desired"]
    await desired.create_item(gateway.model_dump(mode="json", by_alias=True, exclude={"etag"}))
    await desired.create_item(endpoint.model_dump(mode="json", by_alias=True, exclude={"etag"}))
    stored_gateway = await repository._read(Gateway, "tenant-test", gateway.id)
    stored_endpoint = await repository._read(ModelEndpoint, "tenant-test", endpoint.id)
    assert stored_gateway is not None
    assert stored_endpoint is not None
    desired.items[("tenant-test", endpoint.id)]["_etag"] = "stale-now"

    with pytest.raises(ConflictError):
        await repository.save_environment_assignments(
            [
                stored_gateway.model_copy(update={"environment": "production"}),
                stored_endpoint.model_copy(update={"environment": "production"}),
            ],
            _audit(),
        )

    assert desired.items[("tenant-test", gateway.id)].get("environment") is None
    assert desired.items[("tenant-test", endpoint.id)].get("environment") is None
