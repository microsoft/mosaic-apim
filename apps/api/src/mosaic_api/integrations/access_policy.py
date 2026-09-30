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
from collections.abc import Sequence
from typing import Protocol

from mosaic_api.domain import (
    ApiShape,
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
# Readers split the message on spaces into key=value pairs and ignore keys they don't know.
# Only a change that breaks those readers bumps the version.
GRANT_ATTRIBUTION_TRACE_PREFIX = "mosaic-attribution v=1"
_DENIED = "Model access denied."
_GROUPS_OVERAGE_DENIED = (
    "Model access denied. Your token doesn't list your groups because you belong to too many; "
    "ask an administrator for a direct grant."
)
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


def _deny(parent: ET.Element, *, code: int = 403, message: str = _DENIED) -> None:
    response = ET.SubElement(parent, "return-response")
    ET.SubElement(
        response,
        "set-status",
        {"code": str(code), "reason": "Unauthorized" if code == 401 else "Forbidden"},
    )
    ET.SubElement(response, "set-body").text = message


def _reject(parent: ET.Element, condition: str, *, code: int = 403, message: str = _DENIED) -> None:
    when = ET.SubElement(ET.SubElement(parent, "choose"), "when", {"condition": condition})
    _deny(when, code=code, message=message)


def grant_counter_identity(publication: AccessPolicyPublication, grant: AccessPolicyGrant) -> str:
    """The shared credential-independent identity, before each native policy's namespace."""
    identity = json.dumps(
        [publication.tenant_id, publication.id, grant.entitlement_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


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
        f'@("{GRANT_ATTRIBUTION_TRACE_PREFIX} g=" + {_GRANT} + " m=" + {_MEMBER})'
    )
    ET.SubElement(trace, "metadata", {"name": "mosaic-grant", "value": f"@({_GRANT})"})
    if any(grant.enabled and grant.is_group_grant for grant in grants):
        ET.SubElement(trace, "metadata", {"name": "mosaic-member", "value": f"@({_MEMBER})"})


def describe_grant_attribution_trace(facet: PolicyFacet, *, has_group_grants: bool) -> None:
    facet.summary = "Tags each authorized call with its MOSAIC grant so usage can be attributed."
    facet.details = [
        "The message records the grant as versioned key=value text. Resource logs keep it in "
        "TraceRecords when the gateway's Azure Monitor diagnostic logs at Information.",
        "Application Insights records one unsampled trace per call when its diagnostic "
        "verbosity is Information; set it to Error to stop.",
    ]
    if has_group_grants:
        facet.details.append("Security-group grants also record the caller's validated object ID.")


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
        identities = {
            "entitlement": grant.entitlement_id,
            "object": grant.object_id,
            "subject": grant.subject.id,
        }
        for kind, identity in identities.items():
            if not identity.strip() or identity.casefold() in seen[kind]:
                raise ValidationError(
                    "Governed access grants must have nonempty, unambiguous identities."
                )
            seen[kind].add(identity.casefold())
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
    lines = [
        'if (context.Subscription == null) { return ""; }',
        "var subscription = context.Subscription.Id;",
    ]
    for grant in grants:
        if grant.is_group_grant:
            continue
        assert grant.subscription_name is not None
        lines.append(
            f"if (String.Equals(subscription, {_literal(grant.subscription_name)}, "
            "StringComparison.OrdinalIgnoreCase)) "
            f"{{ return {_literal(grant_counter_identity(publication, grant))}; }}"
        )
    return _expression([*lines, 'return "";'])


def _token_lookup(
    publication: AccessPolicyPublication,
    grants: Sequence[AccessPolicyGrant],
    *,
    delegated_scope: str = "Models.Invoke",
    application_role: str = "Models.Invoke.Application",
) -> str:
    direct_grants = [grant for grant in grants if not grant.is_group_grant]
    group_grants = sorted(
        (grant for grant in grants if grant.is_group_grant),
        key=lambda grant: grant_precedence_key(grant.enforcement, grant.entitlement_id),
    )
    lines = [
        'var jwt = context.Variables.ContainsKey("mosaic-validated-token")'
        ' ? context.Variables["mosaic-validated-token"] as Jwt : null;',
        'if (jwt == null || jwt.Claims == null || !jwt.Claims.ContainsKey("oid")) { return ""; }',
        'var objects = jwt.Claims["oid"];',
        "if (objects == null || objects.Length != 1 || String.IsNullOrWhiteSpace(objects[0]))"
        ' { return ""; }',
        "var oid = objects[0];",
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
            "StringComparison.OrdinalIgnoreCase)) "
            f"{{ return {_literal(grant_counter_identity(publication, grant))}; }}"
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
                    "StringComparison.OrdinalIgnoreCase)) "
                    f"{{ return {_literal(grant_counter_identity(publication, grant))}; }}",
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
    _reject(fragment, f"@(!{_HAS_KEY} && !{_HAS_TOKEN})", code=401)
    if not snapshot.settings.keys_enabled:
        _reject(fragment, f"@({_HAS_KEY})", code=401)
    if not snapshot.settings.entra_enabled:
        _reject(fragment, f"@({_HAS_TOKEN})", code=401)
    _variable(fragment, "mosaic-key-grant", "")
    _variable(fragment, "mosaic-token-grant", "")
    _variable(fragment, "mosaic-member", "")

    if snapshot.settings.keys_enabled:
        key = ET.SubElement(
            ET.SubElement(fragment, "choose"), "when", {"condition": f"@({_HAS_KEY})"}
        )
        _reject(key, _key_shape_check(), code=401)
        _variable(key, "mosaic-key-grant", _key_lookup(publication, grants))
        _reject(key, f"@(String.IsNullOrEmpty({_KEY_GRANT}))")

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
        _variable(
            token,
            "mosaic-token-grant",
            _token_lookup(
                publication,
                grants,
                delegated_scope=delegated_scope,
                application_role=application_role,
            ),
        )
        _variable(token, "mosaic-member", _token_member_lookup(publication, grants))
        if any(grant.is_group_grant for grant in grants):
            _reject(token, _token_groups_overage(), message=_GROUPS_OVERAGE_DENIED)
        _reject(token, f"@(String.IsNullOrEmpty({_TOKEN_GRANT}))")

    _reject(fragment, f"@({_HAS_KEY} && {_HAS_TOKEN} && {_KEY_GRANT} != {_TOKEN_GRANT})")
    _variable(fragment, "mosaic-grant", f"@({_HAS_KEY} ? {_KEY_GRANT} : {_TOKEN_GRANT})")


def _operation_guard(
    fragment: ET.Element, publication: Publication, operations: tuple[OperationSpec, ...]
) -> None:
    allowed = " || ".join(f"context.Operation.Id == {_literal(op.name)}" for op in operations)
    _reject(
        fragment,
        f'@(context.Operation == null || context.Request.Method != "POST" || !({allowed}))',
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
    identity: str, period: QuotaPeriod, *, per_member: bool, prefix: str = _COUNTER_PREFIX
) -> str:
    key_prefix = _literal(f"{prefix}grant-request-quota:{identity}:{period}:")
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
                    },
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
    for facet in [*fragment_analysis.facets, *api_analysis.facets]:
        facet.managed_by_mosaic = True
        if facet.section == PolicySection.UNKNOWN:
            facet.section = PolicySection.INBOUND
        if facet.element in {"llm-token-limit", "rate-limit-by-key", "quota-by-key"}:
            element = next(limit_elements)
            counter = element.get("counter-key", "")
            namespace = next(
                name
                for name in (
                    "publication-tokens",
                    "grant-tokens",
                    "grant-request-rate",
                    "grant-request-quota",
                )
                if f"{_COUNTER_PREFIX}{name}:" in counter
            )
            facet.summary = re.sub(
                r"counted .+\.$",
                "counted per stable tenant/publication/entitlement grant.",
                facet.summary,
            )
            facet.attributes.update(
                {"counter-scope": "stable-grant", "counter-namespace": namespace}
            )
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
            if facet.element == "quota-by-key":
                period = next(period for period in _PERIOD_LABELS if f":{period}:" in counter)
                facet.summary = (
                    f"Caps requests at {element.get('calls')} per UTC calendar "
                    f"{_PERIOD_LABELS[period]}, "
                    "counted per stable tenant/publication/entitlement grant."
                )
                facet.attributes["calendar-period"] = period
                facet.details.append(
                    "A period-qualified key resets the quota at UTC calendar boundaries."
                )
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
        elif facet.element == "trace":
            describe_grant_attribution_trace(facet, has_group_grants=enabled_group_grants > 0)
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
        _deny(fragment)
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
        removed_headers = ("Ocp-Apim-Subscription-Key", "api-key", "Authorization")
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
