"""Compile governed model access into an APIM fragment, never a per-request control-plane call.

Only native APIM subscription validation and a validated Entra JWT establish a grant. The
compiler deliberately replaces legacy subscription counters with credential-independent grant
counters. APIM's distributed limits are safeguards, not an exact billing ledger.

Only curated chat-completions and responses operations are forwarded. Routes without a deployment
in their path require a matching request model. Request quotas use UTC calendar keys; weeks start
on Monday.
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET

from mosaic_api.domain import (
    EntitlementSubjectKind,
    ModelAccessGrant,
    ModelAccessSnapshot,
    PolicyFacet,
    PolicyFacetKind,
    PolicySection,
    Publication,
    QuotaPeriod,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.apim.model_apis import OperationSpec, curated_operations
from mosaic_api.integrations.apim.policy_semantics import analyze_policy
from mosaic_api.integrations.policy import (
    MANAGED_IDENTITY_RESOURCE,
    METRIC_NAMESPACE,
    PublicationPolicy,
    _serialize,
    _token_limit_attributes,
)

MAX_FRAGMENT_BYTES = 512 * 1024
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SUPPORTED_OPERATIONS = frozenset({"chat-completions", "responses"})
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
_TOKEN_GRANT = '(string)context.Variables["mosaic-token-grant"]'
_DENIED = "Model access denied."
_PERIOD_LABELS: dict[QuotaPeriod, str] = {
    "Hourly": "hour",
    "Daily": "day",
    "Weekly": "week (starting Monday)",
    "Monthly": "month",
    "Yearly": "year",
}


def _literal(value: str) -> str:
    # C# and JSON share these string escapes. Escape braces as well so APIM cannot expand a
    # named-value reference inside an otherwise correctly quoted C# string.
    return json.dumps(value, ensure_ascii=True).replace("{", r"\u007b").replace("}", r"\u007d")


def _expression(lines: list[str]) -> str:
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


def grant_counter_identity(publication: Publication, grant: ModelAccessGrant) -> str:
    """The shared credential-independent identity, before each native policy's namespace."""
    identity = json.dumps(
        [publication.tenant_id, publication.id, grant.entitlement_id],
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _validate(publication: Publication, snapshot: ModelAccessSnapshot) -> None:
    if not all(
        _RESOURCE_NAME.fullmatch(name)
        for name in (publication.backend_name, publication.fragment_name)
    ):
        raise ValidationError("Governed policies require literal APIM backend and fragment names.")
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
    counters = [snapshot.publication_enforcement.counter_key_expression]
    for grant in snapshot.grants:
        if grant.subject.kind not in {
            EntitlementSubjectKind.USER,
            EntitlementSubjectKind.APPLICATION,
        }:
            raise ValidationError(
                "Governed policies support only direct user and application grants."
            )
        identities = {
            "entitlement": grant.entitlement_id,
            "subscription": grant.subscription_name,
            "object": grant.object_id,
            "subject": grant.subject.id,
        }
        for kind, identity in identities.items():
            if not identity.strip() or identity.casefold() in seen[kind]:
                raise ValidationError(
                    "Governed access grants must have nonempty, unambiguous identities."
                )
            seen[kind].add(identity.casefold())
        if grant.subscription_name.casefold() in {
            "master",
            publication.subscription_name.casefold(),
        }:
            raise ValidationError(
                "A governed grant cannot use the all-access or publication bootstrap subscription."
            )
        if grant.enabled and grant.enforcement:
            if grant.enforcement.tokens:
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
            " return true;",
            "    header = values[0];",
            "}",
            'if (query.ContainsKey("subscription-key")) {',
            '    var values = query["subscription-key"];',
            "    if (values == null || values.Length != 1 || String.IsNullOrWhiteSpace(values[0]))"
            " return true;",
            "    parameter = values[0];",
            "}",
            "if (header != null && parameter != null &&"
            " !String.Equals(header, parameter, StringComparison.Ordinal)) return true;",
            "return context.Subscription == null || String.IsNullOrEmpty(context.Subscription.Id);",
        ]
    )


def _key_lookup(publication: Publication, grants: list[ModelAccessGrant]) -> str:
    lines = [
        'if (context.Subscription == null) return "";',
        "var subscription = context.Subscription.Id;",
    ]
    for grant in grants:
        lines.append(
            f"if (String.Equals(subscription, {_literal(grant.subscription_name)}, "
            "StringComparison.OrdinalIgnoreCase)) "
            f"return {_literal(grant_counter_identity(publication, grant))};"
        )
    return _expression([*lines, 'return "";'])


def _token_lookup(publication: Publication, grants: list[ModelAccessGrant]) -> str:
    lines = [
        'var jwt = context.Variables.ContainsKey("mosaic-validated-token")'
        ' ? context.Variables["mosaic-validated-token"] as Jwt : null;',
        'if (jwt == null || jwt.Claims == null || !jwt.Claims.ContainsKey("oid")) return "";',
        'var objects = jwt.Claims["oid"];',
        "if (objects == null || objects.Length != 1 || String.IsNullOrWhiteSpace(objects[0]))"
        ' return "";',
        "var oid = objects[0];",
        "bool delegated = false;",
        "bool application = false;",
        'if (jwt.Claims.ContainsKey("scp")) {',
        '    var scopes = jwt.Claims["scp"];',
        "    delegated = scopes != null && scopes.Length == 1 && scopes[0] != null"
        " && scopes[0].Split(' ').Contains(\"Models.Invoke\");",
        '} else if (jwt.Claims.ContainsKey("roles")) {',
        '    var roles = jwt.Claims["roles"];',
        '    application = roles != null && roles.Contains("Models.Invoke.Application");',
        "}",
    ]
    for grant in grants:
        kind = "delegated" if grant.subject.kind == EntitlementSubjectKind.USER else "application"
        lines.append(
            f"if ({kind} && String.Equals(oid, {_literal(grant.object_id)}, "
            "StringComparison.OrdinalIgnoreCase)) "
            f"return {_literal(grant_counter_identity(publication, grant))};"
        )
    return _expression([*lines, 'return "";'])


def _authentication(
    fragment: ET.Element,
    publication: Publication,
    snapshot: ModelAccessSnapshot,
    grants: list[ModelAccessGrant],
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
                    "if (values == null || values.Length != 1) return true;",
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
        _variable(token, "mosaic-token-grant", _token_lookup(publication, grants))
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
        message="This operation is not available with governed token limits.",
    )
    unscoped = [op for op in operations if not op.url_template.startswith("/openai/deployments/")]
    if unscoped:
        needs_model = " || ".join(f"context.Operation.Id == {_literal(op.name)}" for op in unscoped)
        _reject(
            fragment,
            _expression(
                [
                    f"if (!({needs_model})) return false;",
                    "if (context.Request.Body == null) return true;",
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


def _calendar_counter(identity: str, period: QuotaPeriod) -> str:
    prefix = _literal(f"{_COUNTER_PREFIX}grant-request-quota:{identity}:{period}:")
    if period == "Weekly":
        date = "now.Date.AddDays(-(((int)now.DayOfWeek + 6) % 7))"
    else:
        date = "now"
    length = {"Hourly": 13, "Daily": 10, "Weekly": 10, "Monthly": 7, "Yearly": 4}[period]
    # "u" is the invariant Gregorian format. CultureInfo is not in APIM's expression allowlist.
    return _expression(
        [
            "var now = DateTime.UtcNow;",
            f'return {prefix} + {date}.ToString("u").Substring(0, {length});',
        ]
    )


def _limits(
    fragment: ET.Element,
    publication: Publication,
    snapshot: ModelAccessSnapshot,
    grants: list[ModelAccessGrant],
) -> None:
    limited = [grant for grant in grants if grant.enforcement is not None]
    if limited:
        choose = ET.SubElement(fragment, "choose")
        for grant in limited:
            identity = grant_counter_identity(publication, grant)
            when = ET.SubElement(
                choose, "when", {"condition": f"@({_GRANT} == {_literal(identity)})"}
            )
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
                            "counter-key": f"{_COUNTER_PREFIX}grant-request-rate:{identity}",
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
                            "counter-key": _calendar_counter(identity, requests.call_quota_period),
                        },
                    )
            if tokens := enforcement.tokens:
                ET.SubElement(
                    when,
                    "llm-token-limit",
                    {
                        **_token_limit_attributes(tokens),
                        "counter-key": f"{_COUNTER_PREFIX}grant-tokens:{identity}",
                    },
                )
    ET.SubElement(
        fragment,
        "llm-token-limit",
        {
            **_token_limit_attributes(snapshot.publication_enforcement),
            "counter-key": f'@("{_COUNTER_PREFIX}publication-tokens:" + {_GRANT})',
        },
    )


def _facets(
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
    facets = [
        PolicyFacet(
            kind=PolicyFacetKind.AUTHORIZATION,
            element="choose",
            section=PolicySection.INBOUND,
            summary=f"Requires {' or '.join(methods)}."
            if methods
            else "All model access is denied.",
            details=[
                "Every presented credential must be enabled and valid; there is no fallback.",
                "When a key and a token are both presented, they must resolve to the same "
                "enabled grant.",
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
            ],
            attributes={
                "keys-enabled": str(snapshot.settings.keys_enabled).lower(),
                "entra-enabled": str(snapshot.settings.entra_enabled).lower(),
                "enabled-grants": str(sum(grant.enabled for grant in snapshot.grants)),
            },
            managed_by_mosaic=True,
        ),
        PolicyFacet(
            kind=PolicyFacetKind.AUTHORIZATION,
            element="choose",
            section=PolicySection.INBOUND,
            summary="Governed token limits permit only supported chat-completions and "
            "responses operations.",
            details=[
                "Allowed curated operation IDs: " + ", ".join(op.name for op in operations) + ".",
                "All other operations, including embeddings, image, audio and legacy completions, "
                "are denied.",
                "Routes not scoped to a deployment require the request model to equal this "
                "publication's deployment; malformed or missing model values are denied "
                "without forwarding.",
            ],
            attributes={"allowed-operations": ",".join(op.name for op in operations)},
            managed_by_mosaic=True,
        ),
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
            facet.summary = "Removes the subscription-key query parameter before forwarding."
        facets.append(facet)
    return facets, sorted(
        set(fragment_analysis.unrecognized_elements + api_analysis.unrecognized_elements)
    )


def governed_operations(publication: Publication) -> tuple[OperationSpec, ...]:
    operations = tuple(
        op
        for op in curated_operations(publication.provider, publication.deployment_name)
        if op.name in _SUPPORTED_OPERATIONS and op.method == "POST"
    )
    if not operations:
        raise ValidationError("This provider has no operations supported by governed token limits.")
    return operations


def render_governed_policy(
    publication: Publication, snapshot: ModelAccessSnapshot
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
        _authentication(fragment, publication, snapshot, grants)
        _operation_guard(fragment, publication, operations)
        _limits(fragment, publication, snapshot, grants)
        for name in ("Ocp-Apim-Subscription-Key", "api-key", "Authorization"):
            ET.SubElement(fragment, "set-header", {"name": name, "exists-action": "delete"})
        ET.SubElement(
            fragment,
            "set-query-parameter",
            {
                "name": "subscription-key",
                "exists-action": "delete",
            },
        )
        ET.SubElement(
            fragment,
            "authentication-managed-identity",
            {
                "resource": MANAGED_IDENTITY_RESOURCE,
            },
        )
        ET.SubElement(fragment, "set-backend-service", {"backend-id": publication.backend_name})
        metric = ET.SubElement(fragment, "llm-emit-token-metric", {"namespace": METRIC_NAMESPACE})
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
    facets, unrecognized = _facets(snapshot, fragment, policies, operations)
    return PublicationPolicy(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        content_sha256=hashlib.sha256(f"{fragment_xml}\n{api_policy_xml}".encode()).hexdigest(),
        facets=facets,
        unrecognized_elements=unrecognized,
    )
