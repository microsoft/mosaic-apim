"""What the administrator analytics routes return. See ADR 0019.

Every figure comes from the usage rollups: nothing here is estimated or simulated. A bucket MOSAIC
has no rolled-up telemetry for reports None rather than zero, so a chart shows a gap instead of a
quiet day that never happened.
"""

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from pydantic import Field

from mosaic_api.domain import (
    EntitlementResourceKind,
    EntitlementSubjectKind,
    MosaicModel,
    PrincipalKind,
    QuotaPeriod,
)
from mosaic_api.services.usage import FreshnessStatus, Metric, UsageFreshness

AnalyticsRange = Literal["24h", "7d", "30d", "90d", "12m", "custom"]
Granularity = Literal["hour", "day", "month"]
AnalyticsDataSource = Literal["logAnalytics", "notConfigured"]
ExportView = Literal[
    "trend",
    "people",
    "applications",
    "groups",
    "grants",
    "clientApps",
    "apis",
    "models",
    "deployments",
    "denials",
    "limits",
    "unusedGrants",
    "unusedKeys",
    "untrackedGrants",
    "unattributed",
]
ConsumerKind = Literal["person", "application", "group"]
GrantState = Literal["active", "disabled", "removed"]
LimitStatus = Literal["ok", "near", "reached", "unknown"]
UntrackedReason = Literal["mosaicGroup", "notApplied", "noLink"]
# sharedKey: the call used a model publication's own subscription, whose key isn't any one caller's.
UnattributedReason = Literal["noSubscription", "unknownSubscription", "sharedKey"]
BackfillStatus = Literal["idle", "running", "done", "failed"]


@dataclass(frozen=True)
class AnalyticsFilters:
    """What an analytics request is narrowed to. Every filter is optional."""

    range: AnalyticsRange = "30d"
    start: date | None = None
    end: date | None = None
    gateway_id: str | None = None
    environment: str | None = None
    # A governed model API or MCP server, by its record ID or its publication's.
    resource_id: str | None = None
    # Narrows callers and grants, which carry a subject. API-level totals include every caller.
    subject_kind: EntitlementSubjectKind | None = None


class AnalyticsWindow(MosaicModel):
    range: AnalyticsRange
    granularity: Granularity
    # Inclusive start and exclusive end.
    start: datetime
    end: datetime
    # The whole UTC days, or months, that breakdowns cover. For the last 24 hours these are the
    # days the 24 hours touch, because only totals are kept by the hour.
    breakdown_start: date
    breakdown_end: date
    previous_start: datetime
    previous_end: datetime


class AnalyticsKpis(MosaicModel):
    """Headline figures. Hour-grained windows can't split some of them, which are then None."""

    requests: int
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int
    ok: int
    throttled: int
    quota_refused: int
    denied: int
    errors: int
    client_errors: int | None
    server_errors: int | None
    backend_throttled: int | None
    success_rate: float | None
    error_rate: float | None
    throttle_rate: float | None
    denial_rate: float | None
    p50_latency_ms: float | None
    p95_latency_ms: float | None
    average_latency_ms: float | None
    average_backend_ms: float | None
    active_callers: int | None
    active_grants: int | None
    active_apis: int | None
    unattributed_requests: int | None


class AnalyticsTrendPoint(MosaicModel):
    start: datetime
    requests: int | None
    total_tokens: int | None
    throttled: int | None
    quota_refused: int | None
    denied: int | None
    errors: int | None


class AnalyticsSeries(MosaicModel):
    key: str
    label: str
    # One value per trend bucket, in the same order.
    values: list[int | None]


class AnalyticsRankRow(MosaicModel):
    key: str
    label: str
    detail: str | None = None
    requests: int
    total_tokens: int
    request_share: float | None
    token_share: float | None


class AnalyticsGatewayHealth(MosaicModel):
    gateway_id: str
    name: str
    environment: str | None
    environment_name: str
    status: FreshnessStatus
    governed_apis: int
    instrumented_apis: int
    last_run_at: datetime | None
    last_success_at: datetime | None
    queried_through: datetime | None
    lag_minutes: int | None
    data_available_from: str | None
    last_error: str | None
    last_error_at: datetime | None
    backfill_status: BackfillStatus
    backfill_from: str | None
    backfill_next: str | None
    unknown_trace_versions: int
    diagnostics_error: str | None


class AnalyticsReport(MosaicModel):
    data_source: AnalyticsDataSource
    generated_at: datetime
    window: AnalyticsWindow
    freshness: UsageFreshness
    notes: list[str]


class AnalyticsOverview(AnalyticsReport):
    kpis: AnalyticsKpis
    # The equal-length window just before, or None when MOSAIC has no figures for all of it.
    previous: AnalyticsKpis | None
    trend: list[AnalyticsTrendPoint]
    # Tokens per bucket for the busiest models. Empty for the last 24 hours.
    model_trend: list[AnalyticsSeries]
    top_models: list[AnalyticsRankRow]
    top_callers: list[AnalyticsRankRow]
    top_apis: list[AnalyticsRankRow]
    gateways: list[AnalyticsGatewayHealth]


class AnalyticsUsage(MosaicModel):
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    throttled: int = 0
    quota_refused: int = 0
    errors: int = 0
    last_seen: datetime | None = None
    request_share: float | None = None
    token_share: float | None = None


class AnalyticsConsumerRow(AnalyticsUsage):
    key: str
    kind: ConsumerKind
    label: str
    detail: str | None = None
    principal_id: str | None = None
    # What the Entra object is. Agent users sign in as users, so they are counted with people,
    # and this tells them apart. None for a MOSAIC group or a caller MOSAIC couldn't name.
    principal_kind: PrincipalKind | None = None
    grants: int = 0
    resources: int = 0
    # Security groups only: the members MOSAIC saw calling.
    members: int | None = None


class AnalyticsGrantRow(AnalyticsUsage):
    key: str
    entitlement_id: str | None
    state: GrantState
    subject_kind: EntitlementSubjectKind | None
    subject_label: str
    subject_detail: str | None = None
    # What a person or application subject is in Entra, such as an agent user.
    subject_principal_kind: PrincipalKind | None = None
    resource_kind: EntitlementResourceKind | None
    resource_label: str
    gateway_id: str | None
    gateway_name: str | None
    callers: int
    key_requests: int
    peak_minute_tokens: int
    peak_minute_requests: int


class AnalyticsClientAppRow(AnalyticsUsage):
    client_app_id: str
    label: str
    principal_id: str | None = None
    apis: int


class AnalyticsConsumers(AnalyticsReport):
    # Admitted calls MOSAIC linked to a grant, which every share on this page is a share of.
    linked_requests: int
    linked_tokens: int
    # Linked calls MOSAIC couldn't tie to one person or application.
    unidentified_requests: int
    people: list[AnalyticsConsumerRow]
    applications: list[AnalyticsConsumerRow]
    groups: list[AnalyticsConsumerRow]
    grants: list[AnalyticsGrantRow]
    client_apps: list[AnalyticsClientAppRow]
    truncated: bool


class AnalyticsApiRow(AnalyticsUsage):
    key: str
    gateway_id: str
    gateway_name: str
    api_name: str
    label: str
    kind: Literal["model", "mcp"] | None
    resource_id: str | None
    removed: bool
    metered_requests: int
    denied: int
    client_errors: int
    server_errors: int
    backend_throttled: int
    p95_latency_ms: float | None
    average_latency_ms: float | None
    error_rate: float | None
    # The models the API's calls were served by. None where a report doesn't read models.
    models: list[str] | None


class AnalyticsModelRow(AnalyticsUsage):
    model: str
    apis: int


class AnalyticsDeploymentRow(AnalyticsUsage):
    key: str
    endpoint_id: str
    endpoint_name: str | None
    deployment_name: str
    model_name: str | None
    sku_name: str | None
    # Azure OpenAI standard deployments only. Provisioned capacity is measured in PTUs, and other
    # providers' models are held to a regional rate limit rather than their deployment's capacity.
    capacity_tokens_per_minute: int | None
    backend_throttled: int
    peak_minute_tokens: int
    peak_minute_requests: int
    utilization: float | None
    gateways: int
    # The busiest minute in each UTC hour of the day across the window. Daily windows only.
    hourly_peak_tokens: list[int] | None


class AnalyticsBreakdownRow(AnalyticsUsage):
    key: str
    label: str
    denied: int


class AnalyticsModels(AnalyticsReport):
    apis: list[AnalyticsApiRow]
    models: list[AnalyticsModelRow]
    deployments: list[AnalyticsDeploymentRow]
    gateways: list[AnalyticsBreakdownRow]
    environments: list[AnalyticsBreakdownRow]


class AnalyticsStatusMix(MosaicModel):
    requests: int
    ok: int
    throttled: int
    quota_refused: int
    denied: int
    client_errors: int
    server_errors: int
    backend_throttled: int


class AnalyticsLatencyBucket(MosaicModel):
    # None for the open-ended last bucket.
    upper_ms: int | None
    count: int


class AnalyticsLatency(MosaicModel):
    p50_ms: float | None
    p95_ms: float | None
    p99_ms: float | None
    average_ms: float | None
    average_backend_ms: float | None
    buckets: list[AnalyticsLatencyBucket]


class AnalyticsDenialReason(MosaicModel):
    reason: str
    label: str
    requests: int
    share: float | None


class AnalyticsDenialRow(MosaicModel):
    reason: str
    reason_label: str
    caller_object_id: str | None
    caller_label: str | None
    client_app_id: str | None
    client_app_label: str | None
    gateway_id: str
    gateway_name: str
    api_name: str
    api_label: str
    requests: int
    last_seen: datetime | None


class AnalyticsReliability(AnalyticsReport):
    status_mix: AnalyticsStatusMix
    latency: AnalyticsLatency
    trend: list[AnalyticsTrendPoint]
    apis: list[AnalyticsApiRow]
    denial_reasons: list[AnalyticsDenialReason]
    denials: list[AnalyticsDenialRow]


class AnalyticsLimitUse(MosaicModel):
    kind: Literal["quota", "rateLimit"]
    metric: Metric
    limit: int
    period: QuotaPeriod | None = None
    window_seconds: int | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    # A quota's use so far in its current window, or a rate limit's busiest minute in the
    # analysis window. None when MOSAIC can't tell.
    used: float | None
    utilization: float | None
    partial: bool = False


class AnalyticsLimitRow(MosaicModel):
    key: str
    entitlement_id: str
    subject_kind: EntitlementSubjectKind
    subject_label: str
    subject_detail: str | None
    subject_principal_kind: PrincipalKind | None = None
    # Security-group grants count each member separately, so each member gets a row.
    member_object_id: str | None
    member_label: str | None
    resource_kind: EntitlementResourceKind
    resource_label: str
    gateway_id: str | None
    gateway_name: str | None
    limits: list[AnalyticsLimitUse]
    utilization: float | None
    status: LimitStatus
    throttled: int
    quota_refused: int


class AnalyticsLimits(AnalyticsReport):
    threshold: float
    near: int
    reached: int
    rows: list[AnalyticsLimitRow]
    truncated: bool


class AnalyticsGrantRef(MosaicModel):
    entitlement_id: str
    subject_kind: EntitlementSubjectKind
    subject_label: str
    subject_detail: str | None
    subject_principal_kind: PrincipalKind | None = None
    resource_kind: EntitlementResourceKind
    resource_label: str
    gateway_id: str | None
    gateway_name: str | None


class AnalyticsUnusedGrant(AnalyticsGrantRef):
    granted_at: datetime
    last_used_at: datetime | None


class AnalyticsUnusedKey(AnalyticsGrantRef):
    subscription_name: str | None
    token_requests: int


class AnalyticsUntrackedGrant(AnalyticsGrantRef):
    reason: UntrackedReason


class AnalyticsHygiene(AnalyticsReport):
    # Grants whose gateway MOSAIC has rolled up for the whole window, so no use means none.
    judged_grants: int
    unused_grants: list[AnalyticsUnusedGrant]
    unused_keys: list[AnalyticsUnusedKey]
    denied_callers: list[AnalyticsDenialRow]
    untracked_grants: list[AnalyticsUntrackedGrant]
    truncated: bool


class AnalyticsUnattributedRow(MosaicModel):
    gateway_id: str
    gateway_name: str
    api_name: str
    api_label: str
    subscription: str | None
    reason: UnattributedReason
    requests: int
    total_tokens: int
    last_seen: datetime | None
    share: float | None


class AnalyticsUnattributed(AnalyticsReport):
    requests: int
    total_tokens: int
    # Calls the gateway let through, which unattributed calls are a share of.
    admitted_requests: int
    share: float | None
    rows: list[AnalyticsUnattributedRow]
    truncated: bool


class AnalyticsStatus(MosaicModel):
    data_source: AnalyticsDataSource
    rollups_enabled: bool
    generated_at: datetime
    freshness: UsageFreshness
    gateways: list[AnalyticsGatewayHealth]


class TelemetryBackfillRequest(MosaicModel):
    # Days of history to re-read from Log Analytics, ending yesterday. None uses the default.
    days: int | None = Field(default=None, ge=1, le=730)
