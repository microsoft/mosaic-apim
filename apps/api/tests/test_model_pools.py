"""Model pools: one vendor's deployments, on many endpoints, behind one API (ADR 0024)."""

import time
import xml.etree.ElementTree as ET
from typing import Any

import pytest
from aoai_double import FakeCognitiveServices
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import build_endpoint_service, build_gateway_service, build_model_pool_service
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    DeclaredDeployment,
    EndpointAuthMode,
    EntitlementSubject,
    Gateway,
    GatewayCreate,
    GatewayRuntimeAccess,
    GatewayUpdate,
    ManagementMode,
    ModelAccessSettings,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    Publication,
    PublicationStatus,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishPlan,
    PublishRun,
    PublishRunStatus,
    PublishStepResult,
    RuntimeAccessEvaluation,
    RuntimeAccessReason,
    deterministic_id,
    general_cost_center_id,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.apim.client import ApimClient
from mosaic_api.integrations.apim.policy_semantics import content_digest
from mosaic_api.integrations.pool_policy import (
    ATTEMPT_TRACE_SUMMARY,
    NOT_FOUND_BODY,
    PoolRoute,
    PoolTarget,
    render_pool_policy,
)
from mosaic_api.model_pools import (
    BreakerPreset,
    ModelPool,
    ModelPoolCreate,
    ModelPoolType,
    ModelPoolUpdate,
    ModelPoolVisibility,
    PoolAccessGrant,
    PoolAccessSnapshot,
    PoolMember,
    PoolModel,
    PoolReference,
    PoolSafeguard,
    PoolSuggestionModel,
    backend_pool_name,
    circuit_breaker,
    member_backend_name,
    model_pool_id,
    pool_key_name,
    pool_model_id,
)
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.repositories import (
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_endpoints import ModelEndpointService
from pydantic import ValidationError as SchemaError

# A tenant ID like Entra's: a governed pool's Entra policy names the tenant whose tokens it takes.
TENANT = "aaaaaaaa-1111-4aaa-8aaa-aaaaaaaaaaaa"
ACTOR = Actor(object_id="admin-object-id", tenant_id=TENANT)
GATEWAY_URL = "https://apim-contoso-dev.azure-api.net"
AOAI_OPERATIONS = {
    "chat-completions",
    "completions",
    "embeddings",
    "images-generations",
    "audio-transcriptions",
    "audio-translations",
}


def _audit(action: str = "test.updated") -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action=action,
        resource_type="test",
        resource_id="test",
        actor_object_id=ACTOR.object_id,
    )


def _deployment(
    name: str,
    model: str,
    *,
    version: str | None = "2024-08-06",
    model_format: str = "OpenAI",
    sku: str = "Standard",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "deployment_name": name,
        "model_name": model,
        "model_version": version,
        "model_format": model_format,
        "sku_name": sku,
        "sku_capacity": 10,
        "provisioning_state": "Succeeded",
        "capabilities": {"chatCompletion": "true"},
        **extra,
    }


def _claude(name: str = "claude-opus-4-5") -> dict[str, Any]:
    return _deployment(
        name, "claude-opus-4-5", version="1", model_format="Anthropic", sku="GlobalStandard"
    )


# A fictional estate: Azure OpenAI in three regions, two Foundry resources serving Claude, and an
# Azure OpenAI resource MOSAIC reaches with a key.
ESTATE: dict[str, dict[str, Any]] = {
    "aoai-east": {
        "provider": ModelProvider.AZURE_OPENAI,
        "location": "eastus2",
        "deployments": [
            _deployment("gpt-4o", "gpt-4o"),
            _deployment("gpt-4o-mini", "gpt-4o-mini", version="2024-07-18"),
            _deployment("gpt-4o-batch", "gpt-4o", sku="GlobalBatch"),
        ],
    },
    "aoai-sweden": {
        "provider": ModelProvider.AZURE_OPENAI,
        "location": "swedencentral",
        "deployments": [
            _deployment("gpt-4o", "gpt-4o", sku="GlobalStandard"),
            _deployment("gpt-4o-1120", "gpt-4o", version="2024-11-20"),
        ],
    },
    "aoai-ptu": {
        "provider": ModelProvider.AZURE_OPENAI,
        "location": "eastus",
        "deployments": [
            _deployment(
                "gpt-4o",
                "gpt-4o",
                sku="ProvisionedManaged",
                spillover_deployment_name="gpt-4o-overflow",
            ),
        ],
    },
    "foundry-east": {
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "location": "eastus2",
        "deployments": [
            _claude(),
            _deployment("gpt-4o", "gpt-4o", sku="GlobalStandard"),
            _deployment(
                "llama-3-3-70b",
                "Llama-3.3-70B-Instruct",
                version="5",
                model_format="Meta",
                sku="GlobalStandard",
            ),
        ],
    },
    "foundry-west": {
        "provider": ModelProvider.AZURE_AI_FOUNDRY,
        "location": "westus3",
        "deployments": [_claude(), _claude("opus-west")],
    },
    "aoai-key": {
        "provider": ModelProvider.AZURE_OPENAI,
        "location": "westeurope",
        "auth_mode": EndpointAuthMode.API_KEY,
        # An API key can't list deployments, so MOSAIC knows only what's declared.
        "deployments": [],
        "declared": [
            DeclaredDeployment(
                deployment_name="gpt-4o",
                model_name="gpt-4o",
                model_version="2024-08-06",
                api_shape=ApiShape.AZURE_OPENAI,
            )
        ],
    },
}


def _endpoint_id(name: str) -> str:
    return f"ep-{name}"


def _member(endpoint: str, deployment: str, **extra: Any) -> dict[str, Any]:
    return {"model_endpoint_id": _endpoint_id(endpoint), "deployment_name": deployment, **extra}


def _model(*members: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"members": list(members), **extra}


def _gpt4o(*endpoints: str, **extra: Any) -> dict[str, Any]:
    return _model(*(_member(endpoint, "gpt-4o") for endpoint in endpoints), **extra)


def _granted(gateway_id: str) -> GatewayRuntimeAccess:
    return GatewayRuntimeAccess(
        gateway_id=gateway_id,
        gateway_name="apim-contoso-dev",
        can_invoke=True,
        evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
        reason=RuntimeAccessReason.GRANTED,
    )


def _policy(fake: FakeApim, suffix: str) -> str:
    value = fake.written[suffix]["properties"]["value"]
    assert isinstance(value, str)
    return value


class Estate:
    """A managed Developer gateway in development, and the estate's endpoints beside it."""

    def __init__(self) -> None:
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
        self.gateway_repository = InMemoryGatewayRepository()
        self.endpoint_repository = InMemoryModelEndpointRepository()
        self.environment_repository = InMemoryEnvironmentRepository(
            self.gateway_repository, self.endpoint_repository, InMemoryMcpEndpointRepository()
        )
        self.gateways = build_gateway_service(self.apim, self.gateway_repository)
        self.service = build_model_pool_service(
            self.apim,
            self.gateway_repository,
            self.endpoint_repository,
            environment_repository=self.environment_repository,
        )
        self.gateway_id = ""

    async def setup(self) -> None:
        gateway = await self.gateways.register(
            ACTOR, GatewayCreate.model_validate({"azure_resource_id": RESOURCE_ID})
        )
        await self.gateways.sync_now(ACTOR, gateway.id)
        gateway = await self.gateways.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
        )
        self.gateway_id = gateway.id
        await self.update_gateway(environment="development")
        for name, spec in ESTATE.items():
            await self.add_endpoint(name, **spec)

    async def update_gateway(self, **changes: Any) -> None:
        gateway = await self.gateway_repository.get_gateway(TENANT, self.gateway_id)
        assert gateway is not None
        await self.gateway_repository.save_gateway(gateway.model_copy(update=changes), _audit())

    async def set_gateway_sku(self, sku_name: str) -> None:
        gateway = await self.gateway_repository.get_gateway(TENANT, self.gateway_id)
        assert gateway is not None
        await self.update_gateway(
            capabilities=gateway.capabilities.model_copy(update={"sku_name": sku_name})
        )

    async def add_endpoint(
        self,
        name: str,
        *,
        provider: ModelProvider,
        location: str,
        deployments: list[dict[str, Any]],
        auth_mode: EndpointAuthMode = EndpointAuthMode.MANAGED_IDENTITY,
        environment: str | None = "development",
        declared: list[DeclaredDeployment] | None = None,
    ) -> str:
        host = (
            f"https://{name}.openai.azure.com/"
            if provider == ModelProvider.AZURE_OPENAI
            else f"https://{name}.cognitiveservices.azure.com/"
        )
        endpoint = ModelEndpoint(
            id=_endpoint_id(name),
            tenant_id=TENANT,
            name=name,
            provider=provider,
            endpoint=host,
            environment=environment,
            auth_mode=auth_mode,
            runtime_access=[_granted(self.gateway_id)],
            capabilities=ModelEndpointCapabilities(
                location=location,
                kind="OpenAI" if provider == ModelProvider.AZURE_OPENAI else "AIServices",
            ),
            declared_deployments=list(declared or []),
        )
        await self.endpoint_repository.save_endpoint(endpoint, _audit())
        await self.observe(name, deployments)
        return endpoint.id

    async def update_endpoint(self, name: str, **changes: Any) -> None:
        endpoint = await self.endpoint_repository.get_endpoint(TENANT, _endpoint_id(name))
        assert endpoint is not None
        await self.endpoint_repository.save_endpoint(endpoint.model_copy(update=changes), _audit())

    async def observe(self, name: str, deployments: list[dict[str, Any]]) -> None:
        endpoint_id = _endpoint_id(name)
        snapshot = new_id("snapshot")
        observed = [
            ObservedModelDeployment(
                id=deterministic_id(
                    "observedModelDeployment", TENANT, endpoint_id, item["deployment_name"]
                ),
                tenant_id=TENANT,
                endpoint_id=endpoint_id,
                snapshot_id=snapshot,
                **item,
            )
            for item in deployments
        ]
        await self.endpoint_repository.replace_observed_for_endpoint(
            TENANT, endpoint_id, list(observed), snapshot
        )

    async def create(self, display_name: str, *models: dict[str, Any], **extra: Any) -> ModelPool:
        return await self.service.create(
            ACTOR,
            ModelPoolCreate.model_validate(
                {
                    "gateway_id": self.gateway_id,
                    "display_name": display_name,
                    "models": list(models),
                    **extra,
                }
            ),
        )

    async def update(self, pool_id: str, **changes: Any) -> ModelPool:
        return await self.service.update(ACTOR, pool_id, ModelPoolUpdate.model_validate(changes))

    async def apply(self, pool_id: str, plan: PublishPlan) -> PublishRun:
        run = await self.service.apply(ACTOR, pool_id, plan.id)
        await self.service.wait_for_idle()
        return await self.service.get_run(ACTOR, pool_id, run.id)

    async def publish(self, pool_id: str) -> PublishRun:
        plan = await self.service.plan(ACTOR, pool_id)
        run = await self.apply(pool_id, plan)
        assert run.status == PublishRunStatus.SUCCEEDED, run.errors
        return run

    async def unpublish(self, pool_id: str) -> PublishRun:
        plan = await self.service.plan_unpublish(ACTOR, pool_id)
        run = await self.service.unpublish(ACTOR, pool_id, plan.id)
        await self.service.wait_for_idle()
        return await self.service.get_run(ACTOR, pool_id, run.id)

    async def pool(self, pool_id: str) -> ModelPool:
        return await self.service.get_pool(ACTOR, pool_id)

    def audit_actions(self) -> list[str]:
        return [event.action for event in self.gateway_repository.audit_events.values()]


@pytest.fixture
async def estate() -> Estate:
    built = Estate()
    await built.setup()
    return built


@pytest.fixture
def settings() -> Settings:
    """The API's settings, signed in to the estate's tenant."""

    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        cors_origins=["http://localhost:5173"],
    )


async def test_create_derives_names_and_the_model_from_inventory(estate: Estate) -> None:
    pool = await estate.create(
        "OpenAI GPT", _gpt4o("aoai-east", "aoai-sweden"), description="Chat models"
    )

    assert pool.id == model_pool_id(TENANT, estate.gateway_id, "mosaic-pool-openai-gpt")
    assert pool.api_name == "mosaic-pool-openai-gpt"
    assert pool.api_path == "mosaic/pool-openai-gpt"
    assert pool.fragment_name == pool.api_name
    assert pool.product_name == pool.api_name
    assert pool.subscription_name == pool.api_name
    assert pool.api_shape == ApiShape.AZURE_OPENAI
    assert pool.vendor == "OpenAI"
    assert pool.pool_type == ModelPoolType.BREAKER
    assert pool.status == PublicationStatus.DRAFT
    assert pool.resources == []

    [model] = pool.models
    assert model.id == pool_model_id(pool.id, "gpt-4o")
    assert model.public_name == "gpt-4o"
    assert model.display_name == "gpt-4o"
    assert (model.model_name, model.model_format) == ("gpt-4o", "OpenAI")
    assert model.expected_version == "2024-08-06"
    assert model.backend_pool_name == backend_pool_name(pool.api_name, model.id, "gpt-4o")
    assert [member.model_endpoint_id for member in model.members] == [
        _endpoint_id("aoai-east"),
        _endpoint_id("aoai-sweden"),
    ]
    assert model.members[0].backend_name == member_backend_name(
        pool.api_name, model.id, _endpoint_id("aoai-east"), "gpt-4o"
    )
    assert len({member.backend_name for member in model.members}) == 2
    assert all(len(member.backend_name) <= 80 for member in model.members)
    assert "modelPool.created" in estate.audit_actions()
    assert [item.id for item in await estate.service.list_pools(ACTOR, None)] == [pool.id]
    assert await estate.service.list_pools(ACTOR, "another-gateway") == []


async def test_create_takes_an_explicit_api_name_path_and_product(estate: Estate) -> None:
    pool = await estate.create(
        "Claude",
        _model(
            _member("foundry-east", "claude-opus-4-5"),
            _member("foundry-west", "opus-west"),
            public_name="claude-opus-4-5",
            display_name="Claude Opus 4.5",
        ),
        api_name="anthropic",
        api_path="ai/anthropic",
        product_name="anthropic-callers",
    )

    assert (pool.api_name, pool.api_path) == ("anthropic", "ai/anthropic")
    assert pool.product_name == "anthropic-callers"
    assert pool.vendor == "Anthropic"
    assert pool.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert pool.models[0].display_name == "Claude Opus 4.5"
    assert pool.models[0].expected_version == "1"


@pytest.mark.parametrize(
    ("models", "message"),
    [
        (
            [
                _model(_member("foundry-east", "gpt-4o")),
                _model(_member("foundry-east", "llama-3-3-70b")),
            ],
            "A pool serves one vendor's models",
        ),
        (
            [
                _gpt4o("aoai-east"),
                _model(_member("foundry-east", "gpt-4o"), public_name="gpt-4o-foundry"),
            ],
            "these models need different ones",
        ),
        (
            [
                _model(
                    _member("aoai-east", "gpt-4o"),
                    _member("aoai-east", "gpt-4o-mini"),
                    public_name="gpt",
                )
            ],
            "Every deployment of gpt must serve the same model from the same vendor.",
        ),
        (
            [_model(_member("aoai-east", "gpt-4o-batch"))],
            "is a batch deployment, which can't serve requests as they arrive.",
        ),
        (
            [_model(_member("aoai-key", "gpt-4o-mini"))],
            "gpt-4o-mini on aoai-key isn't declared on its endpoint.",
        ),
        (
            [_model(_member("aoai-east", "gpt-5"))],
            "MOSAIC hasn't observed",
        ),
        (
            [
                _model(
                    _member("aoai-east", "gpt-4o"),
                    _member("aoai-sweden", "gpt-4o-1120"),
                    public_name="gpt-4o",
                )
            ],
            "serve different versions",
        ),
        (
            [
                _model(
                    _member("foundry-east", "claude-opus-4-5"),
                    _member("foundry-west", "opus-west"),
                )
            ],
            "Give this model a public name",
        ),
    ],
    ids=[
        "vendors",
        "shapes",
        "models",
        "batch",
        "undeclared",
        "unobserved",
        "versions",
        "public-name",
    ],
)
async def test_create_refuses_members_that_cant_share_a_pool(
    estate: Estate, models: list[dict[str, Any]], message: str
) -> None:
    with pytest.raises(ValidationError) as refused:
        await estate.create("Refused", *models)

    assert message in refused.value.message
    assert estate.gateway_repository.model_pools == {}


async def test_create_refuses_an_unknown_endpoint(estate: Estate) -> None:
    with pytest.raises((NotFoundError, ValidationError)) as refused:
        await estate.create("Ghost", _model(_member("nowhere", "gpt-4o")))

    assert "Model endpoint was not found" in str(refused.value)


async def test_mixed_versions_publish_when_the_administrator_accepts_them(estate: Estate) -> None:
    pool = await estate.create(
        "Mixed versions",
        _model(
            _member("aoai-east", "gpt-4o"),
            _member("aoai-sweden", "gpt-4o-1120"),
            public_name="gpt-4o",
            allow_mixed_versions=True,
        ),
    )

    assert pool.models[0].allow_mixed_versions is True
    assert len(pool.models[0].members) == 2


def test_a_pool_cant_offer_two_models_under_one_name() -> None:
    with pytest.raises(SchemaError, match="Two models in the pool are both named gpt-4o"):
        ModelPoolCreate.model_validate(
            {
                "gateway_id": "gateway",
                "display_name": "Twice",
                "models": [_gpt4o("aoai-east"), _gpt4o("aoai-sweden")],
            }
        )
    with pytest.raises(SchemaError, match="listed twice"):
        ModelPoolCreate.model_validate(
            {
                "gateway_id": "gateway",
                "display_name": "Twice",
                "models": [_gpt4o("aoai-east", "aoai-east")],
            }
        )


async def test_create_refuses_a_second_pool_with_the_same_api_name(estate: Estate) -> None:
    await estate.create("OpenAI", _gpt4o("aoai-east"))

    with pytest.raises(ConflictError, match="already has a pool with that API name"):
        await estate.create("OpenAI", _gpt4o("aoai-sweden"))


async def test_a_consumption_gateway_runs_only_linear_pools(estate: Estate) -> None:
    await estate.set_gateway_sku("Consumption")

    with pytest.raises(ValidationError, match="can run only linear pools"):
        await estate.create("Breaker", _gpt4o("aoai-east", "aoai-sweden"))

    pool = await estate.create("Linear", _gpt4o("aoai-east", "aoai-sweden"), pool_type="linear")
    assert pool.pool_type == ModelPoolType.LINEAR
    candidates = await estate.service.candidates(ACTOR, estate.gateway_id)
    assert candidates.pool_types["linear"] is None
    assert candidates.pool_types["breaker"] is not None


async def test_a_linear_pool_tries_at_most_ten_deployments_of_a_model(estate: Estate) -> None:
    names = [f"gpt-4o-{index:02d}" for index in range(1, 12)]
    await estate.add_endpoint(
        "aoai-many",
        provider=ModelProvider.AZURE_OPENAI,
        location="eastus2",
        deployments=[_deployment(name, "gpt-4o") for name in names],
    )
    members = [_member("aoai-many", name) for name in names]

    with pytest.raises(ValidationError, match="gpt-4o has 11 active"):
        await estate.create("Linear", _model(*members, public_name="gpt-4o"), pool_type="linear")

    members[-1]["drained"] = True
    pool = await estate.create("Linear", _model(*members, public_name="gpt-4o"), pool_type="linear")
    assert len(pool.models[0].active_members()) == 10


def _group(candidates: Any, model_name: str, shape: ApiShape) -> Any:
    return next(
        item
        for item in candidates.models
        if item.model_name == model_name and item.api_shape == shape
    )


def _candidate(group: Any, endpoint: str, deployment: str) -> Any:
    return next(
        item
        for item in group.deployments
        if item.model_endpoint_id == _endpoint_id(endpoint) and item.deployment_name == deployment
    )


async def test_candidates_group_deployments_by_model_and_say_why_some_cant_be_pooled(
    estate: Estate,
) -> None:
    await estate.observe(
        "aoai-sweden",
        [
            *ESTATE["aoai-sweden"]["deployments"],
            _deployment("gpt-4o-new", "gpt-4o", provisioning_state="Creating"),
        ],
    )
    await estate.add_endpoint(
        "compatible",
        provider=ModelProvider.OPENAI_COMPATIBLE,
        location="eastus2",
        deployments=[_deployment("gpt-4o", "gpt-4o")],
    )
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))

    candidates = await estate.service.candidates(ACTOR, estate.gateway_id)

    assert candidates.gateway_id == estate.gateway_id
    assert candidates.gateway_environment == "development"
    assert candidates.pool_types == {"breaker": None, "preferential": None, "linear": None}
    gpt = _group(candidates, "gpt-4o", ApiShape.AZURE_OPENAI)
    assert gpt.model_format == "OpenAI"
    assert {item.model_endpoint_id for item in gpt.deployments} == {
        _endpoint_id(name) for name in ("aoai-east", "aoai-sweden", "aoai-ptu", "aoai-key")
    }
    east = _candidate(gpt, "aoai-east", "gpt-4o")
    assert east.eligible is True
    assert east.pool_ids == [pool.id]
    assert east.readiness == "ready"
    assert east.region == "eastus2"
    assert _candidate(gpt, "aoai-ptu", "gpt-4o").capacity_type == "provisioned"
    assert _candidate(gpt, "aoai-sweden", "gpt-4o").processing_scope == "global"
    batch = _candidate(gpt, "aoai-east", "gpt-4o-batch")
    assert batch.eligible is False
    assert batch.reason == "Batch deployments can't serve requests as they arrive."
    keyed = _candidate(gpt, "aoai-key", "gpt-4o")
    assert keyed.eligible is True
    assert (keyed.declared, keyed.api_key) == (True, True)
    assert keyed.region == "westeurope"
    assert _candidate(gpt, "aoai-east", "gpt-4o").declared is False
    creating = _candidate(gpt, "aoai-sweden", "gpt-4o-new")
    assert creating.eligible is False
    assert "The deployment is" in (creating.reason or "")

    foundry = _group(candidates, "gpt-4o", ApiShape.FOUNDRY_MODELS)
    assert [item.model_endpoint_id for item in foundry.deployments] == [
        _endpoint_id("foundry-east")
    ]
    claude = _group(candidates, "claude-opus-4-5", ApiShape.ANTHROPIC_MESSAGES)
    assert claude.model_format == "Anthropic"
    assert sorted(item.deployment_name for item in claude.deployments) == [
        "claude-opus-4-5",
        "claude-opus-4-5",
        "opus-west",
    ]
    assert all(
        item.model_endpoint_id != _endpoint_id("compatible")
        for group in candidates.models
        for item in group.deployments
    )


async def test_detail_ranks_a_preferential_pools_provisioned_members_first(
    estate: Estate,
) -> None:
    pool = await estate.create(
        "Preferential",
        _gpt4o("aoai-east", "aoai-ptu", "aoai-sweden"),
        pool_type="preferential",
    )

    detail = await estate.service.detail(ACTOR, pool.id)

    assert detail.problems == []
    assert detail.gateway_environment == "development"
    assert detail.base_url == f"{GATEWAY_URL}/mosaic/pool-preferential"
    [model] = detail.models
    assert model.backend_pool_name == pool.models[0].backend_pool_name
    assert model.capacity == "provisionedWithOverflow"
    by_endpoint = {member.model_endpoint_id: member for member in model.members}
    assert by_endpoint[_endpoint_id("aoai-ptu")].priority == 1
    assert by_endpoint[_endpoint_id("aoai-east")].priority == 2
    assert by_endpoint[_endpoint_id("aoai-sweden")].priority == 2
    assert all(member.order is None for member in model.members)
    ptu = by_endpoint[_endpoint_id("aoai-ptu")]
    assert (ptu.region, ptu.capacity_type, ptu.processing_scope) == (
        "eastus",
        "provisioned",
        "regional",
    )
    assert ptu.observed is True
    assert ptu.readiness == "ready"
    assert ptu.endpoint_name == "aoai-ptu"
    assert any(
        "spills over to gpt-4o-overflow, which isn't in the pool" in item
        for item in detail.warnings
    )
    assert any("process data in different scopes" in item for item in detail.warnings)


async def test_detail_orders_a_linear_pools_members_without_a_backend_pool(
    estate: Estate,
) -> None:
    pool = await estate.create("Linear", _gpt4o("aoai-sweden", "aoai-east"), pool_type="linear")

    detail = await estate.service.detail(ACTOR, pool.id)

    [model] = detail.models
    assert model.backend_pool_name is None
    assert [(member.model_endpoint_id, member.order) for member in model.members] == [
        (_endpoint_id("aoai-sweden"), 1),
        (_endpoint_id("aoai-east"), 2),
    ]
    assert all(member.priority is None for member in model.members)


async def test_detail_warns_when_mosaic_cant_confirm_the_gateway_can_call_a_member(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    await estate.update_endpoint("aoai-east", runtime_access=[])

    detail = await estate.service.detail(ACTOR, pool.id)

    assert detail.problems == []
    assert detail.models[0].members[0].readiness == "notConfirmed"
    assert any("couldn't confirm" in item for item in detail.warnings)


async def test_a_member_the_gateway_cant_call_blocks_the_plan(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.update_endpoint(
        "aoai-sweden",
        runtime_access=[
            GatewayRuntimeAccess(
                gateway_id=estate.gateway_id,
                gateway_name="apim-contoso-dev",
                can_invoke=False,
                evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
                reason=RuntimeAccessReason.MISSING_ROLE,
            )
        ],
    )

    detail = await estate.service.detail(ACTOR, pool.id)
    assert detail.models[0].members[1].readiness == "cannotInvoke"
    assert any("has no role that lets it call this endpoint" in item for item in detail.problems)

    with pytest.raises(ConflictError, match="can't be published until its problems are fixed") as (
        refused
    ):
        await estate.service.plan(ACTOR, pool.id)
    assert refused.value.details["problems"] == detail.problems

    await estate.update(pool.id, models=[_gpt4o("aoai-east")])
    assert (await estate.service.plan(ACTOR, pool.id)).steps


async def test_a_development_gateway_cant_front_a_production_member(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.update_endpoint("aoai-sweden", environment="production")

    detail = await estate.service.detail(ACTOR, pool.id)

    assert any("can't front a Production endpoint" in item for item in detail.problems)
    sweden = detail.models[0].members[1]
    assert sweden.environment == "production"
    assert sweden.environment_verdict is not None
    with pytest.raises(ConflictError, match="problems are fixed"):
        await estate.service.plan(ACTOR, pool.id)

    # A drained member takes no traffic, so it doesn't block the pool.
    await estate.update(
        pool.id,
        models=[
            _model(
                _member("aoai-east", "gpt-4o"),
                _member("aoai-sweden", "gpt-4o", drained=True),
            )
        ],
    )
    assert (await estate.service.detail(ACTOR, pool.id)).problems == []


async def test_detail_of_a_pool_whose_gateway_is_gone(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    estate.gateway_repository.gateways.pop(estate.gateway_id)

    detail = await estate.service.detail(ACTOR, pool.id)

    assert detail.problems == ["The pool's gateway is no longer registered with MOSAIC."]
    assert detail.base_url is None
    [summary] = await estate.service.summaries(ACTOR)
    assert (summary.pool.id, summary.problem_count, summary.gateway_name) == (pool.id, 1, None)


async def test_summaries_count_active_members_by_capacity_and_readiness(estate: Estate) -> None:
    pool = await estate.create(
        "Preferential",
        _model(
            _member("aoai-east", "gpt-4o"),
            _member("aoai-ptu", "gpt-4o"),
            _member("aoai-sweden", "gpt-4o", drained=True),
        ),
        pool_type="preferential",
    )
    await estate.update_endpoint("aoai-east", runtime_access=[])

    [summary] = await estate.service.summaries(ACTOR, estate.gateway_id)

    assert summary.pool.id == pool.id
    assert summary.gateway_name == "apim-contoso-dev"
    assert summary.gateway_environment == "development"
    # The drained member takes no traffic, so it isn't counted.
    assert summary.capacity == {"payAsYouGo": 1, "provisioned": 1}
    assert summary.readiness == {"notConfirmed": 1, "ready": 1}
    assert summary.problem_count == 0
    assert summary.warning_count >= 1
    assert await estate.service.summaries(ACTOR, "gateway-elsewhere") == []


async def test_an_endpoint_lists_the_pools_that_use_it_and_the_deployments_they_use(
    estate: Estate,
) -> None:
    openai = await estate.create(
        "OpenAI",
        _gpt4o("aoai-east", "aoai-sweden", display_name="GPT-4o"),
        _model(_member("aoai-east", "gpt-4o-mini", drained=True), display_name="GPT-4o mini"),
    )
    backup = await estate.create("Backup", _gpt4o("aoai-east", "aoai-ptu"))
    await estate.create("Sweden only", _gpt4o("aoai-sweden"))

    uses = await estate.service.endpoint_pools(ACTOR, _endpoint_id("aoai-east"))

    # By name, and only the pools with a member on the endpoint.
    assert [use.pool.display_name for use in uses] == ["Backup", "OpenAI"]
    first, second = uses
    assert (first.pool.id, second.pool.id) == (backup.id, openai.id)
    assert second.pool.gateway_id == estate.gateway_id
    assert second.gateway_name == "apim-contoso-dev"
    assert second.pool.status == PublicationStatus.DRAFT
    assert second.pool.visibility == ModelPoolVisibility.LISTED
    # One entry for each of the pool's deployments on the endpoint, drained ones included.
    assert [
        (item.deployment_name, item.pool_model_id, item.public_name, item.model_display_name)
        for item in second.deployments
    ] == [
        ("gpt-4o", openai.models[0].id, "gpt-4o", "GPT-4o"),
        ("gpt-4o-mini", openai.models[1].id, "gpt-4o-mini", "GPT-4o mini"),
    ]
    assert [item.drained for item in second.deployments] == [False, True]
    # Nothing is published on its own here, so no one would see a model twice.
    assert [item.warning for use in uses for item in use.deployments] == [None, None, None]

    assert [
        use.pool.display_name
        for use in await estate.service.endpoint_pools(ACTOR, _endpoint_id("aoai-ptu"))
    ] == ["Backup"]
    assert await estate.service.endpoint_pools(ACTOR, _endpoint_id("foundry-east")) == []
    with pytest.raises(NotFoundError):
        await estate.service.endpoint_pools(ACTOR, "ep-missing")

    estate.gateway_repository.gateways.pop(estate.gateway_id)
    gone = await estate.service.endpoint_pools(ACTOR, _endpoint_id("aoai-ptu"))
    assert [(use.pool.display_name, use.gateway_name) for use in gone] == [("Backup", None)]


def _sonnet(name: str = "claude-sonnet-4-5") -> dict[str, Any]:
    return _deployment(
        name, "claude-sonnet-4-5", version="1", model_format="Anthropic", sku="GlobalStandard"
    )


async def test_suggestions_offer_one_pool_per_vendor_for_models_on_two_or_more_endpoints(
    estate: Estate,
) -> None:
    suggestions = await estate.service.suggestions(ACTOR)

    # gpt-4o-mini, Llama, and gpt-4o through the Foundry API are each on one endpoint only.
    assert [(item.vendor, item.api_shape) for item in suggestions] == [
        ("Anthropic", ApiShape.ANTHROPIC_MESSAGES),
        ("OpenAI", ApiShape.AZURE_OPENAI),
    ]
    claude, openai = suggestions
    assert (claude.gateway_id, claude.gateway_name, claude.gateway_environment) == (
        estate.gateway_id,
        "apim-contoso-dev",
        "development",
    )
    assert claude.models == [
        PoolSuggestionModel(
            model_name="claude-opus-4-5",
            model_format="Anthropic",
            deployment_count=3,
            endpoint_count=2,
            regions=["eastus2", "westus3"],
        )
    ]
    assert (claude.endpoint_count, claude.regions, claude.family_pools) == (
        2,
        ["eastus2", "westus3"],
        [],
    )
    # The batch deployment can't serve a pool. The declared one on the keyed endpoint can.
    [gpt] = openai.models
    assert (gpt.model_name, gpt.deployment_count, gpt.endpoint_count) == ("gpt-4o", 5, 4)
    assert gpt.regions == ["eastus", "eastus2", "swedencentral", "westeurope"]
    assert openai.endpoint_count == 4


async def test_suggestions_leave_out_pooled_models_and_name_the_pools_that_could_take_the_rest(
    estate: Estate,
) -> None:
    for name in ("foundry-east", "foundry-west"):
        await estate.observe(name, [*ESTATE[name]["deployments"], _sonnet()])
    # The pool uses only some of opus's deployments, which still takes opus out of suggestions:
    # a second pool listing it would show portal users the model twice.
    claude = await estate.create(
        "Claude",
        _model(
            _member("foundry-east", "claude-opus-4-5"),
            _member("foundry-west", "opus-west"),
            public_name="claude-opus-4-5",
        ),
    )
    await estate.create("OpenAI", _gpt4o("aoai-east"))

    [suggestion] = await estate.service.suggestions(ACTOR)

    assert (suggestion.vendor, suggestion.api_shape) == ("Anthropic", ApiShape.ANTHROPIC_MESSAGES)
    assert [item.model_name for item in suggestion.models] == ["claude-sonnet-4-5"]
    assert suggestion.family_pools == [PoolReference(id=claude.id, display_name="Claude")]


async def test_suggestions_count_only_deployments_the_gateway_can_use_and_skip_observed_gateways(
    estate: Estate,
) -> None:
    await estate.update_endpoint(
        "foundry-west",
        runtime_access=[
            GatewayRuntimeAccess(
                gateway_id=estate.gateway_id,
                gateway_name="apim-contoso-dev",
                can_invoke=False,
                evaluation=RuntimeAccessEvaluation.ROLE_ASSIGNMENTS,
                reason=RuntimeAccessReason.MISSING_ROLE,
            )
        ],
    )

    # Opus is left on one endpoint the gateway can call.
    assert [item.vendor for item in await estate.service.suggestions(ACTOR)] == ["OpenAI"]

    await estate.update_gateway(management_mode=ManagementMode.OBSERVE)
    assert await estate.service.suggestions(ACTOR) == []


def _steps(plan: PublishPlan) -> list[tuple[str, str, str]]:
    return [(str(step.kind), step.name, str(step.action)) for step in plan.steps]


def _xml(text: str) -> ET.Element:
    return ET.fromstring(text)


async def test_publishing_a_breaker_pool_writes_member_backends_behind_a_backend_pool(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI GPT", _gpt4o("aoai-east", "aoai-sweden"))
    model = pool.models[0]
    east, sweden = model.members

    plan = await estate.service.plan(ACTOR, pool.id)

    assert (plan.target, plan.operation) == ("pool", "publish")
    assert {action for _, _, action in _steps(plan)} == {"create"}
    kinds = [(kind, name) for kind, name, _ in _steps(plan)]
    assert kinds[:5] == [
        ("backend", east.backend_name),
        ("backend", sweden.backend_name),
        ("backendPool", model.backend_pool_name),
        ("policyFragment", pool.fragment_name),
        ("api", pool.api_name),
    ]
    assert {name for kind, name in kinds if kind == "apiOperation"} == AOAI_OPERATIONS
    assert kinds[-4:] == [
        ("apiPolicy", "policy"),
        ("product", pool.product_name),
        ("productApi", pool.api_name),
        ("subscription", pool.subscription_name),
    ]
    planned = await estate.pool(pool.id)
    assert planned.status == PublicationStatus.PLANNED
    assert planned.last_plan_id == plan.id
    assert await estate.service.get_plan(ACTOR, plan.id) == plan

    run = await estate.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert run.target == "pool"
    fake = estate.apim
    assert fake.dangling_references == []
    member = fake.written[f"backends/{east.backend_name}"]["properties"]
    assert member["url"] == "https://aoai-east.openai.azure.com/openai/deployments/gpt-4o"
    assert member["protocol"] == "http"
    assert member["title"] == "OpenAI GPT: gpt-4o on aoai-east"
    assert member["circuitBreaker"] == circuit_breaker(BreakerPreset.THROTTLING)
    balancer = fake.written[f"backends/{model.backend_pool_name}"]["properties"]
    assert balancer["type"] == "Pool"
    assert "url" not in balancer
    services = balancer["pool"]["services"]
    assert [item["id"].rsplit("/", 1)[-1] for item in services] == [
        east.backend_name,
        sweden.backend_name,
    ]
    assert all(item["id"].casefold().startswith(RESOURCE_ID.casefold()) for item in services)
    assert [(item["priority"], item["weight"]) for item in services] == [(1, 1), (1, 1)]
    chat = fake.written[f"apis/{pool.api_name}/operations/chat-completions"]["properties"]
    assert "{deployment-id}" in chat["urlTemplate"]
    assert chat["templateParameters"] == [
        {"name": "deployment-id", "type": "string", "required": True}
    ]
    assert fake.written[f"apis/{pool.api_name}"]["properties"]["path"] == pool.api_path

    policy = _xml(_policy(fake, f"apis/{pool.api_name}/policies/policy"))
    assert policy.find("inbound/include-fragment").get("fragment-id") == pool.fragment_name
    retry = policy.find("backend/retry")
    assert retry is not None
    assert retry.get("count") == "1"
    backends = [item.get("backend-id") for item in policy.iter("set-backend-service")]
    assert backends == [model.backend_pool_name]
    assert policy.find("backend/retry/forward-request").get("buffer-request-body") == "true"
    deleted = {
        item.get("name")
        for item in policy.findall("outbound/set-header")
        if item.get("exists-action") == "delete"
    }
    assert {"x-ms-region"} <= deleted

    fragment = _xml(_policy(fake, f"policyFragments/{pool.fragment_name}"))
    requested = fragment.find("set-variable[@name='mosaic-pool-model']")
    assert requested is not None
    assert "deployment-id" in requested.get("value", "")
    conditions = [item.get("condition", "") for item in fragment.findall("choose/when")]
    assert any('"gpt-4o"' in item for item in conditions)
    refusal = fragment.find("choose/otherwise/return-response")
    assert refusal.find("set-status").get("code") == "404"
    assert refusal.find("set-body").text == NOT_FOUND_BODY
    assert fragment.find("choose/when/rewrite-uri") is not None or any(
        item.find("rewrite-uri") is not None for item in fragment.iter("when")
    )

    published = await estate.pool(pool.id)
    assert published.status == PublicationStatus.PUBLISHED
    assert published.last_run_id == run.id
    assert published.last_applied_at is not None
    assert all(item.created_by_mosaic for item in published.resources)
    assert {str(item.kind) for item in published.resources} >= {
        "backend",
        "backendPool",
        "policyFragment",
        "api",
        "apiOperation",
        "apiPolicy",
        "product",
        "productApi",
        "subscription",
    }
    assert published.owned_backends() == {
        east.backend_name,
        sweden.backend_name,
        model.backend_pool_name,
    }

    # The record changed, so the plan that was applied no longer describes the pool.
    with pytest.raises(ConflictError) as stale:
        await estate.service.apply(ACTOR, pool.id, plan.id)
    assert stale.value.details["reason"] == "stalePlan"

    replan = await estate.service.plan(ACTOR, pool.id)
    assert {action for _, _, action in _steps(replan)} == {"update"}
    assert (await estate.pool(pool.id)).status == PublicationStatus.PUBLISHED
    rerun = await estate.apply(pool.id, replan)
    assert rerun.status == PublishRunStatus.SUCCEEDED, rerun.errors


async def test_apply_needs_a_reviewed_plan(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))

    with pytest.raises(ConflictError) as unplanned:
        await estate.service.apply(ACTOR, pool.id)
    assert unplanned.value.details["reason"] == "planRequired"

    await estate.service.plan(ACTOR, pool.id)
    with pytest.raises(NotFoundError, match="Pool plan was not found"):
        await estate.service.apply(ACTOR, pool.id, "publishplan-unknown")

    old = await estate.service.plan(ACTOR, pool.id)
    await estate.service.plan(ACTOR, pool.id)
    with pytest.raises(ConflictError) as replaced:
        await estate.service.apply(ACTOR, pool.id, old.id)
    assert replaced.value.details["reason"] == "stalePlan"
    assert await estate.service.get_lock_owner(ACTOR, pool.id) is None


async def test_an_edit_after_planning_needs_a_new_plan(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    plan = await estate.service.plan(ACTOR, pool.id)

    updated = await estate.update(pool.id, models=[_gpt4o("aoai-east", "aoai-sweden")])

    assert updated.status == PublicationStatus.DRAFT
    assert updated.last_plan_id is None
    assert "modelPool.updated" in estate.audit_actions()
    with pytest.raises(ConflictError) as refused:
        await estate.service.apply(ACTOR, pool.id, plan.id)
    assert refused.value.details["reason"] == "stalePlan"


async def test_a_model_with_applied_grants_cant_leave_the_pool(estate: Estate) -> None:
    mini = _model(_member("aoai-east", "gpt-4o-mini"))
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"), mini)
    await estate.publish(pool.id)
    pool = await estate.pool(pool.id)
    mini_id = pool_model_id(pool.id, "gpt-4o-mini")
    subject = EntitlementSubject(kind="user", id="principal-ana")

    async def applied(*, enabled: bool) -> None:
        grant = PoolAccessGrant(
            entitlement_id="entitlement-ana",
            pool_model_id=mini_id,
            subject=subject,
            object_id="11111111-1111-1111-1111-111111111111",
            display_name="Ana",
            key_name=pool_key_name(TENANT, pool.id, subject.id, general_cost_center_id(TENANT)),
            enabled=enabled,
            intent_digest="digest",
        )
        snapshot = PoolAccessSnapshot(version=1, settings=ModelAccessSettings(), grants=[grant])
        await estate.gateway_repository.save_model_pool(
            pool.model_copy(update={"applied_access": snapshot}), _audit()
        )

    await applied(enabled=True)
    with pytest.raises(ConflictError, match="gpt-4o-mini still has applied grants") as refused:
        await estate.update(pool.id, models=[_gpt4o("aoai-east")])
    assert refused.value.details == {"poolModelIds": [mini_id]}
    assert [model.public_name for model in (await estate.pool(pool.id)).models] == [
        "gpt-4o",
        "gpt-4o-mini",
    ]

    # An applied revocation leaves the grant in the snapshot, disabled, and the model can go.
    await applied(enabled=False)
    updated = await estate.update(pool.id, models=[_gpt4o("aoai-east")])
    assert [model.public_name for model in updated.models] == ["gpt-4o"]


async def test_the_console_learns_which_saved_changes_the_gateway_does_not_run(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))

    async def waiting() -> bool:
        detail = await estate.service.detail(ACTOR, pool.id)
        [summary] = await estate.service.summaries(ACTOR)
        assert summary.unapplied_changes == detail.unapplied_changes
        return detail.unapplied_changes

    # Nothing of a draft is on the gateway, so no change is waiting to reach it.
    assert not await waiting()

    await estate.publish(pool.id)
    assert (await estate.pool(pool.id)).applied_intent_digest is not None
    assert not await waiting()

    # Reviewing a plan changes nothing, and the portal-only settings never reach the gateway.
    await estate.service.plan(ACTOR, pool.id)
    await estate.update(pool.id, visibility="hidden", show_capacity=False)
    assert not await waiting()

    drained = _model(_member("aoai-east", "gpt-4o"), _member("aoai-sweden", "gpt-4o", drained=True))
    await estate.update(pool.id, models=[drained])
    assert await waiting()

    # Undoing the change before it's applied leaves nothing waiting.
    await estate.update(pool.id, models=[_gpt4o("aoai-east", "aoai-sweden")])
    assert not await waiting()

    await estate.update(pool.id, models=[drained])
    await estate.publish(pool.id)
    assert not await waiting()

    run = await estate.unpublish(pool.id)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert (await estate.pool(pool.id)).applied_intent_digest is None
    assert not await waiting()


def _drift(plan: PublishPlan) -> list[str]:
    """The plan's warnings about what changed in API Management outside MOSAIC."""

    return [
        warning
        for warning in plan.warnings
        if "outside MOSAIC" in warning or "after MOSAIC last applied" in warning
    ]


def _edit(fake: FakeApim, suffix: str) -> None:
    """Change a policy in API Management, as someone working in the Azure portal would."""

    value = _policy(fake, suffix)
    edited = value.replace(">", ">\n<!-- edited by hand -->", 1)
    assert edited != value
    fake.written[suffix]["properties"]["value"] = edited


async def test_a_replan_warns_that_applying_replaces_policy_edits_made_outside_mosaic(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    api_policy = f"apis/{pool.api_name}/policies/policy"
    fragment = f"policyFragments/{pool.fragment_name}"

    # Nothing of a draft has been applied, so there's nothing to compare.
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == []

    await estate.publish(pool.id)
    published = await estate.pool(pool.id)
    assert published.applied_policy_sha256 == content_digest(_policy(estate.apim, api_policy))
    assert published.applied_fragment_sha256 == content_digest(_policy(estate.apim, fragment))
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == []

    _edit(estate.apim, api_policy)
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == [
        "Someone changed the pool's API policy in API Management after MOSAIC last applied it. "
        "Applying replaces those changes."
    ]

    _edit(estate.apim, fragment)
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == [
        "Someone changed the pool's API policy in API Management after MOSAIC last applied it. "
        "Applying replaces those changes.",
        f"Someone changed the pool's policy fragment {pool.fragment_name} in API Management "
        "after MOSAIC last applied it. Applying replaces those changes.",
    ]

    # Applying puts MOSAIC's policies back, and records them as the ones to compare against.
    await estate.publish(pool.id)
    assert "edited by hand" not in _policy(estate.apim, api_policy)
    assert "edited by hand" not in _policy(estate.apim, fragment)
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == []


async def test_a_replan_warns_that_applying_recreates_what_was_removed_outside_mosaic(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.publish(pool.id)
    sweden = pool.models[0].members[1]
    estate.apim.written.pop(f"backends/{sweden.backend_name}")

    plan = await estate.service.plan(ACTOR, pool.id)

    assert ("backend", sweden.backend_name, "create") in _steps(plan)
    assert _drift(plan) == [
        f"API Management no longer has backend {sweden.backend_name}, which MOSAIC created for "
        "this pool. Someone removed it outside MOSAIC, and applying creates it again."
    ]
    await estate.publish(pool.id)
    assert f"backends/{sweden.backend_name}" in estate.apim.written
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == []


async def test_a_removed_api_is_named_once_rather_than_with_everything_it_held(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.publish(pool.id)
    sweden = pool.models[0].members[1]
    held = [
        key
        for key in estate.apim.written
        if key == f"apis/{pool.api_name}"
        or key.startswith(f"apis/{pool.api_name}/")
        or key == f"products/{pool.product_name}/apis/{pool.api_name}"
    ]
    assert f"apis/{pool.api_name}/policies/policy" in held
    for key in [*held, f"backends/{sweden.backend_name}"]:
        estate.apim.written.pop(key)

    plan = await estate.service.plan(ACTOR, pool.id)

    # The policy went with the API, so there's nothing to say about edits to it.
    assert _drift(plan) == [
        f"API Management no longer has backend {sweden.backend_name}, API {pool.api_name}, "
        "which MOSAIC created for this pool. Someone removed them outside MOSAIC, and applying "
        "creates them again."
    ]


async def test_only_a_successful_apply_records_the_policies_to_compare(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.publish(pool.id)

    # A run that fails can leave some of its own policy changes behind, which aren't edits.
    estate.apim.fail_write(f"products/{pool.product_name}")
    plan = await estate.service.plan(ACTOR, pool.id)
    run = await estate.apply(pool.id, plan)
    assert run.status != PublishRunStatus.SUCCEEDED
    failed = await estate.pool(pool.id)
    assert (failed.applied_policy_sha256, failed.applied_fragment_sha256) == (None, None)
    _edit(estate.apim, f"apis/{pool.api_name}/policies/policy")
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == []

    estate.apim.write_failures.clear()
    await estate.publish(pool.id)
    assert (await estate.pool(pool.id)).applied_policy_sha256 is not None

    run = await estate.unpublish(pool.id)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    gone = await estate.pool(pool.id)
    assert (gone.applied_policy_sha256, gone.applied_fragment_sha256) == (None, None)
    assert _drift(await estate.service.plan(ACTOR, pool.id)) == []


async def test_an_unreadable_policy_leaves_nothing_to_compare_but_the_apply_succeeds(
    estate: Estate, monkeypatch: pytest.MonkeyPatch
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    plan = await estate.service.plan(ACTOR, pool.id)

    async def unreadable(self: ApimClient, name: str) -> str | None:
        raise RuntimeError("API Management timed out")

    monkeypatch.setattr(ApimClient, "get_policy_fragment", unreadable)

    run = await estate.apply(pool.id, plan)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    published = await estate.pool(pool.id)
    assert published.status == PublicationStatus.PUBLISHED
    assert (published.applied_policy_sha256, published.applied_fragment_sha256) == (None, None)


async def test_a_member_whose_capacity_changes_makes_the_plan_stale(estate: Estate) -> None:
    pool = await estate.create(
        "OpenAI", _gpt4o("aoai-east", "aoai-sweden"), pool_type="preferential"
    )
    plan = await estate.service.plan(ACTOR, pool.id)
    await estate.observe("aoai-east", [_deployment("gpt-4o", "gpt-4o", sku="ProvisionedManaged")])

    with pytest.raises(ConflictError) as refused:
        await estate.service.apply(ACTOR, pool.id, plan.id)
    assert refused.value.details["reason"] == "stalePlan"

    # And an endpoint whose environment changes fails the plan's checks again.
    replan = await estate.service.plan(ACTOR, pool.id)
    await estate.update_endpoint("aoai-sweden", environment="test")
    with pytest.raises(ConflictError, match="problems are fixed"):
        await estate.service.apply(ACTOR, pool.id, replan.id)


async def test_removing_a_member_deletes_its_backend_after_the_rest_is_rewritten(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.publish(pool.id)
    sweden = pool.models[0].members[1]

    await estate.update(pool.id, models=[_gpt4o("aoai-east")])
    plan = await estate.service.plan(ACTOR, pool.id)

    assert _steps(plan)[-1] == ("backend", sweden.backend_name, "delete")
    run = await estate.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    assert f"backends/{sweden.backend_name}" not in estate.apim.written
    deletes = estate.apim.write_paths("DELETE")
    puts = estate.apim.write_paths("PUT")
    assert deletes == [f"backends/{sweden.backend_name}"]
    assert puts.index(f"backends/{pool.models[0].backend_pool_name}") >= 0
    services = estate.apim.written[f"backends/{pool.models[0].backend_pool_name}"]["properties"][
        "pool"
    ]["services"]
    assert len(services) == 1
    assert sweden.backend_name not in (await estate.pool(pool.id)).owned_backends()


async def test_draining_a_member_takes_it_out_of_the_backend_pool(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.publish(pool.id)
    sweden = pool.models[0].members[1]

    await estate.update(
        pool.id,
        models=[
            _model(_member("aoai-east", "gpt-4o"), _member("aoai-sweden", "gpt-4o", drained=True))
        ],
    )
    await estate.publish(pool.id)

    services = estate.apim.written[f"backends/{pool.models[0].backend_pool_name}"]["properties"][
        "pool"
    ]["services"]
    assert [item["id"].rsplit("/", 1)[-1] for item in services] == [
        pool.models[0].members[0].backend_name
    ]
    assert f"backends/{sweden.backend_name}" not in estate.apim.written


async def test_a_failed_apply_rolls_back_what_it_created(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    estate.apim.fail_write(f"apis/{pool.api_name}")
    plan = await estate.service.plan(ACTOR, pool.id)

    run = await estate.apply(pool.id, plan)

    assert run.status == PublishRunStatus.ROLLED_BACK
    assert run.rolled_back is True
    assert run.errors
    assert not any(key.startswith("backends/mosaic-pool") for key in estate.apim.written)
    assert f"policyFragments/{pool.fragment_name}" not in estate.apim.written
    failed = await estate.pool(pool.id)
    assert failed.status == PublicationStatus.ROLLED_BACK
    assert failed.created_resources() == []
    assert await estate.service.get_lock_owner(ACTOR, pool.id) is None


async def test_a_plan_wont_take_over_an_api_mosaic_didnt_create(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"), api_name="chat-api")

    with pytest.raises(ConflictError, match="MOSAIC did not create it"):
        await estate.service.plan(ACTOR, pool.id)
    assert estate.apim.write_paths("PUT") == []


async def test_a_plan_wont_serve_a_path_another_api_uses(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"), api_path="OpenAI")

    with pytest.raises(ConflictError, match="already served at that path") as refused:
        await estate.service.plan(ACTOR, pool.id)
    assert refused.value.details["conflictingApi"] == "chat-api"


async def test_a_plan_wont_replace_a_backend_mosaic_didnt_create(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    member = pool.models[0].members[0]
    estate.apim.seed(
        f"backends/{member.backend_name}",
        {"properties": {"url": "https://elsewhere.example.com", "protocol": "http"}},
    )

    with pytest.raises(ConflictError, match="already has a") as refused:
        await estate.service.plan(ACTOR, pool.id)
    assert refused.value.details["name"] == member.backend_name


async def test_pools_on_one_gateway_cant_share_an_api_path_or_product(estate: Estate) -> None:
    first = await estate.create("OpenAI", _gpt4o("aoai-east"))

    with pytest.raises(ConflictError, match="The OpenAI pool on this gateway already uses that"):
        await estate.create("Sweden", _gpt4o("aoai-sweden"), api_path=first.api_path.upper())
    with pytest.raises(ConflictError, match="already uses product"):
        await estate.create("Sweden", _gpt4o("aoai-sweden"), product_name=first.product_name)

    second = await estate.create("Sweden", _gpt4o("aoai-sweden"))
    assert {first.api_path, second.api_path} == {"mosaic/pool-openai", "mosaic/pool-sweden"}


async def test_a_pool_cant_reuse_a_publications_api_name_or_path(estate: Estate) -> None:
    publication = Publication(
        id="publication-chat",
        tenant_id=TENANT,
        gateway_id=estate.gateway_id,
        model_endpoint_id=_endpoint_id("aoai-east"),
        deployment_name="gpt-4o",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Chat",
        api_name="mosaic-chat",
        api_path="chat",
        backend_name="mosaic-chat",
        fragment_name="mosaic-chat",
        product_name="mosaic-chat",
        subscription_name="mosaic-chat",
        shape_version="v1",
        api_shape=ApiShape.AZURE_OPENAI,
    )
    await estate.gateway_repository.save_publication(publication, _audit())

    with pytest.raises(ConflictError, match="A publication on this gateway already uses") as path:
        await estate.create("Chat", _gpt4o("aoai-east"), api_path="Chat")
    assert path.value.details["conflictingPublicationId"] == publication.id
    with pytest.raises(ConflictError, match="A publication on this gateway already uses"):
        await estate.create("Chat", _gpt4o("aoai-east"), api_name="mosaic-chat")


async def test_unpublishing_removes_what_the_pool_created_in_reverse_order(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    model = pool.models[0]
    east = model.members[0]

    with pytest.raises(ConflictError, match="nothing to remove"):
        await estate.service.plan_unpublish(ACTOR, pool.id)

    await estate.publish(pool.id)
    with pytest.raises(ConflictError, match="Unpublish it first"):
        await estate.service.delete(ACTOR, pool.id)
    with pytest.raises(ConflictError) as unplanned:
        await estate.service.unpublish(ACTOR, pool.id)
    assert unplanned.value.details["reason"] == "planRequired"

    plan = await estate.service.plan_unpublish(ACTOR, pool.id)

    assert (plan.target, plan.operation) == ("pool", "unpublish")
    assert {action for _, _, action in _steps(plan)} == {"delete"}
    kinds = [kind for kind, _, _ in _steps(plan)]
    assert kinds[0] == "subscription"
    assert kinds[-2:] == ["backend", "backend"]
    assert any("stops working" in warning for warning in plan.warnings)
    # Planning an unpublish changes nothing.
    assert (await estate.pool(pool.id)).status == PublicationStatus.PUBLISHED

    estate.apim.writes.clear()
    run = await estate.service.unpublish(ACTOR, pool.id, plan.id)
    await estate.service.wait_for_idle()
    run = await estate.service.get_run(ACTOR, pool.id, run.id)

    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    deletes = estate.apim.write_paths("DELETE")
    assert deletes.index(f"backends/{model.backend_pool_name}") < deletes.index(
        f"backends/{east.backend_name}"
    )
    assert deletes.index(f"apis/{pool.api_name}") < deletes.index(
        f"policyFragments/{pool.fragment_name}"
    )
    assert not any(
        key.startswith((f"apis/{pool.api_name}", f"backends/{pool.api_name}"))
        for key in estate.apim.written
    )
    unpublished = await estate.pool(pool.id)
    assert unpublished.status == PublicationStatus.DRAFT
    assert unpublished.resources == []
    assert unpublished.unpublished_at is not None

    await estate.service.delete(ACTOR, pool.id)
    with pytest.raises(NotFoundError):
        await estate.pool(pool.id)
    assert "modelPool.removed" in estate.audit_actions()


def _endpoint_service(estate: Estate) -> ModelEndpointService:
    return build_endpoint_service(
        FakeCognitiveServices(),
        repository=estate.endpoint_repository,
        gateway_repository=estate.gateway_repository,
    )


async def test_an_endpoint_a_published_pool_routes_to_cant_be_removed(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.publish(pool.id)

    with pytest.raises(ConflictError, match="Remove aoai-sweden from the model pool") as refused:
        await _endpoint_service(estate).delete(ACTOR, _endpoint_id("aoai-sweden"))

    assert [item["id"] for item in refused.value.details["modelPools"]] == [pool.id]
    assert await estate.endpoint_repository.get_endpoint(TENANT, _endpoint_id("aoai-sweden"))
    assert len((await estate.pool(pool.id)).models[0].members) == 2


async def test_removing_an_endpoint_forgets_the_members_no_gateway_holds(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east", "aoai-sweden"))
    await estate.service.plan(ACTOR, pool.id)

    await _endpoint_service(estate).delete(ACTOR, _endpoint_id("aoai-sweden"))

    forgotten = await estate.pool(pool.id)
    assert [member.model_endpoint_id for member in forgotten.models[0].members] == [
        _endpoint_id("aoai-east")
    ]
    # The plan described the removed member, so the pool has to be planned again.
    assert forgotten.status == PublicationStatus.DRAFT
    assert forgotten.last_plan_id is None
    assert "modelPool.membersRemoved" in estate.audit_actions()


async def test_a_gateway_with_a_published_pool_cant_be_removed(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    await estate.publish(pool.id)

    with pytest.raises(ConflictError, match="MOSAIC published model pools into this gateway"):
        await estate.gateways.delete(ACTOR, estate.gateway_id)

    run = await estate.unpublish(pool.id)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    await estate.gateways.delete(ACTOR, estate.gateway_id)
    assert estate.gateway_repository.model_pools == {}


def _published_pool(gateway_id: str, endpoint_id: str) -> ModelPool:
    """A pool whose record says it created the backend of its one member."""

    api_name = "mosaic-pool-openai"
    pool_id = model_pool_id(TENANT, gateway_id, api_name)
    model_id = pool_model_id(pool_id, "gpt-4o")
    backend = member_backend_name(api_name, model_id, endpoint_id, "gpt-4o")
    return ModelPool(
        id=pool_id,
        tenant_id=TENANT,
        gateway_id=gateway_id,
        display_name="OpenAI",
        api_name=api_name,
        api_path="mosaic/pool-openai",
        fragment_name=api_name,
        product_name=api_name,
        subscription_name=api_name,
        status=PublicationStatus.PUBLISHED,
        models=[
            PoolModel(
                id=model_id,
                public_name="gpt-4o",
                display_name="gpt-4o",
                backend_pool_name=backend_pool_name(api_name, model_id, "gpt-4o"),
                members=[
                    PoolMember(
                        model_endpoint_id=endpoint_id,
                        deployment_name="gpt-4o",
                        backend_name=backend,
                    )
                ],
            )
        ],
        resources=[
            PublishedResource(
                kind=PublishedResourceKind.BACKEND,
                name=backend,
                resource_id=f"backends/{backend}",
                created_by_mosaic=True,
            )
        ],
    )


async def test_a_published_pool_blocks_an_environment_change_that_would_break_it(
    client: TestClient,
) -> None:
    gateways = client.app.state.gateway_repository  # type: ignore[attr-defined]
    endpoints = client.app.state.model_endpoint_repository  # type: ignore[attr-defined]
    await gateways.save_gateway(
        Gateway(
            id="gateway-pools",
            tenant_id=TENANT,
            name="apim-contoso-dev",
            azure_resource_id=RESOURCE_ID,
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim-contoso-dev",
            environment="development",
        ),
        _audit(),
    )
    await endpoints.save_endpoint(
        ModelEndpoint(
            id="endpoint-east",
            tenant_id=TENANT,
            name="aoai-east",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://aoai-east.openai.azure.com/",
            environment="development",
        ),
        _audit(),
    )
    pool = await gateways.save_model_pool(
        _published_pool("gateway-pools", "endpoint-east"), _audit()
    )

    refused = client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": "gateway-pools",
                    "environment": "production",
                }
            ]
        },
    )

    assert refused.status_code == 409, refused.text
    details = refused.json()["details"]
    assert details["reason"] == "publicationsBlocked"
    [blocked] = details["publications"]
    assert blocked["kind"] == "pool"
    assert blocked["publicationId"] == pool.id
    assert blocked["displayName"] == "OpenAI"
    assert blocked["modelEndpointId"] == "endpoint-east"
    assert blocked["deploymentName"] == "gpt-4o"
    assert details["suggestedAssignments"] == [
        {
            "resourceKind": "modelEndpoint",
            "resourceId": "endpoint-east",
            "environment": "production",
        }
    ]

    # A draft pool has written nothing, so it doesn't stand in the way.
    await gateways.record_model_pool_state(
        pool.model_copy(update={"status": PublicationStatus.DRAFT, "resources": []})
    )
    moved = client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": "gateway-pools",
                    "environment": "production",
                }
            ]
        },
    )
    assert moved.status_code == 200, moved.text


def _operations(fake: FakeApim, api_name: str) -> set[str]:
    prefix = f"apis/{api_name}/operations/"
    return {
        key.removeprefix(prefix)
        for key in fake.written
        if key.startswith(prefix) and "/" not in key.removeprefix(prefix)
    }


async def test_an_anthropic_pool_reads_the_model_from_the_request_body(estate: Estate) -> None:
    pool = await estate.create(
        "Claude",
        _model(
            _member("foundry-east", "claude-opus-4-5"),
            _member("foundry-west", "claude-opus-4-5"),
            display_name="Claude Opus 4.5",
        ),
    )
    [model] = pool.models
    east, west = model.members

    assert pool.api_shape == ApiShape.ANTHROPIC_MESSAGES
    assert pool.vendor == "Anthropic"
    assert (model.public_name, model.display_name) == ("claude-opus-4-5", "Claude Opus 4.5")
    assert (model.model_name, model.model_format) == ("claude-opus-4-5", "Anthropic")

    await estate.publish(pool.id)

    fake = estate.apim
    assert _operations(fake, pool.api_name) == {"messages", "count-tokens"}
    urls = [
        fake.written[f"backends/{item.backend_name}"]["properties"]["url"] for item in (east, west)
    ]
    # Foundry serves Anthropic models only on its services.ai.azure.com host.
    assert urls == [
        "https://foundry-east.services.ai.azure.com",
        "https://foundry-west.services.ai.azure.com",
    ]
    fragment = _xml(_policy(fake, f"policyFragments/{pool.fragment_name}"))
    requested = fragment.find("set-variable[@name='mosaic-pool-model']")
    assert requested is not None
    assert 'body["model"]' in requested.get("value", "")
    assert list(fragment.iter("rewrite-uri")) == []
    policy = _xml(_policy(fake, f"apis/{pool.api_name}/policies/policy"))
    backend = policy.find("backend")
    assert backend is not None
    # Every member's deployment has the name callers send, so the body goes through untouched.
    assert list(backend.iter("set-body")) == []
    assert [item.get("backend-id") for item in backend.iter("set-backend-service")] == [
        model.backend_pool_name
    ]


async def test_a_safeguard_needs_a_tier_that_meters_anthropic_tokens(estate: Estate) -> None:
    pool = await estate.create(
        "Claude",
        _model(_member("foundry-east", "claude-opus-4-5")),
        safeguard={"tokens_per_minute": 1000},
    )

    with pytest.raises(ConflictError, match="problems are fixed") as refused:
        await estate.service.plan(ACTOR, pool.id)
    [problem] = refused.value.details["problems"]
    assert problem.startswith("This gateway can't apply the pool's safeguard.")
    assert "classic tier" in problem
    assert problem.endswith("Remove the safeguard to publish the pool here.")
    assert (await estate.service.detail(ACTOR, pool.id)).problems == [problem]

    await estate.set_gateway_sku("StandardV2")
    await estate.publish(pool.id)

    fragment = _xml(_policy(estate.apim, f"policyFragments/{pool.fragment_name}"))
    limit = fragment.find("choose/when/llm-token-limit")
    assert limit is not None
    assert limit.get("counter-key") == pool.models[0].id
    assert limit.get("tokens-per-minute") == "1000"


async def test_a_breaker_pool_needs_one_deployment_name_when_the_body_names_the_model(
    estate: Estate,
) -> None:
    pool = await estate.create(
        "Claude",
        _model(
            _member("foundry-east", "claude-opus-4-5"),
            _member("foundry-west", "opus-west"),
            public_name="claude-opus-4-5",
        ),
    )
    east, west = pool.models[0].members

    with pytest.raises(ConflictError, match="problems are fixed") as refused:
        await estate.service.plan(ACTOR, pool.id)
    assert any("same deployment name" in item for item in refused.value.details["problems"])

    # A linear pool names each attempt's backend itself, so it can rewrite the body per attempt.
    await estate.update(pool.id, pool_type="linear")
    await estate.publish(pool.id)

    policy = _xml(_policy(estate.apim, f"apis/{pool.api_name}/policies/policy"))
    attempts = policy.findall("backend/retry/choose/when/choose/when")
    assert [item.find("set-backend-service").get("backend-id") for item in attempts] == [
        east.backend_name,
        west.backend_name,
    ]
    bodies = [item.find("set-body").text or "" for item in attempts]
    assert '"claude-opus-4-5"' in bodies[0]
    assert '"opus-west"' in bodies[1]


async def test_a_linear_pool_tries_each_member_in_order_without_breakers(estate: Estate) -> None:
    pool = await estate.create(
        "OpenAI", _gpt4o("aoai-ptu", "aoai-east", "aoai-sweden"), pool_type="linear"
    )
    model = pool.models[0]

    plan = await estate.service.plan(ACTOR, pool.id)

    assert "backendPool" not in {kind for kind, _, _ in _steps(plan)}
    assert any("spills over to gpt-4o-overflow" in item for item in plan.warnings)
    run = await estate.apply(pool.id, plan)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    fake = estate.apim
    for member in model.members:
        assert "circuitBreaker" not in fake.written[f"backends/{member.backend_name}"]["properties"]
    assert f"backends/{model.backend_pool_name}" not in fake.written
    policy = _xml(_policy(fake, f"apis/{pool.api_name}/policies/policy"))
    retry = policy.find("backend/retry")
    assert retry is not None
    assert retry.get("count") == "2"
    attempts = policy.findall("backend/retry/choose/when/choose/when")
    assert [item.find("set-backend-service").get("backend-id") for item in attempts] == [
        member.backend_name for member in model.members
    ]
    assert [item.get("condition", "")[-5:] for item in attempts] == ["== 1)", "== 2)", "== 3)"]
    backend = policy.find("backend")
    assert backend is not None
    assert list(backend.iter("set-body")) == []

    # Reordering the members rewrites only the order the policy tries them in.
    await estate.update(pool.id, models=[_gpt4o("aoai-sweden", "aoai-east", "aoai-ptu")])
    await estate.publish(pool.id)

    reordered = _xml(_policy(fake, f"apis/{pool.api_name}/policies/policy"))
    assert [
        item.find("set-backend-service").get("backend-id")
        for item in reordered.findall("backend/retry/choose/when/choose/when")
    ] == [member.backend_name for member in reversed(model.members)]
    assert estate.apim.write_paths("DELETE") == []


async def _interrupt(estate: Estate, pool: ModelPool, *, locked: bool = True) -> PublishRun:
    """A run that stopped part way: it wrote its first member's backend, then the process died."""

    plan = await estate.service.plan(ACTOR, pool.id)
    member = pool.models[0].members[0]
    estate.apim.seed(
        f"backends/{member.backend_name}",
        {"properties": {"url": "https://aoai-east.openai.azure.com/openai/deployments/gpt-4o"}},
    )
    run = PublishRun(
        id=new_id("publishrun"),
        tenant_id=TENANT,
        publication_id=pool.id,
        target="pool",
        gateway_id=estate.gateway_id,
        plan_id=plan.id,
        plan_digest=plan.digest,
        actor_object_id=ACTOR.object_id,
        steps=[
            PublishStepResult(
                kind=PublishedResourceKind.BACKEND,
                name=member.backend_name,
                action=PublishAction.CREATE,
                resource_id=f"backends/{member.backend_name}",
                created_by_mosaic=True,
            )
        ],
    )
    repository = estate.gateway_repository
    await repository.save_publish_run(run)
    if locked:
        await repository.acquire_publication_lock(TENANT, pool.id, run.id)
    current = await estate.pool(pool.id)
    await repository.record_model_pool_state(
        current.model_copy(update={"status": PublicationStatus.APPLYING, "last_run_id": run.id})
    )
    return run


async def test_an_interrupted_run_holds_the_pool_until_an_administrator_recovers_it(
    estate: Estate,
) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    member = pool.models[0].members[0]
    run = await _interrupt(estate, pool)

    assert await estate.service.get_lock_owner(ACTOR, pool.id) == run.id
    with pytest.raises(ConflictError, match="require explicit recovery"):
        await estate.update(pool.id, description="Changed")
    with pytest.raises(ConflictError, match="exact run holding"):
        await estate.service.recover_interrupted(
            ACTOR, pool.id, run_id="publishrun-other", confirm_quiesced=True
        )
    # Without the administrator's word that the process stopped, recovery only describes the run.
    described = await estate.service.recover_interrupted(
        ACTOR, pool.id, run_id=run.id, confirm_quiesced=False
    )
    assert described.status == PublishRunStatus.RUNNING
    assert await estate.service.get_lock_owner(ACTOR, pool.id) == run.id
    # A run that still holds its lock is left to recovery, not reaped.
    assert await estate.service.reap_stale_publish_runs(TENANT) == 0

    recovered = await estate.service.recover_interrupted(
        ACTOR, pool.id, run_id=run.id, confirm_quiesced=True
    )

    assert recovered.status == PublishRunStatus.INTERRUPTED
    assert recovered.completed_at is not None
    assert await estate.service.get_lock_owner(ACTOR, pool.id) is None
    failed = await estate.pool(pool.id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.last_plan_id is None
    # What the run wrote is now on the pool's record, so unpublishing removes it.
    assert failed.owned_backends() == {member.backend_name}
    assert "modelPool.recovered" in estate.audit_actions()
    plan = await estate.service.plan_unpublish(ACTOR, pool.id)
    assert ("backend", member.backend_name, "delete") in _steps(plan)


async def test_the_reaper_closes_a_pool_run_that_lost_its_lock(estate: Estate) -> None:
    pool = await estate.create("OpenAI", _gpt4o("aoai-east"))
    run = await _interrupt(estate, pool, locked=False)
    other = PublishRun(
        id=new_id("publishrun"),
        tenant_id=TENANT,
        publication_id="publication-chat",
        gateway_id=estate.gateway_id,
        plan_id="publishplan-chat",
        plan_digest="digest",
    )
    await estate.gateway_repository.save_publish_run(other)

    assert await estate.service.reap_stale_publish_runs(TENANT) == 1

    reaped = await estate.service.get_run(ACTOR, pool.id, run.id)
    assert reaped.status == PublishRunStatus.FAILED
    assert reaped.completed_at is not None
    assert reaped.errors
    failed = await estate.pool(pool.id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.last_error == reaped.errors[-1]
    # A model publication's run is the publishing service's to reap.
    untouched = await estate.gateway_repository.get_publish_run(TENANT, other.id)
    assert untouched is not None
    assert untouched.status == PublishRunStatus.RUNNING
    assert await estate.service.reap_stale_publish_runs(TENANT) == 0


def _route(
    model_id: str, public_name: str, *targets: tuple[str, str], attempts: int | None = None
) -> PoolRoute:
    resolved = tuple(PoolTarget(backend, deployment) for backend, deployment in targets)
    return PoolRoute(model_id, public_name, resolved, attempts or len(resolved))


def _render(
    *routes: PoolRoute,
    shape: ApiShape = ApiShape.AZURE_OPENAI,
    preset: BreakerPreset = BreakerPreset.THROTTLING,
    safeguard: PoolSafeguard | None = None,
) -> tuple[ET.Element, ET.Element]:
    policy = render_pool_policy(
        pool_id="modelpool-1",
        fragment_name="mosaic-pool-models",
        shape=shape,
        routes=list(routes),
        preset=preset,
        safeguard=safeguard,
    )
    return _xml(policy.fragment_xml), _xml(policy.api_policy_xml)


def test_a_pool_policy_needs_a_model() -> None:
    with pytest.raises(ValueError, match="at least one model"):
        _render()


def test_a_model_with_one_attempt_is_sent_once() -> None:
    _, policy = _render(_route("model-a", "gpt-4o", ("backend-a", "gpt-4o")))

    assert policy.find("backend/retry") is None
    assert policy.find("backend/forward-request") is not None
    assert [item.get("backend-id") for item in policy.iter("set-backend-service")] == ["backend-a"]


def test_the_longest_route_sets_how_often_a_request_is_retried() -> None:
    fragment, policy = _render(
        _route("model-a", "gpt-4o", ("pool-a", "gpt-4o"), attempts=4),
        _route("model-b", "gpt-4o-mini", ("pool-b", "gpt-4o-mini"), attempts=2),
        preset=BreakerPreset.THROTTLING_AND_ERRORS,
    )

    retry = policy.find("backend/retry")
    assert retry is not None
    assert (retry.get("count"), retry.get("interval"), retry.get("first-fast-retry")) == (
        "3",
        "0",
        "true",
    )
    condition = retry.get("condition", "")
    for status in (429, 500, 502, 503, 504):
        assert f"context.Response.StatusCode == {status}" in condition
    # A pool whose every member is tripped answers 503 itself; trying it again can't succeed.
    assert 'StatusReason.Contains("Backend pool")' in condition
    assert 'GetValueOrDefault<int>("mosaic-pool-attempts", 1)' in condition
    routes = fragment.find("choose")
    assert routes is not None
    assert {
        when.find("set-variable[@name='mosaic-pool-model-id']").get("value"): when.find(
            "set-variable[@name='mosaic-pool-attempts']"
        ).get("value")
        for when in routes.findall("when")
    } == {"model-a": "@(4)", "model-b": "@(2)"}
    failure = policy.find("outbound/choose/when")
    assert failure is not None
    assert "context.Response.StatusCode == 502" in failure.get("condition", "")


def test_the_throttling_preset_cascades_only_on_throttling() -> None:
    _, policy = _render(_route("model-a", "gpt-4o", ("pool-a", "gpt-4o"), attempts=3))

    retry = policy.find("backend/retry")
    assert retry is not None
    condition = retry.get("condition", "")
    assert "StatusCode == 429" in condition
    assert "StatusCode == 503" in condition
    assert "StatusCode == 500" not in condition


def test_a_safeguard_limits_and_meters_each_pool_model() -> None:
    fragment, _ = _render(
        _route("model-a", "claude-opus-4-5", ("pool-a", "claude-opus-4-5"), attempts=2),
        shape=ApiShape.ANTHROPIC_MESSAGES,
        safeguard=PoolSafeguard(
            tokens_per_minute=5000, token_quota=1000000, token_quota_period="Daily"
        ),
    )

    when = fragment.find("choose/when")
    assert when is not None
    limit = when.find("llm-token-limit")
    assert limit is not None
    assert limit.attrib == {
        "counter-key": "model-a",
        "estimate-prompt-tokens": "false",
        "tokens-per-minute": "5000",
        "token-quota": "1000000",
        "token-quota-period": "Daily",
    }
    metric = when.find("llm-emit-token-metric")
    assert metric is not None
    assert {item.get("name"): item.get("value") for item in metric.findall("dimension")} == {
        "Pool": "modelpool-1",
        "Model": "claude-opus-4-5",
    }
    # A model the pool doesn't offer is refused before any limit counts it.
    refusal = fragment.find("choose/otherwise/return-response/set-status")
    assert refusal is not None
    assert refusal.get("code") == "404"


def test_once_one_target_rewrites_the_model_every_target_does() -> None:
    _, policy = _render(
        _route(
            "model-a",
            "claude-opus-4-5",
            ("backend-east", "claude-opus-4-5"),
            ("backend-west", "opus-west"),
        ),
        shape=ApiShape.ANTHROPIC_MESSAGES,
    )

    attempts = policy.findall("backend/retry/choose/when/choose/when")
    assert [item.get("condition") for item in attempts] == [
        '@(context.Variables.GetValueOrDefault<int>("mosaic-pool-attempt", 1) == 1)',
        '@(context.Variables.GetValueOrDefault<int>("mosaic-pool-attempt", 1) == 2)',
    ]
    bodies = [item.find("set-body").text or "" for item in attempts]
    assert 'body["model"] = "claude-opus-4-5"' in bodies[0]
    assert 'body["model"] = "opus-west"' in bodies[1]


def test_the_outbound_section_hides_which_member_answered() -> None:
    _, policy = _render(_route("model-a", "gpt-4o", ("pool-a", "gpt-4o"), attempts=2))

    deleted = {
        item.get("name")
        for item in policy.findall("outbound/set-header")
        if item.get("exists-action") == "delete"
    }
    assert {"x-ms-region", "x-ms-deployment-name", "x-ratelimit-remaining-tokens"} <= deleted
    assert "retry-after" not in {name.casefold() for name in deleted if name}


def _attempt_message(container: ET.Element) -> str:
    tags = [child.tag for child in container]
    # The trace follows the attempt it records, so a retry records every attempt.
    assert tags[tags.index("forward-request") + 1] == "trace"
    trace = container.find("trace")
    assert trace is not None
    assert trace.attrib == {"source": "mosaic", "severity": "information"}
    return trace.findtext("message") or ""


def test_each_attempt_records_where_it_went_and_how_it_was_answered() -> None:
    _, policy = _render(
        _route("model-a", "gpt-4o", ("pool-a", "gpt-4o"), attempts=3),
        _route("model-b", "gpt-4o-mini", ("backend-b", "gpt-4o-mini"), ("backend-c", "mini")),
    )

    retry = policy.find("backend/retry")
    assert retry is not None
    message = _attempt_message(retry)
    assert message.startswith('@("mosaic-attempt v=1 m=" + ')
    # Readers take the first value of each key, so the path, part of which the caller chose,
    # comes last.
    positions = [message.index(f' {key}="') for key in "mnbsehp"]
    assert positions == sorted(positions)
    assert all(message.count(f' {key}="') == 1 for key in "mnbsehp")
    for read in (
        'GetValueOrDefault<string>("mosaic-pool-model-id", "")',
        'GetValueOrDefault<int>("mosaic-pool-attempt", 0)',
        'GetValueOrDefault<string>("mosaic-pool-backend", "")',
        "context.Response.StatusCode",
        'StatusReason.Contains("Backend pool")',
        "context.Request.Url.Host",
    ):
        assert read in message, read
    assert message.endswith(' + " p=" + context.Request.Url.Path)')
    for withheld in ("Query", "Headers", "Body", "OriginalUrl"):
        assert withheld not in message, withheld

    # Each target names its backend, and a backend pool's attempts name the backend pool.
    named: list[str | None] = []
    for parent in policy.iter():
        children = list(parent)
        for index, child in enumerate(children):
            if child.tag != "set-backend-service":
                continue
            follower = children[index + 1]
            assert follower.attrib == {
                "name": "mosaic-pool-backend",
                "value": child.get("backend-id"),
            }
            named.append(follower.get("value"))
    assert named == ["pool-a", "backend-b", "backend-c"]


def test_a_request_sent_once_records_its_one_attempt() -> None:
    _, policy = _render(_route("model-a", "gpt-4o", ("backend-a", "gpt-4o")))

    backend = policy.find("backend")
    assert backend is not None
    assert [child.tag for child in backend][-2:] == ["forward-request", "trace"]
    assert _attempt_message(backend).startswith('@("mosaic-attempt v=1 m=" + ')


def test_the_attempt_trace_is_explained_without_naming_a_member() -> None:
    policy = render_pool_policy(
        pool_id="modelpool-1",
        fragment_name="mosaic-pool-models",
        shape=ApiShape.AZURE_OPENAI,
        routes=[_route("model-a", "gpt-4o", ("pool-a", "gpt-4o"), attempts=2)],
        preset=BreakerPreset.THROTTLING,
        safeguard=None,
    )

    (facet,) = [facet for facet in policy.facets if facet.element == "trace"]
    assert facet.summary == ATTEMPT_TRACE_SUMMARY
    assert facet.attributes == {"trace": "attempt"}
    withheld = "never records a query string, a header, or a request or response body"
    assert withheld in facet.details[0]
    text = facet.model_dump_json()
    for hidden in ("pool-a", "model-a", "mosaic-attempt", "context."):
        assert hidden not in text, hidden


def _await_pool_run(client: TestClient, pool_id: str, run_id: str) -> dict[str, Any]:
    for _ in range(250):
        run = client.get(f"/api/v1/model-pools/{pool_id}/runs/{run_id}")
        assert run.status_code == 200, run.text
        if run.json()["status"] != "running":
            return dict(run.json())
        time.sleep(0.02)
    raise AssertionError("The pool run did not finish")


async def test_an_administrator_publishes_and_unpublishes_a_pool_over_http(
    client: TestClient, estate: Estate
) -> None:
    app: FastAPI = client.app  # type: ignore[assignment]
    app.state.model_pool_service = estate.service
    gateway_id = estate.gateway_id

    candidates = client.get(f"/api/v1/gateways/{gateway_id}/pool-candidates")
    assert candidates.status_code == 200, candidates.text
    offered = {(item["modelName"], item["apiShape"]): item for item in candidates.json()["models"]}
    assert ("gpt-4o", ApiShape.FOUNDRY_MODELS.value) in offered
    assert {
        (item["endpointName"], item["deploymentName"], item["eligible"])
        for item in offered[("gpt-4o", ApiShape.AZURE_OPENAI.value)]["deployments"]
        if item["endpointName"] in {"aoai-east", "aoai-key"}
    } == {
        ("aoai-east", "gpt-4o", True),
        ("aoai-east", "gpt-4o-batch", False),
        ("aoai-key", "gpt-4o", True),
    }
    suggested = client.get("/api/v1/model-pool-suggestions")
    assert suggested.status_code == 200, suggested.text
    assert [(item["vendor"], item["apiShape"]) for item in suggested.json()] == [
        ("Anthropic", ApiShape.ANTHROPIC_MESSAGES.value),
        ("OpenAI", ApiShape.AZURE_OPENAI.value),
    ]
    assert suggested.json()[1]["models"][0] == {
        "modelName": "gpt-4o",
        "modelFormat": "OpenAI",
        "deploymentCount": 5,
        "endpointCount": 4,
        "regions": ["eastus", "eastus2", "swedencentral", "westeurope"],
    }

    body = {
        "gatewayId": gateway_id,
        "displayName": "OpenAI",
        "models": [
            {
                "displayName": "GPT-4o",
                "members": [
                    {"modelEndpointId": _endpoint_id("aoai-east"), "deploymentName": "gpt-4o"},
                    {"modelEndpointId": _endpoint_id("aoai-sweden"), "deploymentName": "gpt-4o"},
                ],
            }
        ],
    }
    created = client.post("/api/v1/model-pools", json=body)
    assert created.status_code == 201, created.text
    pool = created.json()
    pool_id = pool["id"]
    assert (pool["apiName"], pool["poolType"], pool["status"]) == (
        "mosaic-pool-openai",
        "breaker",
        "draft",
    )
    assert client.post("/api/v1/model-pools", json=body).status_code == 409
    assert (
        client.post(
            "/api/v1/model-pools", json={**body, "apiName": "openai-two", "poolType": "fastest"}
        ).status_code
        == 422
    )
    assert [item["id"] for item in client.get("/api/v1/model-pools").json()] == [pool_id]
    assert client.get(f"/api/v1/model-pools?gateway={gateway_id}").json()[0]["id"] == pool_id
    assert client.get(f"/api/v1/model-pools/{pool_id}").json()["displayName"] == "OpenAI"
    detail = client.get(f"/api/v1/model-pools/{pool_id}/detail")
    assert detail.status_code == 200, detail.text
    [model] = detail.json()["models"]
    assert [member["endpointName"] for member in model["members"]] == ["aoai-east", "aoai-sweden"]
    assert detail.json()["problems"] == []
    [summary] = client.get(f"/api/v1/model-pool-summaries?gateway={gateway_id}").json()
    assert summary["pool"]["id"] == pool_id
    assert summary["capacity"] == {"payAsYouGo": 2}
    assert summary["problemCount"] == 0
    used = client.get(f"/api/v1/model-endpoints/{_endpoint_id('aoai-east')}/pools")
    assert used.status_code == 200, used.text
    [use] = used.json()
    assert (
        use["pool"]["id"],
        use["gatewayName"],
        use["pool"]["status"],
        use["pool"]["visibility"],
    ) == (pool_id, "apim-contoso-dev", "draft", "listed")
    assert [
        (item["deploymentName"], item["modelDisplayName"], item["drained"], item["warning"])
        for item in use["deployments"]
    ] == [("gpt-4o", "GPT-4o", False, None)]
    assert client.get("/api/v1/model-endpoints/ep-missing/pools").status_code == 404
    # The pool now serves gpt-4o here, so only Anthropic is still suggested.
    assert [item["vendor"] for item in client.get("/api/v1/model-pool-suggestions").json()] == [
        "Anthropic"
    ]

    unplanned = client.post(f"/api/v1/model-pools/{pool_id}/apply")
    assert unplanned.status_code == 409, unplanned.text
    assert unplanned.json()["details"]["reason"] == "planRequired"
    plan = client.post(f"/api/v1/model-pools/{pool_id}/plan")
    assert plan.status_code == 200, plan.text
    plan_id = plan.json()["id"]
    reviewed = client.get(f"/api/v1/model-pool-plans/{plan_id}")
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["digest"] == plan.json()["digest"]

    started = client.post(f"/api/v1/model-pools/{pool_id}/apply", params={"plan": plan_id})
    assert started.status_code == 202, started.text
    run = _await_pool_run(client, pool_id, started.json()["id"])
    assert run["status"] == "succeeded", run["errors"]
    assert [item["id"] for item in client.get(f"/api/v1/model-pools/{pool_id}/runs").json()] == [
        run["id"]
    ]
    assert client.get(f"/api/v1/model-pools/{pool_id}/lock").json()["ownerId"] is None
    assert client.get(f"/api/v1/model-pools/{pool_id}").json()["status"] == "published"
    # A pool with resources in API Management has to be unpublished before it's removed.
    assert client.delete(f"/api/v1/model-pools/{pool_id}").status_code == 409
    recovered = client.post(
        f"/api/v1/model-pools/{pool_id}/recover",
        json={"runId": run["id"], "confirmQuiesced": True},
    )
    assert recovered.status_code == 409, recovered.text

    removal = client.post(f"/api/v1/model-pools/{pool_id}/unpublish-plan")
    assert removal.status_code == 200, removal.text
    assert removal.json()["operation"] == "unpublish"
    assert client.post(f"/api/v1/model-pools/{pool_id}/unpublish").status_code == 409
    started = client.post(
        f"/api/v1/model-pools/{pool_id}/unpublish", params={"plan": removal.json()["id"]}
    )
    assert started.status_code == 202, started.text
    run = _await_pool_run(client, pool_id, started.json()["id"])
    assert run["status"] == "succeeded", run["errors"]

    assert client.delete(f"/api/v1/model-pools/{pool_id}").status_code == 204
    assert client.get(f"/api/v1/model-pools/{pool_id}").status_code == 404
    assert client.get(f"/api/v1/model-pools/{pool_id}/detail").status_code == 404
    assert client.get("/api/v1/model-pool-plans/publishplan-unknown").status_code == 404
