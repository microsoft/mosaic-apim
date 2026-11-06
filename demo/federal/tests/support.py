import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import uvicorn
from azure.core.credentials import AccessToken
from starlette.types import ASGIApp

FAKE_TOKEN = "private-managed-identity-token"
SCOPE = "api://11111111-1111-1111-1111-111111111111/.default"


class FakeCredential:
    def __init__(self, token: str = FAKE_TOKEN, *, fail: bool = False) -> None:
        self.token, self.fail = token, fail
        self.scopes: list[tuple[str, ...]] = []
        self.closed = False

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        self.scopes.append(scopes)
        if self.fail:
            raise RuntimeError("private credential details must not escape")
        return AccessToken(self.token, int(time.time()) + 3600)

    async def close(self) -> None:
        self.closed = True


def model_env(**changes: str) -> dict[str, str]:
    return {
        "AZURE_CLIENT_ID": "22222222-2222-2222-2222-222222222222",
        "MOSAIC_MODEL_ENDPOINT": "https://gateway.example.test/models/luna",
        "MOSAIC_MODEL_DEPLOYMENT": "gpt-5.6-luna",
        "MOSAIC_MODEL_API_VERSION": "2024-10-21",
        "MOSAIC_RUNTIME_SCOPE": SCOPE,
        "MOSAIC_COST_CENTER": "FAA-RND",
        **changes,
    }


@contextmanager
def serve(app: ASGIApp) -> Iterator[str]:
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
            raise RuntimeError("Local test server did not start.")
        time.sleep(0.01)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        if thread.is_alive():
            raise RuntimeError("Local test server did not stop.")
