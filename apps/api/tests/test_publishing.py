"""Publishing: deterministic plans, ordered applies, and rollback that deletes only what it made.

These drive the real ``ArmClient``/``ApimWriter``/``PublishingService`` against the API Management
double, so ordering, long-running operations, error mapping, and rollback are exercised rather than
mocked.
"""

import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import build_endpoint_service, build_gateway_service, build_publishing_service
from mosaic_api.domain import (
    GatewayUpdate,
    ManagementMode,
    ModelEndpoint,
    ModelEndpointCreate,
    PublicationCreate,
    PublicationStatus,
    PublicationUpdate,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishRunStatus,
    PublishStepStatus,
    TokenEnforcement,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository
from mosaic_api.services import PublishingService
from mosaic_api.services.directory import Actor

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
DEPLOYMENT = "gpt-4o-prod"

# The names PublishingService derives from the endpoint name and deployment. Asserting on the
# literals keeps the deterministic-naming promise honest rather than restating the algorithm.
API_NAME = "mosaic-contoso-aoai-gpt-4o-prod"
FRAGMENT_SUFFIX = f"policyFragments/{API_NAME}"
BACKEND_SUFFIX = f"backends/{API_NAME}"
API_SUFFIX = f"apis/{API_NAME}"
API_POLICY_SUFFIX = f"apis/{API_NAME}/policies/policy"
PRODUCT_SUFFIX = f"products/{API_NAME}"
PRODUCT_API_SUFFIX = f"products/{API_NAME}/apis/{API_NAME}"
SUBSCRIPTION_SUFFIX = f"subscriptions/{API_NAME}"


def enforcement(**kwargs: object) -> TokenEnforcement:
    payload: dict[str, object] = {
        "counter_key_expression": "@(context.Subscription.Id)",
        "tokens_per_minute": 10000,
    }
    payload.update(kwargs)
    return TokenEnforcement.model_validate(payload)


class Harness:
    """A gateway in manage mode and an endpoint whose deployments have been observed."""

    def __init__(self) -> None:
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
        self.aoai = FakeCognitiveServices()
        self.gateway_repository = InMemoryGatewayRepository()
        self.endpoint_repository = InMemoryModelEndpointRepository()
        self.gateways = build_gateway_service(self.apim, self.gateway_repository)
        self.endpoints = build_endpoint_service(
            self.aoai,
            repository=self.endpoint_repository,
            gateway_repository=self.gateway_repository,
        )
        self.service: PublishingService = build_publishing_service(
            self.apim, self.gateway_repository, self.endpoint_repository
        )
        self.gateway_id = ""
        self.endpoint_id = ""

    async def setup(self, *, manage: bool = True) -> None:
        from mosaic_api.domain import GatewayCreate

        gateway = await self.gateways.register(
            ACTOR, GatewayCreate.model_validate({"azure_resource_id": RESOURCE_ID})
        )
        await self.gateways.sync_now(ACTOR, gateway.id)
        if manage:
            gateway = await self.gateways.update(
                ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
            )
        self.gateway_id = gateway.id

        endpoint: ModelEndpoint = await self.endpoints.register(
            ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
        )
        await self.endpoints.sync_now(ACTOR, endpoint.id)
        self.endpoint_id = endpoint.id

    async def publish(self, **overrides: object) -> str:
        payload: dict[str, object] = {
            "gateway_id": self.gateway_id,
            "model_endpoint_id": self.endpoint_id,
            "deployment_name": DEPLOYMENT,
            "enforcement": enforcement(),
        }
        payload.update(overrides)
        publication = await self.service.create(
            ACTOR, PublicationCreate.model_validate(payload)
        )
        return publication.id

    async def apply(self, publication_id: str) -> object:
        plan = await self.service.plan(ACTOR, publication_id)
        run = await self.service.apply(ACTOR, publication_id, plan.id)
        await self.service.wait_for_idle()
        return await self.service.get_run(ACTOR, run.id)


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.setup()
    return built


async def test_plan_creates_every_resource_in_dependency_order(harness: Harness) -> None:
    publication_id = await harness.publish()

    plan = await harness.service.plan(ACTOR, publication_id)

    assert [step.kind for step in plan.steps] == [
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.BACKEND,
        PublishedResourceKind.API,
        *[PublishedResourceKind.API_OPERATION] * 7,
        PublishedResourceKind.API_POLICY,
        PublishedResourceKind.PRODUCT,
        PublishedResourceKind.PRODUCT_API,
        PublishedResourceKind.SUBSCRIPTION,
    ]
    assert all(step.action == PublishAction.CREATE for step in plan.steps)
    assert all(step.existed is False for step in plan.steps)
    assert plan.digest


async def test_plan_carries_facets_and_never_policy_markup(harness: Harness) -> None:
    publication_id = await harness.publish()

    plan = await harness.service.plan(ACTOR, publication_id)

    summaries = " ".join(facet.summary for facet in plan.facets)
    assert plan.policy_content_sha256
    assert any(facet.kind == "tokenLimit" for facet in plan.facets)
    assert any(facet.managed_by_mosaic for facet in plan.facets)
    assert "<" not in summaries
    assert "policyXml" not in plan.model_dump(by_alias=True)


async def test_plan_is_deterministic(harness: Harness) -> None:
    publication_id = await harness.publish()

    first = await harness.service.plan(ACTOR, publication_id)
    second = await harness.service.plan(ACTOR, publication_id)

    assert first.digest == second.digest
    assert first.id != second.id


async def test_editing_enforcement_changes_the_digest(harness: Harness) -> None:
    publication_id = await harness.publish()
    before = await harness.service.plan(ACTOR, publication_id)

    await harness.service.update(
        ACTOR, publication_id, PublicationUpdate(enforcement=enforcement(tokens_per_minute=99))
    )
    after = await harness.service.plan(ACTOR, publication_id)

    assert before.digest != after.digest


async def test_apply_writes_in_order_and_records_ownership(harness: Harness) -> None:
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.SUCCEEDED
    assert all(step.status == PublishStepStatus.SUCCEEDED for step in run.steps)
    assert harness.apim.write_paths("PUT")[:3] == [
        FRAGMENT_SUFFIX,
        BACKEND_SUFFIX,
        API_SUFFIX,
    ]
    assert harness.apim.write_paths("PUT")[-3:] == [
        PRODUCT_SUFFIX,
        PRODUCT_API_SUFFIX,
        SUBSCRIPTION_SUFFIX,
    ]
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.last_applied_at is not None
    assert {item.kind for item in publication.created_resources()} == {
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.BACKEND,
        PublishedResourceKind.API,
        PublishedResourceKind.API_OPERATION,
        PublishedResourceKind.API_POLICY,
        PublishedResourceKind.PRODUCT,
        PublishedResourceKind.PRODUCT_API,
        PublishedResourceKind.SUBSCRIPTION,
    }


async def test_applying_twice_keeps_ownership_and_unpublish_removes_all(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)
    await harness.apply(publication_id)

    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.resources
    assert all(item.created_by_mosaic for item in publication.resources)
    harness.apim.writes.clear()

    run_started = await harness.service.unpublish(ACTOR, publication_id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, run_started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    deleted = harness.apim.write_paths("DELETE")
    assert SUBSCRIPTION_SUFFIX in deleted
    assert API_SUFFIX in deleted
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.created_resources() == []


async def test_apply_never_reads_subscription_keys(harness: Harness) -> None:
    publication_id = await harness.publish()

    await harness.apply(publication_id)

    assert not any("listSecrets" in path for path in harness.apim.requests)


async def test_replanning_after_apply_reports_updates_not_creates(harness: Harness) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)

    plan = await harness.service.plan(ACTOR, publication_id)

    assert all(step.action == PublishAction.UPDATE for step in plan.steps)
    assert all(step.existed for step in plan.steps)


async def test_long_running_write_is_polled_to_completion(harness: Harness) -> None:
    harness.apim.make_async(API_SUFFIX, polls=2)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.SUCCEEDED
    assert sum(1 for path in harness.apim.requests if "mosaic-test-operations" in path) == 3


async def test_failed_long_running_write_fails_the_step(harness: Harness) -> None:
    harness.apim.make_async(API_SUFFIX, polls=0, result="Failed")
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    failed = [step for step in run.steps if step.status == PublishStepStatus.FAILED]
    assert [step.kind for step in failed] == [PublishedResourceKind.API]


async def test_partial_failure_rolls_back_only_what_it_created(harness: Harness) -> None:
    harness.apim.fail_write(PRODUCT_SUFFIX, 500)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    assert run.rolled_back is True
    assert not run.orphaned_resources
    deleted = harness.apim.write_paths("DELETE")
    # Reverse dependency order, and only resources this apply created.
    assert deleted[0] == API_POLICY_SUFFIX
    assert deleted[-1] == FRAGMENT_SUFFIX
    assert PRODUCT_API_SUFFIX not in deleted
    assert SUBSCRIPTION_SUFFIX not in deleted
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.ROLLED_BACK
    assert publication.created_resources() == []


async def test_resource_appearing_between_plan_and_apply_is_not_overwritten(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    third_party = {"properties": {"displayName": "Third party API"}}
    harness.apim.seed(API_SUFFIX, third_party)

    run_started = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, run_started.id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    api_step = next(step for step in run.steps if step.kind == PublishedResourceKind.API)
    assert api_step.status == PublishStepStatus.FAILED
    assert "appeared after the plan was produced" in (api_step.error or "")
    assert harness.apim.written[API_SUFFIX] == third_party
    assert API_SUFFIX not in harness.apim.write_paths("PUT")
    assert API_SUFFIX not in harness.apim.write_paths("DELETE")


async def test_rollback_never_deletes_a_resource_it_only_replaced(harness: Harness) -> None:
    # A backend of the same name already exists. MOSAIC replaces it, then the API write fails.
    harness.apim.seed(BACKEND_SUFFIX)
    harness.apim.fail_write(API_SUFFIX, 500)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    assert BACKEND_SUFFIX not in harness.apim.write_paths("DELETE")
    assert FRAGMENT_SUFFIX in harness.apim.write_paths("DELETE")
    backend_step = next(
        step for step in run.steps if step.kind == PublishedResourceKind.BACKEND
    )
    assert backend_step.status == PublishStepStatus.SKIPPED
    assert backend_step.created_by_mosaic is False
    assert any("cannot restore their previous contents" in error for error in run.errors)


async def test_rollback_failure_is_reported_with_the_orphans(harness: Harness) -> None:
    harness.apim.fail_write(PRODUCT_SUFFIX, 500)
    # The fragment is created successfully and then refuses to be deleted, so the rollback of a
    # failed apply itself fails and has to say precisely what it left behind.
    harness.apim.fail_delete(FRAGMENT_SUFFIX, 500)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLBACK_FAILED
    assert [item.name for item in run.orphaned_resources] == [API_NAME]
    assert any("rollback of" in error for error in run.errors)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.FAILED
    assert [item.name for item in publication.created_resources()] == [API_NAME]


async def test_a_stale_plan_is_rejected(harness: Harness) -> None:
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.service.update(
        ACTOR, publication_id, PublicationUpdate(enforcement=enforcement(tokens_per_minute=1))
    )

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(ACTOR, publication_id, plan.id)

    assert "re-plan" in str(error.value.message).casefold()
    assert not harness.apim.writes


async def test_apply_requires_a_plan(harness: Harness) -> None:
    publication_id = await harness.publish()

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(ACTOR, publication_id)

    assert "plan this publication" in str(error.value.message).casefold()


async def test_a_plan_from_another_publication_is_rejected(harness: Harness) -> None:
    first = await harness.publish()
    plan = await harness.service.plan(ACTOR, first)
    second = await harness.publish(deployment_name="text-embedding-3-large")

    with pytest.raises(NotFoundError):
        await harness.service.apply(ACTOR, second, plan.id)


async def test_observe_mode_refuses_to_plan() -> None:
    built = Harness()
    await built.setup(manage=False)
    publication_id = await built.publish()

    with pytest.raises(ConflictError) as error:
        await built.service.plan(ACTOR, publication_id)

    assert "observe mode" in str(error.value.message)
    assert not built.apim.writes


async def test_unverified_write_access_refuses_to_plan() -> None:
    built = Harness()
    built.apim.permissions = CONTRIBUTOR_PERMISSIONS
    await built.setup(manage=True)
    gateway = await built.gateways.get_gateway(ACTOR, built.gateway_id)
    denied = gateway.access.model_copy(update={"can_write": False})
    await built.gateway_repository.record_gateway_state(
        gateway.model_copy(update={"access": denied})
    )
    publication_id = await built.publish()

    with pytest.raises(ConflictError) as error:
        await built.service.plan(ACTOR, publication_id)

    assert "cannot write" in str(error.value.message)


async def test_an_api_mosaic_did_not_create_is_never_taken_over(harness: Harness) -> None:
    publication_id = await harness.publish(api_name="chat-api")

    with pytest.raises(ConflictError) as error:
        await harness.service.plan(ACTOR, publication_id)

    assert "did not create" in str(error.value.message)
    assert not harness.apim.writes


async def test_orphaned_non_api_record_does_not_disable_api_takeover_guard(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    await harness.gateway_repository.record_publication_state(
        publication.model_copy(
            update={
                "resources": [
                    PublishedResource(
                        kind=PublishedResourceKind.POLICY_FRAGMENT,
                        name=API_NAME,
                        resource_id=f"{RESOURCE_ID}/{FRAGMENT_SUFFIX}",
                        created_by_mosaic=True,
                    )
                ]
            }
        )
    )
    harness.apim.seed(API_SUFFIX)

    with pytest.raises(ConflictError) as error:
        await harness.service.plan(ACTOR, publication_id)

    assert "did not create" in str(error.value.message)
    assert not harness.apim.writes


async def test_orphaned_non_api_record_does_not_disable_path_guard(
    harness: Harness,
) -> None:
    publication_id = await harness.publish(api_path="openai")
    publication = await harness.service.get_publication(ACTOR, publication_id)
    await harness.gateway_repository.record_publication_state(
        publication.model_copy(
            update={
                "resources": [
                    PublishedResource(
                        kind=PublishedResourceKind.POLICY_FRAGMENT,
                        name="chat-api",
                        resource_id=f"{RESOURCE_ID}/policyFragments/chat-api",
                        created_by_mosaic=True,
                    )
                ]
            }
        )
    )

    with pytest.raises(ConflictError) as error:
        await harness.service.plan(ACTOR, publication_id)

    assert error.value.details["conflictingApi"] == "chat-api"
    assert not harness.apim.writes


async def test_a_path_another_api_already_serves_is_refused(harness: Harness) -> None:
    publication_id = await harness.publish(api_path="openai")

    with pytest.raises(ConflictError) as error:
        await harness.service.plan(ACTOR, publication_id)

    assert error.value.details["conflictingApi"] == "chat-api"


async def test_an_unobserved_deployment_is_refused(harness: Harness) -> None:
    with pytest.raises(ValidationError) as error:
        await harness.publish(deployment_name="not-deployed")

    assert "has not observed" in str(error.value.message)


async def test_a_second_apply_is_refused_while_one_is_running(harness: Harness) -> None:
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.service.apply(ACTOR, publication_id, plan.id)

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(ACTOR, publication_id, plan.id)

    assert "already running" in str(error.value.message)
    await harness.service.wait_for_idle()


async def test_unpublish_removes_only_tracked_resources_in_reverse(harness: Harness) -> None:
    harness.apim.seed(BACKEND_SUFFIX)
    publication_id = await harness.publish()
    await harness.apply(publication_id)
    harness.apim.writes.clear()

    run_started = await harness.service.unpublish(ACTOR, publication_id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, run_started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    deleted = harness.apim.write_paths("DELETE")
    assert deleted[0] == SUBSCRIPTION_SUFFIX
    assert deleted[-1] == FRAGMENT_SUFFIX
    assert BACKEND_SUFFIX not in deleted
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.DRAFT
    assert publication.created_resources() == []


async def test_a_publication_owning_resources_cannot_be_deleted(harness: Harness) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)

    with pytest.raises(ConflictError) as error:
        await harness.service.delete(ACTOR, publication_id)

    assert "unpublish it first" in str(error.value.message).casefold()


async def test_after_two_applies_publication_and_gateway_deletes_are_refused(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)
    await harness.apply(publication_id)

    with pytest.raises(ConflictError) as publication_error:
        await harness.service.delete(ACTOR, publication_id)
    with pytest.raises(ConflictError) as gateway_error:
        await harness.gateways.delete(ACTOR, harness.gateway_id)

    assert "unpublish it first" in str(publication_error.value.message).casefold()
    assert "unpublish them first" in str(gateway_error.value.message).casefold()


async def test_a_gateway_with_published_models_cannot_be_removed(harness: Harness) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)

    with pytest.raises(ConflictError) as error:
        await harness.gateways.delete(ACTOR, harness.gateway_id)

    assert "unpublish them first" in str(error.value.message).casefold()


async def test_subscription_record_survives_when_subscription_requirement_is_removed(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)
    await harness.service.update(
        ACTOR, publication_id, PublicationUpdate(subscription_required=False)
    )
    await harness.apply(publication_id)

    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert any(
        item.kind == PublishedResourceKind.SUBSCRIPTION and item.created_by_mosaic
        for item in publication.resources
    )
    harness.apim.writes.clear()

    run_started = await harness.service.unpublish(ACTOR, publication_id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, run_started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    assert SUBSCRIPTION_SUFFIX in harness.apim.write_paths("DELETE")
    assert SUBSCRIPTION_SUFFIX not in harness.apim.written


async def test_publishable_models_report_runtime_access_without_inventing_it(
    harness: Harness,
) -> None:
    candidates = await harness.service.publishable_models(ACTOR, harness.gateway_id)

    names = [item.deployment_name for item in candidates]
    assert DEPLOYMENT in names
    chosen = next(item for item in candidates if item.deployment_name == DEPLOYMENT)
    assert chosen.suggested_api_name == API_NAME
    assert chosen.publication_id is None
    # No role assignment was seeded, so the gateway is not known to be able to call the model. That
    # must never be reported as a confirmed denial.
    assert chosen.runtime_access is not None
    assert chosen.runtime_access.can_invoke is False


async def test_a_plan_warns_when_the_gateway_cannot_be_shown_to_reach_the_model(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()

    plan = await harness.service.plan(ACTOR, publication_id)

    assert plan.warnings
