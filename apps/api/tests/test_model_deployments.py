import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any, cast

import httpx
import pytest
from aoai_double import AI_PROJECT_ID, AI_RESOURCE_ID, FakeDeploymentServices
from azure.core.credentials_async import AsyncTokenCredential
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from conftest import _no_sleep, build_aoai_arm_client, build_endpoint_service
from mosaic_api.clouds import cloud_profile
from mosaic_api.domain import AuditEvent, ModelEndpointCreate, ModelEndpointUpdate
from mosaic_api.errors import ConflictError, UpstreamError, ValidationError
from mosaic_api.integrations.aoai.client import CognitiveServicesClient
from mosaic_api.integrations.aoai.writer import ModelDeploymentWriter
from mosaic_api.integrations.apim import ArmClient
from mosaic_api.model_deployments import (
    DeploymentRecoveryRequest,
    DeploymentRunStatus,
    ModelDeploymentCreate,
    ModelDeploymentRun,
    deployment_lock_scope,
)
from mosaic_api.repositories import InMemoryGatewayRepository, InMemoryModelEndpointRepository
from mosaic_api.repositories.cosmos_endpoints import CosmosModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_deployments import ModelDeploymentService
from mosaic_api.services.model_endpoints import ModelEndpointService
from test_cloud_routing import ScopedCredential
from test_cosmos_usage_state import Cosmos
from test_notification_repository import NotificationContainer

ACTOR = Actor(object_id="deployment-admin", tenant_id="tenant-test")


class DeploymentCosmos:
    def __init__(self) -> None:
        self.containers = {
            name: NotificationContainer() for name in ("desired", "audit", "sync", "observed")
        }

    def get_database_client(self, _name: str) -> "DeploymentCosmos":
        return self

    def get_container_client(self, name: str) -> NotificationContainer:
        return self.containers[name]


def cosmos_repository() -> tuple[CosmosModelEndpointRepository, DeploymentCosmos]:
    cosmos = DeploymentCosmos()
    repository = CosmosModelEndpointRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", "sync", "observed"
    )
    return repository, cosmos


def configuration(**changes: Any) -> ModelDeploymentCreate:
    return ModelDeploymentCreate.model_validate(
        {
            "deploymentName": "new-model",
            "model": {"modelFormat": "OpenAI", "modelName": "gpt-4o", "modelVersion": "2024-11-20"},
            "skuName": "GlobalStandard",
            "capacity": 10,
            **changes,
        }
    )


@pytest.fixture
async def estate() -> AsyncIterator[tuple[Any, ...]]:
    fake = FakeDeploymentServices()
    repository = InMemoryModelEndpointRepository()
    gateways = InMemoryGatewayRepository()
    endpoints = build_endpoint_service(fake, repository=repository, gateway_repository=gateways)
    arm = build_aoai_arm_client(fake)
    service = ModelDeploymentService(
        repository,
        gateway_repository=gateways,
        endpoint_service=endpoints,
        client_factory=lambda resource: CognitiveServicesClient(arm, resource),
        writer_factory=lambda resource: ModelDeploymentWriter(arm, resource),
        sleep=_no_sleep,
        poll_attempts=3,
    )
    endpoint = await endpoints.register(
        ACTOR, ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID)
    )
    yield fake, repository, gateways, endpoints, service, endpoint
    await service.aclose()
    await endpoints.aclose()


async def enable(estate: tuple[Any, ...]) -> None:
    await estate[3].update(ACTOR, estate[5].id, ModelEndpointUpdate(management_mode="manage"))


async def finish(service: ModelDeploymentService) -> None:
    await asyncio.gather(*tuple(service._tasks))


async def test_catalogue_is_live_without_sync_and_preserves_read_only_default(estate) -> None:
    fake, _, _, _, service, endpoint = estate
    assert endpoint.management_mode == "observe"
    catalogue = await service.catalogue(ACTOR, endpoint.id)
    assert catalogue.models[0].model_version == "2024-11-20"
    assert len(catalogue.models[0].skus) == 3
    assert catalogue.models[0].skus[0].quota.available == 100
    assert catalogue.access.can_deploy
    assert await estate[3].list_available_models(ACTOR, endpoint.id) == []
    with pytest.raises(ConflictError, match="Manage deployments"):
        await service.plan(ACTOR, endpoint.id, configuration())
    assert fake.writes == []


@pytest.mark.parametrize("permissions_status", [200, 403, 404])
async def test_reader_and_unknown_permissions_cannot_enable_manage(
    estate, permissions_status
) -> None:
    fake, _, _, endpoints, service, endpoint = estate
    fake.permissions = [{"actions": ["*/read"], "notActions": []}]
    fake.permissions_status = permissions_status
    access = await service.access(ACTOR, endpoint.id)
    assert not access.can_deploy
    assert access.evaluation == ("denied" if permissions_status == 200 else "unknown")
    with pytest.raises(ConflictError):
        await endpoints.update(ACTOR, endpoint.id, ModelEndpointUpdate(management_mode="manage"))
    assert (await endpoints.get_endpoint(ACTOR, endpoint.id)).management_mode == "observe"
    assert not fake.writes


async def test_review_apply_polls_accepted_and_refreshes_inventory_once(estate) -> None:
    fake, _, gateways, _, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    assert not fake.writes
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    duplicate = await service.apply(ACTOR, endpoint.id, plan.id)
    assert duplicate.id == run.id
    await finish(service)
    run = await service.get_run(ACTOR, endpoint.id, run.id)
    assert run.status == "succeeded"
    assert run.provisioning_state == "Succeeded"
    assert run.refresh_status == "succeeded"
    assert len(fake.writes) == 1
    assert any(
        item.deployment_name == "new-model"
        for item in await estate[3].list_deployments(ACTOR, endpoint.id)
    )
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        is None
    )
    assert len(await service.list_runs(ACTOR, endpoint.id)) == 1
    assert (await service.apply(ACTOR, endpoint.id, plan.id)).id == run.id
    assert len(fake.writes) == 1


@pytest.mark.parametrize("change", ["quota", "permissions", "name", "model", "mode"])
async def test_apply_revalidates_after_review(estate, change) -> None:
    fake, _, _, endpoints, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    if change == "quota":
        fake.usages[0]["limit"] = 1
    elif change == "permissions":
        fake.permissions = [{"actions": ["*/read"], "notActions": []}]
    elif change == "name":
        fake.deployments.append({"name": "new-model", "properties": {}})
    elif change == "model":
        fake.models = []
    else:
        await endpoints.update(ACTOR, endpoint.id, ModelEndpointUpdate(management_mode="observe"))
    with pytest.raises((ConflictError, ValidationError)):
        await service.apply(ACTOR, endpoint.id, plan.id)
    assert not fake.writes


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "unknown",
        "exhausted",
        "blocked",
        "nonfinite",
        "ambiguous",
        "unmapped",
        "overflow",
    ],
)
async def test_quota_must_be_known_mapped_finite_and_sufficient(estate, case) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    if case == "missing":
        fake.usages = []
    elif case == "unknown":
        fake.fail_always("usages", 403)
    elif case == "exhausted":
        fake.usages[0]["currentValue"] = 100
    elif case == "blocked":
        fake.usages[0]["status"] = "Blocked"
    elif case == "nonfinite":
        fake.usages[0]["limit"] = "NaN"
    elif case == "ambiguous":
        duplicate = deepcopy(fake.usages[0])
        duplicate["name"]["value"] = duplicate["name"]["value"].upper()
        fake.usages.append(duplicate)
    elif case == "unmapped":
        fake.models[0]["skus"][0]["usageName"] = "unadvertised-quota"
    else:
        fake.usages[0]["limit"] = 10**400
    with pytest.raises(ConflictError):
        await service.plan(ACTOR, endpoint.id, configuration())
    assert not fake.writes


async def test_provisioned_checks_selected_region_capacity_and_billable_warning(estate) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    config = configuration(skuName="ProvisionedManaged")
    plan = await service.plan(ACTOR, endpoint.id, config)
    assert plan.checks.available_capacity == 100
    assert any("billable while allocated" in warning for warning in plan.checks.warnings)
    fake.capacities[0]["properties"]["availableCapacity"] = 0
    with pytest.raises(ConflictError, match="provisioned capacity"):
        await service.apply(ACTOR, endpoint.id, plan.id)
    assert not fake.writes


async def test_batch_is_deployable_but_warns_about_synchronous_publishing(estate) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration(skuName="GlobalBatch"))
    assert any("asynchronous batch" in warning for warning in plan.checks.warnings)
    assert not any(path.endswith("/modelCapacities") for path in fake.requests)
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    assert (await service.get_run(ACTOR, endpoint.id, run.id)).status == "succeeded"


@pytest.mark.parametrize("field", ["deprecation", "capacity", "policy"])
async def test_model_eligibility_and_configuration_fail_closed(estate, field) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    changes = {}
    if field == "deprecation":
        fake.models[0]["deprecation"] = {"inference": "2000-01-01T00:00:00Z"}
    elif field == "capacity":
        fake.models[0]["skus"][0]["capacity"]["step"] = 3
        changes = {"capacity": 2}
    else:
        fake.fail_always("raiPolicies", 403)
        changes = {"raiPolicyName": "unreadable"}
    with pytest.raises(ValidationError):
        await service.plan(ACTOR, endpoint.id, configuration(**changes))
    assert not fake.writes


async def test_unknown_after_put_keeps_lock_and_never_retries_write(estate) -> None:
    fake, _, gateways, endpoints, service, endpoint = estate
    await enable(estate)
    fake.timeout_after_write = True
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    interrupted = await service.get_run(ACTOR, endpoint.id, run.id)
    assert interrupted.status == "interrupted"
    assert interrupted.write_attempted
    assert len(fake.writes) == 1
    assert await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
    with pytest.raises(ConflictError):
        await endpoints.update(ACTOR, endpoint.id, ModelEndpointUpdate(management_mode="observe"))
    with pytest.raises(ConflictError):
        await endpoints.delete(ACTOR, endpoint.id)
    fake.accepted_reads = 0
    checked = await service.reconcile(ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest())
    assert checked.status == "interrupted"
    assert "original deployment worker" in checked.error
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        == run.id
    )
    recovered = await service.reconcile(
        ACTOR,
        endpoint.id,
        run.id,
        DeploymentRecoveryRequest(confirm_worker_stopped=True),
    )
    assert recovered.status == "succeeded"
    assert len(fake.writes) == 1
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        is None
    )


@pytest.mark.parametrize("lost_response", [False, True])
async def test_terminal_mismatch_requires_confirmed_recovery_to_release_the_account(
    estate, monkeypatch, lost_response
) -> None:
    fake, _, gateways, endpoints, service, endpoint = estate
    await enable(estate)
    fake.timeout_after_write = lost_response
    fake.accepted_reads = 0
    original_poll = service._poll

    def change_version() -> None:
        fake.deployments[-1]["properties"]["model"]["version"] = "2025-01-01"

    async def changed_poll(*args):
        change_version()
        return await original_poll(*args)

    monkeypatch.setattr(service, "_poll", changed_poll)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    if lost_response:
        change_version()
    resource = deepcopy(fake.deployments[-1])
    interrupted = await service.get_run(ACTOR, endpoint.id, run.id)
    assert interrupted.status == "interrupted"
    checked = await service.reconcile(ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest())
    assert checked.status == "interrupted"
    assert checked.etag == interrupted.etag
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        == run.id
    )
    recovered = await service.reconcile(
        ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest(confirm_worker_stopped=True)
    )
    assert recovered.status == "failed"
    assert recovered.provisioning_state == "Succeeded"
    assert "does not match the reviewed configuration" in recovered.error
    assert len(fake.writes) == 1
    assert fake.deployments[-1] == resource
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        is None
    )
    await endpoints.update(ACTOR, endpoint.id, ModelEndpointUpdate(management_mode="observe"))
    await endpoints.delete(ACTOR, endpoint.id)


@pytest.mark.parametrize("read_stage", ["deployment", "operation"])
@pytest.mark.parametrize("status", [400, 403, 429])
async def test_a_refused_read_after_accepted_put_is_not_a_rejected_create(
    estate,
    read_stage,
    status,
) -> None:
    fake, _, gateways, _, service, endpoint = estate
    await enable(estate)
    if read_stage == "deployment":
        fake.deployment_read_status = status
    else:
        fake.operation_poll_status = status
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    result = await service.get_run(ACTOR, endpoint.id, run.id)
    assert result.status == "interrupted"
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        == run.id
    )
    assert len(fake.writes) == 1
    fake.deployment_read_status = 200
    fake.accepted_reads = 0
    result = await service.reconcile(
        ACTOR,
        endpoint.id,
        run.id,
        DeploymentRecoveryRequest(confirm_worker_stopped=True),
    )
    assert result.status == "succeeded"
    assert len(fake.writes) == 1


@pytest.mark.parametrize("status", [400, 403, 404, 412, 429, 503])
async def test_only_definite_put_rejections_release_the_account(estate, status) -> None:
    fake, _, gateways, _, service, endpoint = estate
    await enable(estate)
    fake.write_status = status
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    result = await service.get_run(ACTOR, endpoint.id, run.id)
    assert result.status == ("interrupted" if status == 503 else "failed")
    owner = await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
    assert owner == (run.id if status == 503 else None)
    assert len(fake.writes) == 1


async def test_the_blocking_operation_remains_findable_beyond_recent_history(estate) -> None:
    fake, repository, _, _, service, endpoint = estate
    await enable(estate)
    fake.timeout_after_write = True
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    for index in range(21):
        historical = ModelDeploymentRun(
            id=f"deploymentrun_other{index}",
            tenant_id=ACTOR.tenant_id,
            endpoint_id=endpoint.id,
            plan_id=f"deploymentplan_other{index}",
            target=plan.target,
            configuration=plan.configuration,
            actor_object_id=ACTOR.object_id,
            status=DeploymentRunStatus.FAILED,
        )
        await repository.create_model_deployment_run(
            historical,
            service._audit(ACTOR, "modelDeployment.requested", historical.id, endpoint.id),
        )
    assert run.id not in {
        item.id
        for item in await repository.list_model_deployment_runs(ACTOR.tenant_id, endpoint.id)
    }
    assert (await service.list_runs(ACTOR, endpoint.id))[0].id == run.id
    assert (await service.catalogue(ACTOR, endpoint.id)).blocking_run_id == run.id
    fake.accepted_reads = 0
    await service.reconcile(
        ACTOR,
        endpoint.id,
        run.id,
        DeploymentRecoveryRequest(confirm_worker_stopped=True),
    )
    assert (await service.catalogue(ACTOR, endpoint.id)).blocking_run_id is None
    assert len(fake.writes) == 1


async def test_absent_recovery_requires_worker_and_pending_operation_confirmations(estate) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    fake.timeout_after_write = True
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    assert len(fake.writes) == 1
    fake.deployments = []
    result = await service.reconcile(ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest())
    assert result.status == "interrupted"
    result = await service.reconcile(
        ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest(confirm_worker_stopped=True)
    )
    assert result.status == "interrupted"
    result = await service.reconcile(
        ACTOR,
        endpoint.id,
        run.id,
        DeploymentRecoveryRequest(confirm_worker_stopped=True, confirm_no_pending_operation=True),
    )
    assert result.status == "failed"
    assert len(fake.writes) == 1


async def test_marketplace_rejection_is_explicit_and_never_accepts_terms(estate) -> None:
    fake, _, gateways, _, service, endpoint = estate
    await enable(estate)
    fake.write_status = 400
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    result = await service.get_run(ACTOR, endpoint.id, run.id)
    assert result.status == "failed"
    assert "Marketplace" in result.error
    assert len(fake.writes) == 1
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        is None
    )


async def test_successful_provisioning_with_failed_sync_can_refresh_without_create(estate) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    fake.fail_always("deployments", 403)
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    result = await service.get_run(ACTOR, endpoint.id, run.id)
    assert result.status == "succeeded"
    assert result.refresh_status == "failed"
    fake.persistent_failures.pop("deployments")
    result = await service.retry_refresh(ACTOR, endpoint.id, run.id)
    assert result.refresh_status == "succeeded"
    assert len(fake.writes) == 1


@pytest.mark.parametrize("refresh_stage", ["initial", "retry"])
async def test_inventory_refresh_rechecks_a_stored_read_denial(estate, refresh_stage) -> None:
    fake, repository, _, endpoints, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    if refresh_stage == "retry":
        fake.fail_always("deployments", 403)
        run = await service.apply(ACTOR, endpoint.id, plan.id)
        await finish(service)
        assert (await service.get_run(ACTOR, endpoint.id, run.id)).refresh_status == "failed"
        fake.persistent_failures.pop("deployments")
    current = await endpoints.get_endpoint(ACTOR, endpoint.id)
    await repository.record_endpoint_state(
        current.model_copy(update={"access": current.access.model_copy(update={"can_read": False})})
    )
    if refresh_stage == "initial":
        run = await service.apply(ACTOR, endpoint.id, plan.id)
        await finish(service)
        result = await service.get_run(ACTOR, endpoint.id, run.id)
    else:
        result = await service.retry_refresh(ACTOR, endpoint.id, run.id)
    assert result.status == "succeeded"
    assert result.refresh_status == "succeeded"
    assert (await endpoints.get_endpoint(ACTOR, endpoint.id)).access.can_read
    assert len(fake.writes) == 1


async def test_refresh_claim_is_honored_by_other_workers_and_reconciliation(
    estate, monkeypatch
) -> None:
    fake, repository, gateways, endpoints, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    fake.fail_always("deployments", 403)
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    fake.persistent_failures.pop("deployments")
    other = ModelDeploymentService(
        repository,
        gateway_repository=gateways,
        endpoint_service=endpoints,
        client_factory=service._client_factory,
        writer_factory=service._writer_factory,
    )
    started, release = asyncio.Event(), asyncio.Event()
    original_sync = endpoints.sync_now

    async def blocked_sync(*args):
        started.set()
        await release.wait()
        return await original_sync(*args)

    monkeypatch.setattr(endpoints, "sync_now", blocked_sync)
    retry = asyncio.create_task(service.retry_refresh(ACTOR, endpoint.id, run.id))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        claimed = await service.get_run(ACTOR, endpoint.id, run.id)
        assert claimed.refresh_status == "running"
        with pytest.raises(ConflictError, match="still refreshing"):
            await service.retry_refresh(ACTOR, endpoint.id, run.id)
        with pytest.raises(ConflictError, match="may still be running"):
            await other.retry_refresh(ACTOR, endpoint.id, run.id)
        with pytest.raises(ConflictError, match="still executing"):
            await service.reconcile(
                ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest(confirm_worker_stopped=True)
            )
        checked = await other.reconcile(ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest())
        assert checked.refresh_status == "running"
        assert "Verify that it stopped" in checked.refresh_error
        assert (await other.get_run(ACTOR, endpoint.id, run.id)).etag == claimed.etag
    finally:
        release.set()
        await retry
        await other.aclose()
    assert (await service.get_run(ACTOR, endpoint.id, run.id)).refresh_status == "succeeded"
    assert run.id not in service._active
    assert len(fake.writes) == 1


async def test_competing_refresh_claims_cannot_overwrite_the_successful_result(
    estate, monkeypatch
) -> None:
    fake, repository, gateways, endpoints, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    fake.fail_always("deployments", 403)
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await finish(service)
    fake.persistent_failures.pop("deployments")
    other = ModelDeploymentService(
        repository,
        gateway_repository=gateways,
        endpoint_service=endpoints,
        client_factory=service._client_factory,
        writer_factory=service._writer_factory,
    )
    claims = 0
    both_read = asyncio.Event()
    original_save = repository.save_model_deployment_run
    original_sync = endpoints.sync_now
    refreshes = 0

    async def competing_claim(candidate, audit_event=None):
        nonlocal claims
        if candidate.refresh_status == "running":
            claims += 1
            if claims == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=5)
        return await original_save(candidate, audit_event)

    async def count_sync(*args):
        nonlocal refreshes
        refreshes += 1
        return await original_sync(*args)

    monkeypatch.setattr(repository, "save_model_deployment_run", competing_claim)
    monkeypatch.setattr(endpoints, "sync_now", count_sync)
    try:
        results = await asyncio.gather(
            service.retry_refresh(ACTOR, endpoint.id, run.id),
            other.retry_refresh(ACTOR, endpoint.id, run.id),
            return_exceptions=True,
        )
    finally:
        await other.aclose()
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert sum(isinstance(result, ModelDeploymentRun) for result in results) == 1
    assert claims == 2
    assert refreshes == 1
    assert (await service.get_run(ACTOR, endpoint.id, run.id)).refresh_status == "succeeded"
    assert len(fake.writes) == 1


async def test_project_registration_targets_parent_account_for_every_write(estate) -> None:
    fake, repository, gateways, endpoints, service, endpoint = estate
    await endpoints.delete(ACTOR, endpoint.id)
    project = await endpoints.register(ACTOR, ModelEndpointCreate(azure_resource_id=AI_PROJECT_ID))
    await endpoints.update(ACTOR, project.id, ModelEndpointUpdate(management_mode="manage"))
    plan = await service.plan(ACTOR, project.id, configuration())
    run = await service.apply(ACTOR, project.id, plan.id)
    await finish(service)
    assert (await service.get_run(ACTOR, project.id, run.id)).status == "succeeded"
    assert plan.target.account_scope == AI_RESOURCE_ID
    assert not any("/projects/" in path and "/deployments" in path for path in fake.requests)
    stored = await repository.get_model_deployment_run(ACTOR.tenant_id, run.id)
    assert stored is not None
    await repository.save_model_deployment_run(stored.model_copy(update={"error": "new value"}))
    with pytest.raises(ConflictError, match="changed"):
        await repository.save_model_deployment_run(stored)
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(project)) is None
    )


@pytest.mark.parametrize(
    "case",
    [
        "missing-version",
        "fractional-bound",
        "zero-step",
        "reversed-bounds",
        "invalid-choices",
        "duplicate-sku",
        "duplicate-model",
    ],
)
async def test_ambiguous_or_malformed_catalogue_cannot_be_reviewed(estate, case) -> None:
    fake, _, _, _, service, endpoint = estate
    await enable(estate)
    model = fake.models[0]
    capacity = model["skus"][0]["capacity"]
    if case == "missing-version":
        model.pop("version")
    elif case == "fractional-bound":
        capacity["minimum"] = 1.5
    elif case == "zero-step":
        capacity["step"] = 0
    elif case == "reversed-bounds":
        capacity.update(minimum=100, maximum=1)
    elif case == "invalid-choices":
        capacity["allowedValues"] = [True, 10]
    elif case == "duplicate-sku":
        model["skus"].append(deepcopy(model["skus"][0]))
    else:
        fake.models.append(deepcopy(model))
    with pytest.raises(UpstreamError):
        await service.plan(ACTOR, endpoint.id, configuration())
    assert not fake.writes


async def test_legacy_envelope_preserves_exact_publisher_and_source_identity(estate) -> None:
    fake, _, _, _, service, endpoint = estate
    fake.models[0].update(
        publisher="Fictional publisher",
        source="fictional-model-source",
        sourceAccount=AI_RESOURCE_ID,
    )
    fake.models = [{"model": fake.models[0]}]
    await enable(estate)
    catalogue = await service.catalogue(ACTOR, endpoint.id)
    model = catalogue.models[0]
    assert model.model_publisher == "Fictional publisher"
    assert model.source == "fictional-model-source"
    assert model.source_account == AI_RESOURCE_ID
    with pytest.raises(ValidationError, match="model"):
        await service.plan(ACTOR, endpoint.id, configuration())
    config = configuration(
        model={
            "modelFormat": model.model_format,
            "modelName": model.model_name,
            "modelVersion": model.model_version,
            "modelPublisher": model.model_publisher,
            "source": model.source,
            "sourceAccount": model.source_account,
        }
    )
    plan = await service.plan(ACTOR, endpoint.id, config)
    assert plan.configuration.model.azure_payload()["sourceAccount"] == AI_RESOURCE_ID
    assert not fake.writes


async def test_cloud_calls_do_not_hold_record_leases_and_write_lock_has_a_persisted_owner(
    estate,
    monkeypatch,
) -> None:
    _, repository, gateways, endpoints, service, endpoint = estate
    scope = deployment_lock_scope(endpoint)
    original_reader = service._client_factory
    original_acquire = gateways.acquire_publication_lock

    def reader(resource):
        assert (ACTOR.tenant_id, scope) not in gateways.scope_leases
        return original_reader(resource)

    async def acquire(tenant, lock_scope, owner):
        if lock_scope == scope:
            assert await repository.get_model_deployment_run(tenant, owner) is not None
        await original_acquire(tenant, lock_scope, owner)

    monkeypatch.setattr(endpoints, "_client_factory", reader)
    monkeypatch.setattr(service, "_client_factory", reader)
    monkeypatch.setattr(gateways, "acquire_publication_lock", acquire)
    await enable(estate)
    before_version = repository._endpoint_versions[endpoint.id]
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    assert not gateways.publication_locks
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    assert repository._endpoint_versions[endpoint.id] > before_version
    await finish(service)
    assert (await service.get_run(ACTOR, endpoint.id, run.id)).status == "succeeded"
    assert not gateways.scope_leases


@pytest.mark.parametrize("change", [{"name": "A renamed endpoint"}, {"management_mode": "observe"}])
async def test_endpoint_updates_keep_the_version_read_before_checking_the_lock(
    estate, monkeypatch, change
) -> None:
    fake, _, gateways, original_endpoints, _, endpoint = estate
    repository, _cosmos = cosmos_repository()
    await repository.create_endpoint(
        endpoint.model_copy(update={"management_mode": "manage"}),
        original_endpoints._audit(ACTOR, "modelEndpoint.registered", endpoint.id),
    )
    endpoints = build_endpoint_service(fake, repository=repository, gateway_repository=gateways)
    original_lock = gateways.get_publication_lock
    scope = deployment_lock_scope(endpoint)

    async def lock_claimed_after_the_check(tenant_id, lock_scope):
        owner = await original_lock(tenant_id, lock_scope)
        if lock_scope == scope and owner is None:
            # The updater's record lease expired while it was paused after this read.
            await gateways.acquire_publication_lock(tenant_id, scope, "deploymentrun_other")
            current = await repository.get_endpoint(tenant_id, endpoint.id)
            await repository.save_endpoint(
                current,
                ModelDeploymentService._audit(
                    ACTOR, "modelDeployment.locked", "deploymentrun_other", endpoint.id
                ),
            )
        return owner

    monkeypatch.setattr(gateways, "get_publication_lock", lock_claimed_after_the_check)
    try:
        with pytest.raises(ConflictError, match="endpoint changed"):
            await endpoints.update(ACTOR, endpoint.id, ModelEndpointUpdate.model_validate(change))
        stored = await repository.get_endpoint(ACTOR.tenant_id, endpoint.id)
        assert stored.name == endpoint.name
        assert stored.management_mode == "manage"
        assert await original_lock(ACTOR.tenant_id, scope) == "deploymentrun_other"
    finally:
        await endpoints.aclose()


@pytest.mark.parametrize("crash_stage", ["before-lock", "after-lock"])
async def test_a_crash_before_worker_start_always_leaves_recoverable_history(
    estate,
    monkeypatch,
    crash_stage,
) -> None:
    fake, repository, gateways, _, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())

    async def interrupted(*args, **kwargs):
        raise asyncio.CancelledError

    if crash_stage == "before-lock":
        monkeypatch.setattr(gateways, "acquire_publication_lock", interrupted)
    else:
        monkeypatch.setattr(repository, "save_endpoint", interrupted)
    with pytest.raises(asyncio.CancelledError):
        await service.apply(ACTOR, endpoint.id, plan.id)
    runs = await service.list_runs(ACTOR, endpoint.id)
    assert len(runs) == 1
    assert not runs[0].write_attempted
    assert not gateways.scope_leases
    recovered = await service.reconcile(
        ACTOR,
        endpoint.id,
        runs[0].id,
        DeploymentRecoveryRequest(confirm_worker_stopped=True, confirm_no_pending_operation=True),
    )
    assert recovered.status == "failed"
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        is None
    )
    assert (await service.apply(ACTOR, endpoint.id, plan.id)).id == recovered.id
    assert not fake.writes


async def test_shutdown_during_refresh_preserves_azure_success_and_recovers_without_put(
    estate,
    monkeypatch,
) -> None:
    fake, _, gateways, endpoints, service, endpoint = estate
    await enable(estate)
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    original_sync = endpoints.sync_now
    refreshing = asyncio.Event()

    async def blocked_sync(*args, **kwargs):
        refreshing.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(endpoints, "sync_now", blocked_sync)
    run = await service.apply(ACTOR, endpoint.id, plan.id)
    await asyncio.wait_for(refreshing.wait(), timeout=5)
    await service.aclose()
    result = await service.get_run(ACTOR, endpoint.id, run.id)
    assert result.status == "succeeded"
    assert result.refresh_status == "running"
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        == run.id
    )
    checked = await service.reconcile(ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest())
    assert checked.refresh_status == "running"
    assert checked.etag == result.etag
    with pytest.raises(ConflictError, match="may still be running"):
        await service.retry_refresh(ACTOR, endpoint.id, run.id)
    recovered = await service.reconcile(
        ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest(confirm_worker_stopped=True)
    )
    assert recovered.status == "succeeded"
    assert recovered.refresh_status == "failed"
    assert (
        await gateways.get_publication_lock(ACTOR.tenant_id, deployment_lock_scope(endpoint))
        is None
    )
    monkeypatch.setattr(endpoints, "sync_now", original_sync)
    result = await service.retry_refresh(ACTOR, endpoint.id, run.id)
    assert result.refresh_status == "succeeded"
    assert (
        await service.reconcile(ACTOR, endpoint.id, run.id, DeploymentRecoveryRequest())
    ).status == "succeeded"
    assert len(fake.writes) == 1


@pytest.mark.parametrize("cloud", ["commercial", "government"])
async def test_catalogue_creation_and_refresh_use_the_stored_cloud_and_connection(cloud) -> None:
    fake = FakeDeploymentServices()
    requests: list[httpx.Request] = []
    reads, writes = [], []
    profile = cloud_profile(cloud)
    connection = f"connection-{cloud}"
    credential = ScopedCredential(f"{cloud}-deployment-principal")

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        response = fake.handler(request)
        if cloud == "government" and request.url.path == AI_RESOURCE_ID:
            payload = response.json()
            payload["properties"]["endpoint"] = "https://contoso-aoai.openai.azure.us/"
            return httpx.Response(response.status_code, json=payload)
        return response

    async def validate(tenant, selected_connection, selected_cloud):
        assert tenant == ACTOR.tenant_id
        assert selected_connection == connection
        assert selected_cloud == cloud

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        arm = ArmClient(
            cast(AsyncTokenCredential, credential),
            client=http,
            base_url=profile.arm_endpoint,
            sleep=_no_sleep,
        )

        def reader(resource):
            reads.append((resource.azure_cloud, resource.azure_connection_id))
            return CognitiveServicesClient(arm, resource)

        def writer(resource):
            writes.append((resource.azure_cloud, resource.azure_connection_id))
            return ModelDeploymentWriter(arm, resource)

        repository = InMemoryModelEndpointRepository()
        gateways = InMemoryGatewayRepository()
        endpoints = ModelEndpointService(
            repository,
            gateway_repository=gateways,
            client_factory=reader,
            connection_validator=validate,
        )
        service = ModelDeploymentService(
            repository,
            gateway_repository=gateways,
            endpoint_service=endpoints,
            client_factory=reader,
            writer_factory=writer,
            connection_validator=validate,
            sleep=_no_sleep,
            poll_attempts=3,
        )
        try:
            endpoint = await endpoints.register(
                ACTOR,
                ModelEndpointCreate(
                    azure_resource_id=AI_RESOURCE_ID,
                    azure_cloud=cloud,
                    azure_connection_id=connection,
                ),
            )
            await endpoints.update(
                ACTOR, endpoint.id, ModelEndpointUpdate(management_mode="manage")
            )
            plan = await service.plan(ACTOR, endpoint.id, configuration())
            run = await service.apply(ACTOR, endpoint.id, plan.id)
            await finish(service)
            result = await service.get_run(ACTOR, endpoint.id, run.id)
            assert result.status == "succeeded"
            assert result.refresh_status == "succeeded"
            assert plan.target.azure_cloud == cloud
            assert plan.target.azure_connection_id == connection
        finally:
            await service.aclose()
            await endpoints.aclose()
    assert len(fake.writes) == 1
    assert reads and set(reads) == {(cloud, connection)}
    assert writes == [(cloud, connection)]
    assert all(
        request.url.host == profile.arm_endpoint.removeprefix("https://") for request in requests
    )
    assert credential.scopes and set(credential.scopes) == {(profile.arm_scope,)}


@pytest.mark.parametrize("audit_failure", [False, True])
async def test_cosmos_plans_runs_tenant_isolation_and_conditional_updates(
    estate,
    audit_failure,
) -> None:
    await enable(estate)
    plan = await estate[4].plan(ACTOR, estate[5].id, configuration())
    cosmos = Cosmos()
    repository = CosmosModelEndpointRepository(
        cast(CosmosClient, cosmos),
        "mosaic",
        "desired",
        "audit",
        "sync",
        "observed",
    )
    await repository.save_model_deployment_plan(plan)
    restored = await repository.get_model_deployment_plan(ACTOR.tenant_id, plan.id)
    assert restored is not None and restored.configuration == plan.configuration
    assert await repository.get_model_deployment_plan("other-tenant", plan.id) is None
    run = ModelDeploymentRun(
        id="deploymentrun_cosmos",
        tenant_id=ACTOR.tenant_id,
        endpoint_id=plan.endpoint_id,
        plan_id=plan.id,
        target=plan.target,
        configuration=plan.configuration,
        actor_object_id=ACTOR.object_id,
    )
    audit = AuditEvent(
        id="audit_deployment",
        tenant_id=ACTOR.tenant_id,
        action="modelDeployment.requested",
        resource_type="modelDeployment",
        resource_id=run.id,
        actor_object_id=ACTOR.object_id,
    )
    if audit_failure:

        async def fail_audit() -> object:
            raise UpstreamError("Audit storage unavailable")

        cosmos.container.before_write = fail_audit
        with pytest.raises(UpstreamError, match="Audit storage"):
            await repository.create_model_deployment_run(run, audit)
        assert await repository.get_model_deployment_run(ACTOR.tenant_id, run.id) is None
    saved = await repository.create_model_deployment_run(run, audit)
    assert saved.etag
    assert await repository.get_model_deployment_run("other-tenant", saved.id) is None
    with pytest.raises(ConflictError, match="already has a run"):
        await repository.create_model_deployment_run(
            run, audit.model_copy(update={"id": "audit_duplicate"})
        )
    updated = await repository.save_model_deployment_run(
        saved.model_copy(update={"error": "new state"})
    )
    assert updated.etag != saved.etag
    with pytest.raises(ConflictError, match="changed"):
        await repository.save_model_deployment_run(saved)
    with pytest.raises(ConflictError, match="Reload"):
        await repository.save_model_deployment_run(run)
    assert (await repository.get_model_deployment_run(ACTOR.tenant_id, run.id)).error == "new state"


@pytest.mark.parametrize("interruption", ["audit-outage", "before-projection", "after-projection"])
async def test_terminal_audit_intent_survives_projection_failure_and_restart(
    estate, monkeypatch, interruption
) -> None:
    await enable(estate)
    service, endpoint = estate[4], estate[5]
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    repository, cosmos = cosmos_repository()
    run = await repository.create_model_deployment_run(
        ModelDeploymentRun(
            id="deploymentrun_audited",
            tenant_id=ACTOR.tenant_id,
            endpoint_id=endpoint.id,
            plan_id=plan.id,
            target=plan.target,
            configuration=plan.configuration,
            actor_object_id=ACTOR.object_id,
        ),
        service._audit(ACTOR, "modelDeployment.requested", "deploymentrun_audited", endpoint.id),
    )
    audit = service._audit(ACTOR, "modelDeployment.succeeded", run.id, endpoint.id)
    terminal = run.model_copy(update={"status": DeploymentRunStatus.SUCCEEDED})
    sync, audit_container = cosmos.containers["sync"], cosmos.containers["audit"]

    async def unavailable(*args, **kwargs):
        raise exceptions.CosmosHttpResponseError(status_code=503)

    async def stopped(*args, **kwargs):
        raise asyncio.CancelledError

    with monkeypatch.context() as patch:
        if interruption == "audit-outage":
            patch.setattr(audit_container, "create_item", unavailable)
            saved = await repository.save_model_deployment_run(terminal, audit)
            assert saved.status == "succeeded"
            assert saved.etag != run.etag
            assert not await repository.ready()
        else:
            if interruption == "before-projection":
                patch.setattr(repository, "_project_audit_event", stopped)
            else:
                patch.setattr(sync, "delete_item", stopped)
            with pytest.raises(asyncio.CancelledError):
                await repository.save_model_deployment_run(terminal, audit)
    stored = await repository.get_model_deployment_run(ACTOR.tenant_id, run.id)
    assert stored.status == "succeeded"
    assert sync.items[(ACTOR.tenant_id, audit.id)]["action"] == "modelDeployment.succeeded"
    assert not cosmos.containers["desired"].items
    restarted = CosmosModelEndpointRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", "sync", "observed"
    )
    assert await restarted.ready()
    assert (ACTOR.tenant_id, audit.id) not in sync.items
    assert audit_container.items[(ACTOR.tenant_id, audit.id)]["resourceId"] == run.id
    assert await restarted.ready()
    assert len(audit_container.items) == 2
    assert not estate[0].writes


async def test_terminal_run_and_audit_intent_roll_back_together_on_a_conflict(estate) -> None:
    await enable(estate)
    service, endpoint = estate[4], estate[5]
    plan = await service.plan(ACTOR, endpoint.id, configuration())
    repository, cosmos = cosmos_repository()
    run = await repository.create_model_deployment_run(
        ModelDeploymentRun(
            id="deploymentrun_atomic",
            tenant_id=ACTOR.tenant_id,
            endpoint_id=endpoint.id,
            plan_id=plan.id,
            target=plan.target,
            configuration=plan.configuration,
            actor_object_id=ACTOR.object_id,
        ),
        service._audit(ACTOR, "modelDeployment.requested", "deploymentrun_atomic", endpoint.id),
    )
    audit = service._audit(ACTOR, "modelDeployment.succeeded", run.id, endpoint.id)
    cosmos.containers["sync"].conflict_index = 1
    terminal = run.model_copy(update={"status": DeploymentRunStatus.SUCCEEDED})
    with pytest.raises(ConflictError, match="run changed"):
        await repository.save_model_deployment_run(terminal, audit)
    stored = await repository.get_model_deployment_run(ACTOR.tenant_id, run.id)
    assert stored.status == "running"
    assert stored.etag == run.etag
    assert (ACTOR.tenant_id, audit.id) not in cosmos.containers["sync"].items
    assert (ACTOR.tenant_id, audit.id) not in cosmos.containers["audit"].items
    saved = await repository.save_model_deployment_run(terminal, audit)
    assert saved.status == "succeeded"
    assert saved.etag != run.etag
    with pytest.raises(ConflictError, match="run changed"):
        await repository.save_model_deployment_run(
            terminal, audit.model_copy(update={"id": "audit_stale"})
        )
    assert (ACTOR.tenant_id, "audit_stale") not in cosmos.containers["sync"].items
    assert (ACTOR.tenant_id, "audit_stale") not in cosmos.containers["audit"].items
