import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from azure.core import exceptions as core_exceptions
from azure.core.pipeline import PipelineContext, PipelineRequest, PipelineResponse
from azure.core.pipeline.policies import HttpLoggingPolicy
from azure.core.rest import HttpRequest
from azure.cosmos._cosmos_http_logging_policy import CosmosHttpLoggingPolicy
from azure.identity.aio._internal import decorators as identity_decorators
from azure.monitor.opentelemetry._utils import configurations as distro_configurations
from azure.monitor.opentelemetry.exporter.export import _base as exporter
from mosaic_api import observability
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings

MOSAIC = "mosaic_api.services.example"


class Exported(logging.Handler):
    """Keeps what the handler Azure Monitor adds would send to Application Insights."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


class Startup:
    """Configures logging and telemetry as the API does when it starts, with Azure Monitor
    replaced by a handler that keeps what it would export."""

    def __init__(self) -> None:
        self.exported = Exported()
        self._added: list[tuple[logging.Logger, logging.Handler]] = []

    def run(self, log_level: str = "INFO") -> None:
        settings = Settings(
            environment=Environment.TEST,
            auth_mode=AuthMode.LOCAL,
            repository_backend=RepositoryBackend.MEMORY,
            tenant_id="tenant-test",
            applicationinsights_connection_string="InstrumentationKey=test",
            log_level=log_level,
        )
        root = logging.getLogger()
        # The API starts with no handler on the root logger, and basicConfig does nothing to a root
        # logger that has one. Pytest has added its own, so they wait while logging is configured.
        pytest_handlers = root.handlers[:]
        for handler in pytest_handlers:
            root.removeHandler(handler)
        try:
            observability.configure_logging(settings)
        finally:
            self._added.extend((root, handler) for handler in root.handlers)
            for handler in pytest_handlers:
                root.addHandler(handler)
        observability.configure_telemetry(settings)

    def configure_azure_monitor(self, **options: Any) -> None:
        # Like the distro, put the exporting handler on the logger named by logger_name, the root
        # logger by default, so it exports every record that reaches the root.
        logger = logging.getLogger(options.get("logger_name", ""))
        logger.addHandler(self.exported)
        self._added.append((logger, self.exported))

    def undo(self) -> None:
        for logger, handler in self._added:
            logger.removeHandler(handler)
            handler.close()


@pytest.fixture
def startup(monkeypatch: pytest.MonkeyPatch) -> Iterator[Startup]:
    root = logging.getLogger()
    level = root.level
    levels = {name: logging.getLogger(name).level for name in observability.QUIET_LOGGERS}
    structlog_config = structlog.get_config()
    started = Startup()
    monkeypatch.setattr(observability, "configure_azure_monitor", started.configure_azure_monitor)
    try:
        yield started
    finally:
        started.undo()
        root.setLevel(level)
        for name, quiet_level in levels.items():
            logging.getLogger(name).setLevel(quiet_level)
        structlog.configure(**structlog_config)


def call_through(policy: HttpLoggingPolicy) -> None:
    """Runs the policy over one call and its response, as an Azure SDK client's pipeline does."""

    request: PipelineRequest[Any] = PipelineRequest(
        HttpRequest("GET", "https://example.invalid/dbs/mosaic"), PipelineContext(None)
    )
    response: Any = SimpleNamespace(status_code=200, headers={"x-ms-request-id": "request-1"})
    policy.on_request(request)
    policy.on_response(request, PipelineResponse(request.http_request, response, request.context))


def log_an_upload() -> None:
    exporter.logger.info("Transmission succeeded: Item received: %s. Items accepted: %s", 3, 3)


def sdk_loggers() -> list[logging.Logger]:
    return [HttpLoggingPolicy().logger, CosmosHttpLoggingPolicy().logger, exporter.logger]


def test_the_azure_sdk_logs_no_call_and_the_exporter_no_upload(
    startup: Startup, capsys: pytest.CaptureFixture[str]
) -> None:
    startup.run()

    call_through(HttpLoggingPolicy())
    call_through(CosmosHttpLoggingPolicy())
    log_an_upload()

    assert startup.exported.messages() == []
    assert capsys.readouterr().out == ""


def test_mosaic_logs_still_reach_application_insights_and_stdout(
    startup: Startup, capsys: pytest.CaptureFixture[str]
) -> None:
    startup.run()

    logging.getLogger(MOSAIC).info("a record from MOSAIC")
    structlog.get_logger(MOSAIC).info("rollup_finished", gateways=2)

    first, second = startup.exported.messages()
    assert first == "a record from MOSAIC"
    assert json.loads(second)["event"] == "rollup_finished"
    printed = capsys.readouterr().out
    assert "a record from MOSAIC" in printed
    assert "rollup_finished" in printed


@pytest.mark.parametrize("logger", sdk_loggers(), ids=["azure-core", "cosmos", "exporter"])
def test_their_warnings_and_errors_still_reach_application_insights(
    startup: Startup, logger: logging.Logger
) -> None:
    startup.run()

    logger.warning("the service throttled the call")
    logger.error("the call failed")

    assert [(record.name, record.levelno) for record in startup.exported.records] == [
        (logger.name, logging.WARNING),
        (logger.name, logging.ERROR),
    ]


# Loggers beside the quiet ones, each with an INFO record of its own in the locked versions. The
# first records each token MOSAIC's managed identity credential gets.
@pytest.mark.parametrize(
    ("logger", "message"),
    [
        (identity_decorators._LOGGER, "ManagedIdentityCredential.get_token_info succeeded"),
        (core_exceptions._LOGGER, "Received error message was not valid OdataV4 format."),
        (distro_configurations._logger, "Using sampling ratio: 0.5"),
    ],
    ids=["azure-identity", "azure-core", "azure-monitor-distro"],
)
def test_other_azure_sdk_info_records_still_reach_application_insights(
    startup: Startup, logger: logging.Logger, message: str
) -> None:
    startup.run()

    logger.info(message)

    assert [(record.name, record.levelno) for record in startup.exported.records] == [
        (logger.name, logging.INFO)
    ]
    assert startup.exported.messages() == [message]


def test_a_stricter_log_level_holds_them_too(startup: Startup) -> None:
    startup.run("ERROR")

    for logger in sdk_loggers():
        logger.warning("the service throttled the call")
    logging.getLogger(MOSAIC).warning("a warning from MOSAIC")
    logging.getLogger(MOSAIC).error("an error from MOSAIC")

    assert startup.exported.messages() == ["an error from MOSAIC"]


def test_debug_does_not_bring_back_each_call_or_upload(startup: Startup) -> None:
    startup.run("DEBUG")

    call_through(HttpLoggingPolicy())
    call_through(CosmosHttpLoggingPolicy())
    log_an_upload()
    logging.getLogger(MOSAIC).debug("a debug record from MOSAIC")

    assert startup.exported.messages() == ["a debug record from MOSAIC"]
