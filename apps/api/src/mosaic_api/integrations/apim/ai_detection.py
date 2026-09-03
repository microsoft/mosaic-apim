"""Identify which parts of a gateway actually front large language models.

A generic list of APIs is not useful to a MOSAIC operator. Knowing that four of twelve APIs route to
Azure OpenAI, and which policies govern them, is the whole point. Detection combines three
independent signals so a gateway that only matches one of them is still classified.

Detection is a recommendation, not a gate. It decides which rows start checked when an
administrator imports APIs; they remain free to adopt an API MOSAIC did not recognise, and to skip
one it did. That is why the marker list below stays conservative.
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
    ("api.openai.com", AiBackendKind.OPEN_AI),
    ("api.anthropic.com", AiBackendKind.ANTHROPIC),
    ("aiplatform.googleapis.com", AiBackendKind.GOOGLE_VERTEX),
    ("generativelanguage.googleapis.com", AiBackendKind.GOOGLE_VERTEX),
)

# Bedrock is regional — ``bedrock-runtime.us-east-1.amazonaws.com`` — so the distinguishing part is
# the first host label rather than the suffix the other providers are matched on.
_HOST_PREFIXES: tuple[tuple[str, str, AiBackendKind], ...] = (
    ("bedrock", ".amazonaws.com", AiBackendKind.AWS_BEDROCK),
    ("bedrock-runtime", ".amazonaws.com", AiBackendKind.AWS_BEDROCK),
)

# Only markers distinctive enough to be worth acting on. A false positive here silently pre-checks
# the wrong API in the import dialog, so generic paths such as ``/messages`` are left out even
# though some providers use them.
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
    "/converse",
    ":generatecontent",
    ":streamgeneratecontent",
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

_PROVIDER_LABELS: dict[AiBackendKind, str] = {
    AiBackendKind.AZURE_OPENAI: "Azure OpenAI",
    AiBackendKind.AZURE_AI_FOUNDRY: "Azure AI Foundry",
    AiBackendKind.AZURE_AI_INFERENCE: "Azure AI inference",
    AiBackendKind.OPEN_AI: "OpenAI",
    AiBackendKind.ANTHROPIC: "Anthropic",
    AiBackendKind.GOOGLE_VERTEX: "Google Vertex AI",
    AiBackendKind.AWS_BEDROCK: "AWS Bedrock",
    AiBackendKind.OTHER_LLM: "a model",
    AiBackendKind.NONE: "no model backend",
}

_KIND_PRIORITY: tuple[AiBackendKind, ...] = (
    AiBackendKind.AZURE_OPENAI,
    AiBackendKind.AZURE_AI_FOUNDRY,
    AiBackendKind.AZURE_AI_INFERENCE,
    AiBackendKind.OPEN_AI,
    AiBackendKind.ANTHROPIC,
    AiBackendKind.GOOGLE_VERTEX,
    AiBackendKind.AWS_BEDROCK,
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
    first_label = host.split(".", 1)[0]
    for prefix, suffix, kind in _HOST_PREFIXES:
        if first_label == prefix and host.endswith(suffix):
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
        signals.append(f"Backend URL points at {_PROVIDER_LABELS[url_kind]}.")

    for kind in backend_kinds or []:
        if kind != AiBackendKind.NONE:
            candidates.append(kind)
            signals.append(f"Routed to a {_PROVIDER_LABELS[kind]} backend resource.")
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
