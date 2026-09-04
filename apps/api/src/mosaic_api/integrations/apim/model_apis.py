"""Curated API Management operation sets for published models.

MOSAIC ships these rather than fetching a provider's OpenAPI document at apply time. A plan has to
be deterministic and reviewable before an administrator approves it, and a plan whose contents
depend on a third-party document being reachable is neither. The trade is that a provider adding an
operation needs a MOSAIC release; see ADR 0010.

Operation URL templates carry the *literal* deployment name rather than a template parameter. A
publication governs exactly one model, so an API that would happily forward a request naming a
different deployment would be a governance hole rather than a convenience.
"""

from dataclasses import dataclass

from mosaic_api.domain import (
    MOSAIC_RESOURCE_PREFIX,
    ModelProvider,
    Publication,
    apim_slug,
    publication_slug,
)
from mosaic_api.errors import ValidationError

# Bumped whenever a curated set changes. A Publication records the version that produced it, so an
# API published by an older MOSAIC is identifiable rather than silently assumed to be current.
CURATED_SHAPE_VERSION = "1.0"

AZURE_OPENAI_HOST_SUFFIXES: tuple[str, ...] = (".openai.azure.com", ".api.cognitive.microsoft.com")


@dataclass(frozen=True)
class OperationSpec:
    """One API Management operation. ``url_template`` is relative to the API path."""

    name: str
    display_name: str
    method: str
    url_template: str
    description: str


def _azure_openai_operations(deployment: str) -> tuple[OperationSpec, ...]:
    base = f"/openai/deployments/{deployment}"
    return (
        OperationSpec(
            name="chat-completions",
            display_name="Create chat completion",
            method="POST",
            url_template=f"{base}/chat/completions",
            description=f"Chat completions against the {deployment} deployment.",
        ),
        OperationSpec(
            name="completions",
            display_name="Create completion",
            method="POST",
            url_template=f"{base}/completions",
            description=f"Legacy text completions against the {deployment} deployment.",
        ),
        OperationSpec(
            name="embeddings",
            display_name="Create embeddings",
            method="POST",
            url_template=f"{base}/embeddings",
            description=f"Embeddings against the {deployment} deployment.",
        ),
        OperationSpec(
            name="images-generations",
            display_name="Create image",
            method="POST",
            url_template=f"{base}/images/generations",
            description=f"Image generation against the {deployment} deployment.",
        ),
        OperationSpec(
            name="audio-transcriptions",
            display_name="Create transcription",
            method="POST",
            url_template=f"{base}/audio/transcriptions",
            description=f"Audio transcription against the {deployment} deployment.",
        ),
        OperationSpec(
            name="audio-translations",
            display_name="Create translation",
            method="POST",
            url_template=f"{base}/audio/translations",
            description=f"Audio translation against the {deployment} deployment.",
        ),
        OperationSpec(
            name="responses",
            display_name="Create response",
            method="POST",
            url_template="/openai/responses",
            description="Responses API. Not deployment-scoped in the provider contract.",
        ),
    )


def _ai_services_operations(deployment: str) -> tuple[OperationSpec, ...]:
    return (
        OperationSpec(
            name="chat-completions",
            display_name="Create chat completion",
            method="POST",
            url_template="/models/chat/completions",
            description=f"Foundry Models chat completions routed to {deployment}.",
        ),
        OperationSpec(
            name="embeddings",
            display_name="Create embeddings",
            method="POST",
            url_template="/models/embeddings",
            description=f"Foundry Models embeddings routed to {deployment}.",
        ),
        OperationSpec(
            name="model-info",
            display_name="Get model info",
            method="GET",
            url_template="/models/info",
            description="Describe the model behind this route.",
        ),
    )


def curated_operations(provider: ModelProvider, deployment_name: str) -> tuple[OperationSpec, ...]:
    """The operation set MOSAIC publishes for a provider.

    An OpenAI-compatible endpoint has no curated shape: ADR 0006 registers those endpoints without
    listing their models, so MOSAIC has never observed what it would be publishing. Refusing is
    better than guessing at a contract and creating an API that silently 404s.
    """

    if provider == ModelProvider.AZURE_OPENAI:
        return _azure_openai_operations(deployment_name)
    if provider == ModelProvider.AZURE_AI_FOUNDRY:
        return _ai_services_operations(deployment_name)
    raise ValidationError(
        "MOSAIC has no curated API shape for this provider, so it cannot publish from it yet.",
        details={"provider": str(provider), "shapeVersion": CURATED_SHAPE_VERSION},
    )


def is_azure_openai_host(endpoint: str) -> bool:
    host = endpoint.split("//", 1)[-1].split("/", 1)[0].casefold()
    return host.endswith(AZURE_OPENAI_HOST_SUFFIXES)


def backend_url(endpoint: str) -> str:
    """The origin the published API forwards to.

    Trailing path is stripped because the curated operations carry the provider's own path
    segments. Query and fragment go with it: ADR 0004 established that a backend URL routinely
    carries credentials in its query string, and one must never reach a stored record.
    """

    without_fragment = endpoint.split("#", 1)[0].split("?", 1)[0]
    scheme, separator, remainder = without_fragment.partition("//")
    if not separator:
        return without_fragment.rstrip("/")
    host = remainder.split("/", 1)[0]
    return f"{scheme}//{host}"


@dataclass(frozen=True)
class PublicationNames:
    api_name: str
    api_path: str
    backend_name: str
    fragment_name: str
    product_name: str
    subscription_name: str


def default_names(endpoint_name: str, deployment_name: str) -> PublicationNames:
    """Deterministic ``mosaic-`` names so the portal shows plainly what MOSAIC owns.

    The prefix is the same one ADR 0004's fragment detection already recognises, so a published
    fragment is reported as MOSAIC-managed by the existing policy view without special-casing.
    """

    slug = publication_slug(endpoint_name, deployment_name)
    stem = f"{MOSAIC_RESOURCE_PREFIX}{slug}"
    return PublicationNames(
        api_name=stem,
        api_path=f"{MOSAIC_RESOURCE_PREFIX.rstrip('-')}/{slug}",
        backend_name=stem,
        fragment_name=stem,
        product_name=stem,
        subscription_name=stem,
    )


def display_name_for(endpoint_name: str, deployment_name: str) -> str:
    return f"{endpoint_name} - {deployment_name}"


def suggested_names(endpoint_name: str, deployment_name: str) -> tuple[str, str]:
    names = default_names(endpoint_name, deployment_name)
    return names.api_name, names.api_path


def operations_for(publication: Publication) -> tuple[OperationSpec, ...]:
    return curated_operations(publication.provider, publication.deployment_name)


def endpoint_slug(endpoint_name: str) -> str:
    return apim_slug(endpoint_name)
