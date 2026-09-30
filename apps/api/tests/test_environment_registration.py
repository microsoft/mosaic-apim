import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import RESOURCE_ID, FakeApim
from conftest import build_aoai_arm_client, build_arm_client
from mcp_double import FakeMcpServer, build_http_client
from mosaic_api.domain import (
    AuditEvent,
    GatewayCreate,
    GatewayUpdate,
    McpEndpointCreate,
    McpEndpointUpdate,
    ModelEndpointCreate,
    ModelEndpointUpdate,
    new_id,
)
from mosaic_api.environments import EnvironmentCatalog, EnvironmentColor, EnvironmentDefinition
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.repositories import (
    InMemoryEntitlementRepository,
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import GatewayService, McpEndpointService, ModelEndpointService
from mosaic_api.services.directory import Actor
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from pydantic import ValidationError as PydanticValidationError

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
MCP_URL = "https://mcp.example.com/mcp"


def _repositories() -> tuple[
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    InMemoryMcpEndpointRepository,
    InMemoryEnvironmentRepository,
]:
    gateways = InMemoryGatewayRepository()
    endpoints = InMemoryModelEndpointRepository()
    mcp = InMemoryMcpEndpointRepository()
    environments = InMemoryEnvironmentRepository(gateways, endpoints, mcp)
    return gateways, endpoints, mcp, environments


def _gateway_service(
    fake: FakeApim,
    gateways: InMemoryGatewayRepository,
    environments: InMemoryEnvironmentRepository,
) -> GatewayService:
    arm = build_arm_client(fake)
    return GatewayService(
        gateways,
        client_factory=lambda resource: ApimClient(arm, resource),
        principal_id="mosaic-managed-identity",
        environment_repository=environments,
    )


def _endpoint_service(
    fake: FakeCognitiveServices,
    gateways: InMemoryGatewayRepository,
    endpoints: InMemoryModelEndpointRepository,
    environments: InMemoryEnvironmentRepository,
) -> ModelEndpointService:
    arm = build_aoai_arm_client(fake)
    return ModelEndpointService(
        endpoints,
        gateway_repository=gateways,
        client_factory=lambda resource: CognitiveServicesClient(arm, resource),
        scanner=SubscriptionScanner(arm),
        principal_id="mosaic-managed-identity",
        environment_repository=environments,
    )


def _mcp_service(
    server: FakeMcpServer,
    gateways: InMemoryGatewayRepository,
    mcp: InMemoryMcpEndpointRepository,
    environments: InMemoryEnvironmentRepository,
) -> McpEndpointService:
    return McpEndpointService(
        mcp,
        client_factory=build_mcp_client_factory(build_http_client(server)),
        environment_repository=environments,
        gateway_repository=gateways,
        entitlement_repository=InMemoryEntitlementRepository(),
    )


async def _save_custom_catalog(environments: InMemoryEnvironmentRepository) -> None:
    catalog = EnvironmentCatalog.new(ACTOR.tenant_id)
    await environments.save_environment_catalog(
        catalog.model_copy(
            update={
                "environments": [
                    *catalog.environments,
                    EnvironmentDefinition(
                        key="perf",
                        display_name="Perf",
                        color=EnvironmentColor.BRAND,
                        aliases=["performance"],
                    ),
                ]
            }
        ),
        AuditEvent(
            id=new_id("audit"),
            tenant_id=ACTOR.tenant_id,
            action="environment.created",
            resource_type="environmentCatalog",
            resource_id=catalog.id,
            actor_object_id=ACTOR.object_id,
        ),
    )


@pytest.mark.asyncio
async def test_create_accepts_builtin_custom_and_unclassified_environments() -> None:
    gateways, endpoints, mcp, environments = _repositories()
    await _save_custom_catalog(environments)

    gateway = await _gateway_service(FakeApim(), gateways, environments).register(
        ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID, environment="production")
    )
    model_service = _endpoint_service(FakeCognitiveServices(), gateways, endpoints, environments)
    model = await model_service.register(
        ACTOR,
        ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID, environment="perf"),
    )
    mcp_endpoint = await _mcp_service(FakeMcpServer(), gateways, mcp, environments).register(
        ACTOR, McpEndpointCreate(endpoint=MCP_URL)
    )

    assert gateway.environment == "production"
    assert model.environment == "perf"
    assert mcp_endpoint.environment is None


@pytest.mark.asyncio
async def test_unknown_environment_is_a_validation_error() -> None:
    gateways, endpoints, mcp, environments = _repositories()
    cases = [
        (
            _gateway_service(FakeApim(), gateways, environments).register,
            ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID, environment="missing")
        ),
        (
            _endpoint_service(FakeCognitiveServices(), gateways, endpoints, environments).register,
            ACTOR,
            ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID, environment="missing"),
        ),
        (
            _mcp_service(FakeMcpServer(), gateways, mcp, environments).register,
            ACTOR,
            McpEndpointCreate(endpoint=MCP_URL, environment="missing"),
        ),
    ]

    for register, actor, request in cases:
        with pytest.raises(ValidationError) as refused:
            await register(actor, request)
        assert refused.value.details == {
            "reason": "unknownEnvironment",
            "environment": "missing",
        }


def test_update_models_reject_environment_field() -> None:
    with pytest.raises(PydanticValidationError):
        GatewayUpdate.model_validate({"environment": "production"})
    with pytest.raises(PydanticValidationError):
        ModelEndpointUpdate.model_validate({"environment": "production"})
    with pytest.raises(PydanticValidationError):
        McpEndpointUpdate.model_validate({"environment": "production"})


@pytest.mark.asyncio
async def test_preflight_records_and_clears_azure_environment_tag() -> None:
    gateways, endpoints, _, environments = _repositories()
    fake_apim = FakeApim()
    fake_apim.tags = {"Environment": "prod"}
    gateway_service = _gateway_service(fake_apim, gateways, environments)
    gateway = await gateway_service.register(
        ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID, environment="production")
    )
    assert gateway.azure_environment_tag == "prod"

    fake_apim.service_status = 403
    gateway = await gateway_service.preflight(ACTOR, gateway.id)
    assert gateway.status == "unauthorized"
    assert gateway.azure_environment_tag == "prod"

    fake_apim.service_status = 200
    fake_apim.tags = None
    gateway = await gateway_service.preflight(ACTOR, gateway.id)
    assert gateway.azure_environment_tag is None
    assert gateway.environment == "production"

    fake_aoai = FakeCognitiveServices()
    fake_aoai.tags = {"env": "dev"}
    endpoint_service = _endpoint_service(fake_aoai, gateways, endpoints, environments)
    endpoint = await endpoint_service.register(
        ACTOR, ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID, environment="development")
    )
    assert endpoint.azure_environment_tag == "dev"

    fake_aoai.account_status = 403
    endpoint = await endpoint_service.preflight(ACTOR, endpoint.id)
    assert endpoint.status == "unauthorized"
    assert endpoint.azure_environment_tag == "dev"

    fake_aoai.account_status = 200
    fake_aoai.tags = None
    endpoint = await endpoint_service.preflight(ACTOR, endpoint.id)
    assert endpoint.azure_environment_tag is None
    assert endpoint.environment == "development"


@pytest.mark.asyncio
async def test_model_endpoint_suggestions_include_tag_and_suggested_environment() -> None:
    gateways, endpoints, _, environments = _repositories()
    fake = FakeCognitiveServices()
    fake.accounts_by_subscription[next(iter(fake.accounts_by_subscription))][0]["tags"] = {
        "Environment": "prod"
    }
    service = _endpoint_service(fake, gateways, endpoints, environments)

    view = await service.suggestions(ACTOR)

    suggestion = next(item for item in view.suggestions if item.azure_resource_id == AI_RESOURCE_ID)
    assert suggestion.azure_environment_tag == "prod"
    assert suggestion.suggested_environment == "production"


@pytest.mark.asyncio
async def test_environment_lease_blocks_environment_registration_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mosaic_api.services import model_access

    monkeypatch.setattr(model_access, "SCOPE_LEASE_BACKOFF_SECONDS", 0)
    gateways, _, _, environments = _repositories()
    await gateways.acquire_scope_lease(ACTOR.tenant_id, "environments", "other", lease_seconds=60)
    service = _gateway_service(FakeApim(), gateways, environments)

    with pytest.raises(ConflictError):
        await service.register(
            ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID, environment="production")
        )

    assert (
        await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    ).environment is None


@pytest.mark.asyncio
async def test_model_endpoint_delete_holds_and_releases_endpoint_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mosaic_api.services import model_access

    monkeypatch.setattr(model_access, "SCOPE_LEASE_BACKOFF_SECONDS", 0)
    gateways, endpoints, _, environments = _repositories()
    service = _endpoint_service(FakeCognitiveServices(), gateways, endpoints, environments)
    endpoint = await service.register(ACTOR, ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID))
    scope = f"endpoint:{endpoint.id}"
    await gateways.acquire_scope_lease(ACTOR.tenant_id, scope, "other", lease_seconds=60)

    with pytest.raises(ConflictError):
        await service.delete(ACTOR, endpoint.id)

    await gateways.release_scope_lease(ACTOR.tenant_id, scope, "other")
    await service.delete(ACTOR, endpoint.id)
    assert gateways.scope_leases == {}
