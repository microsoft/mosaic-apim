import asyncio
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import (
    AuditEvent,
    EndpointAccess,
    EndpointDetailEvidence,
    Gateway,
    GatewayRuntimeAccess,
    GatewayStatus,
    McpEndpoint,
    McpEndpointStatus,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelEndpointStatus,
    ModelProvider,
    VertexConfiguration,
    new_id,
)
from mosaic_api.endpoint_readiness import ENDPOINT_READINESS_RULES_REVISION
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


@pytest.mark.parametrize("cosmos_backed", [False, True])
@pytest.mark.parametrize(
    "runtime_transition", ["legacy-clock-skew", "empty-to-nonempty", "nonempty-to-empty"]
)
async def test_stale_inventory_retry_preserves_newer_endpoint_evidence(
    cosmos_backed: bool, runtime_transition: str,
) -> None:
    cosmos = Cosmos()
    repo = (
        CosmosModelEndpointRepository(
            cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
        )
        if cosmos_backed
        else HookedMemoryModelEndpointRepository()
    )
    later = datetime(2026, 11, 1, tzinfo=UTC)
    legacy = runtime_transition == "legacy-clock-skew"
    previous_check = later + timedelta(days=1) if legacy else later - timedelta(seconds=1)
    previous_revision = None if legacy else ENDPOINT_READINESS_RULES_REVISION
    original = await repo.create_endpoint(model_endpoint().model_copy(update={
        "access": EndpointAccess(
            checked_at=previous_check, rules_revision=previous_revision
        ),
        "runtime_access": (
            [] if runtime_transition == "empty-to-nonempty" else [GatewayRuntimeAccess(
                gateway_id="gateway", gateway_name="Gateway",
                checked_at=previous_check, rules_revision=previous_revision,
            )]
        ),
    }), audit())
    checked = ModelEndpoint.model_validate({
        **original.model_dump(by_alias=False),
        "endpoint": "https://confirmed.services.ai.azure.com",
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "detail_evidence": EndpointDetailEvidence(
            endpoint_source="account",
            provider_source="accountKind",
            endpoint_confirmed_at=later,
            provider_confirmed_at=later,
            account_read_at=later,
        ),
        "capabilities": ModelEndpointCapabilities(kind="AIServices", location="westus"),
        "access": EndpointAccess(
            can_read=True, checked_at=later, rules_revision=ENDPOINT_READINESS_RULES_REVISION,
        ),
        "runtime_access": (
            [] if runtime_transition == "nonempty-to-empty" else [GatewayRuntimeAccess(
                gateway_id="gateway", gateway_name="Gateway",
                checked_at=later, rules_revision=ENDPOINT_READINESS_RULES_REVISION,
            )]
        ),
    })

    async def finish_newer_check() -> None:
        await repo.record_endpoint_state(checked)

    if isinstance(repo, HookedMemoryModelEndpointRepository):
        repo.before_replace = finish_newer_check
    else:
        cosmos.containers["desired"].before_replace = finish_newer_check
    recorded = await repo.record_endpoint_state(original.model_copy(update={
        "last_synced_at": later + timedelta(seconds=1),
    }))

    assert recorded is not None
    assert recorded.endpoint == checked.endpoint
    assert recorded.provider == checked.provider
    assert recorded.detail_evidence == checked.detail_evidence
    assert recorded.capabilities == checked.capabilities
    assert recorded.access == checked.access
    assert recorded.runtime_access == checked.runtime_access
    assert recorded.last_synced_at == later + timedelta(seconds=1)


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


@pytest.mark.parametrize("cosmos_backed", [False, True])
@pytest.mark.parametrize("missing_fact", ["endpoint", "provider", "both"])
@pytest.mark.parametrize("partial_first", [False, True])
async def test_partial_account_reads_preserve_individual_confirmed_facts(
    cosmos_backed: bool, missing_fact: str, partial_first: bool,
) -> None:
    cosmos = Cosmos()
    repo = (
        CosmosModelEndpointRepository(
            cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
        )
        if cosmos_backed
        else InMemoryModelEndpointRepository()
    )
    original = await repo.create_endpoint(model_endpoint().model_copy(update={
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "detail_evidence": EndpointDetailEvidence(
            endpoint_source="resourceIdPlaceholder", provider_source="default",
        ),
    }), audit())
    first = datetime(2026, 11, 1, tzinfo=UTC)
    later = first + timedelta(seconds=1)
    confirmed = ModelEndpoint.model_validate({
        **original.model_dump(by_alias=False),
        "endpoint": "https://confirmed.openai.azure.com",
        "provider": ModelProvider.AZURE_OPENAI,
        "detail_evidence": EndpointDetailEvidence(
            endpoint_source="account", endpoint_confirmed_at=first,
            provider_source="accountKind", provider_confirmed_at=first,
            account_read_at=first,
        ),
        "capabilities": ModelEndpointCapabilities(kind="OpenAI", location="eastus"),
    })
    missing_url = missing_fact in {"endpoint", "both"}
    missing_kind = missing_fact in {"provider", "both"}
    partial = ModelEndpoint.model_validate({
        **original.model_dump(by_alias=False),
        "endpoint": original.endpoint if missing_url else "https://later.services.ai.azure.com",
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "detail_evidence": EndpointDetailEvidence(
            endpoint_source="resourceIdPlaceholder" if missing_url else "account",
            endpoint_confirmed_at=None if missing_url else later,
            provider_source="default" if missing_kind else "accountKind",
            provider_confirmed_at=None if missing_kind else later,
            account_read_at=later,
        ),
        "capabilities": ModelEndpointCapabilities(
            kind="Unrecognized" if missing_kind else "AIServices", location="westus",
        ),
        "azure_environment_tag": "latest-tag",
    })
    for snapshot in (partial, confirmed) if partial_first else (confirmed, partial):
        await repo.record_endpoint_state(snapshot)

    recorded = await repo.get_endpoint(TENANT_ID, original.id)

    assert recorded is not None and recorded.detail_evidence is not None
    assert recorded.endpoint == (confirmed.endpoint if missing_url else partial.endpoint)
    assert recorded.provider == (confirmed.provider if missing_kind else partial.provider)
    assert recorded.detail_evidence.endpoint_source == "account"
    assert recorded.detail_evidence.endpoint_confirmed_at == (first if missing_url else later)
    assert recorded.detail_evidence.provider_source == "accountKind"
    assert recorded.detail_evidence.provider_confirmed_at == (first if missing_kind else later)
    assert recorded.detail_evidence.account_read_at == later
    assert recorded.capabilities == partial.capabilities
    assert recorded.azure_environment_tag == "latest-tag"


@pytest.mark.parametrize("cosmos_backed", [False, True])
@pytest.mark.parametrize("old_check_finishes_last", [False, True])
async def test_late_runtime_rows_cannot_replace_a_newer_empty_preflight_snapshot(
    cosmos_backed: bool, old_check_finishes_last: bool,
) -> None:
    cosmos = Cosmos()
    repo = (
        CosmosModelEndpointRepository(
            cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
        )
        if cosmos_backed
        else InMemoryModelEndpointRepository()
    )
    original = await repo.create_endpoint(model_endpoint(), audit())
    started = datetime(2026, 11, 1, tzinfo=UTC)
    old_check = original.model_copy(update={
        "access": EndpointAccess(
            checked_at=started, rules_revision=ENDPOINT_READINESS_RULES_REVISION,
        ),
        "runtime_access": [GatewayRuntimeAccess(
            gateway_id="removed-gateway", gateway_name="Removed gateway",
            checked_at=started + timedelta(seconds=2),
            rules_revision=ENDPOINT_READINESS_RULES_REVISION,
        )],
    })
    newer_check = original.model_copy(update={
        "access": EndpointAccess(
            checked_at=started + timedelta(seconds=1),
            rules_revision=ENDPOINT_READINESS_RULES_REVISION,
        ),
        "runtime_access": [],
    })
    snapshots = (
        (newer_check, old_check) if old_check_finishes_last else (old_check, newer_check)
    )
    for snapshot in snapshots:
        await repo.record_endpoint_state(snapshot)

    recorded = await repo.get_endpoint(TENANT_ID, original.id)

    assert recorded is not None
    assert recorded.access == newer_check.access
    assert recorded.runtime_access == []


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


@pytest.mark.parametrize("cosmos_backed", [False, True])
@pytest.mark.parametrize("provider", [ModelProvider.OPENAI_COMPATIBLE, ModelProvider.GOOGLE_VERTEX])
async def test_stale_observation_retry_preserves_admin_provider_contract(
    cosmos_backed: bool, provider: ModelProvider,
) -> None:
    cosmos = Cosmos()
    repo = (
        CosmosModelEndpointRepository(
            cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
        )
        if cosmos_backed else HookedMemoryModelEndpointRepository()
    )
    vertex = provider == ModelProvider.GOOGLE_VERTEX
    original = await repo.create_endpoint(ModelEndpoint(
        id="external-contract", tenant_id=TENANT_ID, name="External",
        provider=provider,
        endpoint=(
            "https://aiplatform.us.rep.googleapis.com" if vertex else "https://chat.example.com/v1"
        ),
        vertex=VertexConfiguration(project_id="fictional-project", auth_mode="apiKey")
        if vertex else None,
    ), audit())
    configured = original.model_copy(update={
        "compatible_chat": not vertex,
        "vertex": VertexConfiguration(
            project_id="fictional-project", auth_mode="serviceAccountKey",
            service_account="runtime@fictional-project.iam.gserviceaccount.com",
        ) if vertex else None,
    })

    async def configure() -> None:
        await repo.save_endpoint(configured, audit())

    if cosmos_backed:
        cosmos.containers["desired"].before_replace = configure
    else:
        assert isinstance(repo, HookedMemoryModelEndpointRepository)
        repo.before_replace = configure
    await repo.record_endpoint_state(original.model_copy(update={
        "status": ModelEndpointStatus.UNREACHABLE,
        "access": EndpointAccess(message="The earlier sync failed"),
    }))
    stored = await repo.get_endpoint(TENANT_ID, original.id)
    assert stored is not None
    assert stored.compatible_chat == configured.compatible_chat
    assert stored.vertex == configured.vertex
    assert stored.status == ModelEndpointStatus.UNREACHABLE
    assert stored.access.message == "The earlier sync failed"


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
