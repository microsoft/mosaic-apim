import pytest
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import build_arm_client, build_gateway_service, reviewed_unpublish
from mosaic_api.domain import (
    AuditEvent,
    GatewayCreate,
    GatewayUpdate,
    ManagementMode,
    McpEndpoint,
    McpEndpointStatus,
    McpInventorySummary,
    McpPublicationCreate,
    PublishRunStatus,
    new_id,
)
from mosaic_api.environments import EnvironmentCatalog
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.apim import ApimClient, ApimWriter
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.mcp_publishing import McpPublishingService

ACTOR = Actor(
    object_id="admin-object-id",
    tenant_id="33333333-3333-3333-3333-333333333333",
)


def _audit(action: str = "environment.updated") -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=ACTOR.tenant_id,
        action=action,
        resource_type="test",
        resource_id="test",
        actor_object_id=ACTOR.object_id,
    )


def _request(gateway_id: str, endpoint_id: str) -> McpPublicationCreate:
    return McpPublicationCreate.model_validate(
        {"gateway_id": gateway_id, "mcp_endpoint_id": endpoint_id}
    )


class Harness:
    def __init__(self) -> None:
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
        self.gateway_repository = InMemoryGatewayRepository()
        self.endpoint_repository = InMemoryMcpEndpointRepository()
        self.model_endpoint_repository = InMemoryModelEndpointRepository()
        self.environment_repository = InMemoryEnvironmentRepository(
            self.gateway_repository,
            self.model_endpoint_repository,
            self.endpoint_repository,
        )
        self.gateways = build_gateway_service(self.apim, self.gateway_repository)
        arm = build_arm_client(self.apim)
        self.service = McpPublishingService(
            self.gateway_repository,
            mcp_endpoint_repository=self.endpoint_repository,
            entitlement_repository=InMemoryEntitlementRepository(),
            directory_repository=InMemoryDirectoryRepository(),
            client_factory=lambda resource: ApimClient(arm, resource),
            writer_factory=lambda resource: ApimWriter(arm, resource),
            runtime_client_id="22222222-2222-2222-2222-222222222222",
            environment_repository=self.environment_repository,
        )
        self.gateway_id = ""
        self.endpoint_id = ""

    async def setup(self) -> None:
        gateway = await self.gateways.register(
            ACTOR, GatewayCreate.model_validate({"azure_resource_id": RESOURCE_ID})
        )
        await self.gateways.sync_now(ACTOR, gateway.id)
        gateway = await self.gateways.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
        )
        self.gateway_id = gateway.id
        endpoint = McpEndpoint(
            id="mcp-endpoint-orders",
            tenant_id=ACTOR.tenant_id,
            name="Orders MCP",
            endpoint="https://mcp.contoso.test/mcp",
            status=McpEndpointStatus.CONNECTED,
            inventory=McpInventorySummary(tools=2),
        )
        saved = await self.endpoint_repository.save_endpoint(endpoint, _audit("mcpEndpoint"))
        self.endpoint_id = saved.id

    async def set_gateway_environment(self, environment: str | None) -> None:
        gateway = await self.gateway_repository.get_gateway(ACTOR.tenant_id, self.gateway_id)
        assert gateway is not None
        await self.gateway_repository.save_gateway(
            gateway.model_copy(update={"environment": environment}), _audit()
        )

    async def set_endpoint_environment(self, environment: str | None) -> None:
        endpoint = await self.endpoint_repository.get_endpoint(ACTOR.tenant_id, self.endpoint_id)
        assert endpoint is not None
        await self.endpoint_repository.save_endpoint(
            endpoint.model_copy(update={"environment": environment}), _audit()
        )

    async def publish(self) -> str:
        publication = await self.service.create(ACTOR, _request(self.gateway_id, self.endpoint_id))
        return publication.id

    async def plan_apply(self, publication_id: str) -> None:
        plan = await self.service.plan(ACTOR, publication_id)
        run = await self.service.apply(ACTOR, publication_id, plan.id)
        await self.service.wait_for_idle()
        saved = await self.service.get_run(ACTOR, publication_id, run.id)
        assert saved.status == PublishRunStatus.SUCCEEDED

    async def save_catalog(self, catalog: EnvironmentCatalog) -> EnvironmentCatalog:
        return await self.environment_repository.save_environment_catalog(catalog, _audit())


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.setup()
    return built


async def test_create_refuses_blocked_pair_and_allows_same_environment(
    harness: Harness,
) -> None:
    await harness.set_gateway_environment("development")
    await harness.set_endpoint_environment("production")

    with pytest.raises(ConflictError) as error:
        await harness.service.create(ACTOR, _request(harness.gateway_id, harness.endpoint_id))

    assert error.value.details["reason"] == "environmentBlocked"
    assert error.value.details["verdict"]["level"] == "blocked"

    await harness.set_endpoint_environment("development")
    publication = await harness.service.create(
        ACTOR, _request(harness.gateway_id, harness.endpoint_id)
    )
    assert publication.id


async def test_plan_warns_for_unclassified_pair(harness: Harness) -> None:
    await harness.set_gateway_environment("development")
    publication_id = await harness.publish()

    plan = await harness.service.plan(ACTOR, publication_id)

    assert any("unclassified" in warning for warning in plan.warnings)


async def test_plan_refuses_when_pairing_becomes_blocked(harness: Harness) -> None:
    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("production")
    publication_id = await harness.publish()
    await harness.set_endpoint_environment("development")

    with pytest.raises(ConflictError) as error:
        await harness.service.plan(ACTOR, publication_id)

    assert error.value.details["reason"] == "environmentBlocked"


async def test_apply_replans_when_environment_fingerprint_changes(harness: Harness) -> None:
    await harness.set_gateway_environment("development")
    await harness.set_endpoint_environment("development")
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.save_catalog(
        EnvironmentCatalog.new(ACTOR.tenant_id).model_copy(update={"require_classification": True})
    )

    with pytest.raises(ConflictError, match="Re-plan"):
        await harness.service.apply(ACTOR, publication_id, plan.id)


async def test_apply_refuses_when_pairing_becomes_blocked(harness: Harness) -> None:
    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("production")
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.set_endpoint_environment("development")

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(ACTOR, publication_id, plan.id)

    assert error.value.details["reason"] == "environmentBlocked"


async def test_unpublish_ignores_current_blocked_environment(harness: Harness) -> None:
    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("production")
    publication_id = await harness.publish()
    await harness.plan_apply(publication_id)
    await harness.set_endpoint_environment("development")

    run = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    assert (
        await harness.service.get_run(ACTOR, publication_id, run.id)
    ).status == PublishRunStatus.SUCCEEDED
