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
from mosaic_api.usage_telemetry import LATENCY_BUCKETS_MS, PoolMembers, only_member

API_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
DEPLOYMENT_KEY = re.compile(r"^[A-Za-z0-9._:/-]{1,300}$")
# A lowercased backend name, host or deployment name spliced into a pool's member lookups.
_NAME_PART = re.compile(r"^[a-z0-9][a-z0-9._-]{0,255}$")
# An LLM log row can be written a little after its gateway log row, as a streamed response ends.
# The LLM leg reads this much either side of the window so a call near midnight keeps its tokens.
LLM_LOG_MARGIN = timedelta(hours=1)


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
        BackendId = tostring(BackendId), BackendUrl = tostring(BackendUrl),
        Traces = tostring(TraceRecords)),
    (datatable(TimeGenerated: datetime, CorrelationId: string, ApiId: string,
        ApimSubscriptionId: string, ResponseCode: int, BackendResponseCode: int, TotalTime: long,
        BackendTime: long, LastErrorSource: string, BackendId: string, BackendUrl: string,
        Traces: string)[])
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


_ATTRIBUTION_KEYS = """| extend v = extract(@"(?:^| )v=([^ ]*)", 1, attribution),
    g = tolower(extract(@"(?:^| )g=([^ ]*)", 1, attribution)),
    m = tolower(extract(@"(?:^| )m=([^ ]*)", 1, attribution)),
    a = tolower(extract(@"(?:^| )a=([^ ]*)", 1, attribution))
"""


def _pool_backends(pool_apis: Iterable[str]) -> tuple[str, str]:
    """For model pool APIs, the backend that served each call: the `let`, and the group-by columns.

    API Management picks a pool's member, so the attribution trace can't name it. The backend ID
    and URL can. Other APIs answer "" for all three, so their rows don't split.
    """

    pools = _api_names(pool_apis)
    if not pools:
        return "", ""
    return (
        f"let poolApis = {_dynamic(pools)};\n",
        """| extend pooled = api in (poolApis)
| extend backendId = iff(pooled, tolower(BackendId), ""),
    backendUrl = iff(pooled, tolower(BackendUrl), "")
| extend backendHost = extract(@"^[a-z][a-z0-9+.-]*://([^/:?#]+)", 1, backendUrl),
    backendDeployment = extract(@"/openai/deployments/([^/?#]+)", 1, backendUrl)
""",
    )


def calls_query(window: QueryWindow, apis: Iterable[str], pool_apis: Iterable[str] = ()) -> str:
    """Admitted calls by hour, trace keys, subscription, API and model deployment.

    Refused calls are left to :func:`denials_query`. 429 is a rate limit, and a 403 raised by a
    token-limit or quota policy is a spent quota. Calls to the ``pool_apis`` are also grouped by the
    backend that served them.
    """

    pool_let, pool_columns = _pool_backends(pool_apis)
    by_backend = ",\n        backendId, backendHost, backendDeployment" if pool_let else ""
    return (
        _gateway_rows(window, apis)
        + _llm_rows(window)
        + pool_let
        + f"""gatewayRows
| where isempty(reason)
| join kind=leftouter llmRows on CorrelationId
{_ATTRIBUTION_KEYS}{pool_columns}| extend status = case(
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
        deployment = llmDeployment, model = llmModel{by_backend}
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


def deployment_peaks_query(
    window: QueryWindow,
    deployments: Mapping[str, str],
    pools: Mapping[str, PoolMembers] | None = None,
) -> str:
    """Each hour's busiest minute per model deployment, across every caller.

    ``deployments`` maps API names to the deployment key they front. ``pools`` maps model pool
    API names to how their calls are placed on a member deployment, which the query repeats from
    each call's backend just as :meth:`PoolMembers.member_for` does. Requests count only calls that
    reached the model; tokens exist only for those anyway.
    """

    mapping: dict[str, str] = {}
    for api, key in deployments.items():
        if not DEPLOYMENT_KEY.fullmatch(key):
            raise ValidationError("A deployment key MOSAIC would query is not valid")
        mapping[_api_names([api])[0]] = key
    placement = _member_placement(pools) if pools else {}
    pooled = _api_names(pools or {})
    member_lets = "".join(
        f"let {name} = {_dynamic(dict(sorted(lookup.items())))};\n"
        for name, lookup in placement.items()
    )
    deployment_key = (
        """coalesce(tostring(deploymentOf[api]),
    tostring(memberByBackend[strcat(api, "|", backendId)]),
    iff(isnotnull(memberByHost[hostKey]),
        coalesce(tostring(memberByRoute[strcat(hostKey, "|", backendDeployment)]),
            tostring(memberByHost[hostKey])),
        tostring(memberByDeployment[strcat(api, "|", backendDeployment)])))"""
        if pooled
        else "tostring(deploymentOf[api])"
    )
    member_columns = (
        """| extend backendId = tolower(BackendId), backendUrl = tolower(BackendUrl)
| extend backendHost = extract(@"^[a-z][a-z0-9+.-]*://([^/:?#]+)", 1, backendUrl),
    backendDeployment = tolower(coalesce(
        extract(@"/openai/deployments/([^/?#]+)", 1, backendUrl), llmDeployment))
| extend hostKey = strcat(api, "|", backendHost)
"""
        if pooled
        else ""
    )
    return (
        _gateway_rows(window, [*mapping, *pooled])
        + _llm_rows(window)
        + f"""let deploymentOf = {_dynamic(dict(sorted(mapping.items())))};
{member_lets}gatewayRows
| where isempty(reason)
| join kind=leftouter llmRows on CorrelationId
{member_columns}| extend deploymentKey = {deployment_key}
| where isnotempty(deploymentKey)
| summarize tokens = sum(totalTokens), requests = countif(BackendResponseCode > 0)
    by deploymentKey, minute = bin(TimeGenerated, 1m)
| summarize peakTokens = max(tokens), peakRequests = max(requests)
    by deploymentKey, hour = hourofday(minute)
"""
    )


def _member_placement(pools: Mapping[str, PoolMembers]) -> dict[str, dict[str, str]]:
    """Each pool's member placement as KQL lookups, keyed by API name and then the call's parts.

    ``memberByHost`` names a host's member, or "" when it has several, so a host the pool uses is
    never placed by deployment alone. A part that isn't a plain name is left out, so the calls it
    would place stay off every member's peaks.
    """

    by_backend: dict[str, str] = {}
    by_route: dict[str, str] = {}
    by_host: dict[str, str] = {}
    by_deployment: dict[str, str] = {}
    for api, members in pools.items():
        name = _api_names([api])[0]
        for backend, key in members.by_backend.items():
            if _NAME_PART.fullmatch(backend) and DEPLOYMENT_KEY.fullmatch(key):
                by_backend[f"{name}|{backend}"] = key
        for (host, deployment), key in members.by_route.items():
            if (
                _NAME_PART.fullmatch(host)
                and _NAME_PART.fullmatch(deployment)
                and DEPLOYMENT_KEY.fullmatch(key)
            ):
                by_route[f"{name}|{host}|{deployment}"] = key
        for host, keys in members.by_host.items():
            if _NAME_PART.fullmatch(host):
                only = only_member(keys)
                by_host[f"{name}|{host}"] = only if only and DEPLOYMENT_KEY.fullmatch(only) else ""
        for deployment, keys in members.by_deployment.items():
            only = only_member(keys)
            if only and _NAME_PART.fullmatch(deployment) and DEPLOYMENT_KEY.fullmatch(only):
                by_deployment[f"{name}|{deployment}"] = only
    return {
        "memberByBackend": by_backend,
        "memberByRoute": by_route,
        "memberByHost": by_host,
        "memberByDeployment": by_deployment,
    }


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
