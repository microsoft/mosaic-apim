"""The KQL MOSAIC runs against API Management's resource-specific log tables.

Every value spliced into a query is validated first: API names and deployment keys against strict
patterns, and times as UTC literals. Nothing a caller sends ever reaches a query.

Both tables are read through a fuzzy union with an empty typed leg, so a workspace that has not yet
received a gateway or LLM log row answers with no rows instead of an error. Each leg casts every
column it projects, because a union whose legs disagree on a type splits that column in two.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from mosaic_api.errors import ValidationError
from mosaic_api.usage_telemetry import LATENCY_BUCKETS_MS

API_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
DEPLOYMENT_KEY = re.compile(r"^[A-Za-z0-9._:/-]{1,300}$")
# An LLM log row can be written a little after its gateway log row, as a streamed response ends.
# The LLM leg reads this much either side of the window so a call near midnight keeps its tokens.
LLM_LOG_MARGIN = timedelta(hours=1)
# A model call an MCP server's application makes for an MCP call counts for that call's caller
# only while the MCP call ran, give or take this much. See ADR 0025.
MCP_CALL_ALLOWANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class QueryWindow:
    """A whole number of UTC hours, [start, end)."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        for value in (self.start, self.end):
            if value.tzinfo is None:
                raise ValueError("Query windows need timezone-aware times")
            utc_value = value.astimezone(UTC)
            if utc_value.minute or utc_value.second or utc_value.microsecond:
                raise ValueError("Query windows start and end on the hour")
        if self.end <= self.start:
            raise ValueError("A query window must end after it starts")

    @property
    def hours(self) -> int:
        return int((self.end - self.start).total_seconds() // 3600)

    @property
    def timespan(self) -> tuple[datetime, datetime]:
        """The range to send as the request's timespan, which caps every table it reads."""

        return self.start - LLM_LOG_MARGIN, self.end + LLM_LOG_MARGIN

    def split(self) -> tuple["QueryWindow", "QueryWindow"]:
        middle = self.start + timedelta(hours=self.hours // 2)
        return QueryWindow(self.start, middle), QueryWindow(middle, self.end)


def _time(value: datetime) -> str:
    return f"datetime({value.astimezone(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')})"


def _api_names(names: Iterable[str]) -> list[str]:
    checked = sorted({name.casefold() for name in names})
    for name in checked:
        if not API_NAME.fullmatch(name):
            raise ValidationError("An API name MOSAIC would query is not a valid APIM name")
    return checked


def _dynamic(value: object) -> str:
    # Validated names and keys contain no quote or backslash, so JSON text is also a valid KQL
    # dynamic literal.
    return f"dynamic({json.dumps(value, separators=(',', ':'))})"


def _latency_counts() -> str:
    bounds = LATENCY_BUCKETS_MS
    parts = [f"l0 = countif(TotalTime < {bounds[0]})"]
    for index in range(1, len(bounds)):
        parts.append(
            f"l{index} = countif(TotalTime >= {bounds[index - 1]} and TotalTime < {bounds[index]})"
        )
    parts.append(f"l{len(bounds)} = countif(TotalTime >= {bounds[-1]})")
    return ",\n    ".join(parts)


def _gateway_rows(window: QueryWindow, apis: Iterable[str]) -> str:
    """Gateway log rows for MOSAIC's APIs, with each call's MOSAIC trace and refusal reason."""

    return f"""let startTime = {_time(window.start)};
let endTime = {_time(window.end)};
let mosaicApis = {_dynamic(_api_names(apis))};
let gatewayRows = union isfuzzy=true
    (ApiManagementGatewayLogs
    | where TimeGenerated >= startTime and TimeGenerated < endTime
    | project TimeGenerated, CorrelationId = tostring(CorrelationId), ApiId = tostring(ApiId),
        ApimSubscriptionId = tostring(ApimSubscriptionId), ResponseCode = toint(ResponseCode),
        BackendResponseCode = toint(BackendResponseCode), TotalTime = tolong(TotalTime),
        BackendTime = tolong(BackendTime), LastErrorSource = tostring(LastErrorSource),
        Traces = tostring(TraceRecords)),
    (datatable(TimeGenerated: datetime, CorrelationId: string, ApiId: string,
        ApimSubscriptionId: string, ResponseCode: int, BackendResponseCode: int, TotalTime: long,
        BackendTime: long, LastErrorSource: string, Traces: string)[])
| extend api = tolower(extract(@"([^/;]+)(?:;rev=[0-9]+)?$", 1, ApiId))
| where api in (mosaicApis)
| extend attribution = extract(@"mosaic-attribution ([^""\\\\]*)", 1, Traces),
    denial = extract(@"mosaic-deny ([^""\\\\]*)", 1, Traces)
| extend reason = case(
    isnotempty(denial), coalesce(extract(@"(?:^| )r=([^ ]*)", 1, denial), "unknown"),
    isempty(attribution) and ResponseCode == 401,
        iff(LastErrorSource has "token" or LastErrorSource has "jwt", "token-invalid",
            "unauthenticated"),
    "")
| extend subscription = tolower(extract(@"([^/]+)$", 1, ApimSubscriptionId));
"""


def _llm_rows(window: QueryWindow) -> str:
    """One row per call from the LLM log, which can hold several rows for one streamed call."""

    start, end = window.timespan
    return f"""let llmRows = union isfuzzy=true
    (ApiManagementGatewayLlmLog
    | where TimeGenerated >= {_time(start)} and TimeGenerated < {_time(end)}
    | project CorrelationId = tostring(CorrelationId), PromptTokens = tolong(PromptTokens),
        CompletionTokens = tolong(CompletionTokens), TotalTokens = tolong(TotalTokens),
        DeploymentName = tostring(DeploymentName), ModelName = tostring(ModelName)),
    (datatable(CorrelationId: string, PromptTokens: long, CompletionTokens: long,
        TotalTokens: long, DeploymentName: string, ModelName: string)[])
| summarize promptTokens = max(PromptTokens), completionTokens = max(CompletionTokens),
    totalTokens = max(TotalTokens),
    llmDeployment = take_anyif(DeploymentName, isnotempty(DeploymentName)),
    llmModel = take_anyif(ModelName, isnotempty(ModelName))
    by CorrelationId;
"""


def _mcp_calls(window: QueryWindow) -> str:
    """Admitted MCP calls whose server calls models as an application, by their own reference.

    Read either side of the window, as the LLM leg is, because a tool's model calls are logged
    while its MCP call is still running, and the MCP call is logged when it ends. See ADR 0025.
    """

    start, end = window.timespan
    return f"""let mcpCalls = union isfuzzy=true
    (ApiManagementGatewayLogs
    | where TimeGenerated >= {_time(start)} and TimeGenerated < {_time(end)}
    | project mcpTime = TimeGenerated, mcpTotalTime = tolong(TotalTime),
        mcpApiId = tostring(ApiId), mcpTraces = tostring(TraceRecords)),
    (datatable(mcpTime: datetime, mcpTotalTime: long, mcpApiId: string, mcpTraces: string)[])
| extend mcpApi = tolower(extract(@"([^/;]+)(?:;rev=[0-9]+)?$", 1, mcpApiId))
| where mcpApi in (mosaicApis)
| extend mcpAttribution = extract(@"mosaic-attribution ([^""\\\\]*)", 1, mcpTraces)
| extend mcpRef = tolower(extract(@"(?:^| )r=([^ ]*)", 1, mcpAttribution)),
    oi = tolower(extract(@"(?:^| )i=([^ ]*)", 1, mcpAttribution))
| where isnotempty(mcpRef) and isnotempty(oi)
| extend og = tolower(extract(@"(?:^| )g=([^ ]*)", 1, mcpAttribution)),
    om = tolower(extract(@"(?:^| )m=([^ ]*)", 1, mcpAttribution))
| summarize arg_min(mcpTime, mcpTotalTime, mcpApi, og, om, oi) by mcpRef;
"""


_ATTRIBUTION_KEYS = """| extend v = extract(@"(?:^| )v=([^ ]*)", 1, attribution),
    g = tolower(extract(@"(?:^| )g=([^ ]*)", 1, attribution)),
    m = tolower(extract(@"(?:^| )m=([^ ]*)", 1, attribution)),
    a = tolower(extract(@"(?:^| )a=([^ ]*)", 1, attribution)),
    r = tolower(extract(@"(?:^| )r=([^ ]*)", 1, attribution)),
    i = tolower(extract(@"(?:^| )i=([^ ]*)", 1, attribution))
"""


def _on_behalf_columns() -> str:
    """Each model call's MCP call, when it names one: found, or why it couldn't be used.

    ``onBehalf`` is empty without a reference, ``found`` when the MCP call it names ran on this
    gateway at the time, and otherwise ``malformed``, ``missing`` or ``late``. Only a found call
    keeps the MCP call's grant (``og``), member (``om``), model caller (``oi``) and API (``oapi``);
    the rollup decides whether this call's caller is that model caller. An MCP call's own trace
    carries its reference beside its model caller (``i``), and names no other call.
    """

    allowance = int(MCP_CALL_ALLOWANCE.total_seconds() * 1000)
    return f"""| extend mcpCall = iff(isempty(i), r, "")
| join kind=leftouter mcpCalls on $left.mcpCall == $right.mcpRef
| extend during = isnotempty(mcpRef)
    and abs(datetime_diff('millisecond', TimeGenerated, mcpTime)) <= mcpTotalTime + {allowance}
| extend onBehalf = case(
    isempty(mcpCall), "",
    mcpCall == "!", "malformed",
    isempty(mcpRef), "missing",
    during, "found",
    "late")
| extend og = iff(onBehalf == "found", og, ""), om = iff(onBehalf == "found", om, ""),
    oi = iff(onBehalf == "found", oi, ""), oapi = iff(onBehalf == "found", mcpApi, "")
"""


def calls_query(window: QueryWindow, apis: Iterable[str]) -> str:
    """Admitted calls by hour, trace keys, subscription, API and model deployment.

    Refused calls are left to :func:`denials_query`. 429 is a rate limit, and a 403 raised by a
    token-limit or quota policy is a spent quota. A model call an MCP server's application made
    also says which MCP call it served; see :func:`_on_behalf_columns`.
    """

    return (
        _gateway_rows(window, apis)
        + _llm_rows(window)
        + _mcp_calls(window)
        + f"""gatewayRows
| where isempty(reason)
| join kind=leftouter llmRows on CorrelationId
{_ATTRIBUTION_KEYS}{_on_behalf_columns()}| extend status = case(
    ResponseCode == 429, "throttled",
    ResponseCode == 403 and (LastErrorSource contains "token-limit"
        or LastErrorSource contains "quota"), "quota",
    ResponseCode >= 200 and ResponseCode < 400, "ok",
    ResponseCode >= 400 and ResponseCode < 500, "clientError",
    "serverError")
| summarize requests = count(),
    ok = countif(status == "ok"),
    throttled = countif(status == "throttled"),
    quota = countif(status == "quota"),
    clientErrors = countif(status == "clientError"),
    serverErrors = countif(status == "serverError"),
    backendThrottled = countif(BackendResponseCode == 429),
    keyRequests = countif(isnotempty(subscription)),
    metered = countif(isnotnull(totalTokens)),
    promptTokens = sum(promptTokens),
    completionTokens = sum(completionTokens),
    totalTokens = sum(totalTokens),
    totalTime = sum(TotalTime),
    backendTime = sum(BackendTime),
    {_latency_counts()},
    lastSeen = max(TimeGenerated)
    by hour = hourofday(TimeGenerated), v, g, m, a, api, subscription,
        deployment = llmDeployment, model = llmModel, onBehalf, og, om, oi, oapi
"""
    )


def peaks_query(window: QueryWindow, apis: Iterable[str]) -> str:
    """Each hour's busiest minute per grant counter: a grant key and member, or a subscription."""

    return (
        _gateway_rows(window, apis)
        + _llm_rows(window)
        + f"""gatewayRows
| where isempty(reason)
| join kind=leftouter llmRows on CorrelationId
{_ATTRIBUTION_KEYS}| extend link = case(
    v == "1" and isnotempty(g), strcat("t:", g, "|", m),
    isnotempty(subscription), strcat("s:", subscription),
    "")
| where isnotempty(link)
| summarize tokens = sum(totalTokens), requests = count()
    by link, minute = bin(TimeGenerated, 1m)
| summarize peakTokens = max(tokens), peakRequests = max(requests)
    by link, hour = hourofday(minute)
"""
    )


def deployment_peaks_query(window: QueryWindow, deployments: Mapping[str, str]) -> str:
    """Each hour's busiest minute per model deployment, across every caller.

    ``deployments`` maps API names to the deployment key they front. Requests count only calls
    that reached the model; tokens exist only for those anyway.
    """

    mapping: dict[str, str] = {}
    for api, key in deployments.items():
        if not DEPLOYMENT_KEY.fullmatch(key):
            raise ValidationError("A deployment key MOSAIC would query is not valid")
        mapping[_api_names([api])[0]] = key
    return (
        _gateway_rows(window, mapping)
        + _llm_rows(window)
        + f"""let deploymentOf = {_dynamic(dict(sorted(mapping.items())))};
gatewayRows
| where isempty(reason)
| join kind=leftouter llmRows on CorrelationId
| extend deploymentKey = tostring(deploymentOf[api])
| where isnotempty(deploymentKey)
| summarize tokens = sum(totalTokens), requests = countif(BackendResponseCode > 0)
    by deploymentKey, minute = bin(TimeGenerated, 1m)
| summarize peakTokens = max(tokens), peakRequests = max(requests)
    by deploymentKey, hour = hourofday(minute)
"""
    )


def denials_query(window: QueryWindow, apis: Iterable[str]) -> str:
    """Refused calls by hour, reason, validated caller and client app, and API."""

    return (
        _gateway_rows(window, apis)
        + """gatewayRows
| where isnotempty(reason)
| extend v = extract(@"(?:^| )v=([^ ]*)", 1, denial),
    o = tolower(extract(@"(?:^| )o=([^ ]*)", 1, denial)),
    a = tolower(extract(@"(?:^| )a=([^ ]*)", 1, denial))
| summarize requests = count(), lastSeen = max(TimeGenerated)
    by hour = hourofday(TimeGenerated), reason, o, a, api, v
"""
    )


PROBE_HOURS = 24


def probe_query() -> str:
    """Whether logs arrive and carry MOSAIC's traces, over the last day. Cheap and read-only."""

    return f"""union isfuzzy=true
    (ApiManagementGatewayLogs
    | where TimeGenerated > ago({PROBE_HOURS}h)
    | project TimeGenerated, source = "gateway",
        traced = tostring(TraceRecords) contains "mosaic-attribution"),
    (ApiManagementGatewayLlmLog
    | where TimeGenerated > ago({PROBE_HOURS}h)
    | project TimeGenerated, source = "llm", traced = false),
    (datatable(TimeGenerated: datetime, source: string, traced: bool)[])
| summarize rows = count(), traced = countif(traced), lastSeen = max(TimeGenerated) by source
"""
