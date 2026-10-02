from collections.abc import Iterator
from typing import Any

import pytest
import structlog
from azure.monitor.opentelemetry.exporter.export.trace import _exporter as trace_exporter
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api import observability
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.main import create_app
from opentelemetry import trace
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from structlog.testing import capture_logs

CONNECTION_STRING = "InstrumentationKey=test"
SEARCH = "/api/v1/directory/search"
# What an administrator might type into the directory search, encoded as a browser sends it. In
# the URL the instrumentation records, the query is decoded, so its "&" and "=" look like another
# parameter.
TYPED = "someone%40example.com%26x%3Dy"
TYPED_PARTS = ("someone", "example.com", "x=y")


def settings(connection_string: str | None = CONNECTION_STRING, **overrides: Any) -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant-test",
        applicationinsights_connection_string=connection_string,
        **overrides,
    )


class Telemetry:
    """Stands in for Azure Monitor, whose tracer provider exports each span the API ends to
    Application Insights. This one keeps them, and it is in place before any app is created, as
    the distro's is once configure_azure_monitor has run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.exported = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(self.exported))
        # OpenTelemetry lets a process set its global tracer provider only once, so the function
        # that returns it returns this one for the test instead.
        monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
        monkeypatch.setattr(observability, "configure_azure_monitor", self.configure_azure_monitor)
        self.options: list[dict[str, Any]] = []
        self._apps: list[FastAPI] = []

    def configure_azure_monitor(self, **options: Any) -> None:
        self.options.append(options)

    def start(self, connection_string: str | None = CONNECTION_STRING, **overrides: Any) -> FastAPI:
        app = create_app(settings(connection_string, **overrides))
        self._apps.append(app)
        return app

    def spans(self) -> tuple[ReadableSpan, ...]:
        return self.exported.get_finished_spans()

    def undo(self) -> None:
        # Instrumenting an app also traces every background task in the process.
        for app in self._apps:
            FastAPIInstrumentor.uninstrument_app(app)


@pytest.fixture
def telemetry(monkeypatch: pytest.MonkeyPatch) -> Iterator[Telemetry]:
    started = Telemetry(monkeypatch)
    try:
        yield started
    finally:
        started.undo()


def opentelemetry_middleware(app: FastAPI) -> list[OpenTelemetryMiddleware]:
    """The OpenTelemetry middleware in the stack the app would serve its requests through."""

    layers: list[Any] = []
    layer: Any = app.build_middleware_stack()
    while layer is not None and len(layers) < 50:
        layers.append(layer)
        layer = getattr(layer, "app", None)
    return [layer for layer in layers if isinstance(layer, OpenTelemetryMiddleware)]


def exported(span: ReadableSpan) -> dict[str, Any]:
    """What the Azure Monitor exporter would send Application Insights for the span."""

    # The exporter has no public way to convert a span without sending it. If a new version takes
    # the request's URL from somewhere else, these tests say so.
    envelope: dict[str, Any] = trace_exporter._convert_span_to_envelope(span).as_dict()
    return envelope


def recorded_url(span: ReadableSpan) -> str:
    """The URL Application Insights would record for the request."""

    data = exported(span)["data"]
    assert data["baseType"] == "RequestData"
    return str(data["baseData"]["url"])


def recorded(span: ReadableSpan) -> str:
    return repr(dict(span.attributes or {})) + repr(exported(span))


@pytest.mark.parametrize(
    ("path", "route", "status_code"),
    [
        ("/api/v1/principals", "/api/v1/principals", 200),
        ("/api/v1/principals/someone", "/api/v1/principals/{principal_id}", 404),
        # A path that only contains a probe's name is recorded like any other.
        ("/api/v1/principals/healthz", "/api/v1/principals/{principal_id}", 404),
    ],
    ids=["a-list", "a-missing-principal", "a-principal-named-like-a-probe"],
)
def test_a_request_is_one_server_span_with_its_route_and_status_code(
    telemetry: Telemetry, path: str, route: str, status_code: int
) -> None:
    with TestClient(telemetry.start()) as client:
        assert client.get(path).status_code == status_code

    spans = telemetry.spans()
    assert [(span.kind, span.name) for span in spans] == [(SpanKind.SERVER, f"GET {route}")]
    attributes = spans[0].attributes or {}
    assert attributes["http.route"] == route
    assert attributes["http.status_code"] == status_code


# A probe configured with a trailing slash is redirected to the path without one. Neither is
# recorded.
@pytest.mark.parametrize("probe", ["/healthz", "/readyz", "/readyz?probe=1", "/healthz/"])
def test_the_health_probes_are_not_recorded(telemetry: Telemetry, probe: str) -> None:
    with TestClient(telemetry.start()) as client:
        assert client.get(probe).status_code == 200
        assert telemetry.spans() == ()
        client.get("/api/v1/principals")

    assert [span.name for span in telemetry.spans()] == ["GET /api/v1/principals"]


def test_no_header_or_body_is_recorded(telemetry: Telemetry) -> None:
    token = "a-bearer-token-in-this-test"
    key = "an-api-key-in-this-test"
    correlation = "a-correlation-id-the-api-echoes"
    label = "a-label-the-api-echoes"
    with TestClient(telemetry.start()) as client:
        created = client.post(
            "/api/v1/principals",
            headers={
                "Authorization": f"Bearer {token}",
                "api-key": key,
                "X-Correlation-ID": correlation,
            },
            json={"objectId": "principal-in-this-test", "kind": "servicePrincipal", "label": label},
        )
    assert created.status_code == 201
    assert created.headers["X-Correlation-ID"] == correlation
    assert created.json()["label"] == label

    spans = telemetry.spans()
    assert [span.kind for span in spans] == [SpanKind.SERVER]
    attributes = dict(spans[0].attributes or {})
    assert [
        name
        for name in attributes
        if name.startswith(("http.request.header.", "http.response.header."))
    ] == []
    recorded = repr(attributes)
    for value in (token, key, correlation, label):
        assert value not in recorded


def test_what_someone_types_into_a_directory_search_is_recorded_nowhere(
    telemetry: Telemetry,
) -> None:
    with TestClient(telemetry.start()) as client:
        client.get(f"{SEARCH}?kind=user&q={TYPED}")

    spans = telemetry.spans()
    assert [span.kind for span in spans] == [SpanKind.SERVER]
    for part in TYPED_PARTS:
        assert part not in recorded(spans[0])


def test_each_query_value_is_redacted_and_each_name_kept(telemetry: Telemetry) -> None:
    with TestClient(telemetry.start()) as client:
        client.get(f"{SEARCH}?kind=user&q={TYPED}&limit=5")

    (span,) = telemetry.spans()
    url = f"http://testserver{SEARCH}?kind=REDACTED&q=REDACTED&limit=REDACTED"
    # The exporter takes the request's URL from http.url, as the instrumentation sets it by default.
    assert (span.attributes or {})["http.url"] == url
    assert recorded_url(span) == url


def test_a_request_without_a_query_is_recorded_as_it_was(telemetry: Telemetry) -> None:
    with TestClient(telemetry.start()) as client:
        client.get("/api/v1/principals")

    (span,) = telemetry.spans()
    attributes = span.attributes or {}
    assert attributes["http.url"] == "http://testserver/api/v1/principals"
    assert attributes["http.target"] == "/api/v1/principals"
    assert recorded_url(span) == "http://testserver/api/v1/principals"


@pytest.mark.parametrize(
    "query",
    [TYPED, f"q={TYPED}&&kind=user", f"{TYPED}=1"],
    ids=["a-value-alone", "an-empty-field", "an-encoded-name"],
)
def test_a_query_that_cant_be_read_safely_is_left_out(telemetry: Telemetry, query: str) -> None:
    with TestClient(telemetry.start()) as client:
        client.get(f"{SEARCH}?{query}")

    (span,) = telemetry.spans()
    assert (span.attributes or {})["http.url"] == f"http://testserver{SEARCH}"
    assert recorded_url(span) == f"http://testserver{SEARCH}"


def test_if_redaction_fails_no_url_is_recorded(
    telemetry: Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_scope: dict[str, Any]) -> tuple[str, int, str]:
        raise ValueError("a scope the instrumentation didn't expect")

    monkeypatch.setattr(observability, "request_url", fail)
    # create_app configures structlog afresh, and a logger keeps the configuration it first logged
    # with, so the module's logger is swapped for one that hasn't logged yet.
    monkeypatch.setattr(observability, "logger", structlog.get_logger())
    with TestClient(telemetry.start()) as client, capture_logs() as structured:
        client.get(f"{SEARCH}?kind=user&q={TYPED}")

    (span,) = telemetry.spans()
    assert (span.attributes or {})["http.url"] == ""
    assert recorded_url(span) == ""
    # The hook raised nothing for the instrumentation to record on the span.
    assert not span.events
    # The warning names the error's type alone, so neither its message nor the query is logged.
    assert structured == [
        {
            "event": "request_query_redaction_failed",
            "log_level": "warning",
            "error_type": "ValueError",
        }
    ]
    for part in TYPED_PARTS:
        assert part not in recorded(span)


def test_if_the_warning_cant_be_logged_the_hook_still_raises_nothing(
    telemetry: Telemetry, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenLogger:
        def warning(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("the log pipeline failed")

    def fail(_scope: dict[str, Any]) -> tuple[str, int, str]:
        raise ValueError("a scope the instrumentation didn't expect")

    monkeypatch.setattr(observability, "request_url", fail)
    monkeypatch.setattr(observability, "logger", BrokenLogger())
    with TestClient(telemetry.start()) as client:
        client.get(f"{SEARCH}?kind=user&q={TYPED}")

    (span,) = telemetry.spans()
    assert (span.attributes or {})["http.url"] == ""
    assert not span.events


@pytest.mark.parametrize(
    ("carried", "redacted", "url"),
    [
        # As the instrumentation records a request with OTEL_SEMCONV_STABILITY_OPT_IN=http. The
        # exporter builds the URL from url.path and url.query.
        (
            {
                "url.scheme": "http",
                "server.address": "testserver",
                "server.port": 80,
                "url.path": SEARCH,
                "url.query": f"kind=user&q={TYPED}",
            },
            {"url.query": "kind=REDACTED&q=REDACTED"},
            f"http://testserver:80{SEARCH}?kind=REDACTED&q=REDACTED",
        ),
        # As other instrumentations record one, with the query in url.full and http.target.
        (
            {
                "url.full": f"http://testserver{SEARCH}?kind=user&q=someone@example.com&x=y",
                "http.target": f"{SEARCH}?kind=user&q={TYPED}",
            },
            {
                "url.full": f"http://testserver{SEARCH}?kind=REDACTED&q=REDACTED",
                "http.target": f"{SEARCH}?kind=REDACTED&q=REDACTED",
            },
            f"http://testserver{SEARCH}?kind=REDACTED&q=REDACTED",
        ),
    ],
    ids=["stable-semantic-conventions", "url-full-and-a-full-target"],
)
def test_the_query_is_redacted_in_every_attribute_that_carries_it(
    carried: dict[str, Any], redacted: dict[str, str], url: str
) -> None:
    spans = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(spans))
    scope = {
        "type": "http",
        "scheme": "http",
        "server": ("testserver", 80),
        "headers": [(b"host", b"testserver")],
        "path": SEARCH,
        "query_string": f"kind=user&q={TYPED}".encode(),
    }
    with provider.get_tracer(__name__).start_as_current_span(
        f"GET {SEARCH}",
        kind=SpanKind.SERVER,
        attributes={"http.request.method": "GET", **carried},
    ) as span:
        observability.redact_query_values(span, scope)

    (ended,) = spans.get_finished_spans()
    attributes = ended.attributes or {}
    assert {name: attributes[name] for name in redacted} == redacted
    assert recorded_url(ended) == url
    for part in TYPED_PARTS:
        assert part not in recorded(ended)


@pytest.mark.parametrize(
    ("query_string", "redacted"),
    [
        (b"kind=user&q=someone%40example.com%26x%3Dy", "kind=REDACTED&q=REDACTED"),
        (b"q=one&q=two", "q=REDACTED&q=REDACTED"),
        (b"q=", "q=REDACTED"),
        (b"q=someone;x=y", "q=REDACTED"),
        (b"someone", ""),
        (b"q=someone&&kind=user", ""),
        (b"q%3Dsomeone=1", ""),
        (b"q=someone\xff", ""),
    ],
)
def test_redacted_query(query_string: bytes, redacted: str) -> None:
    assert observability.redacted_query(query_string) == redacted


def test_a_request_the_cors_middleware_answers_is_recorded(telemetry: Telemetry) -> None:
    origin = "http://localhost:5173"
    # create_app adds the CORS middleware after it instruments the app.
    with TestClient(telemetry.start(cors_origins=[origin])) as client:
        preflight = client.options(
            "/api/v1/principals",
            headers={"Origin": origin, "Access-Control-Request-Method": "POST"},
        )
    assert preflight.status_code == 200
    assert preflight.headers["Access-Control-Allow-Origin"] == origin

    assert [(span.kind, span.name) for span in telemetry.spans()] == [
        (SpanKind.SERVER, "OPTIONS /api/v1/principals")
    ]


def test_without_a_connection_string_the_app_is_not_instrumented(telemetry: Telemetry) -> None:
    app = telemetry.start(connection_string=None)

    assert telemetry.options == []
    assert opentelemetry_middleware(app) == []
    with TestClient(app) as client:
        assert client.get("/api/v1/principals").status_code == 200
    assert telemetry.spans() == ()


def test_each_app_is_instrumented_once_however_often_it_is_asked(telemetry: Telemetry) -> None:
    first, second = telemetry.start(), telemetry.start()
    observability.instrument_requests(second, settings())

    for app in (first, second):
        assert len(opentelemetry_middleware(app)) == 1
        telemetry.exported.clear()
        with TestClient(app) as client:
            client.get("/api/v1/principals")
        assert [span.kind for span in telemetry.spans()] == [SpanKind.SERVER]


def test_the_distro_leaves_fastapi_to_the_api(telemetry: Telemetry) -> None:
    telemetry.start()

    # The distro's own FastAPI instrumentation replaces fastapi.FastAPI. An app built from that
    # class would be instrumented the distro's way, health probes and all, before the API could.
    assert [options["instrumentation_options"] for options in telemetry.options] == [
        {"fastapi": {"enabled": False}}
    ]
