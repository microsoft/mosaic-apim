import pytest
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.main import create_app


def test_health_and_readiness_are_anonymous(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}


# App Service's Always On pings the root every five minutes.
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_the_root_answers_200_with_nothing_and_needs_no_sign_in(method: str) -> None:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        api_client_id="api-client",
    )
    app = create_app(settings)
    with TestClient(app) as client:
        root = client.request(method, "/")
        # The API's own routes, under /api/v1, still need a token.
        assert client.get("/api/v1/groups").status_code == 401

    assert root.status_code == 200
    # No name, version, or configuration: nothing at all.
    assert root.content == b""
    assert "content-type" not in root.headers
    # It's there for App Service, not part of the API.
    assert "/" not in app.openapi()["paths"]
