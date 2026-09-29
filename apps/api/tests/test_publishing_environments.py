import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import (
    build_arm_client,
    build_endpoint_service,
    build_gateway_service,
)
from mosaic_api.domain import (
    AuditEvent,
    GatewayCreate,
    GatewayUpdate,
    ManagementMode,
    ModelEndpointCreate,
    PublicationCreate,
    PublishRunStatus,
    new_id,
    publication_id,
)
from mosaic_api.environments import EnvironmentCatalog, built_in_environments
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.apim import ApimClient, ApimWriter
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.repositories import (
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import PublishingService
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import endpoint_mutation_scope

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")
DEPLOYMENT = "gpt-4o-prod"


def _audit(action: str = "environment.updated") -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=ACTOR.tenant_id,
        action=action,
        resource_type="test",
        resource_id="test",
        actor_object_id=ACTOR.object_id,
    )


def _request(gateway_id: str, endpoint_id: str, deployment: str = DEPLOYMENT) -> PublicationCreate:
    return PublicationCreate.model_validate(
        {
            "gateway_id": gateway_id,
            "model_endpoint_id": endpoint_id,
            "deployment_name": deployment,
            "enforcement": {
                "counter_key_expression": "@(context.Subscription.Id)",
                "tokens_per_minute": 10000,
            },
        }
    )


class Harness:
    def __init__(self) -> None:
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
        self.aoai = FakeCognitiveServices()
        self.gateway_repository = InMemoryGatewayRepository()
        self.endpoint_repository = InMemoryModelEndpointRepository()
        self.mcp_repository = InMemoryMcpEndpointRepository()
        self.environment_repository = InMemoryEnvironmentRepository(
            self.gateway_repository,
            self.endpoint_repository,
            self.mcp_repository,
        )
        self.gateways = build_gateway_service(self.apim, self.gateway_repository)
        self.endpoints = build_endpoint_service(
            self.aoai,
            repository=self.endpoint_repository,
            gateway_repository=self.gateway_repository,
        )
        arm = build_arm_client(self.apim)
        self.service = PublishingService(
            self.gateway_repository,
            endpoint_repository=self.endpoint_repository,
            client_factory=lambda resource: ApimClient(arm, resource),
            writer_factory=lambda resource: ApimWriter(arm, resource),
            environment_repository=self.environment_repository,
        )
        self.gateway_id = ""
        self.endpoint_id = ""

    async def setup(self) -> None:
        gateway = await self.gateways.register(
            ACTOR, GatewayCreate.model_validate({"azure_resource_id": RESOURCE_ID})
        )
        await self.gateways.sync_now(ACTOR, gateway.id)
        gateway = await self.gateways.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
        )
        self.gateway_id = gateway.id

        endpoint = await self.endpoints.register(
            ACTOR, ModelEndpointCreate.model_validate({"azure_resource_id": AI_RESOURCE_ID})
        )
        await self.endpoints.sync_now(ACTOR, endpoint.id)
        self.endpoint_id = endpoint.id

    async def set_gateway_environment(self, environment: str | None) -> None:
        gateway = await self.gateway_repository.get_gateway(ACTOR.tenant_id, self.gateway_id)
        assert gateway is not None
        await self.gateway_repository.save_gateway(
            gateway.model_copy(update={"environment": environment}), _audit()
        )

    async def set_endpoint_environment(
        self, environment: str | None, endpoint_id: str | None = None
    ) -> None:
        endpoint = await self.endpoint_repository.get_endpoint(
            ACTOR.tenant_id, endpoint_id or self.endpoint_id
        )
        assert endpoint is not None
        await self.endpoint_repository.save_endpoint(
            endpoint.model_copy(update={"environment": environment}), _audit()
        )

    async def publish(self) -> str:
        publication = await self.service.create(
            ACTOR, _request(self.gateway_id, self.endpoint_id)
        )
        return publication.id

    async def plan_apply(self, publication_id: str) -> None:
        plan = await self.service.plan(ACTOR, publication_id)
        run = await self.service.apply(ACTOR, publication_id, plan.id)
        await self.service.wait_for_idle()
        saved = await self.service.get_run(ACTOR, run.id)
        assert saved.status == PublishRunStatus.SUCCEEDED

    async def save_catalog(self, catalog: EnvironmentCatalog) -> EnvironmentCatalog:
        return await self.environment_repository.save_environment_catalog(catalog, _audit())


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.setup()
    return built


async def test_publishable_models_carry_environment_verdicts() -> None:
    built = Harness()
    await built.setup()
    await built.set_gateway_environment("staging")
    catalog = EnvironmentCatalog.new(ACTOR.tenant_id)
    catalog = catalog.model_copy(
        update={
            "environments": [
                item.model_copy(
                    update={
                        "accepts_endpoints_from": ["test"],
                    }
                )
                if item.key == "staging"
                else item
                for item in catalog.environments
            ]
        }
    )
    catalog = await built.save_catalog(catalog)

    original = await built.endpoint_repository.get_endpoint(ACTOR.tenant_id, built.endpoint_id)
    assert original is not None
    await built.set_endpoint_environment("staging")
    clones = {
        "warning": None,
        "blocked": "development",
        "exception": "test",
        "prod-exception": "staging",
    }
    for suffix, environment in clones.items():
        endpoint = original.model_copy(
            update={
                "id": f"endpoint-{suffix}",
                "name": f"Endpoint {suffix}",
                "environment": environment,
            }
        )
        await built.endpoint_repository.save_endpoint(endpoint, _audit())
        await built.endpoint_repository.replace_observed_for_endpoint(
            ACTOR.tenant_id,
            endpoint.id,
            [
                ObservedModelDeployment(
                    id=f"observed-{suffix}",
                    tenant_id=ACTOR.tenant_id,
                    endpoint_id=endpoint.id,
                    snapshot_id=f"snapshot-{suffix}",
                    deployment_name=DEPLOYMENT,
                    model_name="gpt-4o",
                    model_format="OpenAI",
                    capabilities={"chatCompletion": "true"},
                )
            ],
            f"snapshot-{suffix}",
        )

    candidates = await built.service.publishable_models(ACTOR, built.gateway_id)
    by_endpoint = {
        item.endpoint_name: item
        for item in candidates
        if item.deployment_name == DEPLOYMENT
    }

    assert by_endpoint[original.name].environment_verdict.level == "allowed"
    assert by_endpoint["Endpoint warning"].environment_verdict.level == "warning"
    assert by_endpoint["Endpoint blocked"].environment_verdict.level == "blocked"
    exception = by_endpoint["Endpoint exception"].environment_verdict
    assert exception.level == "allowed"
    assert exception.via_exception is True
    production_catalog = catalog.model_copy(
        update={
            "environments": [
                item.model_copy(
                    update={
                        "production": True,
                        "accepts_endpoints_from": ["staging"],
                    }
                )
                if item.key == "production"
                else item.model_copy(update={"production": True})
                if item.key == "staging"
                else item
                for item in catalog.environments
            ]
        }
    )
    await built.save_catalog(production_catalog)
    await built.set_gateway_environment("production")
    production_candidates = await built.service.publishable_models(ACTOR, built.gateway_id)
    production_exception = next(
        item
        for item in production_candidates
        if item.endpoint_name == "Endpoint prod-exception" and item.deployment_name == DEPLOYMENT
    ).environment_verdict
    assert production_exception.level == "allowed"
    assert production_exception.via_exception is True


async def test_create_refuses_blocked_pair_and_allows_warning_pair(harness: Harness) -> None:
    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("development")

    with pytest.raises(ConflictError) as error:
        await harness.service.create(ACTOR, _request(harness.gateway_id, harness.endpoint_id))

    assert error.value.details["reason"] == "environmentBlocked"
    assert error.value.details["verdict"]["level"] == "blocked"
    assert (
        await harness.gateway_repository.get_publication(
            ACTOR.tenant_id,
            publication_id(ACTOR.tenant_id, harness.gateway_id, harness.endpoint_id, DEPLOYMENT),
        )
        is None
    )

    await harness.set_gateway_environment("development")
    await harness.set_endpoint_environment(None)
    publication = await harness.service.create(
        ACTOR, _request(harness.gateway_id, harness.endpoint_id)
    )
    assert publication.id


async def test_plan_refuses_blocked_pair_and_warns_for_warning_pair(harness: Harness) -> None:
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    assert any("unclassified" in warning for warning in plan.warnings)

    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("development")
    with pytest.raises(ConflictError) as error:
        await harness.service.plan(ACTOR, publication_id)
    assert error.value.details["reason"] == "environmentBlocked"


async def test_apply_rechecks_environment_before_digest(harness: Harness) -> None:
    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("production")
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)

    await harness.set_endpoint_environment("development")

    with pytest.raises(ConflictError) as error:
        await harness.service.apply(ACTOR, publication_id, plan.id)

    assert error.value.details["reason"] == "environmentBlocked"
    assert not harness.apim.writes


async def test_unrelated_catalog_edit_does_not_stale_the_plan(harness: Harness) -> None:
    await harness.set_gateway_environment("development")
    await harness.set_endpoint_environment("development")
    publication_id = await harness.publish()
    plan = await harness.service.plan(ACTOR, publication_id)
    catalog = EnvironmentCatalog.new(ACTOR.tenant_id).model_copy(
        update={
            "environments": [
                item.model_copy(update={"display_name": "Dev"})
                if item.key == "development"
                else item
                for item in built_in_environments()
            ]
        }
    )
    await harness.save_catalog(catalog)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED


async def test_require_classification_blocks_unclassified_create_and_plan(
    harness: Harness,
) -> None:
    await harness.save_catalog(
        EnvironmentCatalog.new(ACTOR.tenant_id).model_copy(
            update={"require_classification": True}
        )
    )

    with pytest.raises(ConflictError) as create_error:
        await harness.service.create(ACTOR, _request(harness.gateway_id, harness.endpoint_id))
    assert create_error.value.details["reason"] == "environmentBlocked"

    second = Harness()
    await second.setup()
    publication_id = await second.publish()
    await second.save_catalog(
        EnvironmentCatalog.new(ACTOR.tenant_id).model_copy(
            update={"require_classification": True}
        )
    )
    with pytest.raises(ConflictError) as plan_error:
        await second.service.plan(ACTOR, publication_id)
    assert plan_error.value.details["reason"] == "environmentBlocked"


async def test_unpublish_ignores_current_blocked_environment(harness: Harness) -> None:
    await harness.set_gateway_environment("production")
    await harness.set_endpoint_environment("production")
    publication_id = await harness.publish()
    await harness.plan_apply(publication_id)
    await harness.set_endpoint_environment("development")

    run = await harness.service.unpublish(ACTOR, publication_id)
    await harness.service.wait_for_idle()

    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED


async def test_create_environment_scope_lease_races_fail_without_writes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mosaic_api.services import model_access

    monkeypatch.setattr(model_access, "SCOPE_LEASE_BACKOFF_SECONDS", 0)
    await harness.gateway_repository.acquire_scope_lease(
        ACTOR.tenant_id, "environments", "other", lease_seconds=60
    )

    with pytest.raises(ConflictError) as error:
        await harness.service.create(ACTOR, _request(harness.gateway_id, harness.endpoint_id))

    assert "Another environment change is in progress" in error.value.message
    assert (
        await harness.gateway_repository.get_publication(
            ACTOR.tenant_id,
            publication_id(ACTOR.tenant_id, harness.gateway_id, harness.endpoint_id, DEPLOYMENT),
        )
        is None
    )
    await harness.gateway_repository.release_scope_lease(ACTOR.tenant_id, "environments", "other")


async def test_create_endpoint_scope_lease_race_fails_without_writes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mosaic_api.services import model_access

    monkeypatch.setattr(model_access, "SCOPE_LEASE_BACKOFF_SECONDS", 0)
    scope = endpoint_mutation_scope(harness.endpoint_id)
    await harness.gateway_repository.acquire_scope_lease(
        ACTOR.tenant_id, scope, "other", lease_seconds=60
    )

    with pytest.raises(ConflictError):
        await harness.service.create(ACTOR, _request(harness.gateway_id, harness.endpoint_id))

    assert (
        await harness.gateway_repository.get_publication(
            ACTOR.tenant_id,
            publication_id(ACTOR.tenant_id, harness.gateway_id, harness.endpoint_id, DEPLOYMENT),
        )
        is None
    )
    await harness.gateway_repository.release_scope_lease(ACTOR.tenant_id, scope, "other")


async def test_create_releases_environment_and_publication_locks(harness: Harness) -> None:
    await harness.publish()

    assert harness.gateway_repository.scope_leases == {}
    assert harness.gateway_repository.publication_locks == {}
