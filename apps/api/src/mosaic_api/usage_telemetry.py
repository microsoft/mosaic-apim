"""What MOSAIC keeps from gateway telemetry, and how the figures combine.

API Management writes one gateway log row per call and, for language model APIs, one or more LLM
log rows carrying token counts. MOSAIC never serves views straight from Log Analytics. A background
job queries it per gateway, folds the rows into small documents in the ``usage-rollups`` Cosmos
container, and every usage view reads those. See ADR 0019.

Four kinds of document live there, all partitioned by tenant:

- :class:`UsageFact`: one per day, gateway, grant link and caller. The portal reads these.
- :class:`UsageSummary`: one per day or month, gateway and dimension. The console reads these.
- :class:`AttributionRecord`: which grant a gateway log's grant key or subscription stands for.
- :class:`UsageRollupState`: how far each gateway's telemetry has been rolled up.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import Field

from mosaic_api.domain import (
    EntitlementResource,
    EntitlementSubject,
    Entity,
    MosaicModel,
    deterministic_id,
)

# Upper bounds, in milliseconds, of every latency bucket but the last, which is open-ended.
LATENCY_BUCKETS_MS: tuple[int, ...] = (100, 250, 500, 1000, 2000, 5000, 10000, 30000, 60000)
LATENCY_BUCKET_COUNT = len(LATENCY_BUCKETS_MS) + 1
# Cosmos keeps an item whose ttl is -1 until it is deleted.
KEEP_FOREVER = -1
# Rolled-up usage counts as current while its last successful rollup is within this many rollup
# intervals, plus the time Log Analytics takes to ingest a gateway log.
STALE_INTERVALS = 2
INGESTION_ALLOWANCE = timedelta(minutes=30)


def rollup_stale_after(interval: timedelta) -> timedelta:
    """How old a gateway's last successful rollup can be before its figures count as delayed."""

    return interval * STALE_INTERVALS + INGESTION_ALLOWANCE


StatusClass = Literal["ok", "throttled", "quota", "denied", "clientError", "serverError"]
STATUS_CLASSES: tuple[StatusClass, ...] = (
    "ok",
    "throttled",
    "quota",
    "denied",
    "clientError",
    "serverError",
)
UsageLink = Literal["trace", "subscription"]
SummaryPeriod = Literal["day", "month"]
# What a summary's entries are keyed by:
#
# - `total`: one entry, keyed "", for every call including refused ones;
# - `caller`: the Entra object ID that made admitted, linked calls;
# - `grant`: `{link}:{linkKey}`, one grant link;
# - `grantCaller`: `{link}:{linkKey}|{caller}`, one grant link and caller;
# - `clientApp`: `{clientApp}|{api}`, the client application ID the attribution trace recorded;
# - `api`: the API Management API name;
# - `deployment`: `{modelEndpointId}/{deploymentName}`, for published model APIs;
# - `model`: `{model}|{api}`, the model name the LLM log reported, lowercased;
# - `denial`: `{reason}|{caller}|{clientApp}|{api}` for refused calls;
# - `unattributed`: `{api}|{subscription}` for admitted calls MOSAIC could not link;
# - `onBehalf`: `{link}:{linkKey}|{caller}|{person}|{mcpApi}`, a grant's calls that an MCP
#   server's application, its caller, made for the person who called that MCP server;
# - `onBehalfUnresolved`: `{link}:{linkKey}|{reason}`, a grant's calls that named an MCP call
#   MOSAIC couldn't attribute them through. See ADR 0025.
SummaryDimension = Literal[
    "total",
    "caller",
    "grant",
    "grantCaller",
    "clientApp",
    "api",
    "deployment",
    "model",
    "denial",
    "unattributed",
    "onBehalf",
    "onBehalfUnresolved",
]
SUMMARY_DIMENSIONS: tuple[SummaryDimension, ...] = (
    "total",
    "caller",
    "grant",
    "grantCaller",
    "clientApp",
    "api",
    "deployment",
    "model",
    "denial",
    "unattributed",
    "onBehalf",
    "onBehalfUnresolved",
)
# Why a model call that named an MCP call wasn't attributed to that call's caller:
#
# - `malformed`: the application sent something other than one MCP call's reference;
# - `missing`: no MCP call on the gateway has that reference, around that time;
# - `late`: the MCP call it names had ended, beyond the allowance, when it was made;
# - `caller`: the MCP server calls models as a different application, or none;
# - `unknown`: MOSAIC doesn't yet know whose grant the MCP call matched.
OnBehalfUnresolvedReason = Literal["malformed", "missing", "late", "caller", "unknown"]
# A summary item holds at most this many entries; larger dimensions are split into shards so no
# document nears Cosmos' 2 MB item limit.
SUMMARY_SHARD_SIZE = 1000


def _zero_latency() -> list[int]:
    return [0] * LATENCY_BUCKET_COUNT


class UsageMetrics(MosaicModel):
    """Additive call figures, plus busiest-minute peaks that combine by maximum.

    Peaks are exact only at the grain they were measured at: a grant's counter for facts, and a
    model deployment for deployment summaries. Anywhere else the maximum of several peaks is a
    lower bound on the combined busiest minute, and views say so.
    """

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # Calls whose LLM log carried token counts. MCP calls, and calls that failed before reaching
    # a model, carry none.
    metered_requests: int = 0
    ok: int = 0
    throttled: int = 0
    quota: int = 0
    denied: int = 0
    client_errors: int = 0
    server_errors: int = 0
    # 429s the model deployment itself returned, whatever the gateway then answered.
    backend_throttled: int = 0
    # Calls that presented an API Management subscription key rather than only a token.
    key_requests: int = 0
    total_time_ms: int = 0
    backend_time_ms: int = 0
    latency: list[int] = Field(default_factory=_zero_latency)
    peak_minute_tokens: int = 0
    peak_minute_requests: int = 0
    last_seen: datetime | None = None

    @property
    def errors(self) -> int:
        return self.client_errors + self.server_errors

    def add(self, other: "UsageMetrics") -> None:
        self.requests += other.requests
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.metered_requests += other.metered_requests
        self.ok += other.ok
        self.throttled += other.throttled
        self.quota += other.quota
        self.denied += other.denied
        self.client_errors += other.client_errors
        self.server_errors += other.server_errors
        self.backend_throttled += other.backend_throttled
        self.key_requests += other.key_requests
        self.total_time_ms += other.total_time_ms
        self.backend_time_ms += other.backend_time_ms
        latency = list(self.latency)
        for index, count in enumerate(other.latency[:LATENCY_BUCKET_COUNT]):
            latency[index] += count
        self.latency = latency
        self.peak_minute_tokens = max(self.peak_minute_tokens, other.peak_minute_tokens)
        self.peak_minute_requests = max(self.peak_minute_requests, other.peak_minute_requests)
        if other.last_seen is not None and (
            self.last_seen is None or other.last_seen > self.last_seen
        ):
            self.last_seen = other.last_seen

    def count_status(self, status: StatusClass, count: int) -> None:
        if status == "ok":
            self.ok += count
        elif status == "throttled":
            self.throttled += count
        elif status == "quota":
            self.quota += count
        elif status == "denied":
            self.denied += count
        elif status == "clientError":
            self.client_errors += count
        else:
            self.server_errors += count


def combine(metrics: Iterable[UsageMetrics]) -> UsageMetrics:
    total = UsageMetrics()
    for item in metrics:
        total.add(item)
    return total


def latency_percentile(histogram: Sequence[int], percentile: float) -> float | None:
    """Estimate a latency percentile, in milliseconds, from bucket counts.

    Values are interpolated linearly inside the bucket the percentile falls in. The open-ended
    last bucket reports its lower bound, so a p95 there reads "at least a minute".
    """

    total = sum(histogram)
    if total <= 0:
        return None
    rank = percentile / 100 * total
    seen = 0.0
    for index, count in enumerate(histogram):
        if count <= 0:
            continue
        if seen + count >= rank:
            lower = 0 if index == 0 else LATENCY_BUCKETS_MS[index - 1]
            if index >= len(LATENCY_BUCKETS_MS):
                return float(lower)
            upper = LATENCY_BUCKETS_MS[index]
            fraction = (rank - seen) / count
            return round(lower + (upper - lower) * fraction, 1)
        seen += count
    return float(LATENCY_BUCKETS_MS[-1])


def latency_bucket(milliseconds: float) -> int:
    for index, bound in enumerate(LATENCY_BUCKETS_MS):
        if milliseconds < bound:
            return index
    return len(LATENCY_BUCKETS_MS)


class UsageHour(MosaicModel):
    """One UTC hour of calls. A grant counter's hours also keep the hour's busiest minute."""

    hour: int = Field(ge=0, le=23)
    requests: int = 0
    total_tokens: int = 0
    throttled: int = 0
    quota: int = 0
    denied: int = 0
    errors: int = 0
    peak_minute_tokens: int = 0
    peak_minute_requests: int = 0


class UsageBreakdown(MosaicModel):
    """A fact's calls split by where they went and which client made them."""

    api_name: str
    deployment: str | None = None
    model: str | None = None
    client_app_id: str | None = None
    metrics: UsageMetrics = Field(default_factory=UsageMetrics)


class UsageOnBehalf(MosaicModel):
    """A fact's calls that an MCP server's application made for one person. See ADR 0025.

    ``object_id`` is the person who called the MCP server: its call's validated member, or its
    grant's subject. ``mcp_api`` is the MCP server's API on the same gateway, and ``mcp_key`` the
    MCP grant key the person's call matched. The fact's own caller is still the application.
    """

    object_id: str
    mcp_api: str
    mcp_key: str
    metrics: UsageMetrics = Field(default_factory=UsageMetrics)
    hours: list[UsageHour] = Field(default_factory=list)


def _none_made(value: object) -> bool:
    """Whether an on-behalf field is empty, so it's left out of stored documents.

    A release from before ADR 0025 forbids fields it doesn't know, and can still read every fact
    no MCP server's call was attributed through.
    """

    return value == []


def day_bucket(day: str) -> str:
    """The bucket every item rolled up from one gateway's day shares, so a day replaces whole."""

    return f"day:{day}"


def period_bucket(period: SummaryPeriod, period_start: str) -> str:
    return f"{period}:{period_start}"


class UsageFact(Entity):
    """One day of calls on one gateway for one grant link and caller.

    ``link_key`` is what the calls were linked to the grant by: the attribution trace's grant key,
    which equals ``EntitlementBinding.attribution_key``, or the API Management subscription's
    registry key. Readers find a grant's facts by that key, so a fact written before MOSAIC knew
    which grant the key stands for still counts once it does.

    ``caller_object_id`` is the Entra object ID that made the calls: the validated member for a
    security-group grant, and otherwise the grant's own subject, because a direct grant and every
    key belong to exactly one subject. It is None when MOSAIC can't tell.

    ``on_behalf`` splits out the calls the caller, an MCP server's application, made for the
    people who called that MCP server. They stay the caller's calls too. See ADR 0025.
    """

    entity_type: Literal["usageFact"] = "usageFact"
    day: str
    bucket: str
    gateway_id: str
    link: UsageLink
    link_key: str
    caller_object_id: str | None = None
    entitlement_id: str | None = None
    publication_id: str | None = None
    per_member: bool = False
    metrics: UsageMetrics = Field(default_factory=UsageMetrics)
    hours: list[UsageHour] = Field(default_factory=list)
    breakdown: list[UsageBreakdown] = Field(default_factory=list)
    on_behalf: list[UsageOnBehalf] = Field(default_factory=list, exclude_if=_none_made)
    content_hash: str = ""
    ttl: int = KEEP_FOREVER


def usage_fact_id(
    tenant_id: str,
    day: str,
    gateway_id: str,
    link: UsageLink,
    link_key: str,
    caller_object_id: str | None,
) -> str:
    return deterministic_id(
        "usagefact", tenant_id, day, gateway_id, link, link_key, caller_object_id or ""
    )


class UsageSummaryEntry(MosaicModel):
    key: str
    metrics: UsageMetrics = Field(default_factory=UsageMetrics)
    # Daily ``total`` and ``api`` summaries only: the day's calls by UTC hour, for the views that
    # cover the last 24 hours.
    hours: list[UsageHour] | None = None
    # Deployment summaries only: each UTC hour's busiest minute across every caller.
    hourly_peak_tokens: list[int] | None = None
    # Client-app summaries only: the application whose own grant a call with this app ID matched.
    # Only an application's app-only token matches its grant, so for a service principal or a
    # managed identity, whose app IDs MOSAIC doesn't keep, this is how the app ID gets a name.
    application_object_id: str | None = None


class UsageSummary(Entity):
    entity_type: Literal["usageSummary"] = "usageSummary"
    period: SummaryPeriod
    period_start: str
    bucket: str
    gateway_id: str
    dimension: SummaryDimension
    shard: int = 0
    entries: list[UsageSummaryEntry] = Field(default_factory=list)
    content_hash: str = ""
    ttl: int = KEEP_FOREVER


def usage_summary_id(
    tenant_id: str,
    period: SummaryPeriod,
    period_start: str,
    gateway_id: str,
    dimension: SummaryDimension,
    shard: int,
) -> str:
    return deterministic_id(
        "usagesummary", tenant_id, period, period_start, gateway_id, dimension, str(shard)
    )


class AttributionRecord(Entity):
    """What a gateway log's grant key or subscription stands for.

    Copied from a grant's binding whenever a publication applies it, and never deleted: a revoked
    grant's key stays resolvable, so its past calls keep reporting against it. ``revoked_at``
    records when the binding went away.
    """

    entity_type: Literal["attributionRecord"] = "attributionRecord"
    kind: Literal["grant", "subscription"]
    key: str
    gateway_id: str
    entitlement_id: str
    publication_id: str | None = None
    publication_kind: Literal["model", "mcp"] | None = None
    subject: EntitlementSubject
    subject_object_id: str | None = None
    subject_name: str | None = None
    resource: EntitlementResource
    resource_name: str | None = None
    per_member: bool = False
    # The cost center the grant charges. A grant's cost center never changes, so its calls group
    # by cost center without a key of their own in the trace. The code and name are as they were
    # when the record was last copied; reports name a cost center that still exists by its
    # current name. See ADR 0022.
    cost_center_id: str | None = None
    cost_center_code: str | None = None
    cost_center_name: str | None = None
    recorded_at: datetime
    revoked_at: datetime | None = None
    ttl: int = KEEP_FOREVER


def attribution_record_id(tenant_id: str, kind: str, key: str) -> str:
    return deterministic_id("attributionrecord", tenant_id, kind, key)


def subscription_attribution_key(gateway_id: str, subscription: str) -> str:
    """The registry key for an API Management subscription, which is only unique per gateway."""

    name = subscription.rstrip("/").rsplit("/", 1)[-1]
    return f"{gateway_id}/{name.casefold()}"


class RolledUpApi(MosaicModel):
    """A governed API on a gateway, remembered after MOSAIC stops governing it.

    MOSAIC reads the telemetry of every model API and MCP server it governs, whether it published
    them or adopted them. Calls to one since removed are still counted while their day is
    re-aggregated, and still named in the views that show them.
    """

    api_name: str
    # The governed model API or MCP server record, which is what grants name.
    resource_id: str
    publication_id: str | None = None
    kind: Literal["model", "mcp"]
    display_name: str
    model_endpoint_id: str | None = None
    deployment_name: str | None = None
    # A model publication's own subscription. Everyone given its key calls as the publication, so
    # its calls can't be told apart by caller.
    subscription_name: str | None = None
    first_seen_at: datetime
    removed_at: datetime | None = None

    @property
    def deployment_key(self) -> str | None:
        if self.model_endpoint_id is None or self.deployment_name is None:
            return None
        return f"{self.model_endpoint_id}/{self.deployment_name}"


class UsageRollupState(Entity):
    """How far one gateway's telemetry has been rolled up, and what went wrong last."""

    entity_type: Literal["usageRollupState"] = "usageRollupState"
    gateway_id: str
    last_run_at: datetime | None = None
    last_success_at: datetime | None = None
    last_duration_ms: int | None = None
    queried_through: datetime | None = None
    data_available_from: str | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    apis: list[RolledUpApi] = Field(default_factory=list)
    # API names MOSAIC has confirmed log to Azure Monitor at Information.
    instrumented_apis: list[str] = Field(default_factory=list)
    diagnostics_error: str | None = None
    backfill_from: str | None = None
    backfill_next: str | None = None
    backfill_status: Literal["idle", "running", "done", "failed"] = "idle"
    refresh_requested_at: datetime | None = None
    last_rows: int = 0
    last_written: int = 0
    unknown_trace_versions: int = 0
    ttl: int = KEEP_FOREVER


def usage_rollup_state_id(tenant_id: str, gateway_id: str) -> str:
    return deterministic_id("usagerollupstate", tenant_id, gateway_id)


def content_hash(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def iso_day(value: date) -> str:
    return value.isoformat()


def month_start(value: date) -> date:
    return date(value.year, value.month, 1)
