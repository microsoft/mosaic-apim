import pytest
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
