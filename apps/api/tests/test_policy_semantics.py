import json

from mosaic_api.integrations.apim.policy_semantics import (
    analyze_policy,
    content_digest,
    describe_counter_key,
    summarize_facets,
)
from mosaic_api.observed import FacetConfidence, PolicyFacetKind, PolicySection

AI_GATEWAY_POLICY = """
<policies>
  <inbound>
    <base />
    <authentication-managed-identity resource="https://cognitiveservices.azure.com" />
    <llm-token-limit counter-key="@(context.Subscription.Id)"
                     tokens-per-minute="10000"
                     token-quota="500000"
                     token-quota-period="Monthly"
                     estimate-prompt-tokens="true" />
    <llm-emit-token-metric>
      <dimension name="Subscription ID" />
    </llm-emit-token-metric>
    <set-backend-service backend-id="foundry-pool" />
  </inbound>
  <backend>
    <base />
  </backend>
  <outbound>
    <base />
  </outbound>
  <on-error>
    <base />
  </on-error>
</policies>
"""

SECRET_BEARING_POLICY = """
<policies>
  <inbound>
    <base />
    <set-header name="Authorization" exists-action="override">
      <value>Bearer sk-live-01234567890abcdef</value>
    </set-header>
    <set-header name="api-key" exists-action="override">
      <value>{{openai-primary-key}}</value>
    </set-header>
    <authentication-basic username="svc-account" password="hunter2-super-secret" />
  </inbound>
</policies>
"""

UNRECOGNIZED_POLICY = """
<policies>
  <inbound>
    <base />
    <rate-limit calls="120" renewal-period="60" />
    <acme-proprietary-throttle limit="5" />
    <another-unknown-thing />
  </inbound>
</policies>
"""

CONDITIONAL_POLICY = """
<policies>
  <inbound>
    <base />
    <choose>
      <when condition="@(context.Product.Name == &quot;premium&quot;)">
        <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="50000" />
      </when>
      <otherwise>
        <llm-token-limit counter-key="@(context.Subscription.Id)" tokens-per-minute="1000" />
      </otherwise>
    </choose>
  </inbound>
</policies>
"""


def _serialized(xml: str) -> str:
    analysis = analyze_policy(xml)
    return json.dumps([facet.model_dump(mode="json") for facet in analysis.facets])


def test_ai_gateway_policy_is_described_in_plain_language() -> None:
    analysis = analyze_policy(AI_GATEWAY_POLICY)
    summaries = {facet.element: facet.summary for facet in analysis.facets}

    assert "10,000 tokens per minute" in summaries["llm-token-limit"]
    assert "500,000 tokens per month" in summaries["llm-token-limit"]
    assert "counted per subscription" in summaries["llm-token-limit"]
    assert "managed identity" in summaries["authentication-managed-identity"]
    assert "foundry-pool" in summaries["set-backend-service"]
    assert analysis.unrecognized_elements == []


def test_facets_carry_kind_and_section() -> None:
    analysis = analyze_policy(AI_GATEWAY_POLICY)
    by_element = {facet.element: facet for facet in analysis.facets}

    assert by_element["llm-token-limit"].kind == PolicyFacetKind.TOKEN_LIMIT
    assert by_element["llm-token-limit"].section == PolicySection.INBOUND
    assert by_element["llm-emit-token-metric"].kind == PolicyFacetKind.OBSERVABILITY
    assert by_element["set-backend-service"].kind == PolicyFacetKind.ROUTING


def test_base_elements_are_not_reported_as_policy_statements() -> None:
    analysis = analyze_policy(AI_GATEWAY_POLICY)

    assert all(facet.element != "base" for facet in analysis.facets)
    assert analysis.element_count == len(analysis.facets)


def test_raw_policy_markup_never_leaves_the_parser() -> None:
    for xml in (AI_GATEWAY_POLICY, SECRET_BEARING_POLICY, UNRECOGNIZED_POLICY):
        serialized = _serialized(xml)
        assert "<" not in serialized
        assert "policies" not in serialized or "policies" in serialized.casefold()


def test_secret_values_are_redacted() -> None:
    serialized = _serialized(SECRET_BEARING_POLICY)

    assert "sk-live-01234567890abcdef" not in serialized
    assert "hunter2-super-secret" not in serialized


def test_sensitive_headers_are_flagged_without_showing_the_value() -> None:
    analysis = analyze_policy(SECRET_BEARING_POLICY)
    headers = [facet for facet in analysis.facets if facet.element == "set-header"]

    assert any("credential" in detail for facet in headers for detail in facet.details)
    assert all("Bearer" not in facet.summary for facet in headers)


def test_unrecognized_elements_are_reported_not_hidden() -> None:
    analysis = analyze_policy(UNRECOGNIZED_POLICY)

    assert analysis.unrecognized_elements == ["acme-proprietary-throttle", "another-unknown-thing"]
    unknown = [
        facet for facet in analysis.facets if facet.confidence == FacetConfidence.UNRECOGNIZED
    ]
    assert len(unknown) == 2
    assert all(facet.kind == PolicyFacetKind.UNRECOGNIZED for facet in unknown)
    assert all("authored outside MOSAIC" in facet.summary for facet in unknown)


def test_recognized_rate_limit_is_summarized() -> None:
    analysis = analyze_policy(UNRECOGNIZED_POLICY)
    rate_limit = next(facet for facet in analysis.facets if facet.element == "rate-limit")

    assert rate_limit.summary == "Allows 120 calls per minute, counted per subscription."


def test_nested_conditional_policies_are_found_and_labelled() -> None:
    analysis = analyze_policy(CONDITIONAL_POLICY)
    limits = [facet for facet in analysis.facets if facet.element == "llm-token-limit"]

    assert len(limits) == 2
    assert all(
        "Applied only when a condition matches." in facet.details for facet in limits
    )


def test_mosaic_authored_fragments_are_identified() -> None:
    analysis = analyze_policy(
        '<policies><inbound><include-fragment fragment-id="mosaic-rate-standard" />'
        '<include-fragment fragment-id="corp-logging" /></inbound></policies>'
    )
    by_fragment = {facet.attributes["fragment-id"]: facet for facet in analysis.facets}

    assert by_fragment["mosaic-rate-standard"].managed_by_mosaic is True
    assert by_fragment["corp-logging"].managed_by_mosaic is False
    assert analysis.references_mosaic_fragment is True


def test_malformed_policy_is_reported_without_raising() -> None:
    analysis = analyze_policy("<policies><inbound><base /></inbound>")

    assert analysis.unrecognized_elements == ["unparseable"]
    assert analysis.facets[0].summary == "MOSAIC could not read this policy document."


def test_empty_policy_produces_no_facets() -> None:
    analysis = analyze_policy("   ")

    assert analysis.facets == []
    assert analysis.element_count == 0


def test_digest_is_stable_and_change_sensitive() -> None:
    assert content_digest(AI_GATEWAY_POLICY) == content_digest(AI_GATEWAY_POLICY)
    assert content_digest(AI_GATEWAY_POLICY) != content_digest(UNRECOGNIZED_POLICY)
    assert analyze_policy(AI_GATEWAY_POLICY).content_sha256 == content_digest(AI_GATEWAY_POLICY)


def test_summarize_facets_counts_confidence_and_ownership() -> None:
    analysis = analyze_policy(UNRECOGNIZED_POLICY)
    recognized, unrecognized, managed = summarize_facets(analysis.facets)

    assert recognized == 1
    assert unrecognized == 2
    assert managed == 0


def test_counter_key_expressions_become_plain_language() -> None:
    assert describe_counter_key("@(context.Subscription.Id)") == "per subscription"
    assert describe_counter_key("@(context.Request.IpAddress)") == "per caller IP address"
    assert (
        describe_counter_key(
            '@(context.Request.Headers.GetValueOrDefault("x-tenant-id", "anonymous"))'
        )
        == "per x-tenant-id request header"
    )
    assert (
        describe_counter_key('@(context.Request.Headers.GetValueOrDefault("Authorization","")'
                             '.AsJwt()?.Claims.GetValueOrDefault("oid", ""))')
        == "per Entra user"
    )
    assert (
        describe_counter_key('@(context.Request.Headers.GetValueOrDefault("Authorization","")'
                             '.AsJwt()?.Claims.GetValueOrDefault("department", ""))')
        == "per token claim department"
    )
    assert describe_counter_key("shared-counter") == "shared across all callers"
    assert describe_counter_key(None) == "per subscription"
    assert describe_counter_key("@(SomethingUnusual())") == "per custom expression"
