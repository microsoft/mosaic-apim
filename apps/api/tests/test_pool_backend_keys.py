"""Pool members reached with an API key the gateway reads from Key Vault (ADR 0024, phase 3).

An API Management backend pool can't hold a backend that needs its own credentials, so a key
member is its own target: the gateway tries it after the backend pool, sending a Key Vault-backed
named value as the key, and the caller's own credentials never reach any member. These drive the
real pool service and writer against the API Management double, which, like API Management,
refuses a policy that names a missing named value and a delete of a named value a policy names.
"""

from xml.etree.ElementTree import Element

import pytest
from key_vault_double import SECRET_URI
from mosaic_api.budgets import BLOCKED_COST_CENTERS_NAMED_VALUE
from mosaic_api.domain import (
    ApiShape,
    CredentialReference,
    DeclaredDeployment,
    EndpointAuthMode,
    GatewayRuntimeAccess,
    ModelAccessSettings,
    ModelProvider,
    PublicationStatus,
    PublishedResourceKind,
    PublishRunStatus,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
)
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.backend_keys import backend_key_name
from mosaic_api.integrations.pool_policy import (
    ATTEMPT_VARIABLE,
    ATTEMPTS_VARIABLE,
    BALANCED_VARIABLE,
    MODEL_ID_VARIABLE,
    TOKEN_VARIABLE,
)
from mosaic_api.model_pools import ModelPool
from test_key_publishing import forwarded
from test_model_pools import (
    ACTOR,
    TENANT,
    Estate,
    _audit,
    _deployment,
    _gpt4o,
    _member,
    _model,
    _policy,
    _steps,
    _xml,
)
from test_pool_governed import GovernedEstate, _staged

SECOND_SECRET_URI = "https://kv-contoso-ai.vault.azure.net/secrets/aoai-key-2"
ROTATED_SECRET = "https://kv-contoso-ai.vault.azure.net/secrets/rotated"
ATTEMPT = f'context.Variables.GetValueOrDefault<int>("{ATTEMPT_VARIABLE}", 1)'
BEARER = f'@("Bearer " + context.Variables.GetValueOrDefault<string>("{TOKEN_VARIABLE}", ""))'
NO_KEYED_WARNING = "which an API Management backend pool can't hold"

Placement = tuple[str | None, str | None, list[tuple[str | None, str | None, str | None]]]


async def _key_secret(estate: Estate, endpoint: str, uri: str) -> None:
    """Point an endpoint at the Key Vault secret its API key is in."""

    credential = CredentialReference(
        id=f"cred-{endpoint}", tenant_id=TENANT, name=f"{endpoint} key", secret_uri=uri
    )
    await estate.endpoint_repository.save_credential(credential, _audit())
    await estate.update_endpoint(endpoint, credential_reference_id=credential.id)


async def _second_key(estate: Estate) -> None:
    await estate.add_endpoint(
        "aoai-key-2",
        provider=ModelProvider.AZURE_OPENAI,
        location="northeurope",
        auth_mode=EndpointAuthMode.API_KEY,
        deployments=[],
        declared=[
            DeclaredDeployment(
                deployment_name="gpt-4o",
                model_name="gpt-4o",
                model_version="2024-08-06",
                api_shape=ApiShape.AZURE_OPENAI,
            )
        ],
    )
    await _key_secret(estate, "aoai-key-2", SECOND_SECRET_URI)


@pytest.fixture
async def keyed() -> Estate:
    """The pool estate, with aoai-key's API key in Key Vault."""

    built = Estate()
    await built.setup()
    await _key_secret(built, "aoai-key", SECRET_URI)
    return built


@pytest.fixture
async def unkeyed() -> Estate:
    """The pool estate, before anyone has said where aoai-key's API key is."""

    built = Estate()
    await built.setup()
    return built


def _route(policy: Element, model_id: str) -> Element:
    """The ``when`` the API policy routes one model's requests through."""

    [route] = [
        when
        for when in policy.iter("when")
        if MODEL_ID_VARIABLE in (when.get("condition") or "")
        and model_id in (when.get("condition") or "")
    ]
    return route


def _placements(route: Element) -> list[Placement]:
    """Each target a route sends an attempt to: its condition, backend, and header changes."""

    if route.find("set-backend-service") is not None:
        parents: list[tuple[str | None, Element]] = [(None, route)]
    else:
        parents = [(when.get("condition"), when) for when in route.findall("choose/when")]
    placements: list[Placement] = []
    for condition, parent in parents:
        backend = parent.find("set-backend-service")
        assert backend is not None
        headers = [
            (header.get("name"), header.get("exists-action"), header.findtext("value"))
            for header in parent.findall("set-header")
        ]
        placements.append((condition, backend.get("backend-id"), headers))
    return placements


def _variables(fragment: Element) -> dict[str | None, str | None]:
    return {item.get("name"): item.get("value") for item in fragment.iter("set-variable")}


def _fragment(estate: Estate, pool: ModelPool) -> str:
    return _policy(estate.apim, f"policyFragments/{pool.fragment_name}")


def _api_policy(estate: Estate, pool: ModelPool) -> Element:
    return _xml(_policy(estate.apim, f"apis/{pool.api_name}/policies/policy"))


def _keyed_problem(message: str) -> str:
    return f"gpt-4o on aoai-key: {message}"


async def _runtime_access(estate: Estate, *entries: GatewayRuntimeAccess) -> None:
    await estate.update_endpoint("aoai-key", runtime_access=list(entries))


# -- a pool that mixes identity and key members ---------------------------------------------------


async def test_a_key_member_is_tried_after_the_backend_pool_with_its_own_key(
    keyed: Estate,
) -> None:
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden", "aoai-key"))
    model = pool.models[0]
    east, sweden, key = model.members
    key_name = backend_key_name(key.backend_name)
    assert key_name == f"{key.backend_name}-key"

    plan = await keyed.service.plan(ACTOR, pool.id)

    # Every named value exists before anything that could name it, and the backend pool holds
    # only the members the gateway reaches with its managed identity.
    assert _steps(plan)[:7] == [
        ("namedValue", key_name, "create"),
        ("backend", east.backend_name, "create"),
        ("backend", sweden.backend_name, "create"),
        ("backend", key.backend_name, "create"),
        ("backendPool", model.backend_pool_name, "create"),
        ("policyFragment", pool.fragment_name, "create"),
        ("api", pool.api_name, "create"),
    ]
    mixed = (
        "1 of gpt-4o's 3 active deployments is reached with an API key, which an API Management "
        "backend pool can't hold. The gateway tries it once, after the backend pool's attempts, "
        "with no circuit breaker."
    )
    assert mixed in plan.warnings

    run = await keyed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors

    written = keyed.apim.written
    named_value = written[f"namedValues/{key_name}"]["properties"]
    assert named_value["secret"] is True
    assert named_value["keyVault"]["secretIdentifier"] == SECRET_URI
    # MOSAIC never holds the key: API Management reads it from Key Vault.
    assert "value" not in named_value
    assert keyed.apim.list_value_calls == []
    puts = keyed.apim.write_paths("PUT")
    assert puts.index(f"namedValues/{key_name}") < puts.index(
        f"apis/{pool.api_name}/policies/policy"
    )
    assert keyed.apim.dangling_references == []

    key_backend = written[f"backends/{key.backend_name}"]["properties"]
    assert key_backend["url"] == "https://aoai-key.openai.azure.com/openai/deployments/gpt-4o"
    assert "circuitBreaker" not in key_backend
    assert "circuitBreaker" in written[f"backends/{east.backend_name}"]["properties"]
    services = written[f"backends/{model.backend_pool_name}"]["properties"]["pool"]["services"]
    assert [item["id"].rsplit("/", 1)[-1] for item in services] == [
        east.backend_name,
        sweden.backend_name,
    ]

    # Two attempts on the backend pool, which balances them, then one on the key member.
    fragment = _fragment(keyed, pool)
    variables = _variables(_xml(fragment))
    assert variables[ATTEMPTS_VARIABLE] == "@(3)"
    assert variables[BALANCED_VARIABLE] == "@(2)"
    [identity] = _xml(fragment).iter("authentication-managed-identity")
    assert identity.get("output-token-variable-name") == TOKEN_VARIABLE

    policy = _api_policy(keyed, pool)
    retry = policy.find("backend/retry")
    assert retry is not None
    assert retry.get("count") == "2"
    assert _placements(_route(policy, model.id)) == [
        (
            f"@({ATTEMPT} <= 2)",
            model.backend_pool_name,
            [("Authorization", "override", BEARER), ("api-key", "delete", None)],
        ),
        (
            f"@({ATTEMPT} == 3)",
            key.backend_name,
            [("Authorization", "delete", None), ("api-key", "override", f"{{{{{key_name}}}}}")],
        ),
    ]

    # Whatever the caller sent, none of it reaches a member.
    headers, removed_query = forwarded(fragment, {})
    assert {"api-key", "x-api-key", "authorization", "ocp-apim-subscription-key"}.isdisjoint(
        headers
    )
    assert {"subscription-key", "api-key"} <= removed_query

    detail = await keyed.service.detail(ACTOR, pool.id)
    assert mixed in detail.warnings
    views = {member.endpoint_name: member for member in detail.models[0].members}
    assert (views["aoai-key"].order, views["aoai-key"].priority) == (1, None)
    assert views["aoai-key"].api_key
    assert views["aoai-key"].declared
    assert not views["aoai-key"].observed
    for name in ("aoai-east", "aoai-sweden"):
        assert (views[name].order, views[name].priority) == (None, 1)
        assert not views[name].api_key
        assert views[name].observed

    published = await keyed.pool(pool.id)
    [recorded] = [
        item for item in published.resources if item.kind == PublishedResourceKind.NAMED_VALUE
    ]
    assert recorded.name == key_name
    assert recorded.created_by_mosaic


async def test_a_pool_of_key_members_tries_each_in_turn_without_a_backend_pool(
    keyed: Estate,
) -> None:
    await _second_key(keyed)
    pool = await keyed.create("OpenAI", _gpt4o("aoai-key", "aoai-key-2"))
    model = pool.models[0]
    first, second = model.members

    plan = await keyed.service.plan(ACTOR, pool.id)

    kinds = [kind for kind, *_ in _steps(plan)]
    assert "backendPool" not in kinds
    assert [name for kind, name, _ in _steps(plan) if kind == "namedValue"] == [
        backend_key_name(first.backend_name),
        backend_key_name(second.backend_name),
    ]
    assert (
        "All 2 of gpt-4o's active deployments are reached with an API key, which an API "
        "Management backend pool can't hold. The gateway tries each once, in order, with no "
        "circuit breaker."
    ) in plan.warnings

    run = await keyed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert keyed.apim.dangling_references == []

    fragment = _xml(_fragment(keyed, pool))
    # No member uses the gateway's identity, so the fragment doesn't ask for a token.
    assert list(fragment.iter("authentication-managed-identity")) == []
    variables = _variables(fragment)
    assert variables[ATTEMPTS_VARIABLE] == "@(2)"
    assert BALANCED_VARIABLE not in variables

    policy = _api_policy(keyed, pool)
    retry = policy.find("backend/retry")
    assert retry is not None
    assert retry.get("count") == "1"
    assert _placements(_route(policy, model.id)) == [
        (
            f"@({ATTEMPT} == {attempt})",
            member.backend_name,
            [("api-key", "override", f"{{{{{backend_key_name(member.backend_name)}}}}}")],
        )
        for attempt, member in enumerate(model.members, start=1)
    ]


async def test_a_single_key_member_is_tried_once(keyed: Estate) -> None:
    pool = await keyed.create("OpenAI", _gpt4o("aoai-key"))
    model = pool.models[0]
    [key] = model.members

    plan = await keyed.service.plan(ACTOR, pool.id)
    assert (
        "gpt-4o's only active deployment is reached with an API key, which an API Management "
        "backend pool can't hold. The gateway tries it once, with no circuit breaker."
    ) in plan.warnings
    run = await keyed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors

    policy = _api_policy(keyed, pool)
    assert policy.find("backend/retry") is None
    assert _placements(_route(policy, model.id)) == [
        (
            None,
            key.backend_name,
            [("api-key", "override", f"{{{{{backend_key_name(key.backend_name)}}}}}")],
        )
    ]


async def test_a_linear_pool_keeps_a_key_member_in_its_place(keyed: Estate) -> None:
    pool = await keyed.create(
        "OpenAI", _gpt4o("aoai-key", "aoai-east", "aoai-sweden"), pool_type="linear"
    )
    model = pool.models[0]
    key, east, sweden = model.members

    plan = await keyed.service.plan(ACTOR, pool.id)
    # A linear pool has no backend pool, so a key member costs it nothing.
    assert not any(NO_KEYED_WARNING in warning for warning in plan.warnings)
    run = await keyed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors

    for member in model.members:
        assert "circuitBreaker" not in keyed.apim.written[f"backends/{member.backend_name}"][
            "properties"
        ]
    identity = [("Authorization", "override", BEARER), ("api-key", "delete", None)]
    assert _placements(_route(_api_policy(keyed, pool), model.id)) == [
        (
            f"@({ATTEMPT} == 1)",
            key.backend_name,
            [
                ("Authorization", "delete", None),
                ("api-key", "override", f"{{{{{backend_key_name(key.backend_name)}}}}}"),
            ],
        ),
        (f"@({ATTEMPT} == 2)", east.backend_name, identity),
        (f"@({ATTEMPT} == 3)", sweden.backend_name, identity),
    ]

    detail = await keyed.service.detail(ACTOR, pool.id)
    assert [member.order for member in detail.models[0].members] == [1, 2, 3]
    assert not any(NO_KEYED_WARNING in warning for warning in detail.warnings)


# -- what stops a key member being published ------------------------------------------------------

_NO_SECRET = (
    "MOSAIC can't find the Key Vault secret this endpoint's API key is in. Set the endpoint's "
    "Key Vault secret URI, then plan again."
)
_BAD_SECRET = (
    "The endpoint's Key Vault secret URI isn't a Key Vault secret identifier. Set it again, then "
    "plan again."
)


@pytest.mark.parametrize(
    ("credential", "problem"),
    [
        (None, _NO_SECRET),
        ("gone", _NO_SECRET),
        ("https://example.com/not-a-secret", _BAD_SECRET),
    ],
    ids=["never-set", "credential-gone", "not-a-secret"],
)
async def test_a_key_member_needs_a_key_vault_secret(
    unkeyed: Estate, credential: str | None, problem: str
) -> None:
    if credential == "gone":
        await unkeyed.update_endpoint("aoai-key", credential_reference_id="cred-gone")
    elif credential is not None:
        await _key_secret(unkeyed, "aoai-key", credential)
    pool = await unkeyed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))

    detail = await unkeyed.service.detail(ACTOR, pool.id)
    assert _keyed_problem(problem) in detail.problems
    with pytest.raises(ConflictError, match="problems are fixed") as refused:
        await unkeyed.service.plan(ACTOR, pool.id)
    assert _keyed_problem(problem) in refused.value.details["problems"]
    assert unkeyed.apim.write_paths("PUT") == []


async def test_a_drained_key_member_needs_no_secret(unkeyed: Estate) -> None:
    pool = await unkeyed.create(
        "OpenAI",
        _model(_member("aoai-east", "gpt-4o"), _member("aoai-key", "gpt-4o", drained=True)),
    )

    plan = await unkeyed.service.plan(ACTOR, pool.id)

    assert "namedValue" not in {kind for kind, *_ in _steps(plan)}
    assert not any(NO_KEYED_WARNING in warning for warning in plan.warnings)


async def test_a_key_member_needs_a_gateway_identity_to_read_its_key(keyed: Estate) -> None:
    gateway = await keyed.gateway_repository.get_gateway(TENANT, keyed.gateway_id)
    assert gateway is not None
    await keyed.update_gateway(
        capabilities=gateway.capabilities.model_copy(
            update={"identity_observed": True, "principal_id": None}
        )
    )
    no_identity = (
        "API Management reads the API keys of this pool's endpoints from Key Vault with its "
        "managed identity, and apim-contoso-dev has none. Turn on its system-assigned managed "
        "identity and re-run its access check first."
    )
    keyed_pool = await keyed.create("Keyed", _gpt4o("aoai-east", "aoai-key"))
    identity_pool = await keyed.create("Identity", _gpt4o("aoai-sweden"))

    assert no_identity in (await keyed.service.detail(ACTOR, keyed_pool.id)).problems
    assert no_identity not in (await keyed.service.detail(ACTOR, identity_pool.id)).problems


async def test_a_gateway_that_cant_read_the_key_is_a_problem(keyed: Estate) -> None:
    await _runtime_access(
        keyed,
        GatewayRuntimeAccess(
            gateway_id=keyed.gateway_id,
            gateway_name="apim-contoso-dev",
            can_invoke=False,
            evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
            reason=RuntimeAccessReason.MISSING_ROLE,
        ),
    )
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))

    detail = await keyed.service.detail(ACTOR, pool.id)

    assert (
        _keyed_problem(
            "The gateway's managed identity can't read this endpoint's API key from Key Vault. "
            "Grant the role shown on the endpoint."
        )
        in detail.problems
    )
    views = {member.endpoint_name: member for member in detail.models[0].members}
    assert views["aoai-key"].readiness == "cannotInvoke"


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [
            GatewayRuntimeAccess(
                gateway_id="",
                gateway_name="apim-contoso-dev",
                evaluation=RuntimeAccessEvaluation.NOT_EVALUATED,
            )
        ],
    ],
    ids=["never-checked", "not-evaluated"],
)
async def test_an_unconfirmed_key_read_is_a_warning(
    keyed: Estate, entries: list[GatewayRuntimeAccess]
) -> None:
    await _runtime_access(
        keyed, *(entry.model_copy(update={"gateway_id": keyed.gateway_id}) for entry in entries)
    )
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))

    detail = await keyed.service.detail(ACTOR, pool.id)

    unconfirmed = _keyed_problem(
        "MOSAIC couldn't confirm that the gateway's managed identity can read this endpoint's API "
        "key from Key Vault. Check the endpoint's gateway access."
    )
    assert unconfirmed in detail.warnings
    assert unconfirmed not in detail.problems
    assert detail.problems == []


async def test_a_key_member_cant_push_a_request_past_ten_attempts(keyed: Estate) -> None:
    extras = [f"aoai-extra-{index}" for index in range(10)]
    for name in extras:
        await keyed.add_endpoint(
            name,
            provider=ModelProvider.AZURE_OPENAI,
            location="eastus2",
            deployments=[_deployment("gpt-4o", "gpt-4o")],
        )
    pool = await keyed.create("OpenAI", _gpt4o(*extras, "aoai-key"), max_retries=9)

    detail = await keyed.service.detail(ACTOR, pool.id)

    assert (
        "A request makes at most 10 attempts, and gpt-4o would make 11: 10 on its backend pool, "
        "then one on the deployment reached with an API key. Lower the pool's retries, or drain "
        "some deployments."
    ) in detail.problems


# -- changing and removing a key member -----------------------------------------------------------


async def test_a_named_value_that_cant_read_its_key_rolls_the_apply_back(keyed: Estate) -> None:
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden", "aoai-key"))
    key = pool.models[0].members[2]
    key_name = backend_key_name(key.backend_name)
    keyed.apim.named_value_status[key_name] = "Forbidden"
    plan = await keyed.service.plan(ACTOR, pool.id)

    run = await keyed.apply(pool.id, plan)

    assert run.status == PublishRunStatus.ROLLED_BACK, run.errors
    assert run.errors[0].startswith(
        f"named value {key_name}: API Management created named value {key_name} but couldn't "
        "read the key from Key Vault (Forbidden)."
    )
    written = keyed.apim.written
    assert f"namedValues/{key_name}" not in written
    backends = {member.backend_name for member in pool.models[0].members}
    assert not any(path.rsplit("/", 1)[-1] in backends for path in written)
    assert f"policyFragments/{pool.fragment_name}" not in written
    failed = await keyed.pool(pool.id)
    assert failed.status == PublicationStatus.ROLLED_BACK
    assert failed.created_resources() == []


async def test_unpublishing_removes_the_named_value_last(keyed: Estate) -> None:
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))
    await keyed.publish(pool.id)
    key = pool.models[0].members[1]
    key_name = backend_key_name(key.backend_name)

    run = await keyed.unpublish(pool.id)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert keyed.apim.write_paths("DELETE")[-1] == f"namedValues/{key_name}"
    written = keyed.apim.written
    for path in (
        f"namedValues/{key_name}",
        f"backends/{key.backend_name}",
        f"policyFragments/{pool.fragment_name}",
        f"apis/{pool.api_name}",
    ):
        assert path not in written, path
    assert (await keyed.pool(pool.id)).created_resources() == []


async def test_draining_a_key_member_removes_its_named_value_after_its_backend(
    keyed: Estate,
) -> None:
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))
    await keyed.publish(pool.id)
    key = pool.models[0].members[1]
    key_name = backend_key_name(key.backend_name)

    await keyed.update(
        pool.id,
        models=[
            _model(_member("aoai-east", "gpt-4o"), _member("aoai-key", "gpt-4o", drained=True))
        ],
    )
    plan = await keyed.service.plan(ACTOR, pool.id)

    assert _steps(plan)[-2:] == [
        ("backend", key.backend_name, "delete"),
        ("namedValue", key_name, "delete"),
    ]
    run = await keyed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert f"namedValues/{key_name}" not in keyed.apim.written
    assert f"backends/{key.backend_name}" not in keyed.apim.written
    assert "{{" not in _policy(keyed.apim, f"apis/{pool.api_name}/policies/policy")


async def test_a_rotated_secret_repoints_the_named_value(keyed: Estate) -> None:
    pool = await keyed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))
    await keyed.publish(pool.id)
    key_name = backend_key_name(pool.models[0].members[1].backend_name)

    # A versioned identifier would pin the key; the named value follows the secret instead.
    await _key_secret(keyed, "aoai-key", f"{ROTATED_SECRET}/0123456789abcdef0123456789abcdef")
    plan = await keyed.service.plan(ACTOR, pool.id)

    assert ("namedValue", key_name, "update") in _steps(plan)
    run = await keyed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    named_value = keyed.apim.written[f"namedValues/{key_name}"]["properties"]
    assert named_value["keyVault"]["secretIdentifier"] == ROTATED_SECRET


# -- governed access ------------------------------------------------------------------------------


async def test_a_governed_pool_stages_a_key_members_named_value_with_the_blocked_list() -> None:
    governed = GovernedEstate()
    await governed.setup()
    await _key_secret(governed, "aoai-key", SECRET_URI)
    pool = await governed.create("OpenAI", _gpt4o("aoai-east", "aoai-key"))
    pool = await governed.update(pool.id, governed_access=ModelAccessSettings())
    await governed.grant(pool, await governed.principal("Ada"))
    model = pool.models[0]
    east, key = model.members
    key_name = backend_key_name(key.backend_name)

    plan = await governed.service.plan(ACTOR, pool.id)

    # Both named values are in place before anything that could name them.
    assert _staged(plan)[:5] == [
        ("namedValue", BLOCKED_COST_CENTERS_NAMED_VALUE, "create", "prepare"),
        ("namedValue", key_name, "create", "prepare"),
        ("backend", east.backend_name, "create", "prepare"),
        ("backend", key.backend_name, "create", "prepare"),
        ("backendPool", model.backend_pool_name, "create", "prepare"),
    ]
    run = await governed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert governed.apim.dangling_references == []
    assert f"{{{{{key_name}}}}}" in _policy(
        governed.apim, f"apis/{pool.api_name}/policies/policy"
    )
    # Whatever a caller authenticated with, none of it reaches a member.
    fragment = _xml(_policy(governed.apim, f"policyFragments/{pool.fragment_name}"))
    deleted = {
        (element.get("name") or "").casefold()
        for element in fragment.iter("set-header")
        if element.get("exists-action") == "delete"
    }
    assert {"api-key", "x-api-key", "authorization", "ocp-apim-subscription-key"} <= deleted
