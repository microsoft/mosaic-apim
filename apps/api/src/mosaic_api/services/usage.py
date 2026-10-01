from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol

from pydantic import Field

from mosaic_api.domain import (
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementResourceKind,
    MosaicModel,
    QuotaPeriod,
    ResolvedEntitlement,
    ResourceSummary,
)
from mosaic_api.environments import EnvironmentCatalog, is_production
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import (
    Pricer,
    deployment_key,
    month_first,
    token_amount,
)
from mosaic_api.repositories import (
    EnvironmentRepository,
    GatewayRepository,
    ModelEndpointRepository,
    UsageRollupRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import load_environment_catalog
from mosaic_api.services.portal import PortalService
from mosaic_api.services.pricing import PricingService
from mosaic_api.usage_telemetry import (
    UsageFact,
    UsageHour,
    UsageRollupState,
    iso_day,
    rollup_stale_after,
    subscription_attribution_key,
)

UsagePeriod = Literal["7d", "30d", "90d"]
UsageDataSource = Literal["simulated", "logAnalytics"]
UsageAttribution = Literal["simulated", "measured", "unattributed"]
Metric = Literal["tokens", "requests"]
FreshnessStatus = Literal["current", "delayed", "failing", "pending", "notLinked"]

_PERIOD_DAYS: Mapping[UsagePeriod, int] = {"7d": 7, "30d": 30, "90d": 90}
_PERIOD_ORDER: Mapping[QuotaPeriod, int] = {
    "Hourly": 0,
    "Daily": 1,
    "Weekly": 2,
    "Monthly": 3,
    "Yearly": 4,
}

# Illustrative prices only: USD per 1K prompt/input and completion/output tokens.
_ILLUSTRATIVE_MODEL_RATES: Mapping[str, tuple[float, float]] = {
    "text-embedding-3-large": (0.00013, 0.0),
    "text-embedding-3-small": (0.00002, 0.0),
    "gpt-4o-mini": (0.00015, 0.0006),
    "gpt-4.1-mini": (0.0004, 0.0016),
    "gpt-35-turbo": (0.0005, 0.0015),
    "gpt-4.1": (0.002, 0.008),
    "gpt-4o": (0.0025, 0.01),
    "o3-mini": (0.0011, 0.0044),
    "o4-mini": (0.0011, 0.0044),
}


class UsageTotals(MosaicModel):
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float | None
    cost_excluded_resources: int
    # Measured usage only; None when the figures are simulated.
    throttled: int | None = None
    quota_refused: int | None = None
    errors: int | None = None
    last_used_at: datetime | None = None


class UsageTimelinePoint(MosaicModel):
    date: str
    entitlement_id: str
    environment: str | None
    requests: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    # Measured usage only. Throttled calls hit a rate limit, quota-refused calls found the quota
    # spent, and errors are other 4xx and 5xx responses. Peaks are the day's busiest minute.
    throttled: int | None = None
    quota_refused: int | None = None
    errors: int | None = None
    peak_minute_tokens: int | None = None
    peak_minute_requests: int | None = None


class UsageHourPoint(MosaicModel):
    """One UTC hour of measured usage. Figures are None when MOSAIC has no data for the hour."""

    hour: datetime
    requests: int | None
    total_tokens: int | None
    throttled: int | None
    quota_refused: int | None
    errors: int | None
    peak_minute_tokens: int | None
    peak_minute_requests: int | None


class UsageEnvironmentBreakdown(MosaicModel):
    environment: str | None
    resources: int
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float | None
    cost_excluded_resources: int
    # Resources whose usage MOSAIC can't measure. The figures above leave them out.
    unmeasured_resources: int = 0


class UsageQuota(MosaicModel):
    metric: Metric
    limit: int
    period: QuotaPeriod
    window_start: datetime
    window_end: datetime
    used: float | None
    utilization: float | None
    # True when MOSAIC has no figures for part of the window, so ``used`` is a lower bound.
    partial: bool = False


class UsageRateLimit(MosaicModel):
    metric: Metric
    limit: int
    window_seconds: int
    # Measured usage only: the busiest minute in the report period, when the limit is per minute.
    peak: int | None = None
    utilization: float | None = None


class UsageResourceRow(MosaicModel):
    entitlement_id: str
    resource: EntitlementResource
    resource_summary: ResourceSummary
    environment: str | None
    via: Literal["direct", "group", "securityGroup"]
    via_group_name: str | None
    enabled: bool
    bound: bool
    linked_by: Literal["gatewayLog", "subscription"] | None
    attribution: UsageAttribution
    model: str | None
    requests: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None
    cost_note: str | None
    quotas: list[UsageQuota]
    rate_limits: list[UsageRateLimit]
    # Measured usage only.
    throttled: int | None = None
    quota_refused: int | None = None
    errors: int | None = None
    peak_minute_tokens: int | None = None
    peak_minute_requests: int | None = None
    last_used_at: datetime | None = None
    recent_hours: list[UsageHourPoint] = Field(default_factory=list)


class UsageFreshness(MosaicModel):
    """How current measured usage is, across the gateways the caller's grants are on."""

    status: FreshnessStatus
    # The oldest of those gateways' last successful rollups.
    updated_at: datetime | None
    # The first day every one of those gateways has figures for.
    data_from: str | None
    gateways: int
    interval_minutes: int


class MyUsageReport(MosaicModel):
    data_source: UsageDataSource
    period: UsagePeriod
    start: str
    end: str
    generated_at: datetime
    currency: Literal["USD"]
    totals: UsageTotals
    timeline: list[UsageTimelinePoint]
    by_environment: list[UsageEnvironmentBreakdown]
    by_resource: list[UsageResourceRow]
    notes: list[str]
    # Measured usage only: how current the figures are, and the last 24 hours across every grant.
    freshness: UsageFreshness | None = None
    recent_hours: list[UsageHourPoint] = Field(default_factory=list)


@dataclass
class DailyUsage:
    requests: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    # The measured total, when the gateway log reports one; otherwise prompt plus completion.
    tokens: int | None = None
    throttled: int | None = None
    quota_refused: int | None = None
    errors: int | None = None
    peak_minute_tokens: int | None = None
    peak_minute_requests: int | None = None
    hours: dict[int, UsageHour] = field(default_factory=dict)
    last_seen: datetime | None = None

    @property
    def total_tokens(self) -> int | None:
        if self.tokens is not None:
            return self.tokens
        if self.prompt_tokens is None or self.completion_tokens is None:
            return None
        return self.prompt_tokens + self.completion_tokens


UsageSeries = Mapping[str, Mapping[date, DailyUsage] | None]


class UsageSource(Protocol):
    data_source: UsageDataSource

    async def daily_usage(
        self,
        actor: Actor,
        entitlements: Sequence[ResolvedEntitlement],
        *,
        start: date,
        end: date,
    ) -> UsageSeries:
        """Each grant's usage by day, or None for a grant MOSAIC can't link to telemetry.

        A day missing from a grant's series is one MOSAIC has no figures for.
        """
        ...

    async def freshness(
        self, actor: Actor, entitlements: Sequence[ResolvedEntitlement]
    ) -> UsageFreshness | None: ...


class SimulatedUsageSource:
    data_source: UsageDataSource = "simulated"

    def __init__(
        self,
        *,
        environment_repository: EnvironmentRepository,
    ) -> None:
        self._environments = environment_repository

    async def daily_usage(
        self,
        actor: Actor,
        entitlements: Sequence[ResolvedEntitlement],
        *,
        start: date,
        end: date,
    ) -> UsageSeries:
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        return {
            item.entitlement.id: self._series_for(actor, item, catalog, start=start, end=end)
            for item in entitlements
        }

    async def freshness(
        self, actor: Actor, entitlements: Sequence[ResolvedEntitlement]
    ) -> UsageFreshness | None:
        return None

    def _series_for(
        self,
        actor: Actor,
        resolved: ResolvedEntitlement,
        catalog: EnvironmentCatalog,
        *,
        start: date,
        end: date,
    ) -> Mapping[date, DailyUsage]:
        entitlement = resolved.entitlement
        summary = _summary_for(resolved)
        is_mcp = entitlement.resource.kind == EntitlementResourceKind.MCP_SERVER
        # Quotas cap each whole window, so every window a report touches is simulated in full and
        # trimmed afterwards. A day then shows the same figures whichever period includes it.
        first, last = _generation_range(entitlement, start, end)
        series: dict[date, DailyUsage] = {}
        for day in _days(first, last):
            if not entitlement.enabled or day < entitlement.created_at.astimezone(UTC).date():
                series[day] = _zero_usage(is_mcp)
                continue
            requests, prompt, completion = self._raw_day(
                actor, entitlement, summary.environment, catalog, day, is_mcp=is_mcp
            )
            series[day] = DailyUsage(
                requests=requests,
                prompt_tokens=None if is_mcp else prompt,
                completion_tokens=None if is_mcp else completion,
            )

        self._apply_rate_caps(entitlement, series, is_mcp=is_mcp)
        self._apply_quota_caps(entitlement, series, is_mcp=is_mcp)
        return {day: usage for day, usage in series.items() if start <= day <= end}

    def _raw_day(
        self,
        actor: Actor,
        entitlement: Entitlement,
        environment: str | None,
        catalog: EnvironmentCatalog,
        day: date,
        *,
        is_mcp: bool,
    ) -> tuple[int, int, int]:
        digest = _digest(actor, entitlement.id, day)
        volume = _environment_multiplier(catalog, environment)
        if day.weekday() >= 5:
            volume *= 0.62
        request_seed = _fraction(digest[:8])
        token_seed = _fraction(digest[8:16])
        completion_seed = _fraction(digest[16:24])
        base_requests = 22 if is_mcp else 38
        requests = max(1, int(base_requests * volume * (0.55 + request_seed * 1.35)))
        if is_mcp:
            return requests, 0, 0
        prompt_per_request = int(520 + token_seed * 1480)
        completion_per_request = int(160 + completion_seed * 680)
        return requests, requests * prompt_per_request, requests * completion_per_request

    def _apply_rate_caps(
        self, entitlement: Entitlement, series: dict[date, DailyUsage], *, is_mcp: bool
    ) -> None:
        enforcement = entitlement.enforcement
        if enforcement is None:
            return
        if not is_mcp and enforcement.tokens and enforcement.tokens.tokens_per_minute:
            daily_cap = int(enforcement.tokens.tokens_per_minute * 1440 * 0.12)
            for usage in series.values():
                _scale_tokens_to(usage, daily_cap)
        if (
            enforcement.requests
            and enforcement.requests.calls
            and enforcement.requests.renewal_period_seconds
        ):
            intervals = 86400 / enforcement.requests.renewal_period_seconds
            daily_cap = int(enforcement.requests.calls * intervals * 0.2)
            for usage in series.values():
                _scale_requests_to(usage, daily_cap)

    def _apply_quota_caps(
        self, entitlement: Entitlement, series: dict[date, DailyUsage], *, is_mcp: bool
    ) -> None:
        quotas: list[tuple[Metric, int, QuotaPeriod]] = []
        enforcement = entitlement.enforcement
        if enforcement is None:
            return
        if (
            not is_mcp
            and enforcement.tokens
            and enforcement.tokens.token_quota
            and enforcement.tokens.token_quota_period
        ):
            quotas.append(
                ("tokens", enforcement.tokens.token_quota, enforcement.tokens.token_quota_period)
            )
        if (
            enforcement.requests
            and enforcement.requests.call_quota
            and enforcement.requests.call_quota_period
        ):
            quotas.append(
                (
                    "requests",
                    enforcement.requests.call_quota,
                    enforcement.requests.call_quota_period,
                )
            )

        for metric, limit, period in sorted(quotas, key=lambda item: _PERIOD_ORDER[item[2]]):
            if period == "Hourly":
                cap = limit * 24
                for usage in series.values():
                    _scale_metric_to(usage, metric, cap)
                continue
            if period == "Daily":
                for usage in series.values():
                    _scale_metric_to(usage, metric, limit)
                continue
            grouped: dict[date, list[DailyUsage]] = defaultdict(list)
            for day, usage in series.items():
                grouped[_window_start_for(day, period)].append(usage)
            for usages in grouped.values():
                total = sum(_metric_value(usage, metric) or 0 for usage in usages)
                cap = int(limit * 0.9)
                if total > cap:
                    factor = cap / total
                    for usage in usages:
                        _scale_metric_by(usage, metric, factor)


@dataclass
class _GrantLinks:
    keys: set[str] = field(default_factory=set)
    gateway_ids: set[str] = field(default_factory=set)


MCP_COST_NOTE = "MCP servers are billed by their own service, not by tokens."
PRODUCT_COST_NOTE = "Products bundle several APIs, so MOSAIC can't price them."
UNKNOWN_DEPLOYMENT_NOTE = "MOSAIC doesn't know which deployment this API calls."


@dataclass
class _MeasuredPricing:
    """Prices one person's measured usage, day by day, at the price in effect each day."""

    pricer: Pricer
    # Each grant's deployment, keyed as rollups key it, and its model.
    deployments: dict[str, tuple[str | None, str | None]]
    # Each provisioned deployment's tokens by month, across every caller.
    tokens: dict[tuple[str, date], int]
    today: date
    reserved: bool = False

    def cost(self, key: str | None, day: date, usage: DailyUsage) -> float | None:
        rate = self.pricer.rate(key, day)
        prompt = usage.prompt_tokens or 0
        completion = usage.completion_tokens or 0
        total = usage.total_tokens or 0
        if rate.kind == "tokens" and rate.entry is not None:
            return token_amount(rate.entry, prompt, completion)
        if rate.kind == "provisioned" and key is not None:
            # A provisioned deployment's month is shared by its callers' share of its tokens.
            self.reserved = True
            if total <= 0:
                return 0.0
            month = month_first(day)
            monthly = self.pricer.reserved_cost(key, month, self.today)
            shared = max(self.tokens.get((key.casefold(), month), 0), total)
            return None if monthly is None else monthly * total / shared
        return None

    def note(
        self,
        entitlement: Entitlement,
        key: str | None,
        series: Mapping[date, DailyUsage] | None,
        start: date,
        end: date,
    ) -> str | None:
        """Why a grant has no cost in the period, or None when some of it can be priced."""

        kind = entitlement.resource.kind
        if kind == EntitlementResourceKind.MCP_SERVER:
            return MCP_COST_NOTE
        if kind == EntitlementResourceKind.PRODUCT:
            return PRODUCT_COST_NOTE
        if key is None:
            return UNKNOWN_DEPLOYMENT_NOTE
        if series is None:
            return "MOSAIC can't measure this grant's usage yet, so it can't price it."
        used = [
            day
            for day, usage in series.items()
            if start <= day <= end and (usage.total_tokens or 0) > 0
        ]
        for day in used or [end]:
            if self.pricer.rate(key, day).kind != "unpriced":
                return None
        rate = self.pricer.rate(key, (used or [end])[-1])
        return rate.unpriced.message if rate.unpriced else "MOSAIC has no price for this model."


def binding_link_keys(binding: EntitlementBinding) -> list[str]:
    """The keys a binding links gateway calls to its grant by, as facts record them."""

    keys: list[str] = []
    if binding.attribution_key:
        keys.append(binding.attribution_key.casefold())
    if binding.apim_subscription_name:
        keys.append(
            subscription_attribution_key(binding.gateway_id, binding.apim_subscription_name)
        )
    return keys


def usage_freshness(
    states: Sequence[UsageRollupState],
    *,
    gateways: int,
    now: datetime,
    interval: timedelta,
) -> UsageFreshness:
    """How current rolled-up usage is across some gateways, judged by their slowest."""

    interval_minutes = max(1, int(interval.total_seconds() // 60))
    if gateways == 0:
        return UsageFreshness(
            status="notLinked",
            updated_at=None,
            data_from=None,
            gateways=0,
            interval_minutes=interval_minutes,
        )
    succeeded = [state for state in states if state.last_success_at is not None]
    if not succeeded:
        # A gateway whose first rollup failed is failing rather than pending, so the reason shows.
        failed = any(state.last_error_at is not None for state in states)
        return UsageFreshness(
            status="failing" if failed else "pending",
            updated_at=None,
            data_from=None,
            gateways=gateways,
            interval_minutes=interval_minutes,
        )
    updated_at = min(state.last_success_at for state in succeeded if state.last_success_at)
    starts = [state.data_available_from for state in succeeded if state.data_available_from]
    status: FreshnessStatus = "current"
    if any(
        state.last_error_at is not None
        and (state.last_success_at is None or state.last_error_at > state.last_success_at)
        for state in states
    ):
        status = "failing"
    elif len(succeeded) < gateways or now - updated_at > rollup_stale_after(interval):
        status = "delayed"
    return UsageFreshness(
        status=status,
        updated_at=updated_at,
        data_from=max(starts) if starts else None,
        gateways=gateways,
        interval_minutes=interval_minutes,
    )


class RollupUsageSource:
    """Measured usage, read from the ``usage-rollups`` container. See ADR 0019.

    A grant's calls are found by every key that ever linked them to it: the grant keys and
    subscriptions its bindings have carried, from the attribution registry, plus its current
    binding's, in case the registry hasn't caught up yet. A security-group grant is shared by its
    members, so only the calls the caller made count here.

    A day is in a grant's series once its gateway's calls for that day have been rolled up; days
    before that, or after the last successful rollup, are ones MOSAIC has no figures for.
    """

    data_source: UsageDataSource = "logAnalytics"

    def __init__(
        self,
        repository: UsageRollupRepository,
        *,
        interval_seconds: float = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._interval = timedelta(seconds=interval_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _links(
        self, tenant_id: str, entitlements: Sequence[ResolvedEntitlement]
    ) -> dict[str, _GrantLinks]:
        wanted = {item.entitlement.id for item in entitlements}
        links: dict[str, _GrantLinks] = {}
        for record in await self._repository.list_attribution_records(tenant_id):
            if record.entitlement_id in wanted:
                link = links.setdefault(record.entitlement_id, _GrantLinks())
                link.keys.add(record.key)
                link.gateway_ids.add(record.gateway_id)
        for item in entitlements:
            binding = item.entitlement.binding
            keys = binding_link_keys(binding) if binding else []
            if binding is not None and keys:
                link = links.setdefault(item.entitlement.id, _GrantLinks())
                link.keys.update(keys)
                link.gateway_ids.add(binding.gateway_id)
        return links

    async def _states(self, tenant_id: str) -> dict[str, UsageRollupState]:
        return {
            state.gateway_id: state
            for state in await self._repository.list_rollup_states(tenant_id)
        }

    async def daily_usage(
        self,
        actor: Actor,
        entitlements: Sequence[ResolvedEntitlement],
        *,
        start: date,
        end: date,
    ) -> UsageSeries:
        links = await self._links(actor.tenant_id, entitlements)
        states = await self._states(actor.tenant_id)
        keys = sorted({key for link in links.values() for key in link.keys})
        facts = (
            await self._repository.list_facts(
                actor.tenant_id, start_day=iso_day(start), end_day=iso_day(end), link_keys=keys
            )
            if keys
            else []
        )
        caller = actor.object_id.casefold()
        by_key: dict[str, list[UsageFact]] = defaultdict(list)
        for fact in facts:
            if fact.per_member and (fact.caller_object_id or "").casefold() != caller:
                continue
            by_key[fact.link_key].append(fact)

        series: dict[str, Mapping[date, DailyUsage] | None] = {}
        for item in entitlements:
            entitlement = item.entitlement
            link = links.get(entitlement.id)
            if link is None:
                series[entitlement.id] = None
                continue
            is_mcp = entitlement.resource.kind == EntitlementResourceKind.MCP_SERVER
            days: dict[date, DailyUsage] = {}
            covered = _coverage(link.gateway_ids, states)
            if covered is not None:
                for day in _days(max(start, covered[0]), min(end, covered[1])):
                    days[day] = _measured_zero(is_mcp)
            for key in sorted(link.keys):
                for fact in by_key.get(key, []):
                    day = date.fromisoformat(fact.day)
                    usage = days.get(day)
                    if usage is None:
                        usage = days[day] = _measured_zero(is_mcp)
                    _add_fact(usage, fact, is_mcp=is_mcp)
            series[entitlement.id] = dict(sorted(days.items()))
        return series

    async def freshness(
        self, actor: Actor, entitlements: Sequence[ResolvedEntitlement]
    ) -> UsageFreshness:
        links = await self._links(actor.tenant_id, entitlements)
        gateway_ids = {gateway_id for link in links.values() for gateway_id in link.gateway_ids}
        states = await self._states(actor.tenant_id)
        return usage_freshness(
            [states[gateway_id] for gateway_id in sorted(gateway_ids) if gateway_id in states],
            gateways=len(gateway_ids),
            now=self._clock(),
            interval=self._interval,
        )


def _coverage(
    gateway_ids: Iterable[str], states: Mapping[str, UsageRollupState]
) -> tuple[date, date] | None:
    """The days any of these gateways has rolled up, from its earliest through its latest."""

    firsts: list[date] = []
    lasts: list[date] = []
    for gateway_id in gateway_ids:
        state = states.get(gateway_id)
        if (
            state is None
            or state.last_success_at is None
            or state.queried_through is None
            or state.data_available_from is None
        ):
            continue
        firsts.append(date.fromisoformat(state.data_available_from))
        lasts.append(state.queried_through.astimezone(UTC).date())
    if not firsts:
        return None
    return min(firsts), max(lasts)


def _measured_zero(is_mcp: bool) -> DailyUsage:
    return DailyUsage(
        requests=0,
        prompt_tokens=None if is_mcp else 0,
        completion_tokens=None if is_mcp else 0,
        tokens=None if is_mcp else 0,
        throttled=0,
        quota_refused=0,
        errors=0,
        peak_minute_tokens=None if is_mcp else 0,
        peak_minute_requests=0,
    )


def _add_fact(usage: DailyUsage, fact: UsageFact, *, is_mcp: bool) -> None:
    metrics = fact.metrics
    usage.requests = (usage.requests or 0) + metrics.requests
    if not is_mcp:
        usage.prompt_tokens = (usage.prompt_tokens or 0) + metrics.prompt_tokens
        usage.completion_tokens = (usage.completion_tokens or 0) + metrics.completion_tokens
        usage.tokens = (usage.tokens or 0) + metrics.total_tokens
        usage.peak_minute_tokens = max(usage.peak_minute_tokens or 0, metrics.peak_minute_tokens)
    usage.throttled = (usage.throttled or 0) + metrics.throttled
    usage.quota_refused = (usage.quota_refused or 0) + metrics.quota
    usage.errors = (usage.errors or 0) + metrics.errors
    usage.peak_minute_requests = max(usage.peak_minute_requests or 0, metrics.peak_minute_requests)
    for hour in fact.hours:
        slot = usage.hours.setdefault(hour.hour, UsageHour(hour=hour.hour))
        slot.requests += hour.requests
        slot.total_tokens += hour.total_tokens
        slot.throttled += hour.throttled
        slot.quota += hour.quota
        slot.denied += hour.denied
        slot.errors += hour.errors
        slot.peak_minute_tokens = max(slot.peak_minute_tokens, hour.peak_minute_tokens)
        slot.peak_minute_requests = max(slot.peak_minute_requests, hour.peak_minute_requests)
    if metrics.last_seen is not None and (
        usage.last_seen is None or metrics.last_seen > usage.last_seen
    ):
        usage.last_seen = metrics.last_seen


class UsageService:
    def __init__(
        self,
        portal: PortalService,
        *,
        source: UsageSource,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
        environment_repository: EnvironmentRepository,
        pricing: PricingService | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._portal = portal
        self._source = source
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._environments = environment_repository
        self._pricing = pricing
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _measured_pricing(
        self,
        actor: Actor,
        entitlements: Sequence[ResolvedEntitlement],
        *,
        start: date,
        end: date,
    ) -> _MeasuredPricing | None:
        """The prices for the caller's grants, and each provisioned deployment's monthly tokens."""

        if self._pricing is None:
            return None
        deployments: dict[str, tuple[str | None, str | None]] = {}
        for resolved in entitlements:
            deployments[resolved.entitlement.id] = await self._deployment_for(
                actor.tenant_id, resolved.entitlement
            )
        endpoint_ids = {key.partition("/")[0] for key, _ in deployments.values() if key}
        pricer = await self._pricing.pricer(actor.tenant_id, endpoint_ids)
        provisioned = [
            key
            for key, _ in deployments.values()
            if key and (facts := pricer.facts_for(key)) is not None and facts.provisioned
        ]
        tokens = (
            await self._pricing.provisioned_tokens(actor.tenant_id, provisioned, start, end)
            if provisioned
            else {}
        )
        return _MeasuredPricing(pricer=pricer, deployments=deployments, tokens=tokens, today=end)

    async def my_usage(self, actor: Actor, period: UsagePeriod = "30d") -> MyUsageReport:
        now = self._clock().astimezone(UTC)
        end = now.date()
        start = end - timedelta(days=_PERIOD_DAYS[period] - 1)
        entitlements = await self._usage_entitlements(actor)
        quota_start = self._earliest_quota_window_start(entitlements, now).date()
        recent_start = (now - timedelta(hours=23)).date()
        generation_start = min(start, quota_start, recent_start)
        usage = await self._source.daily_usage(actor, entitlements, start=generation_start, end=end)
        freshness = await self._source.freshness(actor, entitlements)
        measured = self._source.data_source != "simulated"
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)
        pricing = (
            await self._measured_pricing(actor, entitlements, start=start, end=end)
            if measured
            else None
        )

        timeline: list[UsageTimelinePoint] = []
        rows: list[UsageResourceRow] = []
        cost_excluded_resources = 0
        env_buckets: dict[str | None, _EnvironmentBucket] = {}

        for resolved in entitlements:
            entitlement = resolved.entitlement
            summary = _summary_for(resolved)
            linked_by = _linked_by(entitlement.binding)
            bound = linked_by is not None
            series = usage.get(entitlement.id)
            if pricing is not None:
                key, model = pricing.deployments.get(entitlement.id, (None, None))
                cost_note = pricing.note(entitlement, key, series, start, end)
            else:
                key = None
                model = await self._model_for(actor.tenant_id, entitlement)
                cost_note = _cost_note(entitlement, model, priced=not measured)
            cost_known = cost_note is None
            if not cost_known:
                cost_excluded_resources += 1
            attribution: UsageAttribution = (
                "simulated"
                if not measured
                else "measured"
                if series is not None
                else "unattributed"
            )
            report_figures = [
                (day, None if series is None else series.get(day)) for day in _days(start, end)
            ]
            present = [item for _, item in report_figures if item is not None]
            # A grant MOSAIC can price costs nothing until it's called; one it can't has no cost.
            resource_cost = 0.0 if cost_known else None
            resource_requests = _sum_optional(item.requests for item in present)
            resource_prompt = _sum_optional(item.prompt_tokens for item in present)
            resource_completion = _sum_optional(item.completion_tokens for item in present)
            resource_tokens = _sum_optional(item.total_tokens for item in present)
            if series is None:
                resource_requests = None
                resource_prompt = None
                resource_completion = None
                resource_tokens = None
                resource_cost = None

            for day, item in report_figures:
                point_cost = None
                if item is not None and cost_known:
                    point_cost = (
                        pricing.cost(key, day, item)
                        if pricing is not None
                        else _price(model, item.prompt_tokens, item.completion_tokens)
                    )
                    if point_cost is not None:
                        resource_cost = (resource_cost or 0.0) + point_cost
                timeline.append(
                    UsageTimelinePoint(
                        date=day.isoformat(),
                        entitlement_id=entitlement.id,
                        environment=summary.environment,
                        requests=None if item is None else item.requests,
                        prompt_tokens=None if item is None else item.prompt_tokens,
                        completion_tokens=None if item is None else item.completion_tokens,
                        total_tokens=None if item is None else item.total_tokens,
                        estimated_cost=None if point_cost is None else round(point_cost, 6),
                        throttled=None if item is None else item.throttled,
                        quota_refused=None if item is None else item.quota_refused,
                        errors=None if item is None else item.errors,
                        peak_minute_tokens=None if item is None else item.peak_minute_tokens,
                        peak_minute_requests=None if item is None else item.peak_minute_requests,
                    )
                )

            peak_tokens = _max_optional(item.peak_minute_tokens for item in present)
            peak_requests = _max_optional(item.peak_minute_requests for item in present)
            quotas = self._quotas(entitlement, series, now, measured=measured)
            row = UsageResourceRow(
                entitlement_id=entitlement.id,
                resource=entitlement.resource,
                resource_summary=summary,
                environment=summary.environment,
                via=resolved.via,
                via_group_name=resolved.via_group_name,
                enabled=entitlement.enabled,
                bound=bound,
                linked_by=linked_by,
                attribution=attribution,
                model=model,
                requests=resource_requests,
                prompt_tokens=resource_prompt,
                completion_tokens=resource_completion,
                total_tokens=resource_tokens,
                estimated_cost=None if resource_cost is None else round(resource_cost, 4),
                cost_note=cost_note,
                quotas=quotas,
                rate_limits=self._rate_limits(
                    entitlement,
                    peak_tokens=peak_tokens if measured else None,
                    peak_requests=peak_requests if measured else None,
                ),
                throttled=_sum_optional(item.throttled for item in present),
                quota_refused=_sum_optional(item.quota_refused for item in present),
                errors=_sum_optional(item.errors for item in present),
                peak_minute_tokens=peak_tokens,
                peak_minute_requests=peak_requests,
                last_used_at=_last_seen(series),
                recent_hours=_recent_hours(series, now) if measured and series else [],
            )
            rows.append(row)

            bucket = env_buckets.setdefault(summary.environment, _EnvironmentBucket())
            bucket.resources += 1
            if row.attribution == "unattributed":
                bucket.unmeasured_resources += 1
            if row.requests is not None:
                bucket.requests += row.requests
            if row.prompt_tokens is not None:
                bucket.prompt_tokens += row.prompt_tokens
            if row.completion_tokens is not None:
                bucket.completion_tokens += row.completion_tokens
            if row.total_tokens is not None:
                bucket.total_tokens += row.total_tokens
            if row.estimated_cost is not None:
                bucket.estimated_cost = (bucket.estimated_cost or 0.0) + row.estimated_cost
            if row.cost_note is not None:
                bucket.cost_excluded_resources += 1

        by_environment = [
            UsageEnvironmentBreakdown(
                environment=environment,
                resources=bucket.resources,
                requests=bucket.requests,
                prompt_tokens=bucket.prompt_tokens,
                completion_tokens=bucket.completion_tokens,
                total_tokens=bucket.total_tokens,
                estimated_cost=None
                if bucket.estimated_cost is None
                else round(bucket.estimated_cost, 4),
                cost_excluded_resources=bucket.cost_excluded_resources,
                unmeasured_resources=bucket.unmeasured_resources,
            )
            for environment, bucket in sorted(
                env_buckets.items(), key=lambda item: _environment_sort(catalog, item[0])
            )
        ]
        estimated_costs = [row.estimated_cost for row in rows if row.estimated_cost is not None]
        last_used = [row.last_used_at for row in rows if row.last_used_at is not None]
        totals = UsageTotals(
            requests=sum(row.requests or 0 for row in rows),
            prompt_tokens=sum(row.prompt_tokens or 0 for row in rows),
            completion_tokens=sum(row.completion_tokens or 0 for row in rows),
            total_tokens=sum(row.total_tokens or 0 for row in rows),
            estimated_cost=round(sum(estimated_costs), 4) if estimated_costs else None,
            cost_excluded_resources=cost_excluded_resources,
            throttled=sum(row.throttled or 0 for row in rows) if measured else None,
            quota_refused=sum(row.quota_refused or 0 for row in rows) if measured else None,
            errors=sum(row.errors or 0 for row in rows) if measured else None,
            last_used_at=max(last_used) if last_used else None,
        )
        return MyUsageReport(
            data_source=self._source.data_source,
            period=period,
            start=start.isoformat(),
            end=end.isoformat(),
            generated_at=now,
            currency="USD",
            totals=totals,
            timeline=timeline,
            by_environment=by_environment,
            by_resource=rows,
            notes=_notes(
                data_source=self._source.data_source,
                unbound=sum(1 for row in rows if row.attribution == "unattributed")
                if measured
                else sum(
                    1 for item in entitlements if _linked_by(item.entitlement.binding) is None
                ),
                cost_excluded=cost_excluded_resources,
                freshness=freshness,
                priced=pricing is not None,
                reserved=pricing is not None and pricing.reserved,
            ),
            freshness=freshness,
            recent_hours=_combine_hours([row.recent_hours for row in rows], now)
            if measured
            else [],
        )

    def _earliest_quota_window_start(
        self, entitlements: Sequence[ResolvedEntitlement], now: datetime
    ) -> datetime:
        starts = [now]
        for resolved in entitlements:
            for _, _, period in quota_definitions(resolved.entitlement):
                starts.append(current_window(now, period)[0])
        return min(starts)

    async def _usage_entitlements(self, actor: Actor) -> Sequence[ResolvedEntitlement]:
        # The portal access list hides disabled grants, but the usage contract shows them with
        # explicit zero usage so users understand the grant exists but is not active.
        return await self._portal.my_entitlements(actor, include_disabled=True)

    async def _deployment_for(
        self, tenant_id: str, entitlement: Entitlement
    ) -> tuple[str | None, str | None]:
        """The deployment a grant's calls reach, keyed as rollups key it, and its model."""

        resource = entitlement.resource
        if resource.kind == EntitlementResourceKind.MODEL_API:
            model_api = await self._gateways.get_model_api(tenant_id, resource.id)
            if model_api is None or model_api.publication_id is None:
                return None, None
            publication = await self._gateways.get_publication(tenant_id, model_api.publication_id)
            if publication is None:
                return None, None
            model = await self._observed_model_name(
                tenant_id, publication.model_endpoint_id, publication.deployment_name
            )
            return (
                deployment_key(publication.model_endpoint_id, publication.deployment_name),
                model,
            )
        if resource.kind == EntitlementResourceKind.MODEL_DEPLOYMENT and resource.scope_id:
            model = await self._observed_model_name(tenant_id, resource.scope_id, resource.id)
            return deployment_key(resource.scope_id, resource.id), model
        return None, None

    async def _model_for(self, tenant_id: str, entitlement: Entitlement) -> str | None:
        return (await self._deployment_for(tenant_id, entitlement))[1]

    async def _observed_model_name(
        self, tenant_id: str, endpoint_id: str, deployment_name: str
    ) -> str | None:
        deployments = await self._endpoints.list_observed_for_endpoint(
            ObservedModelDeployment,
            tenant_id,
            endpoint_id,
            "observedModelDeployment",
        )
        for deployment in deployments:
            if deployment.deployment_name == deployment_name or deployment.id == deployment_name:
                return deployment.model_name
        return None

    def _quotas(
        self,
        entitlement: Entitlement,
        series: Mapping[date, DailyUsage] | None,
        now: datetime,
        *,
        measured: bool,
    ) -> list[UsageQuota]:
        is_mcp = entitlement.resource.kind == EntitlementResourceKind.MCP_SERVER
        quotas: list[UsageQuota] = []
        for metric, limit, period in quota_definitions(entitlement):
            window_start, window_end = current_window(now, period)
            partial = False
            used: float | None
            if series is None or (is_mcp and metric == "tokens"):
                used = None
            elif period == "Hourly":
                today = series.get(now.date())
                if today is None:
                    used = None
                elif measured:
                    slot = today.hours.get(now.hour)
                    used = float(_hour_value(slot, metric) if slot else 0)
                else:
                    used = (_metric_value(today, metric) or 0) / 24
            elif period == "Daily":
                today = series.get(now.date())
                used = None if today is None else float(_metric_value(today, metric) or 0)
            else:
                window_days = _days(window_start.date(), now.date())
                known = [series[day] for day in window_days if day in series]
                partial = measured and len(known) < len(window_days)
                used = (
                    float(sum(_metric_value(usage, metric) or 0 for usage in known))
                    if known or not measured
                    else None
                )
            quotas.append(
                UsageQuota(
                    metric=metric,
                    limit=limit,
                    period=period,
                    window_start=window_start,
                    window_end=window_end,
                    used=None if used is None else round(used, 4),
                    utilization=None if used is None else round(used / limit, 4),
                    partial=partial and used is not None,
                )
            )
        return quotas

    def _rate_limits(
        self,
        entitlement: Entitlement,
        *,
        peak_tokens: int | None = None,
        peak_requests: int | None = None,
    ) -> list[UsageRateLimit]:
        enforcement = entitlement.enforcement
        if enforcement is None:
            return []
        limits: list[UsageRateLimit] = []
        if enforcement.tokens and enforcement.tokens.tokens_per_minute:
            limit = enforcement.tokens.tokens_per_minute
            limits.append(
                UsageRateLimit(
                    metric="tokens",
                    limit=limit,
                    window_seconds=60,
                    peak=peak_tokens,
                    utilization=None if peak_tokens is None else round(peak_tokens / limit, 4),
                )
            )
        if (
            enforcement.requests
            and enforcement.requests.calls
            and enforcement.requests.renewal_period_seconds
        ):
            limit = enforcement.requests.calls
            window = enforcement.requests.renewal_period_seconds
            # Peaks are measured per minute, so they only compare with a per-minute limit.
            peak = peak_requests if window == 60 else None
            limits.append(
                UsageRateLimit(
                    metric="requests",
                    limit=limit,
                    window_seconds=window,
                    peak=peak,
                    utilization=None if peak is None else round(peak / limit, 4),
                )
            )
        return limits


@dataclass
class _EnvironmentBucket:
    resources: int = 0
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost: float | None = None
    cost_excluded_resources: int = 0
    unmeasured_resources: int = 0


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def _digest(actor: Actor, entitlement_id: str, day: date) -> str:
    return hashlib.sha256(
        f"{actor.tenant_id}|{actor.object_id}|{entitlement_id}|{day.isoformat()}".encode()
    ).hexdigest()


def _fraction(hex_value: str) -> float:
    return int(hex_value, 16) / float(16 ** len(hex_value) - 1)


def _environment_multiplier(catalog: EnvironmentCatalog, environment: str | None) -> float:
    if is_production(catalog, environment):
        return 4.0
    if environment in {None, "development", "sandbox"}:
        return 0.8
    return 2.0


def _summary_for(resolved: ResolvedEntitlement) -> ResourceSummary:
    if resolved.resource_summary is not None:
        return resolved.resource_summary
    resource = resolved.entitlement.resource
    return ResourceSummary(
        kind=resource.kind,
        id=resource.id,
        scope_id=resource.scope_id,
        display_name="Unavailable resource",
        available=False,
    )


def _zero_usage(is_mcp: bool) -> DailyUsage:
    return DailyUsage(
        requests=0,
        prompt_tokens=None if is_mcp else 0,
        completion_tokens=None if is_mcp else 0,
    )


def _metric_value(usage: DailyUsage, metric: Metric) -> int | None:
    if metric == "requests":
        return usage.requests
    return usage.total_tokens


def _hour_value(slot: UsageHour, metric: Metric) -> int:
    return slot.requests if metric == "requests" else slot.total_tokens


def _max_optional(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _last_seen(series: Mapping[date, DailyUsage] | None) -> datetime | None:
    if not series:
        return None
    seen = [usage.last_seen for usage in series.values() if usage.last_seen is not None]
    return max(seen) if seen else None


def _recent_hour_starts(now: datetime) -> list[datetime]:
    current = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    return [current - timedelta(hours=offset) for offset in range(23, -1, -1)]


def _recent_hours(series: Mapping[date, DailyUsage], now: datetime) -> list[UsageHourPoint]:
    """The last 24 UTC hours, the current one included, for one grant."""

    points: list[UsageHourPoint] = []
    for hour in _recent_hour_starts(now):
        usage = series.get(hour.date())
        if usage is None:
            points.append(
                UsageHourPoint(
                    hour=hour,
                    requests=None,
                    total_tokens=None,
                    throttled=None,
                    quota_refused=None,
                    errors=None,
                    peak_minute_tokens=None,
                    peak_minute_requests=None,
                )
            )
            continue
        slot = usage.hours.get(hour.hour) or UsageHour(hour=hour.hour)
        metered = usage.tokens is not None
        points.append(
            UsageHourPoint(
                hour=hour,
                requests=slot.requests,
                total_tokens=slot.total_tokens if metered else None,
                throttled=slot.throttled,
                quota_refused=slot.quota,
                errors=slot.errors,
                peak_minute_tokens=slot.peak_minute_tokens if metered else None,
                peak_minute_requests=slot.peak_minute_requests,
            )
        )
    return points


def _combine_hours(
    per_grant: Sequence[Sequence[UsageHourPoint]], now: datetime
) -> list[UsageHourPoint]:
    """Every grant's last 24 hours added together. Peaks are each hour's busiest grant."""

    combined: list[UsageHourPoint] = []
    for index, hour in enumerate(_recent_hour_starts(now)):
        points = [hours[index] for hours in per_grant if len(hours) > index]
        combined.append(
            UsageHourPoint(
                hour=hour,
                requests=_sum_optional(point.requests for point in points),
                total_tokens=_sum_optional(point.total_tokens for point in points),
                throttled=_sum_optional(point.throttled for point in points),
                quota_refused=_sum_optional(point.quota_refused for point in points),
                errors=_sum_optional(point.errors for point in points),
                peak_minute_tokens=_max_optional(point.peak_minute_tokens for point in points),
                peak_minute_requests=_max_optional(
                    point.peak_minute_requests for point in points
                ),
            )
        )
    return combined


def _scale_metric_to(usage: DailyUsage, metric: Metric, cap: int) -> None:
    value = _metric_value(usage, metric)
    if value is not None and value > cap:
        _scale_metric_by(usage, metric, cap / value if value else 0.0)


def _scale_tokens_to(usage: DailyUsage, cap: int) -> None:
    _scale_metric_to(usage, "tokens", cap)


def _scale_requests_to(usage: DailyUsage, cap: int) -> None:
    _scale_metric_to(usage, "requests", cap)


def _scale_metric_by(usage: DailyUsage, metric: Metric, factor: float) -> None:
    if metric == "requests":
        if usage.requests is not None:
            usage.requests = int(usage.requests * factor)
        return
    if usage.prompt_tokens is not None:
        usage.prompt_tokens = int(usage.prompt_tokens * factor)
    if usage.completion_tokens is not None:
        usage.completion_tokens = int(usage.completion_tokens * factor)


def _window_start_for(day: date, period: QuotaPeriod) -> date:
    if period in {"Hourly", "Daily"}:
        return day
    if period == "Weekly":
        return day - timedelta(days=day.weekday())
    if period == "Monthly":
        return date(day.year, day.month, 1)
    return date(day.year, 1, 1)


def _window_last_day_for(day: date, period: QuotaPeriod) -> date:
    if period in {"Hourly", "Daily"}:
        return day
    if period == "Weekly":
        return _window_start_for(day, period) + timedelta(days=6)
    if period == "Monthly":
        following = (
            date(day.year + 1, 1, 1) if day.month == 12 else date(day.year, day.month + 1, 1)
        )
        return following - timedelta(days=1)
    return date(day.year, 12, 31)


def _generation_range(entitlement: Entitlement, start: date, end: date) -> tuple[date, date]:
    first, last = start, end
    for _, _, period in quota_definitions(entitlement):
        first = min(first, _window_start_for(start, period))
        last = max(last, _window_last_day_for(end, period))
    return first, last


def current_window(now: datetime, period: QuotaPeriod) -> tuple[datetime, datetime]:
    current = now.astimezone(UTC)
    if period == "Hourly":
        start = current.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if period == "Daily":
        start = datetime.combine(current.date(), time.min, tzinfo=UTC)
        return start, start + timedelta(days=1)
    if period == "Weekly":
        start_date = current.date() - timedelta(days=current.weekday())
        start = datetime.combine(start_date, time.min, tzinfo=UTC)
        return start, start + timedelta(days=7)
    if period == "Monthly":
        start = datetime(current.year, current.month, 1, tzinfo=UTC)
        end = (
            datetime(current.year + 1, 1, 1, tzinfo=UTC)
            if current.month == 12
            else datetime(current.year, current.month + 1, 1, tzinfo=UTC)
        )
        return start, end
    start = datetime(current.year, 1, 1, tzinfo=UTC)
    return start, datetime(current.year + 1, 1, 1, tzinfo=UTC)


def quota_definitions(entitlement: Entitlement) -> list[tuple[Metric, int, QuotaPeriod]]:
    enforcement = entitlement.enforcement
    if enforcement is None:
        return []
    quotas: list[tuple[Metric, int, QuotaPeriod]] = []
    if (
        enforcement.tokens
        and enforcement.tokens.token_quota
        and enforcement.tokens.token_quota_period
    ):
        quotas.append(
            ("tokens", enforcement.tokens.token_quota, enforcement.tokens.token_quota_period)
        )
    if (
        enforcement.requests
        and enforcement.requests.call_quota
        and enforcement.requests.call_quota_period
    ):
        quotas.append(
            ("requests", enforcement.requests.call_quota, enforcement.requests.call_quota_period)
        )
    return quotas


def _normalize_model(model: str) -> str:
    return re.sub(r"[^a-z0-9.-]+", "", model.casefold())


def _rate_for(model: str | None) -> tuple[float, float] | None:
    if model is None:
        return None
    normalized = _normalize_model(model)
    for prefix, rate in sorted(_ILLUSTRATIVE_MODEL_RATES.items(), key=lambda item: -len(item[0])):
        if normalized.startswith(prefix):
            return rate
    return None


def _price(
    model: str | None, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    rate = _rate_for(model)
    if rate is None or prompt_tokens is None or completion_tokens is None:
        return None
    prompt_rate, completion_rate = rate
    return round(prompt_tokens / 1000 * prompt_rate + completion_tokens / 1000 * completion_rate, 4)


def _cost_note(entitlement: Entitlement, model: str | None, *, priced: bool) -> str | None:
    kind = entitlement.resource.kind
    if kind == EntitlementResourceKind.MCP_SERVER:
        return MCP_COST_NOTE
    if kind == EntitlementResourceKind.PRODUCT:
        return PRODUCT_COST_NOTE
    if not priced:
        return "No price list yet."
    if model is None:
        return "MOSAIC doesn't know which model this API calls."
    if _rate_for(model) is None:
        return f'No illustrative rate for model "{model}".'
    return None


def _sum_optional(values: Iterable[int | None]) -> int | None:
    present = [value for value in values if value is not None]
    if not present:
        return None
    return sum(present)


def _environment_sort(catalog: EnvironmentCatalog, environment: str | None) -> tuple[int, str]:
    if environment is None:
        return (100_000, "")
    for definition in catalog.environments:
        if definition.key == environment:
            return (definition.order, definition.key)
    return (50_000, environment)


def _linked_by(binding: EntitlementBinding | None) -> Literal["gatewayLog", "subscription"] | None:
    if binding is None:
        return None
    if binding.attribution_key is not None:
        return "gatewayLog"
    if binding.apim_subscription_name is not None:
        return "subscription"
    return None


def _notes(
    *,
    data_source: UsageDataSource,
    unbound: int,
    cost_excluded: int,
    freshness: UsageFreshness | None = None,
    priced: bool = False,
    reserved: bool = False,
) -> list[str]:
    if data_source == "simulated":
        notes = [
            "Usage figures are simulated from your real MOSAIC grants and limits.",
            "Estimated costs use illustrative model rates and are not a bill.",
        ]
    else:
        interval = freshness.interval_minutes if freshness else 15
        notes = [
            "Usage comes from API Management's gateway logs, which MOSAIC collects about every "
            f"{interval} minutes. Recent calls can take a little longer to appear.",
            "The gateway applies your limits as you call, so you can reach one before this page "
            "shows it.",
        ]
        if priced:
            notes.append(
                "Costs are estimates at list prices from MOSAIC's price list, before any "
                "discount, and are not a bill. The gateway's logs don't separate cached prompt "
                "tokens, so every prompt token is priced at the full input price."
            )
            if reserved:
                notes.append(
                    "A provisioned deployment costs the same whether or not it's called, so "
                    "each month's cost is shared among its callers by their share of its tokens."
                )
        else:
            notes.append("Costs aren't shown yet because MOSAIC doesn't have a price list.")
    if unbound:
        notes.append(
            "1 resource isn't linked to API Management telemetry yet, so MOSAIC can't measure its "
            "usage."
            if unbound == 1
            else f"{unbound} resources aren't linked to API Management telemetry yet, so MOSAIC "
            "can't measure their usage."
        )
    if cost_excluded and (data_source == "simulated" or priced):
        notes.append(
            "Estimated cost leaves out 1 resource MOSAIC can't price; its row says why."
            if cost_excluded == 1
            else f"Estimated cost leaves out {cost_excluded} resources MOSAIC can't price; "
            "each row says why."
        )
    return notes
