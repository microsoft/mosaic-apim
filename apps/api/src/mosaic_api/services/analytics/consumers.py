"""Who used what: people, applications, security groups, grants and client applications."""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date

from mosaic_api.domain import CostCenterRef, EntitlementSubjectKind
from mosaic_api.services.analytics.cost import CostBook, CostTally, add_cost
from mosaic_api.services.analytics.models import (
    AnalyticsClientAppRow,
    AnalyticsConsumerRow,
    AnalyticsConsumers,
    AnalyticsCostCenterRow,
    AnalyticsGrantRow,
    AnalyticsOnBehalfRow,
    AnalyticsOnBehalfUnresolved,
    AnalyticsUsage,
    ConsumerKind,
    GrantState,
)
from mosaic_api.services.analytics.rows import entries, total, usage_values
from mosaic_api.services.analytics.scope import GrantInfo, Name, Scope, grant_for
from mosaic_api.services.analytics.views import Context, resolved_caller
from mosaic_api.usage_telemetry import UsageMetrics, UsageSummary

NO_TOKEN = "Calls without a token"
# Why MOSAIC couldn't use a model call's MCP call reference, in the order reports list them.
ON_BEHALF_REASONS: tuple[str, ...] = ("malformed", "missing", "late", "caller", "unknown")


@dataclass
class _Tally:
    metrics: UsageMetrics = field(default_factory=UsageMetrics)
    grants: set[str] = field(default_factory=set)
    resources: set[str] = field(default_factory=set)
    members: set[str] = field(default_factory=set)
    subject_kinds: set[str] = field(default_factory=set)
    cost: float | None = None


def _grant_key(grant_key: str, grant: GrantInfo | None) -> str:
    return grant.entitlement_id if grant and grant.entitlement_id else f"link:{grant_key}"


def _ordered[T: AnalyticsUsage](rows: list[T], label: Callable[[T], str]) -> list[T]:
    return sorted(
        rows, key=lambda row: (-row.total_tokens, -row.requests, label(row).casefold())
    )


def _kind(name: Name, tally: _Tally) -> ConsumerKind:
    if name.kind in {"person", "application"}:
        return name.kind
    if tally.subject_kinds and tally.subject_kinds <= {EntitlementSubjectKind.APPLICATION.value}:
        return "application"
    return "person"


def _state(scope: Scope, entitlement_id: str | None) -> GrantState:
    entitlement = scope.entitlements.get(entitlement_id or "")
    if entitlement is None:
        return "removed"
    return "active" if entitlement.enabled else "disabled"


def on_behalf_key(key: str) -> tuple[str, str, str, str] | None:
    """An ``onBehalf`` entry's grant link, application, person and MCP API, or None if malformed."""

    parts = key.rsplit("|", 3)
    if len(parts) != 4 or not parts[2] or not parts[3]:
        return None
    grant_key, caller, person, mcp_api = parts
    return grant_key, caller, person, mcp_api


def _on_behalf_rows(
    context: Context,
    summaries: Sequence[UsageSummary],
    costs: CostBook | None,
    requests: int,
    tokens: int,
) -> list[AnalyticsOnBehalfRow]:
    """Each person's model use through each MCP server, by the application that made the calls.

    Every call here is already one of the application's own linked calls, so the rows add to no
    total and price nothing into the report's cost. Each is priced as its grant's calls are.
    """

    scope = context.scope
    found: dict[tuple[str, str, str, str], _Tally] = defaultdict(_Tally)
    for summary, entry in entries(summaries, scope, "onBehalf"):
        parsed = on_behalf_key(entry.key)
        if parsed is None:
            continue
        grant_key, caller, person, mcp_api = parsed
        application = resolved_caller(scope, grant_key, caller) or ""
        tally = found[(person.casefold(), summary.gateway_id, mcp_api.casefold(), application)]
        tally.metrics.add(entry.metrics)
        if costs is not None:
            tally.cost = add_cost(
                tally.cost,
                costs.grant_cost(
                    summary.gateway_id,
                    grant_key,
                    summary.period,
                    date.fromisoformat(summary.period_start),
                    entry.metrics,
                ),
            )
    rows: list[AnalyticsOnBehalfRow] = []
    for (person, gateway_id, mcp_api, application), tally in found.items():
        who = scope.caller(person)
        app = scope.caller(application or None)
        known = scope.apis.get((gateway_id, mcp_api))
        rows.append(
            AnalyticsOnBehalfRow(
                **usage_values(tally.metrics, requests, tokens, tally.cost),
                key=f"{person}|{gateway_id}/{mcp_api}|{application}",
                person_object_id=person,
                person_label=who.label,
                person_detail=who.detail,
                person_principal_id=who.principal_id,
                person_principal_kind=who.principal_kind,
                gateway_id=gateway_id,
                gateway_name=scope.gateway_name(gateway_id),
                mcp_api_name=mcp_api,
                mcp_label=scope.api_label(gateway_id, mcp_api),
                mcp_server_id=known.resource_id if known else None,
                application_object_id=application,
                application_label=app.label,
                application_detail=app.detail,
                application_principal_id=app.principal_id,
                application_principal_kind=app.principal_kind,
            )
        )
    return rows


def _on_behalf_unresolved(
    scope: Scope, summaries: Sequence[UsageSummary]
) -> list[AnalyticsOnBehalfUnresolved]:
    by_reason: dict[str, UsageMetrics] = defaultdict(UsageMetrics)
    for _, entry in entries(summaries, scope, "onBehalfUnresolved"):
        by_reason[entry.key.rpartition("|")[2] or "unknown"].add(entry.metrics)
    order = {reason: index for index, reason in enumerate(ON_BEHALF_REASONS)}
    return [
        AnalyticsOnBehalfUnresolved(
            reason=reason, requests=metrics.requests, total_tokens=metrics.total_tokens
        )
        for reason, metrics in sorted(
            by_reason.items(), key=lambda item: (order.get(item[0], len(order)), item[0])
        )
        if metrics.requests > 0
    ]


def consumers_report(
    context: Context,
    *,
    callers: Sequence[UsageSummary],
    clients: Sequence[UsageSummary],
    costs: CostBook | None = None,
    on_behalf: Sequence[UsageSummary] = (),
) -> AnalyticsConsumers:
    scope = context.scope
    people: dict[str, _Tally] = defaultdict(_Tally)
    groups: dict[str, _Tally] = defaultdict(_Tally)
    grants: dict[str, _Tally] = defaultdict(_Tally)
    grant_info: dict[str, GrantInfo | None] = {}
    cost_centers: dict[str, _Tally] = defaultdict(_Tally)
    cost_center_refs: dict[str, CostCenterRef] = {}
    linked = UsageMetrics()
    unidentified = 0
    tally = CostTally()
    for summary, entry in entries(callers, scope, "grantCaller"):
        grant_key, _, caller = entry.key.rpartition("|")
        grant = scope.grants.get(grant_key)
        merged = _grant_key(grant_key, grant)
        resource = (grant.resource_id if grant else None) or grant_key
        cost = (
            costs.grant_cost(
                summary.gateway_id,
                grant_key,
                summary.period,
                date.fromisoformat(summary.period_start),
                entry.metrics,
                tally,
            )
            if costs is not None
            else None
        )
        linked.add(entry.metrics)
        grant_tally = grants[merged]
        grant_tally.metrics.add(entry.metrics)
        grant_tally.cost = add_cost(grant_tally.cost, cost)
        grant_info.setdefault(merged, grant)
        object_id = resolved_caller(scope, grant_key, caller)
        cost_center = scope.cost_center(grant)
        if cost_center is not None:
            cost_center_refs.setdefault(cost_center.id, cost_center)
            center = cost_centers[cost_center.id]
            center.metrics.add(entry.metrics)
            center.cost = add_cost(center.cost, cost)
            center.grants.add(merged)
            if object_id is not None:
                center.members.add(object_id)
        if object_id is None:
            unidentified += entry.metrics.requests
        else:
            grant_tally.members.add(object_id)
            person = people[object_id]
            person.metrics.add(entry.metrics)
            person.cost = add_cost(person.cost, cost)
            person.grants.add(merged)
            person.resources.add(resource)
            if grant and grant.subject_kind:
                person.subject_kinds.add(str(grant.subject_kind))
        if grant and grant.subject_kind == EntitlementSubjectKind.SECURITY_GROUP:
            group_key = grant.subject_id or grant.subject_object_id or grant_key
            group = groups[group_key]
            group.metrics.add(entry.metrics)
            group.cost = add_cost(group.cost, cost)
            group.grants.add(merged)
            group.resources.add(resource)
            if object_id is not None:
                group.members.add(object_id)
            grant_info.setdefault(f"group:{group_key}", grant)
    requests, tokens = linked.requests, linked.total_tokens

    person_rows: list[AnalyticsConsumerRow] = []
    app_rows: list[AnalyticsConsumerRow] = []
    for object_id, person_tally in people.items():
        name = scope.caller(object_id)
        kind = _kind(name, person_tally)
        row = AnalyticsConsumerRow(
            **usage_values(person_tally.metrics, requests, tokens, person_tally.cost),
            key=object_id,
            kind=kind,
            label=name.label,
            detail=name.detail,
            principal_id=name.principal_id,
            principal_kind=name.principal_kind,
            grants=len(person_tally.grants),
            resources=len(person_tally.resources),
        )
        (app_rows if kind == "application" else person_rows).append(row)

    group_rows: list[AnalyticsConsumerRow] = []
    for group_key, group_tally in groups.items():
        grant = grant_info.get(f"group:{group_key}")
        name = scope.subject(grant)
        group_rows.append(
            AnalyticsConsumerRow(
                **usage_values(group_tally.metrics, requests, tokens, group_tally.cost),
                key=group_key,
                kind="group",
                label=name.label,
                detail=name.detail,
                principal_id=name.principal_id,
                principal_kind=name.principal_kind,
                grants=len(group_tally.grants),
                resources=len(group_tally.resources),
                members=len(group_tally.members),
            )
        )

    for entitlement in scope.entitlements.values():
        if (
            not entitlement.enabled
            or entitlement.subject.kind == EntitlementSubjectKind.GROUP
            or not scope.entitlement_allowed(entitlement)
            or entitlement.id in grants
        ):
            continue
        grants[entitlement.id] = _Tally()
        grant_info[entitlement.id] = grant_for(entitlement, scope)

    grant_rows: list[AnalyticsGrantRow] = []
    for merged, grant_tally in grants.items():
        grant = grant_info.get(merged)
        subject = scope.subject(grant)
        entitlement_id = grant.entitlement_id if grant else None
        charged = scope.cost_center(grant)
        grant_rows.append(
            AnalyticsGrantRow(
                **usage_values(grant_tally.metrics, requests, tokens, grant_tally.cost),
                key=merged,
                entitlement_id=entitlement_id,
                state=_state(scope, entitlement_id),
                cost_center_id=charged.id if charged else None,
                cost_center_code=charged.code if charged else None,
                cost_center_name=charged.name if charged else None,
                subject_kind=grant.subject_kind if grant else None,
                subject_label=subject.label,
                subject_detail=subject.detail,
                subject_principal_kind=subject.principal_kind,
                resource_kind=grant.resource_kind if grant else None,
                resource_label=scope.resource_label(
                    grant.resource_kind if grant else None,
                    grant.resource_id if grant else None,
                    grant.resource_name if grant else None,
                ),
                gateway_id=grant.gateway_id if grant else None,
                gateway_name=scope.gateway_name(grant.gateway_id) if grant else None,
                callers=len(grant_tally.members),
                key_requests=grant_tally.metrics.key_requests,
                peak_minute_tokens=grant_tally.metrics.peak_minute_tokens,
                peak_minute_requests=grant_tally.metrics.peak_minute_requests,
            )
        )

    by_client: dict[str, _Tally] = defaultdict(_Tally)
    for summary, entry in entries(clients, scope, "clientApp"):
        client, _, api_name = entry.key.rpartition("|")
        client_tally = by_client[client.casefold()]
        client_tally.metrics.add(entry.metrics)
        if costs is not None:
            client_tally.cost = add_cost(
                client_tally.cost, costs.summary_cost(summary, api_name, entry.metrics)
            )
        client_tally.resources.add(f"{summary.gateway_id}/{api_name}")
        if entry.application_object_id:
            client_tally.members.add(entry.application_object_id)
    client_total = total(item.metrics for item in by_client.values())
    client_rows: list[AnalyticsClientAppRow] = []
    for client, client_tally in by_client.items():
        app = scope.application(client, client_tally.members) if client else Name(NO_TOKEN)
        client_rows.append(
            AnalyticsClientAppRow(
                **usage_values(
                    client_tally.metrics,
                    client_total.requests,
                    client_total.total_tokens,
                    client_tally.cost,
                ),
                client_app_id=client,
                label=app.label,
                principal_id=app.principal_id,
                apis=len(client_tally.resources),
            )
        )

    cost_center_rows = [
        AnalyticsCostCenterRow(
            **usage_values(center.metrics, requests, tokens, center.cost),
            key=cost_center_id,
            label=cost_center_refs[cost_center_id].name,
            code=cost_center_refs[cost_center_id].code,
            grants=len(center.grants),
            callers=len(center.members),
        )
        for cost_center_id, center in cost_centers.items()
    ]

    lists = [person_rows, app_rows, group_rows]
    behalf_rows = _on_behalf_rows(context, on_behalf, costs, requests, tokens)
    limit = context.limit
    truncated = (
        any(len(rows) > limit for rows in lists)
        or len(grant_rows) > limit
        or len(client_rows) > limit
        or len(behalf_rows) > limit
    )
    return AnalyticsConsumers(
        **context.report(),
        linked_requests=requests,
        linked_tokens=tokens,
        unidentified_requests=unidentified,
        people=_ordered(person_rows, lambda row: row.label)[:limit],
        applications=_ordered(app_rows, lambda row: row.label)[:limit],
        groups=_ordered(group_rows, lambda row: row.label)[:limit],
        grants=_ordered(grant_rows, lambda row: row.subject_label)[:limit],
        client_apps=_ordered(client_rows, lambda row: row.label)[:limit],
        cost_centers=_ordered(cost_center_rows, lambda row: row.label)[:limit],
        on_behalf=_ordered(
            behalf_rows, lambda row: f"{row.person_label}|{row.mcp_label}|{row.key}"
        )[:limit],
        on_behalf_unresolved=_on_behalf_unresolved(scope, on_behalf),
        truncated=truncated,
        cost=tally.summary(costs.notes) if costs is not None else None,
    )
