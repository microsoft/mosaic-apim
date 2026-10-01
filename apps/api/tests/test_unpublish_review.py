"""Unpublishing runs only a reviewed plan: what it deletes, whose access it ends, and nothing else.

These drive the real publishing services against the API Management double, as the publish tests
do, so the plan, the refusals and the teardown are exercised rather than mocked.
"""

from collections.abc import AsyncIterator

import pytest
from apim_double import RESOURCE_ID
from conftest import reviewed_unpublish
from mosaic_api.domain import (
    ModelAccessSettings,
    PublicationCreate,
    PublicationStatus,
    PublicationUpdate,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishPlan,
    PublishRunStatus,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.services.publishing import unpublish_digest
from test_governed_lifecycle import ACTOR as GOVERNED_ACTOR
from test_governed_lifecycle import APPLICATION, USER
from test_governed_lifecycle import Harness as GovernedHarness
from test_mcp_publishing import ACTOR as MCP_ACTOR
from test_mcp_publishing import Harness as McpHarness
from test_publishing import (
    ACTOR,
    API_NAME,
    API_SUFFIX,
    BACKEND_SUFFIX,
    FRAGMENT_SUFFIX,
    SUBSCRIPTION_SUFFIX,
)
from test_publishing import Harness as ModelHarness


@pytest.fixture
async def models() -> ModelHarness:
    built = ModelHarness()
    await built.setup()
    return built


@pytest.fixture
async def governed() -> AsyncIterator[GovernedHarness]:
    built = GovernedHarness()
    await built.setup()
    yield built
    await built.close()


@pytest.fixture
async def mcp() -> McpHarness:
    built = McpHarness()
    await built.setup()
    return built


async def _published(models: ModelHarness) -> str:
    publication_id = await models.publish()
    await models.apply(publication_id)
    models.apim.writes.clear()
    return publication_id


async def test_planning_an_unpublish_removes_nothing_and_changes_nothing(
    models: ModelHarness,
) -> None:
    publication_id = await _published(models)
    before = await models.service.get_publication(ACTOR, publication_id)

    plan = await models.service.plan_unpublish(ACTOR, publication_id)

    assert models.apim.write_paths() == []
    assert await models.service.get_publication(ACTOR, publication_id) == before
    assert await models.gateway_repository.get_publication_lock(
        ACTOR.tenant_id, publication_id
    ) is None
    assert plan.operation == "unpublish"
    assert plan.target == "model"
    assert plan.digest == unpublish_digest(before)
    assert await models.service.get_plan(ACTOR, plan.id) == plan
    # Everything MOSAIC created, each deleted before what it names.
    assert {step.action for step in plan.steps} == {PublishAction.DELETE}
    assert plan.steps[0].kind == PublishedResourceKind.SUBSCRIPTION
    assert [step.kind for step in plan.steps][-2:] == [
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.BACKEND,
    ]
    assert {(step.kind, step.name) for step in plan.steps} == {
        (item.kind, item.name) for item in before.created_resources()
    }
    assert "Delete the API that fronts this model. The gateway stops serving it." in [
        step.reason for step in plan.steps
    ]
    assert plan.access_snapshot is None
    assert any(f"Deleting product {API_NAME}" in warning for warning in plan.warnings)


async def test_unpublish_without_a_reviewed_plan_removes_nothing(models: ModelHarness) -> None:
    publication_id = await _published(models)

    with pytest.raises(ConflictError) as refused:
        await models.service.unpublish(ACTOR, publication_id)

    assert refused.value.details["reason"] == "planRequired"
    assert models.apim.write_paths() == []
    assert await models.gateway_repository.get_publication_lock(
        ACTOR.tenant_id, publication_id
    ) is None
    publication = await models.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.has_applied_api()


async def test_unpublish_runs_only_an_unpublish_plan_for_the_same_publication(
    models: ModelHarness,
) -> None:
    publication_id = await _published(models)
    publish_plan = await models.service.plan(ACTOR, publication_id)
    review = await models.service.plan_unpublish(ACTOR, publication_id)
    elsewhere = review.model_copy(update={"id": "publishplan_elsewhere", "publication_id": "other"})
    await models.gateway_repository.save_publish_plan(elsewhere)

    for plan_id in (publish_plan.id, elsewhere.id, "publishplan_missing"):
        with pytest.raises(NotFoundError):
            await models.service.unpublish(ACTOR, publication_id, plan_id)

    assert models.apim.write_paths("DELETE") == []
    assert await models.gateway_repository.get_publication_lock(
        ACTOR.tenant_id, publication_id
    ) is None


async def test_apply_refuses_an_unpublish_plan(models: ModelHarness) -> None:
    publication_id = await _published(models)
    review = await models.service.plan_unpublish(ACTOR, publication_id)
    # A plan saved before unpublishing was planned has no operation, only delete steps.
    legacy = PublishPlan.model_validate(
        {
            **review.model_dump(by_alias=False, exclude={"operation"}),
            "id": "publishplan_legacy_unpublish",
        }
    )
    await models.gateway_repository.save_publish_plan(legacy)

    for plan_id in (review.id, legacy.id):
        with pytest.raises(ConflictError, match="apply runs only publish plans"):
            await models.service.apply(ACTOR, publication_id, plan_id)

    assert models.apim.write_paths() == []


async def test_unpublish_runs_exactly_the_reviewed_steps_and_records_when(
    models: ModelHarness,
) -> None:
    publication_id = await _published(models)
    review = await models.service.plan_unpublish(ACTOR, publication_id)

    started = await models.service.unpublish(ACTOR, publication_id, review.id)
    await models.service.wait_for_idle()
    run = await models.service.get_run(ACTOR, started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    assert run.plan_id == review.id
    assert [(step.kind, step.name) for step in run.steps] == [
        (step.kind, step.name) for step in review.steps
    ]
    deleted = models.apim.write_paths("DELETE")
    assert [path.split("?")[0] for path in deleted] == [
        step.resource_id.removeprefix(f"{RESOURCE_ID}/") for step in review.steps
    ]
    publication = await models.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.DRAFT
    assert publication.unpublished_at is not None
    assert not publication.has_applied_api()
    assert publication.created_resources() == []


async def test_republishing_clears_when_it_was_unpublished(models: ModelHarness) -> None:
    publication_id = await _published(models)
    await reviewed_unpublish(models.service, ACTOR, publication_id)
    await models.service.wait_for_idle()

    run = await models.apply(publication_id)

    assert run.status == PublishRunStatus.SUCCEEDED  # type: ignore[attr-defined]
    publication = await models.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.unpublished_at is None
    assert publication.has_applied_api()


async def test_unpublish_refuses_a_plan_the_publication_has_outgrown(
    models: ModelHarness,
) -> None:
    publication_id = await _published(models)
    review = await models.service.plan_unpublish(ACTOR, publication_id)
    publication = await models.service.get_publication(ACTOR, publication_id)
    # An apply since the review recorded one more resource MOSAIC would now delete.
    await models.gateway_repository.record_publication_state(
        publication.model_copy(
            update={
                "resources": [
                    *publication.resources,
                    PublishedResource(
                        kind=PublishedResourceKind.SUBSCRIPTION,
                        name="mosaic-extra",
                        resource_id=f"{RESOURCE_ID}/subscriptions/mosaic-extra",
                        created_by_mosaic=True,
                    ),
                ]
            }
        )
    )

    with pytest.raises(ConflictError) as refused:
        await models.service.unpublish(ACTOR, publication_id, review.id)

    assert refused.value.details == {"planId": review.id, "reason": "stalePlan"}
    assert models.apim.write_paths() == []
    assert await models.gateway_repository.get_publication_lock(
        ACTOR.tenant_id, publication_id
    ) is None
    fresh = await models.service.plan_unpublish(ACTOR, publication_id)
    assert "mosaic-extra" in [step.name for step in fresh.steps]


async def test_turning_on_governed_access_makes_an_unpublish_plan_stale(
    models: ModelHarness,
) -> None:
    publication_id = await _published(models)
    review = await models.service.plan_unpublish(ACTOR, publication_id)
    await models.service.update(
        ACTOR, publication_id, PublicationUpdate(governed_access=ModelAccessSettings())
    )

    with pytest.raises(ConflictError, match="Review a new plan"):
        await models.service.unpublish(ACTOR, publication_id, review.id)

    assert models.apim.write_paths() == []


async def test_the_plan_names_what_mosaic_did_not_create(models: ModelHarness) -> None:
    models.apim.seed(BACKEND_SUFFIX)
    publication_id = await _published(models)

    review = await models.service.plan_unpublish(ACTOR, publication_id)

    assert PublishedResourceKind.BACKEND not in {step.kind for step in review.steps}
    assert f"MOSAIC didn't create backend {API_NAME}, so it stays in API Management." in (
        review.warnings
    )
    await models.service.unpublish(ACTOR, publication_id, review.id)
    await models.service.wait_for_idle()
    deleted = models.apim.write_paths("DELETE")
    assert SUBSCRIPTION_SUFFIX in deleted
    assert API_SUFFIX in deleted
    assert FRAGMENT_SUFFIX in deleted
    assert BACKEND_SUFFIX not in deleted


async def test_nothing_to_unpublish_is_refused_before_a_plan(models: ModelHarness) -> None:
    publication_id = await models.publish()

    with pytest.raises(ConflictError, match="nothing to remove"):
        await models.service.plan_unpublish(ACTOR, publication_id)


async def test_publishing_again_over_an_unpublished_governed_model_says_to_re_plan(
    governed: GovernedHarness,
) -> None:
    await governed.govern()
    await governed.apply()
    await reviewed_unpublish(governed.service, GOVERNED_ACTOR, governed.publication_id)
    await governed.service.wait_for_idle()
    publication = await governed.service.get_publication(GOVERNED_ACTOR, governed.publication_id)

    with pytest.raises(ConflictError, match="Re-plan that publication"):
        await governed.service.create(
            GOVERNED_ACTOR,
            PublicationCreate(
                gateway_id=publication.gateway_id,
                model_endpoint_id=publication.model_endpoint_id,
                deployment_name=publication.deployment_name,
                enforcement=publication.enforcement,
            ),
        )


async def test_a_governed_unpublish_plan_says_who_loses_access(
    governed: GovernedHarness,
) -> None:
    user = await governed.grant()
    application = await governed.grant(APPLICATION, application=True)
    await governed.govern()
    assert (await governed.apply()).status == PublishRunStatus.SUCCEEDED
    await governed.key(user, application)

    review = await governed.service.plan_unpublish(GOVERNED_ACTOR, governed.publication_id)

    assert review.access_snapshot is not None
    assert {grant.entitlement_id for grant in review.access_snapshot.grants if grant.enabled} == {
        user.id,
        application.id,
    }
    # The API goes first, while the deny policy is still attached to it.
    assert review.steps[0].kind == PublishedResourceKind.API
    subscriptions = {
        step.entitlement_id: step
        for step in review.steps
        if step.kind == PublishedResourceKind.SUBSCRIPTION and step.entitlement_id
    }
    assert set(subscriptions) == {user.id, application.id}
    assert subscriptions[user.id].name == governed.subscription(user)
    assert subscriptions[user.id].reason == (
        f"Delete Principal {USER}'s subscription. Its keys stop working."
    )
    assert any("refuses every call" in warning for warning in review.warnings)


async def test_access_applied_after_the_review_makes_a_governed_plan_stale(
    governed: GovernedHarness,
) -> None:
    await governed.grant()
    await governed.govern()
    await governed.apply()
    review = await governed.service.plan_unpublish(GOVERNED_ACTOR, governed.publication_id)
    newcomer = await governed.grant(APPLICATION, application=True)
    assert (await governed.apply()).status == PublishRunStatus.SUCCEEDED
    governed.apim.writes.clear()

    with pytest.raises(ConflictError) as refused:
        await governed.service.unpublish(GOVERNED_ACTOR, governed.publication_id, review.id)

    assert refused.value.details["reason"] == "stalePlan"
    assert governed.apim.write_paths() == []
    fresh = await governed.service.plan_unpublish(GOVERNED_ACTOR, governed.publication_id)
    assert fresh.access_snapshot is not None
    assert newcomer.id in {grant.entitlement_id for grant in fresh.access_snapshot.grants}
    await governed.service.unpublish(GOVERNED_ACTOR, governed.publication_id, fresh.id)
    await governed.service.wait_for_idle()
    revoked = await governed.grants.get_entitlement(GOVERNED_ACTOR, newcomer.id)
    assert revoked.runtime is not None and revoked.runtime.status == "revoked"


async def test_mcp_unpublish_is_planned_reviewed_and_refused_when_stale(mcp: McpHarness) -> None:
    publication_id = await mcp.create()
    publish_plan = await mcp.service.plan(MCP_ACTOR, publication_id)
    await mcp.service.apply(MCP_ACTOR, publication_id, publish_plan.id)
    await mcp.service.wait_for_idle()
    mcp.apim.writes.clear()
    before = await mcp.service.get_publication(MCP_ACTOR, publication_id)

    with pytest.raises(ConflictError) as unreviewed:
        await mcp.service.unpublish(MCP_ACTOR, publication_id)
    review = await mcp.service.plan_unpublish(MCP_ACTOR, publication_id)

    assert unreviewed.value.details["reason"] == "planRequired"
    assert await mcp.service.get_publication(MCP_ACTOR, publication_id) == before
    assert mcp.apim.write_paths() == []
    assert (review.target, review.operation) == ("mcp", "unpublish")
    assert review.mcp_access_snapshot == before.applied_access
    assert review.steps[0].reason == "Delete the MCP API. The gateway stops serving the server."
    assert any("refuses every call" in warning for warning in review.warnings)
    with pytest.raises(ConflictError, match="apply runs only publish plans"):
        await mcp.service.apply(MCP_ACTOR, publication_id, review.id)
    with pytest.raises(NotFoundError):
        await mcp.service.unpublish(MCP_ACTOR, publication_id, publish_plan.id)

    await mcp.gateway_repository.record_mcp_publication_state(
        before.model_copy(update={"resources": before.resources[:-1]})
    )
    with pytest.raises(ConflictError) as stale:
        await mcp.service.unpublish(MCP_ACTOR, publication_id, review.id)
    assert stale.value.details["reason"] == "stalePlan"
    assert mcp.apim.write_paths() == []
    await mcp.gateway_repository.record_mcp_publication_state(before)

    run = await mcp.service.unpublish(MCP_ACTOR, publication_id, review.id)
    await mcp.service.wait_for_idle()

    assert (await mcp.service.get_run(MCP_ACTOR, publication_id, run.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    unpublished = await mcp.service.get_publication(MCP_ACTOR, publication_id)
    assert unpublished.unpublished_at is not None
    assert not unpublished.has_applied_api()
    republished = await mcp.service.plan(MCP_ACTOR, publication_id)
    await mcp.service.apply(MCP_ACTOR, publication_id, republished.id)
    await mcp.service.wait_for_idle()
    again = await mcp.service.get_publication(MCP_ACTOR, publication_id)
    assert again.unpublished_at is None
    assert again.has_applied_api()


async def test_a_never_applied_mcp_publication_has_nothing_to_unpublish(mcp: McpHarness) -> None:
    publication_id = await mcp.create()

    with pytest.raises(ConflictError, match="nothing to remove"):
        await mcp.service.plan_unpublish(MCP_ACTOR, publication_id)
