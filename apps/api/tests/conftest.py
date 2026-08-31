import pytest
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.main import create_app


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
def client(settings: Settings) -> TestClient:
    with TestClient(create_app(settings)) as test_client:
        yield test_client
