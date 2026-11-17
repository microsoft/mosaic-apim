from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from aoai_double import FakeCognitiveServices
from apim_double import RESOURCE_ID, FakeApim, FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp_double import FakeMcpServer, build_http_client
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    Gateway,
    ModelApi,
    ModelEndpoint,
    ModelProvider,
    Publication,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    PublishRun,
    TokenEnforcement,
    new_id,
)
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.repositories import (
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
)
from mosaic_api.services import (
    DirectoryService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
    PublishingService,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from mosaic_api.services.mcp_publishing import McpPublishingService
from mosaic_api.services.model_pools import ModelPoolService


async def _no_sleep(_seconds: float) -> None:
    return None


async def seed_known_policy_target(
    gateways: InMemoryGatewayRepository,
    endpoints: InMemoryModelEndpointRepository,
    model: ModelApi,
    *,
    publication: Publication | None = None,
) -> ModelApi:
    """Seed fictional observed tier/chat evidence, not a checker override or live observation."""

    tenant = model.tenant_id
    audit = AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant,
        action="test.policyEvidence",
        resource_type="test",
        resource_id=model.id,
        actor_object_id="local-admin",
    )
    gateway = await gateways.get_gateway(tenant, model.gateway_id)
    if gateway is None:
        arm = ApimResourceId.parse(RESOURCE_ID)
        gateway = Gateway(
            id=model.gateway_id,
            tenant_id=tenant,
            name="Fictional Developer gateway",
            azure_resource_id=arm.canonical,
            subscription_id=arm.subscription_id,
            resource_group=arm.resource_group,
            service_name=arm.service_name,
        )
    await gateways.save_gateway(
        gateway.model_copy(
            update={
                "capabilities": gateway.capabilities.model_copy(update={"sku_name": "Developer"})
            }
        ),
        audit,
    )
    publication = publication or Publication(
        id=f"policy-publication-{model.id}",
        tenant_id=tenant,
        gateway_id=model.gateway_id,
        model_endpoint_id=f"policy-endpoint-{model.id}",
        deployment_name="policy-chat",
        provider=ModelProvider.AZURE_OPENAI,
        display_name=model.display_name,
        api_name=model.api_name,
        api_path=model.path,
        backend_name="mosaic-policy-chat",
        fragment_name="mosaic-policy-chat",
        product_name="mosaic-policy-chat",
        subscription_name="mosaic-policy-chat",
        enforcement=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            tokens_per_minute=1000,
        ),
        shape_version="1.0",
        status=PublicationStatus.PUBLISHED,
        model_api_id=model.id,
        resources=[
            PublishedResource(
                kind=PublishedResourceKind.API,
                name=model.api_name,
                resource_id=f"{gateway.azure_resource_id}/apis/{model.api_name}",
                created_by_mosaic=True,
            )
        ],
    )
    endpoint = ModelEndpoint(
        id=publication.model_endpoint_id,
        tenant_id=tenant,
        name="Fictional chat endpoint",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint="https://fixture.openai.azure.com",
    )
    await endpoints.save_endpoint(endpoint, audit)
    snapshot = new_id("snapshot")
    deployment = ObservedModelDeployment(
        id=f"policy-deployment-{model.id}",
        tenant_id=tenant,
        endpoint_id=endpoint.id,
        snapshot_id=snapshot,
        deployment_name=publication.deployment_name,
        model_name="gpt-4o",
        model_format="OpenAI",
        capabilities={"chatCompletion": "true"},
    )
    await endpoints.replace_observed_for_endpoint(tenant, endpoint.id, [deployment], snapshot)
    await gateways.save_publication(publication, audit)
    return await gateways.save_model_api(
        model.model_copy(update={"publication_id": publication.id}), audit
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        cors_origins=["http://localhost:5173"],
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def directory_client(settings: Settings) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def install_directory_lookup(app: FastAPI, lookup: DirectoryLookup) -> None:
    app.state.directory_lookup = lookup
    app.state.directory_service = DirectoryService(
        app.state.repository,
        gateway_repository=app.state.gateway_repository,
        entitlement_repository=app.state.entitlement_repository,
        directory_lookup=lookup,
        group_claims_enabled=app.state.settings.entra_group_claims,
        managed_identity_principal_id=app.state.settings.managed_identity_principal_id,
        apim_service_name=app.state.settings.apim_service_name,
        api_client_id=app.state.settings.api_client_id,
        spa_client_id=app.state.settings.spa_client_id,
        model_runtime_client_id=app.state.settings.model_runtime_client_id,
        model_client_id=app.state.settings.model_client_id,
    )
    # The portal services hold this same instance, so swap its lookup rather than rebuild it.
    app.state.entitlement_service._directory_lookup = lookup


@pytest.fixture
def fake_apim() -> FakeApim:
    return FakeApim()


def build_arm_client(fake: FakeApim) -> ArmClient:
    transport = httpx.MockTransport(fake.handler)
    return ArmClient(
        cast(AsyncTokenCredential, FakeCredential()),
        client=httpx.AsyncClient(transport=transport),
        sleep=_no_sleep,
    )


def build_gateway_service(
    fake: FakeApim,
    repository: InMemoryGatewayRepository | None = None,
    **kwargs: Any,
) -> GatewayService:
    arm = build_arm_client(fake)
    return GatewayService(
        repository or InMemoryGatewayRepository(),
        client_factory=lambda resource: ApimClient(arm, resource),
        principal_id=kwargs.pop("principal_id", "mosaic-managed-identity"),
        bootstrap_resource_id=kwargs.pop("bootstrap_resource_id", RESOURCE_ID),
    )


@pytest.fixture
def gateway_repository() -> InMemoryGatewayRepository:
    return InMemoryGatewayRepository()


@pytest.fixture
def gateway_service(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> GatewayService:
    return build_gateway_service(fake_apim, gateway_repository)


@pytest.fixture
def gateway_client(
    settings: Settings,
    fake_apim: FakeApim,
    gateway_repository: InMemoryGatewayRepository,
) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    with TestClient(app) as test_client:
        app.state.gateway_repository = gateway_repository
        app.state.gateway_service = build_gateway_service(fake_apim, gateway_repository)
        yield test_client


@pytest.fixture
def apim_resource() -> ApimResourceId:
    return ApimResourceId.parse(RESOURCE_ID)


@pytest.fixture
def fake_aoai() -> FakeCognitiveServices:
    return FakeCognitiveServices()


def build_aoai_arm_client(fake: FakeCognitiveServices) -> ArmClient:
    transport = httpx.MockTransport(fake.handler)
    return ArmClient(
        cast(AsyncTokenCredential, FakeCredential()),
        client=httpx.AsyncClient(transport=transport),
        sleep=_no_sleep,
    )


def build_endpoint_service(
    fake: FakeCognitiveServices,
    *,
    repository: InMemoryModelEndpointRepository | None = None,
    gateway_repository: InMemoryGatewayRepository | None = None,
    scanner: bool = True,
    **kwargs: Any,
) -> ModelEndpointService:
    arm = build_aoai_arm_client(fake)
    return ModelEndpointService(
        repository or InMemoryModelEndpointRepository(),
        gateway_repository=gateway_repository or InMemoryGatewayRepository(),
        client_factory=lambda resource: CognitiveServicesClient(arm, resource),
        scanner=SubscriptionScanner(arm) if scanner else None,
        principal_id=kwargs.pop("principal_id", "mosaic-managed-identity"),
        bootstrap_subscription_id=kwargs.pop("bootstrap_subscription_id", None),
        secret_resolver=kwargs.pop("secret_resolver", None),
        key_probe=kwargs.pop("key_probe", None),
        vault_locator=kwargs.pop("vault_locator", None),
        key_store=kwargs.pop("key_store", None),
    )


@pytest.fixture
def endpoint_repository() -> InMemoryModelEndpointRepository:
    return InMemoryModelEndpointRepository()


@pytest.fixture
def endpoint_service(
    fake_aoai: FakeCognitiveServices,
    endpoint_repository: InMemoryModelEndpointRepository,
    gateway_repository: InMemoryGatewayRepository,
) -> ModelEndpointService:
    return build_endpoint_service(
        fake_aoai,
        repository=endpoint_repository,
        gateway_repository=gateway_repository,
    )


@pytest.fixture
def endpoint_client(
    settings: Settings,
    fake_aoai: FakeCognitiveServices,
    endpoint_repository: InMemoryModelEndpointRepository,
    gateway_repository: InMemoryGatewayRepository,
) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    with TestClient(app) as test_client:
        app.state.gateway_repository = gateway_repository
        app.state.model_endpoint_repository = endpoint_repository
        app.state.model_endpoint_service = build_endpoint_service(
            fake_aoai,
            repository=endpoint_repository,
            gateway_repository=gateway_repository,
        )
        yield test_client


def build_publishing_service(
    fake: FakeApim,
    gateway_repository: InMemoryGatewayRepository,
    endpoint_repository: InMemoryModelEndpointRepository,
) -> PublishingService:
    arm = build_arm_client(fake)
    return PublishingService(
        gateway_repository,
        endpoint_repository=endpoint_repository,
        client_factory=lambda resource: ApimClient(arm, resource),
        writer_factory=lambda resource: ApimWriter(arm, resource),
    )


def build_model_pool_service(
    fake: FakeApim,
    gateway_repository: InMemoryGatewayRepository,
    endpoint_repository: InMemoryModelEndpointRepository,
    *,
    environment_repository: InMemoryEnvironmentRepository | None = None,
    directory_repository: InMemoryDirectoryRepository | None = None,
    entitlement_repository: InMemoryEntitlementRepository | None = None,
    cost_center_repository: InMemoryCostCenterRepository | None = None,
    runtime_client_id: str | None = "22222222-2222-2222-2222-222222222222",
    security_group_claims: bool = True,
    pricing_repository: InMemoryPricingRepository | None = None,
) -> ModelPoolService:
    arm = build_arm_client(fake)
    return ModelPoolService(
        gateway_repository,
        endpoint_repository=endpoint_repository,
        client_factory=lambda resource: ApimClient(arm, resource),
        writer_factory=lambda resource: ApimWriter(arm, resource),
        environment_repository=environment_repository,
        directory_repository=directory_repository,
        entitlement_repository=entitlement_repository,
        cost_center_repository=cost_center_repository,
        model_runtime_client_id=runtime_client_id,
        security_group_claims=security_group_claims,
        pricing_repository=pricing_repository,
    )


def build_mcp_publishing_service(
    fake: FakeApim,
    gateway_repository: InMemoryGatewayRepository,
    mcp_repository: InMemoryMcpEndpointRepository,
    *,
    directory_repository: InMemoryDirectoryRepository | None = None,
    entitlement_repository: InMemoryEntitlementRepository | None = None,
    runtime_client_id: str | None = "22222222-2222-2222-2222-222222222222",
    security_group_claims: bool = True,
    cost_center_repository: InMemoryCostCenterRepository | None = None,
) -> McpPublishingService:
    arm = build_arm_client(fake)
    return McpPublishingService(
        gateway_repository,
        mcp_endpoint_repository=mcp_repository,
        entitlement_repository=entitlement_repository or InMemoryEntitlementRepository(),
        directory_repository=directory_repository or InMemoryDirectoryRepository(),
        client_factory=lambda resource: ApimClient(arm, resource),
        writer_factory=lambda resource: ApimWriter(arm, resource),
        runtime_client_id=runtime_client_id,
        security_group_claims=security_group_claims,
        cost_center_repository=cost_center_repository,
    )


async def reviewed_unpublish(
    service: PublishingService | McpPublishingService, actor: Actor, publication_id: str
) -> PublishRun:
    """Unpublish the way the console does: plan the unpublish, then run exactly that plan."""

    plan = await service.plan_unpublish(actor, publication_id)
    return await service.unpublish(actor, publication_id, plan.id)


@pytest.fixture
def fake_mcp() -> FakeMcpServer:
    return FakeMcpServer()


def build_mcp_service(
    server: FakeMcpServer,
    *,
    repository: InMemoryMcpEndpointRepository | None = None,
    gateway_repository: InMemoryGatewayRepository | None = None,
    entitlement_repository: InMemoryEntitlementRepository | None = None,
    secret: str = "vault-token",
    token: str = "entra-token",
) -> McpEndpointService:
    async def read_secret(_uri: str) -> str:
        return secret

    async def issue_token(_audience: str) -> str:
        return token

    return McpEndpointService(
        repository or InMemoryMcpEndpointRepository(),
        gateway_repository=gateway_repository or InMemoryGatewayRepository(),
        entitlement_repository=entitlement_repository or InMemoryEntitlementRepository(),
        client_factory=build_mcp_client_factory(build_http_client(server)),
        secret_resolver=read_secret,
        token_resolver=issue_token,
        # The guard has its own tests; service tests use an ordinary https host so they exercise
        # the service rather than re-testing admission.
        require_https=True,
        allow_private_endpoints=False,
    )


@pytest.fixture
def mcp_repository() -> InMemoryMcpEndpointRepository:
    return InMemoryMcpEndpointRepository()


@pytest.fixture
def mcp_service(
    fake_mcp: FakeMcpServer, mcp_repository: InMemoryMcpEndpointRepository
) -> McpEndpointService:
    return build_mcp_service(fake_mcp, repository=mcp_repository)


@pytest.fixture
def mcp_client(
    settings: Settings,
    fake_mcp: FakeMcpServer,
    mcp_repository: InMemoryMcpEndpointRepository,
) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    with TestClient(app) as test_client:
        app.state.mcp_endpoint_repository = mcp_repository
        app.state.mcp_endpoint_service = build_mcp_service(fake_mcp, repository=mcp_repository)
        yield test_client
