import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import uvicorn
from azure.core.credentials import AccessToken
from starlette.types import ASGIApp

from m_agent.gateway import ModelSettings
from m_agent.server import Settings

# Shaped like a JWT, so the tests prove a real one would be kept out of errors.
FAKE_TOKEN = "eyJhbGciOiJub25lIn0.eyJzdWIiOiJtLWFnZW50LXRlc3QifQ.ZmFrZS1zaWduYXR1cmU"
RUNTIME_SCOPE = "api://11111111-1111-1111-1111-111111111111/.default"
ANSWER = {
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": " Paris. "},
            "finish_reason": "stop",
        }
    ]
}


class FakeCredential:
    """Stands in for the container app's managed identity."""

    def __init__(self, token: str = FAKE_TOKEN, error: Exception | None = None) -> None:
        self.token = token
        self.error = error
        self.scopes: list[tuple[str, ...]] = []
        self.closed = False

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return AccessToken(self.token, int(time.time()) + 3600)

    async def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> "FakeCredential":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()


class FakeGateway:
    """Stands in for MOSAIC's gateway, and records every model call it receives."""

    def __init__(self, respond: Callable[[httpx.Request], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.respond = respond or (lambda request: httpx.Response(200, json=ANSWER))

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


def model_settings(**changes: Any) -> ModelSettings:
    values: dict[str, Any] = {
        "endpoint": "https://gateway.example.test/models/chat",
        "deployment": "gpt-test",
        "api_version": "2024-10-21",
        "runtime_scope": RUNTIME_SCOPE,
    }
    return ModelSettings(**(values | changes))


def settings(**changes: Any) -> Settings:
    return Settings(model=model_settings(**changes))


@contextmanager
def serve(app: ASGIApp) -> Iterator[str]:
    """Run the app on a free local port and yield its origin."""

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=0,
        log_config=None,
        access_log=False,
        lifespan="on",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("The test server didn't start.")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
