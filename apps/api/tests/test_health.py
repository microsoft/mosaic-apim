from fastapi.testclient import TestClient


def test_health_and_readiness_are_anonymous(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/readyz").json() == {"status": "ready"}
