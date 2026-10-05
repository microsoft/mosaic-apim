import contextlib
import logging
import re
import sys
from collections.abc import Callable
from typing import Any

import structlog
from azure.monitor.opentelemetry import configure_azure_monitor
from fastapi import FastAPI
from opentelemetry.instrumentation.asgi import get_host_port_url_tuple
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import Span
from opentelemetry.util.http import redact_url
from structlog.tracebacks import ExceptionDictTransformer

from mosaic_api.config import Environment, Settings

logger = structlog.get_logger()

# A logged exception is rendered without each frame's local variables. A frame on the way to an
# error can hold an API key read from Key Vault or given by an administrator, or a token, and none
# of those may reach a log.
render_exceptions = structlog.processors.ExceptionRenderer(
    ExceptionDictTransformer(show_locals=False)
)

# The Azure SDK logs every HTTP call it makes at INFO, and the Azure Monitor exporter logs every
# batch it uploads. Azure Monitor exports the root logger, so each upload was itself logged and
# exported, and these records buried MOSAIC's own. These loggers log only warnings and errors, or
# less when the log level is stricter. Cosmos DB has its own HTTP logger; the rest of the Azure
# SDK, the exporter's uploads included, uses azure-core's.
QUIET_LOGGERS = (
    "azure.core.pipeline.policies.http_logging_policy",
    "azure.cosmos._cosmos_http_logging_policy",
    "azure.monitor.opentelemetry.exporter",
)

# App Service's health check, the container's HEALTHCHECK and the deployment's smoke checks call
# the health probes often enough to bury the requests people make, and App Service's Always On
# pings the root every five minutes, so none of them is recorded. The instrumentation searches each
# request's URL, which it builds without the query string, for these patterns. Each matches its
# path with or without a trailing slash, so the root's matches the host and an optional slash, and
# no other path matches any of them.
HEALTH_PROBES = ("/healthz", "/readyz")
ROOT = "/"
UNRECORDED_URLS = ",".join(
    rf"^https?://[^/]+{re.escape(path.rstrip('/'))}/?$" for path in (ROOT, *HEALTH_PROBES)
)

# A recorded request keeps its query's parameter names, but each value is replaced with this. No
# route takes a secret in its query, but the directory search's q is whatever an administrator
# typed, often a person's name or email address, and a route added later may take anything.
REDACTED = "REDACTED"
PARAMETER_NAME = re.compile(r"[A-Za-z0-9_.-]+")

# The attributes a server span can carry the query in. The instrumentation sets http.url by
# default, url.query with OTEL_SEMCONV_STABILITY_OPT_IN=http, and both with http/dup. Its
# http.target is the path alone, and it never sets url.full, but other instrumentations put the
# query in both. The Azure Monitor exporter takes a request's URL from url.full or http.url, or
# else builds it from url.path and url.query.
QUERY_ATTRIBUTES = ("http.url", "url.full", "http.target", "url.query")

# The instrumentation's helper that builds a request's URL, before it adds the query. It has no
# annotations, so it's given them here.
request_url: Callable[[dict[str, Any]], tuple[str, int, str]] = get_host_port_url_tuple


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(level, logging.WARNING))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            render_exceptions,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def configure_telemetry(settings: Settings) -> None:
    if not settings.applicationinsights_connection_string:
        return
    options: dict[str, Any] = {
        "connection_string": settings.applicationinsights_connection_string,
        "resource_attributes": {"service.name": "mosaic-api"},
        # instrument_requests instruments the API's app instead. See there.
        "instrumentation_options": {"fastapi": {"enabled": False}},
    }
    if settings.environment is Environment.TEST:
        options["disable_offline_storage"] = True
    configure_azure_monitor(**options)


def instrument_requests(app: FastAPI, settings: Settings) -> None:
    """Records each request the app serves in Application Insights, except the probes and the root.

    The Azure Monitor distro instruments FastAPI by replacing ``fastapi.FastAPI`` with a subclass
    that instruments each app built from it. The API's app is built from the class main imported
    before telemetry was configured, so it was never instrumented and no request was recorded.
    configure_telemetry turns the distro's FastAPI instrumentation off, so this is the only one,
    with these exclusions, however the app is built. Instrumenting an app a second time logs a
    warning and changes nothing, so a request is always one server span.

    No header is recorded, so neither a bearer token nor an API key reaches Application Insights:
    the instrumentation records a header only when this call or an
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_* environment variable names it, and none does. It
    never records a body, and redact_query_values replaces every query value.
    """

    if not settings.applicationinsights_connection_string:
        return
    FastAPIInstrumentor.instrument_app(
        app,
        server_request_hook=redact_query_values,
        excluded_urls=UNRECORDED_URLS,
        # Without this, every message the app received or sent would be a span of its own, which
        # Application Insights records as an in-process dependency, three or more for each request.
        exclude_spans=["receive", "send"],
    )


def redacted_query(query_string: bytes) -> str:
    """The query with each value replaced with REDACTED, or "" if it can't be read safely.

    A field without "=" or with an unusual name may be a value, so the whole query is dropped.
    """

    try:
        fields = query_string.decode("ascii").split("&")
    except UnicodeDecodeError:
        return ""
    names = []
    for field in fields:
        name, equals, _value = field.partition("=")
        if not equals or not PARAMETER_NAME.fullmatch(name):
            return ""
        names.append(name)
    return "&".join(f"{name}={REDACTED}" for name in names)


def redact_query_values(span: Span, scope: dict[str, Any]) -> None:
    """Replaces each query value in a request's server span, as the instrumentation starts it.

    The new values come from the request's raw query rather than the span's URL, where the
    instrumentation has decoded the query, so an encoded "&" in a value would look like another
    parameter. If anything goes wrong, the span records no URL at all rather than one that might
    hold a value, and a warning names only the exception's type.
    """

    if not scope.get("query_string") or not span.is_recording():
        return
    error_type: str | None = None
    try:
        redacted = _redacted_attributes(span, scope) if isinstance(span, ReadableSpan) else None
    except Exception as error:
        redacted = None
        error_type = type(error).__name__
    if redacted is None:
        redacted = dict.fromkeys(QUERY_ATTRIBUTES, "")
    span.set_attributes(redacted)
    if error_type:
        # Only the error's type: its message could hold part of the query, and logs reach
        # Application Insights too. The URL is already gone, and the hook mustn't raise.
        with contextlib.suppress(Exception):
            logger.warning("request_query_redaction_failed", error_type=error_type)


def _redacted_attributes(span: ReadableSpan, scope: dict[str, Any]) -> dict[str, str]:
    carried = span.attributes or {}
    query = redacted_query(scope["query_string"])
    suffix = f"?{query}" if query else ""
    _, _, url = request_url(scope)
    path = scope.get("path", "")
    redacted: dict[str, str] = {}
    for name in ("http.url", "url.full"):
        if name in carried:
            redacted[name] = redact_url(url) + suffix
    if carried.get("http.target", path) != path:
        redacted["http.target"] = path + suffix
    if "url.query" in carried:
        redacted["url.query"] = query
    return redacted
