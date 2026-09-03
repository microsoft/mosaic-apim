"""Identify which parts of a gateway actually front Azure AI models.

A generic list of APIs is not useful to a MOSAIC operator. Knowing that four of twelve APIs route to
Azure OpenAI, and which policies govern them, is the whole point. Detection combines three
independent signals so a gateway that only matches one of them is still classified.
"""

from urllib.parse import urlparse

from mosaic_api.observed import AiBackendKind

_HOST_SUFFIXES: tuple[tuple[str, AiBackendKind], ...] = (
    (".openai.azure.com", AiBackendKind.AZURE_OPENAI),
    (".api.cognitive.microsoft.com", AiBackendKind.AZURE_OPENAI),
    (".services.ai.azure.com", AiBackendKind.AZURE_AI_FOUNDRY),
    (".cognitiveservices.azure.com", AiBackendKind.AZURE_AI_FOUNDRY),
    (".inference.ai.azure.com", AiBackendKind.AZURE_AI_INFERENCE),
    (".models.ai.azure.com", AiBackendKind.AZURE_AI_INFERENCE),
)

_OPERATION_MARKERS: tuple[str, ...] = (
    "/chat/completions",
    "/completions",
    "/embeddings",
    "/deployments/",
    "/responses",
    "/images/generations",
    "/audio/transcriptions",
    "/audio/speech",
    "/models/chat",
)

AI_POLICY_ELEMENTS: frozenset[str] = frozenset(
    {
        "llm-token-limit",
        "azure-openai-token-limit",
        "llm-emit-token-metric",
        "azure-openai-emit-token-metric",
        "llm-semantic-cache-lookup",
        "llm-semantic-cache-store",
        "azure-openai-semantic-cache-lookup",
        "azure-openai-semantic-cache-store",
        "llm-content-safety",
    }
)

_KIND_PRIORITY: tuple[AiBackendKind, ...] = (
    AiBackendKind.AZURE_OPENAI,
    AiBackendKind.AZURE_AI_FOUNDRY,
    AiBackendKind.AZURE_AI_INFERENCE,
    AiBackendKind.OTHER_LLM,
    AiBackendKind.NONE,
)


def classify_url(url: str | None) -> AiBackendKind:
    """Classify a backend or service URL by host."""

    if not url:
        return AiBackendKind.NONE
    candidate = url if "//" in url else f"https://{url}"
    host = (urlparse(candidate).hostname or "").casefold()
    if not host:
        return AiBackendKind.NONE
    for suffix, kind in _HOST_SUFFIXES:
        if host.endswith(suffix):
            return kind
    return AiBackendKind.NONE


def _strongest(kinds: list[AiBackendKind]) -> AiBackendKind:
    for kind in _KIND_PRIORITY:
        if kind in kinds:
            return kind
    return AiBackendKind.NONE


def classify_api(
    *,
    service_url: str | None,
    path: str | None = None,
    operation_templates: list[str] | None = None,
    policy_elements: list[str] | None = None,
    backend_kinds: list[AiBackendKind] | None = None,
) -> tuple[AiBackendKind, list[str]]:
    """Return the AI backend kind for an API plus the human-readable signals that produced it."""

    signals: list[str] = []
    candidates: list[AiBackendKind] = []

    url_kind = classify_url(service_url)
    if url_kind != AiBackendKind.NONE:
        candidates.append(url_kind)
        signals.append("Backend URL points at an Azure AI endpoint.")

    for kind in backend_kinds or []:
        if kind != AiBackendKind.NONE:
            candidates.append(kind)
            signals.append("Routed to an Azure AI backend resource.")
            break

    templates = [template.casefold() for template in operation_templates or []]
    if path:
        templates.append(path.casefold())
    if any(marker in template for template in templates for marker in _OPERATION_MARKERS):
        candidates.append(AiBackendKind.OTHER_LLM)
        signals.append("Exposes model inference operations.")

    if any(element in AI_POLICY_ELEMENTS for element in policy_elements or []):
        candidates.append(AiBackendKind.OTHER_LLM)
        signals.append("Governed by AI gateway policies.")

    return _strongest(candidates), signals
