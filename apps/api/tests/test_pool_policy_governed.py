"""Governed model pool policy: what its fragment compiles, not a gateway conformance suite.

A governed pool authorizes a call exactly as a governed publication does, except that each grant
is to one pool model, and the model a call names selects which of the caller's grants applies
(ADR 0024).
"""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest
from apim_double import expression_error, policy_expression_error
from mosaic_api.budgets import budget_key
from mosaic_api.domain import (
    ApiShape,
    EntitlementEnforcement,
    EntitlementSubject,
    EntitlementSubjectKind,
    ModelAccessSettings,
    PolicySection,
    RequestEnforcement,
    TokenEnforcement,
    general_cost_center_id,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations import access_policy, pool_policy
from mosaic_api.integrations.access_policy import (
    BLOCKED_LIST_UNREADABLE,
    BUDGET_DENIED,
    COST_CENTER_DENIED,
    COST_CENTER_KEYS_OFF_DENIED,
    COST_CENTER_MISMATCH_DENIED,
)
from mosaic_api.integrations.policy import (
    METRIC_NAMESPACE,
    PublicationPolicy,
    managed_identity_resource,
)
from mosaic_api.integrations.pool_policy import (
    ATTEMPT_TRACE_SUMMARY,
    NOT_FOUND_BODY,
    PoolRoute,
    PoolTarget,
    governed_pool_operations,
    pool_operations,
    render_governed_pool_policy,
    render_pool_policy,
)
from mosaic_api.model_pools import (
    BreakerPreset,
    ModelPool,
    PoolAccessGrant,
    PoolAccessSnapshot,
    PoolModelQuota,
    PoolSafeguard,
    model_pool_id,
    pool_key_name,
    pool_model_id,
)

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "22222222-2222-2222-2222-222222222222"
GATEWAY = "gateway-1"
API_NAME = "mosaic-pool-openai"
POOL_ID = model_pool_id(TENANT, GATEWAY, API_NAME)
GPT = pool_model_id(POOL_ID, "gpt-4o")
MINI = pool_model_id(POOL_ID, "gpt-4o-mini")
GENERAL = general_cost_center_id(TENANT)
RESEARCH = "costCenter_research"
SALES = "costCenter_sales"
# One code is mixed case, so the tests see every code compiled casefolded.
_CODES = {GENERAL: "general", RESEARCH: "Research", SALES: "sales"}
SUBSCRIPTION_COUNTER = "@(context.Subscription.Id)"
GPT_BACKEND_POOL = "mosaic-pool-openai-gpt-4o-pool-1a2b3c4d"
MINI_MEMBERS = (
    "mosaic-pool-openai-gpt-4o-mini-aaaa1111",
    "mosaic-pool-openai-gpt-4o-mini-bbbb2222",
)
_SHAPES = (ApiShape.AZURE_OPENAI, ApiShape.FOUNDRY_MODELS, ApiShape.ANTHROPIC_MESSAGES)
_DENIED = access_policy._DENIED
_OPERATION_DENIED = "This operation is not available through governed access."
_HAS_KEY = '@((bool)context.Variables["mosaic-has-key"])'
_HAS_TOKEN = '@((bool)context.Variables["mosaic-has-token"])'
_MODEL = 'context.Variables.GetValueOrDefault<string>("mosaic-pool-model", "")'
_MODEL_ID = 'context.Variables.GetValueOrDefault<string>("mosaic-pool-model-id", "")'
_GRANT = '(string)context.Variables["mosaic-grant"]'
_HELD = (
    '@(String.IsNullOrEmpty((string)context.Variables["mosaic-token-grant"]) && '
    '(string)context.Variables["mosaic-cost-center-header"] != "")'
)
_REASON = re.compile(r"mosaic-deny v=1 r=([a-z-]+)")


def _pool(**overrides: object) -> ModelPool:
    return ModelPool.model_validate(
        {
            "id": POOL_ID,
            "tenant_id": TENANT,
            "gateway_id": GATEWAY,
            "display_name": "OpenAI",
            "api_name": API_NAME,
            "api_path": "mosaic/pool-openai",
            "fragment_name": API_NAME,
            "product_name": API_NAME,
            "subscription_name": API_NAME,
            **overrides,
        }
    )


def _routes() -> list[PoolRoute]:
    return [
        PoolRoute(GPT, "gpt-4o", (PoolTarget(GPT_BACKEND_POOL, "gpt-4o"),), 3),
        PoolRoute(
            MINI,
            "gpt-4o-mini",
            tuple(PoolTarget(member, "gpt-4o-mini") for member in MINI_MEMBERS),
            2,
        ),
    ]


def _object_id(person: int) -> str:
    return f"{person:08d}-3333-3333-3333-333333333333"


def _key(person: int, cost_center: str = GENERAL) -> str:
    return pool_key_name(TENANT, POOL_ID, f"principal-{person}", cost_center)


def _grant(
    number: int,
    *,
    person: int | None = None,
    model: str = GPT,
    cost_center: str = GENERAL,
    kind: EntitlementSubjectKind = EntitlementSubjectKind.USER,
    **overrides: object,
) -> PoolAccessGrant:
    """A direct grant to one pool model. ``person`` is whom it's for: by default, ``number``."""

    person = number if person is None else person
    return PoolAccessGrant.model_validate(
        {
            "entitlement_id": f"entitlement-{number}",
            "pool_model_id": model,
            "subject": EntitlementSubject(kind=kind, id=f"principal-{person}"),
            "object_id": _object_id(person),
            "display_name": f"Private Person {person}",
            "key_name": _key(person, cost_center),
            "enabled": True,
            "intent_digest": f"intent-{number}",
            "cost_center_id": cost_center,
            "cost_center_code": _CODES[cost_center],
            **overrides,
        }
    )


def _group_object_id(number: int) -> str:
    return f"{number:08d}-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


def _group_grant(
    number: int, *, model: str = GPT, cost_center: str = GENERAL, **overrides: object
) -> PoolAccessGrant:
    return PoolAccessGrant.model_validate(
        {
            "entitlement_id": f"entitlement-{number}",
            "pool_model_id": model,
            "subject": EntitlementSubject(
                kind=EntitlementSubjectKind.SECURITY_GROUP, id=f"group-{number}"
            ),
            "object_id": _group_object_id(number),
            "display_name": f"Private Group {number}",
            "enabled": True,
            "intent_digest": f"intent-{number}",
            "cost_center_id": cost_center,
            "cost_center_code": _CODES[cost_center],
            **overrides,
        }
    )


def _limits(
    *, tokens: dict[str, Any] | None = None, requests: dict[str, Any] | None = None
) -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=(
            None
            if tokens is None
            else TokenEnforcement.model_validate(
                {"counter_key_expression": SUBSCRIPTION_COUNTER, **tokens}
            )
        ),
        requests=(
            None
            if requests is None
            else RequestEnforcement.model_validate(
                {"counter_key_expression": SUBSCRIPTION_COUNTER, **requests}
            )
        ),
    )


def _snapshot(
    *grants: PoolAccessGrant, keys: bool = True, entra: bool = True, **overrides: object
) -> PoolAccessSnapshot:
    return PoolAccessSnapshot.model_validate(
        {
            "version": 1,
            "settings": ModelAccessSettings(keys_enabled=keys, entra_enabled=entra),
            "audience": AUDIENCE,
            "grants": list(grants),
            **overrides,
        }
    )


def _quota(model: str, cost_center: str, **limits: int) -> PoolModelQuota:
    return PoolModelQuota.model_validate(
        {
            "pool_model_id": model,
            "cost_center_id": cost_center,
            "cost_center_code": _CODES[cost_center],
            **limits,
        }
    )


def _render(
    snapshot: PoolAccessSnapshot,
    *,
    shape: str = ApiShape.AZURE_OPENAI,
    routes: list[PoolRoute] | None = None,
    safeguard: PoolSafeguard | None = None,
    pool: ModelPool | None = None,
) -> PublicationPolicy:
    return render_governed_pool_policy(
        pool=pool or _pool(),
        snapshot=snapshot,
        shape=shape,
        routes=_routes() if routes is None else routes,
        preset=BreakerPreset.THROTTLING,
        safeguard=safeguard,
    )


def _fragment(snapshot: PoolAccessSnapshot, **options: Any) -> ET.Element:
    return ET.fromstring(_render(snapshot, **options).fragment_xml)


def _identity(*parts: str) -> str:
    value = json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _grant_id(grant: PoolAccessGrant) -> str:
    """A grant's counter identity: its tenant, pool and entitlement, never its credential."""

    return _identity(TENANT, POOL_ID, grant.entitlement_id)


def _match(grant: PoolAccessGrant) -> str:
    """What a lookup answers for a grant: its identity and its cost center's code."""

    return f"{_grant_id(grant)}|{grant.cost_center_code.casefold()}"


def _cost_center_identity(model: str, cost_center: str) -> str:
    """A pooled quota's counter identity: one cost center on one pool model."""

    return _identity(TENANT, f"{POOL_ID}/{model}", "cost-center", cost_center)


@dataclass(frozen=True)
class _Refusal:
    reason: str
    code: str
    status: str
    body: str
    records_caller: bool


def _refusals(root: ET.Element) -> list[_Refusal]:
    """Every refusal under ``root``, in document order."""

    parents = {child: parent for parent in root.iter() for child in parent}
    refusals = []
    for trace in root.iter("trace"):
        message = trace.findtext("message") or ""
        match = _REASON.search(message)
        if match is None:
            continue
        response = parents[trace].find("return-response")
        assert response is not None
        status = response.find("set-status")
        assert status is not None
        refusals.append(
            _Refusal(
                reason=match.group(1),
                code=status.get("code", ""),
                status=status.get("reason", ""),
                body=response.findtext("set-body") or "",
                records_caller=" o=" in message,
            )
        )
    return refusals


def _reasons(root: ET.Element) -> list[str]:
    return [refusal.reason for refusal in _refusals(root)]


def _refused_reason(when: ET.Element) -> str | None:
    """The reason a ``when`` refuses a call for, if it holds a refusal itself."""

    trace = when.find("trace")
    match = _REASON.search(trace.findtext("message") or "") if trace is not None else None
    return match.group(1) if match else None


def _refusal_condition(root: ET.Element, reason: str) -> str:
    """The condition of the one refusal for ``reason`` among ``root``'s own chooses."""

    (condition,) = [
        when.get("condition", "")
        for choose in root.findall("choose")
        for when in choose.findall("when")
        if _refused_reason(when) == reason
    ]
    return condition


def _branch(root: ET.Element, condition: str) -> ET.Element:
    """The one ``when`` with ``condition`` among ``root``'s own chooses."""

    (when,) = [
        when
        for choose in root.findall("choose")
        for when in choose.findall("when")
        if when.get("condition") == condition
    ]
    return when


def _variables(parent: ET.Element) -> dict[str, str]:
    return {
        element.get("name", ""): element.get("value", "")
        for element in parent.findall("set-variable")
    }


def _describe(element: ET.Element) -> str:
    """What one of the fragment's own elements does, in a word or two."""

    if element.tag == "set-variable":
        return f"set {element.get('name')}"
    if element.tag in {"set-header", "set-query-parameter"}:
        return f"{element.tag} {element.get('name')} {element.get('exists-action')}"
    if element.tag == "trace":
        message = element.findtext("message") or ""
        assert message.startswith('@("mosaic-attribution v=1 g=')
        return "attribution"
    if element.tag != "choose":
        return element.tag
    whens = element.findall("when")
    conditions = [when.get("condition", "") for when in whens]
    assert conditions
    assert element.find("otherwise") is None
    if len(whens) == 1 and (reason := _refused_reason(whens[0])):
        return f"refuse {reason}"
    if conditions == [_HAS_KEY]:
        return "key"
    if conditions == [_HAS_TOKEN]:
        return "token"
    if all(condition.startswith(f"@({_MODEL} == ") for condition in conditions):
        return "models"
    if all(condition.startswith(f"@({_MODEL_ID} == ") for condition in conditions):
        return "per-model"
    if all(condition.startswith(f"@({_GRANT} == ") for condition in conditions):
        return "grant-limits"
    if conditions == ['@(context.Operation.Id == "chat-completions")']:
        return "rewrites"
    if conditions == [_HELD]:
        return "held"
    raise AssertionError(f"An unexpected choose: {conditions}")


def _outline(root: ET.Element) -> list[str]:
    return [_describe(element) for element in root]


_KEY_BLOCK = re.compile(
    r'if \(String\.Equals\(subscription, "([^"]+)", StringComparison\.OrdinalIgnoreCase\)\) \{\n'
    r"(.*?)\n\}",
    re.S,
)
_KEY_MODEL = re.compile(r'    if \(model == "([^"]+)"\) \{ return "([^"]*)"; \}')
_KEY_FALLBACK = re.compile(r'    return "([^"]*)";')


def _key_answers(expression: str) -> dict[str, tuple[dict[str, str], str]]:
    """A key lookup's answers, in order: per key, each model's answer and any other model's."""

    answers: dict[str, tuple[dict[str, str], str]] = {}
    for key, body in _KEY_BLOCK.findall(expression):
        *models, last = body.split("\n")
        fallback = _KEY_FALLBACK.fullmatch(last)
        assert fallback is not None
        matches = [_KEY_MODEL.fullmatch(line) for line in models]
        assert all(matches)
        answers[key] = (
            {match.group(1): match.group(2) for match in matches if match},
            fallback.group(1),
        )
    return answers


def _key_lookup(fragment: ET.Element) -> str:
    return _variables(_branch(fragment, _HAS_KEY))["mosaic-key-match"]


def _token_models(fragment: ET.Element) -> dict[str, ET.Element]:
    """The token branch's ``when`` for each model it has grants to, in document order."""

    token = _branch(fragment, _HAS_TOKEN)
    pattern = re.compile(rf'@\({re.escape(_MODEL_ID)} == "([^"]+)"\)')
    found: dict[str, ET.Element] = {}
    for choose in token.findall("choose"):
        for when in choose.findall("when"):
            if match := pattern.fullmatch(when.get("condition", "")):
                found[match.group(1)] = when
    return found


_PRELUDE = [
    "set mosaic-has-key",
    "set mosaic-has-token",
    "set mosaic-caller",
    "set mosaic-client",
    "refuse no-credential",
]
_RESOLUTION = [
    "set mosaic-cost-center-header",
    "set mosaic-cc",
    "refuse cost-center",
    "set mosaic-key-grant",
    "set mosaic-key-cost-center",
    "set mosaic-token-grant",
    "set mosaic-token-cost-center",
    "set mosaic-member",
    "set mosaic-pool-model",
    "models",
]
_SELECTION = [
    "refuse grant-mismatch",
    "set mosaic-grant",
    "set mosaic-cost-center",
    "set mosaic-cost-center-id",
    "set mosaic-budget-key",
    "refuse budget-list",
    "refuse budget",
    "refuse operation",
    "attribution",
]
_FORWARDING = [
    "set-header Ocp-Apim-Subscription-Key delete",
    "set-header api-key delete",
    "set-header Authorization delete",
    "set-header x-mosaic-cost-center delete",
    "set-header x-ms-spillover-deployment delete",
    "set-query-parameter subscription-key delete",
]


def _limited_grant(number: int = 1, **overrides: Any) -> PoolAccessGrant:
    return _grant(
        number,
        enforcement=_limits(
            tokens={"tokens_per_minute": 1_000},
            requests={"calls": 10, "renewal_period_seconds": 60},
        ),
        **overrides,
    )


def test_a_governed_pool_keeps_its_api_policy_and_digests_both_documents() -> None:
    snapshot = _snapshot(_limited_grant(1), _grant(2, model=MINI))
    safeguard = PoolSafeguard(tokens_per_minute=10_000)
    result = _render(snapshot, safeguard=safeguard)
    ungoverned = render_pool_policy(
        pool_id=POOL_ID,
        fragment_name=API_NAME,
        shape=ApiShape.AZURE_OPENAI,
        routes=_routes(),
        preset=BreakerPreset.THROTTLING,
        safeguard=safeguard,
    )

    # Governance changes who may call, never how a call is routed or retried.
    assert result.api_policy_xml == ungoverned.api_policy_xml
    assert result.fragment_xml != ungoverned.fragment_xml
    assert (
        result.content_sha256
        == hashlib.sha256(f"{result.fragment_xml}\n{result.api_policy_xml}".encode()).hexdigest()
    )
    assert result.unrecognized_elements == []


def test_the_documents_depend_on_the_grants_not_their_order_or_the_snapshot_version() -> None:
    grants = [_limited_grant(1), _grant(2, model=MINI), _group_grant(3, model=MINI)]
    result = _render(_snapshot(*grants))

    assert _render(_snapshot(*reversed(grants))) == result
    # Counters never move with a new snapshot, so a re-apply never resets anyone's usage.
    assert _render(_snapshot(*grants, version=9)) == result


def test_a_pool_with_both_access_methods_off_refuses_every_call() -> None:
    result = _render(_snapshot(_grant(1), keys=False, entra=False))
    fragment = ET.fromstring(result.fragment_xml)

    assert [element.tag for element in fragment] == ["trace", "return-response"]
    assert _refusals(fragment) == [_Refusal("access-off", "403", "Forbidden", _DENIED, False)]
    assert result.facets[0].summary == "All model access is denied."
    assert result.facets[0].attributes["keys-enabled"] == "false"
    assert result.facets[0].attributes["entra-enabled"] == "false"


@pytest.mark.parametrize(
    ("keys", "entra", "authorization"),
    [
        (True, False, ["refuse tokens-off", *_RESOLUTION, "key"]),
        (False, True, ["refuse keys-off", *_RESOLUTION, "token"]),
        (True, True, [*_RESOLUTION, "key", "token"]),
    ],
    ids=["keys", "entra", "both"],
)
def test_the_fragment_authorizes_then_selects_a_grant_then_limits_then_forwards(
    keys: bool, entra: bool, authorization: list[str]
) -> None:
    fragment = _fragment(
        _snapshot(_limited_grant(), keys=keys, entra=entra),
        safeguard=PoolSafeguard(tokens_per_minute=10_000),
    )

    assert _outline(fragment) == [
        *_PRELUDE,
        *authorization,
        *_SELECTION,
        "grant-limits",
        "per-model",
        *_FORWARDING,
        "authentication-managed-identity",
        "rewrites",
    ]


def test_an_anthropic_pool_also_replaces_the_callers_key_header_and_rewrites_no_route() -> None:
    fragment = _fragment(_snapshot(_grant(1)), shape=ApiShape.ANTHROPIC_MESSAGES)

    assert _outline(fragment) == [
        *_PRELUDE,
        *_RESOLUTION,
        "key",
        "token",
        *_SELECTION,
        "per-model",
        *_FORWARDING,
        "set-header x-api-key delete",
        "set-header anthropic-version skip",
        "authentication-managed-identity",
    ]
    identity = fragment.find("authentication-managed-identity")
    assert identity is not None
    assert identity.get("resource") == managed_identity_resource(ApiShape.ANTHROPIC_MESSAGES)


def test_a_foundry_pool_has_no_route_to_rewrite() -> None:
    fragment = _fragment(_snapshot(_grant(1)), shape=ApiShape.FOUNDRY_MODELS)

    assert _outline(fragment)[-2:] == [
        "set-query-parameter subscription-key delete",
        "authentication-managed-identity",
    ]
    identity = fragment.find("authentication-managed-identity")
    assert identity is not None
    assert identity.get("resource") == managed_identity_resource(ApiShape.FOUNDRY_MODELS)


def test_each_refusal_says_why_with_a_fixed_status_and_body() -> None:
    fragment = _fragment(
        _snapshot(_grant(1), _grant(2, keys_allowed=False), _group_grant(3, model=MINI))
    )

    # Nothing before the token validates can record a caller: there's none yet.
    assert _refusals(fragment) == [
        _Refusal("no-credential", "401", "Unauthorized", _DENIED, False),
        _Refusal("cost-center", "403", "Forbidden", COST_CENTER_DENIED, False),
        _Refusal("key-malformed", "401", "Unauthorized", _DENIED, False),
        _Refusal("key-unknown", "403", "Forbidden", _DENIED, False),
        _Refusal("model", "403", "Forbidden", _DENIED, False),
        _Refusal("no-grant", "403", "Forbidden", _DENIED, False),
        _Refusal("keys-off", "401", "Unauthorized", COST_CENTER_KEYS_OFF_DENIED, False),
        _Refusal("cost-center-mismatch", "403", "Forbidden", COST_CENTER_MISMATCH_DENIED, False),
        _Refusal("token-malformed", "401", "Unauthorized", _DENIED, False),
        _Refusal("groups-overage", "403", "Forbidden", access_policy._GROUPS_OVERAGE_DENIED, True),
        _Refusal("cost-center", "403", "Forbidden", COST_CENTER_DENIED, True),
        _Refusal("model", "403", "Forbidden", _DENIED, True),
        _Refusal("no-grant", "403", "Forbidden", _DENIED, True),
        _Refusal("grant-mismatch", "403", "Forbidden", _DENIED, True),
        _Refusal("budget-list", "503", "Service Unavailable", BLOCKED_LIST_UNREADABLE, True),
        _Refusal("budget", "403", "Forbidden", BUDGET_DENIED, True),
        _Refusal("operation", "403", "Forbidden", _OPERATION_DENIED, True),
    ]


@pytest.mark.parametrize(
    ("keys", "entra", "reason", "condition"),
    [(False, True, "keys-off", _HAS_KEY), (True, False, "tokens-off", _HAS_TOKEN)],
)
def test_a_credential_whose_method_is_off_is_refused_before_anything_reads_it(
    keys: bool, entra: bool, reason: str, condition: str
) -> None:
    fragment = _fragment(_snapshot(_grant(1), keys=keys, entra=entra))

    assert _refusals(fragment)[1] == _Refusal(reason, "401", "Unauthorized", _DENIED, False)
    assert _refusal_condition(fragment, reason) == condition


def test_a_key_lookup_answers_each_keys_grant_for_each_model_and_nothing_else() -> None:
    held = [
        _grant(1, model=MINI),
        _grant(2, person=1),
        _grant(3, person=1, cost_center=RESEARCH),
        _grant(4, keys_allowed=False),
        _group_grant(5),
        _grant(6, enabled=False),
    ]
    lookup = _key_lookup(_fragment(_snapshot(*held)))
    answers = _key_answers(lookup)

    # One key serves every model its subject holds directly under one cost center. Any other
    # model gets "~", and a grant whose cost center turned keys off gets "-".
    assert answers == {
        _key(1): ({GPT: _match(held[1]), MINI: _match(held[0])}, "~"),
        _key(1, RESEARCH): ({GPT: _match(held[2])}, "~"),
        _key(4): ({GPT: "-"}, "~"),
    }
    assert list(answers) == sorted(answers)
    assert all(list(models) == sorted(models) for models, _ in answers.values())
    assert lookup.startswith(
        '@{\nif (context.Subscription == null) { return ""; }\n'
        f"var subscription = context.Subscription.Id;\nvar model = {_MODEL_ID};\n"
    )
    # Any other subscription, including the all-access and bootstrap ones, is no key at all.
    assert lookup.endswith('\nreturn "";\n}')


@pytest.mark.parametrize("keys_off", [False, True], ids=["keys-on", "keys-off"])
def test_a_key_is_matched_to_the_named_model_then_held_to_its_cost_center(keys_off: bool) -> None:
    key = _branch(_fragment(_snapshot(_grant(1), _grant(2, keys_allowed=not keys_off))), _HAS_KEY)

    assert _outline(key) == [
        "refuse key-malformed",
        "set mosaic-key-match",
        "set mosaic-key-grant",
        "set mosaic-key-cost-center",
        "refuse key-unknown",
        "refuse model",
        "refuse no-grant",
        *(["refuse keys-off"] if keys_off else []),
        "refuse cost-center-mismatch",
        "set mosaic-cc",
    ]
    assert _refusal_condition(key, "model") == f"@(String.IsNullOrEmpty({_MODEL_ID}))"
    assert (
        _refusal_condition(key, "no-grant")
        == '@((string)context.Variables["mosaic-key-grant"] == "~")'
    )
    # The key's cost center is the one the token, if any, must resolve under.
    assert _variables(key)["mosaic-cc"] == '@((string)context.Variables["mosaic-key-cost-center"])'


def test_a_group_grant_whose_cost_center_turned_keys_off_adds_no_key_refusal() -> None:
    key = _branch(_fragment(_snapshot(_grant(1), _group_grant(2, keys_allowed=False))), _HAS_KEY)

    assert "refuse keys-off" not in _outline(key)


def test_a_token_is_validated_then_matched_to_a_grant_for_the_named_model() -> None:
    gpt, mini = _grant(1), _grant(2, model=MINI)
    fragment = _fragment(_snapshot(gpt, mini))
    token = _branch(fragment, _HAS_TOKEN)

    assert _outline(token) == [
        "refuse token-malformed",
        "validate-azure-ad-token",
        "set mosaic-caller",
        "set mosaic-client",
        "set mosaic-token-held",
        "per-model",
        "refuse cost-center",
        "refuse model",
        "refuse no-grant",
    ]
    validation = token.find("validate-azure-ad-token")
    assert validation is not None
    assert validation.attrib == {
        "tenant-id": TENANT,
        "header-name": "Authorization",
        "failed-validation-httpcode": "401",
        "failed-validation-error-message": _DENIED,
        "output-token-variable-name": "mosaic-validated-token",
    }
    assert validation.findtext("audiences/audience") == AUDIENCE
    claim = validation.find("required-claims/claim")
    assert claim is not None
    assert claim.attrib == {"name": "ver", "match": "all"}
    assert claim.findtext("value") == "2.0"
    assert _variables(token)["mosaic-token-held"] == ""

    models = _token_models(fragment)
    assert list(models) == sorted([GPT, MINI])
    for grant, other in ((gpt, mini), (mini, gpt)):
        when = models[grant.pool_model_id]
        assert _outline(when) == [
            "set mosaic-token-match",
            "set mosaic-token-grant",
            "set mosaic-token-cost-center",
            "held",
        ]
        # Each model's lookup knows only the grants to that model.
        lookup = _variables(when)["mosaic-token-match"]
        assert _match(grant) in lookup
        assert _match(other) not in lookup
        assert 'var cc = (string)context.Variables["mosaic-cc"];' in lookup
        # Whether the caller holds the model under any cost center at all, for the refusal.
        held = _variables(_branch(when, _HELD))["mosaic-token-held"]
        assert held == lookup.replace(
            'var cc = (string)context.Variables["mosaic-cc"];', 'var cc = "";'
        )
    assert (
        _refusal_condition(token, "cost-center")
        == '@((string)context.Variables["mosaic-token-held"] != "")'
    )


def test_only_a_model_with_group_grants_records_the_member_its_limits_count_per() -> None:
    fragment = _fragment(_snapshot(_grant(1), _group_grant(2, model=MINI)))
    models = _token_models(fragment)

    assert "mosaic-member" not in _variables(models[GPT])
    assert _variables(models[MINI])["mosaic-member"].startswith("@{")
    assert _outline(_branch(fragment, _HAS_TOKEN))[-4:] == [
        "refuse groups-overage",
        "refuse cost-center",
        "refuse model",
        "refuse no-grant",
    ]


def test_a_pool_with_no_grants_still_validates_tokens_and_refuses_them() -> None:
    fragment = _fragment(_snapshot())

    assert "per-model" not in _outline(_branch(fragment, _HAS_TOKEN))
    assert _key_answers(_key_lookup(fragment)) == {}
    assert "grant-limits" not in _outline(fragment)


def test_a_model_the_pool_doesnt_serve_is_refused_like_one_the_caller_doesnt_hold() -> None:
    result = _render(_snapshot(_grant(1)))
    fragment = ET.fromstring(result.fragment_xml)

    # No 404, no "no such model": a refusal never reveals which models the pool serves.
    assert NOT_FOUND_BODY not in result.fragment_xml
    assert {refusal.code for refusal in _refusals(fragment)} == {"401", "403", "503"}
    (models,) = [element for element in fragment if _describe(element) == "models"]
    assert models.find("otherwise") is None


@pytest.mark.parametrize(
    ("shape", "requested"),
    [
        (ApiShape.AZURE_OPENAI, '@(context.Request.MatchedParameters["deployment-id"])'),
        (
            ApiShape.FOUNDRY_MODELS,
            "@{ try { var body = context.Request.Body.As<JObject>(preserveContent: true); "
            'return (string)body["model"] ?? ""; } catch { return ""; } }',
        ),
        (
            ApiShape.ANTHROPIC_MESSAGES,
            "@{ try { var body = context.Request.Body.As<JObject>(preserveContent: true); "
            'return (string)body["model"] ?? ""; } catch { return ""; } }',
        ),
    ],
)
def test_the_model_a_call_names_selects_its_pool_model_and_attempts(
    shape: str, requested: str
) -> None:
    fragment = _fragment(_snapshot(_grant(1)), shape=shape)
    (models,) = [element for element in fragment if _describe(element) == "models"]

    assert _variables(fragment)["mosaic-pool-model"] == requested
    assert [(when.get("condition"), _variables(when)) for when in models.findall("when")] == [
        (
            f'@({_MODEL} == "gpt-4o")',
            {"mosaic-pool-model-id": GPT, "mosaic-pool-attempts": "@(3)"},
        ),
        (
            f'@({_MODEL} == "gpt-4o-mini")',
            {"mosaic-pool-model-id": MINI, "mosaic-pool-attempts": "@(2)"},
        ),
    ]


@pytest.mark.parametrize(
    ("shape", "operation"),
    [
        (ApiShape.AZURE_OPENAI, "chat-completions"),
        (ApiShape.FOUNDRY_MODELS, "chat-completions"),
        (ApiShape.ANTHROPIC_MESSAGES, "messages"),
    ],
)
def test_governed_access_permits_only_the_operation_its_limits_can_count(
    shape: str, operation: str
) -> None:
    fragment = _fragment(_snapshot(_grant(1)), shape=shape)

    assert [found.name for found in governed_pool_operations(shape)] == [operation]
    assert _refusal_condition(fragment, "operation") == (
        '@(context.Operation == null || context.Request.Method != "POST" || '
        f'!(context.Operation.Id == "{operation}"))'
    )


def test_a_shape_with_no_countable_operation_cant_be_governed() -> None:
    with pytest.raises(
        ValidationError,
        match=re.escape("This API shape has no operations governed pool access supports."),
    ):
        governed_pool_operations("bogus")


def _calendar(namespace: str, identity: str, period: str, length: int, suffix: str = "") -> str:
    return (
        "@{\nvar now = DateTime.UtcNow;\n"
        f'return "mosaic:pool:{namespace}:{identity}:{period}:" + '
        f'now.ToString("u").Substring(0, {length}){suffix};\n}}'
    )


def _top(fragment: ET.Element, kind: str) -> ET.Element:
    (element,) = [element for element in fragment if _describe(element) == kind]
    return element


def test_a_grants_own_limits_count_per_stable_grant_and_per_member_for_a_group() -> None:
    direct = _grant(
        1,
        enforcement=_limits(
            tokens={
                "tokens_per_minute": 1_000,
                "token_quota": 50_000,
                "token_quota_period": "Monthly",
            },
            requests={
                "calls": 10,
                "renewal_period_seconds": 60,
                "call_quota": 500,
                "call_quota_period": "Daily",
            },
        ),
    )
    group = _group_grant(
        2,
        model=MINI,
        enforcement=_limits(
            tokens={"tokens_per_minute": 2_000, "estimate_prompt_tokens": False},
            requests={"calls": 5, "renewal_period_seconds": 30},
        ),
    )
    fragment = _fragment(_snapshot(direct, group, _grant(3)))
    direct_id, group_id = _grant_id(direct), _grant_id(group)
    member = ' + (string)context.Variables["mosaic-member"]'

    # One when per limited grant, holding all its limits: requests first, then tokens.
    when_direct, when_group = _top(fragment, "grant-limits").findall("when")
    assert when_direct.get("condition") == f'@({_GRANT} == "{direct_id}")'
    assert [element.attrib for element in when_direct] == [
        {
            "calls": "10",
            "renewal-period": "60",
            "counter-key": f"mosaic:pool:grant-request-rate:{direct_id}",
            "remaining-calls-header-name": "x-mosaic-remaining-calls",
        },
        {
            "calls": "500",
            "renewal-period": "0",
            "counter-key": _calendar("grant-request-quota", direct_id, "Daily", 10),
        },
        {
            "counter-key": f"mosaic:pool:grant-tokens:{direct_id}",
            "estimate-prompt-tokens": "true",
            "tokens-per-minute": "1000",
            "token-quota": "50000",
            "token-quota-period": "Monthly",
            "remaining-tokens-header-name": "x-mosaic-remaining-tokens",
            "remaining-quota-tokens-header-name": "x-mosaic-remaining-quota-tokens",
        },
    ]
    # A group grant's limits apply to each of its members separately.
    assert when_group.get("condition") == f'@({_GRANT} == "{group_id}")'
    assert [element.attrib for element in when_group] == [
        {
            "calls": "5",
            "renewal-period": "30",
            "counter-key": f'@("mosaic:pool:grant-request-rate:{group_id}:"{member})',
            "remaining-calls-header-name": "x-mosaic-remaining-calls",
        },
        {
            "counter-key": f'@("mosaic:pool:grant-tokens:{group_id}:"{member})',
            "estimate-prompt-tokens": "false",
            "tokens-per-minute": "2000",
            "remaining-tokens-header-name": "x-mosaic-remaining-tokens",
        },
    ]


def test_each_pool_model_counts_its_cost_centers_quotas_then_its_safeguard_then_meters() -> None:
    grants = [_grant(1), _grant(2, cost_center=RESEARCH), _grant(3, model=MINI, cost_center=SALES)]
    quotas = [
        _quota(GPT, RESEARCH, monthly_tokens=1_000_000, monthly_calls=10_000),
        _quota(GPT, GENERAL, monthly_calls=500),
        _quota(MINI, SALES, monthly_tokens=2_000),
    ]
    safeguard = PoolSafeguard(
        tokens_per_minute=10_000, token_quota=5_000_000, token_quota_period="Monthly"
    )
    result = _render(_snapshot(*grants, quotas=quotas), safeguard=safeguard)
    fragment = ET.fromstring(result.fragment_xml)
    gpt, mini = _top(fragment, "per-model").findall("when")

    assert gpt.get("condition") == f'@({_MODEL_ID} == "{GPT}")'
    assert mini.get("condition") == f'@({_MODEL_ID} == "{MINI}")'
    for when, model, public in ((gpt, GPT, "gpt-4o"), (mini, MINI, "gpt-4o-mini")):
        assert [element.tag for element in when] == [
            "choose",
            "llm-token-limit",
            "llm-emit-token-metric",
        ]
        # The safeguard counts last, per pool model, whoever calls.
        assert when[1].attrib == {
            "counter-key": model,
            "estimate-prompt-tokens": "false",
            "tokens-per-minute": "10000",
            "token-quota": "5000000",
            "token-quota-period": "Monthly",
        }
        assert when[1].attrib == pool_policy._safeguard_attributes(model, safeguard)
        assert when[2].attrib == {"namespace": METRIC_NAMESPACE}
        assert [dimension.attrib for dimension in when[2]] == [
            {"name": "Pool", "value": f'@("{POOL_ID}")'},
            {"name": "Model", "value": f'@("{public}")'},
        ]

    # Each cost center's quota on a model is shared by every grant charging it on that model.
    general, research = gpt[0].findall("when")
    cost_center = '@((string)context.Variables["mosaic-cost-center"] == '
    assert general.get("condition") == f'{cost_center}"general")'
    assert research.get("condition") == f'{cost_center}"research")'
    assert [element.attrib for element in general] == [
        {
            "calls": "500",
            "renewal-period": "0",
            "counter-key": _calendar(
                "cost-center-request-quota", _cost_center_identity(GPT, GENERAL), "Monthly", 7
            ),
        }
    ]
    research_id = _cost_center_identity(GPT, RESEARCH)
    assert [element.attrib for element in research] == [
        {
            "counter-key": f"mosaic:pool:cost-center-tokens:{research_id}",
            "estimate-prompt-tokens": "false",
            "token-quota": "1000000",
            "token-quota-period": "Monthly",
            "remaining-quota-tokens-header-name": "x-mosaic-cost-center-remaining-quota-tokens",
        },
        {
            "calls": "10000",
            "renewal-period": "0",
            "counter-key": _calendar("cost-center-request-quota", research_id, "Monthly", 7),
        },
    ]
    (sales,) = mini[0].findall("when")
    assert sales.get("condition") == f'{cost_center}"sales")'
    assert [element.get("counter-key") for element in sales] == [
        f"mosaic:pool:cost-center-tokens:{_cost_center_identity(MINI, SALES)}"
    ]
    # A pool's counters never share a namespace with a publication's.
    assert "mosaic:governed:" not in result.fragment_xml


def test_a_cost_centers_quota_compiles_only_where_a_grant_on_the_model_charges_it() -> None:
    grants = [
        _grant(1),
        _grant(2, cost_center=RESEARCH, enabled=False),
        _grant(3, model=MINI, cost_center=SALES),
    ]
    quotas = [
        _quota(GPT, RESEARCH, monthly_calls=1),
        _quota(GPT, SALES, monthly_calls=2),
        _quota(MINI, SALES, monthly_calls=3),
    ]
    gpt, mini = _top(_fragment(_snapshot(*grants, quotas=quotas)), "per-model").findall("when")

    assert [element.tag for element in gpt] == ["llm-emit-token-metric"]
    pooled = mini.find("choose")
    assert pooled is not None
    assert [element.get("calls") for when in pooled for element in when] == ["3"]


def test_an_unmetered_pool_counts_calls_but_never_tokens() -> None:
    grant = _grant(1, enforcement=_limits(requests={"calls": 10, "renewal_period_seconds": 60}))
    fragment = _fragment(
        _snapshot(grant, token_metering=False, quotas=[_quota(GPT, GENERAL, monthly_calls=100)])
    )
    tags = {element.tag for element in fragment.iter()}

    assert {"rate-limit-by-key", "quota-by-key"} <= tags
    assert not {"llm-token-limit", "llm-emit-token-metric"} & tags
    # With nothing to count, there's no per-model step at all.
    assert "per-model" not in _outline(_fragment(_snapshot(_grant(1), token_metering=False)))


def test_a_metered_pool_without_limits_still_meters_each_models_tokens() -> None:
    choose = _top(_fragment(_snapshot(_grant(1))), "per-model")

    assert [[element.tag for element in when] for when in choose] == [
        ["llm-emit-token-metric"],
        ["llm-emit-token-metric"],
    ]


def test_an_azure_openai_pool_strips_the_route_model_before_forwarding() -> None:
    fragment = _fragment(_snapshot(_grant(1)))
    (operation,) = governed_pool_operations(ApiShape.AZURE_OPENAI)
    (when,) = _top(fragment, "rewrites").findall("when")
    identity = fragment.find("authentication-managed-identity")

    assert identity is not None
    assert identity.get("resource") == managed_identity_resource(ApiShape.AZURE_OPENAI)
    assert [element.attrib for element in when] == [
        {
            "template": operation.url_template.removeprefix("/openai/deployments/{deployment-id}"),
            "copy-unmatched-params": "true",
        }
    ]
    assert when[0].get("template") == "/chat/completions"


@pytest.mark.parametrize("shape", _SHAPES)
@pytest.mark.parametrize(
    ("keys", "entra"), [(True, False), (False, True), (True, True)], ids=["keys", "entra", "both"]
)
def test_every_expression_parses_as_api_management_requires(
    shape: str, keys: bool, entra: bool
) -> None:
    grants = [
        _limited_grant(1),
        _grant(
            2,
            model=MINI,
            cost_center=RESEARCH,
            keys_allowed=False,
            enforcement=_limits(requests={"call_quota": 100, "call_quota_period": "Weekly"}),
        ),
    ]
    if entra:
        grants.append(
            _group_grant(
                3,
                model=MINI,
                cost_center=SALES,
                enforcement=_limits(tokens={"token_quota": 1_000, "token_quota_period": "Daily"}),
            )
        )
    result = _render(
        _snapshot(
            *grants,
            keys=keys,
            entra=entra,
            quotas=[_quota(GPT, GENERAL, monthly_tokens=10, monthly_calls=10)],
        ),
        shape=shape,
        safeguard=PoolSafeguard(tokens_per_minute=100),
    )

    for document in (result.fragment_xml, result.api_policy_xml):
        assert policy_expression_error(document) is None
        for element in ET.fromstring(document).iter():
            for value in [*element.attrib.values(), element.text or ""]:
                assert expression_error(value) is None, value


_UNROUTED = pool_model_id(POOL_ID, "o3")


def _metering_off(*grants: PoolAccessGrant, **overrides: object) -> PoolAccessSnapshot:
    return _snapshot(*grants, token_metering=False, **overrides)


def _route_to(backend: str, model_id: str = GPT) -> list[PoolRoute]:
    return [PoolRoute(model_id, "gpt-4o", (PoolTarget(backend, "gpt-4o"),), 3)]


def _custom_counter() -> EntitlementEnforcement:
    return EntitlementEnforcement(
        requests=RequestEnforcement(
            counter_key_expression="@(context.Request.IpAddress)",
            calls=1,
            renewal_period_seconds=60,
        )
    )


def _mosaic_group_grant() -> PoolAccessSnapshot:
    """A MOSAIC group's grant, which validation keeps out of a snapshot, forced into one."""

    grant = _grant(1).model_copy(
        update={"subject": EntitlementSubject(kind=EntitlementSubjectKind.GROUP, id="group-1")}
    )
    return _snapshot().model_copy(update={"grants": [grant]})


def _uncoded_quota() -> PoolModelQuota:
    return PoolModelQuota(
        pool_model_id=GPT, cost_center_id=GENERAL, cost_center_code="", monthly_calls=1
    )


_LITERAL = "Governed pool policies require literal APIM backend, fragment, and model names."
_AUDIENCE = "Governed Entra access requires the runtime application's GUID audience."
_AMBIGUOUS = "Governed pool grants must have nonempty, unambiguous identities."
_WRONG_KEY = (
    "A governed pool grant's key must be its subject's key to the pool under the grant's cost "
    "center."
)
_CODE_CONFLICT = (
    "Each cost center needs exactly one code, and no two may share one, so the header can tell "
    "them apart."
)
_ONE_QUOTA = "Each cost center has at most one pooled quota per pool model."

_INVALID: list[Any] = [
    pytest.param(
        lambda: _render(_snapshot(_grant(1)), routes=_route_to("{{backend}}")),
        _LITERAL,
        id="templated-backend",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1)), routes=_route_to(GPT_BACKEND_POOL, "{{model}}")),
        _LITERAL,
        id="templated-model",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1)), pool=_pool(tenant_id="contoso")),
        "Governed Entra access requires a specific tenant GUID.",
        id="tenant",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), audience=None)),
        _AUDIENCE,
        id="no-audience",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), audience="api://mosaic-runtime")),
        _AUDIENCE,
        id="audience",
    ),
    pytest.param(
        lambda: _render(_metering_off(_grant(1)), safeguard=PoolSafeguard(tokens_per_minute=1)),
        "This pool can't be token-metered on its gateway's tier, so it can't have a safeguard.",
        id="unmetered-safeguard",
    ),
    pytest.param(
        lambda: _render(_mosaic_group_grant()),
        "Governed pools support only user, application and security-group grants.",
        id="mosaic-group",
    ),
    pytest.param(
        lambda: _render(_snapshot(_group_grant(1), entra=False)),
        "Security-group grants require governed Entra access.",
        id="group-without-entra",
    ),
    pytest.param(
        lambda: _render(_snapshot(_group_grant(1, object_id="engineering"))),
        "Security-group grants require a GUID object ID.",
        id="group-object",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, entitlement_id=" "))),
        _AMBIGUOUS,
        id="blank-entitlement",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, object_id=""))),
        _AMBIGUOUS,
        id="blank-object",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), _grant(1, model=MINI))),
        _AMBIGUOUS,
        id="one-entitlement-twice",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), _grant(2, person=1))),
        _AMBIGUOUS,
        id="one-object-twice",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), _grant(2, person=1, object_id=_object_id(9)))),
        _AMBIGUOUS,
        id="one-subject-twice",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, key_name=_key(2)))),
        _WRONG_KEY,
        id="another-subjects-key",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, key_name=_key(1, RESEARCH)))),
        _WRONG_KEY,
        id="another-cost-centers-key",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), _grant(2, key_name=_key(3), enabled=False))),
        _WRONG_KEY,
        id="disabled-grants-key",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1)), pool=_pool(subscription_name=_key(1))),
        "A governed grant cannot use the all-access or pool bootstrap subscription.",
        id="bootstrap-key",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, model=_UNROUTED))),
        "A governed pool grant names a model the pool doesn't serve.",
        id="unrouted-model",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, cost_center_code="R&D"))),
        "A cost center's code must be 1 to 64 letters, digits, . - or _.",
        id="code-pattern",
    ),
    pytest.param(
        lambda: _render(
            _snapshot(_grant(1), _grant(2, cost_center=RESEARCH, cost_center_code="GENERAL"))
        ),
        _CODE_CONFLICT,
        id="shared-code",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), _grant(2, cost_center_code="general-2"))),
        _CODE_CONFLICT,
        id="two-codes",
    ),
    pytest.param(
        lambda: _render(
            _snapshot(
                _grant(1),
                quotas=[
                    PoolModelQuota(
                        pool_model_id=GPT,
                        cost_center_id=RESEARCH,
                        cost_center_code="General",
                        monthly_calls=1,
                    )
                ],
            )
        ),
        _CODE_CONFLICT,
        id="quota-shares-a-code",
    ),
    pytest.param(
        lambda: _render(_metering_off(_limited_grant(1))),
        "This pool can't be token-metered on its gateway's tier, so its grants can't carry "
        "token limits.",
        id="unmetered-grant-tokens",
    ),
    pytest.param(
        lambda: _render(
            _snapshot(
                _grant(1, enforcement=_limits(requests={"calls": 1, "renewal_period_seconds": 301}))
            )
        ),
        "Governed request rate renewal must not exceed 300 seconds.",
        id="long-renewal",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1, enforcement=_custom_counter()))),
        "Governed access replaces the standard subscription counter with a stable grant "
        "counter; custom counter expressions are not supported.",
        id="custom-counter",
    ),
    pytest.param(
        lambda: _render(_snapshot(_grant(1), quotas=[_uncoded_quota()])),
        _ONE_QUOTA,
        id="uncoded-quota",
    ),
    pytest.param(
        lambda: _render(
            _snapshot(
                _grant(1),
                quotas=[
                    _quota(GPT, GENERAL, monthly_calls=1),
                    _quota(GPT, GENERAL, monthly_tokens=1),
                ],
            )
        ),
        _ONE_QUOTA,
        id="two-quotas",
    ),
    pytest.param(
        lambda: _render(_metering_off(_grant(1), quotas=[_quota(GPT, GENERAL, monthly_tokens=1)])),
        "This pool can't be token-metered on its gateway's tier, so a cost center's quota on it "
        "must count calls, not tokens.",
        id="unmetered-quota-tokens",
    ),
]


@pytest.mark.parametrize(("render", "message"), _INVALID)
def test_unsafe_or_ambiguous_intent_is_refused_before_anything_compiles(
    render: Callable[[], PublicationPolicy], message: str
) -> None:
    with pytest.raises(ValidationError, match=re.escape(message)):
        render()


def test_a_pool_with_no_models_has_no_policy_to_write() -> None:
    with pytest.raises(ValueError, match="A pool policy needs at least one model"):
        _render(_snapshot(_grant(1)), routes=[])


def test_a_disabled_grant_is_carried_without_being_checked_as_if_it_compiled() -> None:
    """A disabled grant left by an earlier apply mustn't stop the plan that removes it."""

    retired = _grant(
        2,
        model=_UNROUTED,
        cost_center_code="R&D",
        enabled=False,
        enforcement=EntitlementEnforcement(
            tokens=TokenEnforcement(
                counter_key_expression="@(context.Request.IpAddress)", tokens_per_minute=1
            ),
            requests=RequestEnforcement(
                counter_key_expression=SUBSCRIPTION_COUNTER, calls=1, renewal_period_seconds=3600
            ),
        ),
    )
    fragment_xml = _render(_metering_off(_grant(1), retired)).fragment_xml

    assert _key(1) in fragment_xml
    assert _key(2) not in fragment_xml
    assert _grant_id(retired) not in fragment_xml


def test_a_grant_without_a_code_compiles_though_no_header_can_select_it() -> None:
    grant = _grant(1, cost_center_code="")

    assert _key_answers(_key_lookup(_fragment(_snapshot(grant)))) == {
        _key(1): ({GPT: f"{_grant_id(grant)}|"}, "~")
    }


def test_a_call_is_held_to_its_cost_centers_budget_under_the_key_the_gateways_list_uses() -> None:
    """Pool calls share the gateway's blocked list with publications' calls (ADR 0023)."""

    fragment = _fragment(
        _snapshot(
            _grant(1),
            _grant(2, cost_center=RESEARCH),
            _group_grant(3, model=MINI, cost_center=SALES),
            _grant(4, cost_center_code="retired", enabled=False),
            _grant(5, cost_center=RESEARCH, cost_center_code=""),
        )
    )

    assert _variables(fragment)["mosaic-budget-key"] == "\n".join(
        [
            "@{",
            'var code = (string)context.Variables["mosaic-cost-center"];',
            f'if (code == "general") {{ return "{budget_key(GENERAL)}"; }}',
            f'if (code == "research") {{ return "{budget_key(RESEARCH)}"; }}',
            f'if (code == "sales") {{ return "{budget_key(SALES)}"; }}',
            'return "";',
            "}",
        ]
    )


def test_a_fragment_at_api_managements_size_limit_compiles_and_one_byte_more_doesnt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot(_limited_grant(1), _grant(2, model=MINI))
    size = len(_render(snapshot).fragment_xml.encode("utf-8"))

    monkeypatch.setattr(pool_policy, "MAX_FRAGMENT_BYTES", size)
    _render(snapshot)
    monkeypatch.setattr(pool_policy, "MAX_FRAGMENT_BYTES", size - 1)
    with pytest.raises(
        ValidationError,
        match=re.escape(
            "The governed pool policy fragment exceeds APIM's 512 KB UTF-8 size limit."
        ),
    ):
        _render(snapshot)


def test_facets_explain_the_policy_without_naming_who_holds_what_or_where_it_runs() -> None:
    grants = [
        _limited_grant(1),
        _group_grant(
            2,
            model=MINI,
            cost_center=SALES,
            enforcement=_limits(
                tokens={"token_quota": 1_000, "token_quota_period": "Daily"},
                requests={"call_quota": 5, "call_quota_period": "Weekly"},
            ),
        ),
        _grant(3, cost_center=RESEARCH),
    ]
    result = _render(
        _snapshot(*grants, quotas=[_quota(GPT, RESEARCH, monthly_tokens=10, monthly_calls=10)]),
        safeguard=PoolSafeguard(tokens_per_minute=100),
    )
    facets = result.facets
    hidden = [
        POOL_ID,
        GPT,
        MINI,
        API_NAME,
        GPT_BACKEND_POOL,
        *MINI_MEMBERS,
        _cost_center_identity(GPT, RESEARCH),
        *(grant.key_name for grant in grants if grant.key_name),
        *(grant.display_name for grant in grants),
        *(grant.object_id for grant in grants),
        *(_grant_id(grant) for grant in grants),
    ]
    text = json.dumps([facet.model_dump(mode="json") for facet in facets])

    for value in hidden:
        assert value not in text, value
    for facet in facets:
        for value in [facet.summary, *facet.details, *facet.attributes.values()]:
            assert not any(markup in value for markup in ("<", "@(", "@{")), value
        assert facet.managed_by_mosaic
        assert facet.section != PolicySection.UNKNOWN
    assert result.unrecognized_elements == []
    assert {
        json.dumps(facet.attributes)
        for facet in facets
        if facet.element in {"set-backend-service", "include-fragment"}
    } == {'{"backend-id": "[redacted]"}', '{"fragment-id": "[redacted]"}'}

    # Each limit says what it's counted on, in the order the gateway applies them.
    limits = [
        facet
        for facet in facets
        if facet.element in {"rate-limit-by-key", "quota-by-key", "llm-token-limit"}
    ]
    endings = {
        "stable-grant": "counted per stable tenant/pool/entitlement grant.",
        "cost-center-pool": "counted per cost center on this pool model.",
        "pool-model": "counted per pool model, shared by every caller.",
    }
    assert [facet.attributes["counter-scope"] for facet in limits] == [
        *["stable-grant"] * 4,
        *["cost-center-pool"] * 2,
        *["pool-model"] * 2,
    ]
    for facet in limits:
        assert facet.summary.endswith(endings[facet.attributes["counter-scope"]]), facet.summary

    authorization = facets[0]
    assert authorization.summary == (
        "Requires an allowlisted APIM subscription key or a validated Microsoft Entra bearer token."
    )
    assert authorization.attributes == {
        "keys-enabled": "true",
        "entra-enabled": "true",
        "enabled-grants": "3",
        "security-group-grants": "1",
        "cost-centers": "3",
    }


def test_a_governed_pool_records_each_attempt_apart_from_who_was_let_in() -> None:
    result = _render(_snapshot(_grant(1), _group_grant(2, model=MINI)))

    traces = [facet for facet in result.facets if facet.element == "trace"]
    attempt = [facet for facet in traces if facet.attributes == {"trace": "attempt"}]
    assert len(attempt) == 1
    assert attempt[0].summary == ATTEMPT_TRACE_SUMMARY
    assert attempt[0].section == PolicySection.BACKEND
    assert attempt[0].managed_by_mosaic
    # The fragment's traces keep their own words: one for refusals and one for attribution.
    assert {facet.attributes.get("trace") for facet in traces} == {"denial", "attempt", None}
    assert sum(facet.summary == ATTEMPT_TRACE_SUMMARY for facet in traces) == 1

    api_policy = ET.fromstring(result.api_policy_xml)
    retry = api_policy.find("backend/retry")
    assert retry is not None
    assert [child.tag for child in retry][-2:] == ["forward-request", "trace"]
    assert (retry.findtext("trace/message") or "").startswith(
        f'@("{pool_policy.ATTEMPT_TRACE_PREFIX} m=" + '
    )


@pytest.mark.parametrize(
    ("keys", "entra", "summary"),
    [
        (True, False, "Requires an allowlisted APIM subscription key."),
        (False, True, "Requires a validated Microsoft Entra bearer token."),
        (False, False, "All model access is denied."),
    ],
    ids=["keys", "entra", "off"],
)
def test_the_authorization_facet_names_only_the_methods_access_allows(
    keys: bool, entra: bool, summary: str
) -> None:
    authorization = _render(
        _snapshot(_grant(1), _grant(2, enabled=False), keys=keys, entra=entra)
    ).facets[0]

    assert authorization.summary == summary
    assert authorization.attributes == {
        "keys-enabled": str(keys).lower(),
        "entra-enabled": str(entra).lower(),
        "enabled-grants": "1",
        "cost-centers": "1",
    }


@pytest.mark.parametrize("shape", _SHAPES)
def test_the_operations_facet_names_the_one_operation_governed_access_permits(shape: str) -> None:
    operations = _render(_snapshot(_grant(1)), shape=shape).facets[1]
    (allowed,) = governed_pool_operations(shape)
    refused = [
        operation.name for operation in pool_operations(shape) if operation.name != allowed.name
    ]

    assert refused
    assert operations.summary == (
        "Governed access permits only the Anthropic Messages operation."
        if shape == ApiShape.ANTHROPIC_MESSAGES
        else "Governed access permits only chat completions."
    )
    assert operations.attributes == {"allowed-operations": allowed.name}
    assert operations.details[:2] == [
        f"Allowed curated operation IDs: {allowed.name}.",
        f"The pool's other operations are denied: {', '.join(refused)}.",
    ]
