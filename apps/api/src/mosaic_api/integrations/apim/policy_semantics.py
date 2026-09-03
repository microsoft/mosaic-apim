"""Turn API Management policy XML into plain-language facets.

This module is the only place in MOSAIC that sees raw policy XML. It takes markup in and returns a
digest plus redacted semantic facets; callers persist and render the result. Two invariants hold:

* No raw markup is ever returned. Callers cannot accidentally leak it because they never receive it.
* Attribute values that can carry secrets are redacted before they leave the parser. Policy
  documents routinely contain inline keys in ``set-header`` and backend credentials.

Elements MOSAIC does not recognise are reported by name and counted, never hidden. Claiming to
govern configuration we cannot read would be worse than admitting the gap.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from mosaic_api.observed import (
    FacetConfidence,
    PolicyFacet,
    PolicyFacetKind,
    PolicySection,
)

MOSAIC_FRAGMENT_PREFIX = "mosaic-"
REDACTED = "[redacted]"
MAX_ATTRIBUTE_LENGTH = 200

SECTION_ELEMENTS: dict[str, PolicySection] = {
    "inbound": PolicySection.INBOUND,
    "backend": PolicySection.BACKEND,
    "outbound": PolicySection.OUTBOUND,
    "on-error": PolicySection.ON_ERROR,
}
CONTAINER_ELEMENTS = frozenset({"choose", "when", "otherwise", "try", "catch"})
IGNORED_ELEMENTS = frozenset({"base"})

_SECRET_ATTRIBUTE_HINTS = (
    "key",
    "secret",
    "password",
    "credential",
    "token-value",
    "thumbprint",
    "certificate",
    "connection-string",
)
_SENSITIVE_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "api-key",
        "ocp-apim-subscription-key",
        "x-api-key",
        "cookie",
        "set-cookie",
    }
)
_NAMED_VALUE_PATTERN = re.compile(r"^\{\{(?P<name>[^}]+)\}\}$")
_CLAIM_PATTERN = re.compile(
    r"Claims\s*(?:\.|\[)\s*(?:GetValueOrDefault\s*\(\s*)?[\"'](?P<claim>[^\"']+)[\"']"
)
_HEADER_PATTERN = re.compile(
    r"Headers\s*(?:\.|\[)\s*(?:GetValueOrDefault\s*\(\s*)?[\"'](?P<header>[^\"']+)[\"']"
)
_QUOTA_PERIOD_LABELS = {
    "hourly": "hour",
    "daily": "day",
    "weekly": "week",
    "monthly": "month",
    "yearly": "year",
}


@dataclass
class PolicyAnalysis:
    content_sha256: str
    element_count: int = 0
    facets: list[PolicyFacet] = field(default_factory=list)
    unrecognized_elements: list[str] = field(default_factory=list)
    references_mosaic_fragment: bool = False


def content_digest(xml: str) -> str:
    return hashlib.sha256(xml.encode("utf-8")).hexdigest()


def sanitize_url(url: str | None) -> str | None:
    """Drop query and fragment from a URL before it is stored or displayed.

    Backend URLs routinely carry credentials as query parameters: Azure Functions keys (``?code=``)
    and storage SAS tokens (``?sig=``) are the common cases. The host and path are what an operator
    needs to recognise a backend; the secret part is not.
    """

    if not url:
        return url
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return REDACTED
    if not parts.query and not parts.fragment:
        return url.strip()
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", "")) + " (parameters hidden)"


def _truncate(value: str) -> str:
    collapsed = " ".join(value.split())
    if len(collapsed) <= MAX_ATTRIBUTE_LENGTH:
        return collapsed
    return f"{collapsed[:MAX_ATTRIBUTE_LENGTH]}…"


def _redact(name: str, value: str) -> str:
    lowered = name.casefold()
    if any(hint in lowered for hint in _SECRET_ATTRIBUTE_HINTS):
        return REDACTED
    named_value = _NAMED_VALUE_PATTERN.match(value.strip())
    if named_value:
        return f"named value: {named_value.group('name')}"
    return _truncate(value)


def _attributes(element: ET.Element, *names: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in names:
        raw = element.get(name)
        if raw is not None:
            result[name] = _redact(name, raw)
    return result


def _int_attribute(element: ET.Element, name: str) -> int | None:
    raw = element.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _describe_period(seconds: int | None) -> str:
    if seconds is None:
        return "the configured period"
    mapping = {1: "second", 60: "minute", 3600: "hour", 86400: "day"}
    if seconds in mapping:
        return mapping[seconds]
    if seconds % 86400 == 0:
        return f"{seconds // 86400} days"
    if seconds % 3600 == 0:
        return f"{seconds // 3600} hours"
    if seconds % 60 == 0:
        return f"{seconds // 60} minutes"
    return f"{seconds} seconds"


def describe_counter_key(expression: str | None) -> str:
    """Translate an APIM counter-key expression into a plain-language scope."""

    if not expression:
        return "per subscription"
    text = expression.strip()
    if "@" not in text:
        return "shared across all callers"
    claim = _CLAIM_PATTERN.search(text)
    if claim:
        name = claim.group("claim")
        friendly = {
            "oid": "per Entra user",
            "sub": "per token subject",
            "appid": "per calling application",
            "azp": "per calling application",
            "tid": "per Entra tenant",
        }.get(name)
        return friendly or f"per token claim {name}"
    header = _HEADER_PATTERN.search(text)
    if header:
        return f"per {header.group('header')} request header"
    lowered = text.casefold()
    if "subscription" in lowered:
        return "per subscription"
    if "ipaddress" in lowered:
        return "per caller IP address"
    if "context.user" in lowered:
        return "per gateway user"
    if "context.product" in lowered:
        return "per product"
    if "context.operation" in lowered:
        return "per operation"
    if "context.api" in lowered:
        return "per API"
    return "per custom expression"


def _plural(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _child_texts(element: ET.Element, path: str) -> list[str]:
    return [
        " ".join(child.text.split())
        for child in element.findall(path)
        if child.text and child.text.strip()
    ]


def _rate_limit(element: ET.Element, *, keyed: bool) -> PolicyFacet:
    calls = _int_attribute(element, "calls")
    period = _describe_period(_int_attribute(element, "renewal-period"))
    scope = describe_counter_key(element.get("counter-key")) if keyed else "per subscription"
    call_text = _plural(calls, "call") if calls is not None else "a configured number of calls"
    attributes = _attributes(element, "calls", "renewal-period", "retry-after-header-name")
    details: list[str] = []
    if element.get("increment-condition"):
        details.append("Only some requests count toward the limit.")
    return PolicyFacet(
        kind=PolicyFacetKind.RATE_LIMIT,
        element=element.tag,
        summary=f"Allows {call_text} per {period}, counted {scope}.",
        details=details,
        attributes=attributes,
    )


def _quota(element: ET.Element, *, keyed: bool) -> PolicyFacet:
    calls = _int_attribute(element, "calls")
    bandwidth = _int_attribute(element, "bandwidth")
    period = _describe_period(_int_attribute(element, "renewal-period"))
    scope = describe_counter_key(element.get("counter-key")) if keyed else "per subscription"
    allowances: list[str] = []
    if calls is not None:
        allowances.append(_plural(calls, "call"))
    if bandwidth is not None:
        allowances.append(f"{bandwidth} KB of bandwidth")
    allowance = " and ".join(allowances) if allowances else "a configured allowance"
    return PolicyFacet(
        kind=PolicyFacetKind.QUOTA,
        element=element.tag,
        summary=f"Caps usage at {allowance} per {period}, counted {scope}.",
        attributes=_attributes(element, "calls", "bandwidth", "renewal-period"),
    )


def _token_limit(element: ET.Element) -> PolicyFacet:
    tokens_per_minute = _int_attribute(element, "tokens-per-minute")
    quota = _int_attribute(element, "token-quota")
    quota_period = element.get("token-quota-period")
    scope = describe_counter_key(element.get("counter-key"))
    clauses: list[str] = []
    if tokens_per_minute is not None:
        clauses.append(f"{tokens_per_minute:,} tokens per minute")
    if quota is not None:
        label = _QUOTA_PERIOD_LABELS.get((quota_period or "").casefold())
        period = f" per {label}" if label else ""
        clauses.append(f"{quota:,} tokens{period}")
    allowance = " and ".join(clauses) if clauses else "a configured token allowance"
    details: list[str] = []
    if element.get("estimate-prompt-tokens", "").casefold() == "true":
        details.append("Prompt tokens are estimated before the request reaches the model.")
    else:
        details.append("Limits are applied using token counts reported by the model.")
    return PolicyFacet(
        kind=PolicyFacetKind.TOKEN_LIMIT,
        element=element.tag,
        summary=f"Limits model usage to {allowance}, counted {scope}.",
        details=details,
        attributes=_attributes(
            element,
            "tokens-per-minute",
            "token-quota",
            "token-quota-period",
            "estimate-prompt-tokens",
        ),
    )


def _emit_token_metric(element: ET.Element) -> PolicyFacet:
    dimensions = [
        name
        for child in element.findall("dimension")
        if (name := child.get("name")) is not None
    ]
    details = (
        [f"Broken down by {', '.join(dimensions)}."] if dimensions else []
    )
    return PolicyFacet(
        kind=PolicyFacetKind.OBSERVABILITY,
        element=element.tag,
        summary="Reports model token consumption to Application Insights.",
        details=details,
    )


def _managed_identity(element: ET.Element) -> PolicyFacet:
    resource = element.get("resource") or "the backend service"
    details: list[str] = []
    if element.get("client-id"):
        details.append("Uses a specific user-assigned managed identity.")
    return PolicyFacet(
        kind=PolicyFacetKind.AUTHENTICATION,
        element=element.tag,
        summary=f"The gateway authenticates to {resource} with its own managed identity.",
        details=details,
        attributes=_attributes(element, "resource", "output-token-variable-name"),
    )


def _validate_jwt(element: ET.Element) -> PolicyFacet:
    audiences = _child_texts(element, "audiences/audience")
    issuers = _child_texts(element, "issuers/issuer")
    claims = [
        name
        for child in element.findall("required-claims/claim")
        if (name := child.get("name")) is not None
    ]
    details: list[str] = []
    if issuers:
        details.append(f"Accepted from {_plural(len(issuers), 'issuer')}.")
    if audiences:
        details.append(f"Must target {_plural(len(audiences), 'audience')}.")
    if claims:
        details.append(f"Requires claims: {', '.join(claims)}.")
    return PolicyFacet(
        kind=PolicyFacetKind.AUTHORIZATION,
        element=element.tag,
        summary="Callers must present a valid signed token.",
        details=details,
    )


def _validate_entra_token(element: ET.Element) -> PolicyFacet:
    tenant = element.get("tenant-id")
    applications = _child_texts(element, "client-application-ids/application-id")
    details: list[str] = []
    if applications:
        details.append(f"Restricted to {_plural(len(applications), 'client application')}.")
    audiences = _child_texts(element, "audiences/audience")
    if audiences:
        details.append(f"Must target {_plural(len(audiences), 'audience')}.")
    tenant_text = f" from tenant {tenant}" if tenant else ""
    return PolicyFacet(
        kind=PolicyFacetKind.AUTHORIZATION,
        element=element.tag,
        summary=f"Callers must present a valid Microsoft Entra token{tenant_text}.",
        details=details,
    )


def _set_backend(element: ET.Element) -> PolicyFacet:
    backend_id = element.get("backend-id")
    base_url = sanitize_url(element.get("base-url"))
    if backend_id:
        summary = f"Routes requests to the {backend_id} backend."
    elif base_url:
        summary = f"Routes requests to {_truncate(base_url)}."
    else:
        summary = "Overrides the backend the request is sent to."
    attributes: dict[str, str] = {}
    if backend_id:
        attributes["backend-id"] = _truncate(backend_id)
    if base_url:
        attributes["base-url"] = _truncate(base_url)
    return PolicyFacet(
        kind=PolicyFacetKind.ROUTING,
        element=element.tag,
        summary=summary,
        attributes=attributes,
    )


def _semantic_cache_lookup(element: ET.Element) -> PolicyFacet:
    threshold = element.get("score-threshold")
    details = (
        [f"Similarity threshold {threshold}."] if threshold else []
    )
    return PolicyFacet(
        kind=PolicyFacetKind.CACHING,
        element=element.tag,
        summary="Serves semantically similar prompts from cache instead of calling the model.",
        details=details,
        attributes=_attributes(element, "score-threshold", "max-message-count"),
    )


def _semantic_cache_store(element: ET.Element) -> PolicyFacet:
    duration = _int_attribute(element, "duration")
    details = (
        [f"Cached responses expire after {_describe_period(duration)}."] if duration else []
    )
    return PolicyFacet(
        kind=PolicyFacetKind.CACHING,
        element=element.tag,
        summary="Stores model responses so similar prompts can be served from cache.",
        details=details,
        attributes=_attributes(element, "duration"),
    )


def _cache_lookup(element: ET.Element) -> PolicyFacet:
    return PolicyFacet(
        kind=PolicyFacetKind.CACHING,
        element=element.tag,
        summary="Serves matching responses from the gateway cache.",
        attributes=_attributes(element, "downstream-caching-type", "vary-by-developer"),
    )


def _cache_store(element: ET.Element) -> PolicyFacet:
    duration = _int_attribute(element, "duration")
    details = (
        [f"Cached responses expire after {_describe_period(duration)}."] if duration else []
    )
    return PolicyFacet(
        kind=PolicyFacetKind.CACHING,
        element=element.tag,
        summary="Stores responses in the gateway cache.",
        details=details,
    )


def _content_safety(element: ET.Element) -> PolicyFacet:
    categories = [
        name
        for child in element.findall("categories/category")
        if (name := child.get("name")) is not None
    ]
    details = (
        [f"Screens for {', '.join(categories)}."] if categories else []
    )
    return PolicyFacet(
        kind=PolicyFacetKind.CONTENT_SAFETY,
        element=element.tag,
        summary="Screens prompts with Azure AI Content Safety before they reach the model.",
        details=details,
    )


def _include_fragment(element: ET.Element) -> PolicyFacet:
    fragment_id = element.get("fragment-id") or "an unnamed fragment"
    managed = fragment_id.casefold().startswith(MOSAIC_FRAGMENT_PREFIX)
    summary = (
        f"Applies the MOSAIC-managed {fragment_id} rule set."
        if managed
        else f"Applies the shared {fragment_id} rule set."
    )
    return PolicyFacet(
        kind=PolicyFacetKind.FRAGMENT_INCLUDE,
        element=element.tag,
        summary=summary,
        attributes={"fragment-id": fragment_id},
        managed_by_mosaic=managed,
    )


def _ip_filter(element: ET.Element) -> PolicyFacet:
    action = (element.get("action") or "filter").casefold()
    entries = len(element.findall("address")) + len(element.findall("address-range"))
    verb = "Allows" if action == "allow" else "Blocks"
    return PolicyFacet(
        kind=PolicyFacetKind.NETWORK,
        element=element.tag,
        summary=f"{verb} traffic from {_plural(entries, 'configured address range')}.",
    )


def _cors(element: ET.Element) -> PolicyFacet:
    origins = _child_texts(element, "allowed-origins/origin")
    details = (
        [f"Allows {_plural(len(origins), 'origin')}."] if origins else []
    )
    return PolicyFacet(
        kind=PolicyFacetKind.NETWORK,
        element=element.tag,
        summary="Allows browser requests from configured origins.",
        details=details,
    )


def _set_header(element: ET.Element) -> PolicyFacet:
    name = element.get("name") or "a header"
    action = (element.get("exists-action") or "override").casefold()
    sensitive = name.casefold() in _SENSITIVE_HEADERS
    detail = (
        ["The value is a credential and is not shown."]
        if sensitive
        else []
    )
    verb = "Removes" if action == "delete" else "Sets"
    return PolicyFacet(
        kind=PolicyFacetKind.TRANSFORMATION,
        element=element.tag,
        summary=f"{verb} the {name} request header.",
        details=detail,
        attributes={"name": name, "exists-action": action},
    )


def _set_query_parameter(element: ET.Element) -> PolicyFacet:
    name = element.get("name") or "a query parameter"
    return PolicyFacet(
        kind=PolicyFacetKind.TRANSFORMATION,
        element=element.tag,
        summary=f"Sets the {name} query parameter.",
        attributes={"name": name},
    )


def _rewrite_uri(element: ET.Element) -> PolicyFacet:
    return PolicyFacet(
        kind=PolicyFacetKind.TRANSFORMATION,
        element=element.tag,
        summary="Rewrites the request path before it reaches the backend.",
    )


def _set_variable(element: ET.Element) -> PolicyFacet:
    name = element.get("name") or "a variable"
    return PolicyFacet(
        kind=PolicyFacetKind.TRANSFORMATION,
        element=element.tag,
        summary=f"Computes the {name} value for use later in the request.",
        confidence=FacetConfidence.PARTIAL,
        attributes={"name": name},
    )


def _emit_metric(element: ET.Element) -> PolicyFacet:
    name = element.get("name") or "a custom metric"
    return PolicyFacet(
        kind=PolicyFacetKind.OBSERVABILITY,
        element=element.tag,
        summary=f"Emits the {name} metric to Application Insights.",
    )


def _log_to_eventhub(element: ET.Element) -> PolicyFacet:
    return PolicyFacet(
        kind=PolicyFacetKind.OBSERVABILITY,
        element=element.tag,
        summary="Sends request records to an Event Hub.",
        attributes=_attributes(element, "logger-id"),
    )


def _trace(element: ET.Element) -> PolicyFacet:
    return PolicyFacet(
        kind=PolicyFacetKind.OBSERVABILITY,
        element=element.tag,
        summary="Adds a diagnostic trace entry.",
    )


def _forward_request(element: ET.Element) -> PolicyFacet:
    timeout = element.get("timeout")
    details = [f"Times out after {timeout} seconds."] if timeout else []
    return PolicyFacet(
        kind=PolicyFacetKind.ROUTING,
        element=element.tag,
        summary="Forwards the request to the backend.",
        details=details,
    )


def _return_response(element: ET.Element) -> PolicyFacet:
    return PolicyFacet(
        kind=PolicyFacetKind.ROUTING,
        element=element.tag,
        summary="Returns a response directly from the gateway without calling the backend.",
    )


_RECOGNIZERS: dict[str, Callable[[ET.Element], PolicyFacet]] = {
    "rate-limit": lambda element: _rate_limit(element, keyed=False),
    "rate-limit-by-key": lambda element: _rate_limit(element, keyed=True),
    "quota": lambda element: _quota(element, keyed=False),
    "quota-by-key": lambda element: _quota(element, keyed=True),
    "llm-token-limit": _token_limit,
    "azure-openai-token-limit": _token_limit,
    "llm-emit-token-metric": _emit_token_metric,
    "azure-openai-emit-token-metric": _emit_token_metric,
    "authentication-managed-identity": _managed_identity,
    "validate-jwt": _validate_jwt,
    "validate-azure-ad-token": _validate_entra_token,
    "set-backend-service": _set_backend,
    "llm-semantic-cache-lookup": _semantic_cache_lookup,
    "azure-openai-semantic-cache-lookup": _semantic_cache_lookup,
    "llm-semantic-cache-store": _semantic_cache_store,
    "azure-openai-semantic-cache-store": _semantic_cache_store,
    "cache-lookup": _cache_lookup,
    "cache-store": _cache_store,
    "llm-content-safety": _content_safety,
    "include-fragment": _include_fragment,
    "ip-filter": _ip_filter,
    "cors": _cors,
    "set-header": _set_header,
    "set-query-parameter": _set_query_parameter,
    "rewrite-uri": _rewrite_uri,
    "set-variable": _set_variable,
    "emit-metric": _emit_metric,
    "log-to-eventhub": _log_to_eventhub,
    "trace": _trace,
    "forward-request": _forward_request,
    "return-response": _return_response,
}

RECOGNIZED_ELEMENTS: frozenset[str] = frozenset(_RECOGNIZERS)


def _unrecognized(element: ET.Element) -> PolicyFacet:
    return PolicyFacet(
        kind=PolicyFacetKind.UNRECOGNIZED,
        element=element.tag,
        summary=(
            f"MOSAIC does not interpret the {element.tag} rule; it was authored outside MOSAIC."
        ),
        confidence=FacetConfidence.UNRECOGNIZED,
    )


def _walk(
    element: ET.Element,
    section: PolicySection,
    analysis: PolicyAnalysis,
    *,
    conditional: bool,
) -> None:
    for child in element:
        if not isinstance(child.tag, str):
            continue
        tag = child.tag
        if tag in IGNORED_ELEMENTS:
            continue
        if tag in SECTION_ELEMENTS:
            _walk(child, SECTION_ELEMENTS[tag], analysis, conditional=conditional)
            continue
        if tag in CONTAINER_ELEMENTS:
            _walk(child, section, analysis, conditional=tag in {"when", "otherwise", "choose"})
            continue
        analysis.element_count += 1
        recognizer = _RECOGNIZERS.get(tag)
        if recognizer is None:
            facet = _unrecognized(child)
            analysis.unrecognized_elements.append(tag)
        else:
            facet = recognizer(child)
        facet.section = section
        if conditional:
            facet.details = [*facet.details, "Applied only when a condition matches."]
        if facet.managed_by_mosaic:
            analysis.references_mosaic_fragment = True
        analysis.facets.append(facet)


def analyze_policy(xml: str) -> PolicyAnalysis:
    """Reduce a policy document to a digest and plain-language facets."""

    analysis = PolicyAnalysis(content_sha256=content_digest(xml))
    stripped = xml.strip()
    if not stripped:
        return analysis
    try:
        root = ET.fromstring(stripped)
    except ET.ParseError:
        analysis.facets.append(
            PolicyFacet(
                kind=PolicyFacetKind.UNRECOGNIZED,
                element="policies",
                summary="MOSAIC could not read this policy document.",
                confidence=FacetConfidence.UNRECOGNIZED,
            )
        )
        analysis.unrecognized_elements.append("unparseable")
        return analysis
    if root.tag in SECTION_ELEMENTS:
        _walk(root, SECTION_ELEMENTS[root.tag], analysis, conditional=False)
    else:
        _walk(root, PolicySection.UNKNOWN, analysis, conditional=False)
    return analysis


def summarize_facets(facets: Iterable[PolicyFacet]) -> tuple[int, int, int]:
    """Return (recognized, unrecognized, mosaic-managed) counts."""

    recognized = 0
    unrecognized = 0
    managed = 0
    for facet in facets:
        if facet.confidence == FacetConfidence.UNRECOGNIZED:
            unrecognized += 1
        else:
            recognized += 1
        if facet.managed_by_mosaic:
            managed += 1
    return recognized, unrecognized, managed
