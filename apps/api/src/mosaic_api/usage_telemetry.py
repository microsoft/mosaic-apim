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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
# - `deployment`: `{modelEndpointId}/{deploymentName}`, for published model APIs and for each
#   model pool member that served calls;
# - `model`: `{model}|{api}`, the model name the LLM log reported, lowercased;
# - `denial`: `{reason}|{caller}|{clientApp}|{api}` for refused calls;
# - `unattributed`: `{api}|{subscription}` for admitted calls MOSAIC could not link.
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
)
# A summary item holds at most this many entries; larger dimensions are split into shards so no
# document nears Cosmos' 2 MB item limit. An entry that splits a pool's calls by member counts as
# more than one; see `summary_entry_weight`.
SUMMARY_SHARD_SIZE = 1000
# How many member splits weigh as much as one entry's own figures.
MEMBERS_PER_ENTRY = 4


def _zero_latency() -> list[int]:
    return [0] * LATENCY_BUCKET_COUNT


class MemberUsage(MosaicModel):
    """The calls one model pool member served, out of a pool API's figures.

    Never changed in place once made, so figures that share one stay correct.
    """

    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def plus(self, other: "MemberUsage") -> "MemberUsage":
        return MemberUsage(
            requests=self.requests + other.requests,
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )


def _no_members(value: object) -> bool:
    return not value


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
    # Model pool calls only: the calls each member deployment served, keyed
    # `{modelEndpointId}/{deploymentName}` like the deployment dimension, so they can be priced at
    # the member's own price. Calls MOSAIC couldn't place on a member count in the figures above
    # and in no member. Left out of stored documents when empty, so other figures hash as before.
    members: dict[str, MemberUsage] | None = Field(default=None, exclude_if=_no_members)

    @property
    def errors(self) -> int:
        return self.client_errors + self.server_errors

    def without_members(self) -> "UsageMetrics":
        """A copy without the member split, for figures that don't price calls."""

        return self.model_copy(update={"members": None})

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
        if other.members:
            members = dict(self.members or {})
            for key, usage in other.members.items():
                current = members.get(key)
                members[key] = usage if current is None else current.plus(usage)
            self.members = members

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


def summary_entry_weight(entry: UsageSummaryEntry) -> int:
    """How many entries' worth of room one entry takes in a summary shard."""

    members = len(entry.metrics.members or ())
    return 1 + -(-members // MEMBERS_PER_ENTRY)


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
    publication_kind: Literal["model", "mcp", "pool"] | None = None
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


class RolledUpMember(MosaicModel):
    """One deployment a model pool's API sends calls to, as gateway logs can name it."""

    model_endpoint_id: str
    deployment_name: str
    # The API Management backend MOSAIC wrote for the member.
    backend_name: str
    # The host the member's backend calls, lowercased, or "" when MOSAIC can't tell.
    host: str = ""
    # When MOSAIC first saw the member behind the pool, which is when its share of a provisioned
    # deployment starts.
    first_seen_at: datetime | None = None

    @property
    def deployment_key(self) -> str:
        return f"{self.model_endpoint_id}/{self.deployment_name}"


class RolledUpApi(MosaicModel):
    """A governed API on a gateway, remembered after MOSAIC stops governing it.

    MOSAIC reads the telemetry of every model API, model pool and MCP server it governs, whether
    it published them or adopted them. Calls to one since removed are still counted while their
    day is re-aggregated, and still named in the views that show them.
    """

    api_name: str
    # The governed model API, model pool or MCP server record. Grants name a model API or MCP
    # server by this ID, and a pool's models by it as their scope.
    resource_id: str
    publication_id: str | None = None
    kind: Literal["model", "mcp", "pool"]
    display_name: str
    model_endpoint_id: str | None = None
    deployment_name: str | None = None
    # A model publication's own subscription. Everyone given its key calls as the publication, so
    # its calls can't be told apart by caller.
    subscription_name: str | None = None
    # A model pool's member deployments, every model's, including members since removed, so calls
    # can still be priced at the deployment that served them.
    members: list[RolledUpMember] = Field(default_factory=list, exclude_if=_no_members)
    first_seen_at: datetime
    removed_at: datetime | None = None

    @property
    def deployment_key(self) -> str | None:
        if self.model_endpoint_id is None or self.deployment_name is None:
            return None
        return f"{self.model_endpoint_id}/{self.deployment_name}"

    @property
    def is_llm(self) -> bool:
        """Whether the API's calls reach a language model, so carry token counts."""

        return self.kind != "mcp"

    def deployment_keys(self) -> list[str]:
        """The deployments the API's calls reach, keyed as rollups key them."""

        if self.kind == "pool":
            return list(dict.fromkeys(member.deployment_key for member in self.members))
        key = self.deployment_key
        return [key] if key else []

    def endpoint_ids(self) -> set[str]:
        """The model endpoints the API's calls reach."""

        if self.kind == "pool":
            return {member.model_endpoint_id for member in self.members}
        return {self.model_endpoint_id} if self.model_endpoint_id else set()


@dataclass(frozen=True)
class PoolMembers:
    """Which member deployment served a call to a model pool's API.

    API Management picks the member, so the attribution trace can't name it. The gateway log
    can, in up to three ways, tried most precise first: the backend that served the call, which is
    one MOSAIC wrote for a single member; the host and deployment its backend URL called; and the
    host alone or the deployment alone, when only one member has it. A call none of these places
    stays off every member, so it's never priced at the wrong deployment's price.
    """

    by_backend: Mapping[str, str]
    by_route: Mapping[tuple[str, str], str]
    by_host: Mapping[str, frozenset[str]]
    by_deployment: Mapping[str, frozenset[str]]

    @classmethod
    def of(cls, api: RolledUpApi) -> "PoolMembers":
        by_backend: dict[str, str] = {}
        by_route: dict[tuple[str, str], str] = {}
        by_host: dict[str, set[str]] = {}
        by_deployment: dict[str, set[str]] = {}
        for member in api.members:
            key = member.deployment_key
            deployment = member.deployment_name.casefold()
            by_backend[member.backend_name.casefold()] = key
            by_deployment.setdefault(deployment, set()).add(key)
            host = member.host.casefold()
            if host:
                by_route.setdefault((host, deployment), key)
                by_host.setdefault(host, set()).add(key)
        return cls(
            by_backend=by_backend,
            by_route=by_route,
            by_host={host: frozenset(keys) for host, keys in by_host.items()},
            by_deployment={name: frozenset(keys) for name, keys in by_deployment.items()},
        )

    def member_for(self, backend_id: str, host: str, deployment: str) -> str | None:
        """The deployment key of the member that served a call, or None when it's unclear."""

        key = self.by_backend.get(backend_id.casefold()) if backend_id else None
        if key is not None:
            return key
        host = host.casefold()
        deployment = deployment.casefold()
        if host in self.by_host:
            routed = self.by_route.get((host, deployment)) if deployment else None
            if routed is not None:
                return routed
            return only_member(self.by_host[host])
        return only_member(self.by_deployment.get(deployment, frozenset())) if deployment else None


def only_member(keys: frozenset[str]) -> str | None:
    """The one deployment key in ``keys``, or None when there are none or several."""

    return next(iter(keys)) if len(keys) == 1 else None


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
