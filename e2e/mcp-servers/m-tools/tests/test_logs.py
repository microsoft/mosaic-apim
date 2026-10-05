import io
import logging
import re
import sys

import httpx
import pytest

from m_tools.logs import LOGGER_NAME, WithholdOtherMessages, configure_logging
from m_tools.server import create_app
from support import serve

MOSAIC_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def record(
    name: str, message: str, *args: object, error: BaseException | None = None
) -> logging.LogRecord:
    exc_info = (type(error), error, None) if error else None
    return logging.LogRecord(name, logging.WARNING, __file__, 1, message, args, exc_info)


def test_another_logger_s_message_and_arguments_are_withheld() -> None:
    session_record = record(
        "mcp.server.streamable_http_manager", "Created new transport with session ID: %s", "abc123"
    )
    assert WithholdOtherMessages().filter(session_record)
    assert session_record.getMessage() == "message withheld"


def test_another_logger_s_exception_keeps_only_its_type() -> None:
    failed = record("httpx", "GET https://gateway.example.test failed", error=ValueError("secret"))
    WithholdOtherMessages().filter(failed)
    assert failed.getMessage() == "message withheld (ValueError)"
    assert failed.exc_info is None


def test_the_server_s_own_records_are_kept() -> None:
    own = record(LOGGER_NAME, "tool=%s outcome=%s duration_ms=%d", "echo", "ok", 3)
    WithholdOtherMessages().filter(own)
    assert own.getMessage() == "tool=echo outcome=ok duration_ms=3"


def test_configure_logging_withholds_third_party_details_and_drops_their_info_records() -> None:
    stream = io.StringIO()
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger(LOGGER_NAME).level)
    try:
        configure_logging(stream)
        logging.getLogger("mcp.server.streamable_http_manager").info("session %s", "abc123")
        logging.getLogger("uvicorn.error").warning("bad request from %s", "203.0.113.9")
        logging.getLogger(LOGGER_NAME).info("request status=%d duration_ms=%d", 200, 4)
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
        logging.getLogger(LOGGER_NAME).setLevel(saved[2])

    output = stream.getvalue()
    assert "abc123" not in output
    assert "203.0.113.9" not in output
    assert "uvicorn.error message withheld" in output
    assert "m_tools request status=200 duration_ms=4" in output


def test_requests_and_tool_calls_log_only_statuses_durations_and_tool_names(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with serve(create_app()) as origin:
        with httpx.Client() as client:
            started = client.post(
                f"{origin}/mcp",
                headers=MOSAIC_HEADERS,
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "mosaic", "version": "1.0.0"},
                    },
                },
            )
            session_id = started.headers["mcp-session-id"]
            client.post(
                f"{origin}/mcp",
                headers={**MOSAIC_HEADERS, "Mcp-Session-Id": session_id},
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "echo", "arguments": {"text": "private words"}},
                },
            )

    messages = [entry.getMessage() for entry in caplog.records if entry.name == LOGGER_NAME]
    assert messages
    for text in messages:
        assert re.fullmatch(
            r"request status=\d{3} duration_ms=\d+|tool=[a-z_]+ outcome=(ok|error) duration_ms=\d+",
            text,
        ), text
    assert any(text.startswith("tool=echo outcome=ok") for text in messages)
    every_message = "\n".join(entry.getMessage() for entry in caplog.records)
    assert session_id not in every_message
    assert "private words" not in every_message
    assert origin.removeprefix("http://") not in every_message


def test_configure_logging_writes_to_standard_output_by_default() -> None:
    root = logging.getLogger()
    saved = (root.handlers[:], root.level, logging.getLogger(LOGGER_NAME).level)
    try:
        configure_logging()
        handler = root.handlers[0]
        assert isinstance(handler, logging.StreamHandler)
        assert handler.stream is sys.stdout
        assert any(isinstance(item, WithholdOtherMessages) for item in handler.filters)
        assert root.level == logging.WARNING
    finally:
        root.handlers[:], root.level = saved[0], saved[1]
        logging.getLogger(LOGGER_NAME).setLevel(saved[2])
