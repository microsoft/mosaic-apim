"""What usage cost, priced from the price list each time a report is read. See ADR 0020.

Nothing about cost is stored. A report prices each rolled-up entry at the price in effect on each
day it covers, so a corrected price changes history the next time anyone looks, and a new price
changes only the days from its date.

- A model API's calls are priced by the deployment its publication fronts. An adopted API, whose
  deployment MOSAIC doesn't know, has no cost.
- A grant's calls are priced by the model API it grants. Products and model deployments can't be
  tied to one API, and MCP servers carry no tokens.
- A provisioned deployment costs its reserved capacity whatever its calls. Each month's cost is
  shared among its calls by their share of its tokens that month, counted across every gateway,
  so filtering by gateway never inflates anyone's share. A month with no calls leaves the cost
  idle, and reports in scope count it apart.
- A month MOSAIC reads only as a total is priced day by day as if its calls were spread evenly.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from mosaic_api.domain import EntitlementResourceKind
from mosaic_api.pricing import (
    UNKNOWN_DEPLOYMENT,
    DayRate,
    Pricer,
    Unpriced,
    month_first,
    month_last,
    token_amount,
)
from mosaic_api.services.analytics.models import AnalyticsCostSummary, AnalyticsUnpricedUse
from mosaic_api.services.analytics.scope import Scope
from mosaic_api.usage_telemetry import SummaryPeriod, UsageMetrics, UsageSummary

LIST_PRICE_NOTE = (
    "Costs are at list prices from MOSAIC's price list, before any discount, and are estimates, "
    "not a bill."
)
CACHED_NOTE = (
    "The gateway's logs don't separate cached prompt tokens, so every prompt token is priced at "
    "the full input price."
)
RESERVED_NOTE = (
    "A provisioned deployment costs its reserved capacity whether or not it's called. Each month's "
    "cost is shared among its callers by their share of its tokens."
)
AVERAGED_NOTE = (
    "A price changed during a month MOSAIC keeps only as a total, so that month is priced as if "
    "its calls were spread evenly across it."
)
HOURS_NOTE = "MOSAIC prices whole days, so it shows no cost by the hour. Choose 7 days or more."
UNPRICED_ROWS = 100


def add_cost(current: float | None, value: float | None) -> float | None:
    """Two costs added, where None means nothing could be priced."""

    if value is None:
        return current
    return value if current is None else current + value


@dataclass
class Priced:
    amount: float | None
    # The part of ``amount`` that is a share of reserved capacity.
    reserved: float = 0.0
    unpriced: Unpriced | None = None
    # The share of the tokens that couldn't be priced.
    unpriced_share: float = 0.0


@dataclass
class _Left:
    key: str
    kind: Literal["deployment", "api", "grant"]
    label: str
    detail: str | None
    unpriced: Unpriced
    requests: int = 0
    tokens: int = 0


@dataclass
class CostTally:
    """A report's total cost, and the usage it leaves out because nothing prices it."""

    total: float | None = None
    reserved: float = 0.0
    priced_tokens: int = 0
    unpriced_tokens: int = 0
    unpriced_requests: int = 0
    left: dict[str, _Left] = field(default_factory=dict)

    def add_amount(self, amount: float | None, reserved: float = 0.0) -> None:
        self.total = add_cost(self.total, amount)
        self.reserved += reserved

    def record(
        self,
        priced: Priced,
        metrics: UsageMetrics,
        *,
        key: str,
        kind: Literal["deployment", "api", "grant"],
        label: str,
        detail: str | None,
    ) -> None:
        tokens = metrics.total_tokens
        share = 1.0 if priced.amount is None else priced.unpriced_share
        left_tokens = round(tokens * share)
        if priced.amount is not None:
            self.add_amount(priced.amount, priced.reserved)
            self.priced_tokens += tokens - left_tokens
        if left_tokens <= 0 or priced.unpriced is None:
            return
        left_requests = round(metrics.requests * share)
        self.unpriced_tokens += left_tokens
        self.unpriced_requests += left_requests
        item = self.left.setdefault(
            key, _Left(key=key, kind=kind, label=label, detail=detail, unpriced=priced.unpriced)
        )
        item.tokens += left_tokens
        item.requests += left_requests

    def summary(self, notes: Iterable[str] = ()) -> AnalyticsCostSummary:
        ordered = sorted(
            self.left.values(), key=lambda item: (-item.tokens, -item.requests, item.label)
        )
        return AnalyticsCostSummary(
            total=None if self.total is None else round(self.total, 4),
            reserved=round(self.reserved, 4) if self.reserved else None,
            priced_tokens=self.priced_tokens,
            unpriced_tokens=self.unpriced_tokens,
            unpriced_requests=self.unpriced_requests,
            unpriced_items=len(ordered),
            unpriced=[
                AnalyticsUnpricedUse(
                    key=item.key,
                    kind=item.kind,
                    label=item.label,
                    detail=item.detail,
                    reason=item.unpriced.reason,
                    message=item.unpriced.message,
                    requests=item.requests,
                    total_tokens=item.tokens,
                )
                for item in ordered[:UNPRICED_ROWS]
            ],
            notes=list(dict.fromkeys(notes)),
        )


@dataclass
class CostBook:
    """Prices one report's entries. Built per request from the price list and the rollups."""

    pricer: Pricer
    scope: Scope
    today: date
    # Each provisioned deployment's tokens by month, across every gateway and caller.
    provisioned_tokens: dict[tuple[str, date], int]
    notes: list[str] = field(default_factory=list)
    _reserved: dict[tuple[str, date], float | None] = field(default_factory=dict)
    _resource_apis: dict[tuple[str, str], str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._resource_apis = {
            (gateway_id, api.resource_id): api.api_name
            for (gateway_id, _), api in self.scope.apis.items()
        }
        self.note(LIST_PRICE_NOTE)
        self.note(CACHED_NOTE)

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    # -- one deployment ---------------------------------------------------------------------

    def reserved_month(self, key: str, month: date) -> float | None:
        """A provisioned deployment's cost for a month, or for the month so far."""

        cache_key = (key.casefold(), month)
        if cache_key not in self._reserved:
            self._reserved[cache_key] = self.pricer.reserved_cost(key, month, self.today)
        return self._reserved[cache_key]

    def month_tokens(self, key: str, month: date) -> int:
        return self.provisioned_tokens.get((key.casefold(), month), 0)

    def _days(self, period: SummaryPeriod, start: date) -> list[date]:
        if period == "day":
            return [start] if start <= self.today else []
        return self.pricer.month_days(month_first(start), self.today)

    def price(
        self, key: str | None, period: SummaryPeriod, start: date, metrics: UsageMetrics
    ) -> Priced:
        """What one rolled-up entry's tokens cost on the deployment ``key``."""

        days = self._days(period, start)
        if not days:
            return Priced(0.0)
        prompt, completion = metrics.prompt_tokens, metrics.completion_tokens
        total = metrics.total_tokens or prompt + completion
        if total <= 0:
            quiet = self.pricer.rate(key, days[-1])
            return (
                Priced(None, unpriced=quiet.unpriced) if quiet.kind == "unpriced" else Priced(0.0)
            )
        count = len(days)
        amount = 0.0
        reserved = 0.0
        priced = False
        unpriced_days = 0
        first: Unpriced | None = None
        seen: set[tuple[str, str | None]] = set()
        for day in days:
            rate: DayRate = self.pricer.rate(key, day)
            seen.add((rate.kind, rate.entry.id if rate.entry else None))
            if rate.kind == "tokens" and rate.entry is not None:
                value = token_amount(rate.entry, prompt / count, completion / count)
                if value is None:
                    unpriced_days += 1
                    first = first or Unpriced(
                        "noOutputPrice", "The price lists no output price for this model."
                    )
                else:
                    amount += value
                    priced = True
            elif rate.kind == "provisioned" and key is not None:
                month = month_first(day)
                monthly = self.reserved_month(key, month)
                shared = max(self.month_tokens(key, month), total)
                if monthly is not None and shared > 0:
                    part = monthly * (total / count) / shared
                    amount += part
                    reserved += part
                    priced = True
                    self.note(RESERVED_NOTE)
            else:
                unpriced_days += 1
                first = first or rate.unpriced or UNKNOWN_DEPLOYMENT
        if period == "month" and len(seen) > 1:
            self.note(AVERAGED_NOTE)
        if not priced:
            return Priced(None, unpriced=first, unpriced_share=1.0)
        return Priced(
            amount, reserved=reserved, unpriced=first, unpriced_share=unpriced_days / count
        )

    def idle(self, key: str, first: date, last: date) -> float:
        """Reserved capacity on days in months that had no calls to share it among."""

        total = 0.0
        day = first
        end = min(last, self.today)
        while day <= end:
            month = month_first(day)
            if self.month_tokens(key, month) <= 0:
                rate = self.pricer.rate(key, day)
                if rate.kind == "provisioned" and rate.daily is not None:
                    total += rate.daily
                day += timedelta(days=1)
            else:
                day = month_last(day) + timedelta(days=1)
        return total

    def idle_keys(self) -> list[str]:
        """Provisioned deployments that a current governed API in scope fronts."""

        keys: set[str] = set()
        for gateway_id, apis in self.scope.governed.items():
            if not self.scope.in_scope(gateway_id) or gateway_id not in self.scope.gateways:
                continue
            for api in apis:
                if not self.scope.api_allowed(gateway_id, api.api_name) or not api.deployment_key:
                    continue
                facts = self.pricer.facts_for(api.deployment_key)
                if facts is not None and facts.provisioned:
                    keys.add(api.deployment_key)
        return sorted(keys)

    # -- what reports price -----------------------------------------------------------------

    def describe(self, key: str | None, gateway_id: str, api_name: str) -> tuple[str, str | None]:
        facts = self.pricer.facts_for(key)
        if facts is not None:
            return facts.deployment_name, facts.endpoint_name
        return self.scope.api_label(gateway_id, api_name), self.scope.gateway_name(gateway_id)

    def api_cost(
        self,
        gateway_id: str,
        api_name: str,
        period: SummaryPeriod,
        start: date,
        metrics: UsageMetrics,
        tally: CostTally | None = None,
    ) -> float | None:
        api = self.scope.apis.get((gateway_id, api_name))
        if api is not None and api.kind == "mcp":
            return None
        key = api.deployment_key if api else None
        priced = self.price(key, period, start, metrics)
        if tally is not None:
            label, detail = self.describe(key, gateway_id, api_name)
            tally.record(
                priced,
                metrics,
                key=key.casefold() if key else f"api:{gateway_id}/{api_name}",
                kind="deployment" if key else "api",
                label=label,
                detail=detail,
            )
        return priced.amount

    def grant_api(self, gateway_id: str, grant_key: str) -> str | None:
        grant = self.scope.grants.get(grant_key)
        if grant is None or grant.resource_kind != EntitlementResourceKind.MODEL_API:
            return None
        return self._resource_apis.get((gateway_id, grant.resource_id or ""))

    def grant_cost(
        self,
        gateway_id: str,
        grant_key: str,
        period: SummaryPeriod,
        start: date,
        metrics: UsageMetrics,
        tally: CostTally | None = None,
    ) -> float | None:
        grant = self.scope.grants.get(grant_key)
        if grant is not None and grant.resource_kind == EntitlementResourceKind.MCP_SERVER:
            return None
        api_name = self.grant_api(gateway_id, grant_key)
        if api_name is not None:
            return self.api_cost(gateway_id, api_name, period, start, metrics, tally)
        if tally is not None and metrics.total_tokens > 0:
            message = (
                "A product bundles several APIs, so MOSAIC can't tell which deployment its calls "
                "reached."
                if grant is not None and grant.resource_kind == EntitlementResourceKind.PRODUCT
                else "MOSAIC can't tell which model API this grant's calls reached."
            )
            tally.record(
                Priced(None, unpriced=Unpriced("unknownDeployment", message), unpriced_share=1.0),
                metrics,
                key=f"grant:{grant_key}",
                kind="grant",
                label=self.scope.subject(grant).label,
                detail=self.scope.resource_label(
                    grant.resource_kind if grant else None,
                    grant.resource_id if grant else None,
                    grant.resource_name if grant else None,
                ),
            )
        return None

    def summary_cost(
        self,
        summary: UsageSummary,
        api_name: str,
        metrics: UsageMetrics,
        tally: CostTally | None = None,
    ) -> float | None:
        return self.api_cost(
            summary.gateway_id,
            api_name,
            summary.period,
            date.fromisoformat(summary.period_start),
            metrics,
            tally,
        )


def days_between(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def changed_months(pricer: Pricer, months: Iterable[date]) -> set[date]:
    """Months in which some price takes effect after the first day."""

    return {month for month in months if pricer.book.changes_within(month, month_last(month))}
