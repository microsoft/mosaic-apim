from collections.abc import Iterator
from typing import Any, cast

import httpx
import pytest
from apim_double import RESOURCE_ID, FakeApim, FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import ApimResourceId
from mosaic_api.integrations.apim import ApimClient, ArmClient
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryGatewayRepository
from mosaic_api.services import GatewayService


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
