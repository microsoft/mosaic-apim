"""API Management's Azure Monitor telemetry settings, as MOSAIC writes and judges them.

Three things decide whether a gateway's calls reach the tables MOSAIC reads:

- a diagnostic setting on the service that sends ``GatewayLogs`` and ``GatewayLlmLogs`` to a Log
  Analytics workspace as resource-specific tables;
- the ``azuremonitor`` logger, through which API diagnostics feed those logs;
- an ``azuremonitor`` diagnostic on each API, which decides what a call's log row holds.

MOSAIC needs every call (sampling at 100%), each call's trace records (verbosity Information) and,
for model APIs, the LLM log that carries token counts. It asks for no client IPs, headers, bodies,
prompts or completions.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Literal

JsonObject = dict[str, Any]

AZURE_MONITOR = "azuremonitor"
GATEWAY_LOGS = "GatewayLogs"
GATEWAY_LLM_LOGS = "GatewayLlmLogs"
GATEWAY_LOG_CATEGORIES: tuple[str, ...] = (GATEWAY_LOGS, GATEWAY_LLM_LOGS)
DIAGNOSTIC_SETTING_NAME = "mosaic-gateway-logs"
WORKSPACE_PLACEHOLDER = "<log-analytics-workspace-resource-id>"

ApiDiagnosticGap = Literal["missing", "logger", "verbosity", "sampling", "llmLogs"]
LogRoutingProblem = Literal["missing", "legacyTable", "noGatewayLogs", "noLlmLogs"]

_VERBOSE_ENOUGH = frozenset({"information", "verbose"})


def azure_monitor_logger_payload() -> JsonObject:
    return {"properties": {"loggerType": "azureMonitor", "isBuffered": True}}


def _no_payload() -> JsonObject:
    return {"headers": [], "body": {"bytes": 0}}


def api_diagnostic_payload(logger_id: str, *, llm: bool) -> JsonObject:
    """The API diagnostic MOSAIC writes: every call, its traces, and no request content.

    The LLM log is turned on without its message settings, so it records token counts but no
    prompts or completions.
    """

    properties: JsonObject = {
        "loggerId": logger_id,
        "verbosity": "information",
        "logClientIp": False,
        "sampling": {"samplingType": "fixed", "percentage": 100},
        "frontend": {"request": _no_payload(), "response": _no_payload()},
        "backend": {"request": _no_payload(), "response": _no_payload()},
    }
    if llm:
        properties["largeLanguageModel"] = {"logs": "enabled"}
    return {"properties": properties}


def api_diagnostic_gaps(diagnostic: JsonObject | None, *, llm: bool) -> list[ApiDiagnosticGap]:
    """What an API's diagnostic lacks for MOSAIC's rollups. Empty when it is ready."""

    if diagnostic is None:
        return ["missing"]
    properties = diagnostic.get("properties")
    if not isinstance(properties, dict):
        return ["missing"]
    gaps: list[ApiDiagnosticGap] = []
    logger_id = str(properties.get("loggerId") or "").casefold()
    if not logger_id.endswith(f"/loggers/{AZURE_MONITOR}"):
        gaps.append("logger")
    if str(properties.get("verbosity") or "").casefold() not in _VERBOSE_ENOUGH:
        gaps.append("verbosity")
    sampling = properties.get("sampling")
    percentage = sampling.get("percentage") if isinstance(sampling, dict) else None
    if not isinstance(percentage, int | float) or percentage < 100:
        gaps.append("sampling")
    if llm:
        settings = properties.get("largeLanguageModel")
        logs = settings.get("logs") if isinstance(settings, dict) else None
        if str(logs or "").casefold() != "enabled":
            gaps.append("llmLogs")
    return gaps


@dataclass(frozen=True)
class LogRouting:
    """Whether the service's diagnostic settings send what MOSAIC reads where it reads it."""

    ready: bool
    problem: LogRoutingProblem | None
    setting_name: str | None = None
    workspace_id: str | None = None
    # The setting's enabled log entries, kept so a remediation command preserves them.
    logs: list[JsonObject] = field(default_factory=list)
    metrics: bool = False


def _enabled_categories(logs: list[JsonObject]) -> set[str]:
    enabled: set[str] = set()
    for entry in logs:
        if not entry.get("enabled"):
            continue
        if str(entry.get("categoryGroup") or "").casefold() == "alllogs":
            enabled.update(category.casefold() for category in GATEWAY_LOG_CATEGORIES)
        category = entry.get("category")
        if isinstance(category, str) and category:
            enabled.add(category.casefold())
    return enabled


def evaluate_diagnostic_settings(settings: list[JsonObject]) -> LogRouting:
    """Pick the diagnostic setting that best serves MOSAIC and say what, if anything, it lacks."""

    candidates: list[tuple[int, LogRouting]] = []
    for setting in settings:
        properties = setting.get("properties")
        if not isinstance(properties, dict):
            continue
        workspace = properties.get("workspaceId")
        if not isinstance(workspace, str) or not workspace:
            continue
        raw_logs = properties.get("logs")
        logs = [entry for entry in raw_logs if isinstance(entry, dict)] if isinstance(
            raw_logs, list
        ) else []
        enabled = _enabled_categories(logs)
        raw_metrics = properties.get("metrics")
        metrics = isinstance(raw_metrics, list) and any(
            isinstance(entry, dict) and entry.get("enabled") for entry in raw_metrics
        )
        dedicated = str(properties.get("logAnalyticsDestinationType") or "").casefold() == (
            "dedicated"
        )
        problem: LogRoutingProblem | None
        rank: int
        if GATEWAY_LOGS.casefold() not in enabled:
            problem, rank = "noGatewayLogs", 1
        elif not dedicated:
            problem, rank = "legacyTable", 2
        elif GATEWAY_LLM_LOGS.casefold() not in enabled:
            problem, rank = "noLlmLogs", 3
        else:
            problem, rank = None, 4
        candidates.append(
            (
                rank,
                LogRouting(
                    ready=problem is None,
                    problem=problem,
                    setting_name=str(setting.get("name") or "") or None,
                    workspace_id=workspace,
                    logs=[entry for entry in logs if entry.get("enabled")],
                    metrics=metrics,
                ),
            )
        )
    if not candidates:
        return LogRouting(ready=False, problem="missing")
    return max(candidates, key=lambda item: item[0])[1]


def _logs_argument(existing: list[JsonObject]) -> str:
    entries: list[JsonObject] = []
    for entry in existing:
        group = entry.get("categoryGroup")
        category = entry.get("category")
        if isinstance(group, str) and group:
            entries.append({"categoryGroup": group, "enabled": True})
        elif isinstance(category, str) and category:
            entries.append({"category": category, "enabled": True})
    enabled = _enabled_categories(entries)
    for category in GATEWAY_LOG_CATEGORIES:
        if category.casefold() not in enabled:
            entries.append({"category": category, "enabled": True})
    return json.dumps(entries, separators=(",", ":"))


def diagnostic_setting_command(resource_id: str, routing: LogRouting) -> str:
    """An Azure CLI command that makes the service's logs readable by MOSAIC.

    It updates the setting MOSAIC found, keeping its other categories and metrics, or creates one.
    Azure refuses a second setting that sends a category to the same workspace, so updating in
    place is also the only way to fix one that writes to the legacy ``AzureDiagnostics`` table.
    """

    name = routing.setting_name or DIAGNOSTIC_SETTING_NAME
    workspace = routing.workspace_id or WORKSPACE_PLACEHOLDER
    command = (
        f"az monitor diagnostic-settings create --name {name} --resource {resource_id} "
        f"--workspace {workspace} --export-to-resource-specific true "
        f"--logs '{_logs_argument(routing.logs)}'"
    )
    if routing.metrics:
        command += """ --metrics '[{"category":"AllMetrics","enabled":true}]'"""
    return command


def monitoring_reader_command(resource_id: str, principal_id: str | None) -> str:
    assignee = principal_id or "<mosaic-api-principal-id>"
    return (
        f"az role assignment create --assignee-object-id {assignee} "
        "--assignee-principal-type ServicePrincipal --role \"Monitoring Reader\" "
        f"--scope {resource_id}"
    )
