"""Cost centers on pool models: their limits, and the rechecks that revoke grants (ADR 0024).

A cost center's per-person limits on a pool model apply to each grant on it that sets none, and
its pooled quota is shared by every grant under it on that model. A grant whose subject may no
longer charge its cost center is revoked under the pool's lock. Until MOSAIC has checked it,
every plan leaves it out, as ADR 0022 requires of a publication's grants.
"""

import xml.etree.ElementTree as ET

import pytest
from mosaic_api.cost_centers import (
    CostCenterCreate,
    CostCenterLimit,
    CostCenterLimitsUpdate,
    CostCenterView,
    PersonLimits,
    PooledQuota,
)
from mosaic_api.domain import (
    GENERAL_COST_CENTER_CODE,
    Entitlement,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    PoolModelQuota,
    Principal,
    PublishAction,
    PublishedResourceKind,
    PublishRunStatus,
    TokenEnforcement,
    entitlement_id,
)
from mosaic_api.integrations.pool_policy import pool_grant_counter_key_expression
from mosaic_api.model_pools import ModelPool
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.pool_access import entitlement_key_name
from test_model_pools import ACTOR, TENANT, _audit
from test_pool_governed import GENERAL, GovernedEstate

WAITING = "waiting for MOSAIC to check"


class Charged(GovernedEstate):
    """The governed pool estate, with the services that manage cost centers and revoke grants."""

    def __init__(self) -> None:
        super().__init__()
        self.grants = EntitlementService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateway_repository,
            endpoint_repository=self.endpoint_repository,
            cost_center_repository=self.cost_centers,
        )
        self.charging = CostCenterService(
            self.cost_centers,
            directory_repository=self.directory,
            entitlement_repository=self.entitlements,
            gateway_repository=self.gateway_repository,
            entitlements=self.grants,
        )

    async def research(self, *members: Principal) -> CostCenterView:
        created = await self.charging.create_cost_center(
            ACTOR, CostCenterCreate(name="Research", code="RES")
        )
        for member in members:
            await self.charging.add_member(ACTOR, created.id, member.id)
        return created

    async def charged_grant(
        self,
        pool: ModelPool,
        principal: Principal,
        cost_center_id: str,
        *,
        enforcement: EntitlementEnforcement | None = None,
    ) -> Entitlement:
        """A direct grant on the pool's first model, charged to ``cost_center_id``."""

        subject = EntitlementSubject(kind="user", id=principal.id)
        resource = _pool_model(pool)
        grant = Entitlement(
            id=entitlement_id(TENANT, subject, resource, cost_center_id),
            tenant_id=TENANT,
            subject=subject,
            resource=resource,
            cost_center_id=cost_center_id,
            enforcement=enforcement,
        )
        await self.entitlements.create_entitlement(grant, _audit())
        return grant

    def revocations(self, grant: Entitlement) -> list[dict[str, object]]:
        return [
            event.details
            for event in self.entitlements.audit_events.values()
            if event.action == "entitlement.revoked" and event.resource_id == grant.id
        ]


@pytest.fixture
async def estate() -> Charged:
    built = Charged()
    await built.setup()
    return built


def _pool_model(pool: ModelPool) -> EntitlementResource:
    return EntitlementResource(kind="poolModel", id=pool.models[0].id, scope_id=pool.id)


def _tokens_per_minute(value: int) -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=value
        )
    )


def _limits(estate: Charged, pool: ModelPool, element: str) -> list[dict[str, str]]:
    """The attributes of every ``element`` in the pool's applied fragment."""

    value = estate.apim.written[f"policyFragments/{pool.fragment_name}"]["properties"]["value"]
    return [dict(item.attrib) for item in ET.fromstring(value).iter(element)]


# -- limits ---------------------------------------------------------------------------------------


async def test_a_cost_centers_limits_on_a_pool_model_reach_the_pools_policy(
    estate: Charged,
) -> None:
    pool = await estate.governed_pool()
    model = pool.models[0]
    inheriting = await estate.grant(pool, await estate.principal("Ada"))
    own = _tokens_per_minute(500)
    limited = await estate.charged_grant(
        pool, await estate.principal("Grace"), GENERAL, enforcement=own
    )
    person = PersonLimits(tokens_per_minute=100)
    await estate.charging.set_limits(
        ACTOR,
        GENERAL,
        CostCenterLimitsUpdate(
            limits=[
                CostCenterLimit(
                    resource=_pool_model(pool),
                    person=person,
                    pool=PooledQuota(monthly_tokens=1_000_000, monthly_calls=5_000),
                )
            ]
        ),
    )

    plan = await estate.service.plan(ACTOR, pool.id)

    snapshot = plan.pool_access_snapshot
    assert snapshot is not None
    # A grant that sets no limits takes its cost center's per-person limits for the model.
    assert {grant.entitlement_id: grant.enforcement for grant in snapshot.grants} == {
        inheriting.id: person.enforcement(),
        limited.id: own,
    }
    # The pooled quota is counted once per cost center and pool model, whoever calls.
    assert snapshot.quotas == [
        PoolModelQuota(
            cost_center_id=GENERAL,
            cost_center_code=GENERAL_COST_CENTER_CODE,
            monthly_tokens=1_000_000,
            monthly_calls=5_000,
            pool_model_id=model.id,
        )
    ]

    await estate.publish(pool.id)

    applied = await estate.pool(pool.id)
    assert applied.applied_access is not None
    compiled = {grant.entitlement_id: grant for grant in applied.applied_access.grants}
    tokens = {item["counter-key"]: item for item in _limits(estate, applied, "llm-token-limit")}
    for grant, tokens_per_minute in ((inheriting, "100"), (limited, "500")):
        counter = pool_grant_counter_key_expression(applied, compiled[grant.id])
        assert tokens[counter]["tokens-per-minute"] == tokens_per_minute
    [pooled] = [
        item for key, item in tokens.items() if key.startswith("mosaic:pool:cost-center-tokens:")
    ]
    assert (pooled["token-quota"], pooled["token-quota-period"]) == ("1000000", "Monthly")
    [calls] = _limits(estate, applied, "quota-by-key")
    assert calls["calls"] == "5000"
    assert "cost-center-request-quota" in calls["counter-key"]


# -- rechecks -------------------------------------------------------------------------------------


async def test_leaving_a_cost_center_revokes_the_pool_grants_charged_to_it(
    estate: Charged,
) -> None:
    pool = await estate.governed_pool()
    ada = await estate.principal("Ada")
    research = await estate.research(ada)
    charged = await estate.charged_grant(pool, ada, research.id)
    # Everyone may charge General, so leaving Research doesn't touch it.
    kept = await estate.grant(pool, ada)
    await estate.publish(pool.id)
    charged_key = entitlement_key_name(pool, charged)
    kept_key = entitlement_key_name(pool, kept)
    assert charged_key is not None and kept_key is not None and charged_key != kept_key
    for key in (charged_key, kept_key):
        await estate.issue_key(pool.id, key)

    view = await estate.charging.remove_member(ACTOR, research.id, ada.id)

    assert view.pending_rechecks == []
    revoked = await estate.entitlement(charged.id)
    assert revoked.enabled is False
    assert revoked.revocation is not None
    assert revoked.revocation.cost_center_id == research.id
    assert estate.revocations(charged) == [
        {"reason": "costCenterMembership", "costCenterId": research.id}
    ]
    assert (await estate.entitlement(kept.id)).revocation is None

    plan = await estate.service.plan(ACTOR, pool.id)

    deletions = [
        step.name
        for step in plan.steps
        if step.kind == PublishedResourceKind.SUBSCRIPTION and step.action == PublishAction.DELETE
    ]
    assert deletions == [charged_key]
    run = await estate.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert f"subscriptions/{charged_key}" not in estate.apim.written
    assert estate.apim.written[f"subscriptions/{kept_key}"]["properties"]["state"] == "active"
    applied = await estate.pool(pool.id)
    assert applied.applied_access is not None
    assert {(grant.entitlement_id, grant.enabled) for grant in applied.applied_access.grants} == {
        (charged.id, False),
        (kept.id, True),
    }


async def test_a_recheck_waits_for_a_busy_pool_and_its_grant_stays_out_until_then(
    estate: Charged,
) -> None:
    pool = await estate.governed_pool()
    ada = await estate.principal("Ada")
    research = await estate.research(ada)
    charged = await estate.charged_grant(pool, ada, research.id)
    await estate.publish(pool.id)
    await estate.gateway_repository.acquire_publication_lock(TENANT, pool.id, "run-elsewhere")

    # Ada leaves at once. Revoking her grant needs the pool's lock, so the recheck waits.
    view = await estate.charging.remove_member(ACTOR, research.id, ada.id)

    assert ada.id not in view.member_ids()
    [pending] = view.pending_rechecks
    assert (pending.reason, pending.subject_id, pending.entitlement_ids) == (
        "memberRemoved",
        ada.id,
        [charged.id],
    )
    assert (await estate.entitlement(charged.id)).revocation is None

    await estate.gateway_repository.release_publication_lock(TENANT, pool.id, "run-elsewhere")
    plan = await estate.service.plan(ACTOR, pool.id)

    snapshot = plan.pool_access_snapshot
    assert snapshot is not None
    # The applied grant stays in the snapshot, off, so this apply would take its access away.
    assert [(grant.entitlement_id, grant.enabled) for grant in snapshot.grants] == [
        (charged.id, False)
    ]
    [warning] = [item for item in plan.warnings if WAITING in item]
    assert charged.id in warning and "Research (RES)" in warning

    view = await estate.charging.recheck(ACTOR, research.id)

    assert view.pending_rechecks == []
    revoked = await estate.entitlement(charged.id)
    assert revoked.revocation is not None
    assert revoked.revocation.cost_center_id == research.id
    assert estate.revocations(charged) == [
        {"reason": "costCenterMembership", "costCenterId": research.id}
    ]


async def test_a_recheck_that_finds_the_subject_may_still_charge_keeps_the_grant(
    estate: Charged,
) -> None:
    pool = await estate.governed_pool()
    ada = await estate.principal("Ada")
    grant = await estate.grant(pool, ada)
    await estate.grants.request_recheck(ACTOR, GENERAL, reason="memberRemoved", subject_id=ada.id)

    plan = await estate.service.plan(ACTOR, pool.id)

    assert plan.pool_access_snapshot is not None
    assert plan.pool_access_snapshot.grants == []
    assert any(grant.id in item and WAITING in item for item in plan.warnings)

    # General is the tenant's default, which everyone may charge, so the recheck keeps the grant.
    assert await estate.grants.resume_rechecks(ACTOR) == []
    plan = await estate.service.plan(ACTOR, pool.id)

    assert plan.pool_access_snapshot is not None
    assert [item.entitlement_id for item in plan.pool_access_snapshot.grants] == [grant.id]
    assert not any(WAITING in item for item in plan.warnings)
