"""How close grants are to their limits, and the grants and keys nobody uses."""

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from mosaic_api.domain import (
    Entitlement,
    EntitlementResourceKind,
    EntitlementSubjectKind,
)
from mosaic_api.services.analytics.models import (
    AnalyticsHygiene,
    AnalyticsLimitRow,
    AnalyticsLimits,
    AnalyticsLimitUse,
    AnalyticsUntrackedGrant,
    AnalyticsUnusedGrant,
    AnalyticsUnusedKey,
    LimitStatus,
    UntrackedReason,
)
from mosaic_api.services.analytics.scope import Scope, binding_links, grant_for
from mosaic_api.services.analytics.views import Context, denial_rows
from mosaic_api.services.analytics.window import coverage_of
from mosaic_api.services.usage import Metric, current_window, quota_definitions
from mosaic_api.usage_telemetry import UsageFact, UsageMetrics, UsageRollupState, UsageSummary

NEAR = 0.8
DENIED_CALLERS = 100
_Index = dict[str, list[tuple[date, str, UsageMetrics]]]


@dataclass
class LimitInputs:
    # Today's facts, for hourly quotas.
    facts: list[UsageFact]
    # Daily grant-and-caller figures from the start of the week, for daily and weekly quotas.
    days: list[UsageSummary]
    # Monthly grant-and-caller figures from January, for monthly and yearly quotas.
    months: list[UsageSummary]
    # The analysis window's grant-and-caller figures, for rate-limit peaks and refusals.
    window: list[UsageSummary]


@dataclass
class HygieneInputs:
    grants: list[UsageSummary]
    history: list[UsageSummary]
    denials: list[UsageSummary]


def _index(summaries: Iterable[UsageSummary]) -> _Index:
    index: _Index = defaultdict(list)
    for summary in summaries:
        if summary.dimension != "grantCaller":
            continue
        day = date.fromisoformat(summary.period_start)
        for entry in summary.entries:
            grant_key, _, caller = entry.key.rpartition("|")
            index[grant_key].append((day, caller.casefold(), entry.metrics))
    return index


def _pick(
    index: _Index, links: Iterable[str], member: str | None, first: date, last: date
) -> UsageMetrics:
    picked = UsageMetrics()
    for key in links:
        for day, caller, metrics in index.get(key, []):
            if first <= day <= last and (member is None or caller == member):
                picked.add(metrics)
    return picked


def _value(metrics: UsageMetrics, metric: Metric) -> int:
    return metrics.total_tokens if metric == "tokens" else metrics.requests


def grant_links(scope: Scope, entitlement: Entitlement) -> set[str]:
    """Every grant key that has linked calls to an entitlement, as summaries record them."""

    links = set(scope.links_by_entitlement.get(entitlement.id, set()))
    if entitlement.binding is not None:
        links.update(binding_links(entitlement.binding))
    return links


def _reporting(state: UsageRollupState | None) -> bool:
    return (
        state is not None
        and state.last_success_at is not None
        and state.queried_through is not None
    )


def grant_ref(scope: Scope, entitlement: Entitlement) -> dict[str, Any]:
    grant = grant_for(entitlement, scope)
    subject = scope.subject(grant)
    gateway_id = scope.entitlement_gateway(entitlement)
    cost_center = scope.cost_centers.get(entitlement.cost_center_id) or scope.cost_center(grant)
    return {
        "entitlement_id": entitlement.id,
        "cost_center_code": cost_center.code if cost_center else None,
        "cost_center_name": cost_center.name if cost_center else None,
        "subject_kind": entitlement.subject.kind,
        "subject_label": subject.label,
        "subject_detail": subject.detail,
        "subject_principal_kind": subject.principal_kind,
        "resource_kind": entitlement.resource.kind,
        "resource_label": scope.resource_label(
            entitlement.resource.kind, entitlement.resource.id, grant.resource_name
        ),
        "gateway_id": gateway_id,
        "gateway_name": scope.gateway_name(gateway_id) if gateway_id else None,
    }


def limited(scope: Scope) -> Iterator[Entitlement]:
    """Enabled, enforced grants MOSAIC can link calls to, within the request's scope."""

    for entitlement in scope.entitlements.values():
        if (
            entitlement.enabled
            and entitlement.enforcement is not None
            and entitlement.binding is not None
            and binding_links(entitlement.binding)
            and entitlement.subject.kind != EntitlementSubjectKind.GROUP
            and scope.entitlement_allowed(entitlement)
        ):
            yield entitlement


def _status(utilization: float | None, refused: int) -> LimitStatus:
    """``reached`` once the gateway refused a call under the grant's limits, or use is at one.

    The gateway stops calls at a limit, so the busiest minute it admitted only comes close to it.
    """

    if refused:
        return "reached"
    if utilization is None:
        return "unknown"
    if utilization >= 1:
        return "reached"
    return "near" if utilization >= NEAR else "ok"


def _utilization(used: float | None, limit: int) -> float | None:
    return None if used is None or limit <= 0 else round(used / limit, 4)


def limits_report(context: Context, inputs: LimitInputs, now: datetime) -> AnalyticsLimits:
    scope, window = context.scope, context.window
    today = window.today
    window_index = _index(inputs.window)
    day_index = _index(inputs.days)
    month_index = _index(inputs.months)
    facts: dict[str, list[UsageFact]] = defaultdict(list)
    for fact in inputs.facts:
        if fact.day == today.isoformat():
            facts[f"{fact.link}:{fact.link_key}"].append(fact)

    rows: list[AnalyticsLimitRow] = []
    for entitlement in limited(scope):
        binding = entitlement.binding
        assert binding is not None
        links = grant_links(scope, entitlement)
        members: set[str | None] = set()
        if binding.attribution_per_member:
            for key in links:
                for index in (window_index, day_index, month_index):
                    members.update(caller for _, caller, _ in index.get(key, []) if caller)
                members.update(
                    fact.caller_object_id.casefold()
                    for fact in facts.get(key, [])
                    if fact.caller_object_id
                )
        if not members:
            members = {None}
        ref = grant_ref(scope, entitlement)
        state = scope.states.get(binding.gateway_id)
        reporting = _reporting(state)
        first_day = (
            date.fromisoformat(state.data_available_from)
            if state is not None and state.data_available_from
            else None
        )
        is_mcp = entitlement.resource.kind == EntitlementResourceKind.MCP_SERVER
        for member in sorted(members, key=lambda value: value or ""):
            in_window = _pick(window_index, links, member, window.first_day, window.last_day)
            uses: list[AnalyticsLimitUse] = []
            for metric, limit, period in quota_definitions(entitlement):
                start, end = current_window(now, period)
                used: float | None = None
                partial = False
                through = state.queried_through if state is not None else None
                if (
                    reporting
                    and through is not None
                    and through >= start
                    and not (is_mcp and metric == "tokens")
                ):
                    if period == "Hourly":
                        used = float(
                            sum(
                                (hour.total_tokens if metric == "tokens" else hour.requests)
                                for key in links
                                for fact in facts.get(key, [])
                                if member is None
                                or (fact.caller_object_id or "").casefold() == member
                                for hour in fact.hours
                                if hour.hour == now.hour
                            )
                        )
                    elif period in {"Daily", "Weekly"}:
                        picked = _pick(day_index, links, member, start.date(), today)
                        used = float(_value(picked, metric))
                    else:
                        used = float(
                            _value(_pick(month_index, links, member, start.date(), today), metric)
                        )
                    partial = first_day is not None and first_day > start.date()
                uses.append(
                    AnalyticsLimitUse(
                        kind="quota",
                        metric=metric,
                        limit=limit,
                        period=period,
                        window_start=start,
                        window_end=end,
                        used=used,
                        utilization=_utilization(used, limit),
                        partial=partial,
                    )
                )
            enforcement = entitlement.enforcement
            assert enforcement is not None
            if enforcement.tokens and enforcement.tokens.tokens_per_minute and not is_mcp:
                limit = enforcement.tokens.tokens_per_minute
                peak = float(in_window.peak_minute_tokens) if reporting else None
                uses.append(
                    AnalyticsLimitUse(
                        kind="rateLimit",
                        metric="tokens",
                        limit=limit,
                        window_seconds=60,
                        window_start=window.start,
                        window_end=window.end,
                        used=peak,
                        utilization=_utilization(peak, limit),
                    )
                )
            requests = enforcement.requests
            if requests and requests.calls and requests.renewal_period_seconds:
                # Peaks are measured per minute, so they only compare with a per-minute limit.
                peak = (
                    float(in_window.peak_minute_requests)
                    if reporting and requests.renewal_period_seconds == 60
                    else None
                )
                uses.append(
                    AnalyticsLimitUse(
                        kind="rateLimit",
                        metric="requests",
                        limit=requests.calls,
                        window_seconds=requests.renewal_period_seconds,
                        window_start=window.start,
                        window_end=window.end,
                        used=peak,
                        utilization=_utilization(peak, requests.calls),
                    )
                )
            if not uses:
                continue
            known = [use.utilization for use in uses if use.utilization is not None]
            utilization = max(known) if known else None
            member_name = scope.caller(member) if member else None
            # A 429 the model deployment returned is its capacity, not this grant's limit.
            throttled = max(in_window.throttled - in_window.backend_throttled, 0)
            rows.append(
                AnalyticsLimitRow(
                    **ref,
                    key=f"{entitlement.id}|{member or ''}",
                    member_object_id=member,
                    member_label=member_name.label if member_name else None,
                    limits=uses,
                    utilization=utilization,
                    status=_status(utilization, throttled + in_window.quota),
                    throttled=throttled,
                    quota_refused=in_window.quota,
                )
            )
    rows.sort(
        key=lambda row: (
            row.utilization is None,
            -(row.utilization or 0),
            row.subject_label.casefold(),
            row.member_label or "",
        )
    )
    return AnalyticsLimits(
        **context.report(),
        threshold=NEAR,
        near=sum(1 for row in rows if row.status == "near"),
        reached=sum(1 for row in rows if row.status == "reached"),
        rows=rows[: context.limit],
        truncated=len(rows) > context.limit,
    )


def _grant_usage(summaries: Iterable[UsageSummary], scope: Scope) -> dict[str, UsageMetrics]:
    used: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    for summary in summaries:
        if summary.dimension != "grant" or not scope.in_scope(summary.gateway_id):
            continue
        for entry in summary.entries:
            used[entry.key].add(entry.metrics)
    return used


def hygiene_report(
    context: Context, inputs: HygieneInputs, now: datetime, *, stale_after: timedelta
) -> AnalyticsHygiene:
    scope, window = context.scope, context.window
    used = _grant_usage(inputs.grants, scope)
    history = _grant_usage([*inputs.history, *inputs.grants], scope)
    untracked: list[AnalyticsUntrackedGrant] = []
    unused: list[AnalyticsUnusedGrant] = []
    unused_keys: list[AnalyticsUnusedKey] = []
    judged = 0
    for entitlement in scope.entitlements.values():
        if not entitlement.enabled or not scope.entitlement_allowed(entitlement):
            continue
        binding = entitlement.binding
        reason: UntrackedReason | None = None
        if entitlement.subject.kind == EntitlementSubjectKind.GROUP:
            reason = "mosaicGroup"
        elif binding is None:
            reason = "notApplied"
        elif not binding_links(binding):
            reason = "noLink"
        if reason is not None or binding is None:
            untracked.append(
                AnalyticsUntrackedGrant(
                    **grant_ref(scope, entitlement), reason=reason or "notApplied"
                )
            )
            continue
        links = grant_links(scope, entitlement)
        metrics = UsageMetrics()
        for key in links:
            if key in used:
                metrics.add(used[key])
        coverage = coverage_of([state] if (state := scope.states.get(binding.gateway_id)) else [])
        granted_at = binding.bound_at or entitlement.created_at
        if (
            coverage is not None
            and coverage.first_start <= window.start
            and now - coverage.through <= stale_after
            and granted_at <= window.start
        ):
            judged += 1
            if metrics.requests == 0:
                seen = [
                    history[key].last_seen
                    for key in links
                    if key in history and history[key].last_seen is not None
                ]
                unused.append(
                    AnalyticsUnusedGrant(
                        **grant_ref(scope, entitlement),
                        granted_at=granted_at,
                        last_used_at=max(value for value in seen if value) if seen else None,
                    )
                )
        methods = entitlement.runtime.applied_methods if entitlement.runtime else None
        if (
            binding.apim_subscription_name
            and metrics.requests > 0
            and metrics.key_requests == 0
            and (methods is None or methods.keys_enabled)
        ):
            unused_keys.append(
                AnalyticsUnusedKey(
                    **grant_ref(scope, entitlement),
                    subscription_name=binding.apim_subscription_name,
                    token_requests=metrics.requests,
                )
            )
    untracked.sort(key=lambda row: (row.reason, row.subject_label.casefold()))
    unused.sort(key=lambda row: (row.granted_at, row.subject_label.casefold()))
    unused_keys.sort(key=lambda row: (-row.token_requests, row.subject_label.casefold()))
    return AnalyticsHygiene(
        **context.report(),
        judged_grants=judged,
        unused_grants=unused[: context.limit],
        unused_keys=unused_keys[: context.limit],
        denied_callers=denial_rows(scope, inputs.denials, DENIED_CALLERS),
        untracked_grants=untracked[: context.limit],
        truncated=max(len(unused), len(unused_keys), len(untracked)) > context.limit,
    )
