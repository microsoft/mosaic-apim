"""The API Management policy a model pool writes (ADR 0024).

A pool writes two documents, like a publication. The fragment, in the inbound section, works out
which pool model a request names, refuses a model the pool doesn't offer, applies the pool's
safeguard, and turns the request's path into one every member's backend accepts. The API policy
includes the fragment, chooses each attempt's backend in its backend section, retries a cascading
failure on another member, and in its outbound section removes what would tell a caller which
member answered.

Routing and retry live in the API policy rather than the fragment because API Management runs a
fragment only where it's included, and the inbound section can't forward a request.

A pool with governed access (phase 2) writes the same API policy, but its fragment first
authorizes every call against grants on the pool's models, exactly as a governed publication's
does, and then applies each grant's limits, its cost center's pooled quota on the model, and the
pool's safeguard, in that order.

A pool with a member reached with an API key (phase 3) removes every credential a caller sent,
acquires the gateway's managed identity token into a variable, and has each attempt send its own
member's credential: that token, or the member's key from a Key Vault-backed named value. A key
member is a target on its own. In a backend pool route it's tried after the backend pool, which
hands its remaining attempts on as soon as it has no member left.
"""

import hashlib
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace

from mosaic_api.domain import (
    COST_CENTER_HEADER,
    ON_BEHALF_HEADER,
    ApiShape,
    EntitlementSubjectKind,
    PolicyFacet,
    PolicyFacetKind,
    PolicySection,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.access_policy import (
    _COST_CENTER_HEADER,
    _GROUPS_OVERAGE_DENIED,
    _GUID,
    _HAS_KEY,
    _HAS_TOKEN,
    _KEY_COST_CENTER,
    _KEY_GRANT,
    _KEYS_OFF,
    _RESOURCE_NAME,
    _SUBSCRIPTION_COUNTERS,
    _TOKEN_GRANT,
    COST_CENTER_DENIED,
    COST_CENTER_KEYS_OFF_DENIED,
    COST_CENTER_MISMATCH_DENIED,
    MAX_FRAGMENT_BYTES,
    MCP_CALL,
    _code,
    _cost_center_ids,
    _credential_prelude,
    _deny,
    _expression,
    _grant_counter_key,
    _grant_limits,
    _grant_token_limits,
    _key_shape_check,
    _match,
    _pool_limits,
    _reject,
    _select_grant,
    _split_match,
    _token_groups_overage,
    _token_lookup,
    _token_member_lookup,
    _validate_cost_center,
    _validate_token,
    _variable,
    append_budget_check,
    append_grant_attribution_trace,
    classify_traces,
    cost_center_details,
    describe_limit_facet,
    describe_on_behalf_removal,
    grant_counter_identity,
    mcp_call_reference,
)
from mosaic_api.integrations.access_policy import _literal as _safe_literal
from mosaic_api.integrations.apim.model_apis import OperationSpec, shape_operations
from mosaic_api.integrations.apim.policy_semantics import analyze_policy
from mosaic_api.integrations.backend_keys import (
    backend_key_header,
    describe_backend_key,
    is_backend_key_facet,
    set_backend_key,
    strip_caller_credentials,
)
from mosaic_api.integrations.policy import (
    METRIC_NAMESPACE,
    PublicationPolicy,
    add_shape_headers,
    managed_identity_resource,
    shape_removed_headers,
)
from mosaic_api.model_pools import (
    BreakerPreset,
    ModelPool,
    PoolAccessGrant,
    PoolAccessSnapshot,
    PoolSafeguard,
    cascade_statuses,
    pool_key_name,
)

DEPLOYMENT_PARAMETER = "deployment-id"
MODEL_VARIABLE = "mosaic-pool-model"
MODEL_ID_VARIABLE = "mosaic-pool-model-id"
ATTEMPTS_VARIABLE = "mosaic-pool-attempts"
ATTEMPT_VARIABLE = "mosaic-pool-attempt"
# How many of a route's attempts its backend pool takes before the targets after it.
BALANCED_VARIABLE = "mosaic-pool-balanced-attempts"
# The gateway's managed identity token, in a pool whose attempts set their own credentials.
TOKEN_VARIABLE = "mosaic-pool-identity-token"
# The backend an attempt is sent to, which its attempt trace records.
BACKEND_VARIABLE = "mosaic-pool-backend"
# Readers split the message on spaces into key=value pairs and ignore keys they don't know.
# Only a change that breaks those readers bumps the version.
ATTEMPT_TRACE_PREFIX = "mosaic-attempt v=1"
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
    # What AWS Bedrock adds, which would tell a caller AWS answered.
    "x-amzn-requestid",
    "x-amzn-errortype",
    "x-amzn-bedrock-invocation-latency",
    "x-amzn-bedrock-performanceconfig-latency",
    "x-amzn-bedrock-input-token-count",
    "x-amzn-bedrock-output-token-count",
    "x-amzn-bedrock-cache-read-input-token-count",
    "x-amzn-bedrock-cache-write-input-token-count",
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
# Governed counters are namespaced apart from publications', so a pool and a publication can
# never share a count.
_COUNTER_PREFIX = "mosaic:pool:"
# What a pool key lookup answers for one of the pool's keys whose subject holds no grant for the
# requested model.
_NO_GRANT = "~"
# The operations governed access meters and therefore permits, by shape.
_GOVERNED_OPERATIONS: dict[str, frozenset[str]] = {
    ApiShape.AZURE_OPENAI: frozenset({"chat-completions"}),
    ApiShape.FOUNDRY_MODELS: frozenset({"chat-completions"}),
    ApiShape.ANTHROPIC_MESSAGES: frozenset({"messages"}),
}
_LIMIT_ELEMENTS = frozenset({"llm-token-limit", "rate-limit-by-key", "quota-by-key"})


@dataclass(frozen=True)
class PoolTarget:
    """Where attempts go: a backend, and the deployment name that backend expects.

    A ``balanced`` target is a backend pool, and can take several attempts, each on whichever
    member API Management chooses. Any other target takes one. A target reached with an API key
    names the Key Vault-backed named value its key is read from; any other is reached with the
    gateway's managed identity.
    """

    backend_name: str
    deployment_name: str
    attempts: int = 1
    balanced: bool = False
    key_named_value: str | None = None

    def __post_init__(self) -> None:
        if self.attempts < 1 or (self.attempts > 1 and not self.balanced):
            raise ValueError("Only a backend pool target takes more than one attempt")
        if self.balanced and self.key_named_value:
            raise ValueError("A backend pool's members are reached with the gateway's identity")


@dataclass(frozen=True)
class PoolRoute:
    """How the gateway serves one pool model.

    A backend pool route has one target, the backend pool, and makes up to ``attempts`` calls to
    it. A linear route has one target per member in order, and tries each once. A backend pool
    route can also have targets after its backend pool, each tried once.
    """

    model_id: str
    public_name: str
    targets: tuple[PoolTarget, ...]
    attempts: int

    def __post_init__(self) -> None:
        if any(target.balanced for target in self.targets[1:]):
            raise ValueError("Only a route's first target can be a backend pool")
        if len(self.targets) > 1 and self.attempts != sum(
            target.attempts for target in self.targets
        ):
            raise ValueError("A route with several targets makes exactly their attempts")


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


def _keyed(routes: Sequence[PoolRoute]) -> bool:
    """Whether any member is reached with an API key, so each attempt sets its own credential."""

    return any(target.key_named_value for route in routes for target in route.targets)


def _uses_identity(routes: Sequence[PoolRoute]) -> bool:
    return any(target.key_named_value is None for route in routes for target in route.targets)


def _balanced_attempts(route: PoolRoute) -> int:
    """How many attempts a route's backend pool takes before the targets after it, or 0."""

    first = route.targets[0]
    return first.attempts if first.balanced and len(route.targets) > 1 else 0


def _authenticate(fragment: ET.Element, shape: str, routes: Sequence[PoolRoute]) -> None:
    """Acquire the gateway's managed identity token for the members that are reached with it.

    In a pool with a member reached with a key, the token goes into a variable instead, and each
    attempt sets its own member's credential. A pool reached only with keys acquires no token.
    """

    attributes = {"resource": managed_identity_resource(shape)}
    if _keyed(routes):
        if not _uses_identity(routes):
            return
        attributes["output-token-variable-name"] = TOKEN_VARIABLE
    ET.SubElement(fragment, "authentication-managed-identity", attributes)


def _fragment(
    *,
    pool_id: str,
    shape: str,
    routes: list[PoolRoute],
    safeguard: PoolSafeguard | None,
) -> ET.Element:
    fragment = ET.Element("fragment")
    if _keyed(routes):
        # Only the gateway's own credentials may reach a member, as ADR 0018 requires.
        strip_caller_credentials(fragment, removed_headers=shape_removed_headers(shape))
    add_shape_headers(fragment, shape)
    # A caller must not steer Azure's own overflow to a deployment it was never told about.
    ET.SubElement(
        fragment, "set-header", {"name": "x-ms-spillover-deployment", "exists-action": "delete"}
    )
    _authenticate(fragment, shape, routes)
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
        if balanced := _balanced_attempts(route):
            ET.SubElement(
                when, "set-variable", {"name": BALANCED_VARIABLE, "value": f"@({balanced})"}
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
    _append_rewrites(fragment, shape, pool_operations(shape))
    return fragment


def _append_rewrites(
    fragment: ET.Element, shape: str, operations: tuple[OperationSpec, ...]
) -> None:
    """Strip the route's deployment segment, which names the pool model, before forwarding.

    Each member's backend URL ends with its own deployment, so the request keeps only what follows
    the route's deployment segment, and its query string. A body-routed shape has no such segment.
    """

    if body_routed(shape):
        return
    rewrites = ET.SubElement(fragment, "choose")
    for operation in operations:
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


def _target(parent: ET.Element, target: PoolTarget, *, rewrite: bool) -> None:
    ET.SubElement(parent, "set-backend-service", {"backend-id": target.backend_name})
    ET.SubElement(parent, "set-variable", {"name": BACKEND_VARIABLE, "value": target.backend_name})
    if rewrite:
        ET.SubElement(parent, "set-body").text = _rewrite_model(target.deployment_name)


def _attempt_trace(parent: ET.Element) -> None:
    """Record where one attempt went and how it was answered, for the pool's health.

    A backend pool's attempt names the backend pool, so the host and path it called are what
    place it on a member. The path comes last: part of it is the caller's, and readers take the
    first value of each key. The query string is never recorded.
    """

    status = "(context.Response != null ? context.Response.StatusCode : 0)"
    exhausted = f"(context.Response != null && ({_POOL_EXHAUSTED}) ? 1 : 0)"
    trace = ET.SubElement(parent, "trace", {"source": "mosaic", "severity": "information"})
    ET.SubElement(trace, "message").text = (
        f'@("{ATTEMPT_TRACE_PREFIX} m=" + {_string_variable(MODEL_ID_VARIABLE)}'
        f' + " n=" + {_int_variable(ATTEMPT_VARIABLE, 0)}'
        f' + " b=" + {_string_variable(BACKEND_VARIABLE)}'
        f' + " s=" + {status} + " e=" + {exhausted}'
        ' + " h=" + context.Request.Url.Host + " p=" + context.Request.Url.Path)'
    )


ATTEMPT_TRACE_SUMMARY = (
    "Records which member each attempt reached and how it answered, so MOSAIC can show the "
    "pool's health."
)


def _describe_attempt_traces(facets: Sequence[PolicyFacet]) -> None:
    """Word the API policy's attempt trace. It's the only trace an API policy has."""

    for facet in facets:
        if facet.element != "trace":
            continue
        facet.summary = ATTEMPT_TRACE_SUMMARY
        facet.details = [
            "Each attempt records the pool model, the attempt's number, the backend it was sent "
            "to, the status it answered, whether the backend pool had no member left to try, and "
            "the host and path it called. It never records a query string, a header, or a "
            "request or response body.",
            "Resource logs keep it in TraceRecords when the gateway's Azure Monitor diagnostic "
            "logs at Information.",
            "Application Insights records one trace per attempt when its diagnostic verbosity is "
            "Information; set it to Error to stop.",
        ]
        facet.attributes = {"trace": "attempt"}


def _credential(
    parent: ET.Element, target: PoolTarget, shape: str, *, drop_token: bool, drop_key: bool
) -> None:
    """Set the credential one attempt sends its member, and remove the other kind.

    An earlier attempt of the same request may have set the other kind. ``drop_token`` is for a
    pool that acquires a token, in case API Management sets it as well as storing it;
    ``drop_key`` is for a route with members reached with a key.
    """

    if target.key_named_value is None:
        header = ET.SubElement(
            parent, "set-header", {"name": "Authorization", "exists-action": "override"}
        )
        ET.SubElement(header, "value").text = f'@("Bearer " + {_string_variable(TOKEN_VARIABLE)})'
        if drop_key:
            ET.SubElement(
                parent,
                "set-header",
                {"name": backend_key_header(shape), "exists-action": "delete"},
            )
        return
    if drop_token:
        ET.SubElement(parent, "set-header", {"name": "Authorization", "exists-action": "delete"})
    set_backend_key(parent, shape=shape, named_value=target.key_named_value)


IDENTITY_TOKEN_SUMMARY = (
    "Sends the gateway's managed identity token to a member reached with the gateway's identity."
)


def _is_identity_token_facet(facet: PolicyFacet) -> bool:
    return (
        facet.element == "set-header"
        and facet.attributes.get("exists-action") == "override"
        and facet.attributes.get("name", "").casefold() == "authorization"
    )


def _describe_credentials(facets: Sequence[PolicyFacet], shape: str) -> None:
    """Word each attempt's credential as backend authentication rather than a header write.

    Only a pool with a member reached with a key sets credentials itself, so other pools' facets
    are untouched.
    """

    for facet in facets:
        if is_backend_key_facet(facet, shape):
            describe_backend_key(facet)
        elif _is_identity_token_facet(facet):
            facet.kind = PolicyFacetKind.AUTHENTICATION
            facet.summary = IDENTITY_TOKEN_SUMMARY
            facet.details = [
                "The token is the one the gateway acquired with its managed identity. Every "
                "credential a caller sent is removed first, so nothing a caller sends can "
                "replace it.",
            ]


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
    attempt_number = _int_variable(ATTEMPT_VARIABLE, 0)
    balanced = _int_variable(BALANCED_VARIABLE, 0)
    # Whether a route's backend pool hands its remaining attempts on to the targets after it.
    spills = any(_balanced_attempts(route) for route in routes)
    exhausted = f"!({_POOL_EXHAUSTED})"
    if spills:
        exhausted = f"(!({_POOL_EXHAUSTED}) || {attempt_number} <= {balanced})"
    if most > 1:
        container = ET.SubElement(
            backend,
            "retry",
            {
                "condition": (
                    f"@(context.Response != null && ({_status_condition(statuses)}) && "
                    f"{exhausted} && "
                    f"{attempt_number} < {_int_variable(ATTEMPTS_VARIABLE, 1)})"
                ),
                "count": str(most - 1),
                "interval": "0",
                "first-fast-retry": "true",
            },
        )
    ET.SubElement(
        container,
        "set-variable",
        {"name": ATTEMPT_VARIABLE, "value": f"@({attempt_number} + 1)"},
    )
    if spills:
        # A backend pool with no member left to try can't take its remaining attempts, so the
        # next attempt goes to the first target after it.
        jump = ET.SubElement(
            ET.SubElement(container, "choose"),
            "when",
            {
                "condition": (
                    f"@(context.Response != null && {_POOL_EXHAUSTED} && "
                    f"{attempt_number} <= {balanced})"
                )
            },
        )
        ET.SubElement(
            jump, "set-variable", {"name": ATTEMPT_VARIABLE, "value": f"@({balanced} + 1)"}
        )
    keyed = _keyed(routes)
    identity = _uses_identity(routes)
    choose = ET.SubElement(container, "choose")
    for route in routes:
        chosen = _string_variable(MODEL_ID_VARIABLE)
        when = ET.SubElement(
            choose,
            "when",
            {"condition": f"@({chosen} == {_literal(route.model_id)})"},
        )
        rewrite = _rewrites_model(shape, route)
        route_keyed = _keyed([route])
        placements: list[tuple[ET.Element, PoolTarget]] = []
        if len(route.targets) == 1:
            placements.append((when, route.targets[0]))
        else:
            attempts = ET.SubElement(when, "choose")
            start = 1
            for target in route.targets:
                end = start + target.attempts - 1
                # Each target takes the attempts after the ones before it.
                number = _int_variable(ATTEMPT_VARIABLE, 1)
                condition = f"{number} == {start}" if start == end else f"{number} <= {end}"
                placements.append(
                    (ET.SubElement(attempts, "when", {"condition": f"@({condition})"}), target)
                )
                start = end + 1
        for parent, target in placements:
            _target(parent, target, rewrite=rewrite)
            if keyed:
                _credential(parent, target, shape, drop_token=identity, drop_key=route_keyed)
    ET.SubElement(
        container,
        "forward-request",
        {"buffer-request-body": "true", "buffer-response": "false"},
    )
    _attempt_trace(container)

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
    _describe_attempt_traces(api_analysis.facets)
    facets = [*fragment_analysis.facets, *api_analysis.facets]
    _describe_credentials(facets, shape)
    return PublicationPolicy(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        content_sha256=combined,
        facets=facets,
        unrecognized_elements=sorted(
            set(fragment_analysis.unrecognized_elements) | set(api_analysis.unrecognized_elements)
        ),
    )


def governed_pool_operations(shape: str) -> tuple[OperationSpec, ...]:
    """The operations a governed pool permits: those whose usage its limits can count."""

    supported = _GOVERNED_OPERATIONS.get(shape)
    operations = (
        tuple(
            operation
            for operation in pool_operations(shape)
            if operation.name in supported and operation.method == "POST"
        )
        if supported
        else ()
    )
    if not operations:
        raise ValidationError("This API shape has no operations governed pool access supports.")
    return operations


@dataclass
class _CounterScope:
    """What a cost center's pooled quota on a pool is counted on: one pool model."""

    tenant_id: str
    id: str


_AMBIGUOUS = "Governed pool grants must have nonempty, unambiguous identities."


def _validate_pool(
    pool: ModelPool,
    snapshot: PoolAccessSnapshot,
    routes: Sequence[PoolRoute],
    safeguard: PoolSafeguard | None,
) -> None:
    names = [
        pool.fragment_name,
        *(route.model_id for route in routes),
        *(target.backend_name for route in routes for target in route.targets),
        *(
            target.key_named_value
            for route in routes
            for target in route.targets
            if target.key_named_value
        ),
    ]
    if not all(_RESOURCE_NAME.fullmatch(name) for name in names):
        raise ValidationError(
            "Governed pool policies require literal APIM backend, fragment, and model names."
        )
    if snapshot.settings.entra_enabled:
        if not _GUID.fullmatch(pool.tenant_id):
            raise ValidationError("Governed Entra access requires a specific tenant GUID.")
        if not snapshot.audience or not _GUID.fullmatch(snapshot.audience):
            raise ValidationError(
                "Governed Entra access requires the runtime application's GUID audience."
            )
    metered = snapshot.token_metering
    if safeguard is not None and not metered:
        raise ValidationError(
            "This pool can't be token-metered on its gateway's tier, so it can't have a safeguard."
        )
    routed = {route.model_id for route in routes}
    reserved = {"master", pool.subscription_name.casefold()}
    seen: dict[str, set[str]] = {"entitlement": set(), "object": set(), "subject": set()}
    counters: list[str] = []
    for grant in snapshot.grants:
        if grant.subject.kind not in {
            EntitlementSubjectKind.USER,
            EntitlementSubjectKind.APPLICATION,
            EntitlementSubjectKind.SECURITY_GROUP,
        }:
            raise ValidationError(
                "Governed pools support only user, application and security-group grants."
            )
        if grant.is_group_grant:
            if not snapshot.settings.entra_enabled:
                raise ValidationError("Security-group grants require governed Entra access.")
            if not _GUID.fullmatch(grant.object_id):
                raise ValidationError("Security-group grants require a GUID object ID.")
        if not all(
            value.strip()
            for value in (
                grant.entitlement_id,
                grant.object_id,
                grant.subject.id,
                grant.pool_model_id,
            )
        ):
            raise ValidationError(_AMBIGUOUS)
        # A subject holds at most one grant to each pool model under each cost center.
        identities = {
            "entitlement": grant.entitlement_id,
            "object": f"{grant.object_id}|{grant.cost_center_id}|{grant.pool_model_id}",
            "subject": f"{grant.subject.id}|{grant.cost_center_id}|{grant.pool_model_id}",
        }
        for kind, identity in identities.items():
            if identity.casefold() in seen[kind]:
                raise ValidationError(_AMBIGUOUS)
            seen[kind].add(identity.casefold())
        if grant.key_name is not None:
            # A key belongs to one subject under one cost center, so it can never serve another
            # subject's grant.
            expected = pool_key_name(
                pool.tenant_id, pool.id, grant.subject.id, grant.cost_center_id
            )
            if grant.key_name != expected:
                raise ValidationError(
                    "A governed pool grant's key must be its subject's key to the pool under the "
                    "grant's cost center."
                )
            if grant.key_name.casefold() in reserved:
                raise ValidationError(
                    "A governed grant cannot use the all-access or pool bootstrap subscription."
                )
        if not grant.enabled:
            # Only enabled grants are compiled, and a disabled one carried forward from an earlier
            # apply must not stop the plan that removes it.
            continue
        if grant.pool_model_id not in routed:
            raise ValidationError("A governed pool grant names a model the pool doesn't serve.")
        _validate_cost_center(grant.cost_center_id, grant.cost_center_code, seen)
        if grant.enforcement is None:
            continue
        if grant.enforcement.tokens:
            if not metered:
                raise ValidationError(
                    "This pool can't be token-metered on its gateway's tier, so its grants can't "
                    "carry token limits."
                )
            counters.append(grant.enforcement.tokens.counter_key_expression)
        if requests := grant.enforcement.requests:
            counters.append(requests.counter_key_expression)
            if requests.renewal_period_seconds and requests.renewal_period_seconds > 300:
                raise ValidationError("Governed request rate renewal must not exceed 300 seconds.")
    if any(re.sub(r"\s+", "", counter) not in _SUBSCRIPTION_COUNTERS for counter in counters):
        raise ValidationError(
            "Governed access replaces the standard subscription counter with a stable grant "
            "counter; custom counter expressions are not supported."
        )
    quoted: set[tuple[str, str]] = set()
    for quota in snapshot.quotas:
        pair = (quota.pool_model_id, quota.cost_center_id)
        if not quota.cost_center_code or pair in quoted:
            raise ValidationError("Each cost center has at most one pooled quota per pool model.")
        quoted.add(pair)
        codes = {
            _code(grant)
            for grant in snapshot.grants
            if grant.enabled and grant.pool_model_id == quota.pool_model_id
        }
        if quota.cost_center_code.casefold() in codes:
            _validate_cost_center(quota.cost_center_id, quota.cost_center_code, seen)
        if quota.monthly_tokens is not None and not metered:
            raise ValidationError(
                "This pool can't be token-metered on its gateway's tier, so a cost center's quota "
                "on it must count calls, not tokens."
            )


def _pool_key_lookup(pool: ModelPool, grants: Sequence[PoolAccessGrant]) -> str:
    """A key's grant to the requested model, as ``identity|code``.

    ``~`` for one of the pool's keys whose subject holds no grant to the model, ``-`` for a grant
    whose cost center turned keys off, and empty for a key that isn't one of the pool's.
    """

    keys: dict[str, list[PoolAccessGrant]] = defaultdict(list)
    for grant in grants:
        if grant.key_name is not None:
            keys[grant.key_name].append(grant)
    lines = [
        'if (context.Subscription == null) { return ""; }',
        "var subscription = context.Subscription.Id;",
        f"var model = {_string_variable(MODEL_ID_VARIABLE)};",
    ]
    for key_name in sorted(keys):
        lines.append(
            f"if (String.Equals(subscription, {_safe_literal(key_name)}, "
            "StringComparison.OrdinalIgnoreCase)) {"
        )
        for grant in sorted(keys[key_name], key=lambda item: item.pool_model_id):
            answer = _match(pool, grant) if grant.keys_allowed else _KEYS_OFF
            lines.append(
                f"    if (model == {_safe_literal(grant.pool_model_id)}) "
                f"{{ return {_safe_literal(answer)}; }}"
            )
        lines.extend([f"    return {_safe_literal(_NO_GRANT)};", "}"])
    return _expression([*lines, 'return "";'])


def _resolve_pool_token_grant(
    parent: ET.Element,
    pool: ModelPool,
    grants: Sequence[PoolAccessGrant],
    *,
    delegated_scope: str,
    application_role: str,
) -> None:
    """Match the validated token to one of a pool model's grants under the selected cost center.

    When none matches and the call names a cost center, also note whether the caller holds the
    model under another, which is the only case refused for naming the wrong cost center. Any
    other unmatched call is refused like one for a model the caller doesn't hold, so a refusal
    says nothing about grants the caller lacks.
    """

    _variable(
        parent,
        "mosaic-token-match",
        _token_lookup(
            pool, grants, delegated_scope=delegated_scope, application_role=application_role
        ),
    )
    _split_match(parent, "mosaic-token-match", "mosaic-token-grant", "mosaic-token-cost-center")
    if any(grant.is_group_grant for grant in grants):
        _variable(parent, "mosaic-member", _token_member_lookup(pool, grants))
    elsewhere = ET.SubElement(
        ET.SubElement(parent, "choose"),
        "when",
        {"condition": f'@(String.IsNullOrEmpty({_TOKEN_GRANT}) && {_COST_CENTER_HEADER} != "")'},
    )
    _variable(
        elsewhere,
        "mosaic-token-held",
        _token_lookup(
            pool,
            grants,
            delegated_scope=delegated_scope,
            application_role=application_role,
            filter_cost_center=False,
        ),
    )


def _model_limits(
    fragment: ET.Element,
    pool: ModelPool,
    snapshot: PoolAccessSnapshot,
    *,
    routes: Sequence[PoolRoute],
    grants: Sequence[PoolAccessGrant],
    safeguard: PoolSafeguard | None,
) -> None:
    """Each pool model's own limits, after the grant's: its cost centers' pooled quotas, then the
    safeguard every caller of the model shares, which counts last."""

    model_id = _string_variable(MODEL_ID_VARIABLE)
    choose = ET.Element("choose")
    for route in routes:
        when = ET.Element(
            "when", {"condition": f"@({model_id} == {_safe_literal(route.model_id)})"}
        )
        codes = {_code(grant) for grant in grants if grant.pool_model_id == route.model_id}
        _pool_limits(
            when,
            _CounterScope(pool.tenant_id, f"{pool.id}/{route.model_id}"),
            [
                quota
                for quota in snapshot.quotas
                if quota.pool_model_id == route.model_id
                and quota.cost_center_code.casefold() in codes
            ],
            estimate_prompt_tokens=False if snapshot.token_metering else None,
            prefix=_COUNTER_PREFIX,
        )
        if safeguard is not None:
            ET.SubElement(when, "llm-token-limit", _safeguard_attributes(route.model_id, safeguard))
        if snapshot.token_metering:
            metric = ET.SubElement(when, "llm-emit-token-metric", {"namespace": METRIC_NAMESPACE})
            for name, value in (("Pool", pool.id), ("Model", route.public_name)):
                ET.SubElement(
                    metric, "dimension", {"name": name, "value": f"@({_safe_literal(value)})"}
                )
        if len(when):
            choose.append(when)
    if len(choose):
        fragment.append(choose)


def _governed_fragment(
    pool: ModelPool,
    snapshot: PoolAccessSnapshot,
    *,
    shape: str,
    routes: Sequence[PoolRoute],
    safeguard: PoolSafeguard | None,
    operations: tuple[OperationSpec, ...],
    grants: Sequence[PoolAccessGrant],
    delegated_scope: str,
    application_role: str,
) -> ET.Element:
    fragment = ET.Element("fragment")
    settings = snapshot.settings
    if not (settings.keys_enabled or settings.entra_enabled):
        _deny(fragment, reason="access-off")
        return fragment
    _credential_prelude(fragment, settings)

    # The pool model the call names. One the pool doesn't serve leaves the model ID empty, and is
    # refused exactly like one the caller holds no grant to, so a refusal never says which
    # models the pool serves.
    model_id = _string_variable(MODEL_ID_VARIABLE)
    _variable(fragment, MODEL_VARIABLE, _requested_model(shape))
    models = ET.SubElement(fragment, "choose")
    for route in routes:
        when = ET.SubElement(
            models,
            "when",
            {
                "condition": (
                    f"@({_string_variable(MODEL_VARIABLE)} == {_safe_literal(route.public_name)})"
                )
            },
        )
        _variable(when, MODEL_ID_VARIABLE, route.model_id)
        _variable(when, ATTEMPTS_VARIABLE, f"@({route.attempts})")
        if balanced := _balanced_attempts(route):
            _variable(when, BALANCED_VARIABLE, f"@({balanced})")

    if settings.keys_enabled:
        key = ET.SubElement(
            ET.SubElement(fragment, "choose"), "when", {"condition": f"@({_HAS_KEY})"}
        )
        _reject(key, _key_shape_check(), reason="key-malformed", code=401)
        _variable(key, "mosaic-key-match", _pool_key_lookup(pool, grants))
        _split_match(key, "mosaic-key-match", "mosaic-key-grant", "mosaic-key-cost-center")
        _reject(key, f"@(String.IsNullOrEmpty({_KEY_GRANT}))", reason="key-unknown")
        _reject(key, f"@(String.IsNullOrEmpty({model_id}))", reason="model")
        _reject(key, f"@({_KEY_GRANT} == {_safe_literal(_NO_GRANT)})", reason="no-grant")
        if any(not grant.keys_allowed for grant in grants if not grant.is_group_grant):
            _reject(
                key,
                f"@({_KEY_GRANT} == {_safe_literal(_KEYS_OFF)})",
                reason="keys-off",
                code=401,
                message=COST_CENTER_KEYS_OFF_DENIED,
            )
        # A key serves one cost center. A header naming another is refused rather than ignored.
        _reject(
            key,
            f'@({_COST_CENTER_HEADER} != "" && {_COST_CENTER_HEADER} != {_KEY_COST_CENTER})',
            reason="cost-center-mismatch",
            message=COST_CENTER_MISMATCH_DENIED,
        )
        _variable(key, "mosaic-cc", f"@({_KEY_COST_CENTER})")

    if settings.entra_enabled:
        token = ET.SubElement(
            ET.SubElement(fragment, "choose"), "when", {"condition": f"@({_HAS_TOKEN})"}
        )
        _validate_token(token, tenant_id=pool.tenant_id, audience=snapshot.audience)
        _variable(token, "mosaic-token-held", "")
        by_model: dict[str, list[PoolAccessGrant]] = defaultdict(list)
        for grant in grants:
            by_model[grant.pool_model_id].append(grant)
        if by_model:
            choose = ET.SubElement(token, "choose")
            for pool_model_id in sorted(by_model):
                when = ET.SubElement(
                    choose,
                    "when",
                    {"condition": f"@({model_id} == {_safe_literal(pool_model_id)})"},
                )
                _resolve_pool_token_grant(
                    when,
                    pool,
                    by_model[pool_model_id],
                    delegated_scope=delegated_scope,
                    application_role=application_role,
                )
        # The refusals below apply to every model alike, so none can tell a model the pool
        # doesn't serve from one the caller doesn't hold, or which models have group grants.
        if any(grant.is_group_grant for grant in grants):
            _reject(
                token,
                _token_groups_overage(),
                reason="groups-overage",
                with_caller=True,
                message=_GROUPS_OVERAGE_DENIED,
            )
        _reject(
            token,
            '@((string)context.Variables["mosaic-token-held"] != "")',
            reason="cost-center",
            with_caller=True,
            message=COST_CENTER_DENIED,
        )
        _reject(token, f"@(String.IsNullOrEmpty({model_id}))", reason="model", with_caller=True)
        _reject(
            token,
            f"@(String.IsNullOrEmpty({_TOKEN_GRANT}))",
            reason="no-grant",
            with_caller=True,
        )

    _select_grant(fragment)
    _cost_center_ids(fragment, grants)
    append_budget_check(fragment, grants)
    allowed = " || ".join(
        f"context.Operation.Id == {_safe_literal(operation.name)}" for operation in operations
    )
    _reject(
        fragment,
        f'@(context.Operation == null || context.Request.Method != "POST" || !({allowed}))',
        reason="operation",
        with_caller=True,
        message="This operation is not available through governed access.",
    )
    _variable(
        fragment,
        "mosaic-mcp-call",
        mcp_call_reference(application_role) if settings.entra_enabled else "",
    )
    append_grant_attribution_trace(fragment, grants, keys=[("r", MCP_CALL, "mosaic-mcp-call")])
    _grant_limits(fragment, pool, grants, prefix=_COUNTER_PREFIX)
    _grant_token_limits(fragment, pool, grants, prefix=_COUNTER_PREFIX)
    _model_limits(fragment, pool, snapshot, routes=routes, grants=grants, safeguard=safeguard)

    removed = (
        "Ocp-Apim-Subscription-Key",
        "api-key",
        "Authorization",
        COST_CENTER_HEADER,
        ON_BEHALF_HEADER,
        # A caller must not steer Azure's own overflow to a deployment it was never told about.
        "x-ms-spillover-deployment",
    )
    for name in removed:
        ET.SubElement(fragment, "set-header", {"name": name, "exists-action": "delete"})
    ET.SubElement(
        fragment, "set-query-parameter", {"name": "subscription-key", "exists-action": "delete"}
    )
    if _keyed(routes):
        # Only the gateway's own credentials may reach a member, as ADR 0018 requires.
        strip_caller_credentials(
            fragment,
            removed_headers=(*removed, *shape_removed_headers(shape)),
            removed_query_parameters=("subscription-key",),
        )
    add_shape_headers(fragment, shape)
    _authenticate(fragment, shape, routes)
    _append_rewrites(fragment, shape, operations)
    return fragment


def _describe_safeguard(facet: PolicyFacet) -> None:
    facet.summary = re.sub(
        r"counted .+\.$", "counted per pool model, shared by every caller.", facet.summary
    )
    facet.attributes["counter-scope"] = "pool-model"
    facet.details.extend(
        [
            "The pool's safeguard protects its members' capacity from the pool's own callers. It "
            "counts last, after the grant's limits and its cost center's pooled quota.",
            "Native APIM limits are distributed/per gateway, not exact global or billing totals.",
        ]
    )


def _governed_operations_facet(shape: str, operations: tuple[OperationSpec, ...]) -> PolicyFacet:
    names = [operation.name for operation in operations]
    refused = [
        operation.name for operation in pool_operations(shape) if operation.name not in names
    ]
    details = [f"Allowed curated operation IDs: {', '.join(names)}."]
    if refused:
        details.append(f"The pool's other operations are denied: {', '.join(refused)}.")
    details.append(
        "The call's model must be one the caller holds a grant to; the pool then routes it to "
        "one of that model's members."
    )
    return PolicyFacet(
        kind=PolicyFacetKind.AUTHORIZATION,
        element="choose",
        section=PolicySection.INBOUND,
        summary=(
            "Governed access permits only the Anthropic Messages operation."
            if shape == ApiShape.ANTHROPIC_MESSAGES
            else "Governed access permits only chat completions."
        ),
        details=details,
        attributes={"allowed-operations": ",".join(names)},
        managed_by_mosaic=True,
    )


def _governed_facets(
    snapshot: PoolAccessSnapshot,
    *,
    shape: str,
    fragment: ET.Element,
    fragment_xml: str,
    api_policy_xml: str,
    operations: tuple[OperationSpec, ...],
    delegated_scope: str,
    application_role: str,
) -> tuple[list[PolicyFacet], list[str]]:
    fragment_analysis = analyze_policy(fragment_xml)
    api_analysis = analyze_policy(api_policy_xml)
    settings = snapshot.settings
    methods = []
    if settings.keys_enabled:
        methods.append("an allowlisted APIM subscription key")
    if settings.entra_enabled:
        methods.append("a validated Microsoft Entra bearer token")
    enabled = [grant for grant in snapshot.grants if grant.enabled]
    group_grants = sum(grant.is_group_grant for grant in enabled)
    details = [
        "Every presented credential must be enabled and valid; there is no fallback.",
        "When a key and a token are both presented, they must resolve to the same enabled grant.",
        "Keys in both header and query must be identical; ambiguous or empty credentials "
        "are denied.",
        f"User tokens require {delegated_scope} in scp; application tokens require "
        f"{application_role} in roles with no scp claim. Claims are read only after signature, "
        "tenant, audience and expiry validation.",
        "Each grant is to one pool model, and the model a call names selects which of the "
        "caller's grants applies.",
        "One key serves every pool model its subject holds directly under one cost center.",
        "Only the pool's own keys are allowlisted; all-access, bootstrap and unrelated "
        "subscriptions are denied. No caller identity header or control-plane callback is used.",
        "A model the pool doesn't serve is refused with 403 exactly like one the caller holds no "
        "grant to, so a refusal never reveals which models the pool serves.",
    ]
    attributes = {
        "keys-enabled": str(settings.keys_enabled).lower(),
        "entra-enabled": str(settings.entra_enabled).lower(),
        "enabled-grants": str(len(enabled)),
    }
    if group_grants:
        details.extend(
            [
                f"{group_grants} enabled security-group grant"
                f"{'' if group_grants == 1 else 's'} match the validated token's groups claim.",
                "Security-group grants accept Microsoft Entra tokens only; they have no APIM "
                "subscription key path.",
                "Security-group grant limits apply separately to each validated member object ID.",
                "A direct user or application grant to the caller wins before any group grant is "
                "considered.",
            ]
        )
        attributes["security-group-grants"] = str(group_grants)
    details.extend(cost_center_details())
    attributes["cost-centers"] = str(len({grant.cost_center_id for grant in enabled}))
    summary = f"Requires {' or '.join(methods)}." if methods else "All model access is denied."
    facets = [
        PolicyFacet(
            kind=PolicyFacetKind.AUTHORIZATION,
            element="choose",
            section=PolicySection.INBOUND,
            summary=summary,
            details=details,
            attributes=attributes,
            managed_by_mosaic=True,
        ),
        _governed_operations_facet(shape, operations),
    ]
    limits = iter(element for element in fragment.iter() if element.tag in _LIMIT_ELEMENTS)
    fragment_facets = classify_traces(
        fragment, fragment_analysis.facets, has_group_grants=group_grants > 0
    )
    _describe_attempt_traces(api_analysis.facets)
    _describe_credentials([*fragment_facets, *api_analysis.facets], shape)
    for facet in [*fragment_facets, *api_analysis.facets]:
        facet.managed_by_mosaic = True
        if facet.section == PolicySection.UNKNOWN:
            facet.section = PolicySection.INBOUND
        if facet.element in _LIMIT_ELEMENTS:
            # Every limit is in the fragment, in document order.
            element = next(limits)
            if _COUNTER_PREFIX in element.get("counter-key", ""):
                describe_limit_facet(
                    facet, element, _COUNTER_PREFIX, owner="pool", pooled_owner="pool model"
                )
            else:
                _describe_safeguard(facet)
        elif facet.element == "set-backend-service":
            facet.summary = "Routes authorized requests to a member of the requested pool model."
            facet.attributes = {"backend-id": "[redacted]"}
        elif facet.element == "include-fragment":
            facet.summary = "Applies the MOSAIC-managed governed pool access rule set."
            facet.attributes = {"fragment-id": "[redacted]"}
        elif facet.element == "set-query-parameter":
            name = facet.attributes.get("name", "subscription-key")
            facet.summary = f"Removes the {name} query parameter before forwarding."
        elif (
            facet.element == "set-header"
            and facet.attributes.get("name", "").casefold() == ON_BEHALF_HEADER
        ):
            describe_on_behalf_removal(facet, "model")
        facets.append(facet)
    return facets, sorted(
        set(fragment_analysis.unrecognized_elements) | set(api_analysis.unrecognized_elements)
    )


def render_governed_pool_policy(
    *,
    pool: ModelPool,
    snapshot: PoolAccessSnapshot,
    shape: str,
    routes: list[PoolRoute],
    preset: BreakerPreset,
    safeguard: PoolSafeguard | None,
    delegated_scope: str = "Models.Invoke",
    application_role: str = "Models.Invoke.Application",
) -> PublicationPolicy:
    """Author a governed pool's fragment and API policy; reject unsafe intent with ValidationError.

    ``snapshot``, rather than the pool's saved access settings or its grants' current state, is
    the authority for what the fragment enforces. Only the pool's identity and resource names are
    read from ``pool``.
    """

    if not routes:
        raise ValueError("A pool policy needs at least one model")
    _validate_pool(pool, snapshot, routes, safeguard)
    operations = governed_pool_operations(shape)
    grants = sorted(
        (grant for grant in snapshot.grants if grant.enabled),
        key=lambda grant: grant.entitlement_id,
    )
    fragment = _governed_fragment(
        pool,
        snapshot,
        shape=shape,
        routes=routes,
        safeguard=safeguard,
        operations=operations,
        grants=grants,
        delegated_scope=delegated_scope,
        application_role=application_role,
    )
    fragment_xml = _serialize(fragment)
    if len(fragment_xml.encode("utf-8")) > MAX_FRAGMENT_BYTES:
        raise ValidationError(
            "The governed pool policy fragment exceeds APIM's 512 KB UTF-8 size limit."
        )
    api_policy_xml = _serialize(
        _api_policy(fragment_name=pool.fragment_name, shape=shape, routes=routes, preset=preset)
    )
    facets, unrecognized = _governed_facets(
        snapshot,
        shape=shape,
        fragment=fragment,
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        operations=operations,
        delegated_scope=delegated_scope,
        application_role=application_role,
    )
    return PublicationPolicy(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        content_sha256=hashlib.sha256(f"{fragment_xml}\n{api_policy_xml}".encode()).hexdigest(),
        facets=facets,
        unrecognized_elements=unrecognized,
    )


def pool_grant_counter_identity(pool: ModelPool, grant: PoolAccessGrant) -> str:
    """The identity a governed pool's trace names a grant's calls by, as ``g=``."""

    return grant_counter_identity(pool, grant)


def pool_grant_counter_key_expression(pool: ModelPool, grant: PoolAccessGrant) -> str:
    """The counter a governed pool grant's own token limit counts on."""

    return _grant_counter_key(
        "grant-tokens",
        pool_grant_counter_identity(pool, grant),
        per_member=grant.is_group_grant,
        prefix=_COUNTER_PREFIX,
    )


__all__ = [
    "DEPLOYMENT_PARAMETER",
    "IDENTITY_TOKEN_SUMMARY",
    "PoolRoute",
    "PoolTarget",
    "body_routed",
    "governed_pool_operations",
    "member_backend_url",
    "pool_grant_counter_identity",
    "pool_grant_counter_key_expression",
    "pool_operations",
    "render_governed_pool_policy",
    "render_pool_policy",
    "template_parameters",
]
