"""Folding rolled-up summaries into the figures and rows analytics reports."""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

from mosaic_api.services.analytics.models import (
    AnalyticsKpis,
    AnalyticsRankRow,
    AnalyticsTrendPoint,
)
from mosaic_api.services.analytics.scope import Scope
from mosaic_api.services.analytics.window import Coverage, Window, bucket_known, day_start
from mosaic_api.usage_telemetry import (
    SummaryDimension,
    UsageHour,
    UsageMetrics,
    UsageSummary,
    UsageSummaryEntry,
    latency_percentile,
)

DENIAL_LABELS = {
    "no-credential": "No key or token",
    "keys-off": "Keys are turned off",
    "tokens-off": "Entra sign-in is turned off",
    "key-malformed": "Malformed key",
    "key-unknown": "Unknown key",
    "token-malformed": "Malformed token",
    "groups-overage": "Too many groups in the token",
    "no-grant": "No grant for this caller",
    "grant-mismatch": "Key and token name different grants",
    "cost-center": "No grant under the named cost center",
    "cost-center-mismatch": "Key belongs to a different cost center",
    "operation": "Operation not allowed",
    "model": "Model not allowed",
    "access-off": "Access is turned off",
    # 401s that carry no MOSAIC trace, such as a failed validate-azure-ad-token check.
    "unauthenticated": "Not signed in",
    "token-invalid": "Invalid or expired token",
    "unknown": "Unknown",
}


def denial_label(reason: str) -> str:
    return DENIAL_LABELS.get(reason, DENIAL_LABELS["unknown"])


def share(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole > 0 else None


def usage_values(
    metrics: UsageMetrics, requests: int, tokens: int, cost: float | None = None
) -> dict[str, Any]:
    """The shared usage columns of a row, with its shares of the given totals."""

    return {
        "requests": metrics.requests,
        "prompt_tokens": metrics.prompt_tokens,
        "completion_tokens": metrics.completion_tokens,
        "total_tokens": metrics.total_tokens,
        "throttled": metrics.throttled,
        "quota_refused": metrics.quota,
        "errors": metrics.errors,
        "last_seen": metrics.last_seen,
        "request_share": share(metrics.requests, requests),
        "token_share": share(metrics.total_tokens, tokens),
        "cost": None if cost is None else round(cost, 4),
    }


def answered(metrics: UsageMetrics) -> int:
    """Calls the gateway passed on to the backend."""

    return max(0, metrics.requests - metrics.denied - metrics.throttled - metrics.quota)


def average_latency(metrics: UsageMetrics) -> float | None:
    timed = sum(metrics.latency)
    return round(metrics.total_time_ms / timed, 1) if timed > 0 else None


def average_backend(metrics: UsageMetrics) -> float | None:
    calls = answered(metrics)
    return round(metrics.backend_time_ms / calls, 1) if calls > 0 else None


@dataclass
class Activity:
    callers: int | None = None
    grants: int | None = None
    apis: int | None = None
    unattributed: int | None = None


def kpis(metrics: UsageMetrics, activity: Activity) -> AnalyticsKpis:
    return AnalyticsKpis(
        requests=metrics.requests,
        prompt_tokens=metrics.prompt_tokens,
        completion_tokens=metrics.completion_tokens,
        total_tokens=metrics.total_tokens,
        ok=metrics.ok,
        throttled=metrics.throttled,
        quota_refused=metrics.quota,
        denied=metrics.denied,
        errors=metrics.errors,
        client_errors=metrics.client_errors,
        server_errors=metrics.server_errors,
        backend_throttled=metrics.backend_throttled,
        success_rate=share(metrics.ok, metrics.requests),
        error_rate=share(metrics.errors, metrics.requests),
        throttle_rate=share(metrics.throttled + metrics.quota, metrics.requests),
        denial_rate=share(metrics.denied, metrics.requests),
        p50_latency_ms=latency_percentile(metrics.latency, 50),
        p95_latency_ms=latency_percentile(metrics.latency, 95),
        average_latency_ms=average_latency(metrics),
        average_backend_ms=average_backend(metrics),
        active_callers=activity.callers,
        active_grants=activity.grants,
        active_apis=activity.apis,
        unattributed_requests=activity.unattributed,
    )


@dataclass
class Point:
    """One trend bucket's calls, which hourly figures can also fill."""

    requests: int = 0
    total_tokens: int = 0
    throttled: int = 0
    quota: int = 0
    denied: int = 0
    errors: int = 0
    cost: float | None = None

    def add_metrics(self, metrics: UsageMetrics) -> None:
        self.requests += metrics.requests
        self.total_tokens += metrics.total_tokens
        self.throttled += metrics.throttled
        self.quota += metrics.quota
        self.denied += metrics.denied
        self.errors += metrics.errors

    def add_hour(self, hour: UsageHour) -> None:
        self.requests += hour.requests
        self.total_tokens += hour.total_tokens
        self.throttled += hour.throttled
        self.quota += hour.quota
        self.denied += hour.denied
        self.errors += hour.errors

    def add(self, other: "Point") -> None:
        self.requests += other.requests
        self.total_tokens += other.total_tokens
        self.throttled += other.throttled
        self.quota += other.quota
        self.denied += other.denied
        self.errors += other.errors


def hour_kpis(point: Point, activity: Activity) -> AnalyticsKpis:
    """Headline figures for hour-grained windows, which keep only call and token counts."""

    ok = max(0, point.requests - point.throttled - point.quota - point.denied - point.errors)
    return AnalyticsKpis(
        requests=point.requests,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=point.total_tokens,
        ok=ok,
        throttled=point.throttled,
        quota_refused=point.quota,
        denied=point.denied,
        errors=point.errors,
        client_errors=None,
        server_errors=None,
        backend_throttled=None,
        success_rate=share(ok, point.requests),
        error_rate=share(point.errors, point.requests),
        throttle_rate=share(point.throttled + point.quota, point.requests),
        denial_rate=share(point.denied, point.requests),
        p50_latency_ms=None,
        p95_latency_ms=None,
        average_latency_ms=None,
        average_backend_ms=None,
        active_callers=activity.callers,
        active_grants=activity.grants,
        active_apis=activity.apis,
        unattributed_requests=activity.unattributed,
    )


def period_start(summary: UsageSummary) -> datetime:
    return day_start(date.fromisoformat(summary.period_start))


def entries(
    summaries: Iterable[UsageSummary], scope: Scope, dimension: SummaryDimension
) -> Iterator[tuple[UsageSummary, UsageSummaryEntry]]:
    """Every entry of one dimension the request may see."""

    for summary in summaries:
        if summary.dimension != dimension or not scope.in_scope(summary.gateway_id):
            continue
        for entry in summary.entries:
            if scope.entry_allowed(summary.gateway_id, dimension, entry.key):
                yield summary, entry


def fold(
    summaries: Iterable[UsageSummary], scope: Scope, dimension: SummaryDimension
) -> dict[tuple[str, str], UsageMetrics]:
    """Each gateway and key's figures, summed across every period read."""

    folded: dict[tuple[str, str], UsageMetrics] = {}
    for summary, entry in entries(summaries, scope, dimension):
        folded.setdefault((summary.gateway_id, entry.key), UsageMetrics()).add(entry.metrics)
    return folded


def total(values: Iterable[UsageMetrics]) -> UsageMetrics:
    combined = UsageMetrics()
    for value in values:
        combined.add(value)
    return combined


def bucket_points(
    window: Window, summaries: Iterable[UsageSummary], scope: Scope
) -> dict[int, Point]:
    """The window's API figures by trend bucket. Hourly windows read the days' hours."""

    points: dict[int, Point] = {}
    for summary, entry in entries(summaries, scope, "api"):
        start = period_start(summary)
        if window.granularity == "hour":
            for hour in entry.hours or []:
                index = window.bucket_index(start + timedelta(hours=hour.hour))
                if index is not None:
                    points.setdefault(index, Point()).add_hour(hour)
            continue
        index = window.bucket_index(start)
        if index is not None:
            points.setdefault(index, Point()).add_metrics(entry.metrics)
    return points


def hours_between(
    summaries: Iterable[UsageSummary], scope: Scope, start: datetime, end: datetime
) -> Point:
    """API figures for the whole hours in a span, from daily summaries' hours."""

    point = Point()
    for summary, entry in entries(summaries, scope, "api"):
        day = period_start(summary)
        for hour in entry.hours or []:
            moment = day + timedelta(hours=hour.hour)
            if start <= moment < end:
                point.add_hour(hour)
    return point


def trend(
    window: Window, coverage: Coverage | None, points: dict[int, Point]
) -> list[AnalyticsTrendPoint]:
    series: list[AnalyticsTrendPoint] = []
    for index, start in enumerate(window.buckets):
        if not bucket_known(window, coverage, start):
            series.append(
                AnalyticsTrendPoint(
                    start=start,
                    requests=None,
                    total_tokens=None,
                    throttled=None,
                    quota_refused=None,
                    denied=None,
                    errors=None,
                    cost=None,
                )
            )
            continue
        point = points.get(index, Point())
        series.append(
            AnalyticsTrendPoint(
                start=start,
                requests=point.requests,
                total_tokens=point.total_tokens,
                throttled=point.throttled,
                quota_refused=point.quota,
                denied=point.denied,
                errors=point.errors,
                cost=None if point.cost is None else round(point.cost, 4),
            )
        )
    return series


@dataclass
class Ranked:
    key: str
    label: str
    detail: str | None
    metrics: UsageMetrics
    cost: float | None = None


def rank(
    items: Iterable[Ranked],
    *,
    requests: int,
    tokens: int,
    limit: int,
    by: Literal["tokens", "requests"] = "tokens",
) -> list[AnalyticsRankRow]:
    def key(item: Ranked) -> tuple[int, int, str]:
        metrics = item.metrics
        if by == "requests":
            return (-metrics.requests, -metrics.total_tokens, item.label)
        return (-metrics.total_tokens, -metrics.requests, item.label)

    ordered = sorted(
        (item for item in items if item.metrics.requests > 0 or item.metrics.total_tokens > 0),
        key=key,
    )
    return [
        AnalyticsRankRow(
            key=item.key,
            label=item.label,
            detail=item.detail,
            requests=item.metrics.requests,
            total_tokens=item.metrics.total_tokens,
            request_share=share(item.metrics.requests, requests),
            token_share=share(item.metrics.total_tokens, tokens),
            cost=None if item.cost is None else round(item.cost, 4),
        )
        for item in ordered[:limit]
    ]
