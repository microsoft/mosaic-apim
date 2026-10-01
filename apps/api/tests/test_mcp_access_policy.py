import hashlib
import re
import xml.etree.ElementTree as ET

import pytest
from apim_double import expression_error, policy_expression_error
from mosaic_api.domain import (
    EntitlementEnforcement,
    EntitlementSubject,
    EntitlementSubjectKind,
    McpAccessGrant,
    McpAccessSnapshot,
    McpAuthMode,
    McpPublication,
    PublicationStatus,
    QuotaPeriod,
    RequestEnforcement,
    TokenEnforcement,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations import mcp_access_policy
from mosaic_api.integrations.mcp_access_policy import (
    mcp_grant_counter_identity,
    render_mcp_policy,
)
from test_access_policy import _guarded_metadata

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"
# The wire format a Log Analytics source parses from TraceRecords.
_ATTRIBUTION_MESSAGE = (
    '@("mosaic-attribution v=1 g=" + (string)context.Variables["mosaic-grant"]'
    ' + " m=" + (string)context.Variables["mosaic-member"]'
    ' + " a=" + (string)context.Variables["mosaic-client"])'
)


def _attribution_trace(fragment: ET.Element) -> ET.Element:
    return next(
        element
        for element in fragment.findall("trace")
        if "mosaic-attribution" in (element.findtext("message") or "")
    )


def _denial_reasons(fragment: ET.Element) -> dict[str, str]:
    reasons: dict[str, str] = {}
    for trace in fragment.iter("trace"):
        message = trace.findtext("message") or ""
        match = re.search(r"mosaic-deny v=1 r=([a-z-]+)", message)
        if match:
            reasons[match.group(1)] = message
    return reasons


def _publication(**overrides: object) -> McpPublication:
    return McpPublication.model_validate(
        {
            "id": "mcp-publication",
            "tenant_id": TENANT,
            "gateway_id": "gateway",
            "mcp_endpoint_id": "endpoint",
            "display_name": "Weather MCP",
            "api_name": "mosaic-mcp-weather",
            "api_path": "mosaic/mcp/weather",
            "backend_name": "mosaic-mcp-weather",
            "fragment_name": "mosaic-mcp-weather",
            "metadata_api_name": "mosaic-mcp-weather-prm",
            "mcp_server_id": "mcp-server",
            "status": PublicationStatus.DRAFT,
            **overrides,
        }
    )


def _requests(**overrides: object) -> RequestEnforcement:
    return RequestEnforcement.model_validate(
        {
            "counter_key_expression": SUBSCRIPTION_COUNTER,
            "calls": 10,
            "renewal_period_seconds": 60,
            **overrides,
        }
    )


def _enforcement(**overrides: object) -> EntitlementEnforcement:
    return EntitlementEnforcement.model_validate({"requests": _requests(**overrides)})


def _grant(number: int = 1, **overrides: object) -> McpAccessGrant:
    return McpAccessGrant.model_validate(
        {
            "entitlement_id": f"entitlement-{number}",
            "subject": EntitlementSubject(
                kind=EntitlementSubjectKind.USER, id=f"principal-{number}"
            ),
            "object_id": f"{number:08x}-3333-3333-3333-333333333333",
            "display_name": f"Caller {number}",
            "enabled": True,
            "intent_digest": f"intent-{number}",
            **overrides,
        }
    )


def _group_grant(number: int = 1, **overrides: object) -> McpAccessGrant:
    values: dict[str, object] = {
        "subject": EntitlementSubject(
            kind=EntitlementSubjectKind.SECURITY_GROUP, id=f"group-principal-{number}"
        ),
        "object_id": f"{number:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "display_name": f"Group {number}",
    }
    values.update(overrides)
    return _grant(number, **values)


def _app_grant(number: int = 2, **overrides: object) -> McpAccessGrant:
    return _grant(
        number,
        subject=EntitlementSubject(
            kind=EntitlementSubjectKind.APPLICATION, id=f"application-{number}"
        ),
        **overrides,
    )


def _snapshot(**overrides: object) -> McpAccessSnapshot:
    return McpAccessSnapshot.model_validate(
        {
            "version": 1,
            "audience": AUDIENCE,
            "grants": [_grant()],
            **overrides,
        }
    )


def _render(
    publication: McpPublication | None = None,
    snapshot: McpAccessSnapshot | None = None,
    *,
    backend_auth: McpAuthMode = McpAuthMode.NONE,
    backend_audience: str | None = None,
):
    return render_mcp_policy(
        publication or _publication(),
        snapshot or _snapshot(),
        backend_auth=backend_auth,
        backend_audience=backend_audience,
    )


def _fragment(snapshot: McpAccessSnapshot | None = None) -> ET.Element:
    return ET.fromstring(_render(snapshot=snapshot).fragment_xml)


def _conditions(fragment: ET.Element) -> list[str]:
    return [when.attrib["condition"] for when in fragment.iter("when")]


def _variable_values(fragment: ET.Element, name: str) -> list[str]:
    return [
        variable.attrib["value"]
        for variable in fragment.findall(f".//set-variable[@name='{name}']")
    ]


def _header_values(root: ET.Element, name: str) -> list[str]:
    return [
        value.text or ""
        for header in root.findall(f".//set-header[@name='{name}']")
        for value in header.findall("value")
    ]


def _counters(fragment: ET.Element) -> list[str]:
    return [
        element.attrib["counter-key"]
        for element in fragment.iter()
        if "counter-key" in element.attrib
    ]


def test_grant_trace_is_after_authentication_before_limits_and_members_only_for_groups() -> None:
    group = _group_grant(2, enforcement=_enforcement())
    result = _render(snapshot=_snapshot(grants=[_grant(enforcement=_enforcement()), group]))
    fragment = ET.fromstring(result.fragment_xml)
    children = list(fragment)
    trace = _attribution_trace(fragment)

    grant_variable_index = next(
        index
        for index, element in enumerate(children)
        if element.tag == "set-variable" and element.attrib["name"] == "mosaic-grant"
    )
    limit_index = next(
        index
        for index, element in enumerate(children)
        if element.tag == "choose" and element.find("when/rate-limit-by-key") is not None
    )
    attribution_traces = [
        element
        for element in fragment.iter("trace")
        if "mosaic-attribution" in (element.findtext("message") or "")
    ]
    assert attribution_traces == [trace]
    assert grant_variable_index < children.index(trace) < limit_index
    member_index = next(
        index
        for index, element in enumerate(children)
        if element.tag == "set-variable" and element.attrib["name"] == "mosaic-member"
    )
    assert member_index < children.index(trace)
    assert trace.findtext("message") == _ATTRIBUTION_MESSAGE
    metadata = {item.attrib["name"]: item.attrib["value"] for item in trace.findall("metadata")}
    assert metadata == {
        name: _guarded_metadata(name)
        for name in ("mosaic-grant", "mosaic-member", "mosaic-client")
    }
    assert any(
        facet.element == "trace"
        and "MOSAIC grant" in facet.summary
        and "validated object ID" in " ".join(facet.details)
        for facet in result.facets
    )

    direct_trace = _attribution_trace(ET.fromstring(_render().fragment_xml))
    assert direct_trace.findtext("message") == _ATTRIBUTION_MESSAGE
    assert [item.attrib["name"] for item in direct_trace.findall("metadata")] == [
        "mosaic-grant",
        "mosaic-client",
    ]


def test_every_mcp_refusal_records_a_fixed_reason_before_it_responds() -> None:
    fragment = _fragment(_snapshot(grants=[_grant(), _group_grant(2)]))
    reasons = _denial_reasons(fragment)
    assert set(reasons) == {
        "no-credential",
        "token-malformed",
        "cost-center",
        "groups-overage",
        "no-grant",
        "budget-list",
        "budget",
    }
    assert reasons["no-credential"] == "mosaic-deny v=1 r=no-credential"
    assert reasons["no-grant"] == (
        '@("mosaic-deny v=1 r=no-grant o=" + (string)context.Variables["mosaic-caller"]'
        ' + " a=" + (string)context.Variables["mosaic-client"])'
    )
    for when in fragment.iter("when"):
        response = when.find("return-response")
        if response is None:
            continue
        children = list(when)
        trace_index = children.index(response) - 1
        assert trace_index >= 0 and children[trace_index].tag == "trace"
    result = _render(snapshot=_snapshot(grants=[_grant(), _group_grant(2)]))
    trace_facets = [facet for facet in result.facets if facet.element == "trace"]
    assert [facet.attributes.get("trace") for facet in trace_facets].count("denial") == 1


def test_documents_parse_are_deterministic_and_digest_all_three_documents() -> None:
    first = _render()
    second = _render()

    assert first == second
    assert (
        first.content_sha256
        == hashlib.sha256(
            f"{first.fragment_xml}\n{first.api_policy_xml}\n{first.metadata_policy_xml}".encode()
        ).hexdigest()
    )
    assert ET.fromstring(first.fragment_xml).tag == "fragment"
    assert ET.fromstring(first.api_policy_xml).tag == "policies"
    assert ET.fromstring(first.metadata_policy_xml).tag == "policies"
    assert "Body" not in first.fragment_xml + first.api_policy_xml + first.metadata_policy_xml
    assert "set-backend-service" not in first.fragment_xml
    assert first.unrecognized_elements == []


def test_expressions_are_ones_api_management_can_parse() -> None:
    result = _render(snapshot=_snapshot(grants=[_grant(), _group_grant(3), _app_grant()]))
    documents = (result.fragment_xml, result.api_policy_xml, result.metadata_policy_xml)

    expressions = [
        value
        for document in documents
        for element in ET.fromstring(document).iter()
        for value in (*element.attrib.values(), element.text or "")
        if value.startswith("@{")
    ]

    # API Management parses each @{ ... } with Razor, which refuses an unbraced control-flow body.
    assert expressions
    errors = {expression: expression_error(expression) for expression in expressions}
    assert errors == dict.fromkeys(expressions)
    assert all(policy_expression_error(document) is None for document in documents)


def test_fragment_order_and_authenticate_challenges_match_contract() -> None:
    fragment = _fragment()

    children = list(fragment)
    assert [child.tag for child in children[:5]] == [
        "set-variable",
        "set-variable",
        "choose",
        "choose",
        "set-variable",
    ]
    assert [child.attrib["name"] for child in children[:2]] == ["mosaic-caller", "mosaic-client"]
    assert '@(!context.Request.Headers.ContainsKey("Authorization"))' in _conditions(fragment)
    assert any("values.Length != 1" in condition for condition in _conditions(fragment))
    values = _header_values(fragment, "WWW-Authenticate")
    assert any("resource_metadata" in value for value in values)
    assert any("invalid_token" in value for value in values)
    assert any("insufficient_scope" in value for value in values)
    assert any(f"api://{AUDIENCE}/Mcp.Invoke" in value for value in values)
    assert all(
        ".well-known/oauth-protected-resource/mosaic/mcp/weather/mcp" in value for value in values
    )
    assert all("context.Request.OriginalUrl" in value for value in values)
    assert all("url.Port == 80 || url.Port == 443" in value for value in values)


def test_validate_entra_token_and_mcp_permission_lookup_are_rendered() -> None:
    fragment = _fragment(_snapshot(grants=[_grant(), _app_grant()]))
    validator = fragment.find("validate-azure-ad-token")

    assert validator is not None
    assert validator.attrib == {
        "tenant-id": TENANT,
        "header-name": "Authorization",
        "failed-validation-httpcode": "401",
        "failed-validation-error-message": "MCP access denied.",
        "output-token-variable-name": "mosaic-validated-token",
    }
    assert validator.findtext("audiences/audience") == AUDIENCE
    assert validator.find("required-claims/claim").attrib == {"name": "ver", "match": "all"}  # type: ignore[union-attr]
    assert validator.findtext("required-claims/claim/value") == "2.0"
    lookup = _variable_values(fragment, "mosaic-token-match")[-1]
    assert '"Mcp.Invoke"' in lookup
    assert '"Mcp.Invoke.Application"' in lookup
    assert '"Models.Invoke"' not in lookup
    assert f'if (delegated && String.Equals(oid, "{_grant().object_id}"' in lookup
    assert f'if (application && String.Equals(oid, "{_app_grant().object_id}"' in lookup


def test_disabled_grants_are_excluded_and_direct_grants_precede_group_grants() -> None:
    publication = _publication()
    disabled = _grant(2, enabled=False)
    direct = _grant(9, entitlement_id="z-direct")
    group = _group_grant(3, entitlement_id="a-group")
    snapshot = _snapshot(grants=[group, disabled, direct])

    fragment = ET.fromstring(_render(publication, snapshot).fragment_xml)
    lookup = _variable_values(fragment, "mosaic-token-match")[-1]

    assert mcp_grant_counter_identity(publication, disabled) not in lookup
    assert lookup.index(mcp_grant_counter_identity(publication, direct)) < lookup.index(
        'jwt.Claims.ContainsKey("groups")'
    )
    assert mcp_grant_counter_identity(publication, group) in lookup


def test_group_grants_use_precedence_and_overage_denial() -> None:
    publication = _publication()
    limited = _group_grant(
        2,
        entitlement_id="a-limited",
        enforcement=_enforcement(calls=1, renewal_period_seconds=60),
    )
    generous = _group_grant(3, entitlement_id="z-unlimited")
    fragment = ET.fromstring(
        _render(publication, _snapshot(grants=[limited, generous])).fragment_xml
    )
    lookup = _variable_values(fragment, "mosaic-token-match")[-1]

    assert lookup.index(mcp_grant_counter_identity(publication, generous)) < lookup.index(
        mcp_grant_counter_identity(publication, limited)
    )
    assert "hasgroups" in _render(publication, _snapshot(grants=[limited])).fragment_xml
    assert (
        "ask an administrator for a direct grant"
        in _render(publication, _snapshot(grants=[limited])).fragment_xml
    )


def test_call_limits_use_mcp_prefix_and_group_counters_are_per_member() -> None:
    publication = _publication()
    direct = _grant(1, enforcement=_enforcement(call_quota=1000, call_quota_period="Monthly"))
    group = _group_grant(2, enforcement=_enforcement(call_quota=2000, call_quota_period="Daily"))

    fragment = ET.fromstring(_render(publication, _snapshot(grants=[direct, group])).fragment_xml)
    counters = _counters(fragment)

    assert all("mosaic:mcp:" in counter for counter in counters)
    assert all("mosaic:governed:" not in counter for counter in counters)
    assert any("grant-request-rate" in counter for counter in counters)
    assert any("grant-request-quota" in counter for counter in counters)
    group_id = mcp_grant_counter_identity(publication, group)
    assert any(f"mosaic:mcp:grant-request-rate:{group_id}:" in counter for counter in counters)
    assert any(
        f"mosaic:mcp:grant-request-quota:{group_id}:Daily:" in counter
        and '(string)context.Variables["mosaic-member"]' in counter
        for counter in counters
    )


def test_credentials_are_stripped_before_managed_identity_is_attached() -> None:
    result = _render(
        backend_auth=McpAuthMode.MANAGED_IDENTITY,
        backend_audience="api://backend",
    )
    fragment = ET.fromstring(result.fragment_xml)
    flat = list(fragment.iter())
    identity = fragment.find("authentication-managed-identity")

    assert identity is not None
    assert identity.attrib == {"resource": "api://backend"}
    for name in ("Authorization", "Ocp-Apim-Subscription-Key", "api-key"):
        header = fragment.find(f"set-header[@name='{name}']")
        assert header is not None and header.attrib["exists-action"] == "delete"
        assert flat.index(header) < flat.index(identity)
    query = fragment.find("set-query-parameter")
    assert query is not None
    assert query.attrib == {"name": "subscription-key", "exists-action": "delete"}
    assert flat.index(query) < flat.index(identity)
    assert _render().fragment_xml.find("authentication-managed-identity") == -1


def test_api_policy_includes_fragment_and_on_error_www_authenticate() -> None:
    api = ET.fromstring(_render().api_policy_xml)

    assert [section.tag for section in api] == ["inbound", "backend", "outbound", "on-error"]
    inbound = api.find("inbound")
    assert inbound is not None
    assert [element.tag for element in inbound] == ["base", "include-fragment"]
    assert inbound[1].attrib == {"fragment-id": "mosaic-mcp-weather"}
    on_error = api.find("on-error")
    assert on_error is not None
    assert [element.tag for element in on_error] == ["base", "choose"]
    assert any("context.Response.StatusCode == 401" in condition for condition in _conditions(api))
    assert any("invalid_token" in value for value in _header_values(api, "WWW-Authenticate"))


def test_metadata_policy_returns_runtime_jobject_document() -> None:
    metadata = ET.fromstring(_render().metadata_policy_xml)
    response = metadata.find("inbound/return-response")

    assert response is not None
    assert response.find("set-status").attrib == {"code": "200", "reason": "OK"}  # type: ignore[union-attr]
    assert "application/json" in _header_values(metadata, "Content-Type")
    assert "public, max-age=3600" in _header_values(metadata, "Cache-Control")
    policy = _render().metadata_policy_xml
    assert "new JObject()" in policy
    assert '"resource"' in policy
    assert f"https://login.microsoftonline.com/{TENANT}/v2.0" in policy
    assert '"bearer_methods_supported"' in policy
    assert '"header"' in policy
    assert f"api://{AUDIENCE}/Mcp.Invoke" in policy
    assert "mosaic/mcp/weather/mcp" in policy


@pytest.mark.parametrize(
    ("publication", "snapshot", "message"),
    [
        (_publication(tenant_id="common"), _snapshot(), "tenant GUID"),
        (_publication(api_path=".well-known/oauth-protected-resource/x"), _snapshot(), "API path"),
        (_publication(fragment_name='name" /><send-request />'), _snapshot(), "resource names"),
        (_publication(), _snapshot(audience="api://runtime"), "GUID audience"),
        (
            _publication(),
            _snapshot(grants=[_grant(object_id="not-a-guid")]),
            "GUID object IDs",
        ),
    ],
)
def test_guid_name_and_path_validation_errors(
    publication: McpPublication, snapshot: McpAccessSnapshot, message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        _render(publication, snapshot)


def test_backend_auth_validation() -> None:
    with pytest.raises(ValidationError, match="API key"):
        _render(backend_auth=McpAuthMode.API_KEY)
    with pytest.raises(ValidationError, match="resource audience"):
        _render(backend_auth=McpAuthMode.MANAGED_IDENTITY)


def test_token_limit_grant_is_refused() -> None:
    tokens = TokenEnforcement.model_construct(
        counter_key_expression=SUBSCRIPTION_COUNTER,
        tokens_per_minute=1,
        token_quota=None,
        token_quota_period=None,
        estimate_prompt_tokens=True,
    )
    grant = McpAccessGrant.model_construct(
        entitlement_id="entitlement-token",
        subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id="principal-token"),
        object_id="99999999-3333-3333-3333-333333333333",
        display_name="Token grant",
        enabled=True,
        enforcement=EntitlementEnforcement.model_construct(tokens=tokens, requests=None),
        intent_digest="intent-token",
    )
    snapshot = McpAccessSnapshot.model_construct(
        version=1,
        audience=AUDIENCE,
        delegated_scope="Mcp.Invoke",
        application_role="Mcp.Invoke.Application",
        grants=[grant],
    )

    with pytest.raises(ValidationError, match="calls, not tokens"):
        _render(snapshot=snapshot)


def test_call_rate_renewal_above_300_seconds_is_refused_before_rendering() -> None:
    allowed = _snapshot(grants=[_grant(enforcement=_enforcement(renewal_period_seconds=300))])
    assert 'renewal-period="300"' in _render(snapshot=allowed).fragment_xml

    hourly = _snapshot(grants=[_grant(enforcement=_enforcement(renewal_period_seconds=3600))])
    with pytest.raises(ValidationError, match="must not exceed 300 seconds"):
        _render(snapshot=hourly)


def test_literals_are_escaped_and_cannot_inject_xml_or_named_values() -> None:
    hostile = '{{secret}}"; return "attacker"; //'
    snapshot = _snapshot(delegated_scope=hostile, application_role=hostile)
    result = _render(snapshot=snapshot)

    ET.fromstring(result.fragment_xml)
    ET.fromstring(result.metadata_policy_xml)
    assert "{{secret}}" not in result.fragment_xml
    assert "{{secret}}" not in result.metadata_policy_xml
    assert result.fragment_xml.count("<validate-azure-ad-token") == 1
    assert result.metadata_policy_xml.count("<return-response>") == 1


def test_fragment_size_boundary_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    size = len(_render().fragment_xml.encode("utf-8"))

    monkeypatch.setattr(mcp_access_policy, "MAX_FRAGMENT_BYTES", size)
    _render()
    monkeypatch.setattr(mcp_access_policy, "MAX_FRAGMENT_BYTES", size - 1)
    with pytest.raises(ValidationError, match="512 KB UTF-8"):
        _render()


@pytest.mark.parametrize("period", ["Hourly", "Daily", "Weekly", "Monthly", "Yearly"])
def test_quota_periods_are_utc_calendar_keys(period: QuotaPeriod) -> None:
    grant = _grant(
        enforcement=_enforcement(
            calls=None,
            renewal_period_seconds=None,
            call_quota=500,
            call_quota_period=period,
        )
    )
    counter = _counters(_fragment(_snapshot(grants=[grant])))[0]

    assert f":{period}:" in counter
    assert "var now = DateTime.UtcNow;" in counter
    if period == "Weekly":
        assert "now.Date.AddDays(-(((int)now.DayOfWeek + 6) % 7))" in counter
    else:
        assert "AddDays" not in counter


def test_facets_are_redacted_human_readable_and_managed() -> None:
    result = _render(
        snapshot=_snapshot(
            grants=[_grant(enforcement=_enforcement()), _group_grant(2, enforcement=_enforcement())]
        )
    )
    serialized = " ".join(
        part
        for facet in result.facets
        for part in [
            facet.summary,
            *facet.details,
            *facet.attributes.keys(),
            *facet.attributes.values(),
        ]
    )

    assert all(facet.managed_by_mosaic for facet in result.facets)
    assert "private" not in serialized
    assert any(facet.kind == "authorization" for facet in result.facets)
    assert any(facet.kind == "rateLimit" for facet in result.facets)
    assert any("metadata" in facet.summary for facet in result.facets)
    assert any(
        "managed identity" in facet.summary or "without attaching" in facet.summary
        for facet in result.facets
    )


def test_counter_identity_is_stable_and_isolated() -> None:
    publication = _publication()
    grant = _grant()

    identity = mcp_grant_counter_identity(publication, grant)
    assert re.fullmatch(r"[a-f0-9]{64}", identity)
    assert identity == mcp_grant_counter_identity(publication, grant)
    assert identity != mcp_grant_counter_identity(_publication(id="other-publication"), grant)
    assert identity != mcp_grant_counter_identity(publication, _grant(entitlement_id="other"))
