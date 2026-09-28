"""Curated provider shapes and the policy a publication authors.

These are pure functions with no Azure involvement, so they are the cheapest place to pin the
determinism the plan depends on.
"""

import xml.etree.ElementTree as ET

import pytest
from mosaic_api.domain import (
    ApiShape,
    DeploymentCapability,
    GatewayTier,
    ModelProvider,
    Publication,
    PublicationStatus,
    TokenEnforcement,
    apim_slug,
    gateway_tier,
    publication_id,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.apim.model_apis import (
    CURATED_SHAPE_VERSION,
    DeploymentFit,
    anthropic_origin,
    assess_deployment,
    backend_origin,
    backend_url,
    classify_deployment,
    curated_operations,
    default_names,
    operations_for,
    required_data_actions,
    shape_operations,
    token_limits_note,
    token_limits_supported,
)
from mosaic_api.integrations.policy import render_publication_policy


def _publication(**overrides: object) -> Publication:
    payload: dict[str, object] = {
        "id": publication_id("tenant-test", "gateway_1", "endpoint_1", "gpt-4o-prod"),
        "tenant_id": "tenant-test",
        "gateway_id": "gateway_1",
        "model_endpoint_id": "endpoint_1",
        "deployment_name": "gpt-4o-prod",
        "provider": ModelProvider.AZURE_OPENAI,
        "display_name": "Contoso - gpt-4o-prod",
        "api_name": "mosaic-contoso-gpt-4o-prod",
        "api_path": "mosaic/contoso-gpt-4o-prod",
        "backend_name": "mosaic-contoso-gpt-4o-prod",
        "fragment_name": "mosaic-contoso-gpt-4o-prod",
        "product_name": "mosaic-contoso-gpt-4o-prod",
        "subscription_name": "mosaic-contoso-gpt-4o-prod",
        "enforcement": TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=12000
        ),
        "shape_version": CURATED_SHAPE_VERSION,
    }
    payload.update(overrides)
    return Publication.model_validate(payload)


def test_azure_openai_operations_are_stable_and_deployment_scoped() -> None:
    operations = curated_operations(ModelProvider.AZURE_OPENAI, "gpt-4o-prod")

    assert [item.name for item in operations] == [
        "chat-completions",
        "completions",
        "embeddings",
        "images-generations",
        "audio-transcriptions",
        "audio-translations",
        "responses",
    ]
    chat = operations[0]
    assert chat.method == "POST"
    assert chat.url_template == "/openai/deployments/gpt-4o-prod/chat/completions"
    # The responses API is not deployment-scoped in the provider contract.
    assert operations[-1].url_template == "/openai/responses"


def test_ai_services_operations_use_the_foundry_models_route() -> None:
    operations = curated_operations(ModelProvider.AZURE_AI_FOUNDRY, "llama-3")

    assert [item.url_template for item in operations] == [
        "/models/chat/completions",
        "/models/embeddings",
        "/models/info",
    ]


def test_an_openai_compatible_endpoint_has_no_curated_shape() -> None:
    with pytest.raises(ValidationError) as error:
        curated_operations(ModelProvider.OPENAI_COMPATIBLE, "anything")

    assert "no curated API shape" in str(error.value.message)


_ACCOUNTS = "Microsoft.CognitiveServices/accounts"


def test_each_operation_declares_the_data_action_its_route_requires() -> None:
    # As ``az provider operation show --namespace Microsoft.CognitiveServices`` lists them. The
    # gateway runtime check is derived from these, so a wrong one is a wrong readiness answer.
    assert {
        item.name: item.data_action
        for item in curated_operations(ModelProvider.AZURE_OPENAI, "gpt-4o-prod")
    } == {
        "chat-completions": f"{_ACCOUNTS}/OpenAI/deployments/chat/completions/action",
        "completions": f"{_ACCOUNTS}/OpenAI/deployments/completions/action",
        "embeddings": f"{_ACCOUNTS}/OpenAI/deployments/embeddings/action",
        "images-generations": f"{_ACCOUNTS}/OpenAI/images/generations/action",
        "audio-transcriptions": f"{_ACCOUNTS}/OpenAI/deployments/audio/action",
        "audio-translations": f"{_ACCOUNTS}/OpenAI/deployments/audio/action",
        "responses": f"{_ACCOUNTS}/OpenAI/responses/write",
    }
    assert {
        item.name: item.data_action
        for item in curated_operations(ModelProvider.AZURE_AI_FOUNDRY, "llama-3")
    } == {
        "chat-completions": f"{_ACCOUNTS}/MaaS/chat/completions/action",
        "embeddings": f"{_ACCOUNTS}/MaaS/embeddings/action",
        "model-info": f"{_ACCOUNTS}/MaaS/info/read",
    }
    # Foundry authorizes its provider-native routes, /anthropic/* among them, with the one
    # provider-model action; the operation list has nothing Anthropic- or Messages-specific.
    assert {
        item.name: item.data_action
        for item in shape_operations(ApiShape.ANTHROPIC_MESSAGES, "claude-sonnet-4-5")
    } == {
        "messages": f"{_ACCOUNTS}/AIServices/providers/action",
        "count-tokens": f"{_ACCOUNTS}/AIServices/providers/action",
    }


def test_required_data_actions_are_the_distinct_actions_of_a_shape() -> None:
    # Both audio routes share one data action, so it is required once; so do both Messages routes.
    assert len(required_data_actions(ApiShape.AZURE_OPENAI)) == 6
    assert required_data_actions(ApiShape.FOUNDRY_MODELS) == (
        f"{_ACCOUNTS}/MaaS/chat/completions/action",
        f"{_ACCOUNTS}/MaaS/embeddings/action",
        f"{_ACCOUNTS}/MaaS/info/read",
    )
    assert required_data_actions(ApiShape.ANTHROPIC_MESSAGES) == (
        f"{_ACCOUNTS}/AIServices/providers/action",
    )


@pytest.mark.parametrize("shape", list(ApiShape))
def test_every_shape_declares_data_actions_in_the_cognitive_services_namespace(
    shape: ApiShape,
) -> None:
    actions = required_data_actions(shape)

    assert actions
    assert all(action.startswith(f"{_ACCOUNTS}/") for action in actions)


def test_names_are_deterministic_and_prefixed_for_ownership() -> None:
    first = default_names("Contoso AOAI", "gpt-4o-prod")
    second = default_names("Contoso AOAI", "gpt-4o-prod")

    assert first == second
    assert first.api_name == "mosaic-contoso-aoai-gpt-4o-prod"
    assert first.api_path == "mosaic/contoso-aoai-gpt-4o-prod"
    # The fragment prefix is the one ADR 0004's detection already recognises.
    assert first.fragment_name.startswith("mosaic-")


def test_slug_drops_characters_apim_will_not_accept() -> None:
    assert apim_slug("Contoso (EU) / Prod!") == "contoso-eu-prod"


def test_backend_url_drops_path_query_and_fragment() -> None:
    assert (
        backend_url("https://contoso.openai.azure.com/openai?sig=SasTokenSecret")
        == "https://contoso.openai.azure.com"
    )
    assert backend_url("https://contoso.services.ai.azure.com/") == (
        "https://contoso.services.ai.azure.com"
    )


def test_publication_policy_puts_enforcement_in_a_fragment_the_api_includes() -> None:
    publication = _publication()

    first = render_publication_policy(publication)
    second = render_publication_policy(publication)

    assert first.content_sha256 == second.content_sha256
    assert first.fragment_xml.startswith("<fragment>")
    assert 'backend-id="mosaic-contoso-gpt-4o-prod"' in first.fragment_xml
    assert 'tokens-per-minute="12000"' in first.fragment_xml
    assert '<authentication-managed-identity resource="https://cognitiveservices.azure.com"' in (
        first.fragment_xml
    )
    # The API policy is a thin include, so MOSAIC never owns rules in two places.
    assert '<include-fragment fragment-id="mosaic-contoso-gpt-4o-prod"' in first.api_policy_xml
    assert "llm-token-limit" not in first.api_policy_xml


def test_publication_policy_facets_carry_no_markup() -> None:
    result = render_publication_policy(_publication())

    assert result.facets
    assert any(facet.kind == "tokenLimit" for facet in result.facets)
    assert any(facet.managed_by_mosaic for facet in result.facets)
    assert all("<" not in facet.summary for facet in result.facets)
    assert not result.unrecognized_elements


def test_changing_enforcement_changes_the_policy_digest() -> None:
    base = render_publication_policy(_publication())
    changed = render_publication_policy(
        _publication(
            enforcement=TokenEnforcement(
                counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=1
            )
        )
    )

    assert base.content_sha256 != changed.content_sha256


def test_created_resources_excludes_anything_mosaic_only_replaced() -> None:
    publication = _publication(
        status=PublicationStatus.PUBLISHED,
        resources=[
            {
                "kind": "backend",
                "name": "pre-existing",
                "resourceId": "/x/backends/pre-existing",
                "createdByMosaic": False,
            },
            {
                "kind": "api",
                "name": "mosaic-contoso-gpt-4o-prod",
                "resourceId": "/x/apis/mosaic-contoso-gpt-4o-prod",
                "createdByMosaic": True,
            },
        ],
    )

    assert [item.name for item in publication.created_resources()] == [
        "mosaic-contoso-gpt-4o-prod"
    ]


# --- API shapes (ADR 0012) -------------------------------------------------------------------

FOUNDRY_ENDPOINT = "https://contoso-ai.cognitiveservices.azure.com/"
AOAI_ENDPOINT = "https://contoso.openai.azure.com/"


def _claude(**overrides: object) -> Publication:
    values: dict[str, object] = {
        "deployment_name": "claude-sonnet-4-5",
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "api_shape": ApiShape.ANTHROPIC_MESSAGES,
        "enforcement": None,
    }
    values.update(overrides)
    return _publication(**values)


def test_anthropic_shape_publishes_the_messages_routes() -> None:
    operations = shape_operations(ApiShape.ANTHROPIC_MESSAGES, "claude-sonnet-4-5")

    assert [(item.name, item.method, item.url_template) for item in operations] == [
        ("messages", "POST", "/anthropic/v1/messages"),
        ("count-tokens", "POST", "/anthropic/v1/messages/count_tokens"),
    ]
    assert operations_for(_claude()) == operations


@pytest.mark.parametrize(
    ("provider", "shape"),
    [
        (ModelProvider.AZURE_OPENAI, ApiShape.AZURE_OPENAI),
        (ModelProvider.AZURE_AI_FOUNDRY, ApiShape.FOUNDRY_MODELS),
    ],
)
def test_a_publication_stored_before_shapes_reads_as_its_providers_default(
    provider: ModelProvider, shape: ApiShape
) -> None:
    stored = _publication(provider=provider).model_dump(by_alias=True, mode="json")
    stored.pop("apiShape")

    publication = Publication.model_validate(stored)

    assert publication.api_shape == shape
    assert operations_for(publication) == curated_operations(provider, "gpt-4o-prod")


@pytest.mark.parametrize(
    ("model_name", "capabilities", "expected"),
    [
        ("gpt-4o", {"chatCompletion": "true"}, DeploymentCapability.CHAT),
        ("Llama-3.3-70B-Instruct", {"chatCompletion": "true"}, DeploymentCapability.CHAT),
        # A realtime model also carries the chat flag, but no chat operation can serve it.
        (
            "gpt-4o-realtime-preview",
            {"chatCompletion": "true", "realtime": "true"},
            DeploymentCapability.REALTIME,
        ),
        ("gpt-realtime", {}, DeploymentCapability.REALTIME),
        ("sora-2", {}, DeploymentCapability.VIDEO),
        ("whisper", {"audio": "true"}, DeploymentCapability.TRANSCRIPTION),
        ("gpt-4o-transcribe", {}, DeploymentCapability.TRANSCRIPTION),
        ("tts-hd", {}, DeploymentCapability.SPEECH),
        ("gpt-4o-mini-tts", {}, DeploymentCapability.SPEECH),
        ("Cohere-rerank-v3.5", {}, DeploymentCapability.RERANK),
        ("dall-e-3", {"imageGenerations": "true"}, DeploymentCapability.IMAGE),
        ("gpt-image-1", {}, DeploymentCapability.IMAGE),
        ("text-embedding-3-large", {"embeddings": "true"}, DeploymentCapability.EMBEDDINGS),
        ("gpt-35-turbo-instruct", {"completion": "true"}, DeploymentCapability.COMPLETION),
        ("codex-mini", {"responses": "true"}, DeploymentCapability.RESPONSES),
        # Partner models often carry no flags at all; a false flag is no flag.
        ("grok-4.3", {}, DeploymentCapability.UNKNOWN),
        ("DeepSeek-V4-Pro", {"chatCompletion": "false"}, DeploymentCapability.UNKNOWN),
    ],
)
def test_classify_deployment(
    model_name: str, capabilities: dict[str, str], expected: DeploymentCapability
) -> None:
    assert classify_deployment(model_name, capabilities) == expected


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://contoso-ai.cognitiveservices.azure.com/",
        "https://contoso-ai.openai.azure.com",
        "https://Contoso-AI.services.ai.azure.com/api/projects/default?sig=secret",
    ],
)
def test_anthropic_origin_uses_the_resources_foundry_host(endpoint: str) -> None:
    assert anthropic_origin(endpoint) == "https://contoso-ai.services.ai.azure.com"
    assert backend_origin(ApiShape.ANTHROPIC_MESSAGES, endpoint) == (
        "https://contoso-ai.services.ai.azure.com"
    )


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://eastus2.api.cognitive.microsoft.com/",
        "https://cognitiveservices.azure.com/",
        "https://a.b.cognitiveservices.azure.com/",
        "https://models.example.com/",
    ],
)
def test_anthropic_origin_refuses_hosts_without_a_resource_subdomain(endpoint: str) -> None:
    with pytest.raises(ValidationError) as error:
        anthropic_origin(endpoint)

    assert "services.ai.azure.com" in error.value.message


def test_other_shapes_forward_to_the_registered_origin() -> None:
    assert backend_origin(ApiShape.FOUNDRY_MODELS, FOUNDRY_ENDPOINT) == (
        "https://contoso-ai.cognitiveservices.azure.com"
    )
    assert backend_origin(ApiShape.AZURE_OPENAI, AOAI_ENDPOINT) == "https://contoso.openai.azure.com"


@pytest.mark.parametrize(
    ("sku", "tier"),
    [
        ("BasicV2", GatewayTier.V2),
        ("StandardV2", GatewayTier.V2),
        ("PremiumV2", GatewayTier.V2),
        ("Developer", GatewayTier.CLASSIC),
        ("Basic", GatewayTier.CLASSIC),
        ("Standard", GatewayTier.CLASSIC),
        ("Premium", GatewayTier.CLASSIC),
        ("Isolated", GatewayTier.CLASSIC),
        ("Consumption", GatewayTier.CONSUMPTION),
        (None, GatewayTier.UNKNOWN),
        ("", GatewayTier.UNKNOWN),
        ("Mystery", GatewayTier.UNKNOWN),
    ],
)
def test_gateway_tier(sku: str | None, tier: GatewayTier) -> None:
    assert gateway_tier(sku) == tier


def test_only_v2_tiers_token_meter_the_anthropic_messages_api() -> None:
    assert token_limits_supported(ApiShape.ANTHROPIC_MESSAGES, GatewayTier.V2)
    for tier in (GatewayTier.CLASSIC, GatewayTier.CONSUMPTION, GatewayTier.UNKNOWN):
        assert not token_limits_supported(ApiShape.ANTHROPIC_MESSAGES, tier)
        # The OpenAI-style shapes keep their existing behaviour on every tier.
        assert token_limits_supported(ApiShape.AZURE_OPENAI, tier)
        assert token_limits_supported(ApiShape.FOUNDRY_MODELS, tier)

    assert token_limits_note(ApiShape.ANTHROPIC_MESSAGES, "StandardV2") is None
    assert token_limits_note(ApiShape.FOUNDRY_MODELS, "Developer") is None
    classic = token_limits_note(ApiShape.ANTHROPIC_MESSAGES, "Developer")
    assert classic is not None
    assert "only on v2 tiers" in classic
    assert "Developer tier, a classic tier" in classic
    assert "call limits" in classic
    consumption = token_limits_note(ApiShape.ANTHROPIC_MESSAGES, "Consumption")
    assert consumption is not None and "Consumption tier" in consumption
    unknown = token_limits_note(ApiShape.ANTHROPIC_MESSAGES, None)
    assert unknown is not None and "access check" in unknown


def _assess(
    provider: ModelProvider = ModelProvider.AZURE_AI_FOUNDRY,
    *,
    model_name: str = "claude-sonnet-4-5",
    model_format: str | None = "Anthropic",
    capabilities: dict[str, str] | None = None,
    endpoint: str = FOUNDRY_ENDPOINT,
    gateway_sku: str | None = "Developer",
) -> DeploymentFit:
    return assess_deployment(
        provider,
        model_name=model_name,
        model_format=model_format,
        capabilities=capabilities or {},
        endpoint=endpoint,
        gateway_sku=gateway_sku,
    )


def test_claude_on_a_classic_gateway_publishes_without_token_limits() -> None:
    fit = _assess()

    assert fit.publishable
    assert fit.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert fit.capability == DeploymentCapability.CHAT
    assert not fit.token_limits_supported
    assert fit.token_limits_note == token_limits_note(ApiShape.ANTHROPIC_MESSAGES, "Developer")


def test_claude_on_a_v2_gateway_keeps_token_limits() -> None:
    fit = _assess(gateway_sku="StandardV2")

    assert fit.publishable
    assert fit.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert fit.token_limits_supported
    assert fit.token_limits_note is None


def test_anthropic_is_recognised_by_format_or_by_name() -> None:
    assert _assess(model_name="house-model").api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert _assess(model_format=None).api_shape == ApiShape.ANTHROPIC_MESSAGES


@pytest.mark.parametrize(
    ("provider", "endpoint", "gateway_sku", "reason"),
    [
        (ModelProvider.AZURE_OPENAI, AOAI_ENDPOINT, "Developer", "Foundry"),
        (
            ModelProvider.AZURE_AI_FOUNDRY,
            "https://eastus2.api.cognitive.microsoft.com/",
            "Developer",
            "services.ai.azure.com",
        ),
        (ModelProvider.AZURE_AI_FOUNDRY, FOUNDRY_ENDPOINT, None, "pricing tier"),
    ],
)
def test_claude_is_not_publishable_where_the_messages_shape_cannot_work(
    provider: ModelProvider, endpoint: str, gateway_sku: str | None, reason: str
) -> None:
    fit = _assess(provider, endpoint=endpoint, gateway_sku=gateway_sku)

    assert not fit.publishable
    assert fit.api_shape is None
    assert fit.unpublishable_reason is not None and reason in fit.unpublishable_reason


@pytest.mark.parametrize(
    ("model_name", "model_format", "capabilities"),
    [
        ("grok-4.3", "xAI", {}),
        ("Llama-3.3-70B-Instruct", "Meta", {"chatCompletion": "true"}),
        ("DeepSeek-V4-Pro", "DeepSeek", {"chatCompletion": "true"}),
        ("text-embedding-3-small", "OpenAI", {"embeddings": "true"}),
    ],
)
def test_other_foundry_models_keep_the_foundry_models_shape(
    model_name: str, model_format: str, capabilities: dict[str, str]
) -> None:
    fit = _assess(model_name=model_name, model_format=model_format, capabilities=capabilities)

    assert fit.publishable
    assert fit.api_shape == ApiShape.FOUNDRY_MODELS
    assert fit.token_limits_supported
    assert fit.token_limits_note is None


@pytest.mark.parametrize(
    ("provider", "model_name", "capabilities", "reason"),
    [
        (ModelProvider.AZURE_OPENAI, "gpt-realtime", {"realtime": "true"}, "WebSocket"),
        (ModelProvider.AZURE_AI_FOUNDRY, "sora-2", {}, "asynchronous jobs API"),
        (ModelProvider.AZURE_OPENAI, "tts-hd", {}, "audio speech API"),
        (ModelProvider.AZURE_AI_FOUNDRY, "Cohere-rerank-v3.5", {}, "rerank API"),
        (
            ModelProvider.AZURE_AI_FOUNDRY,
            "dall-e-3",
            {"imageGenerations": "true"},
            "Image generation needs the images API, which MOSAIC publishes only from Azure "
            "OpenAI resources today.",
        ),
        (ModelProvider.AZURE_AI_FOUNDRY, "whisper", {}, "audio transcription API"),
        (ModelProvider.OPENAI_COMPATIBLE, "gpt-4o", {}, "no curated API shape"),
    ],
)
def test_deployments_no_shape_can_serve_are_not_publishable_with_a_reason(
    provider: ModelProvider, model_name: str, capabilities: dict[str, str], reason: str
) -> None:
    fit = _assess(
        provider,
        model_name=model_name,
        model_format="OpenAI",
        capabilities=capabilities,
        endpoint=AOAI_ENDPOINT,
    )

    assert not fit.publishable
    assert fit.api_shape is None
    assert fit.unpublishable_reason is not None and reason in fit.unpublishable_reason


@pytest.mark.parametrize(
    ("model_name", "capabilities"),
    [
        ("gpt-4o", {"chatCompletion": "true"}),
        ("dall-e-3", {"imageGenerations": "true"}),
        ("whisper", {}),
        ("gpt-35-turbo-instruct", {"completion": "true"}),
        ("mystery-model", {}),
    ],
)
def test_azure_openai_capabilities_with_a_curated_operation_stay_publishable(
    model_name: str, capabilities: dict[str, str]
) -> None:
    fit = _assess(
        ModelProvider.AZURE_OPENAI,
        model_name=model_name,
        model_format="OpenAI",
        capabilities=capabilities,
        endpoint=AOAI_ENDPOINT,
    )

    assert fit.publishable
    assert fit.api_shape == ApiShape.AZURE_OPENAI
    assert fit.token_limits_supported


def _children(fragment_xml: str) -> list[ET.Element]:
    return list(ET.fromstring(fragment_xml))


def test_anthropic_policy_supplies_the_messages_headers_and_foundry_audience() -> None:
    result = render_publication_policy(_claude())
    children = _children(result.fragment_xml)

    # Headers are settled before the gateway attaches its own token and routes the request, and
    # a classic tier can't meter the Messages API, so neither token policy is rendered.
    assert [child.tag for child in children] == [
        "set-header",
        "set-header",
        "authentication-managed-identity",
        "set-backend-service",
    ]
    api_key, version, identity, _ = children
    assert api_key.attrib == {"name": "x-api-key", "exists-action": "delete"}
    assert version.attrib == {"name": "anthropic-version", "exists-action": "skip"}
    assert [value.text for value in version.findall("value")] == ["2023-06-01"]
    assert identity.attrib == {"resource": "https://ai.azure.com"}
    summaries = [facet.summary for facet in result.facets]
    assert "Removes the x-api-key request header." in summaries
    assert (
        "Sets the anthropic-version request header when the request doesn't already carry it."
        in summaries
    )
    assert not any(facet.kind == "tokenLimit" for facet in result.facets)
    assert not result.unrecognized_elements


def test_anthropic_policy_on_a_v2_gateway_keeps_its_token_policies() -> None:
    children = _children(
        render_publication_policy(
            _claude(
                enforcement=TokenEnforcement(
                    counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=5000
                )
            )
        ).fragment_xml
    )

    assert [child.tag for child in children][-2:] == ["llm-token-limit", "llm-emit-token-metric"]
    assert children[-2].get("tokens-per-minute") == "5000"
    assert children[2].attrib == {"resource": "https://ai.azure.com"}


@pytest.mark.parametrize(
    ("provider", "shape"),
    [
        (ModelProvider.AZURE_OPENAI, ApiShape.AZURE_OPENAI),
        (ModelProvider.AZURE_AI_FOUNDRY, ApiShape.FOUNDRY_MODELS),
    ],
)
def test_openai_style_policies_are_unchanged_by_shapes(
    provider: ModelProvider, shape: ApiShape
) -> None:
    stored = _publication(provider=provider).model_dump(by_alias=True, mode="json")
    stored.pop("apiShape")
    legacy = render_publication_policy(Publication.model_validate(stored))
    explicit = render_publication_policy(_publication(provider=provider, api_shape=shape))

    assert legacy.content_sha256 == explicit.content_sha256
    children = _children(legacy.fragment_xml)
    assert [child.tag for child in children] == [
        "authentication-managed-identity",
        "set-backend-service",
        "llm-token-limit",
        "llm-emit-token-metric",
    ]
    assert children[0].attrib == {"resource": "https://cognitiveservices.azure.com"}
