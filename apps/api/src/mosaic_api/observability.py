import logging
import sys
from typing import Any

import structlog
from azure.monitor.opentelemetry import configure_azure_monitor

from mosaic_api.config import Environment, Settings


def configure_logging(settings: Settings) -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.dict_tracebacks,
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
