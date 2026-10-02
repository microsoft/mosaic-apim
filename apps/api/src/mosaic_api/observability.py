import logging
import sys
from typing import Any

import structlog
from azure.monitor.opentelemetry import configure_azure_monitor
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
    }
    if settings.environment is Environment.TEST:
        options["disable_offline_storage"] = True
    configure_azure_monitor(**options)
