"""Logs that carry only status codes, durations and tool names.

The MCP SDK logs session IDs, and HTTP libraries log URLs and host names. None of that may reach
the logs, so a record from any other logger keeps its level and exception type, never its message.
"""

import logging
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO

from starlette.types import ASGIApp, Message, Receive, Scope, Send

LOGGER_NAME = "m_agent"
logger = logging.getLogger(LOGGER_NAME)


class WithholdOtherMessages(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == LOGGER_NAME or record.name.startswith(f"{LOGGER_NAME}."):
            return True
        error = record.exc_info[1] if record.exc_info else None
        record.msg = "message withheld" + (f" ({type(error).__name__})" if error else "")
        record.args = None
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_logging(stream: IO[str] | None = None) -> None:
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(WithholdOtherMessages())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    logger.setLevel(logging.INFO)


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


@contextmanager
def tool_call(name: str) -> Iterator[None]:
    started = time.perf_counter()
    outcome = "error"
    try:
        yield
        outcome = "ok"
    finally:
        logger.info("tool=%s outcome=%s duration_ms=%d", name, outcome, _elapsed_ms(started))


class RequestLog:
    """ASGI middleware that logs each HTTP request's status and duration, and nothing else."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        status = 500

        async def record_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, record_status)
        finally:
            logger.info("request status=%d duration_ms=%d", status, _elapsed_ms(started))
