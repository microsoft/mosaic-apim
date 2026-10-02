"""The API Management policy a model pool writes (ADR 0024).

A pool writes two documents, like a publication. The fragment, in the inbound section, works out
which pool model a request names, refuses a model the pool doesn't offer, applies the pool's
safeguard, and turns the request's path into one every member's backend accepts. The API policy
includes the fragment, chooses each attempt's backend in its backend section, retries a cascading
failure on another member, and in its outbound section removes what would tell a caller which
member answered.

Routing and retry live in the API policy rather than the fragment because API Management runs a
fragment only where it's included, and the inbound section can't forward a request.
"""

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace

from mosaic_api.domain import ApiShape
from mosaic_api.integrations.apim.model_apis import OperationSpec, shape_operations
from mosaic_api.integrations.apim.policy_semantics import analyze_policy
from mosaic_api.integrations.policy import (
    METRIC_NAMESPACE,
    PublicationPolicy,
    add_shape_headers,
    managed_identity_resource,
)
from mosaic_api.model_pools import BreakerPreset, PoolSafeguard, cascade_statuses

DEPLOYMENT_PARAMETER = "deployment-id"
MODEL_VARIABLE = "mosaic-pool-model"
MODEL_ID_VARIABLE = "mosaic-pool-model-id"
ATTEMPTS_VARIABLE = "mosaic-pool-attempts"
ATTEMPT_VARIABLE = "mosaic-pool-attempt"
_AZURE_OPENAI_PREFIX = f"/openai/deployments/{{{DEPLOYMENT_PARAMETER}}}"
# Response headers that would tell a caller which member answered, or describe one member's
# capacity as if it were the pool's.
MEMBER_RESPONSE_HEADERS: tuple[str, ...] = (
    "x-ms-region",
    "x-ms-deployment-name",
    "x-ms-spillover-from-deployment",
    "x-ms-spillover-error",
    "azureml-model-deployment",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-limit",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-input-tokens-reset",
    "anthropic-ratelimit-output-tokens-limit",
    "anthropic-ratelimit-output-tokens-remaining",
    "anthropic-ratelimit-output-tokens-reset",
)
NOT_FOUND_BODY = '{"error":{"code":"ModelNotFound","message":"This API has no such model."}}'
UNAVAILABLE_BODY = (
    '{"error":{"code":"ModelUnavailable","message":"The model is busy or unavailable. '
    'Retry after the time in the Retry-After header, if there is one."}}'
)

_POOL_DESCRIPTIONS: dict[str, str] = {
    "chat-completions": "Chat completions with the model the route names.",
    "completions": "Legacy text completions with the model the route names.",
    "embeddings": "Embeddings with the model the route or the request names.",
    "images-generations": "Image generation with the model the route names.",
    "audio-transcriptions": "Audio transcription with the model the route names.",
    "audio-translations": "Audio translation with the model the route names.",
    "messages": "Anthropic Messages with the model the request names.",
    "count-tokens": "Count a message's input tokens for the model the request names.",
}
# Routes a pool doesn't offer: Responses isn't deployment-scoped in Azure OpenAI's contract, and
# model info describes one deployment rather than a pool model.
_EXCLUDED_OPERATIONS = frozenset({"responses", "model-info"})
# API Management answers 503 itself, with a reason naming the backend pool, when every member of
# a backend pool is tripped. Sending the request to the same exhausted pool again can't succeed.
_POOL_EXHAUSTED = (
    "context.Response.StatusCode == 503 && context.Response.StatusReason != null && "
    '(context.Response.StatusReason.Contains("Backend pool") || '
    'context.Response.StatusReason.Contains("is temporarily unavailable"))'
)


@dataclass(frozen=True)
class PoolTarget:
    """Where one attempt goes: a backend, and the deployment name that backend expects."""

    backend_name: str
    deployment_name: str


@dataclass(frozen=True)
class PoolRoute:
    """How the gateway serves one pool model.

    A backend pool route has one target, the backend pool, and makes up to ``attempts`` calls to
    it. A linear route has one target per member in order, and tries each once.
    """

    model_id: str
    public_name: str
    targets: tuple[PoolTarget, ...]
    attempts: int


def body_routed(shape: str) -> bool:
    """Whether a shape names its model in the request body rather than in the route."""

    return shape != ApiShape.AZURE_OPENAI


def pool_operations(shape: str) -> tuple[OperationSpec, ...]:
    """The operations a pool's API offers for a shape.

    The Azure OpenAI shape takes the model in the route, as ``{deployment-id}``, exactly as a
    caller's SDK sends it. The other shapes take it from the request body's ``model``.
    """

    operations = shape_operations(shape, f"{{{DEPLOYMENT_PARAMETER}}}")
    return tuple(
        replace(
            operation,
            description=_POOL_DESCRIPTIONS.get(operation.name, operation.description),
        )
        for operation in operations
        if operation.name not in _EXCLUDED_OPERATIONS
    )


def template_parameters(operation: OperationSpec) -> list[str]:
    return [DEPLOYMENT_PARAMETER] if f"{{{DEPLOYMENT_PARAMETER}}}" in operation.url_template else []


def member_backend_url(shape: str, origin: str, deployment_name: str) -> str:
    """The URL of one member's backend.

    An Azure OpenAI member's backend carries its deployment in its path, so each member of one
    backend pool can have its own deployment name: the pool's fragment strips the route's
    deployment segment and API Management appends the rest to whichever member it chooses.
    """

    if shape == ApiShape.AZURE_OPENAI:
        return f"{origin}/openai/deployments/{deployment_name}"
    return origin


def _serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


def _literal(value: str) -> str:
    """A C# string literal for a policy expression."""

    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _string_variable(name: str) -> str:
    return f"context.Variables.GetValueOrDefault<string>({_literal(name)}, {_literal('')})"


def _int_variable(name: str, default: int) -> str:
    return f"context.Variables.GetValueOrDefault<int>({_literal(name)}, {default})"


def _status_condition(statuses: tuple[int, ...]) -> str:
    return " || ".join(f"context.Response.StatusCode == {status}" for status in statuses)


def _requested_model(shape: str) -> str:
    if not body_routed(shape):
        return f"@(context.Request.MatchedParameters[{_literal(DEPLOYMENT_PARAMETER)}])"
    return (
        "@{ try { var body = context.Request.Body.As<JObject>(preserveContent: true); "
        'return (string)body["model"] ?? ""; } catch { return ""; } }'
    )


def _rewrite_model(deployment_name: str) -> str:
    return (
        "@{ var body = context.Request.Body.As<JObject>(preserveContent: true); "
        f'body["model"] = {_literal(deployment_name)}; return body.ToString(); }}'
    )


def _json_response(parent: ET.Element, body: str) -> None:
    header = ET.SubElement(
        parent, "set-header", {"name": "Content-Type", "exists-action": "override"}
    )
    ET.SubElement(header, "value").text = "application/json"
    ET.SubElement(parent, "set-body").text = body


def _safeguard_attributes(model_id: str, safeguard: PoolSafeguard) -> dict[str, str]:
    attributes = {"counter-key": model_id, "estimate-prompt-tokens": "false"}
    if safeguard.tokens_per_minute:
        attributes["tokens-per-minute"] = str(safeguard.tokens_per_minute)
    if safeguard.token_quota:
        attributes["token-quota"] = str(safeguard.token_quota)
        attributes["token-quota-period"] = str(safeguard.token_quota_period)
    return attributes


def _fragment(
    *,
    pool_id: str,
    shape: str,
    routes: list[PoolRoute],
    safeguard: PoolSafeguard | None,
) -> ET.Element:
    fragment = ET.Element("fragment")
    add_shape_headers(fragment, shape)
    # A caller must not steer Azure's own overflow to a deployment it was never told about.
    ET.SubElement(
        fragment, "set-header", {"name": "x-ms-spillover-deployment", "exists-action": "delete"}
    )
    ET.SubElement(
        fragment,
        "authentication-managed-identity",
        {"resource": managed_identity_resource(shape)},
    )
    ET.SubElement(
        fragment, "set-variable", {"name": MODEL_VARIABLE, "value": _requested_model(shape)}
    )
    choose = ET.SubElement(fragment, "choose")
    for route in routes:
        requested = _string_variable(MODEL_VARIABLE)
        when = ET.SubElement(
            choose,
            "when",
            {"condition": f"@({requested} == {_literal(route.public_name)})"},
        )
        ET.SubElement(when, "set-variable", {"name": MODEL_ID_VARIABLE, "value": route.model_id})
        ET.SubElement(
            when, "set-variable", {"name": ATTEMPTS_VARIABLE, "value": f"@({route.attempts})"}
        )
        if safeguard is not None:
            ET.SubElement(when, "llm-token-limit", _safeguard_attributes(route.model_id, safeguard))
            metric = ET.SubElement(when, "llm-emit-token-metric", {"namespace": METRIC_NAMESPACE})
            ET.SubElement(metric, "dimension", {"name": "Pool", "value": pool_id})
            ET.SubElement(metric, "dimension", {"name": "Model", "value": route.public_name})
    otherwise = ET.SubElement(choose, "otherwise")
    refusal = ET.SubElement(otherwise, "return-response")
    ET.SubElement(refusal, "set-status", {"code": "404", "reason": "Not Found"})
    _json_response(refusal, NOT_FOUND_BODY)
    if not body_routed(shape):
        # Each member's backend URL ends with its own deployment, so the request keeps only what
        # follows the route's deployment segment, and its query string.
        rewrites = ET.SubElement(fragment, "choose")
        for operation in pool_operations(shape):
            if not operation.url_template.startswith(_AZURE_OPENAI_PREFIX):
                continue
            when = ET.SubElement(
                rewrites,
                "when",
                {"condition": f"@(context.Operation.Id == {_literal(operation.name)})"},
            )
            ET.SubElement(
                when,
                "rewrite-uri",
                {
                    "template": operation.url_template.removeprefix(_AZURE_OPENAI_PREFIX),
                    "copy-unmatched-params": "true",
                },
            )
    return fragment


def _target(parent: ET.Element, target: PoolTarget, *, rewrite: bool) -> None:
    ET.SubElement(parent, "set-backend-service", {"backend-id": target.backend_name})
    if rewrite:
        ET.SubElement(parent, "set-body").text = _rewrite_model(target.deployment_name)


def _rewrites_model(shape: str, route: PoolRoute) -> bool:
    """Whether the request body's model must name each target's deployment.

    Once one attempt rewrites it, every later attempt must too, so a route rewrites for every
    target or for none.
    """

    return body_routed(shape) and any(
        target.deployment_name != route.public_name for target in route.targets
    )


def _api_policy(
    *,
    fragment_name: str,
    shape: str,
    routes: list[PoolRoute],
    preset: BreakerPreset,
) -> ET.Element:
    statuses = cascade_statuses(preset)
    policies = ET.Element("policies")
    inbound = ET.SubElement(policies, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "include-fragment", {"fragment-id": fragment_name})

    # No <base/> here: an inherited forward-request would send each request a second time.
    backend = ET.SubElement(policies, "backend")
    container = backend
    most = max(route.attempts for route in routes)
    if most > 1:
        container = ET.SubElement(
            backend,
            "retry",
            {
                "condition": (
                    f"@(context.Response != null && ({_status_condition(statuses)}) && "
                    f"!({_POOL_EXHAUSTED}) && "
                    f"{_int_variable(ATTEMPT_VARIABLE, 0)} < {_int_variable(ATTEMPTS_VARIABLE, 1)})"
                ),
                "count": str(most - 1),
                "interval": "0",
                "first-fast-retry": "true",
            },
        )
    ET.SubElement(
        container,
        "set-variable",
        {"name": ATTEMPT_VARIABLE, "value": f"@({_int_variable(ATTEMPT_VARIABLE, 0)} + 1)"},
    )
    choose = ET.SubElement(container, "choose")
    for route in routes:
        chosen = _string_variable(MODEL_ID_VARIABLE)
        when = ET.SubElement(
            choose,
            "when",
            {"condition": f"@({chosen} == {_literal(route.model_id)})"},
        )
        rewrite = _rewrites_model(shape, route)
        if len(route.targets) == 1:
            _target(when, route.targets[0], rewrite=rewrite)
            continue
        attempts = ET.SubElement(when, "choose")
        for index, target in enumerate(route.targets, start=1):
            attempt = ET.SubElement(
                attempts,
                "when",
                {"condition": f"@({_int_variable(ATTEMPT_VARIABLE, 1)} == {index})"},
            )
            _target(attempt, target, rewrite=rewrite)
    ET.SubElement(
        container,
        "forward-request",
        {"buffer-request-body": "true", "buffer-response": "false"},
    )

    outbound = ET.SubElement(policies, "outbound")
    ET.SubElement(outbound, "base")
    for header in MEMBER_RESPONSE_HEADERS:
        ET.SubElement(outbound, "set-header", {"name": header, "exists-action": "delete"})
    # A member's error names its deployment, region, or tier. Retry-After survives.
    failed = ET.SubElement(
        ET.SubElement(outbound, "choose"),
        "when",
        {"condition": f"@({_status_condition(statuses)})"},
    )
    _json_response(failed, UNAVAILABLE_BODY)
    ET.SubElement(ET.SubElement(policies, "on-error"), "base")
    return policies


def render_pool_policy(
    *,
    pool_id: str,
    fragment_name: str,
    shape: str,
    routes: list[PoolRoute],
    preset: BreakerPreset,
    safeguard: PoolSafeguard | None,
) -> PublicationPolicy:
    """Author a pool's fragment and API policy, reduced to facets like a publication's."""

    if not routes:
        raise ValueError("A pool policy needs at least one model")
    fragment_xml = _serialize(
        _fragment(pool_id=pool_id, shape=shape, routes=routes, safeguard=safeguard)
    )
    api_policy_xml = _serialize(
        _api_policy(fragment_name=fragment_name, shape=shape, routes=routes, preset=preset)
    )
    combined = hashlib.sha256(f"{fragment_xml}\n{api_policy_xml}".encode()).hexdigest()
    fragment_analysis = analyze_policy(fragment_xml)
    api_analysis = analyze_policy(api_policy_xml)
    return PublicationPolicy(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        content_sha256=combined,
        facets=[*fragment_analysis.facets, *api_analysis.facets],
        unrecognized_elements=sorted(
            set(fragment_analysis.unrecognized_elements) | set(api_analysis.unrecognized_elements)
        ),
    )


__all__ = [
    "DEPLOYMENT_PARAMETER",
    "PoolRoute",
    "PoolTarget",
    "body_routed",
    "member_backend_url",
    "pool_operations",
    "render_pool_policy",
    "template_parameters",
]
