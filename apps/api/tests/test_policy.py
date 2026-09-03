import json

import pytest
from fastapi.testclient import TestClient
from mosaic_api.domain import PolicyPreviewRequest, TokenEnforcement
from mosaic_api.integrations.policy import render_policy_preview


def test_policy_preview_is_deterministic_and_uses_documented_policies() -> None:
    request = PolicyPreviewRequest(
        enforcement=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            tokens_per_minute=12000,
            token_quota=500000,
            token_quota_period="Monthly",
            estimate_prompt_tokens=True,
        )
    )

    first = render_policy_preview(request)
    second = render_policy_preview(request)

    assert first == second
    assert '<authentication-managed-identity resource="https://cognitiveservices.azure.com"' in (
        first.policy_xml
    )
    assert 'tokens-per-minute="12000"' in first.policy_xml
    assert 'token-quota-period="Monthly"' in first.policy_xml
    assert first.content_sha256 == second.content_sha256


def test_policy_preview_rejects_empty_enforcement() -> None:
    with pytest.raises(ValueError, match="At least one token"):
        PolicyPreviewRequest(enforcement=TokenEnforcement(counter_key_expression='@("group")'))


def test_policy_preview_is_described_in_plain_language() -> None:
    preview = render_policy_preview(
        PolicyPreviewRequest(
            enforcement=TokenEnforcement(
                counter_key_expression="@(context.Subscription.Id)",
                tokens_per_minute=12000,
                estimate_prompt_tokens=True,
            )
        )
    )
    summaries = [facet.summary for facet in preview.facets]

    assert any("12,000 tokens per minute" in summary for summary in summaries)
    assert any("counted per subscription" in summary for summary in summaries)
    assert any("managed identity" in summary for summary in summaries)
    assert preview.unrecognized_elements == []


def test_policy_preview_never_serializes_markup() -> None:
    preview = render_policy_preview(
        PolicyPreviewRequest(
            enforcement=TokenEnforcement(
                counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=100
            )
        )
    )
    serialized = json.dumps(preview.model_dump(mode="json"))

    assert "<" not in serialized
    assert "policyXml" not in serialized
    # The markup still exists in process for a later apply phase; it just never leaves the API.
    assert preview.policy_xml.startswith("<policies>")


def test_policy_preview_endpoint_returns_facets_not_xml(client: TestClient) -> None:
    response = client.post(
        "/api/v1/policies/preview",
        json={
            "enforcement": {
                "counterKeyExpression": "@(context.Subscription.Id)",
                "tokensPerMinute": 5000,
                "estimatePromptTokens": True,
            }
        },
    )

    assert response.status_code == 200
    assert "<" not in response.text
    body = response.json()
    assert "policyXml" not in body
    assert body["contentSha256"]
    assert any("5,000 tokens per minute" in facet["summary"] for facet in body["facets"])
