from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol

from mosaic_api.domain import (
    Entitlement,
    EntitlementResource,
    EntitlementResourceKind,
    MosaicModel,
    QuotaPeriod,
    ResolvedEntitlement,
    ResourceSummary,
)
from mosaic_api.environments import EnvironmentCatalog, is_production
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.repositories import (
    EnvironmentRepository,
    GatewayRepository,
    ModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.environments import load_environment_catalog
from mosaic_api.services.portal import PortalService

UsagePeriod = Literal["7d", "30d", "90d"]
UsageDataSource = Literal["simulated", "logAnalytics"]
UsageAttribution = Literal["simulated", "measured", "unattributed"]
Metric = Literal["tokens", "requests"]

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


class UsageTimelinePoint(MosaicModel):
    date: str
    entitlement_id: str
    environment: str | None
    requests: int | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    estimated_cost: float | None


class UsageEnvironmentBreakdown(MosaicModel):
    environment: str | None
    resources: int
    requests: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float | None
    cost_excluded_resources: int


class UsageQuota(MosaicModel):
    metric: Metric
    limit: int
    period: QuotaPeriod
    window_start: datetime
    window_end: datetime
    used: float | None
    utilization: float | None


class UsageRateLimit(MosaicModel):
    metric: Metric
    limit: int
    window_seconds: int


class UsageResourceRow(MosaicModel):
    entitlement_id: str
    resource: EntitlementResource
    resource_summary: ResourceSummary
    environment: str | None
    via: Literal["direct", "group"]
    via_group_name: str | None
    enabled: bool
    bound: bool
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


@dataclass
class DailyUsage:
    requests: int | None
    prompt_tokens: int | None
    completion_tokens: int | None

    @property
    def total_tokens(self) -> int | None:
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
    ) -> UsageSeries: ...


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


class UsageService:
    def __init__(
        self,
        portal: PortalService,
        *,
        source: UsageSource,
        gateway_repository: GatewayRepository,
        endpoint_repository: ModelEndpointRepository,
        environment_repository: EnvironmentRepository,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._portal = portal
        self._source = source
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._environments = environment_repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def my_usage(self, actor: Actor, period: UsagePeriod = "30d") -> MyUsageReport:
        now = self._clock().astimezone(UTC)
        end = now.date()
        start = end - timedelta(days=_PERIOD_DAYS[period] - 1)
        entitlements = await self._usage_entitlements(actor)
        quota_start = self._earliest_quota_window_start(entitlements, now).date()
        generation_start = min(start, quota_start)
        usage = await self._source.daily_usage(actor, entitlements, start=generation_start, end=end)
        catalog = await load_environment_catalog(self._environments, actor.tenant_id)

        timeline: list[UsageTimelinePoint] = []
        rows: list[UsageResourceRow] = []
        cost_excluded_resources = 0
        env_buckets: dict[str | None, _EnvironmentBucket] = {}

        for resolved in entitlements:
            entitlement = resolved.entitlement
            summary = _summary_for(resolved)
            model = await self._model_for(actor.tenant_id, entitlement)
            cost_note = _cost_note(entitlement, model)
            cost_known = cost_note is None
            if not cost_known:
                cost_excluded_resources += 1
            bound = entitlement.binding is not None
            attribution: UsageAttribution = (
                "simulated"
                if self._source.data_source == "simulated"
                else "measured"
                if bound
                else "unattributed"
            )
            series = usage.get(entitlement.id)
            if self._source.data_source != "simulated" and not bound:
                series = None
            report_figures = [
                (day, None if series is None else series.get(day)) for day in _days(start, end)
            ]
            resource_cost = 0.0 if cost_known else None
            resource_requests = _sum_optional(item.requests for _, item in report_figures if item)
            resource_prompt = _sum_optional(
                item.prompt_tokens for _, item in report_figures if item
            )
            resource_completion = _sum_optional(
                item.completion_tokens for _, item in report_figures if item
            )
            if series is None:
                resource_requests = None
                resource_prompt = None
                resource_completion = None
                resource_cost = None

            for day, item in report_figures:
                point_cost = None
                if item is not None and cost_known:
                    point_cost = _price(model, item.prompt_tokens, item.completion_tokens)
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
                        estimated_cost=point_cost,
                    )
                )

            total_tokens = (
                None
                if resource_prompt is None or resource_completion is None
                else resource_prompt + resource_completion
            )
            quotas = self._quotas(entitlement, series, now)
            row = UsageResourceRow(
                entitlement_id=entitlement.id,
                resource=entitlement.resource,
                resource_summary=summary,
                environment=summary.environment,
                via=resolved.via,
                via_group_name=resolved.via_group_name,
                enabled=entitlement.enabled,
                bound=bound,
                attribution=attribution,
                model=model,
                requests=resource_requests,
                prompt_tokens=resource_prompt,
                completion_tokens=resource_completion,
                total_tokens=total_tokens,
                estimated_cost=None if resource_cost is None else round(resource_cost, 4),
                cost_note=cost_note,
                quotas=quotas,
                rate_limits=self._rate_limits(entitlement),
            )
            rows.append(row)

            bucket = env_buckets.setdefault(summary.environment, _EnvironmentBucket())
            bucket.resources += 1
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
            )
            for environment, bucket in sorted(
                env_buckets.items(), key=lambda item: _environment_sort(catalog, item[0])
            )
        ]
        estimated_costs = [row.estimated_cost for row in rows if row.estimated_cost is not None]
        totals = UsageTotals(
            requests=sum(row.requests or 0 for row in rows),
            prompt_tokens=sum(row.prompt_tokens or 0 for row in rows),
            completion_tokens=sum(row.completion_tokens or 0 for row in rows),
            total_tokens=sum(row.total_tokens or 0 for row in rows),
            estimated_cost=round(sum(estimated_costs), 4) if estimated_costs else None,
            cost_excluded_resources=cost_excluded_resources,
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
                unbound=sum(1 for item in entitlements if item.entitlement.binding is None),
                cost_excluded=cost_excluded_resources,
            ),
        )

    def _earliest_quota_window_start(
        self, entitlements: Sequence[ResolvedEntitlement], now: datetime
    ) -> datetime:
        starts = [now]
        for resolved in entitlements:
            for _, _, period in _quota_definitions(resolved.entitlement):
                starts.append(_current_window(now, period)[0])
        return min(starts)

    async def _usage_entitlements(self, actor: Actor) -> Sequence[ResolvedEntitlement]:
        # The portal access list hides disabled grants, but the usage contract shows them with
        # explicit zero usage so users understand the grant exists but is not active.
        return await self._portal.my_entitlements(actor, include_disabled=True)

    async def _model_for(self, tenant_id: str, entitlement: Entitlement) -> str | None:
        resource = entitlement.resource
        if resource.kind == EntitlementResourceKind.MODEL_API:
            model_api = await self._gateways.get_model_api(tenant_id, resource.id)
            if model_api is None or model_api.publication_id is None:
                return None
            publication = await self._gateways.get_publication(tenant_id, model_api.publication_id)
            if publication is None:
                return None
            return await self._observed_model_name(
                tenant_id, publication.model_endpoint_id, publication.deployment_name
            )
        if resource.kind == EntitlementResourceKind.MODEL_DEPLOYMENT and resource.scope_id:
            return await self._observed_model_name(tenant_id, resource.scope_id, resource.id)
        return None

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
        self, entitlement: Entitlement, series: Mapping[date, DailyUsage] | None, now: datetime
    ) -> list[UsageQuota]:
        is_mcp = entitlement.resource.kind == EntitlementResourceKind.MCP_SERVER
        quotas: list[UsageQuota] = []
        for metric, limit, period in _quota_definitions(entitlement):
            window_start, window_end = _current_window(now, period)
            if series is None or (is_mcp and metric == "tokens"):
                used = None
            elif period == "Hourly":
                today = series.get(now.date())
                used = None if today is None else (_metric_value(today, metric) or 0) / 24
            elif period == "Daily":
                today = series.get(now.date())
                used = None if today is None else float(_metric_value(today, metric) or 0)
            else:
                used = float(
                    sum(
                        _metric_value(usage, metric) or 0
                        for day, usage in series.items()
                        if window_start.date() <= day <= now.date()
                    )
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
                )
            )
        return quotas

    def _rate_limits(self, entitlement: Entitlement) -> list[UsageRateLimit]:
        enforcement = entitlement.enforcement
        if enforcement is None:
            return []
        limits: list[UsageRateLimit] = []
        if enforcement.tokens and enforcement.tokens.tokens_per_minute:
            limits.append(
                UsageRateLimit(
                    metric="tokens",
                    limit=enforcement.tokens.tokens_per_minute,
                    window_seconds=60,
                )
            )
        if (
            enforcement.requests
            and enforcement.requests.calls
            and enforcement.requests.renewal_period_seconds
        ):
            limits.append(
                UsageRateLimit(
                    metric="requests",
                    limit=enforcement.requests.calls,
                    window_seconds=enforcement.requests.renewal_period_seconds,
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
    for _, _, period in _quota_definitions(entitlement):
        first = min(first, _window_start_for(start, period))
        last = max(last, _window_last_day_for(end, period))
    return first, last


def _current_window(now: datetime, period: QuotaPeriod) -> tuple[datetime, datetime]:
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


def _quota_definitions(entitlement: Entitlement) -> list[tuple[Metric, int, QuotaPeriod]]:
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


def _cost_note(entitlement: Entitlement, model: str | None) -> str | None:
    kind = entitlement.resource.kind
    if kind == EntitlementResourceKind.MCP_SERVER:
        return "MCP servers are billed by their own service, not by tokens."
    if kind == EntitlementResourceKind.PRODUCT:
        return "Products bundle several APIs, so MOSAIC can't price them."
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


def _notes(
    *,
    data_source: UsageDataSource,
    unbound: int,
    cost_excluded: int,
) -> list[str]:
    notes = [
        (
            "Usage figures are simulated from your real MOSAIC grants and limits."
            if data_source == "simulated"
            else "Usage figures come from API Management telemetry for bound grants."
        ),
        "Estimated costs use illustrative model rates and are not a bill.",
    ]
    if unbound:
        notes.append(
            (
                "1 resource isn't bound to API Management yet, so its real usage will show as "
                "unattributed."
            )
            if unbound == 1
            else (
                f"{unbound} resources aren't bound to API Management yet, so their real usage "
                "will show as unattributed."
            )
        )
    if cost_excluded:
        notes.append(
            "Estimated cost leaves out 1 resource MOSAIC can't price; its row says why."
            if cost_excluded == 1
            else f"Estimated cost leaves out {cost_excluded} resources MOSAIC can't price; "
            "each row says why."
        )
    return notes
