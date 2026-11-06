"""Azure Cost Management billing connector for contract-amortized rate derivation.

Supports two read-only retrieval modes over Azure Commercial and Azure Government:
1. ``costDetailsApi``: Asynchronous Cost Details API (``generateCostDetailsReport``,
   ``metric="AmortizedCost"``) with 202 polling, ``Retry-After`` backoff, and multi-blob
   partition download.
2. ``exportManifest``: Reads an existing Azure Cost Management export ``manifest.json``
   and downloads all referenced CSV/CSV.GZ partitions.
"""

from __future__ import annotations

import calendar
import ipaddress
import json
import posixpath
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import SplitResult, parse_qsl, urlsplit

import httpx
import structlog
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity.aio import ClientSecretCredential, DefaultAzureCredential

from mosaic_api.azure_connections import AzureConnectionService
from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingConnectionCheckResult,
    BillingConnectorPayloadPart,
    BillingConnectorStepResult,
    ConnectedBillingProvider,
    parse_source_month,
    previous_complete_month,
    validate_source_month,
)
from mosaic_api.clouds import AzureCloud, cloud_profile
from mosaic_api.domain import utc_now
from mosaic_api.errors import (
    DomainError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.services.billing_ingestion import (
    DEFAULT_MAX_UPLOAD_BYTES,
    BillingPayloadTooLargeError,
)

logger = structlog.get_logger()

COST_DETAILS_API_VERSION = "2022-05-01"
COST_MANAGEMENT_EXPORTS_API_VERSION = "2023-08-01"
# Storage uses the same resource ID in public and sovereign clouds.
AZURE_STORAGE_SCOPE = "https://storage.azure.com/.default"
DEFAULT_POLL_INTERVAL_SECONDS = 30
MIN_POLL_INTERVAL_SECONDS = 5
MAX_POLL_INTERVAL_SECONDS = 300
MAX_COST_DETAILS_LOOKBACK_MONTHS = 13
DEFAULT_MAX_PARTITIONS = 64
DEFAULT_HTTP_TIMEOUT_SECONDS = 60.0
IMDS_ADDRESS = "169.254.169.254"

AzureBillingScopeKind = Literal[
    "subscription",
    "billingAccount",
    "billingProfile",
    "invoiceSection",
    "enrollmentAccount",
    "department",
    "customer",
]
AzureBillingTokenProvider = Callable[[BillingConnection, str | None], Awaitable[str]]
Clock = Callable[[], datetime]

_ALLOWED_BLOB_SUFFIXES: dict[AzureCloud, tuple[str, ...]] = {
    "commercial": (".blob.core.windows.net",),
    "government": (".blob.core.usgovcloudapi.net",),
}

_SAS_QUERY_KEYS: frozenset[str] = frozenset(
    {
        "sig",
        "sv",
        "se",
        "st",
        "sp",
        "sr",
        "spr",
        "skoid",
        "sktid",
        "skt",
        "ske",
        "sks",
        "skv",
        "sdd",
        "ses",
        "sas",
        "si",
        "sip",
        "api_key",
        "access_token",
        "client_secret",
    }
)

_MANAGEMENT_GROUP_SCOPE_RE = re.compile(
    r"^/providers/microsoft\.management/managementgroups(?:/[^/]+)?$",
    re.IGNORECASE,
)

_SCOPE_PATTERNS: tuple[tuple[AzureBillingScopeKind, re.Pattern[str], str], ...] = (
    (
        "subscription",
        re.compile(r"^/subscriptions/(?P<sub_id>[A-Za-z0-9._-]+)$", re.IGNORECASE),
        "/subscriptions/{sub_id}",
    ),
    (
        "billingAccount",
        re.compile(
            r"^/providers/microsoft\.billing/billingaccounts/(?P<ba_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/billingAccounts/{ba_id}",
    ),
    (
        "billingProfile",
        re.compile(
            r"^/providers/microsoft\.billing/billingaccounts/(?P<ba_id>[A-Za-z0-9._:-]+)"
            r"/billingprofiles/(?P<bp_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/billingAccounts/{ba_id}/billingProfiles/{bp_id}",
    ),
    (
        "invoiceSection",
        re.compile(
            r"^/providers/microsoft\.billing/billingaccounts/(?P<ba_id>[A-Za-z0-9._:-]+)"
            r"/billingprofiles/(?P<bp_id>[A-Za-z0-9._:-]+)"
            r"/invoicesections/(?P<is_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/billingAccounts/{ba_id}"
        "/billingProfiles/{bp_id}/invoiceSections/{is_id}",
    ),
    (
        "enrollmentAccount",
        re.compile(
            r"^/providers/microsoft\.billing/billingaccounts/(?P<ba_id>[A-Za-z0-9._:-]+)"
            r"/enrollmentaccounts/(?P<ea_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/billingAccounts/{ba_id}/enrollmentAccounts/{ea_id}",
    ),
    (
        "enrollmentAccount",
        re.compile(
            r"^/providers/microsoft\.billing/enrollmentaccounts/(?P<ea_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/enrollmentAccounts/{ea_id}",
    ),
    (
        "department",
        re.compile(
            r"^/providers/microsoft\.billing/billingaccounts/(?P<ba_id>[A-Za-z0-9._:-]+)"
            r"/departments/(?P<dept_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/billingAccounts/{ba_id}/departments/{dept_id}",
    ),
    (
        "customer",
        re.compile(
            r"^/providers/microsoft\.billing/billingaccounts/(?P<ba_id>[A-Za-z0-9._:-]+)"
            r"/customers/(?P<cust_id>[A-Za-z0-9._:-]+)$",
            re.IGNORECASE,
        ),
        "/providers/Microsoft.Billing/billingAccounts/{ba_id}/customers/{cust_id}",
    ),
)

_URL_WITH_QUERY_RE = re.compile(r"(https?://[^\s\"'<>?]+)\?[^\s\"'<>]*", re.IGNORECASE)
_SENSITIVE_PARAM_RE = re.compile(
    r"(?i)\b(?:sig|sv|se|st|sp|sr|spr|skoid|sktid|skt|ske|sks|skv|sas|client_secret|"
    r"access_token|api_key)=[^&\s\"'<>]+"
)
_BEARER_TOKEN_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")

_RUNNING_STATES: frozenset[str] = frozenset(
    {"running", "inprogress", "in_progress", "queued", "pending", "notstarted"}
)
_COMPLETED_STATES: frozenset[str] = frozenset({"completed", "succeeded"})
_NO_DATA_STATES: frozenset[str] = frozenset({"nodatafound", "no_data_found"})
_FAILED_STATES: frozenset[str] = frozenset({"failed", "canceled", "cancelled"})

_REQUIRED_BILLING_ROLES: tuple[str, ...] = (
    "Cost Management Reader",
    "EnrollmentReader",
    "Billing profile reader",
)


@dataclass(frozen=True)
class ValidatedAzureBillingScope:
    """Normalized Azure billing scope and its structural classification."""

    scope: str
    kind: AzureBillingScopeKind


def redact_azure_url(url: str | None) -> str:
    """Return ``url`` with credentials, query parameters, and fragments removed."""

    if not url:
        return ""
    cleaned = url.strip()
    try:
        parts = urlsplit(cleaned)
    except ValueError:
        return cleaned.split("?", 1)[0].split("#", 1)[0]
    if not parts.scheme or not parts.hostname:
        return cleaned.split("?", 1)[0].split("#", 1)[0]
    host = parts.hostname
    try:
        port_suffix = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        port_suffix = ""
    return f"{parts.scheme.casefold()}://{host}{port_suffix}{parts.path}"


def redact_sensitive_text(text: str | None, *, max_length: int = 500) -> str:
    """Scrub URL query strings, SAS parameters, and bearer tokens from diagnostic text."""

    if not text:
        return ""
    compact = " ".join("".join(ch if ch.isprintable() else " " for ch in text).split())
    redacted = _URL_WITH_QUERY_RE.sub(r"\1", compact)
    redacted = _SENSITIVE_PARAM_RE.sub("[REDACTED]", redacted)
    redacted = _BEARER_TOKEN_RE.sub("Bearer [REDACTED]", redacted)
    if len(redacted) > max_length:
        return f"{redacted[: max_length - 1].rstrip()}…"
    return redacted


def _contains_sas_query(query: str) -> bool:
    if not query:
        return False
    for key, _ in parse_qsl(query, keep_blank_values=True):
        if key.strip().casefold() in _SAS_QUERY_KEYS:
            return True
    return bool(_SENSITIVE_PARAM_RE.search(query))


def parse_azure_billing_scope(scope: str | None) -> ValidatedAzureBillingScope:
    """Validate and canonicalize an Azure billing scope.

    Explicitly rejects Management Group scopes because the Azure Cost Details API does not
    support management groups for either EA or MCA agreements.
    """

    raw = (scope or "").strip()
    if not raw:
        raise ValidationError(
            "Provide an Azure billing scope (/subscriptions/{subscriptionId} or "
            "/providers/Microsoft.Billing/billingAccounts/...).",
            details={"reason": "missingBillingScope"},
        )

    normalized = "/" + raw.strip("/")
    if any(token in normalized for token in ("?", "#", "..", "\\")) or any(
        ch.isspace() for ch in normalized
    ):
        raise ValidationError(
            "Azure billing scope contains unsupported path, query, or whitespace characters.",
            details={
                "reason": "invalidBillingScope",
                "scope": redact_sensitive_text(normalized),
            },
        )

    if _MANAGEMENT_GROUP_SCOPE_RE.fullmatch(normalized) or normalized.casefold().startswith(
        "/providers/microsoft.management/managementgroups"
    ):
        raise ValidationError(
            "Azure Cost Details API does not support management group scopes "
            "(/providers/Microsoft.Management/managementGroups/...). Configure a subscription "
            "scope (/subscriptions/{subscriptionId}), a billing account or billing profile "
            "scope (/providers/Microsoft.Billing/billingAccounts/...), or upload a Cost Details "
            "CSV extraction.",
            details={
                "reason": "managementGroupUnsupported",
                "scope": normalized,
                "recommendedScopes": [
                    "/subscriptions/{subscriptionId}",
                    "/providers/Microsoft.Billing/billingAccounts/{billingAccountId}",
                    (
                        "/providers/Microsoft.Billing/billingAccounts/{billingAccountId}"
                        "/billingProfiles/{billingProfileId}"
                    ),
                ],
                "recommendedAlternatives": ["subscriptionScope", "billingAccountScope", "upload"],
            },
        )

    for kind, pattern, template in _SCOPE_PATTERNS:
        match = pattern.fullmatch(normalized)
        if match is not None:
            return ValidatedAzureBillingScope(
                scope=template.format(**match.groupdict()),
                kind=kind,
            )

    raise ValidationError(
        "Unsupported Azure billing scope. Use /subscriptions/{subscriptionId}, "
        "/providers/Microsoft.Billing/billingAccounts/{billingAccountId}, or a billing profile, "
        "enrollment account, department, or customer scope under "
        "/providers/Microsoft.Billing/billingAccounts/...",
        details={
            "reason": "invalidBillingScope",
            "scope": redact_sensitive_text(normalized),
        },
    )


def validate_azure_billing_scope(scope: str | None) -> str:
    """Validate and return the canonical Azure billing scope path."""

    return parse_azure_billing_scope(scope).scope


def _is_blocked_or_ip_host(host: str) -> bool:
    folded = host.casefold().rstrip(".")
    if folded in {"localhost", IMDS_ADDRESS} or folded.endswith((".local", ".internal")):
        return True
    try:
        addr = ipaddress.ip_address(folded)
    except ValueError:
        return False
    return (
        addr.is_loopback
        or addr.is_link_local
        or addr.is_private
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
        or True  # Azure ARM and Blob Storage endpoints are never raw IP literals
    )


def _parse_https_url(url: str, *, context: str) -> tuple[SplitResult, str]:
    cleaned = (url or "").strip()
    if not cleaned:
        raise ValidationError(
            f"Missing {context} URL.",
            details={"reason": "missingUrl"},
        )
    try:
        parts = urlsplit(cleaned)
    except ValueError as exc:
        raise ValidationError(
            f"Malformed {context} URL.",
            details={"reason": "malformedUrl"},
        ) from exc

    scheme = parts.scheme.casefold()
    if scheme != "https":
        raise ValidationError(
            f"{context} URL must use https://.",
            details={
                "reason": "insecureScheme",
                "scheme": parts.scheme,
                "url": redact_azure_url(cleaned),
            },
        )
    if parts.username or parts.password:
        raise ValidationError(
            f"Do not embed credentials in {context} URL.",
            details={
                "reason": "embeddedCredentials",
                "url": redact_azure_url(cleaned),
            },
        )
    host = (parts.hostname or "").casefold().rstrip(".")
    if not host:
        raise ValidationError(
            f"{context} URL must include a hostname.",
            details={"reason": "missingHost"},
        )
    if _is_blocked_or_ip_host(host):
        raise ValidationError(
            f"MOSAIC refuses to connect to private, loopback, link-local, or IP-literal host "
            f"{host!r} for {context}.",
            details={
                "reason": "ssrfBlockedHost",
                "host": host,
                "url": redact_azure_url(cleaned),
            },
        )
    return parts, host


def validate_azure_arm_url(url: str, *, cloud: AzureCloud) -> str:
    """Validate that ``url`` is an HTTPS URL on the expected Azure Resource Manager host."""

    parts, host = _parse_https_url(url, context="Azure Resource Manager")
    if _contains_sas_query(parts.query):
        raise ValidationError(
            "Azure Resource Manager URLs must not contain SAS tokens or secret query parameters.",
            details={
                "reason": "sasTokenNotAllowed",
                "url": redact_azure_url(url),
            },
        )
    profile = cloud_profile(cloud)
    expected_host = (urlsplit(profile.arm_endpoint).hostname or "").casefold().rstrip(".")
    if host != expected_host:
        raise ValidationError(
            f"Untrusted Azure Resource Manager host {host!r}; expected {expected_host!r} for "
            f"Azure {cloud}.",
            details={
                "reason": "untrustedArmHost",
                "host": host,
                "expectedHost": expected_host,
                "azureCloud": cloud,
                "url": redact_azure_url(url),
            },
        )
    return url.strip()


def validate_azure_blob_url(
    url: str,
    *,
    cloud: AzureCloud,
    allow_sas_query: bool = False,
) -> str:
    """Validate that ``url`` targets a trusted Azure Blob Storage endpoint for ``cloud``."""

    parts, host = _parse_https_url(url, context="Azure Blob Storage")
    if not allow_sas_query and (_contains_sas_query(parts.query) or bool(parts.query)):
        raise ValidationError(
            "Do not store SAS tokens or query strings in the configured Azure export manifest URL; "
            "authenticate with the configured Azure connection identity instead.",
            details={
                "reason": "sasTokenNotAllowed",
                "url": redact_azure_url(url),
            },
        )

    other_cloud: AzureCloud = "government" if cloud == "commercial" else "commercial"
    if any(host.endswith(suffix) for suffix in _ALLOWED_BLOB_SUFFIXES[other_cloud]):
        raise ValidationError(
            f"Azure Blob Storage host {host!r} belongs to Azure {other_cloud}, but the billing "
            f"connection is configured for Azure {cloud}.",
            details={
                "reason": "cloudMismatch",
                "host": host,
                "azureCloud": cloud,
                "url": redact_azure_url(url),
            },
        )

    allowed_suffixes = _ALLOWED_BLOB_SUFFIXES[cloud]
    matched_suffix = next((s for s in allowed_suffixes if host.endswith(s)), None)
    if matched_suffix is None:
        raise ValidationError(
            f"Untrusted Azure Blob Storage host {host!r}; expected a storage account on "
            f"*{allowed_suffixes[0]} for Azure {cloud}.",
            details={
                "reason": "untrustedBlobHost",
                "host": host,
                "expectedSuffix": allowed_suffixes[0],
                "azureCloud": cloud,
                "url": redact_azure_url(url),
            },
        )

    account_label = host[: -len(matched_suffix)]
    if not account_label or "." in account_label:
        raise ValidationError(
            f"Invalid Azure Blob Storage account hostname {host!r}.",
            details={
                "reason": "untrustedBlobHost",
                "host": host,
                "url": redact_azure_url(url),
            },
        )
    return url.strip()


def validate_azure_export_manifest_url(url: str | None, *, cloud: AzureCloud) -> str:
    """Validate a configured ``azure_export_manifest_url`` against SSRF, cloud, and SAS rules."""

    if not url or not url.strip():
        raise ValidationError(
            "Azure export-manifest retrieval requires an HTTPS export manifest URL.",
            details={"reason": "missingManifestUrl"},
        )
    parts, host = _parse_https_url(url, context="Azure export manifest")
    if _contains_sas_query(parts.query):
        raise ValidationError(
            "Do not store SAS tokens in azure_export_manifest_url; authenticate using the "
            "configured Azure connection identity instead.",
            details={
                "reason": "sasTokenNotAllowed",
                "url": redact_azure_url(url),
            },
        )
    profile = cloud_profile(cloud)
    expected_arm_host = (urlsplit(profile.arm_endpoint).hostname or "").casefold().rstrip(".")
    if host == expected_arm_host:
        return validate_azure_arm_url(url, cloud=cloud)
    return validate_azure_blob_url(url, cloud=cloud, allow_sas_query=False)


def _manifest_token_scope(manifest_url: str, *, cloud: AzureCloud) -> str:
    profile = cloud_profile(cloud)
    host = (urlsplit(manifest_url).hostname or "").rstrip(".")
    return (
        profile.arm_scope
        if host == urlsplit(profile.arm_endpoint).hostname
        else AZURE_STORAGE_SCOPE
    )


def _supported_cost_details_months(today: date) -> list[str]:
    """Return the last 13 completed calendar months in ``YYYY-MM`` form, newest first."""

    months: list[str] = []
    year = today.year
    month = today.month - 1
    if month == 0:
        year -= 1
        month = 12
    for _ in range(MAX_COST_DETAILS_LOOKBACK_MONTHS):
        months.append(f"{year:04d}-{month:02d}")
        month -= 1
        if month == 0:
            year -= 1
            month = 12
    return months


def _validate_lookback_window(
    source_month: str,
    *,
    today: date,
    enforce_cost_details_limit: bool,
) -> tuple[date, date]:
    validate_source_month(source_month, today=today)
    start_date, end_date = parse_source_month(source_month)
    expected_last_day = calendar.monthrange(start_date.year, start_date.month)[1]
    if (
        start_date.day != 1
        or end_date.day != expected_last_day
        or start_date.month != end_date.month
        or start_date.year != end_date.year
    ):
        raise ValidationError(
            "Azure Cost Details requests must span at most one complete calendar month.",
            details={"reason": "invalidDateRange", "sourceMonth": source_month},
        )

    if enforce_cost_details_limit:
        months_ago = (today.year - start_date.year) * 12 + (today.month - start_date.month)
        if months_ago > MAX_COST_DETAILS_LOOKBACK_MONTHS:
            earliest_supported = _supported_cost_details_months(today)[-1]
            raise ValidationError(
                f"Azure Cost Details API on-demand reports only support the past "
                f"{MAX_COST_DETAILS_LOOKBACK_MONTHS} months (from {earliest_supported} through "
                f"{previous_complete_month(today)}); requested month {source_month} is "
                f"{months_ago} months old. Configure an Azure Cost Management export manifest "
                "(exportManifest) or upload a Cost Details CSV extraction for older months.",
                details={
                    "reason": "lookbackExceedsCostDetailsLimit",
                    "sourceMonth": source_month,
                    "monthsAgo": months_ago,
                    "maxLookbackMonths": MAX_COST_DETAILS_LOOKBACK_MONTHS,
                    "recommendedAlternatives": ["exportManifest", "upload"],
                },
            )
    return start_date, end_date


def _parse_retry_after(header_value: str | None) -> int:
    if not header_value:
        return DEFAULT_POLL_INTERVAL_SECONDS
    cleaned = header_value.strip()
    try:
        seconds = int(float(cleaned))
    except ValueError:
        return DEFAULT_POLL_INTERVAL_SECONDS
    return min(max(seconds, MIN_POLL_INTERVAL_SECONDS), MAX_POLL_INTERVAL_SECONDS)


def _extract_arm_error(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return "", redact_sensitive_text(response.text, max_length=300)

    if isinstance(payload, dict):
        err_obj = payload.get("error")
        if isinstance(err_obj, dict):
            code = str(err_obj.get("code") or "").strip()
            msg = str(err_obj.get("message") or "").strip()
            return redact_sensitive_text(code, max_length=80), redact_sensitive_text(
                msg, max_length=300
            )
        code = str(payload.get("code") or "").strip()
        msg = str(payload.get("message") or "").strip()
        return redact_sensitive_text(code, max_length=80), redact_sensitive_text(
            msg, max_length=300
        )
    return "", ""


def _permission_error_message(scope: str, *, include_blob_role: bool = False) -> str:
    blob_clause = (
        " and 'Storage Blob Data Reader' on the export storage container"
        if include_blob_role
        else ""
    )
    return (
        f"MOSAIC's identity lacks permission to read Azure billing data for {scope}. "
        "Grant at least read-only billing access: 'Cost Management Reader' on a subscription "
        "scope, 'EnrollmentReader' on an Enterprise Agreement (EA) scope, or "
        f"'Billing profile reader' on a Microsoft Customer Agreement (MCA) scope{blob_clause}."
    )


def _unsupported_agreement_message(scope: str, upstream_reason: str = "") -> str:
    reason_suffix = f" ({upstream_reason})" if upstream_reason else ""
    return (
        f"Azure Cost Details API is not supported for scope {scope}{reason_suffix}. "
        "On-demand Cost Details reports require an Enterprise Agreement (EA), Microsoft "
        "Customer Agreement (MCA), or Microsoft Partner Agreement (MPA) and are unavailable "
        "for legacy Pay-As-You-Go, MSDN, or Visual Studio subscriptions. Configure an Azure "
        "Cost Management export manifest (exportManifest) or upload a Cost Details CSV "
        "extraction instead."
    )


def _extract_operation_id(payload: object, location: str | None) -> str | None:
    if isinstance(payload, dict):
        raw_name = payload.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            return redact_sensitive_text(raw_name.strip(), max_length=120)
        raw_id = payload.get("id")
        if isinstance(raw_id, str) and raw_id.strip():
            tail = raw_id.strip().rstrip("/").rsplit("/", 1)[-1]
            if tail:
                return redact_sensitive_text(tail, max_length=120)
    if location:
        path = urlsplit(location).path.rstrip("/")
        if path:
            segment = path.rsplit("/", 1)[-1]
            if segment:
                return redact_sensitive_text(segment, max_length=120)
    return None


def _extract_manifest_dict(payload: dict[str, Any]) -> dict[str, Any]:
    props = payload.get("properties")
    if isinstance(props, dict):
        inner_manifest = props.get("manifest")
        if isinstance(inner_manifest, dict):
            return inner_manifest
        if "blobs" in props or "manifestVersion" in props:
            return props
    top_manifest = payload.get("manifest")
    if isinstance(top_manifest, dict):
        return top_manifest
    return payload


def _extract_manifest_metric(manifest: dict[str, Any], outer: dict[str, Any]) -> str | None:
    candidates: list[Any] = []
    for obj in (manifest, outer):
        req_ctx = obj.get("requestContext")
        if isinstance(req_ctx, dict):
            req_body = req_ctx.get("requestBody")
            if isinstance(req_body, dict):
                candidates.append(req_body.get("metric"))
                candidates.append(req_body.get("type"))
            candidates.append(req_ctx.get("metric"))
        exp_cfg = obj.get("exportConfig")
        if isinstance(exp_cfg, dict):
            candidates.append(exp_cfg.get("metric"))
            candidates.append(exp_cfg.get("type"))
        defn = obj.get("definition")
        if isinstance(defn, dict):
            candidates.append(defn.get("type"))
            candidates.append(defn.get("metric"))
        candidates.append(obj.get("metric"))
        candidates.append(obj.get("type"))

    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return None


def _extract_manifest_time_period(
    manifest: dict[str, Any],
    outer: dict[str, Any],
) -> tuple[date | None, date | None]:
    period_candidates: list[tuple[Any, Any]] = []
    for obj in (manifest, outer):
        req_ctx = obj.get("requestContext")
        if isinstance(req_ctx, dict):
            req_body = req_ctx.get("requestBody")
            if isinstance(req_body, dict):
                tp = req_body.get("timePeriod")
                if isinstance(tp, dict):
                    period_candidates.append(
                        (tp.get("start") or tp.get("from"), tp.get("end") or tp.get("to"))
                    )
        tp_top = obj.get("timePeriod")
        if isinstance(tp_top, dict):
            period_candidates.append(
                (
                    tp_top.get("start") or tp_top.get("from"),
                    tp_top.get("end") or tp_top.get("to"),
                )
            )
        run_info = obj.get("runInfo")
        if isinstance(run_info, dict):
            period_candidates.append((run_info.get("startDate"), run_info.get("endDate")))

    def _parse_iso_day(val: Any) -> date | None:
        if not isinstance(val, str) or len(val.strip()) < 10:
            return None
        prefix = val.strip()[:10]
        try:
            return date.fromisoformat(prefix)
        except ValueError:
            return None

    for raw_start, raw_end in period_candidates:
        s_date = _parse_iso_day(raw_start)
        e_date = _parse_iso_day(raw_end)
        if s_date is not None or e_date is not None:
            return s_date, e_date
    return None, None


def _validate_manifest_structure(
    payload: Any,
    *,
    cloud: AzureCloud,
    base_manifest_url: str | None = None,
    expected_source_month: str | None = None,
    max_partitions: int = DEFAULT_MAX_PARTITIONS,
    max_total_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> tuple[str, list[str], bool]:
    """Validate manifest JSON and return ``(manifest_id, validated_blob_urls, compress_data)``."""

    if not isinstance(payload, dict):
        raise ValidationError(
            "Azure billing manifest response must be a JSON object.",
            details={"reason": "invalidManifestJson"},
        )

    manifest = _extract_manifest_dict(payload)
    manifest_version = str(manifest.get("manifestVersion") or "").strip()
    data_format = str(manifest.get("dataFormat") or "Csv").strip()
    if data_format and data_format.casefold() != "csv":
        raise ValidationError(
            f"Unsupported Azure billing manifest dataFormat {data_format!r}; 'Csv' is required.",
            details={"reason": "unsupportedDataFormat", "dataFormat": data_format},
        )

    metric = _extract_manifest_metric(manifest, payload)
    if metric is None:
        raise ValidationError(
            "Azure billing manifest does not declare a cost metric; 'AmortizedCost' is required "
            "so reservation and savings plan usage is not priced at $0.",
            details={"reason": "missingManifestMetric", "requiredMetric": "AmortizedCost"},
        )
    if metric.casefold() != "amortizedcost":
        raise ValidationError(
            f"Azure billing manifest was generated with metric {metric!r}, but MOSAIC requires "
            "'AmortizedCost' so reservation and savings plan usage includes amortized commitment "
            "cost instead of $0 cash charges.",
            details={
                "reason": "actualCostNotSupported",
                "metric": metric,
                "requiredMetric": "AmortizedCost",
            },
        )

    if expected_source_month is not None:
        expected_start, expected_end = parse_source_month(expected_source_month)
        obs_start, obs_end = _extract_manifest_time_period(manifest, payload)
        if obs_start is not None and (obs_start < expected_start or obs_start > expected_end):
            raise ValidationError(
                f"Azure billing manifest period starts on {obs_start.isoformat()}, which does "
                f"not match requested source month {expected_source_month}.",
                details={
                    "reason": "manifestPeriodMismatch",
                    "sourceMonth": expected_source_month,
                    "manifestStart": obs_start.isoformat(),
                },
            )
        if obs_end is not None and (obs_end < expected_start or obs_end > expected_end):
            raise ValidationError(
                f"Azure billing manifest period ends on {obs_end.isoformat()}, which does not "
                f"match requested source month {expected_source_month}.",
                details={
                    "reason": "manifestPeriodMismatch",
                    "sourceMonth": expected_source_month,
                    "manifestEnd": obs_end.isoformat(),
                },
            )

    declared_bytes = manifest.get("byteCount")
    if isinstance(declared_bytes, int) and declared_bytes > max_total_bytes:
        raise BillingPayloadTooLargeError(
            f"Azure billing manifest declares {declared_bytes:,} bytes, which exceeds the "
            f"maximum allowed download size ({max_total_bytes:,} bytes).",
            details={
                "reason": "manifestByteCountExceeded",
                "byteCount": declared_bytes,
                "maxTotalBytes": max_total_bytes,
            },
        )

    raw_blobs = manifest.get("blobs")
    if not isinstance(raw_blobs, list) or not raw_blobs:
        raise ValidationError(
            "Azure billing manifest contains no partition blobs to download.",
            details={"reason": "emptyManifestBlobs"},
        )
    if len(raw_blobs) > max_partitions:
        raise ValidationError(
            f"Azure billing manifest contains {len(raw_blobs)} partitions, exceeding the "
            f"maximum allowed partition count ({max_partitions}).",
            details={
                "reason": "tooManyPartitions",
                "partitionCount": len(raw_blobs),
                "maxPartitions": max_partitions,
            },
        )

    validated_links: list[str] = []
    for idx, blob_entry in enumerate(raw_blobs, start=1):
        if not isinstance(blob_entry, dict):
            raise ValidationError(
                f"Partition entry {idx} in Azure billing manifest is not a JSON object.",
                details={"reason": "invalidManifestBlobEntry", "partitionIndex": idx},
            )
        raw_link = str(blob_entry.get("blobLink") or "").strip()
        raw_name = str(blob_entry.get("blobName") or "").strip()
        if not raw_link and raw_name and base_manifest_url:
            if ".." in raw_name or "?" in raw_name or "#" in raw_name:
                raise ValidationError(
                    f"Partition {idx} blobName in Azure billing manifest contains invalid path "
                    "characters.",
                    details={"reason": "invalidBlobName", "partitionIndex": idx},
                )
            base_parts = urlsplit(base_manifest_url)
            path_segments = [seg for seg in base_parts.path.split("/") if seg]
            container = path_segments[0] if path_segments else "exports"
            raw_link = (
                f"{base_parts.scheme}://{base_parts.netloc}/{container}/{raw_name.lstrip('/')}"
            )

        if not raw_link:
            raise ValidationError(
                f"Partition {idx} in Azure billing manifest is missing a blobLink.",
                details={"reason": "missingBlobLink", "partitionIndex": idx},
            )

        validated_url = validate_azure_blob_url(
            raw_link,
            cloud=cloud,
            allow_sas_query=True,
        )
        validated_links.append(validated_url)

    compress_data = bool(manifest.get("compressData", False))
    raw_manifest_id = (
        str(
            payload.get("name")
            or manifest.get("manifestId")
            or (
                manifest.get("runInfo", {}).get("runId")
                if isinstance(manifest.get("runInfo"), dict)
                else ""
            )
            or manifest_version
            or "azure-cost-details-manifest"
        ).strip()
    )
    clean_manifest_id = redact_sensitive_text(
        raw_manifest_id.split("?", 1)[0].split("#", 1)[0],
        max_length=120,
    )
    return clean_manifest_id, validated_links, compress_data


def _clean_partition_name(blob_url: str, *, index: int, compress_data: bool, content: bytes) -> str:
    """Derive a clean, SAS-free filename for a downloaded partition blob."""

    path = urlsplit(blob_url).path.rstrip("/")
    basename = posixpath.basename(path) if path else ""
    basename = re.sub(r"[^A-Za-z0-9._-]", "_", basename).strip("._")
    if not basename:
        basename = f"part-{index:04d}"

    lower = basename.casefold()
    is_gzip = compress_data or content.startswith(b"\x1f\x8b")
    if is_gzip:
        if not lower.endswith(".gz"):
            if lower.endswith(".csv"):
                basename = f"{basename}.gz"
            else:
                basename = f"{basename}.csv.gz"
    elif not lower.endswith((".csv", ".csv.gz")):
        basename = f"{basename}.csv"
    return basename


class AzureBillingConnector:
    """Connected billing source adapter for Azure Cost Management (Commercial and Government)."""

    provider: ConnectedBillingProvider = "azure"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        token_provider: AzureBillingTokenProvider | None = None,
        connection_service: AzureConnectionService | None = None,
        host_credential: AsyncTokenCredential | None = None,
        clock: Clock = utc_now,
        max_total_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        max_partitions: int = DEFAULT_MAX_PARTITIONS,
    ) -> None:
        self._http_client = http_client
        self._token_provider = token_provider
        self._connection_service = connection_service
        self._host_credential = host_credential
        self._clock = clock
        self._max_total_bytes = max_total_bytes
        self._max_partitions = max_partitions

    async def _acquire_token(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
        scope: str | None = None,
    ) -> str:
        """Obtain a token for the requested resource using the configured Azure identity."""

        cloud: AzureCloud = connection.azure_cloud or "commercial"
        profile = cloud_profile(cloud)
        scope = scope or profile.arm_scope

        if self._token_provider is not None:
            token = await self._token_provider(connection, secret_value)
            if not token or not token.strip():
                raise UpstreamAuthorizationError(
                    "Azure billing token provider returned an empty access token."
                )
            return token.strip()

        if (
            connection.azure_connection_id
            and connection.azure_connection_id != "host"
            and self._connection_service is not None
        ):
            await self._connection_service.validate_resource(
                connection.tenant_id,
                connection.azure_connection_id,
                cloud,
            )
            access_token = await self._connection_service.token(
                connection.azure_connection_id,
                (scope,),
            )
            return access_token.token

        if secret_value is not None and secret_value.strip():
            raw_secret = secret_value.strip()
            if raw_secret.startswith("{"):
                try:
                    parsed = json.loads(raw_secret)
                except ValueError as exc:
                    raise ValidationError(
                        "Stored Azure billing secret JSON is malformed."
                    ) from exc
                if not isinstance(parsed, dict):
                    raise ValidationError(
                        "Stored Azure billing secret must be a JSON object with tenantId, "
                        "clientId, and clientSecret."
                    )
                tenant_id = str(
                    parsed.get("tenantId") or parsed.get("azure_tenant_id") or ""
                ).strip()
                client_id = str(
                    parsed.get("clientId") or parsed.get("client_id") or ""
                ).strip()
                client_secret = str(
                    parsed.get("clientSecret") or parsed.get("client_secret") or ""
                ).strip()
                if not (tenant_id and client_id and client_secret):
                    raise ValidationError(
                        "Stored Azure billing secret JSON must include tenantId, clientId, "
                        "and clientSecret."
                    )
                try:
                    async with ClientSecretCredential(
                        tenant_id,
                        client_id,
                        client_secret,
                        authority=profile.authority,
                    ) as credential:
                        issued = await credential.get_token(scope)
                        return issued.token
                except ClientAuthenticationError:
                    raise UpstreamAuthorizationError(
                        "Azure billing connection authentication failed. Check the target cloud, "
                        "tenant ID, application client ID, and client secret expiry."
                    ) from None
                except (ServiceRequestError, ServiceResponseError):
                    raise UpstreamError(
                        "The Azure billing connection could not reach Entra ID to acquire a token."
                    ) from None
            return raw_secret

        if self._host_credential is not None:
            if cloud == "government" and connection.azure_connection_id in (None, "host"):
                raise ValidationError(
                    "Azure Government billing connections require an Azure Government "
                    "service-principal connection."
                )
            try:
                issued = await self._host_credential.get_token(scope)
                return issued.token
            except ClientAuthenticationError:
                raise UpstreamAuthorizationError(
                    "MOSAIC's managed identity could not authenticate to Azure."
                ) from None
            except (ServiceRequestError, ServiceResponseError):
                raise UpstreamError(
                    "MOSAIC could not reach Azure identity services to acquire a token."
                ) from None

        if self._connection_service is not None and cloud == "government":
            raise ValidationError(
                "Azure Government billing connections require an Azure Government "
                "service-principal connection."
            )

        if self._http_client is not None:
            # When running against an injected HTTP client/transport without an explicit
            # credential provider, avoid live IMDS calls and supply a synthetic bearer token.
            return "mosaic-synthetic-arm-token"

        try:
            async with DefaultAzureCredential(authority=profile.authority) as credential:
                issued = await credential.get_token(scope)
                return issued.token
        except ClientAuthenticationError:
            raise UpstreamAuthorizationError(
                "Azure authentication failed while acquiring a Cost Management access token."
            ) from None
        except (ServiceRequestError, ServiceResponseError):
            raise UpstreamError(
                "Could not reach Azure identity services while acquiring a Cost Management token."
            ) from None

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        redacted_target = redact_azure_url(url)
        try:
            if self._http_client is not None:
                return await self._http_client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                    follow_redirects=False,
                )
            timeout = httpx.Timeout(DEFAULT_HTTP_TIMEOUT_SECONDS)
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
                return await client.request(
                    method,
                    url,
                    headers=headers,
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            logger.warning(
                "azure_billing.http_error",
                method=method,
                url=redacted_target,
                error=redact_sensitive_text(str(exc), max_length=200),
            )
            raise UpstreamError(
                f"Could not reach Azure billing endpoint {redacted_target}. Check outbound "
                "connectivity and retry.",
                details={"url": redacted_target},
            ) from None

    async def _download_manifest_partitions(
        self,
        blob_urls: list[str],
        *,
        connection: BillingConnection,
        secret_value: str | None,
        storage_token: str | None = None,
        compress_data: bool,
    ) -> tuple[list[BillingConnectorPayloadPart], int]:
        parts: list[BillingConnectorPayloadPart] = []
        total_bytes = 0

        for idx, blob_url in enumerate(blob_urls, start=1):
            blob_url = validate_azure_blob_url(
                blob_url,
                cloud=connection.azure_cloud or "commercial",
                allow_sas_query=True,
            )
            redacted_blob_url = redact_azure_url(blob_url)
            parsed = urlsplit(blob_url)
            has_sas = _contains_sas_query(parsed.query)
            headers: dict[str, str] = {"x-ms-version": "2023-11-03"}
            if not has_sas:
                if storage_token is None:
                    storage_token = await self._acquire_token(
                        connection, secret_value=secret_value, scope=AZURE_STORAGE_SCOPE
                    )
                headers["Authorization"] = f"Bearer {storage_token}"

            response = await self._request("GET", blob_url, headers=headers)
            if response.status_code in (401, 403):
                raise UpstreamAuthorizationError(
                    f"Azure Blob Storage rejected access to billing partition {idx} "
                    f"({redacted_blob_url}) with HTTP {response.status_code}. Ensure the "
                    "report SAS URL has not expired or grant 'Storage Blob Data Reader' on "
                    "the export storage account.",
                    details={
                        "reason": "partitionAccessDenied",
                        "partitionIndex": idx,
                        "statusCode": response.status_code,
                        "blobUrl": redacted_blob_url,
                    },
                )
            if response.status_code != 200:
                err_code, err_msg = _extract_arm_error(response)
                raise UpstreamError(
                    f"Failed to download Azure billing partition {idx} ({redacted_blob_url}): "
                    f"HTTP {response.status_code}"
                    + (f" ({err_code}: {err_msg})" if err_code or err_msg else "."),
                    details={
                        "reason": "partitionDownloadFailed",
                        "partitionIndex": idx,
                        "statusCode": response.status_code,
                        "blobUrl": redacted_blob_url,
                    },
                )

            content = response.content
            total_bytes += len(content)
            if total_bytes > self._max_total_bytes:
                raise BillingPayloadTooLargeError(
                    f"Downloaded Azure billing partitions ({total_bytes:,} bytes) exceed the "
                    f"maximum allowed total size ({self._max_total_bytes:,} bytes).",
                    details={
                        "reason": "maxTotalBytesExceeded",
                        "totalReadBytes": total_bytes,
                        "maxTotalBytes": self._max_total_bytes,
                        "partitionIndex": idx,
                    },
                )

            part_name = _clean_partition_name(
                blob_url,
                index=idx,
                compress_data=compress_data,
                content=content,
            )
            content_type = (
                response.headers.get("Content-Type", "text/csv").split(";", 1)[0].strip()
                or "text/csv"
            )
            parts.append(
                BillingConnectorPayloadPart(
                    name=part_name,
                    content=content,
                    content_type=content_type,
                )
            )

        return parts, total_bytes

    async def check_access(
        self,
        connection: BillingConnection,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectionCheckResult:
        """Verify read-only access to the configured Azure billing scope or export manifest."""

        now = self._clock()
        today = now.date()
        has_secret = bool(connection.credential_secret_uri or secret_value)
        cloud: AzureCloud = connection.azure_cloud or "commercial"
        mode = connection.azure_retrieval_mode or "costDetailsApi"

        if connection.provider != "azure":
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider=connection.provider,
                status="failed",
                message="AzureBillingConnector only supports provider='azure'.",
                checked_at=now,
                has_stored_secret=has_secret,
                details={"reason": "invalidProvider"},
            )

        try:
            profile = cloud_profile(cloud)
            validated_scope = parse_azure_billing_scope(connection.azure_scope)
        except (ValidationError, ValueError) as exc:
            msg = exc.message if isinstance(exc, DomainError) else str(exc)
            details = dict(exc.details) if isinstance(exc, DomainError) else {}
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="failed",
                message=redact_sensitive_text(msg),
                checked_at=now,
                scope_summary=redact_sensitive_text(connection.azure_scope),
                has_stored_secret=has_secret,
                details=details,
            )

        scope = validated_scope.scope
        supported_months = _supported_cost_details_months(today)

        if mode == "exportManifest":
            try:
                manifest_url = validate_azure_export_manifest_url(
                    connection.azure_export_manifest_url,
                    cloud=cloud,
                )
            except ValidationError as exc:
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="azure",
                    status="failed",
                    message=redact_sensitive_text(exc.message),
                    checked_at=now,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details=dict(exc.details),
                )

            try:
                manifest_scope = _manifest_token_scope(manifest_url, cloud=cloud)
                token = await self._acquire_token(
                    connection, secret_value=secret_value, scope=manifest_scope
                )
                response = await self._request(
                    "GET",
                    manifest_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                        "x-ms-version": "2023-11-03",
                    },
                )
            except DomainError as exc:
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="azure",
                    status="failed",
                    message=redact_sensitive_text(exc.message),
                    checked_at=now,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details=dict(exc.details),
                )

            redacted_manifest_url = redact_azure_url(manifest_url)
            if response.status_code in (401, 403):
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="azure",
                    status="failed",
                    message=_permission_error_message(scope, include_blob_role=True),
                    checked_at=now,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details={
                        "reason": "missingBillingPermission",
                        "statusCode": response.status_code,
                        "requiredRoles": [
                            *_REQUIRED_BILLING_ROLES,
                            "Storage Blob Data Reader",
                        ],
                        "manifestUrl": redacted_manifest_url,
                        "scope": scope,
                    },
                )
            if response.status_code != 200:
                err_code, err_msg = _extract_arm_error(response)
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="azure",
                    status="failed",
                    message=(
                        f"Could not read Azure export manifest at {redacted_manifest_url} "
                        f"(HTTP {response.status_code}"
                        + (f": {err_code} {err_msg}".rstrip() if err_code or err_msg else "")
                        + "). Verify the export path and storage container permissions."
                    ),
                    checked_at=now,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details={
                        "reason": "manifestFetchFailed",
                        "statusCode": response.status_code,
                        "manifestUrl": redacted_manifest_url,
                        "scope": scope,
                    },
                )

            try:
                manifest_json = response.json()
            except ValueError:
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="azure",
                    status="failed",
                    message=(
                        f"Azure export manifest at {redacted_manifest_url} is not valid JSON."
                    ),
                    checked_at=now,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details={
                        "reason": "invalidManifestJson",
                        "manifestUrl": redacted_manifest_url,
                    },
                )

            try:
                manifest_id, blob_links, _ = _validate_manifest_structure(
                    manifest_json,
                    cloud=cloud,
                    base_manifest_url=manifest_url,
                    max_partitions=self._max_partitions,
                    max_total_bytes=self._max_total_bytes,
                )
            except DomainError as exc:
                return BillingConnectionCheckResult(
                    connection_id=connection.id,
                    provider="azure",
                    status="failed",
                    message=redact_sensitive_text(exc.message),
                    checked_at=now,
                    scope_summary=scope,
                    has_stored_secret=has_secret,
                    details={
                        **exc.details,
                        "manifestUrl": redacted_manifest_url,
                        "scope": scope,
                    },
                )

            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="ready",
                message=(
                    f"Verified read access to Azure AmortizedCost export manifest "
                    f"({len(blob_links)} partition(s)) for {scope}."
                ),
                checked_at=now,
                scope_summary=scope,
                has_stored_secret=has_secret,
                supported_source_months=supported_months,
                details={
                    "azureCloud": cloud,
                    "retrievalMode": mode,
                    "scope": scope,
                    "scopeKind": validated_scope.kind,
                    "manifestId": manifest_id,
                    "manifestUrl": redacted_manifest_url,
                    "partitionCount": len(blob_links),
                },
            )

        # Mode: costDetailsApi
        try:
            token = await self._acquire_token(connection, secret_value=secret_value)
            probe_url = (
                f"{profile.arm_endpoint}{scope}/providers/Microsoft.CostManagement/exports"
                f"?api-version={COST_MANAGEMENT_EXPORTS_API_VERSION}"
            )
            response = await self._request(
                "GET",
                probe_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        except DomainError as exc:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="failed",
                message=redact_sensitive_text(exc.message),
                checked_at=now,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={"scope": scope, **exc.details},
            )

        if response.status_code == 200:
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="ready",
                message=(
                    f"Verified Azure Cost Management access on {scope} ({cloud}). Ready to "
                    "generate on-demand AmortizedCost reports."
                ),
                checked_at=now,
                scope_summary=scope,
                has_stored_secret=has_secret,
                supported_source_months=supported_months,
                details={
                    "azureCloud": cloud,
                    "retrievalMode": mode,
                    "scope": scope,
                    "scopeKind": validated_scope.kind,
                    "armEndpoint": profile.arm_endpoint,
                },
            )

        err_code, err_msg = _extract_arm_error(response)
        upstream_summary = f"{err_code}: {err_msg}".strip(": ")

        if response.status_code in (401, 403):
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="failed",
                message=_permission_error_message(scope),
                checked_at=now,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "reason": "missingBillingPermission",
                    "statusCode": response.status_code,
                    "errorCode": err_code or "AuthorizationFailed",
                    "requiredRoles": list(_REQUIRED_BILLING_ROLES),
                    "scope": scope,
                    "azureCloud": cloud,
                },
            )

        if response.status_code in (400, 404, 405, 501):
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="failed",
                message=_unsupported_agreement_message(scope, upstream_summary),
                checked_at=now,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "reason": "unsupportedBillingAgreement",
                    "statusCode": response.status_code,
                    "errorCode": err_code or "UnsupportedAgreementType",
                    "recommendedAlternatives": ["exportManifest", "upload"],
                    "scope": scope,
                    "azureCloud": cloud,
                },
            )

        if response.status_code == 429:
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            return BillingConnectionCheckResult(
                connection_id=connection.id,
                provider="azure",
                status="failed",
                message=(
                    f"Azure Cost Management throttled the access check (HTTP 429). Retry after "
                    f"{retry_after} seconds."
                ),
                checked_at=now,
                scope_summary=scope,
                has_stored_secret=has_secret,
                details={
                    "reason": "throttled",
                    "statusCode": 429,
                    "retryAfter": retry_after,
                    "scope": scope,
                },
            )

        return BillingConnectionCheckResult(
            connection_id=connection.id,
            provider="azure",
            status="failed",
            message=(
                f"Azure Cost Management returned HTTP {response.status_code} while verifying "
                f"{scope}"
                + (f" ({upstream_summary})." if upstream_summary else ".")
            ),
            checked_at=now,
            scope_summary=scope,
            has_stored_secret=has_secret,
            details={
                "reason": "upstreamError",
                "statusCode": response.status_code,
                "errorCode": err_code,
                "scope": scope,
            },
        )

    async def _complete_cost_details_payload(
        self,
        payload: Any,
        *,
        scope: str,
        cloud: AzureCloud,
        source_month: str,
        connection: BillingConnection,
        secret_value: str | None,
        operation_id: str | None,
        operation_location: str | None,
        retry_after: int,
        now: datetime,
    ) -> BillingConnectorStepResult:
        if not isinstance(payload, dict):
            raise UpstreamError(
                "Azure Cost Management returned an invalid non-JSON operation status payload.",
                details={"scope": scope},
            )

        props = payload.get("properties")
        raw_status = str(
            payload.get("status")
            or (props.get("status") if isinstance(props, dict) else "")
            or ""
        ).strip()
        status_folded = raw_status.casefold()
        resolved_op_id = (
            _extract_operation_id(payload, operation_location) or operation_id
        )

        if status_folded in _RUNNING_STATES:
            return BillingConnectorStepResult(
                status="running",
                source_scope=scope,
                operation_id=resolved_op_id,
                operation_location=operation_location,
                poll_after=now + timedelta(seconds=retry_after),
                status_message=(
                    "Waiting for Azure Cost Management to finish generating the "
                    "AmortizedCost report..."
                ),
            )

        if status_folded in _NO_DATA_STATES:
            manifest_id = redact_sensitive_text(
                str(payload.get("name") or resolved_op_id or f"nodata-{source_month}"),
                max_length=120,
            )
            return BillingConnectorStepResult(
                status="completed",
                parts=[],
                source_scope=scope,
                source_manifest_id=manifest_id,
                operation_id=resolved_op_id,
                status_message=(
                    f"Azure Cost Management reported NoDataFound for {scope} in {source_month}."
                ),
                total_read_bytes=0,
                warnings=[
                    f"Azure Cost Management returned NoDataFound for {scope} in {source_month}."
                ],
            )

        if status_folded in _FAILED_STATES:
            err_obj = payload.get("error") or (
                props.get("error") if isinstance(props, dict) else None
            )
            err_code = ""
            err_msg = ""
            if isinstance(err_obj, dict):
                err_code = redact_sensitive_text(str(err_obj.get("code") or ""), max_length=80)
                err_msg = redact_sensitive_text(str(err_obj.get("message") or ""), max_length=300)
            summary = f"{err_code}: {err_msg}".strip(": ")
            raise UpstreamError(
                f"Azure Cost Management report generation failed for {scope}"
                + (f" ({summary})." if summary else "."),
                details={
                    "reason": "reportGenerationFailed",
                    "scope": scope,
                    "operationId": resolved_op_id,
                    "errorCode": err_code,
                },
            )

        if (
            status_folded not in _COMPLETED_STATES
            and "manifest" not in payload
            and "blobs" not in payload
        ):
            raise UpstreamError(
                f"Azure Cost Management returned unrecognized operation status {raw_status!r}.",
                details={
                    "reason": "unexpectedOperationStatus",
                    "status": redact_sensitive_text(raw_status, max_length=80),
                    "scope": scope,
                },
            )

        manifest_id, blob_urls, compress_data = _validate_manifest_structure(
            payload,
            cloud=cloud,
            expected_source_month=source_month,
            max_partitions=self._max_partitions,
            max_total_bytes=self._max_total_bytes,
        )
        parts, total_bytes = await self._download_manifest_partitions(
            blob_urls,
            connection=connection,
            secret_value=secret_value,
            compress_data=compress_data,
        )
        return BillingConnectorStepResult(
            status="completed",
            parts=parts,
            source_scope=scope,
            source_manifest_id=manifest_id,
            operation_id=resolved_op_id,
            status_message=(
                f"Downloaded {len(parts)} AmortizedCost partition(s) ({total_bytes:,} bytes) "
                f"from Azure Cost Management for {source_month}."
            ),
            total_read_bytes=total_bytes,
        )

    async def start_or_poll_acquisition(
        self,
        connection: BillingConnection,
        run: BillingAcquisitionRun,
        *,
        secret_value: str | None = None,
    ) -> BillingConnectorStepResult:
        """Start or poll an Azure billing extraction for ``run.source_month``."""

        if connection.provider != "azure":
            raise ValidationError(
                f"AzureBillingConnector requires an Azure billing connection, got "
                f"{connection.provider!r}."
            )

        now = self._clock()
        today = now.date()
        cloud: AzureCloud = connection.azure_cloud or "commercial"
        profile = cloud_profile(cloud)
        mode = connection.azure_retrieval_mode or "costDetailsApi"
        scope = validate_azure_billing_scope(connection.azure_scope)

        start_date, end_date = _validate_lookback_window(
            run.source_month,
            today=today,
            enforce_cost_details_limit=(mode == "costDetailsApi"),
        )
        # Mode 2: exportManifest
        if mode == "exportManifest":
            manifest_url = validate_azure_export_manifest_url(
                connection.azure_export_manifest_url,
                cloud=cloud,
            )
            redacted_manifest_url = redact_azure_url(manifest_url)
            manifest_scope = _manifest_token_scope(manifest_url, cloud=cloud)
            token = await self._acquire_token(
                connection, secret_value=secret_value, scope=manifest_scope
            )
            response = await self._request(
                "GET",
                manifest_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "x-ms-version": "2023-11-03",
                },
            )
            if response.status_code in (401, 403):
                raise UpstreamAuthorizationError(
                    _permission_error_message(scope, include_blob_role=True),
                    details={
                        "reason": "missingBillingPermission",
                        "statusCode": response.status_code,
                        "requiredRoles": [
                            *_REQUIRED_BILLING_ROLES,
                            "Storage Blob Data Reader",
                        ],
                        "manifestUrl": redacted_manifest_url,
                        "scope": scope,
                    },
                )
            if response.status_code != 200:
                err_code, err_msg = _extract_arm_error(response)
                raise UpstreamError(
                    f"Failed to fetch Azure export manifest at {redacted_manifest_url} "
                    f"(HTTP {response.status_code}"
                    + (f": {err_code} {err_msg}".rstrip() if err_code or err_msg else "")
                    + ").",
                    details={
                        "reason": "manifestFetchFailed",
                        "statusCode": response.status_code,
                        "manifestUrl": redacted_manifest_url,
                        "scope": scope,
                    },
                )
            try:
                manifest_payload = response.json()
            except ValueError as exc:
                raise ValidationError(
                    f"Azure export manifest at {redacted_manifest_url} is not valid JSON.",
                    details={
                        "reason": "invalidManifestJson",
                        "manifestUrl": redacted_manifest_url,
                    },
                ) from exc

            manifest_id, blob_urls, compress_data = _validate_manifest_structure(
                manifest_payload,
                cloud=cloud,
                base_manifest_url=manifest_url,
                expected_source_month=run.source_month,
                max_partitions=self._max_partitions,
                max_total_bytes=self._max_total_bytes,
            )
            parts, total_bytes = await self._download_manifest_partitions(
                blob_urls,
                connection=connection,
                secret_value=secret_value,
                storage_token=token if manifest_scope == AZURE_STORAGE_SCOPE else None,
                compress_data=compress_data,
            )
            return BillingConnectorStepResult(
                status="completed",
                parts=parts,
                source_scope=scope,
                source_manifest_id=manifest_id,
                status_message=(
                    f"Downloaded {len(parts)} partition(s) ({total_bytes:,} bytes) from "
                    f"configured Azure export manifest for {run.source_month}."
                ),
                total_read_bytes=total_bytes,
            )

        # Mode 1: costDetailsApi
        token = await self._acquire_token(connection, secret_value=secret_value)
        if not run.operation_location:
            generate_url = (
                f"{profile.arm_endpoint}{scope}/providers/Microsoft.CostManagement"
                f"/generateCostDetailsReport?api-version={COST_DETAILS_API_VERSION}"
            )
            request_body = {
                "metric": "AmortizedCost",
                "timePeriod": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                },
            }
            response = await self._request(
                "POST",
                generate_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                },
                json_body=request_body,
            )

            if response.status_code == 202:
                raw_location = (
                    response.headers.get("Location")
                    or response.headers.get("Azure-AsyncOperation")
                    or ""
                ).strip()
                if not raw_location:
                    raise UpstreamError(
                        "Azure Cost Management returned 202 Accepted without a Location or "
                        "Azure-AsyncOperation polling URL.",
                        details={"reason": "missingOperationLocation", "scope": scope},
                    )
                validated_location = validate_azure_arm_url(raw_location, cloud=cloud)
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                try:
                    initial_body = response.json()
                except ValueError:
                    initial_body = None
                operation_id = (
                    _extract_operation_id(initial_body, validated_location)
                    or run.operation_id
                )
                return BillingConnectorStepResult(
                    status="running",
                    source_scope=scope,
                    operation_id=operation_id,
                    operation_location=validated_location,
                    poll_after=now + timedelta(seconds=retry_after),
                    status_message=(
                        "Waiting for Azure Cost Management to generate the AmortizedCost report..."
                    ),
                )

            if response.status_code == 200:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                try:
                    sync_body = response.json()
                except ValueError as exc:
                    raise UpstreamError(
                        "Azure Cost Management returned 200 OK with non-JSON body.",
                        details={"scope": scope},
                    ) from exc
                return await self._complete_cost_details_payload(
                    sync_body,
                    scope=scope,
                    cloud=cloud,
                    source_month=run.source_month,
                    connection=connection,
                    secret_value=secret_value,
                    operation_id=run.operation_id,
                    operation_location=None,
                    retry_after=retry_after,
                    now=now,
                )

            err_code, err_msg = _extract_arm_error(response)
            upstream_summary = f"{err_code}: {err_msg}".strip(": ")

            if response.status_code in (401, 403):
                raise UpstreamAuthorizationError(
                    _permission_error_message(scope),
                    details={
                        "reason": "missingBillingPermission",
                        "statusCode": response.status_code,
                        "errorCode": err_code or "AuthorizationFailed",
                        "requiredRoles": list(_REQUIRED_BILLING_ROLES),
                        "scope": scope,
                    },
                )
            if response.status_code in (400, 404, 405, 501):
                raise ValidationError(
                    _unsupported_agreement_message(scope, upstream_summary),
                    details={
                        "reason": "unsupportedBillingAgreement",
                        "statusCode": response.status_code,
                        "errorCode": err_code or "UnsupportedAgreementType",
                        "recommendedAlternatives": ["exportManifest", "upload"],
                        "scope": scope,
                    },
                )
            if response.status_code == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                raise UpstreamError(
                    f"Azure Cost Management throttled report generation (HTTP 429); retry after "
                    f"{retry_after} seconds.",
                    details={
                        "reason": "throttled",
                        "statusCode": 429,
                        "retryAfter": retry_after,
                        "scope": scope,
                    },
                )
            raise UpstreamError(
                f"Azure Cost Management rejected Cost Details report generation "
                f"(HTTP {response.status_code}"
                + (f": {upstream_summary}" if upstream_summary else "")
                + ").",
                details={
                    "reason": "upstreamError",
                    "statusCode": response.status_code,
                    "errorCode": err_code,
                    "scope": scope,
                },
            )

        # Polling an existing operation_location
        poll_url = validate_azure_arm_url(run.operation_location, cloud=cloud)
        response = await self._request(
            "GET",
            poll_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
        )
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        next_location_raw = (
            response.headers.get("Location")
            or response.headers.get("Azure-AsyncOperation")
            or ""
        ).strip()
        next_location = (
            validate_azure_arm_url(next_location_raw, cloud=cloud)
            if next_location_raw
            else poll_url
        )

        if response.status_code == 202:
            try:
                poll_body = response.json()
            except ValueError:
                poll_body = None
            op_id = _extract_operation_id(poll_body, next_location) or run.operation_id
            if isinstance(poll_body, dict):
                status_val = str(
                    poll_body.get("status")
                    or (
                        poll_body.get("properties", {}).get("status")
                        if isinstance(poll_body.get("properties"), dict)
                        else ""
                    )
                    or ""
                ).strip()
                if status_val.casefold() in _FAILED_STATES:
                    return await self._complete_cost_details_payload(
                        poll_body,
                        scope=scope,
                        cloud=cloud,
                        source_month=run.source_month,
                        connection=connection,
                        secret_value=secret_value,
                        operation_id=op_id,
                        operation_location=next_location,
                        retry_after=retry_after,
                        now=now,
                    )
            return BillingConnectorStepResult(
                status="running",
                source_scope=scope,
                operation_id=op_id,
                operation_location=next_location,
                poll_after=now + timedelta(seconds=retry_after),
                status_message=(
                    "Waiting for Azure Cost Management to finish generating the "
                    "AmortizedCost report..."
                ),
            )

        if response.status_code == 200:
            try:
                completed_body = response.json()
            except ValueError as exc:
                raise UpstreamError(
                    "Azure Cost Management operation poll returned non-JSON response body.",
                    details={"scope": scope},
                ) from exc
            return await self._complete_cost_details_payload(
                completed_body,
                scope=scope,
                cloud=cloud,
                source_month=run.source_month,
                connection=connection,
                secret_value=secret_value,
                operation_id=run.operation_id,
                operation_location=next_location,
                retry_after=retry_after,
                now=now,
            )

        if response.status_code in (401, 403):
            raise UpstreamAuthorizationError(
                _permission_error_message(scope),
                details={
                    "reason": "missingBillingPermission",
                    "statusCode": response.status_code,
                    "requiredRoles": list(_REQUIRED_BILLING_ROLES),
                    "scope": scope,
                },
            )
        if response.status_code == 429:
            raise UpstreamError(
                f"Azure Cost Management throttled report polling (HTTP 429); retry after "
                f"{retry_after} seconds.",
                details={
                    "reason": "throttled",
                    "statusCode": 429,
                    "retryAfter": retry_after,
                    "scope": scope,
                },
            )

        err_code, err_msg = _extract_arm_error(response)
        upstream_summary = f"{err_code}: {err_msg}".strip(": ")
        raise UpstreamError(
            f"Azure Cost Management operation poll failed with HTTP {response.status_code}"
            + (f" ({upstream_summary})." if upstream_summary else "."),
            details={
                "reason": "pollFailed",
                "statusCode": response.status_code,
                "errorCode": err_code,
                "scope": scope,
            },
        )


__all__ = [
    "COST_DETAILS_API_VERSION",
    "COST_MANAGEMENT_EXPORTS_API_VERSION",
    "DEFAULT_MAX_PARTITIONS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "MAX_COST_DETAILS_LOOKBACK_MONTHS",
    "AzureBillingConnector",
    "AzureBillingScopeKind",
    "AzureBillingTokenProvider",
    "ValidatedAzureBillingScope",
    "parse_azure_billing_scope",
    "redact_azure_url",
    "redact_sensitive_text",
    "validate_azure_arm_url",
    "validate_azure_billing_scope",
    "validate_azure_blob_url",
    "validate_azure_export_manifest_url",
]
