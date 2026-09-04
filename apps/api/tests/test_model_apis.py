"""Curated provider shapes and the policy a publication authors.

These are pure functions with no Azure involvement, so they are the cheapest place to pin the
determinism the plan depends on.
"""

import pytest
from mosaic_api.domain import (
    ModelProvider,
    Publication,
    PublicationStatus,
    TokenEnforcement,
    apim_slug,
    publication_id,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.apim.model_apis import (
    CURATED_SHAPE_VERSION,
    backend_url,
    curated_operations,
    default_names,
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
