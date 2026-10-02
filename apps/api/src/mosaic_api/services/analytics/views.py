"""The overview, model, reliability and unattributed reports, built from loaded summaries."""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from mosaic_api.pricing import round_cost
from mosaic_api.services.analytics.cost import HOURS_NOTE, CostBook, CostTally, add_cost
from mosaic_api.services.analytics.models import (
    AnalyticsApiRow,
    AnalyticsBreakdownRow,
    AnalyticsCostSummary,
    AnalyticsDenialReason,
    AnalyticsDenialRow,
    AnalyticsDeploymentRow,
    AnalyticsGatewayHealth,
    AnalyticsLatency,
    AnalyticsLatencyBucket,
    AnalyticsModelRow,
    AnalyticsModels,
    AnalyticsOverview,
    AnalyticsReliability,
    AnalyticsSeries,
    AnalyticsSpend,
    AnalyticsStatusMix,
    AnalyticsUnattributed,
    AnalyticsUnattributedRow,
    UnattributedReason,
)
from mosaic_api.services.analytics.rows import (
    Activity,
    Point,
    Ranked,
    average_backend,
    average_latency,
    bucket_points,
    denial_label,
    entries,
    fold,
    hour_kpis,
    hours_between,
    kpis,
    period_start,
    rank,
    share,
    total,
    trend,
    usage_values,
)
from mosaic_api.services.analytics.scope import Scope
from mosaic_api.services.analytics.window import Coverage, Window
from mosaic_api.usage_telemetry import (
    LATENCY_BUCKETS_MS,
    RolledUpApi,
    UsageMetrics,
    UsageSummary,
    latency_percentile,
)

TOP = 10
MODEL_SERIES = 5
DENIAL_ROWS = 100
ROW_LIMIT = 500
# Azure OpenAI's standard deployments count capacity in thousands of tokens a minute. Other
# providers' models on a Foundry resource are usually deployed at a capacity of 1 and are held to
# a regional rate limit instead, so their capacity says nothing about tokens.
_TOKEN_CAPACITY_SKUS = {"standard", "globalstandard", "datazonestandard"}
_TOKEN_CAPACITY_FORMATS = {"openai"}
PEAKS_NOTE = (
    "A deployment reached through more than one gateway reports the busiest minute on any one of "
    "them, so its true peak may be higher."
)


@dataclass(frozen=True)
class DeploymentInfo:
    endpoint_name: str | None
    model_name: str | None
    model_format: str | None
    sku_name: str | None
    sku_capacity: int | None

    @property
    def capacity_tokens_per_minute(self) -> int | None:
        sku = (self.sku_name or "").casefold().replace(" ", "")
        model_format = (self.model_format or "").strip().casefold()
        if (
            sku in _TOKEN_CAPACITY_SKUS
            and model_format in _TOKEN_CAPACITY_FORMATS
            and self.sku_capacity
        ):
            return self.sku_capacity * 1000
        return None


def deployment_model(api: RolledUpApi | None, observed: Mapping[str, DeploymentInfo]) -> str | None:
    """The model MOSAIC knows a model API calls: its deployment's model, or the deployment.

    Lowercased, as the model breakdown keeps the models the LLM log names.
    """

    if api is None or api.kind != "model":
        return None
    key = api.deployment_key
    info = observed.get(key) if key else None
    name = (info.model_name if info else None) or api.deployment_name
    return name.casefold() if name else None


@dataclass
class Context:
    scope: Scope
    window: Window
    coverage: Coverage | None
    base: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    # How many rows a list keeps, and a top list such as denied callers. Exports keep more.
    limit: int = ROW_LIMIT
    top_limit: int = DENIAL_ROWS

    def report(self, *extra: str) -> dict[str, Any]:
        notes = [*self.base["notes"], *self.notes, *(note for note in extra if note)]
        return {**self.base, "notes": list(dict.fromkeys(notes))}


def resolved_caller(scope: Scope, grant_key: str, caller: str) -> str | None:
    """Who made a grant's calls: the recorded caller, or the subject of a direct grant."""

    if caller:
        return caller.casefold()
    grant = scope.grants.get(grant_key)
    if grant is not None and not grant.per_member and grant.subject_object_id:
        return grant.subject_object_id
    return None


def within(summary: UsageSummary, first: date, last: date) -> bool:
    return first <= date.fromisoformat(summary.period_start) <= last


def _activity(
    context: Context,
    api: Sequence[UsageSummary],
    callers: Sequence[UsageSummary],
    unattributed: Sequence[UsageSummary],
) -> Activity:
    scope, window = context.scope, context.window
    who: set[str] = set()
    grants: set[str] = set()
    for _, entry in entries(callers, scope, "grantCaller"):
        if entry.metrics.requests <= 0:
            continue
        grant_key, _, caller = entry.key.rpartition("|")
        grant = scope.grants.get(grant_key)
        grants.add(grant.entitlement_id if grant and grant.entitlement_id else grant_key)
        if (object_id := resolved_caller(scope, grant_key, caller)) is not None:
            who.add(object_id)
    apis = {
        (summary.gateway_id, entry.key)
        for summary, entry in entries(api, scope, "api")
        if entry.metrics.requests > 0 and within(summary, window.first_day, window.last_day)
    }
    stray = sum(entry.metrics.requests for _, entry in entries(unattributed, scope, "unattributed"))
    return Activity(callers=len(who), grants=len(grants), apis=len(apis), unattributed=stray)


def window_cost(
    costs: CostBook,
    scope: Scope,
    api: Iterable[UsageSummary],
    first: date,
    last: date,
) -> CostTally:
    """What the window's calls cost, and the reserved capacity nobody called, in scope."""

    tally = CostTally()
    for summary, entry in entries(api, scope, "api"):
        if within(summary, first, last):
            costs.summary_cost(summary, entry.key, entry.metrics, tally)
    for key in costs.idle_keys():
        idle = costs.idle(key, first, last)
        if idle > 0:
            tally.add_amount(idle, idle, key)
    return tally


def bucket_costs(
    costs: CostBook, scope: Scope, window: Window, api: Iterable[UsageSummary]
) -> dict[int, float | None]:
    found: dict[int, float | None] = {}
    for summary, entry in entries(api, scope, "api"):
        index = window.bucket_index(period_start(summary))
        if index is not None:
            found[index] = add_cost(
                found.get(index), costs.summary_cost(summary, entry.key, entry.metrics)
            )
    idle_keys = costs.idle_keys()
    for index, start in enumerate(window.buckets):
        first = start.date()
        last = (window.bucket_end(start) - timedelta(microseconds=1)).date()
        for key in idle_keys:
            idle = costs.idle(key, max(first, window.first_day), min(last, window.last_day))
            if idle > 0:
                found[index] = add_cost(found.get(index), idle)
    return found


def overview(
    context: Context,
    *,
    api: Sequence[UsageSummary],
    previous_api: Sequence[UsageSummary],
    models: Sequence[UsageSummary],
    callers: Sequence[UsageSummary],
    unattributed: Sequence[UsageSummary],
    gateways: list[AnalyticsGatewayHealth],
    costs: CostBook | None = None,
    spend: AnalyticsSpend | None = None,
) -> AnalyticsOverview:
    scope, window, coverage = context.scope, context.window, context.coverage
    activity = _activity(context, api, callers, unattributed)
    covered_before = coverage is not None and coverage.covers(
        window.previous_start, window.previous_end
    )
    hourly = window.granularity == "hour"
    # MOSAIC prices whole days, so a window by the hour carries no cost.
    priced = None if hourly else costs
    tally: CostTally | None = None
    if hourly:
        current = hour_kpis(hours_between(api, scope, window.start, window.end), activity)
        before = hours_between(api, scope, window.previous_start, window.previous_end)
        previous = hour_kpis(before, Activity()) if covered_before else None
    else:
        in_window = [s for s in api if within(s, window.first_day, window.last_day)]
        current = kpis(total(fold(in_window, scope, "api").values()), activity)
        previous = (
            kpis(total(fold(previous_api, scope, "api").values()), Activity())
            if covered_before
            else None
        )
        if priced is not None:
            tally = window_cost(priced, scope, in_window, window.first_day, window.last_day)
            current = current.model_copy(
                update={"cost": None if tally.total is None else round(tally.total, 4)}
            )
            if previous is not None:
                before_tally = window_cost(
                    priced,
                    scope,
                    previous_api,
                    window.previous_first_day,
                    window.previous_last_day,
                )
                previous = previous.model_copy(
                    update={
                        "cost": None
                        if before_tally.total is None
                        else round(before_tally.total, 4)
                    }
                )

    by_model: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    model_costs: dict[str, float | None] = {}
    series: dict[str, dict[int, int]] = defaultdict(dict)
    for summary, entry in entries(models, scope, "model"):
        api_name = entry.key.rpartition("|")[2]
        model = scope.model_label(summary.gateway_id, entry.key)
        by_model[model].add(entry.metrics)
        if priced is not None:
            model_costs[model] = add_cost(
                model_costs.get(model), priced.summary_cost(summary, api_name, entry.metrics)
            )
        index = window.bucket_index(period_start(summary))
        if index is not None and not hourly:
            series[model][index] = series[model].get(index, 0) + entry.metrics.total_tokens
    model_total = total(by_model.values())
    top_models = rank(
        (
            Ranked(model, model, None, metrics, model_costs.get(model))
            for model, metrics in by_model.items()
        ),
        requests=model_total.requests,
        tokens=model_total.total_tokens,
        limit=TOP,
    )
    model_trend: list[AnalyticsSeries] = []
    if not hourly:
        known = [
            coverage is not None and coverage.known(start, window.bucket_end(start))
            for start in window.buckets
        ]
        for row in top_models[:MODEL_SERIES]:
            values = series.get(row.key, {})
            model_trend.append(
                AnalyticsSeries(
                    key=row.key,
                    label=row.label,
                    values=[
                        values.get(index, 0) if known[index] else None
                        for index in range(len(window.buckets))
                    ],
                )
            )

    by_caller: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    caller_costs: dict[str, float | None] = {}
    by_cost_center: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    cost_center_costs: dict[str, float | None] = {}
    cost_center_names: dict[str, tuple[str, str]] = {}
    for summary, entry in entries(callers, scope, "grantCaller"):
        grant_key, _, caller = entry.key.rpartition("|")
        object_id = resolved_caller(scope, grant_key, caller)
        cost = (
            priced.grant_cost(
                summary.gateway_id,
                grant_key,
                summary.period,
                date.fromisoformat(summary.period_start),
                entry.metrics,
            )
            if priced is not None
            else None
        )
        if object_id is not None:
            by_caller[object_id].add(entry.metrics)
            if priced is not None:
                caller_costs[object_id] = add_cost(caller_costs.get(object_id), cost)
        cost_center = scope.cost_center(scope.grants.get(grant_key))
        if cost_center is not None:
            by_cost_center[cost_center.id].add(entry.metrics)
            cost_center_costs[cost_center.id] = add_cost(
                cost_center_costs.get(cost_center.id), cost
            )
            cost_center_names[cost_center.id] = (cost_center.name, cost_center.code)
    linked = total(by_caller.values())
    linked_by_cost_center = total(by_cost_center.values())
    top_cost_centers = rank(
        (
            Ranked(
                cost_center_id,
                cost_center_names[cost_center_id][0],
                cost_center_names[cost_center_id][1],
                metrics,
                cost_center_costs.get(cost_center_id),
            )
            for cost_center_id, metrics in by_cost_center.items()
        ),
        requests=linked_by_cost_center.requests,
        tokens=linked_by_cost_center.total_tokens,
        limit=TOP,
    )
    ranked_callers: list[Ranked] = []
    for object_id, metrics in by_caller.items():
        name = scope.caller(object_id)
        ranked_callers.append(
            Ranked(object_id, name.label, name.detail, metrics, caller_costs.get(object_id))
        )
    top_callers = rank(
        ranked_callers, requests=linked.requests, tokens=linked.total_tokens, limit=TOP
    )

    in_range = [s for s in api if within(s, window.first_day, window.last_day)]
    api_metrics = fold(in_range, scope, "api")
    api_costs: dict[tuple[str, str], float | None] = {}
    if priced is not None:
        for summary, entry in entries(in_range, scope, "api"):
            key = (summary.gateway_id, entry.key)
            api_costs[key] = add_cost(
                api_costs.get(key), priced.summary_cost(summary, entry.key, entry.metrics)
            )
    # Over the last 24 hours the APIs cover whole days, so their shares are of those days.
    api_total = total(api_metrics.values())
    # Model APIs and MCP servers share this list, and MCP calls carry no tokens, so calls rank it.
    top_apis = rank(
        (
            Ranked(
                f"{gateway_id}/{name}",
                scope.api_label(gateway_id, name),
                scope.gateway_name(gateway_id),
                metrics,
                api_costs.get((gateway_id, name)),
            )
            for (gateway_id, name), metrics in api_metrics.items()
        ),
        requests=api_total.requests,
        tokens=api_total.total_tokens,
        limit=TOP,
        by="requests",
    )
    points = bucket_points(window, api, scope)
    if priced is not None:
        for index, value in bucket_costs(priced, scope, window, api).items():
            points.setdefault(index, Point()).cost = value
    cost_summary: AnalyticsCostSummary | None = None
    if costs is not None:
        cost_summary = (
            AnalyticsCostSummary(total=None, notes=[HOURS_NOTE])
            if tally is None
            else tally.summary(costs.notes)
        )
    return AnalyticsOverview(
        **context.report(),
        kpis=current,
        previous=previous,
        trend=trend(window, coverage, points),
        model_trend=model_trend,
        top_models=top_models,
        top_callers=top_callers,
        top_apis=top_apis,
        top_cost_centers=top_cost_centers,
        gateways=gateways,
        cost=cost_summary,
        spend=spend,
    )


def api_costs(
    costs: CostBook | None, scope: Scope, api: Iterable[UsageSummary]
) -> dict[tuple[str, str], float | None]:
    """Each API's cost, summed across the summaries read."""

    found: dict[tuple[str, str], float | None] = {}
    if costs is None:
        return found
    for summary, entry in entries(api, scope, "api"):
        key = (summary.gateway_id, entry.key)
        found[key] = add_cost(found.get(key), costs.summary_cost(summary, entry.key, entry.metrics))
    return found


def api_rows(
    scope: Scope,
    metrics_by_api: dict[tuple[str, str], UsageMetrics],
    models: Iterable[UsageSummary] | None,
    costs_by_api: dict[tuple[str, str], float | None] | None = None,
) -> list[AnalyticsApiRow]:
    """Every API with calls in the window, and every governed one in scope without any.

    ``models`` is None when the report didn't read the model dimension, and the rows then name
    no models rather than claiming none served them.
    """

    served: dict[tuple[str, str], set[str]] = defaultdict(set)
    for summary, entry in entries(models or [], scope, "model"):
        name = entry.key.rpartition("|")[2]
        served[(summary.gateway_id, name)].add(scope.model_label(summary.gateway_id, entry.key))
    wanted = dict(metrics_by_api)
    for gateway in scope.gateways.values():
        for api in scope.governed.get(gateway.id, []):
            if scope.api_allowed(gateway.id, api.api_name):
                wanted.setdefault((gateway.id, api.api_name), UsageMetrics())
    combined = total(wanted.values())
    rows: list[AnalyticsApiRow] = []
    for (gateway_id, name), metrics in wanted.items():
        known = scope.apis.get((gateway_id, name))
        rows.append(
            AnalyticsApiRow(
                **usage_values(
                    metrics,
                    combined.requests,
                    combined.total_tokens,
                    (costs_by_api or {}).get((gateway_id, name)),
                ),
                key=f"{gateway_id}/{name}",
                gateway_id=gateway_id,
                gateway_name=scope.gateway_name(gateway_id),
                api_name=name,
                label=known.display_name if known else name,
                kind=known.kind if known else None,
                resource_id=known.resource_id if known else None,
                removed=known is None
                or known.removed_at is not None
                or gateway_id not in scope.all_gateways,
                metered_requests=metrics.metered_requests,
                denied=metrics.denied,
                client_errors=metrics.client_errors,
                server_errors=metrics.server_errors,
                backend_throttled=metrics.backend_throttled,
                p95_latency_ms=latency_percentile(metrics.latency, 95),
                average_latency_ms=average_latency(metrics),
                error_rate=share(metrics.errors, metrics.requests),
                models=(
                    sorted(served.get((gateway_id, name), set())) if models is not None else None
                ),
            )
        )
    rows.sort(key=lambda row: (-row.total_tokens, -row.requests, row.label.casefold()))
    return rows


def _breakdown(
    groups: dict[str, tuple[str, UsageMetrics, float | None]], requests: int, tokens: int
) -> list[AnalyticsBreakdownRow]:
    rows = [
        AnalyticsBreakdownRow(
            **usage_values(metrics, requests, tokens, cost),
            key=key,
            label=label,
            denied=metrics.denied,
        )
        for key, (label, metrics, cost) in groups.items()
    ]
    rows.sort(key=lambda row: (-row.total_tokens, -row.requests, row.label.casefold()))
    return rows


def models_report(
    context: Context,
    *,
    api: Sequence[UsageSummary],
    models: Sequence[UsageSummary],
    deployments: Sequence[UsageSummary],
    observed: dict[str, DeploymentInfo],
    costs: CostBook | None = None,
) -> AnalyticsModels:
    scope, window = context.scope, context.window
    metrics_by_api = fold(api, scope, "api")
    combined = total(metrics_by_api.values())
    requests, tokens = combined.requests, combined.total_tokens
    costs_by_api = api_costs(costs, scope, api)

    by_model: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    model_costs: dict[str, float | None] = {}
    model_apis: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for summary, entry in entries(models, scope, "model"):
        name = entry.key.rpartition("|")[2]
        model = scope.model_label(summary.gateway_id, entry.key)
        by_model[model].add(entry.metrics)
        model_apis[model].add((summary.gateway_id, name))
        if costs is not None:
            model_costs[model] = add_cost(
                model_costs.get(model), costs.summary_cost(summary, name, entry.metrics)
            )
    model_total = total(by_model.values())
    model_rows = [
        AnalyticsModelRow(
            **usage_values(
                metrics, model_total.requests, model_total.total_tokens, model_costs.get(model)
            ),
            model=model,
            apis=len(model_apis[model]),
        )
        for model, metrics in by_model.items()
    ]
    model_rows.sort(key=lambda row: (-row.total_tokens, -row.requests, row.model))

    by_deployment: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    deployment_costs: dict[str, float | None] = {}
    reached: dict[str, set[str]] = defaultdict(set)
    hourly: dict[str, list[int]] = {}
    for summary, entry in entries(deployments, scope, "deployment"):
        by_deployment[entry.key].add(entry.metrics)
        reached[entry.key].add(summary.gateway_id)
        if costs is not None:
            deployment_costs[entry.key] = add_cost(
                deployment_costs.get(entry.key),
                costs.price(
                    entry.key,
                    summary.period,
                    date.fromisoformat(summary.period_start),
                    entry.metrics,
                ).amount,
            )
        if entry.hourly_peak_tokens and summary.period == "day":
            peaks = hourly.setdefault(entry.key, [0] * 24)
            for hour, value in enumerate(entry.hourly_peak_tokens[:24]):
                peaks[hour] = max(peaks[hour], value)
    deployment_total = total(by_deployment.values())
    deployment_rows: list[AnalyticsDeploymentRow] = []
    for key, metrics in by_deployment.items():
        endpoint_id, _, deployment_name = key.partition("/")
        info = observed.get(key)
        capacity = info.capacity_tokens_per_minute if info else None
        deployment_rows.append(
            AnalyticsDeploymentRow(
                **usage_values(
                    metrics,
                    deployment_total.requests,
                    deployment_total.total_tokens,
                    deployment_costs.get(key),
                ),
                key=key,
                endpoint_id=endpoint_id,
                endpoint_name=info.endpoint_name if info else None,
                deployment_name=deployment_name,
                model_name=info.model_name if info else None,
                sku_name=info.sku_name if info else None,
                capacity_tokens_per_minute=capacity,
                backend_throttled=metrics.backend_throttled,
                peak_minute_tokens=metrics.peak_minute_tokens,
                peak_minute_requests=metrics.peak_minute_requests,
                utilization=(round(metrics.peak_minute_tokens / capacity, 4) if capacity else None),
                gateways=len(reached[key]),
                hourly_peak_tokens=(
                    hourly.get(key) if context.window.granularity != "month" else None
                ),
            )
        )
    deployment_rows.sort(key=lambda row: (-row.total_tokens, -row.requests, row.key))

    by_gateway: dict[str, tuple[str, UsageMetrics, float | None]] = {}
    by_environment: dict[str, tuple[str, UsageMetrics, float | None]] = {}
    for (gateway_id, api_name), metrics in metrics_by_api.items():
        cost = costs_by_api.get((gateway_id, api_name))
        label, gateway_metrics, gateway_cost = by_gateway.setdefault(
            gateway_id, (scope.gateway_name(gateway_id), UsageMetrics(), None)
        )
        gateway_metrics.add(metrics)
        by_gateway[gateway_id] = (label, gateway_metrics, add_cost(gateway_cost, cost))
        gateway = scope.all_gateways.get(gateway_id)
        environment = (gateway.environment or "") if gateway else "removed"
        name = scope.environment_name(gateway.environment) if gateway else "Removed gateways"
        _, environment_metrics, environment_cost = by_environment.setdefault(
            environment, (name, UsageMetrics(), None)
        )
        environment_metrics.add(metrics)
        by_environment[environment] = (
            name,
            environment_metrics,
            add_cost(environment_cost, cost),
        )
    shared = any(len(gateways) > 1 for gateways in reached.values())
    cost_summary: AnalyticsCostSummary | None = None
    if costs is not None:
        cost_summary = window_cost(
            costs, scope, api, window.first_day, window.last_day
        ).summary(costs.notes)
    return AnalyticsModels(
        **context.report(PEAKS_NOTE if shared else ""),
        apis=api_rows(scope, metrics_by_api, models, costs_by_api),
        models=model_rows,
        deployments=deployment_rows,
        gateways=_breakdown(by_gateway, requests, tokens),
        environments=_breakdown(by_environment, requests, tokens),
        cost=cost_summary,
    )


def denial_rows(
    scope: Scope, denials: Iterable[UsageSummary], limit: int
) -> list[AnalyticsDenialRow]:
    folded = fold(denials, scope, "denial")
    ordered = sorted(folded.items(), key=lambda item: (-item[1].requests, item[0]))
    lookups: list[AnalyticsDenialRow] = []
    for (gateway_id, key), metrics in ordered[:limit]:
        reason, caller, client, name = [*key.split("|", 3), "", "", ""][:4]
        caller_name = scope.caller(caller) if caller else None
        client_name = scope.application(client, [caller] if caller else ()) if client else None
        lookups.append(
            AnalyticsDenialRow(
                reason=reason,
                reason_label=denial_label(reason),
                caller_object_id=caller or None,
                caller_label=caller_name.label if caller_name else None,
                client_app_id=client or None,
                client_app_label=client_name.label if client_name else None,
                gateway_id=gateway_id,
                gateway_name=scope.gateway_name(gateway_id),
                api_name=name,
                api_label=scope.api_label(gateway_id, name),
                requests=metrics.requests,
                last_seen=metrics.last_seen,
            )
        )
    return lookups


def reliability(
    context: Context,
    *,
    api: Sequence[UsageSummary],
    trend_api: Sequence[UsageSummary],
    denials: Sequence[UsageSummary],
) -> AnalyticsReliability:
    scope, window = context.scope, context.window
    metrics_by_api = fold(api, scope, "api")
    metrics = total(metrics_by_api.values())
    buckets = [
        AnalyticsLatencyBucket(upper_ms=bound, count=metrics.latency[index])
        for index, bound in enumerate(LATENCY_BUCKETS_MS)
    ]
    buckets.append(
        AnalyticsLatencyBucket(upper_ms=None, count=sum(metrics.latency[len(LATENCY_BUCKETS_MS) :]))
    )
    reasons: dict[str, int] = defaultdict(int)
    for _, entry in entries(denials, scope, "denial"):
        reasons[entry.key.split("|", 1)[0]] += entry.metrics.requests
    refused = sum(reasons.values())
    apis = [row for row in api_rows(scope, metrics_by_api, None) if row.requests > 0]
    apis.sort(
        key=lambda row: (
            -(row.errors + row.throttled + row.quota_refused + row.denied),
            -row.requests,
            row.label.casefold(),
        )
    )
    return AnalyticsReliability(
        **context.report(),
        status_mix=AnalyticsStatusMix(
            requests=metrics.requests,
            ok=metrics.ok,
            throttled=metrics.throttled,
            quota_refused=metrics.quota,
            denied=metrics.denied,
            client_errors=metrics.client_errors,
            server_errors=metrics.server_errors,
            backend_throttled=metrics.backend_throttled,
        ),
        latency=AnalyticsLatency(
            p50_ms=latency_percentile(metrics.latency, 50),
            p95_ms=latency_percentile(metrics.latency, 95),
            p99_ms=latency_percentile(metrics.latency, 99),
            average_ms=average_latency(metrics),
            average_backend_ms=average_backend(metrics),
            buckets=buckets,
        ),
        trend=trend(window, context.coverage, bucket_points(window, trend_api, scope)),
        apis=apis,
        denial_reasons=[
            AnalyticsDenialReason(
                reason=reason,
                label=denial_label(reason),
                requests=count,
                share=share(count, refused),
            )
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        ],
        denials=denial_rows(scope, denials, context.top_limit),
    )


def unattributed_report(
    context: Context,
    *,
    api: Sequence[UsageSummary],
    unattributed: Sequence[UsageSummary],
    costs: CostBook | None = None,
) -> AnalyticsUnattributed:
    scope = context.scope
    admitted = sum(
        max(0, metrics.requests - metrics.denied) for metrics in fold(api, scope, "api").values()
    )
    folded = fold(unattributed, scope, "unattributed")
    row_costs: dict[tuple[str, str], float | None] = {}
    tally = CostTally()
    if costs is not None:
        for summary, entry in entries(unattributed, scope, "unattributed"):
            row_key = (summary.gateway_id, entry.key)
            row_costs[row_key] = add_cost(
                row_costs.get(row_key),
                costs.summary_cost(summary, entry.key.partition("|")[0], entry.metrics, tally),
            )
    stray = total(folded.values())
    ordered = sorted(folded.items(), key=lambda item: (-item[1].requests, item[0]))
    rows: list[AnalyticsUnattributedRow] = []
    for (gateway_id, key), metrics in ordered[: context.limit]:
        name, _, subscription = key.partition("|")
        api_record = scope.apis.get((gateway_id, name))
        shared_key = api_record.subscription_name if api_record else None
        reason: UnattributedReason = "noSubscription"
        if subscription:
            reason = (
                "sharedKey"
                if shared_key and subscription.casefold() == shared_key.casefold()
                else "unknownSubscription"
            )
        rows.append(
            AnalyticsUnattributedRow(
                gateway_id=gateway_id,
                gateway_name=scope.gateway_name(gateway_id),
                api_name=name,
                api_label=scope.api_label(gateway_id, name),
                subscription=subscription or None,
                reason=reason,
                requests=metrics.requests,
                total_tokens=metrics.total_tokens,
                last_seen=metrics.last_seen,
                share=share(metrics.requests, stray.requests),
                cost=round_cost(row_costs.get((gateway_id, key))),
            )
        )
    return AnalyticsUnattributed(
        **context.report(),
        requests=stray.requests,
        total_tokens=stray.total_tokens,
        admitted_requests=admitted,
        share=share(stray.requests, admitted),
        rows=rows,
        truncated=len(ordered) > context.limit,
        cost=tally.summary(costs.notes) if costs is not None else None,
    )


def lag_minutes(now: datetime, through: datetime | None) -> int | None:
    if through is None:
        return None
    return max(0, int((now - through).total_seconds() // 60))
