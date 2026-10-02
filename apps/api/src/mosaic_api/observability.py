import logging
import re
import sys
from typing import Any

import structlog
from azure.monitor.opentelemetry import configure_azure_monitor
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from structlog.tracebacks import ExceptionDictTransformer

from mosaic_api.config import Environment, Settings

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
# the health probes often enough to bury the requests people make, so they aren't recorded. The
# instrumentation searches each request's URL, which it builds without the query string, for these
# patterns, so a probe's exact path matches and no other path does.
HEALTH_PROBES = ("/healthz", "/readyz")
UNRECORDED_URLS = ",".join(rf"^\w+://[^/]+{re.escape(path)}$" for path in HEALTH_PROBES)


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
    """Records each request the app serves in Application Insights, except the health probes.

    The Azure Monitor distro instruments FastAPI by replacing ``fastapi.FastAPI`` with a subclass
    that instruments each app built from it. The API's app is built from the class main imported
    before telemetry was configured, so it was never instrumented and no request was recorded.
    configure_telemetry turns the distro's FastAPI instrumentation off, so this is the only one,
    with these exclusions, however the app is built. Instrumenting an app a second time logs a
    warning and changes nothing, so a request is always one server span.

    No header is recorded, so neither a bearer token nor an API key reaches Application Insights:
    the instrumentation records a header only when this call or an
    OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_* environment variable names it, and none does. It
    never records a body.
    """

    if not settings.applicationinsights_connection_string:
        return
    FastAPIInstrumentor.instrument_app(
        app,
        excluded_urls=UNRECORDED_URLS,
        # Without this, every message the app received or sent would be a span of its own, which
        # Application Insights records as an in-process dependency, three or more for each request.
        exclude_spans=["receive", "send"],
    )
