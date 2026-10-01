"""Compile governed model access into an APIM fragment, never a per-request control-plane call.

Only native APIM subscription validation and a validated Entra JWT establish a grant. The
compiler deliberately replaces legacy subscription counters with credential-independent grant
counters. APIM's distributed limits are safeguards, not an exact billing ledger.

Only curated operations APIM can meter are forwarded: chat-completions and responses, or the
Anthropic Messages operation. Routes without a deployment in their path require a matching request
model. Request quotas use UTC calendar keys; weeks start on Monday. A snapshot without publication
token enforcement renders no token policies at all, because its API shape can't be token-metered on
its gateway's tier (ADR 0012).
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Protocol

from mosaic_api.domain import (
    COST_CENTER_CODE_PATTERN,
    COST_CENTER_HEADER,
    ApiShape,
    AppliedCostCenterPool,
    EntitlementEnforcement,
    EntitlementSubject,
    EntitlementSubjectKind,
    ModelAccessGrant,
    ModelAccessSnapshot,
    PolicyFacet,
    PolicyFacetKind,
    PolicySection,
    Publication,
    QuotaPeriod,
    grant_precedence_key,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.apim.model_apis import OperationSpec, operations_for
from mosaic_api.integrations.apim.policy_semantics import analyze_policy
from mosaic_api.integrations.backend_keys import (
    describe_backend_key,
    is_backend_key_facet,
    named_value_reference,
    set_backend_key,
    strip_caller_credentials,
)
from mosaic_api.integrations.policy import (
    METRIC_NAMESPACE,
    PublicationPolicy,
    _serialize,
    _token_limit_attributes,
    add_shape_headers,
    managed_identity_resource,
    shape_removed_headers,
)

MAX_FRAGMENT_BYTES = 512 * 1024
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SUPPORTED_OPERATIONS = frozenset({"chat-completions", "responses"})
# Token counting is left out: it isn't a model call, and nothing meters or needs it at runtime.
_ANTHROPIC_OPERATIONS = frozenset({"messages"})
_COUNTER_PREFIX = "mosaic:governed:"
_SUBSCRIPTION_COUNTERS = frozenset(
    {
        "@(context.Subscription.Id)",
        "@(context.Subscription?.Id)",
        "@(context.Subscription.Key)",
        "@(context.Subscription?.Key)",
    }
)
_GRANT = '(string)context.Variables["mosaic-grant"]'
_HAS_KEY = '(bool)context.Variables["mosaic-has-key"]'
_HAS_TOKEN = '(bool)context.Variables["mosaic-has-token"]'
_KEY_GRANT = '(string)context.Variables["mosaic-key-grant"]'
_MEMBER = '(string)context.Variables["mosaic-member"]'
_TOKEN_GRANT = '(string)context.Variables["mosaic-token-grant"]'
# The cost-center header's code, lowercased: empty when the call names none, "!" when the header
# is malformed. A key's own cost center fills it in when the header is absent, so a token presented
# with the key resolves to the key's grant. See ADR 0021.
_COST_CENTER_HEADER = '(string)context.Variables["mosaic-cost-center-header"]'
_SELECTED_COST_CENTER = '(string)context.Variables["mosaic-cc"]'
_KEY_COST_CENTER = '(string)context.Variables["mosaic-key-cost-center"]'
_TOKEN_COST_CENTER = '(string)context.Variables["mosaic-token-cost-center"]'
# The matched grant's cost center code, lowercased, set once the grant is resolved. Pooled quotas
# read it, and so can later checks, such as one refusing a blocked cost center.
_GRANT_COST_CENTER = '(string)context.Variables["mosaic-cost-center"]'
# A key lookup's answer for a key whose grant's cost center turned keys off.
_KEYS_OFF = "-"
# The validated token's oid and azp, lowercased. Empty for a key-only call and until the token
# validates, so nothing unvalidated is ever recorded.
_CALLER = '(string)context.Variables["mosaic-caller"]'
_CLIENT = '(string)context.Variables["mosaic-client"]'
# Readers split the message on spaces into key=value pairs and ignore keys they don't know.
# Only a change that breaks those readers bumps the version.
GRANT_ATTRIBUTION_TRACE_PREFIX = "mosaic-attribution v=1"
DENIAL_TRACE_PREFIX = "mosaic-deny v=1"
_DENIAL_REASON = re.compile(r"[a-z][a-z-]{1,31}")
_DENIED = "Model access denied."
_GROUPS_OVERAGE_DENIED = (
    "Model access denied. Your token doesn't list your groups because you belong to too many; "
    "ask an administrator for a direct grant."
)
COST_CENTER_DENIED = (
    "Access denied. You hold no grant under the cost center the x-mosaic-cost-center header "
    "names, or the header isn't a cost center code."
)
COST_CENTER_MISMATCH_DENIED = (
    "Access denied. This key belongs to a grant under a different cost center than the "
    "x-mosaic-cost-center header names."
)
COST_CENTER_KEYS_OFF_DENIED = (
    "Model access denied. Keys are turned off for this grant's cost center; use a Microsoft Entra "
    "token."
)
# Response headers the gateway adds from the limits that applied to a call.
REMAINING_TOKENS_HEADER = "x-mosaic-remaining-tokens"
REMAINING_QUOTA_TOKENS_HEADER = "x-mosaic-remaining-quota-tokens"
REMAINING_CALLS_HEADER = "x-mosaic-remaining-calls"
COST_CENTER_REMAINING_QUOTA_TOKENS_HEADER = "x-mosaic-cost-center-remaining-quota-tokens"
_PERIOD_LABELS: dict[QuotaPeriod, str] = {
    "Hourly": "hour",
    "Daily": "day",
    "Weekly": "week (starting Monday)",
    "Monthly": "month",
    "Yearly": "year",
}


class AccessPolicyPublication(Protocol):
    tenant_id: str
    id: str


class AccessPolicyGrant(Protocol):
    entitlement_id: str
    subject: EntitlementSubject
    object_id: str
    enabled: bool
    enforcement: EntitlementEnforcement | None
    cost_center_id: str
    cost_center_code: str
    default_cost_center: bool
    granted_at: datetime | None

    @property
    def is_group_grant(self) -> bool: ...


def _literal(value: str) -> str:
    # C# and JSON share these string escapes. Escape braces as well so APIM cannot expand a
    # named-value reference inside an otherwise correctly quoted C# string.
    return json.dumps(value, ensure_ascii=True).replace("{", r"\u007b").replace("}", r"\u007d")


def _expression(lines: list[str]) -> str:
    # APIM parses multi-statement expressions with Razor, which refuses the whole policy unless
    # every if, else and loop body is a braced block, even a single return.
    return "@{\n" + "\n".join(lines) + "\n}"


def _variable(parent: ET.Element, name: str, value: str) -> None:
    ET.SubElement(parent, "set-variable", {"name": name, "value": value})


def append_denial_trace(parent: ET.Element, reason: str, *, with_caller: bool = False) -> None:
    """Record why MOSAIC refused a call, just before the refusal.

    The reason is a fixed code, never request content. ``with_caller`` adds the validated
    token's oid and azp, which are empty for a key-only call; use it only where those variables
    have been initialized.
    """

    if not _DENIAL_REASON.fullmatch(reason):
        raise ValueError(f"Invalid denial reason code: {reason!r}")
    trace = ET.SubElement(parent, "trace", {"source": "mosaic", "severity": "information"})
    ET.SubElement(trace, "message").text = (
        f'@("{DENIAL_TRACE_PREFIX} r={reason} o=" + {_CALLER} + " a=" + {_CLIENT})'
        if with_caller
        else f"{DENIAL_TRACE_PREFIX} r={reason}"
    )


def _deny(
    parent: ET.Element,
    *,
    reason: str,
    with_caller: bool = False,
    code: int = 403,
    message: str = _DENIED,
) -> None:
    append_denial_trace(parent, reason, with_caller=with_caller)
    response = ET.SubElement(parent, "return-response")
    ET.SubElement(
        response,
        "set-status",
        {"code": str(code), "reason": "Unauthorized" if code == 401 else "Forbidden"},
    )
    ET.SubElement(response, "set-body").text = message


def _reject(
    parent: ET.Element,
    condition: str,
    *,
    reason: str,
    with_caller: bool = False,
    code: int = 403,
    message: str = _DENIED,
) -> None:
    when = ET.SubElement(ET.SubElement(parent, "choose"), "when", {"condition": condition})
    _deny(when, reason=reason, with_caller=with_caller, code=code, message=message)


def _initialize_caller(parent: ET.Element) -> None:
    _variable(parent, "mosaic-caller", "")
    _variable(parent, "mosaic-client", "")


def _record_validated_caller(parent: ET.Element) -> None:
    """Copy the validated token's oid and azp into the caller variables the traces read."""

    for variable, claim in (("mosaic-caller", "oid"), ("mosaic-client", "azp")):
        _variable(parent, variable, _validated_claim(claim))


def _validated_claim(claim: str) -> str:
    name = _literal(claim)
    return _expression(
        [
            'var jwt = context.Variables.ContainsKey("mosaic-validated-token")'
            ' ? context.Variables["mosaic-validated-token"] as Jwt : null;',
            f"if (jwt == null || jwt.Claims == null || !jwt.Claims.ContainsKey({name}))"
            ' { return ""; }',
            f"var values = jwt.Claims[{name}];",
            "if (values == null || values.Length != 1 || String.IsNullOrWhiteSpace(values[0]))"
            ' { return ""; }',
            "return values[0].ToLowerInvariant();",
        ]
    )


def grant_counter_identity(publication: AccessPolicyPublication, grant: AccessPolicyGrant) -> str:
    """The shared credential-independent identity, before each native policy's namespace."""
    identity = json.dumps(
        [publication.tenant_id, publication.id, grant.entitlement_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def cost_center_counter_identity(
    publication: AccessPolicyPublication, cost_center_id: str
) -> str:
    """A pooled quota's counter identity: one cost center on one publication."""

    identity = json.dumps(
        [publication.tenant_id, publication.id, "cost-center", cost_center_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _code(grant: AccessPolicyGrant) -> str:
    return grant.cost_center_code.casefold()


def _match(publication: AccessPolicyPublication, grant: AccessPolicyGrant) -> str:
    """What a lookup returns for a grant: its counter identity and its cost center's code."""

    return f"{grant_counter_identity(publication, grant)}|{_code(grant)}"


def _fallback_order(grant: AccessPolicyGrant) -> tuple[str, bool, datetime, str]:
    """A caller's direct grants, in the order a call that names no cost center tries them.

    The grant under the caller's default cost center first, then the others oldest first.
    """

    return (
        grant.object_id.casefold(),
        not grant.default_cost_center,
        grant.granted_at or datetime.min.replace(tzinfo=UTC),
        grant.entitlement_id,
    )


def _cost_center_filter() -> str:
    # The lookups read the selected cost center into `cc` once, at the start.
    return '(cc == "" || cc == '


def _read_cost_center_header(parent: ET.Element) -> None:
    """Read the cost-center header once, into a code the lookups compare without case."""

    header = _literal(COST_CENTER_HEADER)
    _variable(
        parent,
        "mosaic-cost-center-header",
        _expression(
            [
                f'if (!context.Request.Headers.ContainsKey({header})) {{ return ""; }}',
                f"var values = context.Request.Headers[{header}];",
                'if (values == null || values.Length != 1 || values[0] == null) { return "!"; }',
                "var code = values[0].Trim();",
                "if (!System.Text.RegularExpressions.Regex.IsMatch(code, "
                f'{_literal("^" + COST_CENTER_CODE_PATTERN.pattern + "$")})) {{ return "!"; }}',
                "return code.ToLowerInvariant();",
            ]
        ),
    )
    _variable(parent, "mosaic-cc", f"@({_COST_CENTER_HEADER})")


def _split_match(parent: ET.Element, source: str, grant: str, cost_center: str) -> None:
    """Split a lookup's ``identity|code`` answer into the grant and its cost center's code."""

    value = f'(string)context.Variables["{source}"]'
    _variable(
        parent,
        grant,
        _expression(
            [
                f"var match = {value};",
                "var bar = match.IndexOf('|');",
                "return bar < 0 ? match : match.Substring(0, bar);",
            ]
        ),
    )
    if cost_center:
        _variable(
            parent,
            cost_center,
            _expression(
                [
                    f"var match = {value};",
                    "var bar = match.IndexOf('|');",
                    'return bar < 0 ? "" : match.Substring(bar + 1);',
                ]
            ),
        )


def _cost_center_ids(parent: ET.Element, grants: Sequence[AccessPolicyGrant]) -> None:
    """The matched grant's cost center ID, from its code: one line per cost center, not grant."""

    codes = sorted({_code(grant): grant.cost_center_id for grant in grants if _code(grant)}.items())
    lines = [f"var code = {_GRANT_COST_CENTER};"]
    for code, cost_center_id in codes:
        lines.append(f"if (code == {_literal(code)}) {{ return {_literal(cost_center_id)}; }}")
    _variable(parent, "mosaic-cost-center-id", _expression([*lines, 'return "";']))


def governed_counter_key_expression(
    publication: AccessPolicyPublication,
    grant: AccessPolicyGrant,
    *,
    prefix: str = _COUNTER_PREFIX,
) -> str:
    """The publication token counter expression for one governed grant."""

    identity = grant_counter_identity(publication, grant)
    if grant.is_group_grant:
        return f'@("{prefix}publication-tokens:{identity}:" + {_MEMBER})'
    return f'@("{prefix}publication-tokens:{identity}")'


def append_grant_attribution_trace(
    fragment: ET.Element, grants: Sequence[AccessPolicyGrant]
) -> None:
    # Resource logs keep the message. APIM documents metadata only as Application Insights
    # properties, so the message carries every value and the metadata repeats them there.
    trace = ET.SubElement(fragment, "trace", {"source": "mosaic", "severity": "information"})
    ET.SubElement(trace, "message").text = (
        f'@("{GRANT_ATTRIBUTION_TRACE_PREFIX} g=" + {_GRANT} + " m=" + {_MEMBER}'
        f' + " a=" + {_CLIENT})'
    )
    ET.SubElement(trace, "metadata", {"name": "mosaic-grant", "value": f"@({_GRANT})"})
    if any(grant.enabled and grant.is_group_grant for grant in grants):
        ET.SubElement(trace, "metadata", {"name": "mosaic-member", "value": f"@({_MEMBER})"})
    ET.SubElement(trace, "metadata", {"name": "mosaic-client", "value": f"@({_CLIENT})"})


def describe_grant_attribution_trace(facet: PolicyFacet, *, has_group_grants: bool) -> None:
    facet.summary = "Tags each authorized call with its MOSAIC grant so usage can be attributed."
    facet.details = [
        "The message records the grant as versioned key=value text. Resource logs keep it in "
        "TraceRecords when the gateway's Azure Monitor diagnostic logs at Information.",
        "Application Insights records one unsampled trace per call when its diagnostic "
        "verbosity is Information; set it to Error to stop.",
        "Token calls also record the validated client application ID, so usage can be split "
        "by app.",
    ]
    if has_group_grants:
        facet.details.append("Security-group grants also record the caller's validated object ID.")


def describe_denial_trace(facet: PolicyFacet) -> None:
    facet.summary = "Records why MOSAIC refused a call, so refused traffic shows in analytics."
    facet.details = [
        "Each refusal records a fixed reason code, never request content.",
        "Refusals after token validation also record the caller's validated object ID and "
        "client application ID.",
        "Resource logs keep it in TraceRecords when the gateway's Azure Monitor diagnostic logs "
        "at Information.",
    ]
    facet.attributes = {"trace": "denial"}


def classify_traces(
    fragment: ET.Element, facets: Sequence[PolicyFacet], *, has_group_grants: bool
) -> list[PolicyFacet]:
    """Describe the fragment's trace facets: one for its refusals and one for attribution.

    ``facets`` are the fragment's analyzed facets, whose trace entries arrive in document order.
    Every refusal has its own trace, but one facet describes them all.
    """

    traces = iter(element for element in fragment.iter() if element.tag == "trace")
    kept: list[PolicyFacet] = []
    denial_described = False
    for facet in facets:
        if facet.element != "trace":
            kept.append(facet)
            continue
        element = next(traces, None)
        message = "" if element is None else element.findtext("message", "")
        if DENIAL_TRACE_PREFIX in message:
            if denial_described:
                continue
            denial_described = True
            describe_denial_trace(facet)
        else:
            describe_grant_attribution_trace(facet, has_group_grants=has_group_grants)
        kept.append(facet)
    return kept


def _validate(publication: Publication, snapshot: ModelAccessSnapshot) -> None:
    if not all(
        _RESOURCE_NAME.fullmatch(name)
        for name in (publication.backend_name, publication.fragment_name)
    ):
        raise ValidationError("Governed policies require literal APIM backend and fragment names.")
    if publication.backend_key_name is not None:
        named_value_reference(publication.backend_key_name)
    if snapshot.settings.entra_enabled:
        if not _GUID.fullmatch(publication.tenant_id):
            raise ValidationError("Governed Entra access requires a specific tenant GUID.")
        if not snapshot.audience or not _GUID.fullmatch(snapshot.audience):
            raise ValidationError(
                "Governed Entra access requires the runtime application's GUID audience."
            )

    seen: dict[str, set[str]] = {
        "entitlement": set(),
        "subscription": set(),
        "object": set(),
        "subject": set(),
    }
    metered = snapshot.publication_enforcement is not None
    counters = (
        [snapshot.publication_enforcement.counter_key_expression]
        if snapshot.publication_enforcement is not None
        else []
    )
    for grant in snapshot.grants:
        if grant.subject.kind not in {
            EntitlementSubjectKind.USER,
            EntitlementSubjectKind.APPLICATION,
            EntitlementSubjectKind.SECURITY_GROUP,
        }:
            raise ValidationError(
                "Governed policies support only user, application and security-group grants."
            )
        if grant.is_group_grant:
            if not snapshot.settings.entra_enabled:
                raise ValidationError("Security-group grants require governed Entra access.")
            if not _GUID.fullmatch(grant.object_id):
                raise ValidationError("Security-group grants require a GUID object ID.")
        if not all(
            value.strip() for value in (grant.entitlement_id, grant.object_id, grant.subject.id)
        ):
            raise ValidationError(
                "Governed access grants must have nonempty, unambiguous identities."
            )
        identities = {
            "entitlement": grant.entitlement_id,
            # A subject holds one grant per cost center, so the same object or subject appears
            # once under each cost center it charges.
            "object": f"{grant.object_id}|{grant.cost_center_id}",
            "subject": f"{grant.subject.id}|{grant.cost_center_id}",
        }
        for kind, identity in identities.items():
            if identity.casefold() in seen[kind]:
                raise ValidationError(
                    "Governed access grants must have nonempty, unambiguous identities."
                )
            seen[kind].add(identity.casefold())
        if grant.enabled:
            # Only enabled grants are compiled. A disabled grant carried forward from an earlier
            # apply may keep a code its cost center has since changed, or one a deleted cost
            # center gave up, and must not stop the plan that removes it.
            _validate_cost_center(grant.cost_center_id, grant.cost_center_code, seen)
        if grant.subscription_name is not None:
            if (
                not grant.subscription_name.strip()
                or grant.subscription_name.casefold() in seen["subscription"]
            ):
                raise ValidationError(
                    "Governed access grants must have nonempty, unambiguous identities."
                )
            seen["subscription"].add(grant.subscription_name.casefold())
            if grant.subscription_name.casefold() in {
                "master",
                publication.subscription_name.casefold(),
            }:
                raise ValidationError(
                    "A governed grant cannot use the all-access or publication bootstrap "
                    "subscription."
                )
        if grant.enabled and grant.enforcement:
            if grant.enforcement.tokens:
                if not metered:
                    raise ValidationError(
                        "This publication can't be token-metered on its gateway's tier, so its "
                        "grants can't carry token limits."
                    )
                counters.append(grant.enforcement.tokens.counter_key_expression)
            if grant.enforcement.requests:
                requests = grant.enforcement.requests
                counters.append(requests.counter_key_expression)
                if requests.renewal_period_seconds and requests.renewal_period_seconds > 300:
                    raise ValidationError(
                        "Governed request rate renewal must not exceed 300 seconds."
                    )
    if any(re.sub(r"\s+", "", counter) not in _SUBSCRIPTION_COUNTERS for counter in counters):
        raise ValidationError(
            "Governed access replaces the standard subscription counter with a stable grant "
            "counter; custom counter expressions are not supported."
        )
    _validate_pools(
        snapshot.pools,
        seen,
        tokens_allowed=metered,
        codes={grant.cost_center_code.casefold() for grant in snapshot.grants if grant.enabled},
    )


def _validate_cost_center(cost_center_id: str, code: str, seen: dict[str, set[str]]) -> None:
    """A grant's cost center code must be one the header can name, and name only one cost center.

    A grant without a code is one the header can't select; a call naming no cost center still
    reaches it.
    """

    if not code:
        return
    if not COST_CENTER_CODE_PATTERN.fullmatch(code):
        raise ValidationError("A cost center's code must be 1 to 64 letters, digits, . - or _.")
    pair = f"{code.casefold()}|{cost_center_id}"
    codes = seen.setdefault("code", set())
    if pair not in codes:
        for item in codes:
            known_code, known_id = item.split("|", 1)
            if known_code == code.casefold() or known_id == cost_center_id:
                raise ValidationError(
                    "Each cost center needs exactly one code, and no two may share one, so the "
                    "header can tell them apart."
                )
    codes.add(pair)


def _validate_pools(
    pools: Sequence[AppliedCostCenterPool],
    seen: dict[str, set[str]],
    *,
    tokens_allowed: bool,
    codes: set[str],
) -> None:
    """Check each pool; ``codes`` are the enabled grants' codes, the pools the policy compiles."""

    pooled: set[str] = set()
    for pool in pools:
        if not pool.cost_center_code or pool.cost_center_id in pooled:
            raise ValidationError("Each cost center has at most one pooled quota per publication.")
        pooled.add(pool.cost_center_id)
        if pool.cost_center_code.casefold() in codes:
            _validate_cost_center(pool.cost_center_id, pool.cost_center_code, seen)
        if pool.monthly_tokens is not None and not tokens_allowed:
            raise ValidationError(
                "This publication can't be token-metered on its gateway's tier, so a cost "
                "center's pool on it must count calls, not tokens."
            )


def _key_shape_check() -> str:
    return _expression(
        [
            "var headers = context.Request.Headers;",
            "var query = context.Request.Url.Query;",
            "string header = null;",
            "string parameter = null;",
            'if (headers.ContainsKey("Ocp-Apim-Subscription-Key")) {',
            '    var values = headers["Ocp-Apim-Subscription-Key"];',
            "    if (values == null || values.Length != 1 || String.IsNullOrWhiteSpace(values[0]))"
            " { return true; }",
            "    header = values[0];",
            "}",
            'if (query.ContainsKey("subscription-key")) {',
            '    var values = query["subscription-key"];',
            "    if (values == null || values.Length != 1 || String.IsNullOrWhiteSpace(values[0]))"
            " { return true; }",
            "    parameter = values[0];",
            "}",
            "if (header != null && parameter != null &&"
            " !String.Equals(header, parameter, StringComparison.Ordinal)) { return true; }",
            "return context.Subscription == null || String.IsNullOrEmpty(context.Subscription.Id);",
        ]
    )


def _key_lookup(publication: Publication, grants: list[ModelAccessGrant]) -> str:
    """A key's grant, as ``identity|code``; ``-`` for a key its cost center turned off."""

    lines = [
        'if (context.Subscription == null) { return ""; }',
        "var subscription = context.Subscription.Id;",
    ]
    for grant in grants:
        if grant.is_group_grant:
            continue
        assert grant.subscription_name is not None
        answer = _match(publication, grant) if grant.keys_allowed else _KEYS_OFF
        lines.append(
            f"if (String.Equals(subscription, {_literal(grant.subscription_name)}, "
            "StringComparison.OrdinalIgnoreCase)) "
            f"{{ return {_literal(answer)}; }}"
        )
    return _expression([*lines, 'return "";'])


def _token_lookup(
    publication: AccessPolicyPublication,
    grants: Sequence[AccessPolicyGrant],
    *,
    delegated_scope: str = "Models.Invoke",
    application_role: str = "Models.Invoke.Application",
) -> str:
    """A token's grant, as ``identity|code``, under the cost center the call names, if any.

    A call that names none gets the caller's direct grant under their default cost center, then
    their other direct grants oldest first, then the most generous of their group grants.
    """

    direct_grants = sorted(
        (grant for grant in grants if not grant.is_group_grant), key=_fallback_order
    )
    group_grants = sorted(
        (grant for grant in grants if grant.is_group_grant),
        key=lambda grant: grant_precedence_key(grant.enforcement, grant.entitlement_id),
    )
    selected = _cost_center_filter()
    lines = [
        'var jwt = context.Variables.ContainsKey("mosaic-validated-token")'
        ' ? context.Variables["mosaic-validated-token"] as Jwt : null;',
        'if (jwt == null || jwt.Claims == null || !jwt.Claims.ContainsKey("oid")) { return ""; }',
        'var objects = jwt.Claims["oid"];',
        "if (objects == null || objects.Length != 1 || String.IsNullOrWhiteSpace(objects[0]))"
        ' { return ""; }',
        "var oid = objects[0];",
        f"var cc = {_SELECTED_COST_CENTER};",
        "bool delegated = false;",
        "bool application = false;",
        "bool hasRealScopes = false;",
        'if (jwt.Claims.ContainsKey("scp")) {',
        '    var scopes = jwt.Claims["scp"];',
        "    if (scopes != null && scopes.Length == 1 && scopes[0] != null) {",
        "        foreach (var scope in scopes[0].Split(' ')) {",
        '            if (String.IsNullOrWhiteSpace(scope) || scope == "/") { continue; }',
        "            hasRealScopes = true;",
        f"            if (String.Equals(scope, {_literal(delegated_scope)}, "
        "StringComparison.Ordinal)) { delegated = true; }",
        "        }",
        "    }",
        "}",
        'if (!hasRealScopes && jwt.Claims.ContainsKey("roles")) {',
        '    var roles = jwt.Claims["roles"];',
        f"    application = roles != null && roles.Contains({_literal(application_role)});",
        "}",
    ]
    for grant in direct_grants:
        kind = "delegated" if grant.subject.kind == EntitlementSubjectKind.USER else "application"
        lines.append(
            f"if ({kind} && String.Equals(oid, {_literal(grant.object_id)}, "
            f"StringComparison.OrdinalIgnoreCase) && {selected}{_literal(_code(grant))})) "
            f"{{ return {_literal(_match(publication, grant))}; }}"
        )
    if group_grants:
        lines.extend(
            [
                'var groups = jwt.Claims.ContainsKey("groups") ? jwt.Claims["groups"] : null;',
                "if ((delegated || application) && groups != null) {",
            ]
        )
        for grant in group_grants:
            lines.extend(
                [
                    "    foreach (var group in groups) {",
                    f"        if (String.Equals(group, {_literal(grant.object_id.lower())}, "
                    f"StringComparison.OrdinalIgnoreCase) && {selected}"
                    f"{_literal(_code(grant))})) "
                    f"{{ return {_literal(_match(publication, grant))}; }}",
                    "    }",
                ]
            )
        lines.append("}")
    return _expression([*lines, 'return "";'])


def _token_member_lookup(
    publication: AccessPolicyPublication, grants: Sequence[AccessPolicyGrant]
) -> str:
    group_ids = {
        grant_counter_identity(publication, grant) for grant in grants if grant.is_group_grant
    }
    if not group_ids:
        return ""
    lines = [
        "if (",
        "    "
        + " && ".join(f"{_TOKEN_GRANT} != {_literal(identity)}" for identity in sorted(group_ids)),
        ') { return ""; }',
        'var jwt = context.Variables.ContainsKey("mosaic-validated-token")'
        ' ? context.Variables["mosaic-validated-token"] as Jwt : null;',
        'if (jwt == null || jwt.Claims == null || !jwt.Claims.ContainsKey("oid")) { return ""; }',
        'var objects = jwt.Claims["oid"];',
        "if (objects == null || objects.Length != 1 || String.IsNullOrWhiteSpace(objects[0]))"
        ' { return ""; }',
        "return objects[0].ToLowerInvariant();",
    ]
    return _expression(lines)


def _token_groups_overage() -> str:
    """A whole condition: no token grant matched and the token omitted its groups (overage).

    A single multi-statement expression, because APIM can't nest ``@{...}`` inside ``@(...)``.
    """

    return _expression(
        [
            f"if (!String.IsNullOrEmpty({_TOKEN_GRANT})) {{ return false; }}",
            'var jwt = context.Variables.ContainsKey("mosaic-validated-token")'
            ' ? context.Variables["mosaic-validated-token"] as Jwt : null;',
            "if (jwt == null || jwt.Claims == null) { return false; }",
            'if (jwt.Claims.ContainsKey("hasgroups")) { return true; }',
            "try {",
            '    if (!jwt.Claims.ContainsKey("_claim_names")) { return false; }',
            '    var values = jwt.Claims["_claim_names"];',
            "    if (values == null) { return false; }",
            "    foreach (var value in values) {",
            "        if (value != null && "
            'value.IndexOf("groups", StringComparison.OrdinalIgnoreCase) >= 0) { return true; }',
            "    }",
            "} catch { return false; }",
            "return false;",
        ]
    )


def _authentication(
    fragment: ET.Element,
    publication: Publication,
    snapshot: ModelAccessSnapshot,
    grants: list[ModelAccessGrant],
    *,
    delegated_scope: str = "Models.Invoke",
    application_role: str = "Models.Invoke.Application",
) -> None:
    _variable(
        fragment,
        "mosaic-has-key",
        '@(context.Request.Headers.ContainsKey("Ocp-Apim-Subscription-Key")'
        ' || context.Request.Url.Query.ContainsKey("subscription-key"))',
    )
    _variable(
        fragment, "mosaic-has-token", '@(context.Request.Headers.ContainsKey("Authorization"))'
    )
    _initialize_caller(fragment)
    _reject(fragment, f"@(!{_HAS_KEY} && !{_HAS_TOKEN})", reason="no-credential", code=401)
    if not snapshot.settings.keys_enabled:
        _reject(fragment, f"@({_HAS_KEY})", reason="keys-off", code=401)
    if not snapshot.settings.entra_enabled:
        _reject(fragment, f"@({_HAS_TOKEN})", reason="tokens-off", code=401)
    _read_cost_center_header(fragment)
    _reject(
        fragment,
        f'@({_COST_CENTER_HEADER} == "!")',
        reason="cost-center",
        message=COST_CENTER_DENIED,
    )
    _variable(fragment, "mosaic-key-grant", "")
    _variable(fragment, "mosaic-key-cost-center", "")
    _variable(fragment, "mosaic-token-grant", "")
    _variable(fragment, "mosaic-token-cost-center", "")
    _variable(fragment, "mosaic-member", "")

    if snapshot.settings.keys_enabled:
        key = ET.SubElement(
            ET.SubElement(fragment, "choose"), "when", {"condition": f"@({_HAS_KEY})"}
        )
        _reject(key, _key_shape_check(), reason="key-malformed", code=401)
        _variable(key, "mosaic-key-match", _key_lookup(publication, grants))
        _split_match(key, "mosaic-key-match", "mosaic-key-grant", "mosaic-key-cost-center")
        if any(not grant.keys_allowed for grant in grants if not grant.is_group_grant):
            _reject(
                key,
                f'@({_KEY_GRANT} == "{_KEYS_OFF}")',
                reason="keys-off",
                code=401,
                message=COST_CENTER_KEYS_OFF_DENIED,
            )
        _reject(key, f"@(String.IsNullOrEmpty({_KEY_GRANT}))", reason="key-unknown")
        # A key belongs to one grant, so it charges that grant's cost center. A header naming
        # another is refused rather than ignored; without one, a token sent with the key resolves
        # under the key's cost center.
        _reject(
            key,
            f'@({_COST_CENTER_HEADER} != "" && {_COST_CENTER_HEADER} != {_KEY_COST_CENTER})',
            reason="cost-center-mismatch",
            message=COST_CENTER_MISMATCH_DENIED,
        )
        _variable(key, "mosaic-cc", f"@({_KEY_COST_CENTER})")

    if snapshot.settings.entra_enabled:
        token = ET.SubElement(
            ET.SubElement(fragment, "choose"), "when", {"condition": f"@({_HAS_TOKEN})"}
        )
        _reject(
            token,
            _expression(
                [
                    'var values = context.Request.Headers["Authorization"];',
                    "if (values == null || values.Length != 1) { return true; }",
                    "var authorization = values[0];",
                    "return String.IsNullOrWhiteSpace(authorization)"
                    ' || !authorization.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)'
                    " || String.IsNullOrWhiteSpace(authorization.Substring(7));",
                ]
            ),
            reason="token-malformed",
            code=401,
        )
        validation = ET.SubElement(
            token,
            "validate-azure-ad-token",
            {
                "tenant-id": publication.tenant_id,
                "header-name": "Authorization",
                "failed-validation-httpcode": "401",
                "failed-validation-error-message": _DENIED,
                "output-token-variable-name": "mosaic-validated-token",
            },
        )
        ET.SubElement(ET.SubElement(validation, "audiences"), "audience").text = snapshot.audience
        claim = ET.SubElement(
            ET.SubElement(validation, "required-claims"),
            "claim",
            {
                "name": "ver",
                "match": "all",
            },
        )
        ET.SubElement(claim, "value").text = "2.0"
        _record_validated_caller(token)
        _resolve_token_grant(
            token,
            publication,
            grants,
            delegated_scope=delegated_scope,
            application_role=application_role,
            overage_message=_GROUPS_OVERAGE_DENIED,
        )

    _reject(
        fragment,
        f"@({_HAS_KEY} && {_HAS_TOKEN} && {_KEY_GRANT} != {_TOKEN_GRANT})",
        reason="grant-mismatch",
        with_caller=True,
    )
    _variable(fragment, "mosaic-grant", f"@({_HAS_KEY} ? {_KEY_GRANT} : {_TOKEN_GRANT})")
    _variable(
        fragment,
        "mosaic-cost-center",
        f"@({_HAS_KEY} ? {_KEY_COST_CENTER} : {_TOKEN_COST_CENTER})",
    )
    _cost_center_ids(fragment, grants)


def _resolve_token_grant(
    parent: ET.Element,
    publication: AccessPolicyPublication,
    grants: Sequence[AccessPolicyGrant],
    *,
    delegated_scope: str,
    application_role: str,
    overage_message: str,
    no_grant: Callable[[ET.Element], None] | None = None,
) -> None:
    """Match the validated token to a grant under the selected cost center, or refuse the call."""

    _variable(
        parent,
        "mosaic-token-match",
        _token_lookup(
            publication,
            grants,
            delegated_scope=delegated_scope,
            application_role=application_role,
        ),
    )
    _split_match(parent, "mosaic-token-match", "mosaic-token-grant", "mosaic-token-cost-center")
    _variable(parent, "mosaic-member", _token_member_lookup(publication, grants))
    if any(grant.is_group_grant for grant in grants):
        _reject(
            parent,
            _token_groups_overage(),
            reason="groups-overage",
            with_caller=True,
            message=overage_message,
        )
    # Naming a cost center the caller holds no grant under is refused as such, so the caller
    # learns their header is wrong rather than that they have no access at all.
    _reject(
        parent,
        f'@(String.IsNullOrEmpty({_TOKEN_GRANT}) && {_COST_CENTER_HEADER} != "")',
        reason="cost-center",
        with_caller=True,
        message=COST_CENTER_DENIED,
    )
    if no_grant is not None:
        no_grant(parent)
    else:
        _reject(
            parent,
            f"@(String.IsNullOrEmpty({_TOKEN_GRANT}))",
            reason="no-grant",
            with_caller=True,
        )


def _operation_guard(
    fragment: ET.Element, publication: Publication, operations: tuple[OperationSpec, ...]
) -> None:
    allowed = " || ".join(f"context.Operation.Id == {_literal(op.name)}" for op in operations)
    _reject(
        fragment,
        f'@(context.Operation == null || context.Request.Method != "POST" || !({allowed}))',
        reason="operation",
        with_caller=True,
        message=(
            "This operation is not available through governed access."
            if publication.api_shape == ApiShape.ANTHROPIC_MESSAGES
            else "This operation is not available with governed token limits."
        ),
    )
    unscoped = [op for op in operations if not op.url_template.startswith("/openai/deployments/")]
    if unscoped:
        needs_model = " || ".join(f"context.Operation.Id == {_literal(op.name)}" for op in unscoped)
        _reject(
            fragment,
            _expression(
                [
                    f"if (!({needs_model})) {{ return false; }}",
                    "if (context.Request.Body == null) { return true; }",
                    "try {",
                    "    var body = context.Request.Body.As<JObject>(preserveContent: true);",
                    '    var model = body == null ? null : body["model"];',
                    "    return model == null || model.Type != JTokenType.String ||",
                    "        !String.Equals((string)model, "
                    f"{_literal(publication.deployment_name)}, StringComparison.Ordinal);",
                    "} catch { return true; }",
                ]
            ),
            reason="model",
            with_caller=True,
            message="The request must name this publication's model deployment.",
        )


def _grant_counter_key(
    namespace: str, identity: str, *, per_member: bool, prefix: str = _COUNTER_PREFIX
) -> str:
    key = f"{prefix}{namespace}:{identity}"
    if per_member:
        return f'@("{key}:" + {_MEMBER})'
    return key


def _calendar_counter(
    identity: str,
    period: QuotaPeriod,
    *,
    per_member: bool,
    prefix: str = _COUNTER_PREFIX,
    namespace: str = "grant-request-quota",
) -> str:
    key_prefix = _literal(f"{prefix}{namespace}:{identity}:{period}:")
    if period == "Weekly":
        date = "now.Date.AddDays(-(((int)now.DayOfWeek + 6) % 7))"
    else:
        date = "now"
    length = {"Hourly": 13, "Daily": 10, "Weekly": 10, "Monthly": 7, "Yearly": 4}[period]
    suffix = f' + ":" + {_MEMBER}' if per_member else ""
    # "u" is the invariant Gregorian format. CultureInfo is not in APIM's expression allowlist.
    return _expression(
        [
            "var now = DateTime.UtcNow;",
            f'return {key_prefix} + {date}.ToString("u").Substring(0, {length}){suffix};',
        ]
    )


def _grant_limits(
    fragment: ET.Element,
    publication: AccessPolicyPublication,
    grants: Sequence[AccessPolicyGrant],
    *,
    prefix: str = _COUNTER_PREFIX,
) -> None:
    limited = [grant for grant in grants if grant.enforcement is not None]
    if not limited:
        return
    choose = ET.SubElement(fragment, "choose")
    for grant in limited:
        identity = grant_counter_identity(publication, grant)
        when = ET.SubElement(choose, "when", {"condition": f"@({_GRANT} == {_literal(identity)})"})
        enforcement = grant.enforcement
        assert enforcement is not None
        if requests := enforcement.requests:
            if requests.calls is not None:
                ET.SubElement(
                    when,
                    "rate-limit-by-key",
                    {
                        "calls": str(requests.calls),
                        "renewal-period": str(requests.renewal_period_seconds),
                        "counter-key": _grant_counter_key(
                            "grant-request-rate",
                            identity,
                            per_member=grant.is_group_grant,
                            prefix=prefix,
                        ),
                        "remaining-calls-header-name": REMAINING_CALLS_HEADER,
                    },
                )
            if requests.call_quota is not None:
                assert requests.call_quota_period is not None
                ET.SubElement(
                    when,
                    "quota-by-key",
                    {
                        "calls": str(requests.call_quota),
                        "renewal-period": "0",
                        "counter-key": _calendar_counter(
                            identity,
                            requests.call_quota_period,
                            per_member=grant.is_group_grant,
                            prefix=prefix,
                        ),
                    },
                )


def _pool_limits(
    fragment: ET.Element,
    publication: AccessPolicyPublication,
    pools: Sequence[AppliedCostCenterPool],
    *,
    estimate_prompt_tokens: bool | None,
    prefix: str = _COUNTER_PREFIX,
) -> None:
    """Each cost center's pooled monthly quota, shared by every grant under it here.

    A second limit beside the grant's own, keyed on the cost center and the publication, so every
    caller charging the cost center draws on the same count. ``estimate_prompt_tokens`` is None
    where the gateway can't meter tokens, and then only call pools apply.
    """

    pooled = [pool for pool in pools if pool.cost_center_code]
    if not pooled:
        return
    choose = ET.SubElement(fragment, "choose")
    for pool in sorted(pooled, key=lambda item: item.cost_center_code.casefold()):
        identity = cost_center_counter_identity(publication, pool.cost_center_id)
        when = ET.SubElement(
            choose,
            "when",
            {
                "condition": (
                    f"@({_GRANT_COST_CENTER} == {_literal(pool.cost_center_code.casefold())})"
                )
            },
        )
        if pool.monthly_tokens is not None and estimate_prompt_tokens is not None:
            ET.SubElement(
                when,
                "llm-token-limit",
                {
                    "counter-key": f"{prefix}cost-center-tokens:{identity}",
                    "estimate-prompt-tokens": "true" if estimate_prompt_tokens else "false",
                    "token-quota": str(pool.monthly_tokens),
                    "token-quota-period": "Monthly",
                    "remaining-quota-tokens-header-name": (
                        COST_CENTER_REMAINING_QUOTA_TOKENS_HEADER
                    ),
                },
            )
        if pool.monthly_calls is not None:
            ET.SubElement(
                when,
                "quota-by-key",
                {
                    "calls": str(pool.monthly_calls),
                    "renewal-period": "0",
                    "counter-key": _calendar_counter(
                        identity,
                        "Monthly",
                        per_member=False,
                        prefix=prefix,
                        namespace="cost-center-request-quota",
                    ),
                },
            )


def _limits(
    fragment: ET.Element,
    publication: Publication,
    snapshot: ModelAccessSnapshot,
    grants: list[ModelAccessGrant],
) -> None:
    _grant_limits(fragment, publication, grants)
    token_limited = [grant for grant in grants if grant.enforcement is not None]
    if token_limited:
        choose = next(
            (
                element
                for element in fragment.findall("choose")
                if any(
                    child.get("condition", "").startswith(f"@({_GRANT} ==")
                    for child in element.findall("when")
                )
            ),
            None,
        )
        if choose is None:
            choose = ET.SubElement(fragment, "choose")
        for grant in token_limited:
            enforcement = grant.enforcement
            assert enforcement is not None
            if tokens := enforcement.tokens:
                identity = grant_counter_identity(publication, grant)
                condition = f"@({_GRANT} == {_literal(identity)})"
                when = next(
                    (
                        child
                        for child in choose.findall("when")
                        if child.get("condition") == condition
                    ),
                    None,
                )
                if when is None:
                    when = ET.SubElement(choose, "when", {"condition": condition})
                headers: dict[str, str] = {}
                if tokens.tokens_per_minute:
                    headers["remaining-tokens-header-name"] = REMAINING_TOKENS_HEADER
                if tokens.token_quota:
                    headers["remaining-quota-tokens-header-name"] = REMAINING_QUOTA_TOKENS_HEADER
                ET.SubElement(
                    when,
                    "llm-token-limit",
                    {
                        **_token_limit_attributes(tokens),
                        "counter-key": _grant_counter_key(
                            "grant-tokens",
                            identity,
                            per_member=grant.is_group_grant,
                        ),
                        **headers,
                    },
                )
    enabled_codes = {_code(grant) for grant in grants}
    _pool_limits(
        fragment,
        publication,
        [pool for pool in snapshot.pools if pool.cost_center_code.casefold() in enabled_codes],
        estimate_prompt_tokens=(
            snapshot.publication_enforcement.estimate_prompt_tokens
            if snapshot.publication_enforcement is not None
            else None
        ),
    )
    if snapshot.publication_enforcement is not None:
        counter = f'@("{_COUNTER_PREFIX}publication-tokens:" + {_GRANT})'
        if any(grant.is_group_grant for grant in grants):
            counter = (
                f'@("{_COUNTER_PREFIX}publication-tokens:" + {_GRANT} + '
                f'(String.IsNullOrEmpty({_MEMBER}) ? "" : ":" + {_MEMBER}))'
            )
        ET.SubElement(
            fragment,
            "llm-token-limit",
            {
                **_token_limit_attributes(snapshot.publication_enforcement),
                "counter-key": counter,
            },
        )


_COUNTER_NAMESPACES = (
    "publication-tokens",
    "grant-tokens",
    "grant-request-rate",
    "grant-request-quota",
    "cost-center-tokens",
    "cost-center-request-quota",
)


def cost_center_details() -> list[str]:
    """How the policy picks a call's cost center, for the authorization facet."""

    return [
        f"A call names the cost center it charges with the {COST_CENTER_HEADER} header, "
        "compared without case. It selects among the caller's grants under that cost center "
        "and is removed before the call reaches the backend.",
        "Without the header, the caller's direct grant under their default cost center applies, "
        "then their other direct grants oldest first, then their group grants by precedence.",
        "A malformed header, or one naming a cost center the caller holds no grant under, is "
        "refused with 403; so is a key presented with a different cost center's header.",
    ]


def describe_limit_facet(facet: PolicyFacet, element: ET.Element, prefix: str) -> None:
    """Explain a limit by what it counts: a grant, a cost center's pool, or the publication."""

    counter = element.get("counter-key", "")
    namespace = next(name for name in _COUNTER_NAMESPACES if f"{prefix}{name}:" in counter)
    pooled = namespace.startswith("cost-center")
    counted = (
        "counted per cost center on this publication."
        if pooled
        else "counted per stable tenant/publication/entitlement grant."
    )
    facet.summary = re.sub(r"counted .+\.$", counted, facet.summary)
    facet.attributes.update(
        {
            "counter-scope": "cost-center-pool" if pooled else "stable-grant",
            "counter-namespace": namespace,
        }
    )
    if pooled:
        facet.details.extend(
            [
                "Every grant under the cost center on this publication shares this monthly "
                "pool, whoever calls, beside each grant's own limits.",
                "Native APIM limits are distributed/per gateway, not exact global or "
                "billing totals.",
            ]
        )
    else:
        facet.details.extend(
            [
                "Primary and secondary subscription keys and Entra tokens share this "
                "grant counter.",
                "Counter identity is independent of key rotation, access method and "
                "snapshot revision.",
                "Native APIM limits are distributed/per gateway, not exact global or "
                "billing totals.",
            ]
        )
        if namespace == "publication-tokens":
            facet.details.append(
                "Publication token safeguards apply even to grants without limits."
            )
        else:
            facet.details.append("Applies only to the matching enabled grant.")
    headers = [
        value
        for name, value in sorted(element.attrib.items())
        if name.endswith("-header-name")
    ]
    if headers:
        facet.details.append(f"Reports what remains in the {', '.join(headers)} response header.")
    if facet.element == "quota-by-key":
        period = next(period for period in _PERIOD_LABELS if f":{period}:" in counter)
        facet.summary = (
            f"Caps requests at {element.get('calls')} per UTC calendar "
            f"{_PERIOD_LABELS[period]}, {counted}"
        )
        facet.attributes["calendar-period"] = period
        facet.details.append(
            "A period-qualified key resets the quota at UTC calendar boundaries."
        )


def _operations_facet(
    publication: Publication, operations: tuple[OperationSpec, ...]
) -> PolicyFacet:
    allowed = "Allowed curated operation IDs: " + ", ".join(op.name for op in operations) + "."
    pinned = (
        "Routes not scoped to a deployment require the request model to equal this "
        "publication's deployment; malformed or missing model values are denied "
        "without forwarding."
    )
    if publication.api_shape == ApiShape.ANTHROPIC_MESSAGES:
        summary = "Governed access permits only the Anthropic Messages operation."
        denied = "All other operations, including token counting, are denied."
    else:
        summary = (
            "Governed token limits permit only supported chat-completions and responses operations."
        )
        denied = (
            "All other operations, including embeddings, image, audio and legacy completions, "
            "are denied."
        )
    return PolicyFacet(
        kind=PolicyFacetKind.AUTHORIZATION,
        element="choose",
        section=PolicySection.INBOUND,
        summary=summary,
        details=[allowed, denied, pinned],
        attributes={"allowed-operations": ",".join(op.name for op in operations)},
        managed_by_mosaic=True,
    )


def _facets(
    publication: Publication,
    snapshot: ModelAccessSnapshot,
    fragment: ET.Element,
    api: ET.Element,
    operations: tuple[OperationSpec, ...],
) -> tuple[list[PolicyFacet], list[str]]:
    fragment_analysis = analyze_policy(_serialize(fragment))
    api_analysis = analyze_policy(_serialize(api))
    methods = []
    if snapshot.settings.keys_enabled:
        methods.append("an allowlisted APIM subscription key")
    if snapshot.settings.entra_enabled:
        methods.append("a validated Microsoft Entra bearer token")
    enabled_group_grants = sum(grant.enabled and grant.is_group_grant for grant in snapshot.grants)
    auth_details = [
        "Every presented credential must be enabled and valid; there is no fallback.",
        "When a key and a token are both presented, they must resolve to the same enabled grant.",
        "Keys in both header and query must be identical; ambiguous or empty credentials "
        "are denied.",
        "User tokens require Models.Invoke in scp; application tokens require "
        "Models.Invoke.Application in roles with no scp claim. Claims are read only "
        "after signature, tenant,"
        " audience and expiry validation.",
        "Only enabled direct grants are allowlisted; all-access, bootstrap and unrelated "
        "subscriptions are denied. No caller identity header or control-plane callback "
        "is used.",
        "Legacy subscription ID/key counter defaults are replaced by shared grant "
        "counters in this reviewed policy; saved grant inputs are not silently rewritten.",
    ]
    auth_attributes = {
        "keys-enabled": str(snapshot.settings.keys_enabled).lower(),
        "entra-enabled": str(snapshot.settings.entra_enabled).lower(),
        "enabled-grants": str(sum(grant.enabled for grant in snapshot.grants)),
    }
    if enabled_group_grants:
        auth_details.extend(
            [
                f"{enabled_group_grants} enabled security-group grant"
                f"{'' if enabled_group_grants == 1 else 's'} match the validated token's "
                "groups claim.",
                "Security-group grants accept Microsoft Entra tokens only; they have no "
                "APIM subscription key path.",
                "Security-group grant limits apply separately to each validated member object ID.",
                "A direct user or application grant to the caller wins before any group grant is "
                "considered.",
            ]
        )
        auth_attributes["security-group-grants"] = str(enabled_group_grants)
    auth_details.extend(cost_center_details())
    cost_centers = {grant.cost_center_id for grant in snapshot.grants if grant.enabled}
    auth_attributes["cost-centers"] = str(len(cost_centers))
    facets = [
        PolicyFacet(
            kind=PolicyFacetKind.AUTHORIZATION,
            element="choose",
            section=PolicySection.INBOUND,
            summary=f"Requires {' or '.join(methods)}."
            if methods
            else "All model access is denied.",
            details=auth_details,
            attributes=auth_attributes,
            managed_by_mosaic=True,
        ),
        _operations_facet(publication, operations),
    ]
    limit_elements = iter(
        element
        for element in fragment.iter()
        if element.tag in {"llm-token-limit", "rate-limit-by-key", "quota-by-key"}
    )
    fragment_facets = classify_traces(
        fragment, fragment_analysis.facets, has_group_grants=enabled_group_grants > 0
    )
    for facet in [*fragment_facets, *api_analysis.facets]:
        facet.managed_by_mosaic = True
        if facet.section == PolicySection.UNKNOWN:
            facet.section = PolicySection.INBOUND
        if facet.element in {"llm-token-limit", "rate-limit-by-key", "quota-by-key"}:
            element = next(limit_elements)
            describe_limit_facet(facet, element, _COUNTER_PREFIX)
        elif facet.element == "set-backend-service":
            facet.summary = "Routes authorized requests to the publication's configured backend."
            facet.attributes = {"backend-id": "[redacted]"}
        elif facet.element == "include-fragment":
            facet.summary = "Applies the MOSAIC-managed governed model access rule set."
            facet.attributes = {"fragment-id": "[redacted]"}
        elif facet.element == "set-query-parameter":
            name = facet.attributes.get("name", "subscription-key")
            facet.summary = f"Removes the {name} query parameter before forwarding."
        elif facet.element == "set-header" and is_backend_key_facet(
            facet, publication.api_shape
        ) and publication.backend_key_name is not None:
            describe_backend_key(facet)
        facets.append(facet)
    return facets, sorted(
        set(fragment_analysis.unrecognized_elements + api_analysis.unrecognized_elements)
    )


def governed_operations(publication: Publication) -> tuple[OperationSpec, ...]:
    supported = (
        _ANTHROPIC_OPERATIONS
        if publication.api_shape == ApiShape.ANTHROPIC_MESSAGES
        else _SUPPORTED_OPERATIONS
    )
    operations = tuple(
        op for op in operations_for(publication) if op.name in supported and op.method == "POST"
    )
    if not operations:
        raise ValidationError("This API shape has no operations supported by governed access.")
    return operations


def render_governed_policy(
    publication: Publication,
    snapshot: ModelAccessSnapshot,
    *,
    delegated_scope: str = "Models.Invoke",
    application_role: str = "Models.Invoke.Application",
) -> PublicationPolicy:
    """Return in-process XML and redacted facets; reject unsafe intent with ValidationError.

    The supplied snapshot, rather than mutable publication access settings or grants, is the
    authority for this rendering. Only the publication's identity and routing metadata are used.
    """
    _validate(publication, snapshot)
    operations = governed_operations(publication)
    grants = sorted(
        (grant for grant in snapshot.grants if grant.enabled),
        key=lambda grant: grant.entitlement_id,
    )
    fragment = ET.Element("fragment")
    if not (snapshot.settings.keys_enabled or snapshot.settings.entra_enabled):
        _deny(fragment, reason="access-off")
    else:
        _authentication(
            fragment,
            publication,
            snapshot,
            grants,
            delegated_scope=delegated_scope,
            application_role=application_role,
        )
        _operation_guard(fragment, publication, operations)
        append_grant_attribution_trace(fragment, grants)
        _limits(fragment, publication, snapshot, grants)
        removed_headers = (
            "Ocp-Apim-Subscription-Key",
            "api-key",
            "Authorization",
            COST_CENTER_HEADER,
        )
        for name in removed_headers:
            ET.SubElement(fragment, "set-header", {"name": name, "exists-action": "delete"})
        ET.SubElement(
            fragment,
            "set-query-parameter",
            {
                "name": "subscription-key",
                "exists-action": "delete",
            },
        )
        key_name = publication.backend_key_name
        if key_name is not None:
            # A key-authenticated backend could honour a credential the managed identity path
            # never had to remove, so every one goes before the gateway's key is set (ADR 0018).
            strip_caller_credentials(
                fragment,
                removed_headers=(*removed_headers, *shape_removed_headers(publication.api_shape)),
                removed_query_parameters=("subscription-key",),
            )
        add_shape_headers(fragment, publication.api_shape)
        if key_name is not None:
            set_backend_key(fragment, shape=publication.api_shape, named_value=key_name)
        else:
            ET.SubElement(
                fragment,
                "authentication-managed-identity",
                {
                    "resource": managed_identity_resource(publication.api_shape),
                },
            )
        ET.SubElement(fragment, "set-backend-service", {"backend-id": publication.backend_name})
        if snapshot.publication_enforcement is not None:
            metric = ET.SubElement(
                fragment, "llm-emit-token-metric", {"namespace": METRIC_NAMESPACE}
            )
            for name, value in (
                ("Publication", publication.id),
                ("Deployment", publication.deployment_name),
            ):
                ET.SubElement(metric, "dimension", {"name": name, "value": f"@({_literal(value)})"})
    fragment_xml = _serialize(fragment)
    if len(fragment_xml.encode("utf-8")) > MAX_FRAGMENT_BYTES:
        raise ValidationError(
            "The governed policy fragment exceeds APIM's 512 KB UTF-8 size limit."
        )

    policies = ET.Element("policies")
    inbound = ET.SubElement(policies, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "include-fragment", {"fragment-id": publication.fragment_name})
    for section in ("backend", "outbound", "on-error"):
        ET.SubElement(ET.SubElement(policies, section), "base")
    api_policy_xml = _serialize(policies)
    facets, unrecognized = _facets(publication, snapshot, fragment, policies, operations)
    return PublicationPolicy(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        content_sha256=hashlib.sha256(f"{fragment_xml}\n{api_policy_xml}".encode()).hexdigest(),
        facets=facets,
        unrecognized_elements=unrecognized,
    )
