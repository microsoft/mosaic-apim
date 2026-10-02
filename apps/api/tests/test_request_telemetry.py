from collections.abc import Iterator
from typing import Any

import pytest
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

CONNECTION_STRING = "InstrumentationKey=test"


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
