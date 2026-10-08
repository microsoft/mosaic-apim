"""smoke.py's M-protected checks against stand-in servers, using only the standard library."""

import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import smoke

Answer = Callable[[BaseHTTPRequestHandler], None]
# Releases every stand-in that's holding a request open, so each test's server stops at once.
RELEASED = threading.Event()


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request: object, client_address: object) -> None:
        # A request the smoke check stopped waiting for can't be answered. That's expected here.
        pass


@contextmanager
def serve(answer: Answer) -> Iterator[tuple[str, list[str | None]]]:
    """Answer each POST with answer, on a free local port, recording its Authorization header."""

    received: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            received.append(self.headers.get("Authorization"))
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            answer(self)

        def log_message(self, format: str, *args: object) -> None:
            pass

    RELEASED.clear()
    server = QuietServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp", received
    finally:
        RELEASED.set()
        server.shutdown()
        server.server_close()


def status(code: int, **headers: str) -> Answer:
    def answer(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(code)
        for name, value in headers.items():
            handler.send_header(name.replace("_", "-"), value)
        handler.send_header("Content-Length", "0")
        handler.end_headers()

    return answer


def hang(handler: BaseHTTPRequestHandler) -> None:
    RELEASED.wait(10)


def run(url: str) -> tuple[bool, list[str]]:
    lines: list[str] = []
    passed = smoke.run("protected", url, lines.append)
    return passed, lines


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(smoke, "PROTECTED_TIMEOUT_SECONDS", 0.3)
    return 0.3


def test_401_at_once_without_a_token_and_with_a_malformed_one_passes() -> None:
    with serve(status(401, WWW_Authenticate="Bearer")) as (url, received):
        passed, lines = run(url)

    assert passed, lines
    assert lines == [
        "PASS protected: an initialize without a token returned HTTP 401",
        "PASS protected: an initialize with a token that isn't one returned HTTP 401",
    ]
    # Never a real credential: no token, then a bearer value that isn't even shaped like a JWT.
    anonymous, malformed = received
    assert anonymous is None
    assert malformed is not None
    assert malformed.startswith("Bearer ")
    assert "." not in malformed


def test_a_server_that_doesnt_answer_in_time_fails(short_timeout: float) -> None:
    with serve(hang) as (url, _):
        passed, lines = run(url)

    assert not passed
    assert lines == [
        f"FAIL protected: An initialize without a token got no answer within {short_timeout:g} "
        "seconds."
    ]


def test_answering_only_the_anonymous_request_in_time_fails(short_timeout: float) -> None:
    # The old Functions host answered some requests with a slow 401 and left others hanging.
    def refuse_anonymous_but_hang_on_a_token(handler: BaseHTTPRequestHandler) -> None:
        if handler.headers.get("Authorization"):
            hang(handler)
        else:
            status(401)(handler)

    with serve(refuse_anonymous_but_hang_on_a_token) as (url, _):
        passed, lines = run(url)

    assert not passed
    assert lines == [
        "PASS protected: an initialize without a token returned HTTP 401",
        "FAIL protected: An initialize with a token that isn't one got no answer within "
        f"{short_timeout:g} seconds.",
    ]


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        (status(302, Location="https://login.example.test/authorize"), 302),
        (status(403), 403),
        (status(200), 200),
    ],
)
def test_anything_but_401_fails_and_a_redirect_isn_t_followed(answer: Answer, code: int) -> None:
    with serve(answer) as (url, received):
        passed, lines = run(url)

    assert not passed
    assert lines == [
        f"FAIL protected: An initialize without a token returned HTTP {code}, not 401."
    ]
    assert len(received) == 1


def test_a_401_whose_body_never_arrives_fails_in_time(short_timeout: float) -> None:
    def stall_after_the_headers(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(401)
        handler.send_header("Content-Length", "10")
        handler.end_headers()
        handler.wfile.flush()
        hang(handler)

    with serve(stall_after_the_headers) as (url, _):
        passed, lines = run(url)

    assert not passed
    assert lines == [
        f"FAIL protected: An initialize without a token got no answer within {short_timeout:g} "
        "seconds."
    ]


def test_a_401_trickled_out_past_the_deadline_fails_at_the_deadline(short_timeout: float) -> None:
    # Each byte comes well inside the socket timeout, but the whole answer takes many times longer.
    answer = (
        b"HTTP/1.0 401 Unauthorized\r\nContent-Length: 0\r\nX-Padding: " + b"x" * 100 + b"\r\n\r\n"
    )

    def trickle(handler: BaseHTTPRequestHandler) -> None:
        for byte in answer:
            if RELEASED.wait(short_timeout / 6):
                return
            handler.wfile.write(bytes([byte]))

    with serve(trickle) as (url, _):
        started = time.monotonic()
        passed, lines = run(url)
        elapsed = time.monotonic() - started

    assert not passed
    assert lines == [
        f"FAIL protected: An initialize without a token got no answer within {short_timeout:g} "
        "seconds."
    ]
    # It stopped waiting at the deadline, not when the answer finally ended.
    assert elapsed < short_timeout * 5


def test_a_connection_closed_without_an_answer_fails() -> None:
    def close_without_answering(handler: BaseHTTPRequestHandler) -> None:
        handler.close_connection = True

    with serve(close_without_answering) as (url, _):
        passed, lines = run(url)

    assert not passed
    assert len(lines) == 1
    assert re.fullmatch(
        r"FAIL protected: An initialize without a token got no answer: The connection failed "
        r"before the answer was complete \((RemoteDisconnected|Connection\w+Error)\)\.",
        lines[0],
    ), lines


def test_a_url_the_checks_won_t_use_is_refused_before_any_request() -> None:
    passed, lines = run("http://m-protected.example.test/mcp")
    assert not passed
    assert lines == ["FAIL protected: The URL must use https, or http to this machine."]


def test_the_protected_checks_wait_far_less_than_the_others() -> None:
    assert smoke.PROTECTED_TIMEOUT_SECONDS <= 10
    assert smoke.PROTECTED_TIMEOUT_SECONDS < smoke.TIMEOUT_SECONDS


def test_the_checks_never_print_the_server_s_address(short_timeout: float) -> None:
    with serve(hang) as (url, _):
        _, lines = run(url)
    port = url.rsplit(":", 1)[1].removesuffix("/mcp")
    assert not any("127.0.0.1" in line or port in line for line in lines)
