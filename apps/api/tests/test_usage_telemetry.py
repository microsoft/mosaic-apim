from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from loganalytics_double import FakeLogs, GatewayCall
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings, UsageSourceMode
from mosaic_api.domain import (
    ApimResourceId,
    ApiShape,
    AuditEvent,
    Gateway,
    GatewayAccess,
    ManagementMode,
    McpServer,
    ModelApi,
    ModelProvider,
    Publication,
    new_id,
)
from mosaic_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamError,
)
from mosaic_api.integrations.apim.diagnostics import (
    AZURE_MONITOR,
    GATEWAY_LLM_LOGS,
    GATEWAY_LOGS,
    LogRouting,
    api_diagnostic_gaps,
    api_diagnostic_payload,
    azure_monitor_logger_payload,
    diagnostic_setting_command,
    evaluate_diagnostic_settings,
    monitoring_reader_command,
)
from mosaic_api.main import create_app
from mosaic_api.repositories.memory_entitlements import InMemoryEntitlementRepository
from mosaic_api.repositories.memory_gateway import InMemoryGatewayRepository
from mosaic_api.repositories.memory_usage import InMemoryUsageRollupRepository
from mosaic_api.services.analytics import AnalyticsService
from mosaic_api.services.directory import Actor
from mosaic_api.services.telemetry import (
    GatewayTelemetry,
    TelemetryCheck,
    TelemetryService,
    governed_apis,
)
from mosaic_api.services.usage import usage_freshness
from mosaic_api.services.usage_rollup import UsageRollupService
from mosaic_api.usage_telemetry import UsageRollupState, usage_rollup_state_id

TENANT = "tenant-test"
ACTOR_ID = "admin-oid"
GATEWAY_ID = "gateway-prod"
OTHER_GATEWAY_ID = "gateway-other"
NOW = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)
RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
    "/providers/Microsoft.ApiManagement/service/gateway-prod"
)
LOGGER_ID = f"{RESOURCE_ID}/loggers/{AZURE_MONITOR}"
WORKSPACE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
    "/providers/Microsoft.OperationalInsights/workspaces/mosaic-logs"
)

JsonObject = dict[str, Any]


def _audit(kind: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type=kind,
        resource_id=kind,
        actor_object_id="tester",
    )


def _actor() -> Actor:
    return Actor(object_id=ACTOR_ID, tenant_id=TENANT)


def _ready_setting(*, llm: bool = True) -> JsonObject:
    logs = [{"category": GATEWAY_LOGS, "enabled": True}]
    if llm:
        logs.append({"category": GATEWAY_LLM_LOGS, "enabled": True})
    return {
        "name": "existing",
        "properties": {
            "workspaceId": WORKSPACE_ID,
            "logs": logs,
            "metrics": [{"category": "AllMetrics", "enabled": True}],
            "logAnalyticsDestinationType": "Dedicated",
        },
    }


def _current_rollup() -> UsageRollupState:
    return UsageRollupState(
        id=usage_rollup_state_id(TENANT, GATEWAY_ID),
        tenant_id=TENANT,
        gateway_id=GATEWAY_ID,
        last_run_at=NOW - timedelta(minutes=1),
        last_success_at=NOW - timedelta(minutes=1),
        last_duration_ms=123,
        queried_through=NOW - timedelta(minutes=1),
    )


def _check(report: GatewayTelemetry, check_id: str) -> TelemetryCheck:
    return next(check for check in report.checks if check.id == check_id)


class _TelemetryApim:
    def __init__(self) -> None:
        self.logger: JsonObject | None = azure_monitor_logger_payload()
        self.settings: list[JsonObject] = [_ready_setting()]
        self.diagnostics: dict[str, JsonObject | None] = {}
        # The service's azuremonitor diagnostic, set for All APIs.
        self.service_diagnostic: JsonObject | None = None
        self.logger_error: DomainError | None = None
        self.settings_error: DomainError | None = None
        self.diagnostic_errors: dict[str, DomainError] = {}
        self.diagnostic_reads: list[str] = []

    async def get_logger(self, name: str) -> JsonObject | None:
        assert name == AZURE_MONITOR
        if self.logger_error is not None:
            raise self.logger_error
        return self.logger

    async def list_diagnostic_settings(self) -> list[JsonObject]:
        if self.settings_error is not None:
            raise self.settings_error
        return self.settings

    async def get_api_diagnostic(self, api_name: str, name: str) -> JsonObject | None:
        assert name == AZURE_MONITOR
        self.diagnostic_reads.append(api_name)
        error = self.diagnostic_errors.get(api_name)
        if error is not None:
            raise error
        return self.diagnostics.get(api_name)

    async def get_diagnostic(self, name: str) -> JsonObject | None:
        assert name == AZURE_MONITOR
        return self.service_diagnostic


@dataclass
class _TelemetryWriter:
    apim: _TelemetryApim
    resource: ApimResourceId
    logger_writes: int = 0
    diagnostic_writes: list[tuple[str, JsonObject]] = field(default_factory=list)
    logger_error: DomainError | None = None
    write_errors: dict[str, DomainError] = field(default_factory=dict)
    write_once_errors: dict[str, DomainError] = field(default_factory=dict)

    def resource_id(self, segment: str) -> str:
        return f"{self.resource.canonical}/{segment}"

    async def put_azure_monitor_logger(self) -> JsonObject | None:
        if self.logger_error is not None:
            raise self.logger_error
        self.logger_writes += 1
        self.apim.logger = azure_monitor_logger_payload()
        return self.apim.logger

    async def put_api_diagnostic(self, api_name: str, payload: JsonObject) -> JsonObject | None:
        error = self.write_once_errors.pop(api_name, None) or self.write_errors.get(api_name)
        if error is not None:
            raise error
        self.diagnostic_writes.append((api_name, payload))
        self.apim.diagnostics[api_name] = payload
        return payload


@dataclass
class _TelemetryHarness:
    gateways: InMemoryGatewayRepository
    rollups: InMemoryUsageRollupRepository
    entitlements: InMemoryEntitlementRepository
    apim: _TelemetryApim
    writer: _TelemetryWriter
    logs: FakeLogs
    service: TelemetryService


def _service_harness(*, logs: FakeLogs | None = None) -> _TelemetryHarness:
    gateways = InMemoryGatewayRepository()
    rollups = InMemoryUsageRollupRepository()
    entitlements = InMemoryEntitlementRepository()
    apim = _TelemetryApim()
    resource = ApimResourceId.parse(RESOURCE_ID)
    writer = _TelemetryWriter(apim=apim, resource=resource)
    log_query = logs or FakeLogs(
        calls=[
            GatewayCall(
                time=NOW - timedelta(minutes=5),
                api="chat",
                grant="grant-chat",
                prompt_tokens=10,
                completion_tokens=5,
            )
        ],
        clock=lambda: NOW,
    )
    service = TelemetryService(
        gateway_repository=gateways,
        rollup_repository=rollups,
        entitlement_repository=entitlements,
        client_factory=lambda _resource: apim,
        writer_factory=lambda _resource: writer,
        logs=log_query,
        rollups_enabled=True,
        interval_seconds=900,
        principal_id="mosaic-principal",
        clock=lambda: NOW,
    )
    return _TelemetryHarness(gateways, rollups, entitlements, apim, writer, log_query, service)


async def _seed_gateway(
    harness: _TelemetryHarness,
    *,
    management_mode: ManagementMode = ManagementMode.MANAGE,
    can_write: bool = True,
) -> Gateway:
    gateway = Gateway(
        id=GATEWAY_ID,
        tenant_id=TENANT,
        name="Production gateway",
        azure_resource_id=RESOURCE_ID,
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_group="rg",
        service_name="gateway-prod",
        management_mode=management_mode,
        access=GatewayAccess(can_read=True, can_write=can_write),
    )
    await harness.gateways.save_gateway(gateway, _audit("gateway"))
    return gateway


async def _seed_governed_apis(harness: _TelemetryHarness) -> None:
    publication = Publication(
        id="publication-chat",
        tenant_id=TENANT,
        gateway_id=GATEWAY_ID,
        model_endpoint_id="endpoint-aoai",
        deployment_name="chat-prod",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Chat",
        api_name="chat",
        api_path="chat",
        backend_name="chat",
        fragment_name="chat",
        product_name="chat",
        subscription_name="chat",
        shape_version="v1",
        api_shape=ApiShape.AZURE_OPENAI,
    )
    await harness.gateways.save_publication(publication, _audit("publication"))
    await harness.gateways.save_model_api(
        ModelApi(
            id="model-api-chat",
            tenant_id=TENANT,
            gateway_id=GATEWAY_ID,
            api_name="chat",
            display_name="Chat",
            path="chat",
            publication_id=publication.id,
        ),
        _audit("model-api"),
    )
    await harness.gateways.save_model_api(
        ModelApi(
            id="model-api-mini",
            tenant_id=TENANT,
            gateway_id=GATEWAY_ID,
            api_name="mini",
            display_name="Summaries",
            path="mini",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("model-api"),
    )
    await harness.gateways.save_mcp_server(
        McpServer(
            id="mcp-tickets",
            tenant_id=TENANT,
            gateway_id=GATEWAY_ID,
            api_name="tickets",
            display_name="Ticket tools",
            path="tickets",
            publication_id="mcp-publication-tickets",
        ),
        _audit("mcp"),
    )
    await harness.gateways.save_model_api(
        ModelApi(
            id="model-api-payroll",
            tenant_id=TENANT,
            gateway_id=OTHER_GATEWAY_ID,
            api_name="payroll",
            display_name="Payroll",
            path="payroll",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("model-api"),
    )


async def _seed_ready_rollup(harness: _TelemetryHarness) -> None:
    await harness.rollups.update_rollup_state(TENANT, GATEWAY_ID, lambda _: _current_rollup())


def test_azure_monitor_logger_payload_is_buffered_azure_monitor() -> None:
    assert azure_monitor_logger_payload() == {
        "properties": {"loggerType": "azureMonitor", "isBuffered": True}
    }


def test_model_api_diagnostic_payload_logs_traces_tokens_and_no_content() -> None:
    payload = api_diagnostic_payload(LOGGER_ID, llm=True)

    assert payload == {
        "properties": {
            "loggerId": LOGGER_ID,
            "verbosity": "information",
            "logClientIp": False,
            "sampling": {"samplingType": "fixed", "percentage": 100},
            "frontend": {
                "request": {"headers": [], "body": {"bytes": 0}},
                "response": {"headers": [], "body": {"bytes": 0}},
            },
            "backend": {
                "request": {"headers": [], "body": {"bytes": 0}},
                "response": {"headers": [], "body": {"bytes": 0}},
            },
            "largeLanguageModel": {"logs": "enabled"},
        }
    }


def test_mcp_api_diagnostic_payload_omits_llm_logging() -> None:
    payload = api_diagnostic_payload(LOGGER_ID, llm=False)

    assert "largeLanguageModel" not in payload["properties"]
    assert payload["properties"]["loggerId"] == LOGGER_ID
    assert payload["properties"]["logClientIp"] is False


def test_missing_or_malformed_api_diagnostic_is_missing() -> None:
    assert api_diagnostic_gaps(None, llm=True) == ["missing"]
    assert api_diagnostic_gaps({"properties": []}, llm=True) == ["missing"]


def test_compliant_api_diagnostic_has_no_gaps() -> None:
    diagnostic = api_diagnostic_payload(LOGGER_ID.upper(), llm=True)
    diagnostic["properties"]["verbosity"] = "Verbose"

    assert api_diagnostic_gaps(diagnostic, llm=True) == []


def test_outdated_api_diagnostic_reports_each_noncompliant_field() -> None:
    diagnostic = {
        "properties": {
            "loggerId": f"{RESOURCE_ID}/loggers/applicationinsights",
            "verbosity": "error",
            "sampling": {"samplingType": "fixed", "percentage": 50},
        }
    }

    assert api_diagnostic_gaps(diagnostic, llm=True) == [
        "logger",
        "verbosity",
        "sampling",
        "llmLogs",
    ]


def test_diagnostic_without_numeric_full_sampling_is_outdated() -> None:
    diagnostic = api_diagnostic_payload(LOGGER_ID, llm=False)
    diagnostic["properties"]["sampling"] = {}

    assert api_diagnostic_gaps(diagnostic, llm=False) == ["sampling"]


def test_diagnostic_setting_evaluation_reports_missing_workspace() -> None:
    routing = evaluate_diagnostic_settings([])

    assert routing == LogRouting(ready=False, problem="missing")


def test_diagnostic_setting_evaluation_prefers_nearest_compliant_setting() -> None:
    legacy = _ready_setting()
    legacy["name"] = "legacy"
    legacy["properties"].pop("logAnalyticsDestinationType")
    without_llm = _ready_setting(llm=False)
    without_llm["name"] = "gateway-only"

    routing = evaluate_diagnostic_settings([legacy, without_llm])

    assert routing.setting_name == "gateway-only"
    assert routing.workspace_id == WORKSPACE_ID
    assert routing.problem == "noLlmLogs"
    assert not routing.ready


def test_diagnostic_setting_command_creates_resource_specific_logs() -> None:
    command = diagnostic_setting_command(RESOURCE_ID, LogRouting(ready=False, problem="missing"))

    assert command == (
        "az monitor diagnostic-settings create --name mosaic-gateway-logs "
        f"--resource {RESOURCE_ID} --workspace <log-analytics-workspace-resource-id> "
        "--export-to-resource-specific true --logs "
        "'[{\"category\":\"GatewayLogs\",\"enabled\":true},"
        "{\"category\":\"GatewayLlmLogs\",\"enabled\":true}]'"
    )


def test_diagnostic_setting_command_preserves_existing_logs_and_metrics() -> None:
    routing = LogRouting(
        ready=False,
        problem="noLlmLogs",
        setting_name="existing",
        workspace_id=WORKSPACE_ID,
        logs=[{"categoryGroup": "audit", "enabled": True}],
        metrics=True,
    )

    command = diagnostic_setting_command(RESOURCE_ID, routing)

    assert f"--name existing --resource {RESOURCE_ID} --workspace {WORKSPACE_ID}" in command
    assert (
        "--logs '[{\"categoryGroup\":\"audit\",\"enabled\":true},"
        "{\"category\":\"GatewayLogs\",\"enabled\":true},"
        "{\"category\":\"GatewayLlmLogs\",\"enabled\":true}]'"
    ) in command
    assert '--metrics \'[{"category":"AllMetrics","enabled":true}]\'' in command


def test_monitoring_reader_command_quotes_the_role_and_uses_the_principal() -> None:
    assert monitoring_reader_command(RESOURCE_ID, "principal-oid") == (
        "az role assignment create --assignee-object-id principal-oid "
        '--assignee-principal-type ServicePrincipal --role "Monitoring Reader" '
        f"--scope {RESOURCE_ID}"
    )
    assert "<mosaic-api-principal-id>" in monitoring_reader_command(RESOURCE_ID, None)


async def test_status_reports_missing_logger_and_missing_api_diagnostics() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    await _seed_ready_rollup(harness)
    harness.apim.logger = None

    report = await harness.service.status(_actor(), GATEWAY_ID)

    assert report.ready is False
    assert report.can_enable is True
    assert _check(report, "logger").status == "error"
    assert _check(report, "logger").detail.endswith("Enable API diagnostics to create it.")
    assert _check(report, "apiDiagnostics").status == "error"
    assert {api.api_name: api.gaps for api in report.apis} == {
        "chat": ["missing"],
        "mini": ["missing"],
        "tickets": ["missing"],
    }


async def test_status_is_ready_when_every_telemetry_check_is_ok() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    await _seed_ready_rollup(harness)
    harness.apim.diagnostics = {
        "chat": api_diagnostic_payload(LOGGER_ID, llm=True),
        "mini": api_diagnostic_payload(LOGGER_ID, llm=True),
        "tickets": api_diagnostic_payload(LOGGER_ID, llm=False),
        "payroll": api_diagnostic_payload(LOGGER_ID, llm=True),
    }

    report = await harness.service.status(_actor(), GATEWAY_ID)

    assert report.ready is True
    assert report.workspace_id == WORKSPACE_ID
    assert [api.api_name for api in report.apis] == ["chat", "mini", "tickets"]
    assert all(check.status == "ok" for check in report.checks)
    assert report.probe is not None
    assert (report.probe.gateway_rows, report.probe.traced_rows, report.probe.llm_rows) == (
        1,
        1,
        1,
    )


@pytest.mark.parametrize(("minutes", "expected"), [(60, "ok"), (61, "warning")])
async def test_rollup_check_is_behind_when_usage_freshness_is_delayed(
    minutes: int, expected: str
) -> None:
    # Two 15-minute intervals plus Log Analytics' 30-minute ingestion allowance, as the portal's
    # and Analytics' freshness use, so the check and the freshness never disagree.
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    succeeded = NOW - timedelta(minutes=minutes)
    await harness.rollups.update_rollup_state(
        TENANT,
        GATEWAY_ID,
        lambda _: _current_rollup().model_copy(
            update={
                "last_run_at": succeeded,
                "last_success_at": succeeded,
                "queried_through": succeeded,
            }
        ),
    )

    report = await harness.service.status(_actor(), GATEWAY_ID)
    freshness = usage_freshness(
        [_current_rollup().model_copy(update={"last_success_at": succeeded})],
        gateways=1,
        now=NOW,
        interval=timedelta(seconds=900),
    )

    assert _check(report, "rollups").status == expected
    assert freshness.status == ("current" if expected == "ok" else "delayed")


async def test_an_api_without_its_own_diagnostic_is_judged_by_the_all_apis_setting() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    await _seed_ready_rollup(harness)
    # All APIs log every call, but without the LLM log. Only chat overrides it.
    harness.apim.service_diagnostic = api_diagnostic_payload(LOGGER_ID, llm=False)
    harness.apim.diagnostics = {"chat": api_diagnostic_payload(LOGGER_ID, llm=True)}
    harness.apim.diagnostic_errors["mini"] = UpstreamAuthorizationError("forbidden")

    report = await harness.service.status(_actor(), GATEWAY_ID)

    assert {api.api_name: (api.all_apis, api.gaps) for api in report.apis} == {
        "chat": (False, []),
        # An override MOSAIC couldn't read may exist, so the All APIs setting doesn't vouch for it.
        "mini": (False, ["missing"]),
        "tickets": (True, []),
    }
    check = _check(report, "apiDiagnostics")
    assert (check.status, check.detail.startswith("1 of 3 governed APIs")) == ("warning", True)

    harness.apim.diagnostic_errors.clear()
    report = await harness.service.status(_actor(), GATEWAY_ID)
    assert {api.api_name: api.gaps for api in report.apis}["mini"] == ["llmLogs"]


async def test_status_shows_workspace_command_when_resource_logs_are_not_linked() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    await _seed_ready_rollup(harness)
    harness.apim.settings = []

    report = await harness.service.status(_actor(), GATEWAY_ID)
    routing = _check(report, "logRouting")

    assert routing.status == "error"
    assert routing.command is not None
    assert routing.command.startswith("az monitor diagnostic-settings create")
    assert "<log-analytics-workspace-resource-id>" in routing.command


async def test_status_maps_apim_read_forbidden_to_unknown_checks_with_remediation() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    harness.apim.logger_error = UpstreamAuthorizationError("forbidden")
    harness.apim.settings_error = UpstreamAuthorizationError("forbidden")

    report = await harness.service.status(_actor(), GATEWAY_ID)

    assert _check(report, "logger").status == "unknown"
    routing = _check(report, "logRouting")
    assert routing.status == "unknown"
    assert "Monitoring Reader" in (routing.command or "")
    assert "can't read the gateway's diagnostic settings" in routing.detail


async def test_status_maps_log_access_failures_to_a_helpful_command() -> None:
    logs = FakeLogs(clock=lambda: NOW)
    logs.failures[RESOURCE_ID] = DomainError("query failed", details={"reason": "workspace gone"})
    harness = _service_harness(logs=logs)
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)

    report = await harness.service.status(_actor(), GATEWAY_ID)

    access = _check(report, "logAccess")
    assert access.status == "unknown"
    assert access.detail == "query failed: workspace gone"


async def test_status_unknown_gateway_raises_not_found() -> None:
    harness = _service_harness()

    with pytest.raises(NotFoundError):
        await harness.service.status(_actor(), "missing")


async def test_status_for_observed_gateway_cannot_be_enabled_by_mosaic() -> None:
    logs = FakeLogs(
        calls=[GatewayCall(time=NOW - timedelta(minutes=5), api="chat", traced=False)],
        clock=lambda: NOW,
    )
    harness = _service_harness(logs=logs)
    await _seed_gateway(harness, management_mode=ManagementMode.OBSERVE, can_write=False)
    harness.apim.logger = None

    report = await harness.service.status(_actor(), GATEWAY_ID)

    assert report.management_mode == ManagementMode.OBSERVE
    assert report.can_enable is False
    assert _check(report, "logger").detail.endswith("its owner must add the logger.")
    access = _check(report, "logAccess")
    assert access.status == "warning"
    assert access.detail.endswith("so it counts the calls but can't say who made them.")


async def test_untraced_calls_on_a_gateway_with_publications_point_to_the_publications() -> None:
    logs = FakeLogs(
        calls=[GatewayCall(time=NOW - timedelta(minutes=5), api="chat", traced=False)],
        clock=lambda: NOW,
    )
    harness = _service_harness(logs=logs)
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)

    report = await harness.service.status(_actor(), GATEWAY_ID)

    access = _check(report, "logAccess")
    assert access.status == "warning"
    assert "Re-apply the gateway's publications" in access.detail


async def test_enable_refuses_observed_gateways() -> None:
    harness = _service_harness()
    await _seed_gateway(harness, management_mode=ManagementMode.OBSERVE)

    with pytest.raises(ConflictError):
        await harness.service.enable(_actor(), GATEWAY_ID)


async def test_enable_writes_only_missing_published_api_diagnostics() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    await _seed_ready_rollup(harness)
    harness.apim.logger = None
    harness.apim.diagnostics["chat"] = api_diagnostic_payload(LOGGER_ID, llm=True)

    report = await harness.service.enable(_actor(), GATEWAY_ID)

    assert report.gateway_id == GATEWAY_ID
    assert harness.writer.logger_writes == 1
    assert [api_name for api_name, _ in harness.writer.diagnostic_writes] == ["tickets"]
    assert "mini" not in {api_name for api_name, _ in harness.writer.diagnostic_writes}
    assert harness.apim.diagnostic_reads[:2] == ["chat", "tickets"]
    event = next(
        event
        for event in harness.entitlements.audit_events.values()
        if event.action == "gateway.telemetryEnabled"
    )
    assert event.details["apis"] == ["chat", "tickets"]
    assert event.details["failed"] == []


async def test_enable_is_idempotent_for_api_diagnostics_on_rerun() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)

    await harness.service.enable(_actor(), GATEWAY_ID)
    first_writes = list(harness.writer.diagnostic_writes)
    await harness.service.enable(_actor(), GATEWAY_ID)

    assert [api_name for api_name, _ in first_writes] == ["chat", "tickets"]
    assert harness.writer.diagnostic_writes == first_writes


async def test_enable_records_api_diagnostic_write_failures_without_hiding_them() -> None:
    harness = _service_harness()
    await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    harness.writer.write_errors["tickets"] = UpstreamError(
        "Azure refused the diagnostic",
        details={"reason": "tier denied it"},
    )

    await harness.service.enable(_actor(), GATEWAY_ID)

    event = next(
        event
        for event in harness.entitlements.audit_events.values()
        if event.action == "gateway.telemetryEnabled"
    )
    assert event.details["failed"] == ["tickets"]
    state = await harness.rollups.get_rollup_state(TENANT, GATEWAY_ID)
    assert state is not None
    assert state.diagnostics_error == "MOSAIC couldn't write the diagnostic for tickets"


async def test_ensure_api_diagnostics_falls_back_when_llm_logging_is_not_supported() -> None:
    harness = _service_harness()
    gateway = await _seed_gateway(harness)
    await _seed_governed_apis(harness)
    harness.writer.write_once_errors["chat"] = UpstreamError(
        "unsupported",
        details={"statusCode": 400, "reason": "LLM logs are not supported"},
    )
    apis = (await governed_apis(harness.gateways, TENANT))[GATEWAY_ID]

    outcome = await harness.service.ensure_api_diagnostics(
        gateway,
        apis,
    )

    assert outcome.llm_unsupported == ["chat"]
    assert dict(harness.writer.diagnostic_writes)["chat"] == api_diagnostic_payload(
        LOGGER_ID, llm=False
    )


class _Authenticator:
    def __init__(self, roles: list[str]) -> None:
        self._context = AuthContext(
            object_id=ACTOR_ID,
            tenant_id=TENANT,
            roles=frozenset(roles),
        )

    async def authenticate(self, _request: object) -> AuthContext:
        return self._context

    async def close(self) -> None:
        return None


@dataclass
class _ApiHarness:
    client: TestClient
    state: Any
    telemetry: _TelemetryHarness

    def sign_in(self, roles: list[str]) -> None:
        self.state.authenticator = _Authenticator(roles)


@pytest.fixture
def api_harness() -> Iterator[_ApiHarness]:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=["User", "Admin"],
        usage_source=UsageSourceMode.ROLLUPS,
        usage_rollup_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        telemetry = _service_harness()
        client.app.state.telemetry_service = telemetry.service
        client.app.state.usage_rollup_repository = telemetry.rollups
        client.app.state.gateway_repository = telemetry.gateways
        client.app.state.entitlement_repository = telemetry.entitlements
        client.app.state.usage_rollup_service = UsageRollupService(
            telemetry.rollups,
            gateway_repository=telemetry.gateways,
            entitlement_repository=telemetry.entitlements,
            directory_repository=client.app.state.repository,
            logs=telemetry.logs,
            telemetry=telemetry.service,
            tenant_id=TENANT,
            backfill_max_days=30,
            clock=lambda: NOW,
        )
        client.app.state.analytics_service = AnalyticsService(
            telemetry.rollups,
            gateway_repository=telemetry.gateways,
            entitlement_repository=telemetry.entitlements,
            directory_repository=client.app.state.repository,
            environment_repository=client.app.state.environment_repository,
            endpoint_repository=client.app.state.model_endpoint_repository,
            rollups=client.app.state.usage_rollup_service,
            configured=True,
            clock=lambda: NOW,
        )
        harness = _ApiHarness(client=client, state=client.app.state, telemetry=telemetry)
        harness.sign_in(["User", "Admin"])
        yield harness


async def test_telemetry_api_requires_admin_for_status_and_enable(
    api_harness: _ApiHarness,
) -> None:
    await _seed_gateway(api_harness.telemetry)
    api_harness.sign_in(["User"])

    assert api_harness.client.get(f"/api/v1/gateways/{GATEWAY_ID}/telemetry").status_code == 403
    assert (
        api_harness.client.post(f"/api/v1/gateways/{GATEWAY_ID}/telemetry/enable").status_code
        == 403
    )


async def test_telemetry_api_serializes_camel_case_response_shape(
    api_harness: _ApiHarness,
) -> None:
    await _seed_gateway(api_harness.telemetry)
    await _seed_governed_apis(api_harness.telemetry)
    await _seed_ready_rollup(api_harness.telemetry)
    api_harness.telemetry.apim.diagnostics["chat"] = api_diagnostic_payload(LOGGER_ID, llm=True)

    response = api_harness.client.get(f"/api/v1/gateways/{GATEWAY_ID}/telemetry")

    assert response.status_code == 200, response.text
    body = response.json()
    assert "gatewayId" in body
    assert "gateway_id" not in body
    assert "canEnable" in body
    assert "checkedAt" in body
    assert {"apiName", "displayName", "gaps"} <= set(body["apis"][0])


async def test_telemetry_enable_api_returns_updated_status(
    api_harness: _ApiHarness,
) -> None:
    await _seed_gateway(api_harness.telemetry)
    await _seed_governed_apis(api_harness.telemetry)
    await _seed_ready_rollup(api_harness.telemetry)
    api_harness.telemetry.apim.logger = None

    response = api_harness.client.post(f"/api/v1/gateways/{GATEWAY_ID}/telemetry/enable")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gatewayId"] == GATEWAY_ID
    assert body["checks"][0]["id"] == "logger"
    assert body["checks"][0]["status"] == "ok"
    assert [name for name, _ in api_harness.telemetry.writer.diagnostic_writes] == [
        "chat",
        "tickets",
    ]
