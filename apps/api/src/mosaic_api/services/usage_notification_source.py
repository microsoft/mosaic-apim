"""Truthful notification observations from the same source and attribution as My usage."""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal, Protocol

from pydantic import Field

from mosaic_api.domain import (
    BindingSource,
    EntitlementResourceKind,
    EntitlementSubjectKind,
    MosaicModel,
    ResolvedEntitlement,
)
from mosaic_api.notifications import AlertEvidence, DigestWindow, NotificationSubject
from mosaic_api.scheduling import UtcInstant
from mosaic_api.services.directory import Actor
from mosaic_api.services.notification_events import notification_record_id
from mosaic_api.services.usage import (
    CostCenterUsage,
    DailyUsage,
    FreshnessStatus,
    NotificationUsageContext,
    UsageService,
    current_window,
    quota_definitions,
)
from mosaic_api.usage_telemetry import rollup_stale_after

Coverage = Literal["complete", "delayed", "incomplete", "unavailable"]


class UsageCounterObservation(MosaicModel):
    scope_id: str
    scope_revision: str
    metric: Literal["requests", "tokens"]
    period: str
    period_start_at: UtcInstant
    period_end_at: UtcInstant
    subject: NotificationSubject
    evidence: AlertEvidence
    unsupported_reason: str | None = None


class UsageSummaryResource(MosaicModel):
    entitlement_id: str
    subject: NotificationSubject
    resource_name: str | None = None
    environment: str | None = None
    calls: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    cost_reason: str | None = None
    coverage: Coverage


class UsageNotificationSnapshot(MosaicModel):
    observed_at: UtcInstant
    through: UtcInstant | None = None
    freshness_status: FreshnessStatus | None = None
    freshness_updated_at: UtcInstant | None = None
    freshness_interval_minutes: int | None = Field(default=None, ge=1)
    coverage_start: UtcInstant | None = None
    coverage: Coverage
    calls: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unpriced_tokens: int = Field(default=0, ge=0)
    resources: list[UsageSummaryResource] = Field(default_factory=list)
    counters: list[UsageCounterObservation] = Field(default_factory=list)
    # The portal's existing everyone's-usage totals, explicitly the current UTC month, not a
    # person's cap or the local summary period. These never feed personal alert counters.
    shared_monthly: list[CostCenterUsage] = Field(default_factory=list)
    shared_monthly_coverage: Literal["unverified"] = "unverified"
    unsupported: list[str] = Field(default_factory=list)


class UsageNotificationSource(Protocol):
    async def observe(
        self, actor: Actor, *, now: datetime, window: DigestWindow | None = None
    ) -> UsageNotificationSnapshot: ...

    async def eligible(self, actor: Actor, snapshot: UsageNotificationSnapshot) -> bool: ...


def _scope_revision(item: ResolvedEntitlement) -> str:
    grant = item.entitlement
    return notification_record_id(
        "usageScope",
        grant.enforcement.model_dump_json() if grant.enforcement else "",
        grant.binding.model_dump_json() if grant.binding else "",
        grant.cost_center_id,
    )


def _coverage(
    context: NotificationUsageContext, start: datetime, end: datetime, now: datetime
) -> Coverage:
    freshness = context.freshness
    if not context.measured or freshness is None or freshness.updated_at is None:
        return "unavailable"
    maximum_age = rollup_stale_after(timedelta(minutes=freshness.interval_minutes))
    if (
        freshness.status != "current"
        or now - freshness.updated_at > maximum_age
        or (context.through is not None and now - context.through > maximum_age)
    ):
        return "delayed"
    if (
        context.coverage_start is None
        or context.through is None
        or context.coverage_start > start
        or context.through < end
    ):
        return "incomplete"
    return "complete"


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=index) for index in range((end - start).days + 1)]


def _sum(values: Sequence[int | None]) -> int | None:
    return (
        sum(value for value in values if value is not None)
        if all(value is not None for value in values)
        else None
    )


def _unmetered(series: Mapping[date, DailyUsage] | None, start: date, end: date) -> bool:
    return any(
        figures.successful_requests is not None
        and (
            figures.metered_requests is None
            or figures.metered_requests < figures.successful_requests
        )
        for day in _days(start, end)
        if (figures := (series or {}).get(day)) is not None
    )


def _slice(
    series: Mapping[date, DailyUsage] | None, start: datetime, end: datetime
) -> tuple[int | None, int | None]:
    """Whole UTC days use daily facts; UTC-hour-aligned local boundaries use hour facts.

    Half-hour/quarter-hour boundaries cannot be split using hourly rollups. Never prorate them.
    Missing hourly detail is not a zero, even when there is a daily total.
    """

    if series is None or any(
        (instant.minute, instant.second, instant.microsecond) != (0, 0, 0)
        for instant in (start, end)
    ):
        return None, None
    calls: list[int | None] = []
    tokens: list[int | None] = []
    for day in _days(start.date(), (end - timedelta(microseconds=1)).date()):
        figures = series.get(day)
        if figures is None:
            return None, None
        midnight = datetime.combine(day, time.min, tzinfo=UTC)
        low, high = max(start, midnight), min(end, midnight + timedelta(days=1))
        if low == midnight and high == midnight + timedelta(days=1):
            calls.append(figures.requests)
            tokens.append(figures.total_tokens)
            continue
        complete_calls = figures.requests == sum(slot.requests for slot in figures.hours.values())
        complete_tokens = figures.total_tokens == sum(
            slot.total_tokens for slot in figures.hours.values()
        )
        hours = range(
            int((low - midnight).total_seconds() // 3600),
            int((high - midnight).total_seconds() // 3600),
        )
        calls.append(
            sum(figures.hours[hour].requests for hour in hours if hour in figures.hours)
            if complete_calls
            else None
        )
        tokens.append(
            sum(figures.hours[hour].total_tokens for hour in hours if hour in figures.hours)
            if complete_tokens
            else None
        )
    return _sum(calls), _sum(tokens)


def _quota_used(
    series: Mapping[date, DailyUsage] | None,
    start: datetime,
    through: datetime | None,
    metric: Literal["requests", "tokens"],
    *,
    hourly: bool,
) -> int | None:
    if series is None or through is None or through < start:
        return None
    if hourly:
        figures = series.get(start.date())
        if figures is None:
            return None
        values = list(figures.hours.values())
        known = (
            figures.requests == sum(slot.requests for slot in values)
            if metric == "requests"
            else figures.total_tokens == sum(slot.total_tokens for slot in values)
        )
        if not known:
            return None
        slot = figures.hours.get(start.hour)
        return 0 if slot is None else (slot.requests if metric == "requests" else slot.total_tokens)
    figures_by_day = [series.get(day) for day in _days(start.date(), through.date())]
    return _sum(
        [
            None
            if figures is None
            else (figures.requests if metric == "requests" else figures.total_tokens)
            for figures in figures_by_day
        ]
    )


def _counters(
    actor: Actor, context: NotificationUsageContext, now: datetime
) -> list[UsageCounterObservation]:
    observations: list[UsageCounterObservation] = []
    for resolved in context.entitlements:
        grant = resolved.entitlement
        binding = grant.binding
        group = grant.subject.kind in {
            EntitlementSubjectKind.GROUP,
            EntitlementSubjectKind.SECURITY_GROUP,
        }
        authoritative = bool(
            binding
            and binding.source == BindingSource.ORCHESTRATED
            and binding.attribution_key
            and (not group or binding.attribution_per_member)
            and grant.resource.kind != EntitlementResourceKind.PRODUCT
            and grant.runtime is not None
            and grant.runtime.status == "applied"
        )
        revision = _scope_revision(resolved)
        for metric, limit, period in quota_definitions(grant):
            start, end = current_window(now, period)
            through = min(now, context.through) if context.through else None
            coverage = _coverage(context, start, through or now, now)
            used = _quota_used(
                context.series.get(grant.id), start, through, metric, hourly=period == "Hourly"
            )
            if metric == "tokens" and _unmetered(
                context.series.get(grant.id), start.date(), (through or now).date()
            ):
                used = None
            if used is None or not authoritative:
                coverage = "incomplete" if context.measured else "unavailable"
            # Calls on an MCP server do not have a model-token price. For model usage a missing
            # price is retained as evidence and suppresses crossings, not silently costed at zero.
            is_mcp = grant.resource.kind == EntitlementResourceKind.MCP_SERVER
            relevant_days = _days(start.date(), (through or now).date())
            priced = is_mcp or all(
                context.costs.get(grant.id, {}).get(day) is not None for day in relevant_days
            )
            observations.append(
                UsageCounterObservation(
                    scope_id=notification_record_id(
                        "personalUsage",
                        actor.tenant_id,
                        actor.object_id.casefold(),
                        grant.id,
                        metric,
                        period,
                    ),
                    scope_revision=revision,
                    metric=metric,
                    period=period,
                    period_start_at=start,
                    period_end_at=end,
                    subject=NotificationSubject(
                        entitlement_id=grant.id,
                        resource=grant.resource,
                        cost_center_id=grant.cost_center_id,
                    ),
                    evidence=AlertEvidence(
                        observed_at=now,
                        through=through,
                        coverage=coverage,
                        priced=priced,
                        scope_kind="personal",
                        limit=limit,
                        used=used,
                        unpriced_tokens=(
                            int(used or 0) if metric == "tokens" and not priced else 0
                        ),
                    ),
                    unsupported_reason=None
                    if authoritative
                    else "authoritativeCounterScopeUnavailable",
                )
            )
    return observations


def _summary_resource(
    item: ResolvedEntitlement,
    context: NotificationUsageContext,
    window: DigestWindow,
    now: datetime,
) -> UsageSummaryResource:
    grant = item.entitlement
    calls, tokens = _slice(context.series.get(grant.id), window.start_at, window.end_at)
    missing_metering = _unmetered(
        context.series.get(grant.id),
        window.start_at.date(),
        (window.end_at - timedelta(microseconds=1)).date(),
    )
    if missing_metering:
        tokens = None
    coverage = _coverage(context, window.start_at, window.end_at, now)
    if calls is None or (
        tokens is None and grant.resource.kind != EntitlementResourceKind.MCP_SERVER
    ):
        coverage = "incomplete" if context.measured else "unavailable"
    whole_days = window.start_at.time() == time.min and window.end_at.time() == time.min
    costs = [
        context.costs.get(grant.id, {}).get(day)
        for day in _days(window.start_at.date(), (window.end_at - timedelta(microseconds=1)).date())
    ]
    cost = (
        sum(value for value in costs if value is not None)
        if (
            whole_days
            and not missing_metering
            and coverage == "complete"
            and costs
            and all(value is not None for value in costs)
        )
        else None
    )
    return UsageSummaryResource(
        entitlement_id=grant.id,
        resource_name=item.resource_summary.display_name if item.resource_summary else None,
        subject=NotificationSubject(
            entitlement_id=grant.id, resource=grant.resource, cost_center_id=grant.cost_center_id
        ),
        environment=item.resource_summary.environment if item.resource_summary else None,
        calls=calls,
        tokens=tokens,
        cost_usd=cost,
        cost_reason=(
            None
            if cost is not None
            else "hourlyPricingUnavailable"
            if not whole_days
            else "pricingOrCoverageUnavailable"
        ),
        coverage=coverage,
    )


class PortalUsageNotificationSource:
    """No simulated data, enforcement writes, personal dollar caps, or minute-rate promises."""

    def __init__(self, usage: UsageService) -> None:
        self._usage = usage

    async def eligible(self, actor: Actor, snapshot: UsageNotificationSnapshot) -> bool:
        current = {
            item.entitlement.id: item for item in await self._usage.notification_entitlements(actor)
        }
        required = {resource.entitlement_id for resource in snapshot.resources}
        required.update(
            counter.subject.entitlement_id
            for counter in snapshot.counters
            if counter.subject.entitlement_id
        )
        return (
            bool(current)
            and required.issubset(current)
            and all(
                counter.subject.entitlement_id is not None
                and counter.scope_revision
                == _scope_revision(current[counter.subject.entitlement_id])
                for counter in snapshot.counters
            )
        )

    async def observe(
        self, actor: Actor, *, now: datetime, window: DigestWindow | None = None
    ) -> UsageNotificationSnapshot:
        now = now.astimezone(UTC)
        start = (
            window.start_at
            if window
            else min(current_window(now, "Yearly")[0], current_window(now, "Weekly")[0])
        )
        end = window.end_at if window else now
        unsupported = [
            "exactNearRealtimeRpmTpm:delayedHourlyRollup",
            "personalUsdLimit:notDefined",
            "sharedPoolAlerts:aggregateEnforcementCoverageNotProven",
            "onBehalfUsage:dailyAttributionHasNoLocalHourSplit",
        ]
        if window and any(
            instant.minute or instant.second or instant.microsecond
            for instant in (window.start_at, window.end_at)
        ):
            unsupported.append("subhourLocalCalendarBoundary:hourlyRollupCannotSplit")
        if end - start > timedelta(days=366):
            return UsageNotificationSnapshot(
                observed_at=now,
                coverage="unavailable",
                unsupported=[*unsupported, "catchUpExceeds366DaySourceBound"],
            )
        context = await self._usage.notification_context(actor, start=start.date(), end=end.date())
        through = context.through
        if through is None:
            unsupported.append("telemetryCoverage:verifyApiDiagnosticsAndRollupCoverage")
        coverage = _coverage(context, start, end if window else through or now, now)
        resources = (
            [_summary_resource(item, context, window, now) for item in context.entitlements]
            if window
            else []
        )
        if not context.entitlements:
            coverage = "unavailable"
        elif any(resource.coverage == "incomplete" for resource in resources):
            coverage = "incomplete"
        calls = _sum([resource.calls for resource in resources]) if resources else None
        model_resources = [
            resource
            for resource in resources
            if resource.subject.resource
            and resource.subject.resource.kind != EntitlementResourceKind.MCP_SERVER
        ]
        tokens = (
            _sum([resource.tokens for resource in model_resources]) if model_resources else None
        )
        costs = [resource.cost_usd for resource in resources]
        cost = (
            sum(value for value in costs if value is not None)
            if (costs and all(value is not None for value in costs) and coverage == "complete")
            else None
        )
        return UsageNotificationSnapshot(
            observed_at=now,
            through=through,
            freshness_status=context.freshness.status if context.freshness else None,
            freshness_updated_at=context.freshness.updated_at if context.freshness else None,
            freshness_interval_minutes=(
                context.freshness.interval_minutes if context.freshness else None
            ),
            coverage_start=context.coverage_start,
            coverage=coverage,
            calls=calls,
            tokens=tokens,
            cost_usd=cost,
            unpriced_tokens=sum(
                resource.tokens or 0 for resource in resources if resource.cost_usd is None
            ),
            resources=resources,
            counters=_counters(actor, context, now),
            shared_monthly=list(context.shared_monthly),
            unsupported=unsupported,
        )
