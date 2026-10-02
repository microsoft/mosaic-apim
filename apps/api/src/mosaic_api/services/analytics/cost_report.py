"""The Cost tab, this month's spend and forecast, and the chargeback export. See ADR 0020."""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from mosaic_api.domain import CostCenterRef
from mosaic_api.pricing import cloud_label, days_in_month, month_first, month_last, round_cost
from mosaic_api.services.analytics.consumers import consumers_report
from mosaic_api.services.analytics.cost import (
    HOURS_NOTE,
    CostBook,
    Priced,
    add_cost,
)
from mosaic_api.services.analytics.models import (
    AnalyticsCost,
    AnalyticsCostDeploymentRow,
    AnalyticsCostRow,
    AnalyticsCostTrendPoint,
    AnalyticsSpend,
)
from mosaic_api.services.analytics.rows import bucket_points, entries, fold
from mosaic_api.services.analytics.scope import UNKNOWN_MODEL, GrantInfo, Scope
from mosaic_api.services.analytics.views import (
    Context,
    api_costs,
    bucket_costs,
    window_cost,
)
from mosaic_api.services.analytics.window import add_months, bucket_known
from mosaic_api.usage_telemetry import UsageMetrics, UsageSummary

TOP_CONSUMERS = 10
CHARGEBACK_NOTE = (
    "Each grant's calls are charged to its subject: the person, application, or security group "
    "it was granted to, under the grant's cost center."
)
UNATTRIBUTED_PARTY = "Unattributed calls"
IDLE_PARTY = "Reserved capacity with no calls"


def spend_report(
    costs: CostBook,
    scope: Scope,
    api: Iterable[UsageSummary],
    today: date,
    through: datetime | None,
) -> AnalyticsSpend:
    """This calendar month so far, and the month's end at the same pace.

    Pay-as-you-go spend is projected from the time MOSAIC has figures for, to the hour. Reserved
    capacity costs what its hours cost, so each provisioned deployment's share is projected to its
    whole month, counting only the days it exists.
    """

    first = month_first(today)
    tally = window_cost(costs, scope, api, first, today)
    length = days_in_month(today)
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    elapsed = max(0.0, (through - start).total_seconds() / 86_400) if through else 0.0
    so_far = tally.total
    forecast: float | None = None
    if so_far is not None and elapsed >= 1:
        metered = so_far - tally.reserved
        reserved = 0.0
        for key, amount in tally.reserved_by_key.items():
            to_date = costs.reserved_month(key, first)
            month = costs.pricer.reserved_cost(key, first, month_last(first))
            reserved += amount * month / to_date if to_date and month is not None else amount
        forecast = metered * length / elapsed + reserved
    return AnalyticsSpend(
        month_start=first,
        days_in_month=length,
        days_elapsed=round(elapsed, 2),
        through=through,
        month_to_date=round_cost(so_far),
        reserved=round_cost(tally.reserved) if tally.reserved else None,
        forecast=round_cost(forecast),
        unpriced_tokens=tally.unpriced_tokens,
        unpriced_items=len(tally.left),
    )


def _cost_rows(
    rows: dict[str, tuple[str, str | None, str | None, UsageMetrics, float | None]],
    total_cost: float | None,
) -> list[AnalyticsCostRow]:
    found = [
        AnalyticsCostRow(
            key=key,
            label=label,
            detail=detail,
            kind=kind,
            requests=metrics.requests,
            total_tokens=metrics.total_tokens,
            cost=round_cost(cost),
            cost_share=(
                round(cost / total_cost, 4) if cost is not None and total_cost else None
            ),
        )
        for key, (label, detail, kind, metrics, cost) in rows.items()
    ]
    found.sort(
        key=lambda row: (
            row.cost is None,
            -(row.cost or 0.0),
            -row.total_tokens,
            row.label.casefold(),
        )
    )
    return found


def _utilization(
    costs: CostBook, key: str, metrics: UsageMetrics, minutes: float
) -> float | None:
    facts = costs.pricer.facts_for(key)
    if facts is None or not facts.provisioned or not facts.capacity or minutes <= 0:
        return None
    throughput = costs.pricer.book.throughput_for(facts.model, facts.version)
    if throughput is None:
        return None
    normalized = (
        metrics.prompt_tokens + throughput.output_to_input_ratio * metrics.completion_tokens
    )
    capacity = facts.capacity * throughput.input_tokens_per_minute_per_ptu * minutes
    return round(normalized / capacity, 4) if capacity > 0 else None


def cost_report(
    context: Context,
    *,
    api: Sequence[UsageSummary],
    models: Sequence[UsageSummary],
    deployments: Sequence[UsageSummary],
    callers: Sequence[UsageSummary],
    costs: CostBook,
    spend: AnalyticsSpend | None,
    now: datetime,
) -> AnalyticsCost:
    scope, window = context.scope, context.window
    hourly = window.granularity == "hour"
    tally = window_cost(costs, scope, api, window.first_day, window.last_day)

    points = bucket_points(window, api, scope)
    per_bucket = {} if hourly else bucket_costs(costs, scope, window, api)
    trend = [
        AnalyticsCostTrendPoint(
            start=start,
            cost=(
                None
                if hourly or not bucket_known(window, context.coverage, start)
                else round_cost(per_bucket.get(index))
            ),
            total_tokens=(
                points[index].total_tokens
                if index in points and bucket_known(window, context.coverage, start)
                else None
            ),
        )
        for index, start in enumerate(window.buckets)
    ]

    by_model: dict[str, tuple[str, str | None, str | None, UsageMetrics, float | None]] = {}
    for item, entry in entries(models, scope, "model"):
        api_name = entry.key.rpartition("|")[2]
        label = scope.model_label(item.gateway_id, entry.key)
        current = by_model.get(label) or (label, None, None, UsageMetrics(), None)
        current[3].add(entry.metrics)
        by_model[label] = (
            label,
            None,
            None,
            current[3],
            add_cost(current[4], costs.summary_cost(item, api_name, entry.metrics)),
        )

    by_api: dict[str, tuple[str, str | None, str | None, UsageMetrics, float | None]] = {}
    costs_by_api = api_costs(costs, scope, api)
    for (gateway_id, api_name), metrics in fold(api, scope, "api").items():
        known = scope.apis.get((gateway_id, api_name))
        if known is not None and known.kind == "mcp":
            continue
        by_api[f"{gateway_id}/{api_name}"] = (
            scope.api_label(gateway_id, api_name),
            scope.gateway_name(gateway_id),
            None,
            metrics,
            costs_by_api.get((gateway_id, api_name)),
        )

    consumer_report = consumers_report(context, callers=callers, clients=[], costs=costs)
    by_consumer: dict[str, tuple[str, str | None, str | None, UsageMetrics, float | None]] = {}
    # Callers only: a security group's row repeats its members' calls, so ranking it beside them
    # would count those calls twice.
    for row in [*consumer_report.people, *consumer_report.applications]:
        metrics = UsageMetrics(requests=row.requests, total_tokens=row.total_tokens)
        by_consumer[f"{row.kind}:{row.key}"] = (row.label, row.detail, row.kind, metrics, row.cost)

    by_cost_center: dict[str, tuple[str, str | None, str | None, UsageMetrics, float | None]] = {}
    for center in consumer_report.cost_centers:
        metrics = UsageMetrics(requests=center.requests, total_tokens=center.total_tokens)
        by_cost_center[center.key] = (center.label, center.code, "costCenter", metrics, center.cost)

    total_cost = None if tally.total is None else round(tally.total, 4)
    deployment_rows = _deployment_rows(context, deployments, costs, now, total_cost)
    notes = [HOURS_NOTE] if hourly else []
    summary = tally.summary([*notes, *costs.notes, CHARGEBACK_NOTE])
    return AnalyticsCost(
        **context.report(),
        spend=spend,
        cost=summary,
        trend=trend,
        models=_cost_rows(by_model, summary.total),
        deployments=deployment_rows,
        consumers=_cost_rows(by_consumer, summary.total)[:TOP_CONSUMERS],
        apis=_cost_rows(by_api, summary.total),
        cost_centers=_cost_rows(by_cost_center, summary.total),
    )


def _deployment_rows(
    context: Context,
    deployments: Sequence[UsageSummary],
    costs: CostBook,
    now: datetime,
    total_cost: float | None,
) -> list[AnalyticsCostDeploymentRow]:
    scope, window = context.scope, context.window
    metrics_by_key: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    cost_by_key: dict[str, float | None] = {}
    reached: dict[str, set[str]] = defaultdict(set)
    for item, entry in entries(deployments, scope, "deployment"):
        key = entry.key.casefold()
        metrics_by_key[key].add(entry.metrics)
        reached[key].add(item.gateway_id)
        cost_by_key[key] = add_cost(
            cost_by_key.get(key),
            costs.price(
                entry.key, item.period, date.fromisoformat(item.period_start), entry.metrics
            ).amount,
        )
    idle_keys = {key.casefold() for key in costs.idle_keys()}
    names = {key.casefold(): key for key in [*metrics_by_key, *costs.idle_keys()]}
    minutes = max(0.0, (min(window.end, now) - window.start).total_seconds() / 60)
    today = window.today
    rows: list[AnalyticsCostDeploymentRow] = []
    for folded in sorted(set(metrics_by_key) | idle_keys):
        key = names.get(folded, folded)
        metrics = metrics_by_key.get(folded, UsageMetrics())
        facts = costs.pricer.facts_for(key)
        rate = costs.pricer.rate(key, today)
        price = rate.entry
        idle = costs.idle(key, window.first_day, window.last_day) if folded in idle_keys else 0.0
        cost = add_cost(cost_by_key.get(folded), idle if idle > 0 else None)
        endpoint_id, _, deployment_name = key.partition("/")
        rows.append(
            AnalyticsCostDeploymentRow(
                key=key,
                endpoint_id=facts.endpoint_id if facts else endpoint_id or None,
                endpoint_name=facts.endpoint_name if facts else None,
                deployment_name=facts.deployment_name if facts else deployment_name or key,
                model_name=facts.model if facts else None,
                model_version=facts.version if facts else None,
                cloud=facts.cloud if facts else None,
                cloud_label=cloud_label(facts.cloud if facts else None),
                deployment_type=facts.deployment_type if facts else None,
                region=facts.region if facts else None,
                pricing=rate.kind,
                price_id=price.id if price else None,
                price_origin=price.origin if price else None,
                input_per_million=price.input_per_million if price else None,
                cached_input_per_million=price.cached_input_per_million if price else None,
                output_per_million=price.output_per_million if price else None,
                ptu_hourly=price.ptu_hourly if price else None,
                monthly_amount=price.monthly_amount if price else None,
                capacity=facts.capacity if facts and facts.provisioned else None,
                month_cost=(
                    round_cost(rate.daily * days_in_month(today))
                    if rate.kind == "provisioned" and rate.daily is not None
                    else None
                ),
                utilization=(
                    _utilization(costs, key, metrics, minutes)
                    if rate.kind == "provisioned"
                    else None
                ),
                idle_cost=round_cost(idle) if idle > 0 else None,
                unpriced_reason=rate.unpriced.reason if rate.unpriced else None,
                unpriced_message=rate.unpriced.message if rate.unpriced else None,
                requests=metrics.requests,
                prompt_tokens=metrics.prompt_tokens,
                completion_tokens=metrics.completion_tokens,
                total_tokens=metrics.total_tokens,
                cost=round_cost(cost),
                cost_share=(
                    round(cost / total_cost, 4) if cost is not None and total_cost else None
                ),
                gateways=len(reached.get(folded, set())),
            )
        )
    rows.sort(
        key=lambda row: (
            row.cost is None,
            -(row.cost or 0.0),
            -row.total_tokens,
            row.deployment_name.casefold(),
        )
    )
    return rows[: context.limit]


# -- the chargeback export -------------------------------------------------------------------

CHARGEBACK_COLUMNS = [
    ("Month", "month"),
    ("From", "first_day"),
    ("To", "last_day"),
    ("Charged to", "party"),
    ("Kind", "party_kind"),
    ("Object ID", "party_object_id"),
    ("Cost center", "cost_center"),
    ("Cost center name", "cost_center_name"),
    ("Model", "model"),
    ("Deployment", "deployment"),
    ("Endpoint", "endpoint"),
    ("Requests", "requests"),
    ("Prompt tokens", "prompt_tokens"),
    ("Completion tokens", "completion_tokens"),
    ("Total tokens", "total_tokens"),
    ("Cost (USD)", "cost"),
    ("Priced", "priced"),
    ("Note", "note"),
]


@dataclass
class _Charge:
    month: date
    party: str
    party_kind: str
    party_object_id: str | None
    # The grant's cost center's code and name. Empty for unattributed calls and reserved
    # capacity nobody called, which belong to no grant and so to no cost center.
    cost_center: str | None
    cost_center_name: str | None
    model: str
    deployment: str | None
    endpoint: str | None
    metrics: UsageMetrics = field(default_factory=UsageMetrics)
    cost: float | None = None
    unpriced_tokens: int = 0
    note: str | None = None

    def add(self, metrics: UsageMetrics, priced: Priced) -> None:
        self.metrics.add(metrics)
        self.cost = add_cost(self.cost, priced.amount)
        share_left = 1.0 if priced.amount is None else priced.unpriced_share
        self.unpriced_tokens += round(metrics.total_tokens * share_left)
        if priced.unpriced is not None and self.note is None and share_left > 0:
            self.note = priced.unpriced.message


_SUBJECT_KINDS = {
    "user": "person",
    "application": "application",
    "securityGroup": "group",
    "group": "group",
}


def _party(scope: Scope, grant: GrantInfo | None) -> tuple[str, str, str | None]:
    """Who a grant's calls are charged to: its subject's name, kind, and Entra object ID."""

    name = scope.subject(grant)
    kind: str | None = name.kind
    if kind is None and grant is not None and grant.subject_kind is not None:
        kind = _SUBJECT_KINDS.get(str(grant.subject_kind), "person")
    return name.label, kind or "person", grant.subject_object_id if grant else None


def chargeback_rows(
    context: Context,
    *,
    grants: Sequence[UsageSummary],
    unattributed: Sequence[UsageSummary],
    costs: CostBook,
) -> list[dict[str, Any]]:
    """Each month's cost by who it's charged to and the model that served it."""

    scope, window = context.scope, context.window
    charges: dict[tuple[Any, ...], _Charge] = {}

    def charge(
        month: date,
        party: tuple[str, str, str | None],
        key: str | None,
        metrics: UsageMetrics,
        priced: Priced,
        cost_center: CostCenterRef | None = None,
    ) -> None:
        facts = costs.pricer.facts_for(key)
        model = (facts.model if facts else None) or UNKNOWN_MODEL
        deployment = facts.deployment_name if facts else None
        endpoint = facts.endpoint_name if facts else None
        code = cost_center.code if cost_center else None
        identity = (month, party[1], party[2] or party[0], code, model, deployment, endpoint)
        item = charges.get(identity)
        if item is None:
            item = charges[identity] = _Charge(
                month=month,
                party=party[0],
                party_kind=party[1],
                party_object_id=party[2],
                cost_center=code,
                cost_center_name=cost_center.name if cost_center else None,
                model=model,
                deployment=deployment,
                endpoint=endpoint,
            )
        item.add(metrics, priced)

    for summary, entry in entries(grants, scope, "grant"):
        start = date.fromisoformat(summary.period_start)
        grant = scope.grants.get(entry.key)
        api_name = costs.grant_api(summary.gateway_id, entry.key)
        api = scope.apis.get((summary.gateway_id, api_name)) if api_name else None
        if grant is not None and str(grant.resource_kind) == "mcpServer":
            continue
        key = api.deployment_key if api else None
        priced = costs.price(key, summary.period, start, entry.metrics)
        charge(
            month_first(start),
            _party(scope, grant),
            key,
            entry.metrics,
            priced,
            scope.cost_center(grant),
        )

    for summary, entry in entries(unattributed, scope, "unattributed"):
        start = date.fromisoformat(summary.period_start)
        api_name = entry.key.partition("|")[0]
        api = scope.apis.get((summary.gateway_id, api_name))
        if api is not None and api.kind == "mcp":
            continue
        key = api.deployment_key if api else None
        priced = costs.price(key, summary.period, start, entry.metrics)
        charge(
            month_first(start),
            (UNATTRIBUTED_PARTY, "unattributed", None),
            key,
            entry.metrics,
            priced,
        )

    for key in costs.idle_keys():
        month = month_first(window.first_day)
        while month <= window.last_day:
            first = max(month, window.first_day)
            last = min(month_last(month), window.last_day)
            idle = costs.idle(key, first, last)
            if idle > 0:
                charge(month, (IDLE_PARTY, "reserved", None), key, UsageMetrics(), Priced(idle))
            month = add_months(month, 1)

    rows: list[dict[str, Any]] = []
    for item in sorted(
        charges.values(),
        key=lambda value: (value.month, -(value.cost or 0.0), value.party.casefold(), value.model),
    ):
        first = max(item.month, window.first_day)
        last = min(month_last(item.month), window.last_day)
        if item.cost is None:
            status = "no"
        elif item.unpriced_tokens > 0:
            status = "partly"
        else:
            status = "yes"
        rows.append(
            {
                "month": item.month.strftime("%Y-%m"),
                "first_day": first,
                "last_day": last,
                "party": item.party,
                "party_kind": item.party_kind,
                "party_object_id": item.party_object_id,
                "cost_center": item.cost_center,
                "cost_center_name": item.cost_center_name,
                "model": item.model,
                "deployment": item.deployment,
                "endpoint": item.endpoint,
                "requests": item.metrics.requests,
                "prompt_tokens": item.metrics.prompt_tokens,
                "completion_tokens": item.metrics.completion_tokens,
                "total_tokens": item.metrics.total_tokens,
                "cost": round_cost(item.cost),
                "priced": status,
                "note": item.note if status != "yes" else None,
            }
        )
    return rows
