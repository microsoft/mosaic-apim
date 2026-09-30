"""Publishing: deterministic plans, ordered applies, and rollback that deletes only what it made.

These drive the real ``ArmClient``/``ApimWriter``/``PublishingService`` against the API Management
double, so ordering, long-running operations, error mapping, and rollback are exercised rather than
mocked.
"""

import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import (
    build_endpoint_service,
    build_gateway_service,
    build_publishing_service,
    reviewed_unpublish,
)
from mosaic_api.domain import (
    ApiShape,
    DeploymentCapability,
    GatewayUpdate,
    ManagementMode,
    ModelEndpoint,
    ModelEndpointCreate,
    Publication,
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
from mosaic_api.services import PublishingService, publishing
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
# What API Management says when a fragment routes to a backend that does not exist, as a publish
# run step reports it. The line and column are the fragment's set-backend-service element.
MISSING_BACKEND_FAILURE = (
    "The Azure operation did not succeed (Failed). ValidationError: One or more fields contain "
    "incorrect values. Detail: Error in element 'set-backend-service' on line 3, column 4: "
    f"Backend with id '{API_NAME}' could not be found."
)


def enforcement(**kwargs: object) -> TokenEnforcement:
    payload: dict[str, object] = {
        "counter_key_expression": "@(context.Subscription.Id)",
        "tokens_per_minute": 10000,
    }
    payload.update(kwargs)
    return TokenEnforcement.model_validate(payload)


class Harness:
    """A gateway in manage mode and an endpoint whose deployments have been observed."""

    def __init__(self, *, ai_kind: str = "OpenAI", apim_sku: str | None = "Developer") -> None:
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS, sku_name=apim_sku)
        self.aoai = FakeCognitiveServices(kind=ai_kind)
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

    # The backend comes first: the fragment routes to it, and API Management rejects a fragment
    # whose set-backend-service names a backend that does not exist yet.
    assert [step.kind for step in plan.steps] == [
        PublishedResourceKind.BACKEND,
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.API,
        *[PublishedResourceKind.API_OPERATION] * 7,
        PublishedResourceKind.API_POLICY,
        PublishedResourceKind.PRODUCT,
        PublishedResourceKind.PRODUCT_API,
        PublishedResourceKind.SUBSCRIPTION,
    ]
    # Rollback and unpublish reverse CREATE_ORDER, so the plan has to follow it exactly.
    ranks = [publishing.CREATE_ORDER.index(step.kind) for step in plan.steps]
    assert ranks == sorted(ranks)
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
        BACKEND_SUFFIX,
        FRAGMENT_SUFFIX,
        API_SUFFIX,
    ]
    assert harness.apim.write_paths("PUT")[-3:] == [
        PRODUCT_SUFFIX,
        PRODUCT_API_SUFFIX,
        SUBSCRIPTION_SUFFIX,
    ]
    # Every write named only resources that already existed: the fragment's backend, the API
    # policy's fragment, the operations' API, the link's product and API, the key's product.
    assert harness.apim.dangling_references == []
    assert FRAGMENT_SUFFIX in harness.apim.written
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


async def test_a_fragment_created_before_its_backend_fails_as_api_management_fails_it(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The order MOSAIC used before: the fragment first, routing to a backend not created yet.
    # API Management accepts that write with 201 and then fails the operation MOSAIC polls.
    desired = publishing._desired_resources
    fragment_kind = PublishedResourceKind.POLICY_FRAGMENT

    def fragment_first(publication: Publication) -> list[publishing._Resource]:
        resources = desired(publication)
        fragments = [item for item in resources if item.kind == fragment_kind]
        return [*fragments, *(item for item in resources if item not in fragments)]

    old_order = (
        fragment_kind,
        *(kind for kind in publishing.CREATE_ORDER if kind != fragment_kind),
    )
    monkeypatch.setattr(publishing, "_desired_resources", fragment_first)
    monkeypatch.setattr(publishing, "CREATE_ORDER", old_order)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    [step] = run.steps
    assert step.kind == PublishedResourceKind.POLICY_FRAGMENT
    assert step.status == PublishStepStatus.FAILED
    assert step.error == MISSING_BACKEND_FAILURE
    assert run.errors == [f"policyFragment {API_NAME}: {MISSING_BACKEND_FAILURE}"]
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.last_error == run.errors[0]
    # As on the live gateway, API Management kept nothing, so there was nothing to roll back.
    assert harness.apim.write_paths() == [FRAGMENT_SUFFIX]
    assert FRAGMENT_SUFFIX not in harness.apim.written
    assert harness.apim.dangling_references == [(FRAGMENT_SUFFIX, BACKEND_SUFFIX)]


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

    run_started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
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
    assert failed[0].error == (
        "The Azure operation did not succeed (Failed) and Azure returned no reason. Check the "
        "Azure activity log for this resource."
    )


async def test_a_failed_operation_carries_azures_reason_into_the_run(harness: Harness) -> None:
    harness.apim.make_async(
        API_SUFFIX,
        polls=0,
        result="Failed",
        error={
            "code": "ValidationError",
            "message": "One or more fields contain incorrect values:",
            "details": [
                {
                    "code": "ValidationError",
                    "target": "path",
                    "message": "Value 'mosaic' is reserved.",
                }
            ],
        },
    )
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    reason = (
        "The Azure operation did not succeed (Failed). ValidationError: One or more fields "
        "contain incorrect values. Detail: Value 'mosaic' is reserved. (target: path)"
    )
    assert run.status == PublishRunStatus.ROLLED_BACK
    api_step = next(step for step in run.steps if step.kind == PublishedResourceKind.API)
    assert api_step.status == PublishStepStatus.FAILED
    assert api_step.error == reason
    # The run's errors and the publication's last error are what the console shows.
    assert run.errors[0] == f"api {API_NAME}: {reason}"
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.last_error == run.errors[0]


async def test_a_fragment_update_azure_rejects_fails_its_step_with_azures_reason(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    assert (await harness.apply(publication_id)).status == PublishRunStatus.SUCCEEDED
    previous = harness.apim.written[FRAGMENT_SUFFIX]
    # The backend is deleted between MOSAIC's update of it and of the fragment that routes to it.
    # API Management answers the fragment's update 200 with a Location header, then fails the
    # operation and keeps the fragment it had.
    harness.apim.remove_before_write(FRAGMENT_SUFFIX, BACKEND_SUFFIX)
    harness.apim.writes.clear()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    step = next(
        step for step in run.steps if step.kind == PublishedResourceKind.POLICY_FRAGMENT
    )
    assert step.action == PublishAction.UPDATE
    assert step.status == PublishStepStatus.FAILED
    assert step.error == MISSING_BACKEND_FAILURE
    assert run.errors[0] == f"policyFragment {API_NAME}: {MISSING_BACKEND_FAILURE}"
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.status == PublicationStatus.ROLLED_BACK
    assert publication.last_error == run.errors[0]
    # Nothing after the fragment was written, and API Management still holds the old fragment.
    assert harness.apim.write_paths() == [BACKEND_SUFFIX, FRAGMENT_SUFFIX]
    assert harness.apim.written[FRAGMENT_SUFFIX] == previous


async def test_a_policy_azure_refuses_fails_its_step_with_azures_reason(
    harness: Harness,
) -> None:
    missing = (
        "Error in element 'include-fragment' on line 3, column 6: Fragment with id "
        f"'{API_NAME}' could not be found."
    )
    harness.apim.fail_write(
        API_POLICY_SUFFIX,
        400,
        error={
            "code": "ValidationError",
            "message": "One or more fields contain incorrect values:",
            "details": [
                {"code": "ValidationError", "target": "include-fragment", "message": missing}
            ],
        },
    )
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    reason = (
        "Azure Resource Manager rejected the request (HTTP 400). ValidationError: One or more "
        f"fields contain incorrect values. Detail: {missing}"
    )
    assert run.status == PublishRunStatus.ROLLED_BACK
    step = next(step for step in run.steps if step.kind == PublishedResourceKind.API_POLICY)
    assert step.status == PublishStepStatus.FAILED
    assert step.error == reason
    assert run.errors[0] == f"apiPolicy policy: {reason}"
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.last_error == run.errors[0]


async def test_policy_markup_in_a_refusal_never_reaches_the_run(harness: Harness) -> None:
    harness.apim.fail_write(
        API_POLICY_SUFFIX,
        400,
        error={
            "code": "ValidationError",
            "message": 'Policy <include-fragment fragment-id="secret-route" /> is not allowed',
        },
    )
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    step = next(step for step in run.steps if step.kind == PublishedResourceKind.API_POLICY)
    assert step.error == (
        "Azure Resource Manager rejected the request (HTTP 400). ValidationError: Policy "
        "[policy markup omitted]."
    )
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert "secret-route" not in run.model_dump_json()
    assert "secret-route" not in publication.model_dump_json()


async def test_partial_failure_rolls_back_only_what_it_created(harness: Harness) -> None:
    harness.apim.fail_write(PRODUCT_SUFFIX, 500)
    publication_id = await harness.publish()

    run = await harness.apply(publication_id)

    assert run.status == PublishRunStatus.ROLLED_BACK
    assert run.rolled_back is True
    assert not run.orphaned_resources
    deleted = harness.apim.write_paths("DELETE")
    # Reverse dependency order, and only resources this apply created. The fragment goes before
    # the backend it routes to.
    assert deleted[0] == API_POLICY_SUFFIX
    assert deleted[-2:] == [FRAGMENT_SUFFIX, BACKEND_SUFFIX]
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


async def test_a_plan_saved_in_the_old_order_is_refused(harness: Harness) -> None:
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    backend, fragment, *rest = plan.steps
    # A plan saved before the backend moved first. Its digest still matches, because the digest
    # covers what to publish rather than the order of the steps.
    await harness.gateway_repository.save_publish_plan(
        plan.model_copy(update={"steps": [fragment, backend, *rest]})
    )

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(ACTOR, publication_id, plan.id)

    assert "re-plan" in str(error.value.message).casefold()
    assert error.value.details == {"planId": plan.id}
    assert not harness.apim.writes
    # The refusal left nothing locked: a fresh plan applies.
    assert (await harness.apply(publication_id)).status == PublishRunStatus.SUCCEEDED


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

    run_started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
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


async def test_unpublish_removes_the_fragment_before_the_backend_it_routes_to(
    harness: Harness,
) -> None:
    publication_id = await harness.publish()
    await harness.apply(publication_id)
    harness.apim.writes.clear()

    run_started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()
    run = await harness.service.get_run(ACTOR, run_started.id)

    assert run.status == PublishRunStatus.SUCCEEDED
    deleted = harness.apim.write_paths("DELETE")
    assert deleted[0] == SUBSCRIPTION_SUFFIX
    assert deleted[-2:] == [FRAGMENT_SUFFIX, BACKEND_SUFFIX]


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

    run_started = await reviewed_unpublish(harness.service, ACTOR, publication_id)
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


async def test_azure_openai_capabilities_keep_their_curated_shape(harness: Harness) -> None:
    candidates = {
        item.deployment_name: item
        for item in await harness.service.publishable_models(ACTOR, harness.gateway_id)
    }

    chat = candidates[DEPLOYMENT]
    embeddings = candidates["text-embedding-3-large"]
    assert chat.capability == DeploymentCapability.CHAT
    assert embeddings.capability == DeploymentCapability.EMBEDDINGS
    for item in (chat, embeddings):
        assert item.api_shape == ApiShape.AZURE_OPENAI
        assert item.publishable is True
        assert item.unpublishable_reason is None
        assert item.token_limits_supported is True
        assert item.token_limits_note is None


CLAUDE = "claude-sonnet-4-5"
FOUNDRY_ORIGIN = "https://contoso-aoai.services.ai.azure.com"


def foundry_deployment(name: str, model_format: str, **capabilities: str) -> dict[str, object]:
    return {
        "name": name,
        "sku": {"name": "GlobalStandard", "capacity": 1},
        "properties": {
            "model": {"format": model_format, "name": name, "version": "1"},
            "provisioningState": "Succeeded",
            "capabilities": capabilities,
        },
    }


async def foundry(*, apim_sku: str | None = "Developer") -> Harness:
    """A Foundry (AI Services) account serving an Anthropic model, a partner model, and models
    MOSAIC has no curated shape for."""

    built = Harness(ai_kind="AIServices", apim_sku=apim_sku)
    built.aoai.deployments = [
        foundry_deployment(CLAUDE, "Anthropic", chatCompletion="true"),
        foundry_deployment("grok-4.3", "xAI", chatCompletion="true"),
        foundry_deployment("gpt-realtime", "OpenAI", realtime="true"),
        foundry_deployment("sora-2", "OpenAI"),
        foundry_deployment("dall-e-3", "OpenAI", imageGenerations="true"),
    ]
    await built.setup()
    return built


async def test_publishable_models_list_every_deployment_with_its_shape_or_reason() -> None:
    built = await foundry()

    candidates = {
        item.deployment_name: item
        for item in await built.service.publishable_models(ACTOR, built.gateway_id)
    }

    # Nothing is hidden: a deployment MOSAIC can't publish is listed with the reason.
    assert set(candidates) == {CLAUDE, "grok-4.3", "gpt-realtime", "sora-2", "dall-e-3"}
    claude = candidates[CLAUDE]
    assert claude.model_format == "Anthropic"
    assert claude.capability == DeploymentCapability.CHAT
    assert claude.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert claude.publishable is True
    assert claude.token_limits_supported is False
    assert "Developer tier" in (claude.token_limits_note or "")
    grok = candidates["grok-4.3"]
    assert grok.model_format == "xAI"
    assert grok.api_shape == ApiShape.FOUNDRY_MODELS
    assert grok.publishable is True
    assert grok.token_limits_supported is True
    assert grok.token_limits_note is None
    for name, capability, reason in (
        ("gpt-realtime", DeploymentCapability.REALTIME, "WebSocket"),
        ("sora-2", DeploymentCapability.VIDEO, "asynchronous jobs API"),
        ("dall-e-3", DeploymentCapability.IMAGE, "images API"),
    ):
        item = candidates[name]
        assert item.capability == capability
        assert item.publishable is False
        assert item.api_shape is None
        assert reason in (item.unpublishable_reason or "")


async def test_a_deployment_without_a_curated_shape_is_refused_with_its_reason() -> None:
    built = await foundry()

    with pytest.raises(ValidationError) as error:
        await built.publish(deployment_name="gpt-realtime")

    assert "WebSocket" in error.value.message
    assert error.value.details["capability"] == "realtime"
    assert not built.apim.writes


async def test_claude_publishes_the_messages_shape_unmetered_on_a_classic_tier() -> None:
    built = await foundry()
    publication_id = await built.publish(deployment_name=CLAUDE, enforcement=None)
    publication = await built.service.get_publication(ACTOR, publication_id)

    plan = await built.service.plan(ACTOR, publication_id)
    run = await built.apply(publication_id)

    assert publication.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert publication.enforcement is None
    assert [step.kind for step in plan.steps].count(PublishedResourceKind.API_OPERATION) == 2
    assert not any(facet.kind == "tokenLimit" for facet in plan.facets)
    assert any("classic tier" in warning for warning in plan.warnings)
    assert run.status == PublishRunStatus.SUCCEEDED
    written = built.apim.written
    assert written[f"backends/{publication.backend_name}"]["properties"]["url"] == FOUNDRY_ORIGIN
    operations = {
        path.rsplit("/", 1)[-1]: body["properties"]["urlTemplate"]
        for path, body in written.items()
        if path.startswith(f"apis/{publication.api_name}/operations/")
    }
    assert operations == {
        "messages": "/anthropic/v1/messages",
        "count-tokens": "/anthropic/v1/messages/count_tokens",
    }
    fragment = written[f"policyFragments/{publication.fragment_name}"]["properties"]["value"]
    assert 'resource="https://ai.azure.com"' in fragment
    assert 'name="anthropic-version"' in fragment
    assert 'name="x-api-key"' in fragment
    assert "llm-token-limit" not in fragment
    assert "llm-emit-token-metric" not in fragment


async def test_claude_refuses_token_limits_on_a_classic_tier() -> None:
    built = await foundry()

    with pytest.raises(ValidationError) as create_error:
        await built.publish(deployment_name=CLAUDE)
    publication_id = await built.publish(deployment_name=CLAUDE, enforcement=None)
    with pytest.raises(ValidationError) as update_error:
        await built.service.update(
            ACTOR, publication_id, PublicationUpdate(enforcement=enforcement())
        )

    for error in (create_error, update_error):
        assert "only on v2 tiers" in error.value.message


async def test_claude_on_a_v2_tier_requires_and_applies_token_limits() -> None:
    built = await foundry(apim_sku="StandardV2")

    with pytest.raises(ValidationError) as error:
        await built.publish(deployment_name=CLAUDE, enforcement=None)
    publication_id = await built.publish(deployment_name=CLAUDE)
    plan = await built.service.plan(ACTOR, publication_id)
    candidates = await built.service.publishable_models(ACTOR, built.gateway_id)

    assert "Token enforcement is required" in error.value.message
    assert any(facet.kind == "tokenLimit" for facet in plan.facets)
    assert not any("v2 tiers" in warning for warning in plan.warnings)
    claude = next(item for item in candidates if item.deployment_name == CLAUDE)
    assert claude.token_limits_supported is True
    assert claude.token_limits_note is None


async def test_claude_is_not_publishable_until_the_gateway_tier_is_known() -> None:
    built = await foundry(apim_sku=None)

    candidates = {
        item.deployment_name: item
        for item in await built.service.publishable_models(ACTOR, built.gateway_id)
    }
    with pytest.raises(ValidationError) as error:
        await built.publish(deployment_name=CLAUDE, enforcement=None)

    assert candidates[CLAUDE].publishable is False
    assert "pricing tier" in (candidates[CLAUDE].unpublishable_reason or "")
    assert "pricing tier" in error.value.message
    # Only the Anthropic decision depends on the tier.
    assert candidates["grok-4.3"].publishable is True
