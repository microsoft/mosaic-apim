"""Provider classification.

Detection decides which rows arrive pre-checked in the import dialog, so the cost of a false
positive is an administrator adopting an API they did not mean to. These tests pin both directions:
the providers MOSAIC claims to recognise, and the ordinary APIs it must leave alone.
"""

import pytest
from mosaic_api.integrations.apim.ai_detection import classify_api, classify_url
from mosaic_api.observed import AiBackendKind


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://contoso.openai.azure.com/openai", AiBackendKind.AZURE_OPENAI),
        ("https://contoso.api.cognitive.microsoft.com", AiBackendKind.AZURE_OPENAI),
        ("https://mosaic.services.ai.azure.com", AiBackendKind.AZURE_AI_FOUNDRY),
        ("https://mosaic.cognitiveservices.azure.com", AiBackendKind.AZURE_AI_FOUNDRY),
        ("https://phi.inference.ai.azure.com", AiBackendKind.AZURE_AI_INFERENCE),
        ("https://api.openai.com/v1", AiBackendKind.OPEN_AI),
        ("https://api.anthropic.com/v1", AiBackendKind.ANTHROPIC),
        ("https://us-central1-aiplatform.googleapis.com", AiBackendKind.GOOGLE_VERTEX),
        ("https://generativelanguage.googleapis.com", AiBackendKind.GOOGLE_VERTEX),
        ("https://bedrock-runtime.us-east-1.amazonaws.com", AiBackendKind.AWS_BEDROCK),
        ("https://bedrock.eu-west-1.amazonaws.com", AiBackendKind.AWS_BEDROCK),
    ],
)
def test_known_provider_hosts_are_classified(url: str, expected: AiBackendKind) -> None:
    assert classify_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        None,
        "",
        "not a url",
        "https://orders.contoso.com/api",
        "https://contoso-fn.azurewebsites.net/api",
        # A different Amazon service must not be mistaken for Bedrock just because of the suffix.
        "https://s3.us-east-1.amazonaws.com",
        # Nor a lookalike host that merely ends with a provider domain.
        "https://evil-api.openai.com.attacker.test",
    ],
)
def test_unrelated_hosts_are_not_model_backends(url: str | None) -> None:
    assert classify_url(url) == AiBackendKind.NONE


def test_signal_names_the_provider_it_found() -> None:
    kind, signals = classify_api(service_url="https://api.anthropic.com/v1")

    assert kind == AiBackendKind.ANTHROPIC
    assert signals == ["Backend URL points at Anthropic."]


def test_bedrock_converse_operations_are_recognised_without_a_known_host() -> None:
    kind, signals = classify_api(
        service_url="https://gateway.contoso.com",
        operation_templates=["/model/{modelId}/converse"],
    )

    assert kind == AiBackendKind.OTHER_LLM
    assert "Exposes model inference operations." in signals


def test_google_generate_content_operations_are_recognised() -> None:
    kind, _ = classify_api(
        service_url="https://gateway.contoso.com",
        operation_templates=["/v1beta/models/{model}:generateContent"],
    )

    assert kind == AiBackendKind.OTHER_LLM


def test_a_recognised_host_outranks_a_generic_operation_signal() -> None:
    kind, signals = classify_api(
        service_url="https://contoso.openai.azure.com",
        operation_templates=["/chat/completions"],
    )

    assert kind == AiBackendKind.AZURE_OPENAI
    assert len(signals) == 2


def test_an_ordinary_api_produces_no_signals() -> None:
    kind, signals = classify_api(
        service_url="https://orders.contoso.com",
        path="orders",
        operation_templates=["/orders/{orderId}"],
    )

    assert kind == AiBackendKind.NONE
    assert signals == []


def test_generic_message_paths_do_not_pre_check_an_api() -> None:
    """``/messages`` is common enough in ordinary REST APIs that matching it would mislead."""

    kind, _ = classify_api(
        service_url="https://chat.contoso.com",
        operation_templates=["/conversations/{id}/messages"],
    )

    assert kind == AiBackendKind.NONE
