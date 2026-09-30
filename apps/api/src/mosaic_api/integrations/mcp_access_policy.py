"""Compile MCP access grants into APIM policies for a MOSAIC-owned passthrough MCP API."""

import hashlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from mosaic_api.domain import (
    MCP_MESSAGE_PATH,
    MCP_RESOURCE_METADATA_PREFIX,
    EntitlementSubjectKind,
    McpAccessGrant,
    McpAccessSnapshot,
    McpAuthMode,
    McpPublication,
    PolicyFacet,
    PolicyFacetKind,
    PolicySection,
    QuotaPeriod,
    grant_precedence_key,
    mcp_metadata_api_path,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.access_policy import (
    _TOKEN_GRANT,
    _expression,
    _grant_limits,
    _literal,
    _reject,
    _token_groups_overage,
    _token_lookup,
    _token_member_lookup,
    _variable,
    grant_counter_identity,
)
from mosaic_api.integrations.apim.policy_semantics import analyze_policy
from mosaic_api.integrations.policy import _serialize

MAX_FRAGMENT_BYTES = 512 * 1024
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")
_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_API_PATH = re.compile(r"[A-Za-z0-9][A-Za-z0-9/-]*")
_COUNTER_PREFIX = "mosaic:mcp:"
_DENIED = "MCP access denied."
_GROUPS_OVERAGE_DENIED = (
    "MCP access denied. Your token doesn't list your groups because you belong to too many; "
    "ask an administrator for a direct grant."
)
_PERIOD_LABELS: dict[QuotaPeriod, str] = {
    "Hourly": "hour",
    "Daily": "day",
    "Weekly": "week (starting Monday)",
    "Monthly": "month",
    "Yearly": "year",
}


@dataclass(frozen=True)
class McpPolicyDocuments:
    fragment_xml: str
    api_policy_xml: str
    metadata_policy_xml: str
    content_sha256: str
    facets: list[PolicyFacet]
    unrecognized_elements: list[str]


def mcp_grant_counter_identity(publication: McpPublication, grant: McpAccessGrant) -> str:
    return grant_counter_identity(publication, grant)


def _validate(
    publication: McpPublication,
    snapshot: McpAccessSnapshot,
    *,
    backend_auth: McpAuthMode,
    backend_audience: str | None,
) -> None:
    if not _GUID.fullmatch(publication.tenant_id):
        raise ValidationError("MCP access policies require a specific tenant GUID.")
    if not _GUID.fullmatch(snapshot.audience):
        raise ValidationError(
            "MCP access policies require the runtime application's GUID audience."
        )
    if backend_auth == McpAuthMode.API_KEY:
        raise ValidationError(
            "MOSAIC can't forward an API key to an MCP server yet; register it with managed "
            "identity or no authentication."
        )
    if backend_auth == McpAuthMode.MANAGED_IDENTITY and not backend_audience:
        raise ValidationError("Managed identity MCP backends require a resource audience.")
    if not all(
        _RESOURCE_NAME.fullmatch(name)
        for name in (
            publication.api_name,
            publication.backend_name,
            publication.fragment_name,
            publication.metadata_api_name,
        )
    ):
        raise ValidationError("MCP policies require literal APIM resource names.")
    path = publication.api_path.strip()
    if (
        path != publication.api_path
        or not _API_PATH.fullmatch(path)
        or path.casefold().startswith(f"{MCP_RESOURCE_METADATA_PREFIX}/")
        or path.casefold() == MCP_RESOURCE_METADATA_PREFIX
        or "//" in path
        or path.endswith("/")
    ):
        raise ValidationError(
            "MCP policies require an API path made of letters, digits, hyphens and slashes that "
            "doesn't start with .well-known."
        )
    seen: set[str] = set()
    for grant in snapshot.grants:
        if grant.subject.kind not in {
            EntitlementSubjectKind.USER,
            EntitlementSubjectKind.APPLICATION,
            EntitlementSubjectKind.SECURITY_GROUP,
        }:
            raise ValidationError(
                "MCP access policies support only user, application and security-group grants."
            )
        if not grant.entitlement_id.strip() or grant.entitlement_id.casefold() in seen:
            raise ValidationError("MCP access grants must have nonempty, unambiguous identities.")
        seen.add(grant.entitlement_id.casefold())
        if not _GUID.fullmatch(grant.object_id):
            raise ValidationError("MCP access grants require GUID object IDs.")
        if grant.enforcement is not None and grant.enforcement.tokens is not None:
            raise ValidationError(
                "MCP servers are limited by calls, not tokens. Remove the token limits."
            )
        requests = grant.enforcement.requests if grant.enforcement is not None else None
        if (
            requests is not None
            and requests.renewal_period_seconds is not None
            and requests.renewal_period_seconds > 300
        ):
            raise ValidationError(
                "Governed request rate renewal must not exceed 300 seconds. Use a call quota "
                "for longer periods."
            )


def _runtime_origin_lines() -> list[str]:
    return [
        "var url = context.Request.OriginalUrl;",
        'var port = url.Port == 80 || url.Port == 443 ? "" : ":" + url.Port.ToString();',
        'var origin = url.Scheme + "://" + url.Host + port;',
    ]


def _runtime_url(path: str) -> str:
    return _expression(
        [
            *_runtime_origin_lines(),
            f"return origin + {_literal('/' + path.strip('/'))};",
        ]
    )


def _auth_header_value(path: str, prefix: str, *, suffix: str = "") -> str:
    return _expression(
        [
            *_runtime_origin_lines(),
            f"var metadata = origin + {_literal('/' + path.strip('/'))};",
            f"return {_literal(prefix)} + metadata + {_literal(suffix)};",
        ]
    )


def _set_www_authenticate(parent: ET.Element, value: str) -> None:
    header = ET.SubElement(
        parent,
        "set-header",
        {"name": "WWW-Authenticate", "exists-action": "override"},
    )
    ET.SubElement(header, "value").text = value


def _deny_with_auth(
    parent: ET.Element,
    *,
    code: int,
    message: str,
    www_authenticate: str,
) -> None:
    response = ET.SubElement(parent, "return-response")
    ET.SubElement(
        response,
        "set-status",
        {"code": str(code), "reason": "Unauthorized" if code == 401 else "Forbidden"},
    )
    _set_www_authenticate(response, www_authenticate)
    ET.SubElement(response, "set-body").text = message


def _reject_with_auth(
    parent: ET.Element,
    condition: str,
    *,
    code: int,
    message: str,
    www_authenticate: str,
) -> None:
    when = ET.SubElement(ET.SubElement(parent, "choose"), "when", {"condition": condition})
    _deny_with_auth(when, code=code, message=message, www_authenticate=www_authenticate)


def _authorization_shape_invalid() -> str:
    return _expression(
        [
            'var values = context.Request.Headers["Authorization"];',
            "if (values == null || values.Length != 1) return true;",
            "var authorization = values[0];",
            "return String.IsNullOrWhiteSpace(authorization)"
            ' || !authorization.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)'
            " || String.IsNullOrWhiteSpace(authorization.Substring(7));",
        ]
    )


def _mcp_authentication(
    fragment: ET.Element,
    publication: McpPublication,
    snapshot: McpAccessSnapshot,
    grants: list[McpAccessGrant],
) -> None:
    metadata_path = f"{mcp_metadata_api_path(publication.api_path)}/{MCP_MESSAGE_PATH}"
    no_auth = _auth_header_value(metadata_path, 'Bearer resource_metadata="', suffix='"')
    invalid = _auth_header_value(
        metadata_path, 'Bearer error="invalid_token", resource_metadata="', suffix='"'
    )
    insufficient = _auth_header_value(
        metadata_path,
        (
            'Bearer error="insufficient_scope", '
            f'scope="api://{snapshot.audience}/{snapshot.delegated_scope}", '
            'resource_metadata="'
        ),
        suffix='"',
    )
    _reject_with_auth(
        fragment,
        '@(!context.Request.Headers.ContainsKey("Authorization"))',
        code=401,
        message=_DENIED,
        www_authenticate=no_auth,
    )
    _reject_with_auth(
        fragment,
        _authorization_shape_invalid(),
        code=401,
        message=_DENIED,
        www_authenticate=invalid,
    )
    _variable(fragment, "mosaic-token-grant", "")
    _variable(fragment, "mosaic-member", "")
    validation = ET.SubElement(
        fragment,
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
        {"name": "ver", "match": "all"},
    )
    ET.SubElement(claim, "value").text = "2.0"
    _variable(
        fragment,
        "mosaic-token-grant",
        _token_lookup(
            publication,
            grants,
            delegated_scope=snapshot.delegated_scope,
            application_role=snapshot.application_role,
        ),
    )
    _variable(fragment, "mosaic-member", _token_member_lookup(publication, grants))
    if any(grant.is_group_grant for grant in grants):
        _reject(fragment, _token_groups_overage(), message=_GROUPS_OVERAGE_DENIED)
    _reject_with_auth(
        fragment,
        f"@(String.IsNullOrEmpty({_TOKEN_GRANT}))",
        code=403,
        message=_DENIED,
        www_authenticate=insufficient,
    )
    _variable(fragment, "mosaic-grant", f"@({_TOKEN_GRANT})")


def _strip_credentials(fragment: ET.Element) -> None:
    for name in ("Authorization", "Ocp-Apim-Subscription-Key", "api-key"):
        ET.SubElement(fragment, "set-header", {"name": name, "exists-action": "delete"})
    ET.SubElement(
        fragment,
        "set-query-parameter",
        {"name": "subscription-key", "exists-action": "delete"},
    )


def _api_policy(publication: McpPublication) -> ET.Element:
    policies = ET.Element("policies")
    inbound = ET.SubElement(policies, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "include-fragment", {"fragment-id": publication.fragment_name})
    ET.SubElement(ET.SubElement(policies, "backend"), "base")
    ET.SubElement(ET.SubElement(policies, "outbound"), "base")
    on_error = ET.SubElement(policies, "on-error")
    ET.SubElement(on_error, "base")
    choose = ET.SubElement(on_error, "choose")
    when = ET.SubElement(
        choose,
        "when",
        {"condition": "@(context.Response != null && context.Response.StatusCode == 401)"},
    )
    _set_www_authenticate(
        when,
        _auth_header_value(
            f"{mcp_metadata_api_path(publication.api_path)}/{MCP_MESSAGE_PATH}",
            'Bearer error="invalid_token", resource_metadata="',
            suffix='"',
        ),
    )
    return policies


def _metadata_body(publication: McpPublication, snapshot: McpAccessSnapshot) -> str:
    return _expression(
        [
            *_runtime_origin_lines(),
            f"var resource = origin + {_literal(f'/{publication.api_path}/{MCP_MESSAGE_PATH}')};",
            "var document = new JObject();",
            'document["resource"] = resource;',
            f'document["authorization_servers"] = new JArray({_literal(f"https://login.microsoftonline.com/{publication.tenant_id}/v2.0")});',
            f'document["bearer_methods_supported"] = new JArray({_literal("header")});',
            f'document["scopes_supported"] = new JArray({_literal(f"api://{snapshot.audience}/{snapshot.delegated_scope}")});',
            "return document.ToString(Newtonsoft.Json.Formatting.None);",
        ]
    )


def _metadata_policy(publication: McpPublication, snapshot: McpAccessSnapshot) -> ET.Element:
    policies = ET.Element("policies")
    inbound = ET.SubElement(policies, "inbound")
    ET.SubElement(inbound, "base")
    response = ET.SubElement(inbound, "return-response")
    ET.SubElement(response, "set-status", {"code": "200", "reason": "OK"})
    content = ET.SubElement(
        response,
        "set-header",
        {"name": "Content-Type", "exists-action": "override"},
    )
    ET.SubElement(content, "value").text = "application/json"
    cache = ET.SubElement(
        response,
        "set-header",
        {"name": "Cache-Control", "exists-action": "override"},
    )
    ET.SubElement(cache, "value").text = "public, max-age=3600"
    ET.SubElement(response, "set-body").text = _metadata_body(publication, snapshot)
    for section in ("backend", "outbound", "on-error"):
        ET.SubElement(ET.SubElement(policies, section), "base")
    return policies


def _facets(
    publication: McpPublication,
    snapshot: McpAccessSnapshot,
    grants: list[McpAccessGrant],
    fragment: ET.Element,
    api: ET.Element,
    metadata: ET.Element,
    backend_auth: McpAuthMode,
) -> tuple[list[PolicyFacet], list[str]]:
    enabled_group_grants = sum(grant.is_group_grant for grant in grants)
    facets = [
        PolicyFacet(
            kind=PolicyFacetKind.AUTHORIZATION,
            element="validate-azure-ad-token",
            section=PolicySection.INBOUND,
            summary="Requires a validated Microsoft Entra bearer token for MCP access.",
            details=[
                f"Delegated tokens require {snapshot.delegated_scope} in scp.",
                f"Application tokens require {snapshot.application_role} in roles with no real "
                "scp claim.",
                "Direct user and application grants are checked before security-group grants.",
            ],
            attributes={
                "enabled-grants": str(len(grants)),
                "security-group-grants": str(enabled_group_grants),
            },
            managed_by_mosaic=True,
        ),
        PolicyFacet(
            kind=PolicyFacetKind.TRANSFORMATION,
            element="set-header",
            section=PolicySection.INBOUND,
            summary="Removes caller credentials before forwarding to the MCP backend.",
            details=[
                "Authorization, Ocp-Apim-Subscription-Key, api-key and subscription-key are "
                "stripped."
            ],
            managed_by_mosaic=True,
        ),
        PolicyFacet(
            kind=PolicyFacetKind.AUTHENTICATION,
            element="authentication-managed-identity"
            if backend_auth == McpAuthMode.MANAGED_IDENTITY
            else "none",
            section=PolicySection.INBOUND,
            summary="The gateway authenticates to the MCP backend with managed identity."
            if backend_auth == McpAuthMode.MANAGED_IDENTITY
            else "The gateway forwards to the MCP backend without attaching a credential.",
            attributes={"resource": "[redacted]"}
            if backend_auth == McpAuthMode.MANAGED_IDENTITY
            else {},
            managed_by_mosaic=True,
        ),
        PolicyFacet(
            kind=PolicyFacetKind.ROUTING,
            element="return-response",
            section=PolicySection.INBOUND,
            summary="Serves OAuth protected resource metadata for MCP client discovery.",
            details=[
                "The document advertises the tenant authorization server, header bearer method "
                "and MCP scope."
            ],
            attributes={
                "metadata-path": f"{mcp_metadata_api_path(publication.api_path)}/{MCP_MESSAGE_PATH}"
            },
            managed_by_mosaic=True,
        ),
    ]
    for grant in grants:
        if grant.enforcement is None:
            facets.append(
                PolicyFacet(
                    kind=PolicyFacetKind.AUTHORIZATION,
                    element="grant",
                    section=PolicySection.INBOUND,
                    summary="Allows one enabled MCP grant without per-grant call limits.",
                    attributes={"grant": "[redacted]"},
                    managed_by_mosaic=True,
                )
            )
            continue
        requests = grant.enforcement.requests
        if requests is None:
            continue
        details = (
            ["Security-group grant counters include the validated member object ID."]
            if grant.is_group_grant
            else []
        )
        if requests.calls is not None:
            facets.append(
                PolicyFacet(
                    kind=PolicyFacetKind.RATE_LIMIT,
                    element="rate-limit-by-key",
                    section=PolicySection.INBOUND,
                    summary=(
                        f"Allows {requests.calls} MCP calls per "
                        f"{requests.renewal_period_seconds} seconds for one enabled grant."
                    ),
                    details=details,
                    attributes={"counter-scope": "stable-grant", "counter-prefix": _COUNTER_PREFIX},
                    managed_by_mosaic=True,
                )
            )
        if requests.call_quota is not None and requests.call_quota_period is not None:
            facets.append(
                PolicyFacet(
                    kind=PolicyFacetKind.QUOTA,
                    element="quota-by-key",
                    section=PolicySection.INBOUND,
                    summary=(
                        f"Caps MCP calls at {requests.call_quota} per UTC calendar "
                        f"{_PERIOD_LABELS[requests.call_quota_period]} for one enabled grant."
                    ),
                    details=[
                        *details,
                        "A period-qualified key resets the quota at UTC calendar boundaries.",
                    ],
                    attributes={
                        "counter-scope": "stable-grant",
                        "counter-prefix": _COUNTER_PREFIX,
                        "calendar-period": requests.call_quota_period,
                    },
                    managed_by_mosaic=True,
                )
            )
    analyses = [analyze_policy(_serialize(element)) for element in (fragment, api, metadata)]
    for analysis in analyses:
        for facet in analysis.facets:
            facet.managed_by_mosaic = True
    unrecognized = sorted(
        {item for analysis in analyses for item in analysis.unrecognized_elements}
    )
    return [*facets, *(facet for analysis in analyses for facet in analysis.facets)], unrecognized


def render_mcp_policy(
    publication: McpPublication,
    snapshot: McpAccessSnapshot,
    *,
    backend_auth: McpAuthMode,
    backend_audience: str | None,
) -> McpPolicyDocuments:
    """Return MCP APIM policy XML and redacted facets; reject unsafe intent."""

    _validate(
        publication,
        snapshot,
        backend_auth=backend_auth,
        backend_audience=backend_audience,
    )
    grants = sorted(
        (grant for grant in snapshot.grants if grant.enabled),
        key=lambda grant: grant.entitlement_id,
    )
    group_grants = sorted(
        (grant for grant in grants if grant.is_group_grant),
        key=lambda grant: grant_precedence_key(grant.enforcement, grant.entitlement_id),
    )
    grants_for_lookup = [*(grant for grant in grants if not grant.is_group_grant), *group_grants]
    fragment = ET.Element("fragment")
    _mcp_authentication(fragment, publication, snapshot, grants_for_lookup)
    _grant_limits(fragment, publication, grants, prefix=_COUNTER_PREFIX)
    _strip_credentials(fragment)
    if backend_auth == McpAuthMode.MANAGED_IDENTITY:
        assert backend_audience is not None
        ET.SubElement(fragment, "authentication-managed-identity", {"resource": backend_audience})
    fragment_xml = _serialize(fragment)
    if len(fragment_xml.encode("utf-8")) > MAX_FRAGMENT_BYTES:
        raise ValidationError("The MCP policy fragment exceeds APIM's 512 KB UTF-8 size limit.")
    api = _api_policy(publication)
    metadata = _metadata_policy(publication, snapshot)
    api_policy_xml = _serialize(api)
    metadata_policy_xml = _serialize(metadata)
    facets, unrecognized = _facets(
        publication,
        snapshot,
        grants,
        fragment,
        api,
        metadata,
        backend_auth,
    )
    return McpPolicyDocuments(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        metadata_policy_xml=metadata_policy_xml,
        content_sha256=hashlib.sha256(
            f"{fragment_xml}\n{api_policy_xml}\n{metadata_policy_xml}".encode()
        ).hexdigest(),
        facets=facets,
        unrecognized_elements=unrecognized,
    )
