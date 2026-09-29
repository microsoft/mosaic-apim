import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import (
    AuditEvent,
    Gateway,
    GatewayStatus,
    McpEndpoint,
    McpEndpointStatus,
    ModelEndpoint,
    ModelEndpointStatus,
    ModelProvider,
    new_id,
)
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.repositories.cosmos_endpoints import CosmosModelEndpointRepository
from mosaic_api.repositories.cosmos_gateway import CosmosGatewayRepository
from mosaic_api.repositories.cosmos_mcp_endpoints import CosmosMcpEndpointRepository

TENANT_ID = "tenant-observation"


class Container:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.generation = 0
        self.before_replace: Callable[[], Awaitable[None]] | None = None

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["tenantId"], body["id"])
        if key in self.items:
            raise exceptions.CosmosResourceExistsError(status_code=409)
        return self._store(key, body)

    async def execute_item_batch(
        self, *, batch_operations: list[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]:
        staged = deepcopy(self.items)
        generation = self.generation
        for index, operation in enumerate(batch_operations):
            kind, arguments = operation[0], operation[1]
            options: dict[str, Any] = operation[2] if len(operation) > 2 else {}
            item_id = arguments[0]
            body = None
            if kind == "create":
                body = arguments[0]
                item_id = body["id"]
            if kind == "replace":
                item_id, body = arguments
            key = (partition_key, item_id)
            current = staged.get(key)
            status = 200
            if kind == "create" and current is not None:
                status = 409
            elif kind in {"replace", "delete"} and current is None:
                status = 404
            elif kind in {"replace", "delete"} and options.get("if_match_etag"):
                if options["if_match_etag"] != current["_etag"]:
                    status = 412
            if body is not None and body["tenantId"] != partition_key:
                status = 400
            if status >= 400:
                responses = [{"statusCode": 424} for _ in batch_operations]
                responses[index] = {"statusCode": status}
                raise exceptions.CosmosBatchOperationError(
                    error_index=index,
                    headers={},
                    status_code=status,
                    message="The batch failed",
                    operation_responses=responses,
                )
            if kind == "delete":
                staged.pop(key, None)
                generation += 1
                continue
            assert body is not None
            generation += 1
            staged[key] = {**deepcopy(body), "_etag": f"etag-{generation}"}
        self.items, self.generation = staged, generation
        return [{"statusCode": 200} for _ in batch_operations]

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        key = (partition_key, item)
        if key not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        return deepcopy(self.items[key])

    async def replace_item(
        self, *, item: str, body: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        key = (body["tenantId"], item)
        hook = self.before_replace
        if hook is not None:
            self.before_replace = None
            await hook()
        current = self.items.get(key)
        if current is None:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        if kwargs.get("etag") != current["_etag"]:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412)
        return self._store(key, body)

    async def delete_item(self, *, item: str, partition_key: str, **_kwargs: Any) -> None:
        key = (partition_key, item)
        if key not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        del self.items[key]

    def query_items(self, **_kwargs: Any) -> Any:
        async def empty() -> Any:
            if False:
                yield None

        return empty()

    def _store(self, key: tuple[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.generation += 1
        document = {**deepcopy(body), "_etag": f"etag-{self.generation}"}
        self.items[key] = document
        return deepcopy(document)


class Cosmos:
    def __init__(self) -> None:
        self.containers = {name: Container() for name in ("desired", "audit", "sync", "observed")}

    def get_database_client(self, _name: str) -> "Cosmos":
        return self

    def get_container_client(self, name: str) -> Container:
        return self.containers[name]


class HookedMemoryGatewayRepository(InMemoryGatewayRepository):
    before_replace: Callable[[], Awaitable[None]] | None = None

    async def _before_observation_replace(self) -> None:
        if self.before_replace is not None:
            hook = self.before_replace
            self.before_replace = None
            await hook()


class HookedMemoryModelEndpointRepository(InMemoryModelEndpointRepository):
    before_replace: Callable[[], Awaitable[None]] | None = None

    async def _before_observation_replace(self) -> None:
        if self.before_replace is not None:
            hook = self.before_replace
            self.before_replace = None
            await hook()


class HookedMemoryMcpEndpointRepository(InMemoryMcpEndpointRepository):
    before_replace: Callable[[], Awaitable[None]] | None = None

    async def _before_observation_replace(self) -> None:
        if self.before_replace is not None:
            hook = self.before_replace
            self.before_replace = None
            await hook()


def audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT_ID,
        action="test",
        resource_type="test",
        resource_id="test",
        actor_object_id="actor",
    )


def gateway() -> Gateway:
    return Gateway(
        id="gateway-observation",
        tenant_id=TENANT_ID,
        name="Gateway",
        azure_resource_id=(
            "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/"
            "providers/Microsoft.ApiManagement/service/apim"
        ),
        subscription_id="11111111-1111-1111-1111-111111111111",
        resource_group="rg",
        service_name="apim",
        environment_label="legacy-dev",
        environment="dev",
    )


def model_endpoint() -> ModelEndpoint:
    return ModelEndpoint(
        id="endpoint-observation",
        tenant_id=TENANT_ID,
        name="Model endpoint",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://aoai.openai.azure.com",
        azure_resource_id=(
            "/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/rg/"
            "providers/Microsoft.CognitiveServices/accounts/aoai"
        ),
        subscription_id="11111111-1111-1111-1111-111111111111",
        resource_group="rg",
        account_name="aoai",
        environment_label="legacy-dev",
        environment="dev",
        credential_reference_id="credential-old",
    )


def mcp_endpoint() -> McpEndpoint:
    return McpEndpoint(
        id="mcp-observation",
        tenant_id=TENANT_ID,
        name="MCP server",
        endpoint="https://mcp.example.com/mcp",
        environment_label="legacy-dev",
        environment="dev",
        credential_reference_id="credential-old",
        resource_audience="api://old",
    )


async def test_memory_gateway_observation_preserves_authored_fields_and_updates_observed() -> None:
    repo = InMemoryGatewayRepository()
    original = await repo.create_gateway(gateway(), audit())
    stale = await repo.get_gateway(TENANT_ID, original.id)
    assert stale is not None
    await repo.save_gateway(
        stale.model_copy(
            update={
                "name": "Admin name",
                "environment_label": "prod",
                "environment": "production",
            }
        ),
        audit(),
    )
    recorded = await repo.record_gateway_state(
        stale.model_copy(
            update={"status": GatewayStatus.CONNECTED, "azure_environment_tag": "azure-prod"}
        )
    )
    assert recorded is not None
    stored = await repo.get_gateway(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Admin name"
    assert stored.environment_label == "prod"
    assert stored.environment == "production"
    assert stored.status == GatewayStatus.CONNECTED
    assert stored.azure_environment_tag == "azure-prod"


async def test_memory_model_endpoint_observation_preserves_authored_fields_and_updates_observed(
) -> None:
    repo = InMemoryModelEndpointRepository()
    original = await repo.create_endpoint(model_endpoint(), audit())
    stale = await repo.get_endpoint(TENANT_ID, original.id)
    assert stale is not None
    await repo.save_endpoint(
        stale.model_copy(
            update={
                "name": "Admin endpoint",
                "environment_label": "prod",
                "environment": "production",
                "credential_reference_id": "credential-new",
            }
        ),
        audit(),
    )
    recorded = await repo.record_endpoint_state(
        stale.model_copy(
            update={
                "status": ModelEndpointStatus.CONNECTED,
                "azure_environment_tag": "azure-prod",
            }
        )
    )
    assert recorded is not None
    stored = await repo.get_endpoint(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Admin endpoint"
    assert stored.environment_label == "prod"
    assert stored.environment == "production"
    assert stored.credential_reference_id == "credential-new"
    assert stored.status == ModelEndpointStatus.CONNECTED
    assert stored.azure_environment_tag == "azure-prod"


async def test_memory_mcp_endpoint_observation_preserves_authored_fields_and_updates_observed(
) -> None:
    repo = InMemoryMcpEndpointRepository()
    original = await repo.create_endpoint(mcp_endpoint(), audit())
    stale = await repo.get_endpoint(TENANT_ID, original.id)
    assert stale is not None
    await repo.save_endpoint(
        stale.model_copy(
            update={
                "name": "Admin MCP",
                "environment_label": "prod",
                "environment": "production",
                "credential_reference_id": "credential-new",
                "resource_audience": "api://new",
            }
        ),
        audit(),
    )
    recorded = await repo.record_endpoint_state(
        stale.model_copy(update={"status": McpEndpointStatus.CONNECTED})
    )
    assert recorded is not None
    stored = await repo.get_endpoint(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Admin MCP"
    assert stored.environment_label == "prod"
    assert stored.environment == "production"
    assert stored.credential_reference_id == "credential-new"
    assert stored.resource_audience == "api://new"
    assert stored.status == McpEndpointStatus.CONNECTED


async def test_cosmos_gateway_observation_preserves_authored_fields_and_updates_observed() -> None:
    cosmos = Cosmos()
    repo = CosmosGatewayRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
    )
    original = await repo.create_gateway(gateway(), audit())
    stale = await repo.get_gateway(TENANT_ID, original.id)
    assert stale is not None
    await repo.save_gateway(
        stale.model_copy(
            update={
                "name": "Admin name",
                "environment_label": "prod",
                "environment": "production",
            }
        ),
        audit(),
    )
    recorded = await repo.record_gateway_state(
        stale.model_copy(
            update={"status": GatewayStatus.CONNECTED, "azure_environment_tag": "azure-prod"}
        )
    )
    assert recorded is not None
    stored = await repo.get_gateway(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Admin name"
    assert stored.environment_label == "prod"
    assert stored.environment == "production"
    assert stored.status == GatewayStatus.CONNECTED
    assert stored.azure_environment_tag == "azure-prod"


async def test_cosmos_model_endpoint_observation_preserves_authored_fields_and_updates_observed(
) -> None:
    cosmos = Cosmos()
    repo = CosmosModelEndpointRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
    )
    original = await repo.create_endpoint(model_endpoint(), audit())
    stale = await repo.get_endpoint(TENANT_ID, original.id)
    assert stale is not None
    await repo.save_endpoint(
        stale.model_copy(
            update={
                "name": "Admin endpoint",
                "environment_label": "prod",
                "environment": "production",
                "credential_reference_id": "credential-new",
            }
        ),
        audit(),
    )
    recorded = await repo.record_endpoint_state(
        stale.model_copy(
            update={
                "status": ModelEndpointStatus.CONNECTED,
                "azure_environment_tag": "azure-prod",
            }
        )
    )
    assert recorded is not None
    stored = await repo.get_endpoint(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Admin endpoint"
    assert stored.environment_label == "prod"
    assert stored.environment == "production"
    assert stored.credential_reference_id == "credential-new"
    assert stored.status == ModelEndpointStatus.CONNECTED
    assert stored.azure_environment_tag == "azure-prod"


async def test_cosmos_mcp_endpoint_observation_preserves_authored_fields_and_updates_observed(
) -> None:
    cosmos = Cosmos()
    repo = CosmosMcpEndpointRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
    )
    original = await repo.create_endpoint(mcp_endpoint(), audit())
    stale = await repo.get_endpoint(TENANT_ID, original.id)
    assert stale is not None
    await repo.save_endpoint(
        stale.model_copy(
            update={
                "name": "Admin MCP",
                "environment_label": "prod",
                "environment": "production",
                "credential_reference_id": "credential-new",
                "resource_audience": "api://new",
            }
        ),
        audit(),
    )
    recorded = await repo.record_endpoint_state(
        stale.model_copy(update={"status": McpEndpointStatus.CONNECTED})
    )
    assert recorded is not None
    stored = await repo.get_endpoint(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Admin MCP"
    assert stored.environment_label == "prod"
    assert stored.environment == "production"
    assert stored.credential_reference_id == "credential-new"
    assert stored.resource_audience == "api://new"
    assert stored.status == McpEndpointStatus.CONNECTED


async def test_memory_observation_retries_when_admin_update_interleaves() -> None:
    repo = HookedMemoryGatewayRepository()
    original = await repo.create_gateway(gateway(), audit())
    stale = await repo.get_gateway(TENANT_ID, original.id)
    assert stale is not None
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def hook() -> None:
        entered.set()
        await resume.wait()

    repo.before_replace = hook
    task = asyncio.create_task(
        repo.record_gateway_state(stale.model_copy(update={"status": GatewayStatus.CONNECTED}))
    )
    await entered.wait()
    current = await repo.get_gateway(TENANT_ID, original.id)
    assert current is not None
    await repo.save_gateway(current.model_copy(update={"name": "Concurrent admin"}), audit())
    resume.set()
    await task
    stored = await repo.get_gateway(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Concurrent admin"
    assert stored.status == GatewayStatus.CONNECTED


async def test_cosmos_observation_retries_when_admin_update_interleaves() -> None:
    cosmos = Cosmos()
    repo = CosmosModelEndpointRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
    )
    original = await repo.create_endpoint(model_endpoint(), audit())
    stale = await repo.get_endpoint(TENANT_ID, original.id)
    assert stale is not None
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def hook() -> None:
        entered.set()
        await resume.wait()

    cosmos.containers["desired"].before_replace = hook
    task = asyncio.create_task(
        repo.record_endpoint_state(
            stale.model_copy(update={"status": ModelEndpointStatus.CONNECTED})
        )
    )
    await entered.wait()
    current = await repo.get_endpoint(TENANT_ID, original.id)
    assert current is not None
    await repo.save_endpoint(current.model_copy(update={"name": "Concurrent admin"}), audit())
    resume.set()
    await task
    stored = await repo.get_endpoint(TENANT_ID, original.id)
    assert stored is not None
    assert stored.name == "Concurrent admin"
    assert stored.status == ModelEndpointStatus.CONNECTED


@pytest.mark.parametrize(
    ("repo", "entity"),
    [
        (InMemoryGatewayRepository(), gateway()),
        (InMemoryModelEndpointRepository(), model_endpoint()),
        (InMemoryMcpEndpointRepository(), mcp_endpoint()),
    ],
)
async def test_memory_observation_after_delete_does_not_resurrect(repo: Any, entity: Any) -> None:
    if isinstance(entity, Gateway):
        created = await repo.create_gateway(entity, audit())
        stale = await repo.get_gateway(TENANT_ID, created.id)
        assert stale is not None
        await repo.delete_gateway(created, audit())
        assert await repo.record_gateway_state(stale) is None
        assert await repo.get_gateway(TENANT_ID, created.id) is None
    else:
        created = await repo.create_endpoint(entity, audit())
        stale = await repo.get_endpoint(TENANT_ID, created.id)
        assert stale is not None
        await repo.delete_endpoint(created, audit())
        assert await repo.record_endpoint_state(stale) is None
        assert await repo.get_endpoint(TENANT_ID, created.id) is None


async def test_cosmos_observation_after_delete_does_not_resurrect() -> None:
    cosmos = Cosmos()
    repo = CosmosGatewayRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
    )
    created = await repo.create_gateway(gateway(), audit())
    stale = await repo.get_gateway(TENANT_ID, created.id)
    assert stale is not None
    await repo.delete_gateway(created, audit())
    assert await repo.record_gateway_state(stale) is None
    assert await repo.get_gateway(TENANT_ID, created.id) is None
