"""Curated API Management operation sets for published models.

MOSAIC ships these rather than fetching a provider's OpenAPI document at apply time. A plan has to
be deterministic and reviewable before an administrator approves it, and a plan whose contents
depend on a third-party document being reachable is neither. The trade is that a provider adding an
operation needs a MOSAIC release; see ADR 0010.

Operation URL templates carry the *literal* deployment name rather than a template parameter. A
publication governs exactly one model, so an API that would happily forward a request naming a
different deployment would be a governance hole rather than a convenience.

Which set a deployment gets is decided by its API shape, not by its provider alone. A Foundry
resource serves Anthropic models only through the Anthropic Messages API, and a deployment whose
capability no shape covers is reported as not publishable rather than offered a shape that cannot
serve it; see ADR 0012.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from mosaic_api.domain import (
    MOSAIC_RESOURCE_PREFIX,
    ApiShape,
    DeploymentCapability,
    GatewayTier,
    ModelProvider,
    Publication,
    apim_slug,
    bedrock_region,
    default_api_shape,
    gateway_tier,
    publication_slug,
    shape_fits_provider,
)
from mosaic_api.errors import ValidationError

# Bumped whenever a curated set changes. A Publication records the version that produced it, so an
# API published by an older MOSAIC is identifiable rather than silently assumed to be current.
CURATED_SHAPE_VERSION = "1.0"

AZURE_OPENAI_HOST_SUFFIXES: tuple[str, ...] = (".openai.azure.com", ".api.cognitive.microsoft.com")

_DATA_ACTIONS = "Microsoft.CognitiveServices/accounts"


@dataclass(frozen=True)
class OperationSpec:
    """One API Management operation. ``url_template`` is relative to the API path.

    ``data_action`` is the Azure RBAC data action the provider checks when the gateway calls this
    route with its managed identity, as ``az provider operation show --namespace
    Microsoft.CognitiveServices`` lists it. It is required rather than defaulted so that a shape
    gaining a route cannot be published without declaring what the gateway needs to call it; the
    runtime check in ``integrations/aoai/runtime_access.py`` is derived from these, not maintained
    beside them. It is not rendered into API Management.
    """

    name: str
    display_name: str
    method: str
    url_template: str
    description: str
    data_action: str


def _azure_openai_operations(deployment: str) -> tuple[OperationSpec, ...]:
    base = f"/openai/deployments/{deployment}"
    return (
        OperationSpec(
            name="chat-completions",
            display_name="Create chat completion",
            method="POST",
            url_template=f"{base}/chat/completions",
            description=f"Chat completions against the {deployment} deployment.",
            data_action=f"{_DATA_ACTIONS}/OpenAI/deployments/chat/completions/action",
        ),
        OperationSpec(
            name="completions",
            display_name="Create completion",
            method="POST",
            url_template=f"{base}/completions",
            description=f"Legacy text completions against the {deployment} deployment.",
            data_action=f"{_DATA_ACTIONS}/OpenAI/deployments/completions/action",
        ),
        OperationSpec(
            name="embeddings",
            display_name="Create embeddings",
            method="POST",
            url_template=f"{base}/embeddings",
            description=f"Embeddings against the {deployment} deployment.",
            data_action=f"{_DATA_ACTIONS}/OpenAI/deployments/embeddings/action",
        ),
        OperationSpec(
            name="images-generations",
            display_name="Create image",
            method="POST",
            url_template=f"{base}/images/generations",
            description=f"Image generation against the {deployment} deployment.",
            # The provider names this one without a deployments segment, though the route has one.
            data_action=f"{_DATA_ACTIONS}/OpenAI/images/generations/action",
        ),
        OperationSpec(
            name="audio-transcriptions",
            display_name="Create transcription",
            method="POST",
            url_template=f"{base}/audio/transcriptions",
            description=f"Audio transcription against the {deployment} deployment.",
            data_action=f"{_DATA_ACTIONS}/OpenAI/deployments/audio/action",
        ),
        OperationSpec(
            name="audio-translations",
            display_name="Create translation",
            method="POST",
            url_template=f"{base}/audio/translations",
            description=f"Audio translation against the {deployment} deployment.",
            data_action=f"{_DATA_ACTIONS}/OpenAI/deployments/audio/action",
        ),
        OperationSpec(
            name="responses",
            display_name="Create response",
            method="POST",
            url_template="/openai/responses",
            description="Responses API. Not deployment-scoped in the provider contract.",
            data_action=f"{_DATA_ACTIONS}/OpenAI/responses/write",
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
            data_action=f"{_DATA_ACTIONS}/MaaS/chat/completions/action",
        ),
        OperationSpec(
            name="embeddings",
            display_name="Create embeddings",
            method="POST",
            url_template="/models/embeddings",
            description=f"Foundry Models embeddings routed to {deployment}.",
            data_action=f"{_DATA_ACTIONS}/MaaS/embeddings/action",
        ),
        OperationSpec(
            name="model-info",
            display_name="Get model info",
            method="GET",
            url_template="/models/info",
            description="Describe the model behind this route.",
            data_action=f"{_DATA_ACTIONS}/MaaS/info/read",
        ),
    )


def _anthropic_operations(deployment: str) -> tuple[OperationSpec, ...]:
    # Foundry routes these by the request body's model, which must name the deployment. Neither
    # route is deployment-scoped, exactly like the Foundry Models routes above.
    #
    # Foundry authorizes its provider-native routes, /anthropic/* among them, with one data action.
    # ``az provider operation show --namespace Microsoft.CognitiveServices`` lists it as "Perform
    # an action on a provider model" and lists no Anthropic- or Messages-specific action. The
    # backend host (services.ai.azure.com) and token audience (ai.azure.com) differ from the other
    # shapes, but the account and its role assignments are the same; see ADR 0013.
    return (
        OperationSpec(
            name="messages",
            display_name="Create message",
            method="POST",
            url_template="/anthropic/v1/messages",
            description=f"Anthropic Messages routed to {deployment}.",
            data_action=f"{_DATA_ACTIONS}/AIServices/providers/action",
        ),
        OperationSpec(
            name="count-tokens",
            display_name="Count message tokens",
            method="POST",
            url_template="/anthropic/v1/messages/count_tokens",
            description=f"Count the input tokens of a message for {deployment}.",
            data_action=f"{_DATA_ACTIONS}/AIServices/providers/action",
        ),
    )


_SHAPE_OPERATIONS: dict[str, Callable[[str], tuple[OperationSpec, ...]]] = {
    ApiShape.AZURE_OPENAI: _azure_openai_operations,
    ApiShape.FOUNDRY_MODELS: _ai_services_operations,
    ApiShape.ANTHROPIC_MESSAGES: _anthropic_operations,
}


def _no_shape(provider: str) -> ValidationError:
    return ValidationError(
        "MOSAIC has no curated API shape for this provider, so it cannot publish from it yet.",
        details={"provider": str(provider), "shapeVersion": CURATED_SHAPE_VERSION},
    )


def shape_operations(shape: str, deployment_name: str) -> tuple[OperationSpec, ...]:
    """The operation set MOSAIC publishes for an API shape."""

    return _SHAPE_OPERATIONS[shape](deployment_name)


def curated_operations(provider: ModelProvider, deployment_name: str) -> tuple[OperationSpec, ...]:
    """The operation set a provider's deployments get when their format needs no special shape.

    An OpenAI-compatible endpoint has no curated shape: ADR 0006 registers those endpoints without
    listing their models, so MOSAIC has never observed what it would be publishing. Refusing is
    better than guessing at a contract and creating an API that silently 404s.
    """

    shape = default_api_shape(provider)
    if shape is None:
        raise _no_shape(provider)
    return shape_operations(shape, deployment_name)


def publication_shape(publication: Publication) -> ApiShape:
    if publication.api_shape is None:
        raise _no_shape(publication.provider)
    return ApiShape(publication.api_shape)


def required_data_actions(shape: str) -> tuple[str, ...]:
    """Every data action the gateway needs to call the operations MOSAIC publishes for a shape.

    Derived from the curated operations, so the runtime check cannot drift from what is actually
    published: a route added to a shape adds its permission here with it. The deployment name is
    irrelevant because no provider scopes a data action to one deployment.
    """

    operations = shape_operations(shape, "deployment")
    return tuple(dict.fromkeys(operation.data_action for operation in operations))


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


# Every public host an AI Services account answers on is its custom subdomain under one of these.
_FOUNDRY_HOST_SUFFIXES: tuple[str, ...] = (
    ".cognitiveservices.azure.com",
    ".openai.azure.com",
    ".services.ai.azure.com",
)


def anthropic_origin(endpoint: str) -> str:
    """The origin that serves the Anthropic Messages API for a Foundry resource or AWS Bedrock.

    Foundry serves Anthropic models only on the resource's ``services.ai.azure.com`` host, while
    ARM reports the ``cognitiveservices.azure.com`` host as the account's endpoint. Both carry the
    account's custom subdomain, so the one is derived from the other. A host that carries no
    subdomain this way, such as a regional endpoint, is refused rather than guessed at. An AWS
    Bedrock host serves the Anthropic Messages API itself.
    """

    host = (urlsplit(endpoint).hostname or "").casefold()
    if bedrock_region(host) is not None:
        return f"https://{host}"
    for suffix in _FOUNDRY_HOST_SUFFIXES:
        subdomain = host.removesuffix(suffix)
        if subdomain != host and subdomain and "." not in subdomain:
            return f"https://{subdomain}.services.ai.azure.com"
    raise ValidationError(
        "MOSAIC can't work out this resource's Foundry endpoint "
        "(<subdomain>.services.ai.azure.com), which is the only host that serves Anthropic "
        "models, from its registered endpoint.",
        details={"host": host},
    )


def backend_origin(shape: str | None, endpoint: str) -> str:
    """The origin a published API of this shape forwards to."""

    if shape == ApiShape.ANTHROPIC_MESSAGES:
        return anthropic_origin(endpoint)
    return backend_url(endpoint)


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
    return shape_operations(publication_shape(publication), publication.deployment_name)


def endpoint_slug(endpoint_name: str) -> str:
    return apim_slug(endpoint_name)


def token_limits_supported(shape: str | None, tier: GatewayTier) -> bool:
    """Whether API Management can meter tokens for this shape on this tier family.

    ``llm-token-limit`` and ``llm-emit-token-metric`` understand the Anthropic Messages API only
    on the v2 tiers. The OpenAI-style shapes keep their existing behaviour on every tier.
    """

    if shape == ApiShape.ANTHROPIC_MESSAGES:
        return tier == GatewayTier.V2
    return True


def token_limits_note(shape: str | None, sku_name: str | None) -> str | None:
    """Why this shape can't be token-metered through this gateway, or None when it can."""

    tier = gateway_tier(sku_name)
    if token_limits_supported(shape, tier):
        return None
    lead = (
        "API Management applies token limits and token metrics to the Anthropic Messages API "
        "only on v2 tiers (Basic v2, Standard v2 and Premium v2)."
    )
    if tier == GatewayTier.UNKNOWN:
        return (
            f"{lead} MOSAIC hasn't read this gateway's tier, so it can't apply them. Re-run the "
            "gateway's access check."
        )
    if tier == GatewayTier.CONSUMPTION:
        return (
            f"{lead} This gateway uses the Consumption tier, so this publication applies no token "
            "limits or token metrics."
        )
    return (
        f"{lead} This gateway uses the {sku_name} tier, a classic tier, so this publication "
        "applies no token limits or token metrics. Per-grant call limits are still available "
        "through governed access."
    )


@dataclass(frozen=True)
class DeploymentFit:
    """How, and whether, MOSAIC can publish one observed deployment through one gateway."""

    capability: DeploymentCapability
    api_shape: ApiShape | None
    unpublishable_reason: str | None = None
    token_limits_supported: bool = True
    token_limits_note: str | None = None

    @property
    def publishable(self) -> bool:
        return self.unpublishable_reason is None


# What each shape's curated operations can serve. UNKNOWN stays publishable so that a deployment
# whose ARM flags say nothing is published exactly as it was before capabilities were checked.
_SUPPORTED_CAPABILITIES: dict[str, frozenset[DeploymentCapability]] = {
    ApiShape.AZURE_OPENAI: frozenset(
        {
            DeploymentCapability.CHAT,
            DeploymentCapability.RESPONSES,
            DeploymentCapability.COMPLETION,
            DeploymentCapability.EMBEDDINGS,
            DeploymentCapability.IMAGE,
            DeploymentCapability.TRANSCRIPTION,
            DeploymentCapability.UNKNOWN,
        }
    ),
    ApiShape.FOUNDRY_MODELS: frozenset(
        {
            DeploymentCapability.CHAT,
            DeploymentCapability.EMBEDDINGS,
            DeploymentCapability.UNKNOWN,
        }
    ),
}

_NO_SHAPE_REASONS: dict[DeploymentCapability, str] = {
    DeploymentCapability.REALTIME: (
        "Realtime models use WebSocket sessions, which MOSAIC can't publish yet."
    ),
    DeploymentCapability.VIDEO: (
        "Video generation models use an asynchronous jobs API, which MOSAIC can't publish yet."
    ),
    DeploymentCapability.SPEECH: (
        "Text-to-speech models use the audio speech API, which MOSAIC can't publish yet."
    ),
    DeploymentCapability.RERANK: (
        "Rerank models use a rerank API, which MOSAIC can't publish yet."
    ),
}

_FOUNDRY_REASONS: dict[DeploymentCapability, str] = {
    DeploymentCapability.IMAGE: "Image generation needs the images API",
    DeploymentCapability.TRANSCRIPTION: "Speech-to-text needs the audio transcription API",
    DeploymentCapability.COMPLETION: "Legacy text completion needs the completions API",
    DeploymentCapability.RESPONSES: "This model supports only the Responses API",
}


def _flag(capabilities: Mapping[str, str], name: str) -> bool:
    """Read an ARM capability flag the way the endpoint inventory does."""

    wanted = name.casefold()
    return any(
        key.casefold() == wanted and value.strip().casefold() not in {"false", "0", ""}
        for key, value in capabilities.items()
    )


def is_anthropic_model(model_format: str | None, model_name: str | None) -> bool:
    return (model_format or "").strip().casefold() == "anthropic" or (
        (model_name or "").strip().casefold().startswith("claude")
    )


def classify_deployment(
    model_name: str | None, capabilities: Mapping[str, str]
) -> DeploymentCapability:
    """Name a deployment's capability from its model name and ARM capability flags.

    ARM's flags are incomplete, especially for partner models, so a few model families are
    recognised by name. The order matters: a realtime or transcription model can also carry the
    chat or audio flags, but it can't be served by a chat operation.
    """

    name = (model_name or "").strip().casefold()
    if _flag(capabilities, "realtime") or "realtime" in name:
        return DeploymentCapability.REALTIME
    if name.startswith("sora"):
        return DeploymentCapability.VIDEO
    if "whisper" in name or "transcribe" in name:
        return DeploymentCapability.TRANSCRIPTION
    if name.startswith("tts") or "-tts" in name:
        return DeploymentCapability.SPEECH
    if "rerank" in name:
        return DeploymentCapability.RERANK
    if _flag(capabilities, "imageGenerations") or name.startswith(("dall-e", "gpt-image", "flux")):
        return DeploymentCapability.IMAGE
    if _flag(capabilities, "embeddings") or "embed" in name:
        return DeploymentCapability.EMBEDDINGS
    if _flag(capabilities, "chatCompletion"):
        return DeploymentCapability.CHAT
    if _flag(capabilities, "completion"):
        return DeploymentCapability.COMPLETION
    if _flag(capabilities, "responses"):
        return DeploymentCapability.RESPONSES
    return DeploymentCapability.UNKNOWN


def _anthropic_fit(endpoint: str, gateway_sku: str | None) -> DeploymentFit:
    """Whether and how a Claude deployment on Foundry or AWS Bedrock is served through a gateway."""

    chat = DeploymentCapability.CHAT
    try:
        anthropic_origin(endpoint)
    except ValidationError as error:
        return DeploymentFit(chat, None, error.message)
    if gateway_tier(gateway_sku) == GatewayTier.UNKNOWN:
        return DeploymentFit(
            chat,
            None,
            "MOSAIC hasn't read this gateway's pricing tier, which decides whether API "
            "Management can apply token limits to Anthropic models. Re-run the gateway's "
            "access check, then try again.",
        )
    note = token_limits_note(ApiShape.ANTHROPIC_MESSAGES, gateway_sku)
    return DeploymentFit(
        chat,
        ApiShape.ANTHROPIC_MESSAGES,
        token_limits_supported=note is None,
        token_limits_note=note,
    )


def assess_deployment(
    provider: str,
    *,
    model_name: str | None,
    model_format: str | None,
    capabilities: Mapping[str, str],
    endpoint: str,
    gateway_sku: str | None,
) -> DeploymentFit:
    """Choose the API shape for a deployment, or say why MOSAIC can't publish it yet.

    Pure, so the publishable-models listing, publication create, and plan all reach the same
    answer from the same inputs.
    """

    if is_anthropic_model(model_format, model_name):
        if provider != ModelProvider.AZURE_AI_FOUNDRY:
            return DeploymentFit(
                DeploymentCapability.CHAT,
                None,
                "Anthropic models are served by Foundry (AI Services) resources, and MOSAIC "
                "publishes them only from those.",
            )
        return _anthropic_fit(endpoint, gateway_sku)

    capability = classify_deployment(model_name, capabilities)
    shape = default_api_shape(provider)
    if shape is None:
        return DeploymentFit(capability, None, _no_shape(provider).message)
    if capability in _SUPPORTED_CAPABILITIES[shape]:
        return DeploymentFit(capability, shape)
    reason = _NO_SHAPE_REASONS.get(capability)
    if reason is None:
        needs = _FOUNDRY_REASONS.get(capability, "This model needs an API")
        reason = f"{needs}, which MOSAIC publishes only from Azure OpenAI resources today."
    return DeploymentFit(capability, None, reason)


def assess_declared_deployment(
    provider: str, shape: str, *, endpoint: str, gateway_sku: str | None
) -> DeploymentFit:
    """The fit of a deployment an administrator declared, with the API shape they chose for it.

    A declared deployment is published as chat, the capability every curated shape's governed
    operations serve. The shape still has to be one the resource serves, and an Anthropic
    deployment still needs a gateway whose tier MOSAIC knows, exactly as a discovered one does.
    """

    if not shape_fits_provider(shape, provider):
        if provider == ModelProvider.AWS_BEDROCK:
            reason = (
                "MOSAIC reaches AWS Bedrock only through its Anthropic Messages API, so it can't "
                "serve this model with the API it was declared with."
            )
        else:
            reason = (
                "An Azure OpenAI resource serves only the Azure OpenAI API, so MOSAIC can't "
                "publish this deployment with the API it was declared with."
            )
        return DeploymentFit(DeploymentCapability.CHAT, None, reason)
    if shape == ApiShape.ANTHROPIC_MESSAGES:
        return _anthropic_fit(endpoint, gateway_sku)
    return DeploymentFit(DeploymentCapability.CHAT, ApiShape(shape))
