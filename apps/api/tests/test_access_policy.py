"""Compiler contract tests, not an APIM gateway conformance or token-validation suite."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET

import pytest
from mosaic_api.domain import (
    ApiShape,
    EntitlementEnforcement,
    EntitlementSubject,
    EntitlementSubjectKind,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
    ModelProvider,
    Publication,
    QuotaPeriod,
    RequestEnforcement,
    TokenEnforcement,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations import access_policy
from mosaic_api.integrations.access_policy import (
    governed_counter_key_expression,
    grant_counter_identity,
    render_governed_policy,
)
from mosaic_api.integrations.apim.model_apis import curated_operations
from mosaic_api.integrations.policy import PublicationPolicy, _serialize

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"


def _tokens(**overrides: object) -> TokenEnforcement:
    return TokenEnforcement.model_validate(
        {
            "counter_key_expression": SUBSCRIPTION_COUNTER,
            "tokens_per_minute": 1000,
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


def _publication(**overrides: object) -> Publication:
    return Publication.model_validate(
        {
            "id": "publication-model",
            "tenant_id": TENANT,
            "gateway_id": "gateway",
            "model_endpoint_id": "endpoint",
            "deployment_name": "gpt-4o-prod",
            "provider": ModelProvider.AZURE_OPENAI,
            "display_name": "Example model",
            "api_name": "mosaic-model",
            "api_path": "mosaic/model",
            "backend_name": "mosaic-model",
            "fragment_name": "mosaic-model",
            "product_name": "mosaic-model",
            "subscription_name": "mosaic-bootstrap",
            "enforcement": _tokens(tokens_per_minute=9000),
            "shape_version": "1.0",
            **overrides,
        }
    )


def _grant(number: int = 1, **overrides: object) -> ModelAccessGrant:
    return ModelAccessGrant.model_validate(
        {
            "entitlement_id": f"entitlement-{number}",
            "subject": EntitlementSubject(
                kind=EntitlementSubjectKind.USER, id=f"principal-{number}"
            ),
            "object_id": f"{number:08x}-3333-3333-3333-333333333333",
            "display_name": f"Caller {number}",
            "subscription_name": f"mosaic-grant-{number}",
            "enabled": True,
            "intent_digest": f"intent-{number}",
            **overrides,
        }
    )


def _group_grant(number: int = 1, **overrides: object) -> ModelAccessGrant:
    values: dict[str, object] = {
        "subject": EntitlementSubject(
            kind=EntitlementSubjectKind.SECURITY_GROUP, id=f"group-principal-{number}"
        ),
        "object_id": f"{number:08x}-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "display_name": f"Group {number}",
        "subscription_name": None,
    }
    values.update(overrides)
    return _grant(number, **values)


def _snapshot(**overrides: object) -> ModelAccessSnapshot:
    return ModelAccessSnapshot.model_validate(
        {
            "version": 1,
            "settings": ModelAccessSettings(),
            "audience": AUDIENCE,
            "publication_enforcement": _tokens(tokens_per_minute=5000),
            "grants": [_grant()],
            **overrides,
        }
    )


def _fragment(
    snapshot: ModelAccessSnapshot | None = None, publication: Publication | None = None
) -> ET.Element:
    return ET.fromstring(
        render_governed_policy(publication or _publication(), snapshot or _snapshot()).fragment_xml
    )


def _variable_values(fragment: ET.Element, name: str) -> list[str]:
    return [
        variable.attrib["value"]
        for variable in fragment.findall(f".//set-variable[@name='{name}']")
    ]


def _conditions(fragment: ET.Element) -> list[str]:
    return [when.attrib["condition"] for when in fragment.iter("when")]


def _counters(fragment: ET.Element) -> list[str]:
    return [
        element.attrib["counter-key"]
        for element in fragment.iter()
        if "counter-key" in element.attrib
    ]


def _full_enforcement() -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=_tokens(token_quota=20000, token_quota_period="Monthly"),
        requests=_requests(call_quota=1000, call_quota_period="Monthly"),
    )


def _facets_json(result: PublicationPolicy) -> str:
    return json.dumps([facet.model_dump(mode="json") for facet in result.facets])


def test_documents_are_deterministic_thin_and_digest_both_documents() -> None:
    first = render_governed_policy(_publication(), _snapshot())
    second = render_governed_policy(_publication(), _snapshot())
    assert first == second
    assert (
        first.content_sha256
        == hashlib.sha256(f"{first.fragment_xml}\n{first.api_policy_xml}".encode()).hexdigest()
    )
    fragment = ET.fromstring(first.fragment_xml)
    api = ET.fromstring(first.api_policy_xml)
    assert fragment.tag == "fragment"
    assert not any(
        element.tag in {"base", "include-fragment", "inbound", "backend", "outbound", "on-error"}
        for element in fragment.iter()
    )
    assert [section.tag for section in api] == ["inbound", "backend", "outbound", "on-error"]
    inbound = api.find("inbound")
    assert inbound is not None
    assert [element.tag for element in inbound] == ["base", "include-fragment"]
    assert inbound[1].attrib == {"fragment-id": "mosaic-model"}
    assert api.find(".//llm-token-limit") is None
    assert first.unrecognized_elements == []


def test_authentication_precedes_limits_identity_routing_and_forwarding() -> None:
    fragment = _fragment(_snapshot(grants=[_grant(enforcement=_full_enforcement())]))
    flat = list(fragment.iter())
    validate = fragment.find(".//validate-azure-ad-token")
    limits = fragment.findall(".//llm-token-limit")
    identity = fragment.find("authentication-managed-identity")
    backend = fragment.find("set-backend-service")
    assert validate is not None and identity is not None and backend is not None
    assert identity.attrib == {"resource": "https://cognitiveservices.azure.com"}
    assert backend.attrib == {"backend-id": "mosaic-model"}
    assert flat.index(validate) < min(flat.index(limit) for limit in limits)
    assert max(flat.index(limit) for limit in limits) < flat.index(identity) < flat.index(backend)
    assert fragment.find(".//send-request") is None
    assert fragment.find(".//forward-request") is None
    for name in ("Ocp-Apim-Subscription-Key", "api-key", "Authorization"):
        header = fragment.find(f"set-header[@name='{name}']")
        assert header is not None and header.attrib["exists-action"] == "delete"
        assert flat.index(header) < flat.index(identity)
    query = fragment.find("set-query-parameter")
    assert query is not None
    assert query.attrib == {"name": "subscription-key", "exists-action": "delete"}
    assert flat.index(query) < flat.index(identity)
    metric = fragment.find("llm-emit-token-metric")
    assert metric is not None and metric.attrib["namespace"] == "mosaic"
    assert {dimension.attrib["name"] for dimension in metric} == {"Publication", "Deployment"}


def test_snapshot_is_authoritative_and_display_metadata_does_not_change_xml() -> None:
    snapshot = _snapshot()
    original = render_governed_policy(_publication(), snapshot)
    changed = render_governed_policy(
        _publication(
            enforcement=_tokens(counter_key_expression='@("arbitrary draft counter")'),
            governed_access=ModelAccessSettings(keys_enabled=False, entra_enabled=False),
        ),
        snapshot.model_copy(
            update={
                "version": 19,
                "grants": [_grant(display_name="Another name", intent_digest="changed")],
            }
        ),
    )
    assert original.content_sha256 == changed.content_sha256
    assert 'tokens-per-minute="5000"' in original.fragment_xml
    assert 'tokens-per-minute="9000"' not in original.fragment_xml
    assert "arbitrary draft counter" not in changed.fragment_xml


def test_grant_order_does_not_change_the_documents_or_digest() -> None:
    first = render_governed_policy(_publication(), _snapshot(grants=[_grant(1), _grant(2)]))
    second = render_governed_policy(_publication(), _snapshot(grants=[_grant(2), _grant(1)]))
    assert first == second


@pytest.mark.parametrize(
    ("keys", "entra"), [(True, True), (True, False), (False, True), (False, False)]
)
def test_each_auth_method_is_independently_enabled_and_disabled_credentials_are_denied(
    keys: bool, entra: bool
) -> None:
    result = render_governed_policy(
        _publication(),
        _snapshot(
            settings=ModelAccessSettings(keys_enabled=keys, entra_enabled=entra),
            audience=AUDIENCE if entra else None,
        ),
    )
    fragment = ET.fromstring(result.fragment_xml)
    assert len(fragment.findall(".//validate-azure-ad-token")) == int(entra)
    if not keys and not entra:
        assert [element.tag for element in fragment] == ["return-response"]
        assert fragment.find("return-response/set-status").attrib["code"] == "403"  # type: ignore[union-attr]
        assert fragment.find(".//authentication-managed-identity") is None
        assert fragment.find(".//llm-token-limit") is None
        assert any("All model access is denied" in facet.summary for facet in result.facets)
        return
    conditions = _conditions(fragment)
    assert (
        '@(!(bool)context.Variables["mosaic-has-key"]'
        ' && !(bool)context.Variables["mosaic-has-token"])' in conditions
    )
    assert len(_variable_values(fragment, "mosaic-key-grant")) == 1 + int(keys)
    assert len(_variable_values(fragment, "mosaic-token-grant")) == 1 + int(entra)
    for enabled, method in ((keys, "key"), (entra, "token")):
        if not enabled:
            condition = f'@((bool)context.Variables["mosaic-has-{method}"])'
            rejection = next(
                when
                for when in fragment.findall("choose/when")
                if when.attrib["condition"] == condition
            )
            assert rejection.find("return-response/set-status").attrib["code"] == "401"  # type: ignore[union-attr]
    assert "<" not in _facets_json(result)


def test_presence_includes_empty_query_or_header_and_any_authorization_header() -> None:
    fragment = _fragment()
    assert _variable_values(fragment, "mosaic-has-key") == [
        '@(context.Request.Headers.ContainsKey("Ocp-Apim-Subscription-Key")'
        ' || context.Request.Url.Query.ContainsKey("subscription-key"))'
    ]
    assert _variable_values(fragment, "mosaic-has-token") == [
        '@(context.Request.Headers.ContainsKey("Authorization"))'
    ]
    assert any(
        '!authorization.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase)' in expression
        and "String.IsNullOrWhiteSpace(authorization.Substring(7))" in expression
        for expression in _conditions(fragment)
    )
    assert not any("api-key" in value for value in _variable_values(fragment, "mosaic-has-key"))


def test_keys_require_native_validation_and_exact_enabled_allowlist() -> None:
    group = _group_grant(3)
    fragment = _fragment(_snapshot(grants=[_grant(), _grant(2, enabled=False), group]))
    lookup = _variable_values(fragment, "mosaic-key-grant")[-1]
    assert 'if (context.Subscription == null) return "";' in lookup
    assert "var subscription = context.Subscription.Id;" in lookup
    assert (
        'String.Equals(subscription, "mosaic-grant-1", StringComparison.OrdinalIgnoreCase)'
        in lookup
    )
    assert "mosaic-grant-2" not in lookup
    assert group.object_id not in lookup
    assert grant_counter_identity(_publication(), group) not in lookup
    assert "mosaic-bootstrap" not in lookup
    assert "master" not in lookup
    assert lookup.endswith('return "";\n}')
    assert '@(String.IsNullOrEmpty((string)context.Variables["mosaic-key-grant"]))' in _conditions(
        fragment
    )
    assert not any(
        word in lookup for word in ("PrimaryKey", "SecondaryKey", "context.User", ".EndsWith(")
    )


def test_ambiguous_empty_and_duplicate_key_locations_fail_closed() -> None:
    shape = next(
        expression
        for expression in _conditions(_fragment())
        if "var headers = context.Request.Headers;" in expression
    )
    assert shape.count("values.Length != 1") == 2
    assert shape.count("String.IsNullOrWhiteSpace(values[0])") == 2
    assert "header != null && parameter != null" in shape
    assert "!String.Equals(header, parameter, StringComparison.Ordinal)" in shape
    assert "context.Subscription == null || String.IsNullOrEmpty(context.Subscription.Id)" in shape


def test_token_claims_are_used_only_after_native_validation_for_exact_tenant_and_audience() -> None:
    fragment = _fragment()
    token = next(
        when
        for when in fragment.findall("choose/when")
        if when.find("validate-azure-ad-token") is not None
    )
    validator = token.find("validate-azure-ad-token")
    assert validator is not None
    assert validator.attrib == {
        "tenant-id": TENANT,
        "header-name": "Authorization",
        "failed-validation-httpcode": "401",
        "failed-validation-error-message": "Model access denied.",
        "output-token-variable-name": "mosaic-validated-token",
    }
    assert [audience.text for audience in validator.findall("audiences/audience")] == [AUDIENCE]
    assert validator.find("required-claims/claim").attrib == {"name": "ver", "match": "all"}  # type: ignore[union-attr]
    assert validator.findtext("required-claims/claim/value") == "2.0"
    lookup = token.find("set-variable[@name='mosaic-token-grant']")
    assert lookup is not None and list(token).index(validator) < list(token).index(lookup)
    assert ".AsJwt(" not in ET.tostring(fragment, encoding="unicode")
    assert ".TryParseJwt(" not in ET.tostring(fragment, encoding="unicode")


def test_claim_lookup_guards_nulls_and_cardinality_and_distinguishes_token_kinds() -> None:
    user = _grant()
    app = _grant(2, subject=EntitlementSubject(kind=EntitlementSubjectKind.APPLICATION, id="app-2"))
    fragment = _fragment(_snapshot(grants=[user, app]))
    lookup = _variable_values(fragment, "mosaic-token-grant")[-1]
    assert 'context.Variables.ContainsKey("mosaic-validated-token")' in lookup
    assert 'jwt == null || jwt.Claims == null || !jwt.Claims.ContainsKey("oid")' in lookup
    assert (
        "objects == null || objects.Length != 1 || String.IsNullOrWhiteSpace(objects[0])" in lookup
    )
    assert 'if (jwt.Claims.ContainsKey("scp")) {' in lookup
    assert 'if (!hasRealScopes && jwt.Claims.ContainsKey("roles")) {' in lookup
    assert "scopes != null && scopes.Length == 1 && scopes[0] != null" in lookup
    assert "foreach (var scope in scopes[0].Split(' '))" in lookup
    assert 'String.IsNullOrWhiteSpace(scope) || scope == "/"' in lookup
    assert 'String.Equals(scope, "Models.Invoke", StringComparison.Ordinal)' in lookup
    assert 'application = roles != null && roles.Contains("Models.Invoke.Application");' in lookup
    assert 'roles.Contains("Models.Invoke")' not in lookup
    assert f'if (delegated && String.Equals(oid, "{user.object_id}"' in lookup
    assert f'if (application && String.Equals(oid, "{app.object_id}"' in lookup
    assert f'if (application && String.Equals(oid, "{user.object_id}"' not in lookup
    assert f'if (delegated && String.Equals(oid, "{app.object_id}"' not in lookup
    assert "context.User" not in lookup
    assert "Headers" not in lookup
    assert lookup.endswith('return "";\n}')


@pytest.mark.parametrize("scp_literal", ['""', '"/"'])
def test_empty_and_slash_scp_take_application_role_path(scp_literal: str) -> None:
    lookup = _variable_values(_fragment(), "mosaic-token-grant")[-1]
    scp_index = lookup.index('if (jwt.Claims.ContainsKey("scp"))')
    roles_index = lookup.index('if (!hasRealScopes && jwt.Claims.ContainsKey("roles"))')
    assert scp_index < roles_index
    assert 'scope == "/"' in lookup
    assert "hasRealScopes = true;" in lookup
    assert 'roles.Contains("Models.Invoke.Application")' in lookup
    assert scp_literal.strip('"') in lookup


def test_absent_scp_takes_application_path_and_real_scopes_stay_delegated() -> None:
    lookup = _variable_values(_fragment(), "mosaic-token-grant")[-1]
    assert 'if (!hasRealScopes && jwt.Claims.ContainsKey("roles"))' in lookup
    assert 'String.Equals(scope, "Models.Invoke", StringComparison.Ordinal)' in lookup
    assert "delegated = true;" in lookup
    assert lookup.index("delegated = true;") < lookup.index("roles.Contains")


def test_parameterized_scope_and_role_names_are_emitted() -> None:
    fragment = _fragment()
    default_lookup = _variable_values(fragment, "mosaic-token-grant")[-1]
    custom = ET.fromstring(
        render_governed_policy(
            _publication(),
            _snapshot(),
            delegated_scope="Mcp.Invoke",
            application_role="Mcp.Invoke.Application",
        ).fragment_xml
    )
    custom_lookup = _variable_values(custom, "mosaic-token-grant")[-1]
    assert '"Models.Invoke"' in default_lookup
    assert '"Models.Invoke.Application"' in default_lookup
    assert '"Mcp.Invoke"' in custom_lookup
    assert '"Mcp.Invoke.Application"' in custom_lookup
    assert '"Models.Invoke"' not in custom_lookup


def test_group_token_branch_follows_direct_grants_in_precedence_order() -> None:
    publication = _publication()
    direct = _grant(9)
    limited = _group_grant(
        2,
        entitlement_id="z-limited",
        enforcement=EntitlementEnforcement(tokens=_tokens(tokens_per_minute=10)),
    )
    generous = _group_grant(3, entitlement_id="a-unlimited", enforcement=None)
    fragment = _fragment(_snapshot(grants=[limited, direct, generous]), publication)
    lookup = _variable_values(fragment, "mosaic-token-grant")[-1]

    direct_return = f'return "{grant_counter_identity(publication, direct)}";'
    generous_return = f'return "{grant_counter_identity(publication, generous)}";'
    limited_return = f'return "{grant_counter_identity(publication, limited)}";'
    assert lookup.index(direct_return) < lookup.index('jwt.Claims.ContainsKey("groups")')
    assert lookup.index(generous_return) < lookup.index(limited_return)


def test_group_matching_uses_lowercase_literals_case_insensitively() -> None:
    group = _group_grant(1, object_id="ABCDEF12-AAAA-BBBB-CCCC-ABCDEFABCDEF")
    lookup = _variable_values(_fragment(_snapshot(grants=[group])), "mosaic-token-grant")[-1]
    assert '"abcdef12-aaaa-bbbb-cccc-abcdefabcdef"' in lookup
    assert '"ABCDEF12-AAAA-BBBB-CCCC-ABCDEFABCDEF"' not in lookup
    assert "StringComparison.OrdinalIgnoreCase" in lookup
    assert 'jwt.Claims.ContainsKey("groups") ? jwt.Claims["groups"] : null' in lookup


def test_group_limits_are_counted_per_member_and_direct_keys_stay_exact() -> None:
    publication = _publication()
    direct = _grant(1, enforcement=_full_enforcement())
    group = _group_grant(2, enforcement=_full_enforcement())
    fragment = _fragment(_snapshot(grants=[direct, group]), publication)
    direct_id = grant_counter_identity(publication, direct)
    group_id = grant_counter_identity(publication, group)
    counters = _counters(fragment)

    assert f"mosaic:governed:grant-request-rate:{direct_id}" in counters
    assert f"mosaic:governed:grant-tokens:{direct_id}" in counters
    assert any(
        f'return "mosaic:governed:grant-request-quota:{direct_id}:Monthly:"'
        in counter
        and ' + ":" + (string)context.Variables["mosaic-member"]' not in counter
        for counter in counters
    )
    assert (
        f'@("mosaic:governed:grant-request-rate:{group_id}:" + {access_policy._MEMBER})'
        in counters
    )
    assert f'@("mosaic:governed:grant-tokens:{group_id}:" + {access_policy._MEMBER})' in counters
    assert any(
        f'return "mosaic:governed:grant-request-quota:{group_id}:Monthly:"'
        in counter
        and ' + ":" + (string)context.Variables["mosaic-member"]' in counter
        for counter in counters
    )


def test_publication_counter_appends_member_only_for_group_callers() -> None:
    fragment = _fragment(_snapshot(grants=[_grant(), _group_grant(2)]))
    publication_counter = _counters(fragment)[-1]
    assert publication_counter == (
        '@("mosaic:governed:publication-tokens:" + (string)context.Variables["mosaic-grant"] + '
        '(String.IsNullOrEmpty((string)context.Variables["mosaic-member"]) ? "" : ":" + '
        '(string)context.Variables["mosaic-member"]))'
    )


def test_publication_counter_is_unchanged_without_group_grants() -> None:
    publication_counter = _counters(_fragment(_snapshot(grants=[_grant()])))[-1]
    assert publication_counter == (
        '@("mosaic:governed:publication-tokens:" + (string)context.Variables["mosaic-grant"])'
    )


def test_single_line_expressions_never_nest_multi_statement_blocks() -> None:
    fragment = _fragment(_snapshot(grants=[_grant(), _group_grant(2)]))
    for element in fragment.iter():
        for value in element.attrib.values():
            if value.startswith("@("):
                assert "@{" not in value, value


def test_governed_counter_key_expression_matches_direct_and_group_publication_keys() -> None:
    publication = _publication()
    direct = _grant()
    group = _group_grant(2)
    direct_id = grant_counter_identity(publication, direct)
    group_id = grant_counter_identity(publication, group)

    assert governed_counter_key_expression(publication, direct) == (
        f'@("mosaic:governed:publication-tokens:{direct_id}")'
    )
    assert governed_counter_key_expression(publication, group) == (
        f'@("mosaic:governed:publication-tokens:{group_id}:" + '
        '(string)context.Variables["mosaic-member"])'
    )


def test_member_variable_is_set_on_all_authentication_paths() -> None:
    key_only = _fragment(
        _snapshot(settings=ModelAccessSettings(entra_enabled=False), audience=None)
    )
    assert _variable_values(key_only, "mosaic-member") == [""]

    token_group = _fragment(_snapshot(grants=[_group_grant(2)]))
    members = _variable_values(token_group, "mosaic-member")
    assert members[0] == ""
    assert 'return objects[0].ToLowerInvariant();' in members[-1]


def test_group_overage_message_is_emitted_only_when_group_grants_exist() -> None:
    direct = render_governed_policy(_publication(), _snapshot()).fragment_xml
    group = render_governed_policy(_publication(), _snapshot(grants=[_group_grant(2)])).fragment_xml

    assert "too many" not in direct
    assert "too many" in group
    assert "jwt.Claims.ContainsKey(&quot;hasgroups&quot;)" in group
    assert "jwt.Claims.ContainsKey(&quot;_claim_names&quot;)" in group
    overage = next(
        condition
        for condition in _conditions(_fragment(_snapshot(grants=[_group_grant(2)])))
        if "_claim_names" in condition
    )
    assert overage.startswith("@{\nif (!String.IsNullOrEmpty(")


def test_two_credentials_must_resolve_to_same_grant_with_no_fallback() -> None:
    fragment = _fragment()
    key = _variable_values(fragment, "mosaic-key-grant")[-1]
    token = _variable_values(fragment, "mosaic-token-grant")[-1]
    assert re.findall(r'return "([a-f0-9]{64})";', key) == re.findall(
        r'return "([a-f0-9]{64})";', token
    )
    mismatch = (
        '@((bool)context.Variables["mosaic-has-key"] && (bool)context.Variables["mosaic-has-token"]'
        ' && (string)context.Variables["mosaic-key-grant"]'
        ' != (string)context.Variables["mosaic-token-grant"])'
    )
    assert mismatch in _conditions(fragment)
    for method in ("key", "token"):
        condition = f'@(String.IsNullOrEmpty((string)context.Variables["mosaic-{method}-grant"]))'
        assert condition in _conditions(fragment)
    assert _variable_values(fragment, "mosaic-grant") == [
        '@((bool)context.Variables["mosaic-has-key"]'
        ' ? (string)context.Variables["mosaic-key-grant"]'
        ' : (string)context.Variables["mosaic-token-grant"])'
    ]
    assert fragment.find(".//otherwise") is None


@pytest.mark.parametrize("grants", [[], [_grant(enabled=False)]])
def test_empty_or_disabled_only_allowlist_never_resolves_a_grant(
    grants: list[ModelAccessGrant],
) -> None:
    fragment = _fragment(_snapshot(grants=grants))
    for method in ("key", "token"):
        lookup = _variable_values(fragment, f"mosaic-{method}-grant")[-1]
        assert re.findall(r'return "([a-f0-9]{64})";', lookup) == []
        assert lookup.endswith('return "";\n}')
    assert not any(
        "mosaic-grant-1" in value for value in _variable_values(fragment, "mosaic-key-grant")
    )


@pytest.mark.parametrize(
    "identity", ["entitlement_id", "subscription_name", "object_id", "subject"]
)
def test_duplicate_allowlist_identities_are_rejected_even_if_one_grant_is_disabled(
    identity: str,
) -> None:
    first = _grant()
    second = _grant(2, enabled=False, **{identity: getattr(first, identity)})
    with pytest.raises(ValidationError, match="unambiguous"):
        render_governed_policy(_publication(), _snapshot(grants=[first, second]))


def test_security_group_grants_require_entra_and_guid_object_ids() -> None:
    with pytest.raises(ValidationError, match="Entra"):
        render_governed_policy(
            _publication(),
            _snapshot(
                settings=ModelAccessSettings(entra_enabled=False),
                audience=None,
                grants=[_group_grant()],
            ),
        )
    with pytest.raises(ValidationError, match="GUID object ID"):
        render_governed_policy(
            _publication(),
            _snapshot(grants=[_group_grant(object_id="not-a-guid")]),
        )


def test_security_group_grant_with_subscription_is_rejected_by_domain_model() -> None:
    with pytest.raises(ValueError, match="no subscription"):
        _group_grant(subscription_name="mosaic-group-key")


@pytest.mark.parametrize("identity", ["entitlement_id", "subscription_name", "object_id"])
def test_missing_identity_is_rejected_without_echoing_identity(identity: str) -> None:
    with pytest.raises(ValidationError, match="nonempty"):
        render_governed_policy(
            _publication(), _snapshot(grants=[_grant().model_copy(update={identity: " "})])
        )


@pytest.mark.parametrize("name", ["master", "MASTER", "mosaic-bootstrap"])
def test_bootstrap_and_all_access_subscription_names_cannot_be_allowlisted(name: str) -> None:
    with pytest.raises(ValidationError, match="all-access or publication bootstrap"):
        render_governed_policy(_publication(), _snapshot(grants=[_grant(subscription_name=name)]))


def test_both_publication_and_grant_native_limits_have_distinct_counter_namespaces() -> None:
    fragment = _fragment(_snapshot(grants=[_grant(enforcement=_full_enforcement())]))
    token_limits = fragment.findall(".//llm-token-limit")
    assert len(token_limits) == 2
    assert {limit.attrib["tokens-per-minute"] for limit in token_limits} == {"1000", "5000"}
    assert token_limits[0].attrib["token-quota"] == "20000"
    assert token_limits[0].attrib["token-quota-period"] == "Monthly"
    assert token_limits[0].attrib["estimate-prompt-tokens"] == "true"
    assert "grant-tokens:" in token_limits[0].attrib["counter-key"]
    assert "publication-tokens:" in token_limits[1].attrib["counter-key"]
    assert fragment.find(".//rate-limit-by-key").attrib["calls"] == "10"  # type: ignore[union-attr]
    assert fragment.find(".//quota-by-key").attrib["calls"] == "1000"  # type: ignore[union-attr]
    counters = _counters(fragment)
    assert len(set(counters)) == len(counters) == 4
    assert all("context.Subscription" not in counter for counter in counters)
    assert all("context.Request" not in counter for counter in counters)
    assert all("context.User" not in counter for counter in counters)
    assert "mosaic:governed:grant-request-rate:" in counters[0]
    assert "mosaic:governed:grant-request-quota:" in counters[1]


def test_unrestricted_grants_still_receive_publication_token_safeguards() -> None:
    result = render_governed_policy(_publication(), _snapshot())
    fragment = ET.fromstring(result.fragment_xml)
    assert len(fragment.findall(".//llm-token-limit")) == 1
    assert fragment.find(".//rate-limit-by-key") is None
    assert fragment.find(".//quota-by-key") is None
    assert any(
        "Publication token safeguards apply even to grants without limits." in facet.details
        for facet in result.facets
    )


def test_exported_counter_identity_matches_both_auth_methods_and_native_grant_counters() -> None:
    publication = _publication()
    grant = _grant(enforcement=_full_enforcement())
    identity = grant_counter_identity(publication, grant)
    assert (
        identity
        == hashlib.sha256(
            json.dumps(
                [publication.tenant_id, publication.id, grant.entitlement_id],
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    fragment = _fragment(_snapshot(grants=[grant]), publication)
    for method in ("key", "token"):
        assert f'return "{identity}";' in _variable_values(fragment, f"mosaic-{method}-grant")[-1]
    assert all(identity in counter for counter in _counters(fragment)[:-1])


def test_counters_are_stable_across_credentials_rotation_revision_and_changed_limits() -> None:
    original = _snapshot(grants=[_grant(enforcement=_full_enforcement())])
    changed = original.model_copy(
        update={
            "version": 99,
            "settings": ModelAccessSettings(keys_enabled=False),
            "grants": [
                _grant(
                    subscription_name="mosaic-rotated",
                    object_id="44444444-4444-4444-4444-444444444444",
                    display_name="Updated principal",
                    intent_digest="new-intent",
                    enforcement=EntitlementEnforcement(
                        tokens=_tokens(
                            tokens_per_minute=123, token_quota=500, token_quota_period="Monthly"
                        ),
                        requests=_requests(calls=2, call_quota=33, call_quota_period="Monthly"),
                    ),
                )
            ],
        }
    )
    assert _counters(_fragment(original)) == _counters(_fragment(changed))
    old_lookup = _variable_values(_fragment(original), "mosaic-token-grant")[-1]
    new_lookup = _variable_values(_fragment(changed), "mosaic-token-grant")[-1]
    assert re.findall(r'return "([a-f0-9]{64})";', old_lookup) == re.findall(
        r'return "([a-f0-9]{64})";', new_lookup
    )


@pytest.mark.parametrize("change", ["tenant", "publication", "entitlement"])
def test_counter_identity_is_isolated_by_tenant_publication_and_entitlement(change: str) -> None:
    publication = _publication()
    grant = _grant(enforcement=_full_enforcement())
    original = _fragment(_snapshot(grants=[grant]), publication)
    if change == "tenant":
        publication = _publication(tenant_id="99999999-9999-9999-9999-999999999999")
    elif change == "publication":
        publication = _publication(id="another-publication")
    else:
        grant = grant.model_copy(update={"entitlement_id": "another-entitlement"})
    changed = _fragment(_snapshot(grants=[grant]), publication)
    assert _counters(original)[:-1] != _counters(changed)[:-1]
    assert (
        _variable_values(original, "mosaic-key-grant")[-1]
        != _variable_values(changed, "mosaic-key-grant")[-1]
    )


@pytest.mark.parametrize("period", ["Hourly", "Daily", "Weekly", "Monthly", "Yearly"])
def test_native_token_quota_periods_and_disabled_estimation_are_preserved(
    period: QuotaPeriod,
) -> None:
    fragment = _fragment(
        _snapshot(
            publication_enforcement=_tokens(
                tokens_per_minute=None,
                token_quota=50000,
                token_quota_period=period,
                estimate_prompt_tokens=False,
            )
        )
    )
    limit = fragment.find("llm-token-limit")
    assert limit is not None
    assert "tokens-per-minute" not in limit.attrib
    assert limit.attrib["token-quota"] == "50000"
    assert limit.attrib["token-quota-period"] == period
    assert limit.attrib["estimate-prompt-tokens"] == "false"


@pytest.mark.parametrize(
    ("period", "date_length"),
    [
        ("Hourly", 13),
        ("Daily", 10),
        ("Weekly", 10),
        ("Monthly", 7),
        ("Yearly", 4),
    ],
)
def test_request_quotas_are_utc_calendar_keys_not_rolling_30_day_months(
    period: QuotaPeriod, date_length: int
) -> None:
    result = render_governed_policy(
        _publication(),
        _snapshot(
            grants=[
                _grant(
                    enforcement=EntitlementEnforcement(
                        requests=_requests(
                            calls=None,
                            renewal_period_seconds=None,
                            call_quota=456,
                            call_quota_period=period,
                        )
                    ),
                )
            ]
        ),
    )
    quota = ET.fromstring(result.fragment_xml).find(".//quota-by-key")
    assert quota is not None and quota.attrib["renewal-period"] == "0"
    assert quota.attrib["calls"] == "456"
    counter = quota.attrib["counter-key"]
    assert f":{period}:" in counter
    assert "var now = DateTime.UtcNow;" in counter
    assert f'.ToString("u").Substring(0, {date_length})' in counter
    assert "CultureInfo" not in counter
    if period == "Weekly":
        assert "now.Date.AddDays(-(((int)now.DayOfWeek + 6) % 7))" in counter
    else:
        assert "AddDays" not in counter
    assert "2592000" not in result.fragment_xml
    facet = next(facet for facet in result.facets if facet.element == "quota-by-key")
    assert "UTC calendar" in facet.summary
    assert "0 seconds" not in facet.summary
    assert facet.attributes["calendar-period"] == period


@pytest.mark.parametrize("seconds", [1, 60, 300])
def test_documented_request_renewal_range_is_accepted(seconds: int) -> None:
    fragment = _fragment(
        _snapshot(
            grants=[
                _grant(
                    enforcement=EntitlementEnforcement(
                        requests=_requests(renewal_period_seconds=seconds)
                    )
                )
            ]
        )
    )
    assert fragment.find(".//rate-limit-by-key").attrib["renewal-period"] == str(seconds)  # type: ignore[union-attr]


def test_request_renewal_above_300_seconds_is_rejected_before_rendering() -> None:
    with pytest.raises(ValidationError, match="300 seconds"):
        render_governed_policy(
            _publication(),
            _snapshot(
                grants=[
                    _grant(
                        enforcement=EntitlementEnforcement(
                            requests=_requests(renewal_period_seconds=301)
                        ),
                    )
                ]
            ),
        )


@pytest.mark.parametrize("scope", ["publication", "tokens", "requests"])
@pytest.mark.parametrize(
    "counter",
    [
        '@(context.Request.Headers.GetValueOrDefault("user-id", ""))',
        "@(context.Request.IpAddress)",
        '@("shared")',
        "{{secret}}",
        '@(context.Subscription.Id); throw new Exception("credential-do-not-echo");',
    ],
)
def test_custom_counter_expressions_are_rejected_without_echoing_the_expression(
    scope: str, counter: str
) -> None:
    snapshot = _snapshot()
    if scope == "publication":
        snapshot.publication_enforcement = _tokens(counter_key_expression=counter)
    else:
        enforcement = (
            EntitlementEnforcement(tokens=_tokens(counter_key_expression=counter))
            if scope == "tokens"
            else EntitlementEnforcement(requests=_requests(counter_key_expression=counter))
        )
        snapshot.grants = [_grant(enforcement=enforcement)]
    with pytest.raises(ValidationError, match="custom counter expressions") as error:
        render_governed_policy(_publication(), snapshot)
    assert counter not in str(error.value)
    assert "credential-do-not-echo" not in str(error.value)


def test_standard_legacy_subscription_counter_whitespace_is_safely_overridden() -> None:
    fragment = _fragment(
        _snapshot(
            publication_enforcement=_tokens(
                counter_key_expression=" @ ( context . Subscription . Id ) "
            )
        )
    )
    assert all("Subscription" not in counter for counter in _counters(fragment))


@pytest.mark.parametrize("scope", ["publication", "tokens", "requests"])
@pytest.mark.parametrize(
    "counter",
    [
        "@(context.Subscription.Id)",
        "@(context.Subscription?.Id)",
        "@(context.Subscription.Key)",
        "@(context.Subscription?.Key)",
    ],
)
def test_known_legacy_counter_defaults_are_explicitly_mapped_to_grant_budgets(
    scope: str, counter: str
) -> None:
    snapshot = _snapshot()
    if scope == "publication":
        snapshot.publication_enforcement = _tokens(counter_key_expression=counter)
    elif scope == "tokens":
        snapshot.grants = [
            _grant(enforcement=EntitlementEnforcement(tokens=_tokens(counter_key_expression=counter)))
        ]
    else:
        snapshot.grants = [
            _grant(
                enforcement=EntitlementEnforcement(
                    requests=_requests(counter_key_expression=counter)
                )
            )
        ]
    policy = render_governed_policy(_publication(), snapshot)
    fragment = ET.fromstring(policy.fragment_xml)
    assert all("Subscription" not in item for item in _counters(fragment))
    assert any(
        "Legacy subscription ID/key counter defaults" in detail
        for facet in policy.facets for detail in facet.details
    )


def test_disabled_invalid_limits_cannot_prevent_revocation() -> None:
    grant = _grant(
        enabled=False,
        enforcement=EntitlementEnforcement(
            requests=_requests(
                counter_key_expression="@not-a-supported-counter",
                renewal_period_seconds=301,
            )
        ),
    )
    policy = render_governed_policy(_publication(), _snapshot(grants=[grant]))
    assert "not-a-supported-counter" not in policy.fragment_xml
    assert "rate-limit-by-key" not in policy.fragment_xml


@pytest.mark.parametrize(
    "tenant",
    [
        "common",
        "organizations",
        "consumers",
        "",
        "{{tenant}}",
        "https://login.microsoftonline.com/common",
        '@("tenant")',
    ],
)
def test_token_tenant_must_be_specific_not_alias_expression_or_discovery_url(tenant: str) -> None:
    with pytest.raises(ValidationError, match="specific tenant GUID"):
        render_governed_policy(_publication(tenant_id=tenant), _snapshot())


@pytest.mark.parametrize("audience", [None, "", "api://models", "{{audience}}", '@("audience")'])
def test_token_audience_must_be_the_v2_runtime_guid(audience: str | None) -> None:
    with pytest.raises(ValidationError, match="GUID audience"):
        render_governed_policy(_publication(), _snapshot(audience=audience))


def test_key_only_access_does_not_need_runtime_token_configuration() -> None:
    fragment = _fragment(
        _snapshot(
            settings=ModelAccessSettings(entra_enabled=False),
            audience=None,
        ),
        _publication(tenant_id="legacy-tenant"),
    )
    assert fragment.find(".//validate-azure-ad-token") is None
    assert fragment.find("authentication-managed-identity") is not None


@pytest.mark.parametrize(
    ("provider", "allowed"),
    [
        (ModelProvider.AZURE_OPENAI, ["chat-completions", "responses"]),
        (ModelProvider.AZURE_AI_FOUNDRY, ["chat-completions"]),
    ],
)
def test_operation_allowlist_uses_curated_provider_ids_and_denies_unmetered_operations(
    provider: ModelProvider, allowed: list[str]
) -> None:
    publication = _publication(provider=provider)
    result = render_governed_policy(publication, _snapshot())
    fragment = ET.fromstring(result.fragment_xml)
    guard = next(
        condition for condition in _conditions(fragment) if "context.Operation == null" in condition
    )
    assert "context.Request.Method" in guard
    assert re.findall(r'context.Operation.Id == "([^"]+)"', guard) == allowed
    curated = curated_operations(provider, publication.deployment_name)
    assert set(allowed) < {op.name for op in curated}
    unsupported = {op.name for op in curated} - set(allowed)
    assert all(f'context.Operation.Id == "{name}"' not in guard for name in unsupported)
    guard_when = next(
        when for when in fragment.findall("choose/when") if when.attrib["condition"] == guard
    )
    assert guard_when.find("return-response") is not None
    facet = next(facet for facet in result.facets if "allowed-operations" in facet.attributes)
    assert facet.attributes["allowed-operations"] == ",".join(allowed)
    assert any(
        "embeddings, image, audio and legacy completions" in detail for detail in facet.details
    )
    guard_choose = next(
        choose for choose in fragment.findall("choose") if guard_when in list(choose)
    )
    limit = fragment.find("llm-token-limit")
    assert limit is not None
    assert list(fragment).index(guard_choose) < list(fragment).index(limit)


@pytest.mark.parametrize(
    ("provider", "operation"),
    [
        (ModelProvider.AZURE_OPENAI, "responses"),
        (ModelProvider.AZURE_AI_FOUNDRY, "chat-completions"),
    ],
)
def test_nondeployment_routes_reject_missing_malformed_and_different_model_bodies(
    provider: ModelProvider, operation: str
) -> None:
    fragment = _fragment(publication=_publication(provider=provider))
    pin = next(
        condition for condition in _conditions(fragment) if "preserveContent: true" in condition
    )
    assert f'if (!(context.Operation.Id == "{operation}")) return false;' in pin
    assert "if (context.Request.Body == null) return true;" in pin
    assert 'body == null ? null : body["model"]' in pin
    assert "model == null || model.Type != JTokenType.String" in pin
    assert '!String.Equals((string)model, "gpt-4o-prod", StringComparison.Ordinal)' in pin
    assert "} catch { return true; }" in pin
    if provider == ModelProvider.AZURE_OPENAI:
        assert "chat-completions" not in pin


def test_provider_without_a_curated_supported_operation_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(access_policy, "operations_for", lambda *_: ())
    with pytest.raises(ValidationError, match="no operations supported"):
        render_governed_policy(_publication(), _snapshot())


def _anthropic(**overrides: object) -> Publication:
    values: dict[str, object] = {
        "deployment_name": "claude-sonnet-4-5",
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "api_shape": ApiShape.ANTHROPIC_MESSAGES,
        "enforcement": None,
    }
    values.update(overrides)
    return _publication(**values)


def _unmetered(**overrides: object) -> ModelAccessSnapshot:
    values: dict[str, object] = {
        "publication_enforcement": None,
        "grants": [_grant(enforcement=EntitlementEnforcement(requests=_requests()))],
    }
    values.update(overrides)
    return _snapshot(**values)


def test_anthropic_governed_access_permits_only_messages_and_pins_the_model() -> None:
    result = render_governed_policy(_anthropic(), _unmetered())
    fragment = ET.fromstring(result.fragment_xml)

    guard = next(
        condition for condition in _conditions(fragment) if "context.Operation == null" in condition
    )
    assert re.findall(r'context.Operation.Id == "([^"]+)"', guard) == ["messages"]
    assert "count-tokens" not in result.fragment_xml
    assert "This operation is not available through governed access." in result.fragment_xml
    pin = next(
        condition for condition in _conditions(fragment) if "preserveContent: true" in condition
    )
    assert 'if (!(context.Operation.Id == "messages")) return false;' in pin
    assert '!String.Equals((string)model, "claude-sonnet-4-5", StringComparison.Ordinal)' in pin
    facet = next(facet for facet in result.facets if "allowed-operations" in facet.attributes)
    assert facet.attributes["allowed-operations"] == "messages"
    assert facet.summary == "Governed access permits only the Anthropic Messages operation."
    assert "All other operations, including token counting, are denied." in facet.details


def test_anthropic_governed_access_uses_foundry_audience_and_messages_headers() -> None:
    fragment = _fragment(_unmetered(), _anthropic())
    flat = list(fragment.iter())
    identity = fragment.find("authentication-managed-identity")
    assert identity is not None
    assert identity.attrib == {"resource": "https://ai.azure.com"}
    credentials = fragment.find("set-header[@name='Authorization']")
    api_key = fragment.find("set-header[@name='x-api-key']")
    version = fragment.find("set-header[@name='anthropic-version']")
    assert credentials is not None and api_key is not None and version is not None
    assert api_key.attrib["exists-action"] == "delete"
    assert version.attrib["exists-action"] == "skip"
    assert [value.text for value in version.findall("value")] == ["2023-06-01"]
    assert flat.index(credentials) < flat.index(api_key) < flat.index(identity)
    assert flat.index(version) < flat.index(identity)


def test_an_unmetered_publication_renders_no_token_policies_but_keeps_call_limits() -> None:
    result = render_governed_policy(_anthropic(), _unmetered())
    fragment = ET.fromstring(result.fragment_xml)

    assert fragment.find(".//llm-token-limit") is None
    assert fragment.find(".//llm-emit-token-metric") is None
    assert fragment.find(".//rate-limit-by-key") is not None
    assert not any(facet.kind == "tokenLimit" for facet in result.facets)


def test_anthropic_governed_access_on_a_v2_gateway_keeps_token_policies() -> None:
    fragment = _fragment(
        _snapshot(grants=[_grant(enforcement=_full_enforcement())]),
        _anthropic(enforcement=_tokens(tokens_per_minute=9000)),
    )

    assert fragment.findall(".//llm-token-limit")
    assert fragment.find("llm-emit-token-metric") is not None


def test_token_grants_are_refused_when_the_publication_cannot_be_token_metered() -> None:
    tokens = EntitlementEnforcement(tokens=_tokens())
    with pytest.raises(ValidationError, match="can't be token-metered"):
        render_governed_policy(_anthropic(), _unmetered(grants=[_grant(enforcement=tokens)]))

    # A disabled grant is never rendered, so it can't block revoking access.
    render_governed_policy(
        _anthropic(), _unmetered(grants=[_grant(), _grant(2, enabled=False, enforcement=tokens)])
    )


@pytest.mark.parametrize(
    "literal",
    [
        '"); return "attacker"; //',
        '<choose><when condition="@true">',
        "{{runtime-secret}}",
        '@(context.Request.Headers["Authorization"])',
        'quotes"\\ and \r\n\t \x00 \x1f',
        "Unicode \u0085 \u2028 \u2029 \U0001f680",
    ],
)
def test_csharp_literals_round_trip_without_xml_or_named_value_injection(literal: str) -> None:
    encoded = access_policy._literal(literal)
    assert json.loads(encoded) == literal
    assert "{{" not in encoded and "}}" not in encoded
    assert all(ord(character) >= 32 for character in encoded)
    grant = _grant(subscription_name=literal, object_id=literal)
    result = render_governed_policy(
        _publication(deployment_name=literal), _snapshot(grants=[grant])
    )
    fragment = ET.fromstring(result.fragment_xml)
    assert encoded in _variable_values(fragment, "mosaic-key-grant")[-1]
    assert encoded in _variable_values(fragment, "mosaic-token-grant")[-1]
    assert fragment.find(".//set-body").text == "Model access denied."  # type: ignore[union-attr]
    assert "{{runtime-secret}}" not in result.fragment_xml
    assert result.unrecognized_elements == []
    assert literal not in _facets_json(result)


@pytest.mark.parametrize("field", ["backend_name", "fragment_name"])
@pytest.mark.parametrize(
    "name", ['@("attacker")', "{{secret}}", 'name" /><send-request />', "name\x00"]
)
def test_resource_name_interpolation_is_rejected_not_evaluated_or_echoed(
    field: str, name: str
) -> None:
    with pytest.raises(ValidationError, match="literal APIM") as error:
        render_governed_policy(_publication(**{field: name}), _snapshot())
    assert name not in str(error.value)


def test_facets_are_redacted_semantics_not_markup_claims_or_counter_expressions() -> None:
    grant = _grant(
        display_name="private-display-secret",
        object_id="private-oid-secret",
        subscription_name="private-subscription-secret",
        intent_digest="private-intent-secret",
        enforcement=_full_enforcement(),
    )
    result = render_governed_policy(_publication(), _snapshot(grants=[grant]))
    serialized = _facets_json(result)
    assert "<" not in serialized
    assert "@(" not in serialized and "@{" not in serialized
    assert "private-" not in serialized
    assert "per custom expression" not in serialized
    assert "per subscription." not in serialized
    assert all(facet.managed_by_mosaic for facet in result.facets)
    limits = [facet for facet in result.facets if "counter-scope" in facet.attributes]
    assert len(limits) == 4
    assert all(facet.attributes["counter-scope"] == "stable-grant" for facet in limits)
    assert all("counter-key" not in facet.attributes for facet in result.facets)
    assert all(
        "per stable tenant/publication/entitlement grant" in facet.summary for facet in limits
    )
    assert any(
        "not exact global or billing totals" in detail
        for facet in limits
        for detail in facet.details
    )
    assert any("Removes the subscription-key" in facet.summary for facet in result.facets)
    assert result.unrecognized_elements == []


def test_group_grant_facets_describe_matching_precedence_and_per_member_limits() -> None:
    result = render_governed_policy(
        _publication(),
        _snapshot(grants=[_grant(), _group_grant(2), _group_grant(3, enabled=False)]),
    )
    facet = result.facets[0]
    assert facet.attributes["security-group-grants"] == "1"
    assert any("1 enabled security-group grant" in detail for detail in facet.details)
    assert any("groups claim" in detail for detail in facet.details)
    assert any("Entra tokens only" in detail for detail in facet.details)
    assert any("separately to each validated member" in detail for detail in facet.details)
    assert any("direct user or application grant" in detail for detail in facet.details)


def test_direct_only_facets_remain_without_group_language() -> None:
    result = render_governed_policy(_publication(), _snapshot())
    facet = result.facets[0]
    assert "security-group-grants" not in facet.attributes
    assert not any("security-group grant" in detail for detail in facet.details)


def test_fragment_size_boundary_is_inclusive(monkeypatch: pytest.MonkeyPatch) -> None:
    publication, snapshot = _publication(), _snapshot()
    size = len(render_governed_policy(publication, snapshot).fragment_xml.encode("utf-8"))
    monkeypatch.setattr(access_policy, "MAX_FRAGMENT_BYTES", size)
    render_governed_policy(publication, snapshot)
    monkeypatch.setattr(access_policy, "MAX_FRAGMENT_BYTES", size - 1)
    with pytest.raises(ValidationError, match="512 KB UTF-8"):
        render_governed_policy(publication, snapshot)


def test_fragment_size_measures_utf8_bytes_not_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    def unicode_serializer(element: ET.Element) -> str:
        return _serialize(element).replace("<fragment>", "<fragment><!-- \U0001f680 -->", 1)

    monkeypatch.setattr(access_policy, "_serialize", unicode_serializer)
    result = render_governed_policy(_publication(), _snapshot())
    assert len(result.fragment_xml.encode("utf-8")) > len(result.fragment_xml)
    monkeypatch.setattr(access_policy, "MAX_FRAGMENT_BYTES", len(result.fragment_xml))
    with pytest.raises(ValidationError, match="512 KB UTF-8"):
        render_governed_policy(_publication(), _snapshot())


def test_actual_documented_512_kib_fragment_limit_is_enforced() -> None:
    assert access_policy.MAX_FRAGMENT_BYTES == 512 * 1024
    with pytest.raises(ValidationError, match="512 KB UTF-8"):
        render_governed_policy(
            _publication(), _snapshot(grants=[_grant(subscription_name="x" * (512 * 1024))])
        )
