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
from mosaic_api.domain import ApimResourceId
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.apim import ApimClient, ArmClient
from mosaic_api.main import create_app
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import GatewayService, McpEndpointService, ModelEndpointService
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory


async def _no_sleep(_seconds: float) -> None:
    return None


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


@pytest.fixture
def fake_mcp() -> FakeMcpServer:
    return FakeMcpServer()


def build_mcp_service(
    server: FakeMcpServer,
    *,
    repository: InMemoryMcpEndpointRepository | None = None,
    secret: str = "vault-token",
    token: str = "entra-token",
) -> McpEndpointService:
    async def read_secret(_uri: str) -> str:
        return secret

    async def issue_token(_audience: str) -> str:
        return token

    return McpEndpointService(
        repository or InMemoryMcpEndpointRepository(),
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
