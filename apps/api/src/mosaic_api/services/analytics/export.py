"""Analytics tables as CSV, safe to open in a spreadsheet."""

import csv
import io
from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel

from mosaic_api.services.analytics.models import (
    AnalyticsConsumers,
    AnalyticsCost,
    AnalyticsHygiene,
    AnalyticsLimits,
    AnalyticsModels,
    AnalyticsOverview,
    AnalyticsReliability,
    AnalyticsUnattributed,
    ExportView,
)

# A cell a spreadsheet would read as a formula is prefixed with a quote, so exported names and
# keys can't run anything when the file is opened.
_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")

_USAGE = [
    ("Requests", "requests"),
    ("Prompt tokens", "prompt_tokens"),
    ("Completion tokens", "completion_tokens"),
    ("Total tokens", "total_tokens"),
    ("Throttled", "throttled"),
    ("Quota refused", "quota_refused"),
    ("Errors", "errors"),
    ("Request share", "request_share"),
    ("Token share", "token_share"),
    ("Cost (USD)", "cost"),
    ("Last seen (UTC)", "last_seen"),
]
_GRANT_REF = [
    ("Subject", "subject_label"),
    ("Subject detail", "subject_detail"),
    ("Subject kind", "subject_kind"),
    ("Subject principal kind", "subject_principal_kind"),
    ("Resource", "resource_label"),
    ("Resource kind", "resource_kind"),
    ("Gateway", "gateway_name"),
    ("Entitlement ID", "entitlement_id"),
]
_CONSUMER = [
    ("Name", "label"),
    ("Detail", "detail"),
    ("Object ID", "key"),
    ("Kind", "kind"),
    ("Principal kind", "principal_kind"),
    *_USAGE,
    ("Grants", "grants"),
    ("Resources", "resources"),
]
_DENIALS = [
    ("Reason", "reason_label"),
    ("Reason code", "reason"),
    ("Caller", "caller_label"),
    ("Caller object ID", "caller_object_id"),
    ("Client application", "client_app_label"),
    ("Client application ID", "client_app_id"),
    ("Gateway", "gateway_name"),
    ("API", "api_label"),
    ("API name", "api_name"),
    ("Requests", "requests"),
    ("Last seen (UTC)", "last_seen"),
]

COLUMNS: dict[ExportView, list[tuple[str, str]]] = {
    "trend": [
        ("Start (UTC)", "start"),
        ("Requests", "requests"),
        ("Total tokens", "total_tokens"),
        ("Throttled", "throttled"),
        ("Quota refused", "quota_refused"),
        ("Denied", "denied"),
        ("Errors", "errors"),
        ("Cost (USD)", "cost"),
    ],
    "people": _CONSUMER,
    "applications": _CONSUMER,
    "groups": [*_CONSUMER, ("Members seen", "members")],
    "grants": [
        ("Subject", "subject_label"),
        ("Subject detail", "subject_detail"),
        ("Subject kind", "subject_kind"),
        ("Subject principal kind", "subject_principal_kind"),
        ("Resource", "resource_label"),
        ("Resource kind", "resource_kind"),
        ("Gateway", "gateway_name"),
        ("State", "state"),
        ("Entitlement ID", "entitlement_id"),
        *_USAGE,
        ("Callers", "callers"),
        ("Key requests", "key_requests"),
        ("Peak tokens a minute", "peak_minute_tokens"),
        ("Peak requests a minute", "peak_minute_requests"),
    ],
    "clientApps": [
        ("Client application", "label"),
        ("Client application ID", "client_app_id"),
        *_USAGE,
        ("APIs", "apis"),
    ],
    "apis": [
        ("API", "label"),
        ("API name", "api_name"),
        ("Gateway", "gateway_name"),
        ("Kind", "kind"),
        ("Removed", "removed"),
        *_USAGE,
        ("Metered requests", "metered_requests"),
        ("Denied", "denied"),
        ("Client errors", "client_errors"),
        ("Server errors", "server_errors"),
        ("Backend throttled", "backend_throttled"),
        ("Error rate", "error_rate"),
        ("p95 latency (ms)", "p95_latency_ms"),
        ("Average latency (ms)", "average_latency_ms"),
        ("Models", "models"),
    ],
    "models": [("Model", "model"), ("APIs", "apis"), *_USAGE],
    "deployments": [
        ("Endpoint", "endpoint_name"),
        ("Endpoint ID", "endpoint_id"),
        ("Deployment", "deployment_name"),
        ("Model", "model_name"),
        ("SKU", "sku_name"),
        ("Capacity (tokens a minute)", "capacity_tokens_per_minute"),
        *_USAGE,
        ("Peak tokens a minute", "peak_minute_tokens"),
        ("Peak requests a minute", "peak_minute_requests"),
        ("Peak utilization", "utilization"),
        ("Backend throttled", "backend_throttled"),
        ("Gateways", "gateways"),
    ],
    "denials": _DENIALS,
    "limits": [
        *_GRANT_REF,
        ("Member", "member_label"),
        ("Member object ID", "member_object_id"),
        ("Status", "status"),
        ("Limit kind", "kind"),
        ("Metric", "metric"),
        ("Period", "period"),
        ("Window (seconds)", "window_seconds"),
        ("Limit", "limit"),
        ("Used", "used"),
        ("Utilization", "utilization"),
        ("Partial", "partial"),
        ("Window start (UTC)", "window_start"),
        ("Window end (UTC)", "window_end"),
        ("Throttled", "throttled"),
        ("Quota refused", "quota_refused"),
    ],
    "unusedGrants": [
        *_GRANT_REF,
        ("Granted (UTC)", "granted_at"),
        ("Last used (UTC)", "last_used_at"),
    ],
    "unusedKeys": [
        *_GRANT_REF,
        ("Subscription", "subscription_name"),
        ("Token requests", "token_requests"),
    ],
    "untrackedGrants": [*_GRANT_REF, ("Reason", "reason")],
    "unattributed": [
        ("Gateway", "gateway_name"),
        ("API", "api_label"),
        ("API name", "api_name"),
        ("Subscription", "subscription"),
        ("Reason", "reason"),
        ("Requests", "requests"),
        ("Total tokens", "total_tokens"),
        ("Share", "share"),
        ("Cost (USD)", "cost"),
        ("Last seen (UTC)", "last_seen"),
    ],
    "costDeployments": [
        ("Deployment", "deployment_name"),
        ("Endpoint", "endpoint_name"),
        ("Model", "model_name"),
        ("Model version", "model_version"),
        ("Cloud", "cloud_label"),
        ("Deployment type", "deployment_type"),
        ("Region", "region"),
        ("Charged by", "pricing"),
        ("Input per 1M tokens (USD)", "input_per_million"),
        ("Cached input per 1M tokens (USD)", "cached_input_per_million"),
        ("Output per 1M tokens (USD)", "output_per_million"),
        ("PTU per hour (USD)", "ptu_hourly"),
        ("Monthly amount (USD)", "monthly_amount"),
        ("PTUs", "capacity"),
        ("Month cost (USD)", "month_cost"),
        ("Utilization", "utilization"),
        ("Requests", "requests"),
        ("Prompt tokens", "prompt_tokens"),
        ("Completion tokens", "completion_tokens"),
        ("Total tokens", "total_tokens"),
        ("Idle reserved cost (USD)", "idle_cost"),
        ("Cost (USD)", "cost"),
        ("Cost share", "cost_share"),
        ("Why it has no price", "unpriced_message"),
        ("Price ID", "price_id"),
    ],
    # Built by the chargeback export itself; see cost_report.CHARGEBACK_COLUMNS.
    "chargeback": [],
}

REPORT_FOR: dict[ExportView, str] = {
    "trend": "overview",
    "people": "consumers",
    "applications": "consumers",
    "groups": "consumers",
    "grants": "consumers",
    "clientApps": "consumers",
    "apis": "models",
    "models": "models",
    "deployments": "models",
    "denials": "reliability",
    "limits": "limits",
    "unusedGrants": "hygiene",
    "unusedKeys": "hygiene",
    "untrackedGrants": "hygiene",
    "unattributed": "unattributed",
    "costDeployments": "cost",
    "chargeback": "chargeback",
}


def cell(value: Any) -> str | int | float:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    text = "; ".join(str(item) for item in value) if isinstance(value, list) else str(value)
    return f"'{text}" if text.startswith(_FORMULA_STARTS) else text


def to_csv(columns: Sequence[tuple[str, str]], rows: Iterable[Mapping[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([header for header, _ in columns])
    for row in rows:
        writer.writerow([cell(row.get(name)) for _, name in columns])
    return buffer.getvalue()


def filename(view: ExportView, first: date, last: date) -> str:
    return f"mosaic-{view}-{first:%Y%m%d}-{last:%Y%m%d}.csv"


def _dump(rows: Iterable[BaseModel]) -> list[dict[str, Any]]:
    return [row.model_dump(by_alias=False) for row in rows]


def table(view: ExportView, report: BaseModel) -> list[dict[str, Any]]:
    """The rows one export view takes from the report that holds them."""

    if isinstance(report, AnalyticsOverview) and view == "trend":
        return _dump(report.trend)
    if isinstance(report, AnalyticsConsumers):
        chosen: dict[str, Sequence[BaseModel]] = {
            "people": report.people,
            "applications": report.applications,
            "groups": report.groups,
            "grants": report.grants,
            "clientApps": report.client_apps,
        }
        return _dump(chosen.get(view, []))
    if isinstance(report, AnalyticsModels):
        models: dict[str, Sequence[BaseModel]] = {
            "apis": report.apis,
            "models": report.models,
            "deployments": report.deployments,
        }
        return _dump(models.get(view, []))
    if isinstance(report, AnalyticsReliability) and view == "denials":
        return _dump(report.denials)
    if isinstance(report, AnalyticsLimits) and view == "limits":
        flattened: list[dict[str, Any]] = []
        for row in report.rows:
            base = row.model_dump(by_alias=False, exclude={"limits", "utilization"})
            for use in row.limits:
                flattened.append({**base, **use.model_dump(by_alias=False)})
        return flattened
    if isinstance(report, AnalyticsHygiene):
        hygiene: dict[str, Sequence[BaseModel]] = {
            "unusedGrants": report.unused_grants,
            "unusedKeys": report.unused_keys,
            "untrackedGrants": report.untracked_grants,
        }
        return _dump(hygiene.get(view, []))
    if isinstance(report, AnalyticsUnattributed) and view == "unattributed":
        return _dump(report.rows)
    if isinstance(report, AnalyticsCost) and view == "costDeployments":
        return _dump(report.deployments)
    return []
