"""Governed access to model pools: the staged apply, its failures, recovery, and unpublish.

A governed apply keeps the pool closed while it runs: it denies every call and suspends every key
first, installs the reviewed policy, and only then turns on what that policy allows. A failure
leaves the pool with no more access than its last applied snapshot allowed (ADR 0024).
"""

from typing import Any
from uuid import uuid4

import pytest
from apim_double import RESOURCE_ID
from conftest import build_model_pool_service
from mosaic_api.budgets import BLOCKED_COST_CENTERS_NAMED_VALUE, NONE_BLOCKED
from mosaic_api.domain import (
    BindingSource,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    GrantRevocation,
    ModelAccessSettings,
    Principal,
    PrincipalKind,
    PublicationStatus,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishPlan,
    PublishRun,
    PublishRunStatus,
    entitlement_id,
    general_cost_center_id,
    new_id,
    subject_kind_for,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations.apim.policy_semantics import content_digest
from mosaic_api.integrations.apim.writer import DEFAULT_SUBSCRIPTION_KEY_NAMES
from mosaic_api.integrations.pool_policy import (
    pool_grant_counter_identity,
    pool_grant_counter_key_expression,
)
from mosaic_api.model_pools import ModelPool, pool_key_name
from mosaic_api.repositories import (
    InMemoryBudgetRepository,
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
)
from mosaic_api.services.budget_gate import BlockedListGate
from mosaic_api.services.pool_access import entitlement_key_name
from mosaic_api.services.publishing import DENY_ALL_FRAGMENT, DENY_ALL_POLICY
from test_model_pools import (
    ACTOR,
    TENANT,
    Estate,
    _audit,
    _drift,
    _edit,
    _gpt4o,
    _interrupt,
    _policy,
)

GENERAL = general_cost_center_id(TENANT)
RUNTIME_CLIENT_ID = "22222222-2222-2222-2222-222222222222"
KEYS_ONLY = ModelAccessSettings(entra_enabled=False)


class GovernedEstate(Estate):
    """The pool estate, with the directory, grants, and cost centers governed access reads."""

    def __init__(self) -> None:
        super().__init__()
        self.directory = InMemoryDirectoryRepository()
        self.entitlements = InMemoryEntitlementRepository()
        self.cost_centers = InMemoryCostCenterRepository()
        self.service = build_model_pool_service(
            self.apim,
            self.gateway_repository,
            self.endpoint_repository,
            environment_repository=self.environment_repository,
            directory_repository=self.directory,
            entitlement_repository=self.entitlements,
            cost_center_repository=self.cost_centers,
        )

    async def governed_pool(self, settings: ModelAccessSettings | None = None) -> ModelPool:
        pool = await self.create("OpenAI", _gpt4o("aoai-east"))
        return await self.update(pool.id, governed_access=settings or ModelAccessSettings())

    async def principal(self, label: str) -> Principal:
        principal = Principal(
            id=new_id("principal"),
            tenant_id=TENANT,
            object_id=str(uuid4()),
            kind=PrincipalKind.USER,
            label=label,
        )
        await self.directory.create_principal(principal, _audit())
        return principal

    async def grant(
        self,
        pool: ModelPool,
        principal: Principal | None = None,
        *,
        subject: EntitlementSubject | None = None,
        enabled: bool = True,
    ) -> Entitlement:
        if subject is None:
            assert principal is not None
            subject = EntitlementSubject(kind=subject_kind_for(principal.kind), id=principal.id)
        resource = EntitlementResource(kind="poolModel", id=pool.models[0].id, scope_id=pool.id)
        entitlement = Entitlement(
            id=entitlement_id(TENANT, subject, resource, GENERAL),
            tenant_id=TENANT,
            subject=subject,
            resource=resource,
            enabled=enabled,
        )
        await self.entitlements.create_entitlement(entitlement, _audit())
        return entitlement

    async def entitlement(self, entitlement_id: str) -> Entitlement:
        found = await self.entitlements.get_entitlement(TENANT, entitlement_id)
        assert found is not None
        return found

    async def change_grant(self, entitlement: Entitlement, **changes: Any) -> Entitlement:
        current = await self.entitlement(entitlement.id)
        changed = current.model_copy(update=changes)
        await self.entitlements.save_entitlement(changed, _audit())
        return changed

    async def issue_key(
        self, pool_id: str, name: str, *, scope: str | None = None, state: str = "active"
    ) -> None:
        """A grant's key, as its holder or an administrator creates it on request."""

        pool = await self.pool(pool_id)
        self.apim.seed(
            f"subscriptions/{name}",
            {
                "properties": {
                    "scope": scope or f"{RESOURCE_ID}/apis/{pool.api_name}",
                    "state": state,
                    "displayName": "Ada (General)",
                }
            },
        )
        key = PublishedResource(
            kind=PublishedResourceKind.SUBSCRIPTION,
            name=name,
            resource_id=f"{RESOURCE_ID}/subscriptions/{name}",
            created_by_mosaic=True,
        )
        await self.gateway_repository.record_model_pool_state(
            pool.model_copy(
                update={
                    "resources": [
                        *(item for item in pool.resources if item.name != name),
                        key,
                    ]
                }
            )
        )

    async def set_pool_state(self, pool_id: str, **changes: Any) -> None:
        pool = await self.pool(pool_id)
        await self.gateway_repository.record_model_pool_state(pool.model_copy(update=changes))

    def puts(self, path: str, since: int = 0) -> list[dict[str, Any]]:
        """The properties of every write of one resource, in the order they were made."""

        return [
            call["body"]["properties"]
            for call in self.apim.http_calls[since:]
            if call["method"] == "PUT" and call["path"] == path
        ]

    def grant_actions(self) -> list[str]:
        return [event.action for event in self.entitlements.audit_events.values()]


@pytest.fixture
async def governed() -> GovernedEstate:
    built = GovernedEstate()
    await built.setup()
    return built


def _staged(plan: PublishPlan) -> list[tuple[str, str, str, str | None]]:
    return [(str(step.kind), step.name, str(step.action), step.stage) for step in plan.steps]


def _api_policy(pool: ModelPool) -> str:
    return f"apis/{pool.api_name}/policies/policy"


async def _published_with_key(governed: GovernedEstate) -> tuple[ModelPool, Entitlement, str]:
    """A governed pool applied with one grant, whose holder has since created their key."""

    pool = await governed.governed_pool()
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)
    await governed.publish(pool.id)
    key = entitlement_key_name(pool, grant)
    await governed.issue_key(pool.id, key)
    return pool, grant, key


async def _apply_failing(governed: GovernedEstate, pool: ModelPool) -> PublishRun:
    plan = await governed.service.plan(ACTOR, pool.id)
    run = await governed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.FAILED, run.errors
    return run


# -- planning ------------------------------------------------------------------------------------


async def test_a_governed_plan_keeps_the_pool_closed_until_its_policy_is_in(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    model = pool.models[0]
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)

    plan = await governed.service.plan(ACTOR, pool.id)

    staged = _staged(plan)
    # The list every governed policy reads comes first, so it exists before a policy names it.
    assert staged[0] == ("namedValue", BLOCKED_COST_CENTERS_NAMED_VALUE, "create", "prepare")
    # Then what the policy routes to, and a new API that refuses every call until its policy is in.
    assert staged[1:5] == [
        ("backend", model.members[0].backend_name, "create", "prepare"),
        ("backendPool", model.backend_pool_name, "create", "prepare"),
        ("api", pool.api_name, "create", "prepare"),
        ("apiPolicy", "policy", "create", "prepare"),
    ]
    assert plan.steps[3].reason.startswith("Require a subscription until the governed policy")
    assert plan.steps[4].reason == "Deny every call until the reviewed access policy is installed."
    assert {stage for *_, stage in staged[5:-3]} == {"prepare"}
    assert {kind for kind, *_ in staged[5:-3]} == {"apiOperation", "product", "productApi"}
    # The reviewed policy goes in only once nothing can reach it, and the API opens last.
    assert staged[-3:] == [
        ("policyFragment", pool.fragment_name, "create", "policy"),
        ("apiPolicy", "policy", "update", "policy"),
        ("api", pool.api_name, "create", "activate"),
    ]
    assert plan.steps[-2].reason == "Install the reviewed, fail-closed access policy."
    assert plan.steps[-1].reason == (
        "Allow token-only calls after installing the reviewed authorization policy."
    )
    # An apply never creates a key: keys are made on request.
    assert "subscription" not in {kind for kind, *_ in staged}

    snapshot = plan.pool_access_snapshot
    assert snapshot is not None
    assert snapshot.version == 1
    assert plan.previous_access_version is None
    assert snapshot.settings == ModelAccessSettings()
    assert snapshot.audience == RUNTIME_CLIENT_ID
    [compiled] = snapshot.grants
    assert compiled.entitlement_id == grant.id
    assert compiled.pool_model_id == model.id
    assert compiled.object_id == ada.object_id
    assert compiled.display_name == "Ada"
    assert compiled.key_name == pool_key_name(TENANT, pool.id, ada.id, GENERAL)
    assert compiled.enabled
    assert compiled.cost_center_id == GENERAL
    for warning in (
        "This is a pool-wide batch",
        "Existing generic, product, all-API and all-access keys will not authorize this pool.",
        "Governed key authentication uses Ocp-Apim-Subscription-Key",
    ):
        assert any(item.startswith(warning) for item in plan.warnings), warning


async def test_a_mosaic_group_grant_is_left_out_of_the_snapshot_with_a_warning(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    grant = await governed.grant(pool, subject=EntitlementSubject(kind="group", id="group-x"))

    plan = await governed.service.plan(ACTOR, pool.id)

    assert (
        f"Grant {grant.id} is not a supported direct pool grant and will not be enforced by this "
        "apply."
    ) in plan.warnings
    assert plan.pool_access_snapshot is not None
    assert plan.pool_access_snapshot.grants == []


async def test_a_plan_refuses_a_key_mosaic_did_not_create_or_whose_scope_moved(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)
    await governed.publish(pool.id)
    key = entitlement_key_name(pool, grant)
    governed.apim.seed(
        f"subscriptions/{key}",
        {"properties": {"scope": f"{RESOURCE_ID}/apis/{pool.api_name}", "state": "active"}},
    )

    with pytest.raises(ConflictError, match="MOSAIC didn't create it") as refused:
        await governed.service.plan(ACTOR, pool.id)
    assert refused.value.details == {"subscriptionName": key}

    await governed.issue_key(pool.id, key, scope=f"{RESOURCE_ID}/products/elsewhere")
    with pytest.raises(ConflictError, match="live scope changed"):
        await governed.service.plan(ACTOR, pool.id)


async def test_governed_access_cannot_be_cleared_once_set(governed: GovernedEstate) -> None:
    pool = await governed.governed_pool()

    with pytest.raises(ValidationError, match="Governed access cannot be cleared"):
        await governed.update(pool.id, governed_access=None)

    assert (await governed.pool(pool.id)).governed_access == ModelAccessSettings()


async def test_a_pool_whose_access_is_unknown_refuses_changes_until_it_is_recovered(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    await governed.set_pool_state(pool.id, access_state="unknown")

    with pytest.raises(ConflictError, match="access unknown"):
        await governed.update(pool.id, description="Changed")
    with pytest.raises(ConflictError, match="access unknown"):
        await governed.service.plan(ACTOR, pool.id)
    with pytest.raises(ConflictError, match="access unknown"):
        await governed.service.plan_unpublish(ACTOR, pool.id)


async def test_a_grant_changed_after_the_plan_makes_the_plan_stale(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)
    plan = await governed.service.plan(ACTOR, pool.id)
    await governed.change_grant(grant, enabled=False)
    writes = list(governed.apim.writes)

    with pytest.raises(ConflictError, match="out of date") as refused:
        await governed.service.apply(ACTOR, pool.id, plan.id)

    assert refused.value.details["reason"] == "stalePlan"
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None
    assert governed.apim.writes == writes


# -- applying ------------------------------------------------------------------------------------


async def test_a_first_governed_apply_installs_its_policy_behind_a_deny_and_binds_the_grant(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)
    plan = await governed.service.plan(ACTOR, pool.id)

    run = await governed.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    applied = await governed.pool(pool.id)
    assert applied.status == PublicationStatus.PUBLISHED
    assert applied.access_state == "applied"
    assert applied.applied_access == plan.pool_access_snapshot
    fake = governed.apim
    paths = [call["path"] for call in fake.http_calls if call["method"] == "PUT"]
    policies = [item["value"] for item in governed.puts(_api_policy(pool))]
    assert policies[0] == DENY_ALL_POLICY
    assert len(policies) == 2
    assert policies[-1] != DENY_ALL_POLICY
    # Nothing can reach the fragment before the API's policy refuses every call.
    assert paths.index(_api_policy(pool)) < paths.index(f"policyFragments/{pool.fragment_name}")
    # And nothing is written before what it names exists.
    assert fake.dangling_references == []
    # Token-only calls are allowed only once the policy that checks tokens is in.
    apis = governed.puts(f"apis/{pool.api_name}")
    assert [item["subscriptionRequired"] for item in apis] == [True, False]
    assert all(
        item["subscriptionKeyParameterNames"] == DEFAULT_SUBSCRIPTION_KEY_NAMES for item in apis
    )
    blocked = fake.written[f"namedValues/{BLOCKED_COST_CENTERS_NAMED_VALUE}"]
    assert blocked["properties"]["value"] == NONE_BLOCKED
    # No publication owns the gateway's blocked list, so the pool's record doesn't either.
    assert BLOCKED_COST_CENTERS_NAMED_VALUE not in {item.name for item in applied.resources}
    assert not any(path.startswith("subscriptions/") for path in paths)
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None

    bound = await governed.entitlement(grant.id)
    assert applied.applied_access is not None
    [compiled] = applied.applied_access.grants
    assert bound.binding is not None
    assert bound.binding.source == BindingSource.ORCHESTRATED
    assert bound.binding.gateway_id == governed.gateway_id
    assert bound.binding.apim_subscription_name == compiled.key_name
    assert bound.binding.counter_key_expression == pool_grant_counter_key_expression(
        applied, compiled
    )
    assert bound.binding.attribution_key == pool_grant_counter_identity(applied, compiled)
    assert bound.binding.attribution_per_member is False
    assert bound.runtime is None
    assert "entitlement.runtimeProjected" in governed.grant_actions()


async def test_a_later_apply_suspends_keys_while_it_runs_and_then_activates_them(
    governed: GovernedEstate,
) -> None:
    pool, grant, key = await _published_with_key(governed)

    plan = await governed.service.plan(ACTOR, pool.id)

    assert plan.previous_access_version == 1
    assert plan.pool_access_snapshot is not None
    assert plan.pool_access_snapshot.version == 2
    # The API is live, so the first thing the apply does after the list is deny every call.
    assert _staged(plan)[1] == ("apiPolicy", "policy", "update", "prepare")
    assert plan.steps[1].reason == "Temporarily deny all calls while this pool-wide batch applies."
    keys = [step for step in plan.steps if step.kind == PublishedResourceKind.SUBSCRIPTION]
    assert [(step.name, step.stage, step.subscription_state) for step in keys] == [
        (key, "prepare", "suspended"),
        (key, "activate", "active"),
    ]
    assert keys[0].reason == f"Suspend the key of grant {grant.id} while access is applied."
    assert keys[1].reason == f"Activate key access for grant {grant.id}."
    assert all(step.entitlement_id == grant.id for step in keys)
    since = len(governed.apim.http_calls)

    run = await governed.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    written = governed.puts(f"subscriptions/{key}", since)
    assert [item["state"] for item in written] == ["suspended", "active"]
    assert {item["scope"].casefold() for item in written} == {
        f"{RESOURCE_ID}/apis/{pool.api_name}".casefold()
    }
    applied = await governed.pool(pool.id)
    assert applied.owns_key(key)
    assert applied.applied_access is not None
    assert applied.applied_access.version == 2


async def test_a_governed_replan_warns_about_policy_edits_made_outside_mosaic(
    governed: GovernedEstate,
) -> None:
    pool, _, _ = await _published_with_key(governed)
    applied = _policy(governed.apim, _api_policy(pool))
    # What MOSAIC compares against is the policy the run left in force, not the deny before it.
    assert applied != DENY_ALL_POLICY
    assert (await governed.pool(pool.id)).applied_policy_sha256 == content_digest(applied)
    assert _drift(await governed.service.plan(ACTOR, pool.id)) == []

    _edit(governed.apim, _api_policy(pool))

    assert _drift(await governed.service.plan(ACTOR, pool.id)) == [
        "Someone changed the pool's API policy in API Management after MOSAIC last applied it. "
        "Applying replaces those changes."
    ]
    await governed.publish(pool.id)
    assert _policy(governed.apim, _api_policy(pool)) == applied
    assert _drift(await governed.service.plan(ACTOR, pool.id)) == []


async def test_a_disabled_grant_keeps_its_key_suspended_and_loses_its_binding(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool(KEYS_ONLY)
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)
    await governed.publish(pool.id)
    key = entitlement_key_name(pool, grant)
    await governed.issue_key(pool.id, key)
    assert (await governed.entitlement(grant.id)).binding is not None
    await governed.change_grant(grant, enabled=False)

    plan = await governed.service.plan(ACTOR, pool.id)

    keys = [step for step in plan.steps if step.kind == PublishedResourceKind.SUBSCRIPTION]
    assert [(step.stage, step.subscription_state) for step in keys] == [("prepare", "suspended")]
    # Without Entra, callers keep authenticating with their keys.
    assert plan.steps[-1].reason == "Keep subscription authentication required."

    run = await governed.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert governed.apim.written[f"subscriptions/{key}"]["properties"]["state"] == "suspended"
    api = governed.apim.written[f"apis/{pool.api_name}"]["properties"]
    assert api["subscriptionRequired"] is True
    assert (await governed.entitlement(grant.id)).binding is None
    applied = await governed.pool(pool.id)
    assert applied.applied_access is not None
    assert [item.enabled for item in applied.applied_access.grants] == [False]
    # The key outlives the grant's access, suspended, so turning the grant back on restores it.
    assert applied.owns_key(key)


async def test_a_revoked_grant_has_its_key_deleted(governed: GovernedEstate) -> None:
    pool, grant, key = await _published_with_key(governed)
    await governed.change_grant(
        grant,
        enabled=False,
        revocation=GrantRevocation(cost_center_id=GENERAL, revoked_by=ACTOR.object_id),
    )

    plan = await governed.service.plan(ACTOR, pool.id)

    [deletion] = [step for step in plan.steps if step.kind == PublishedResourceKind.SUBSCRIPTION]
    assert (deletion.name, deletion.action, deletion.stage) == (
        key,
        PublishAction.DELETE,
        "prepare",
    )
    assert deletion.reason == (
        f"Delete the key of grant {grant.id}, revoked when its subject left its cost center."
    )

    run = await governed.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert ("DELETE", f"subscriptions/{key}") in governed.apim.writes
    assert f"subscriptions/{key}" not in governed.apim.written
    applied = await governed.pool(pool.id)
    assert not applied.owns_key(key)
    assert applied.applied_access is not None
    [revoked] = applied.applied_access.grants
    assert revoked.revoked
    assert not revoked.enabled


async def test_governing_an_open_pool_suspends_its_bootstrap_subscription(
    governed: GovernedEstate,
) -> None:
    pool = await governed.create("OpenAI", _gpt4o("aoai-east"))
    await governed.publish(pool.id)
    bootstrap = f"subscriptions/{pool.subscription_name}"
    assert governed.apim.written[bootstrap]["properties"]["state"] == "active"
    await governed.update(pool.id, governed_access=ModelAccessSettings())

    plan = await governed.service.plan(ACTOR, pool.id)

    [step] = [step for step in plan.steps if step.kind == PublishedResourceKind.SUBSCRIPTION]
    assert (step.name, step.stage, step.subscription_state) == (
        pool.subscription_name,
        "prepare",
        "suspended",
    )
    assert step.reason == (
        "Suspend the pool's bootstrap subscription; governed callers use their own keys."
    )

    run = await governed.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    live = governed.apim.written[bootstrap]["properties"]
    assert live["state"] == "suspended"
    # It stays on the pool's product, so unpublishing finds and removes it.
    assert live["scope"].casefold().endswith(f"/products/{pool.product_name}".casefold())
    applied = await governed.pool(pool.id)
    assert applied.owns_key(pool.subscription_name)
    assert applied.access_state == "applied"


async def test_governing_a_pool_resets_customized_key_names(governed: GovernedEstate) -> None:
    pool = await governed.create("OpenAI", _gpt4o("aoai-east"))
    await governed.publish(pool.id)
    api = governed.apim.written[f"apis/{pool.api_name}"]["properties"]
    api["subscriptionKeyParameterNames"] = {"header": "X-Pool-Key", "query": "pool-key"}
    await governed.update(pool.id, governed_access=ModelAccessSettings())

    plan = await governed.service.plan(ACTOR, pool.id)

    assert any(
        item.startswith("This API's customized subscription key names will be reset")
        for item in plan.warnings
    )
    run = await governed.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    live = governed.apim.written[f"apis/{pool.api_name}"]["properties"]
    assert live["subscriptionKeyParameterNames"] == DEFAULT_SUBSCRIPTION_KEY_NAMES


# -- failures ------------------------------------------------------------------------------------


async def test_a_first_apply_that_fails_leaves_the_pool_denied(governed: GovernedEstate) -> None:
    pool = await governed.governed_pool()
    ada = await governed.principal("Ada")
    grant = await governed.grant(pool, ada)
    governed.apim.fail_write(f"policyFragments/{pool.fragment_name}")

    run = await _apply_failing(governed, pool)

    assert any("Access was restricted" in error for error in run.errors)
    # Nothing was restored: the policy that would have been includes the fragment that failed.
    assert not any(error.startswith("Restoring") for error in run.errors)
    assert run.completed_at is not None
    failed = await governed.pool(pool.id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.access_state == "failed"
    assert failed.last_error is not None
    assert pool.fragment_name in failed.last_error
    # Nothing was ever applied, so the safest snapshot is the reviewed one with everything off.
    safe = failed.applied_access
    assert safe is not None
    assert safe.version == 1
    assert (safe.settings.keys_enabled, safe.settings.entra_enabled) == (False, False)
    assert [(item.entitlement_id, item.enabled) for item in safe.grants] == [(grant.id, False)]
    assert governed.apim.written[f"apis/{pool.api_name}"]["properties"]["subscriptionRequired"]
    assert _policy(governed.apim, _api_policy(pool)) == DENY_ALL_POLICY
    assert governed.apim.dangling_references == []
    assert (await governed.entitlement(grant.id)).binding is None
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None


async def test_a_failed_later_apply_restores_the_last_safe_snapshot(
    governed: GovernedEstate,
) -> None:
    pool, first, key = await _published_with_key(governed)
    grace = await governed.principal("Grace")
    second = await governed.grant(pool, grace)
    governed.apim.fail_write(f"products/{pool.product_name}")
    since = len(governed.apim.http_calls)

    await _apply_failing(governed, pool)

    failed = await governed.pool(pool.id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.access_state == "failed"
    safe = failed.applied_access
    assert safe is not None
    # What was applied before stays on; the grant this apply would have added doesn't.
    assert safe.version == 2
    assert [(item.entitlement_id, item.enabled) for item in safe.grants] == [(first.id, True)]
    assert second.id not in {item.entitlement_id for item in safe.grants}
    states = [item["state"] for item in governed.puts(f"subscriptions/{key}", since)]
    assert states[0] == "suspended"
    assert states[-1] == "active"
    policies = [item["value"] for item in governed.puts(_api_policy(pool), since)]
    assert policies[0] == DENY_ALL_POLICY
    assert policies[-1] != DENY_ALL_POLICY
    assert (await governed.entitlement(first.id)).binding is not None
    assert (await governed.entitlement(second.id)).binding is None
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None


async def test_a_restore_that_fails_leaves_the_pool_denied(governed: GovernedEstate) -> None:
    pool, grant, key = await _published_with_key(governed)
    governed.apim.fail_write(f"products/{pool.product_name}")
    # The apply's deny and the failure's deny go in; the restored policy doesn't.
    governed.apim.fail_write_after(_api_policy(pool), 2)

    run = await _apply_failing(governed, pool)

    assert any(
        error.startswith("Restoring the last safe access snapshot failed") for error in run.errors
    )
    assert any("Access was restricted" in error for error in run.errors)
    assert _policy(governed.apim, _api_policy(pool)) == DENY_ALL_POLICY
    assert _policy(governed.apim, f"policyFragments/{pool.fragment_name}") == DENY_ALL_FRAGMENT
    assert governed.apim.written[f"subscriptions/{key}"]["properties"]["state"] == "suspended"
    failed = await governed.pool(pool.id)
    assert failed.access_state == "failed"
    safe = failed.applied_access
    assert safe is not None
    assert [(item.entitlement_id, item.enabled) for item in safe.grants] == [(grant.id, False)]
    assert (await governed.entitlement(grant.id)).binding is None
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None


async def test_a_first_apply_that_cannot_write_its_api_has_nothing_to_deny(
    governed: GovernedEstate,
) -> None:
    pool = await governed.governed_pool()
    governed.apim.fail_write(f"apis/{pool.api_name}")

    await _apply_failing(governed, pool)

    failed = await governed.pool(pool.id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.access_state == "failed"
    assert _api_policy(pool) not in governed.apim.written
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None


# -- recovery ------------------------------------------------------------------------------------


async def test_recovering_an_interrupted_governed_run_denies_the_pool(
    governed: GovernedEstate,
) -> None:
    pool, grant, key = await _published_with_key(governed)
    run = await _interrupt(governed, await governed.pool(pool.id))
    with pytest.raises(ConflictError):
        await governed.service.plan(ACTOR, pool.id)
    with pytest.raises(ConflictError):
        await governed.update(pool.id, description="Changed")

    recovered = await governed.service.recover_interrupted(
        ACTOR, pool.id, run_id=run.id, confirm_quiesced=True
    )

    assert recovered.status == PublishRunStatus.INTERRUPTED
    assert _policy(governed.apim, _api_policy(pool)) == DENY_ALL_POLICY
    assert governed.apim.written[f"subscriptions/{key}"]["properties"]["state"] == "suspended"
    failed = await governed.pool(pool.id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.access_state == "failed"
    assert failed.last_plan_id is None
    safe = failed.applied_access
    assert safe is not None
    assert safe.version == 2
    assert [(item.entitlement_id, item.enabled) for item in safe.grants] == [(grant.id, False)]
    assert (await governed.entitlement(grant.id)).binding is None
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None
    assert "modelPool.recovered" in governed.audit_actions()

    # A reviewed apply puts back what the grants allow.
    plan = await governed.service.plan(ACTOR, pool.id)
    restored = await governed.apply(pool.id, plan)
    assert restored.status == PublishRunStatus.SUCCEEDED, restored.errors
    assert governed.apim.written[f"subscriptions/{key}"]["properties"]["state"] == "active"
    applied = await governed.pool(pool.id)
    assert applied.access_state == "applied"
    assert applied.applied_access is not None
    assert applied.applied_access.version == 3
    assert (await governed.entitlement(grant.id)).binding is not None


async def test_recovery_that_cannot_deny_keeps_the_pool_locked(
    governed: GovernedEstate,
) -> None:
    pool, _, _ = await _published_with_key(governed)
    run = await _interrupt(governed, await governed.pool(pool.id))
    governed.apim.fail_write(_api_policy(pool))
    governed.apim.fail_write(f"policyFragments/{pool.fragment_name}")

    await governed.service.recover_interrupted(ACTOR, pool.id, run_id=run.id, confirm_quiesced=True)

    stopped = await governed.service.get_run(ACTOR, pool.id, run.id)
    assert stopped.status == PublishRunStatus.INTERRUPTED
    assert any("could not confirm complete denial" in error for error in stopped.errors)
    unknown = await governed.pool(pool.id)
    assert unknown.access_state == "unknown"
    assert await governed.service.get_lock_owner(ACTOR, pool.id) == run.id

    # Once API Management takes writes again, recovery goes through.
    governed.apim.write_failures.clear()
    await governed.service.recover_interrupted(ACTOR, pool.id, run_id=run.id, confirm_quiesced=True)
    recovered = await governed.pool(pool.id)
    assert recovered.access_state == "failed"
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None


async def test_the_reaper_fails_a_governed_pool_left_applying(governed: GovernedEstate) -> None:
    pool = await governed.governed_pool()
    await _interrupt(governed, pool, locked=False)
    await governed.set_pool_state(pool.id, access_state="applying")

    assert await governed.service.reap_stale_publish_runs(TENANT) == 1

    reaped = await governed.pool(pool.id)
    assert reaped.status == PublicationStatus.FAILED
    assert reaped.access_state == "failed"


# -- unpublishing --------------------------------------------------------------------------------


async def test_unpublishing_a_governed_pool_cuts_callers_off_before_deleting_anything(
    governed: GovernedEstate,
) -> None:
    pool, grant, key = await _published_with_key(governed)
    published = await governed.pool(pool.id)

    plan = await governed.service.plan_unpublish(ACTOR, pool.id)

    assert any(item.startswith("Before it deletes anything") for item in plan.warnings)
    assert (plan.steps[0].kind, plan.steps[0].name) == (PublishedResourceKind.API, pool.api_name)
    assert plan.pool_access_snapshot == published.applied_access
    [key_step] = [step for step in plan.steps if step.kind == PublishedResourceKind.SUBSCRIPTION]
    assert key_step.entitlement_id == grant.id
    writes = len(governed.apim.writes)

    run = await governed.unpublish(pool.id)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    made = governed.apim.writes[writes:]
    assert made[:2] == [("PUT", _api_policy(pool)), ("PUT", f"subscriptions/{key}")]
    assert made[2] == ("DELETE", f"apis/{pool.api_name}")
    assert {method for method, _ in made[2:]} == {"DELETE"}
    assert ("DELETE", f"subscriptions/{key}") in made
    draft = await governed.pool(pool.id)
    assert draft.status == PublicationStatus.DRAFT
    assert draft.resources == []
    assert draft.access_state == "applied"
    denied = draft.applied_access
    assert denied is not None
    assert denied.version == 2
    assert [(item.entitlement_id, item.enabled) for item in denied.grants] == [(grant.id, False)]
    assert (await governed.entitlement(grant.id)).binding is None
    # The gateway's blocked list belongs to no publication, so it stays for the others.
    assert f"namedValues/{BLOCKED_COST_CENTERS_NAMED_VALUE}" in governed.apim.written
    assert await governed.service.get_lock_owner(ACTOR, pool.id) is None


async def test_an_unpublish_that_cannot_deny_deletes_nothing_and_keeps_the_lock(
    governed: GovernedEstate,
) -> None:
    pool, _, _ = await _published_with_key(governed)
    governed.apim.fail_write(_api_policy(pool))
    governed.apim.fail_write(f"policyFragments/{pool.fragment_name}")
    plan = await governed.service.plan_unpublish(ACTOR, pool.id)

    run = await governed.service.unpublish(ACTOR, pool.id, plan.id)
    with pytest.raises(ConflictError, match="fail-closed"):
        await governed.service.wait_for_idle()

    stopped = await governed.service.get_run(ACTOR, pool.id, run.id)
    assert stopped.status == PublishRunStatus.INTERRUPTED
    assert any(error.startswith("Unpublish outcome is unknown") for error in stopped.errors)
    unknown = await governed.pool(pool.id)
    assert unknown.access_state == "unknown"
    assert await governed.service.get_lock_owner(ACTOR, pool.id) == run.id
    assert governed.apim.write_paths("DELETE") == []


# -- budgets -------------------------------------------------------------------------------------


async def test_the_budget_check_keeps_the_blocked_list_on_gateways_with_governed_pools(
    governed: GovernedEstate,
) -> None:
    gate = BlockedListGate(
        InMemoryBudgetRepository(), gateway_repository=governed.gateway_repository
    )
    pool = await governed.create("OpenAI", _gpt4o("aoai-east"))

    # An open pool's policy charges no cost center, so it never reads the list.
    assert await gate.gateways(TENANT) == []

    await governed.update(pool.id, governed_access=ModelAccessSettings())

    assert [gateway.id for gateway in await gate.gateways(TENANT)] == [governed.gateway_id]
