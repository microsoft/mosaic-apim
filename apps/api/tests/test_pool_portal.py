"""Pool models in the portal's catalog, and the requests people make for them (ADR 0024).

A pool model is offered as a model: its name, its gateway's environment, the API it's called with,
and the kind of capacity that serves it. The catalog lists it only while an administrator lists it
and its pool, the pool governs access, and the gateway serves it. Nothing a portal user reads names
the pool, its endpoints, their regions, or the deployments behind the model.
"""

import json
from typing import Any

import pytest
from mosaic_api.domain import (
    AccessRequestCreate,
    AccessRequestState,
    CatalogEntry,
    EntitlementResource,
    ModelAccessSettings,
    Principal,
    PublishRunStatus,
)
from mosaic_api.model_pools import ModelPool, ModelPoolType, ModelPoolVisibility
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.portal import PortalService
from test_model_pools import TENANT, _gpt4o, _member, _model
from test_pool_governed import GENERAL, GovernedEstate

# One model spread over two regions, under a name that's neither deployment's.
CHAT = _model(
    _member("aoai-east", "gpt-4o"),
    _member("aoai-sweden", "gpt-4o"),
    public_name="contoso-chat",
    display_name="Contoso Chat",
)
POOL_NAME = "East and Sweden GPT"
# What would tell a portal user where a pool model runs: the pool's name and description, the
# members' endpoints, hosts, and regions, and the deployments they serve.
BEHIND_THE_MODEL = (
    POOL_NAME,
    "aoai-east",
    "aoai-sweden",
    "openai.azure.com",
    "eastus2",
    "swedencentral",
    "gpt-4o",
)


class Catalog(GovernedEstate):
    """The governed pool estate, with the portal's read model over the same records."""

    def __init__(self) -> None:
        super().__init__()
        self.portal = PortalService(
            EntitlementService(
                self.entitlements,
                directory_repository=self.directory,
                gateway_repository=self.gateway_repository,
                endpoint_repository=self.endpoint_repository,
                cost_center_repository=self.cost_centers,
            ),
            directory_repository=self.directory,
            gateway_repository=self.gateway_repository,
            endpoint_repository=self.endpoint_repository,
        )

    async def draft(self, *models: dict[str, Any], **changes: Any) -> ModelPool:
        """A governed pool that has never been applied."""

        pool = await self.create(
            POOL_NAME,
            *(models or (CHAT,)),
            description="GPT-4o from eastus2 and swedencentral.",
        )
        return await self.update(pool.id, governed_access=ModelAccessSettings(), **changes)

    async def published(self, *models: dict[str, Any], **changes: Any) -> ModelPool:
        """A governed pool, applied, so its gateway serves its models."""

        pool = await self.draft(*models, **changes)
        await self.publish(pool.id)
        return await self.pool(pool.id)

    async def reader(self, label: str) -> tuple[Principal, Actor]:
        principal = await self.principal(label)
        return principal, Actor(object_id=principal.object_id, tenant_id=TENANT)

    async def entries(self, actor: Actor) -> list[CatalogEntry]:
        return [entry for entry in await self.portal.catalog(actor) if entry.kind == "poolModel"]


@pytest.fixture
async def catalog() -> Catalog:
    built = Catalog()
    await built.setup()
    return built


def _resource(pool: ModelPool, index: int = 0) -> EntitlementResource:
    return EntitlementResource(kind="poolModel", id=pool.models[index].id, scope_id=pool.id)


# -- what the catalog offers ----------------------------------------------------------------------


async def test_a_served_pool_model_is_offered_as_a_model(catalog: Catalog) -> None:
    pool = await catalog.published()
    gateway = await catalog.gateway_repository.get_gateway(TENANT, catalog.gateway_id)
    assert gateway is not None
    _, ada = await catalog.reader("Ada")

    [entry] = await catalog.entries(ada)

    assert (entry.id, entry.scope_id) == (pool.models[0].id, pool.id)
    assert entry.display_name == "Contoso Chat"
    # Never the pool's description, which names the regions behind the model.
    assert entry.summary is None
    assert (entry.gateway_id, entry.gateway_name, entry.environment) == (
        gateway.id,
        gateway.name,
        "development",
    )
    assert entry.api_style == pool.api_shape == "azureOpenAi"
    assert entry.capacity == "payAsYouGo"
    assert not entry.entitled
    assert entry.request_state is None
    # The portal reads these names, and needs the scope to ask for the model.
    shown = entry.model_dump(mode="json")
    assert (shown["kind"], shown["scopeId"], shown["apiStyle"], shown["capacity"]) == (
        "poolModel",
        pool.id,
        "azureOpenAi",
        "payAsYouGo",
    )


async def test_an_administrator_lists_a_pools_models_one_by_one(catalog: Catalog) -> None:
    hidden = _model(_member("aoai-east", "gpt-4o-mini"), display_name="GPT-4o mini", listed=False)
    await catalog.published(CHAT, hidden)
    _, ada = await catalog.reader("Ada")

    assert [entry.display_name for entry in await catalog.entries(ada)] == ["Contoso Chat"]


@pytest.mark.parametrize(
    "case",
    [
        "hidden-pool",
        "unlisted-model",
        "ungoverned",
        "never-applied",
        "unpublished",
        "gateway-removed",
    ],
)
async def test_the_catalog_leaves_out_a_pool_model_no_one_could_use(
    catalog: Catalog, case: str
) -> None:
    if case == "hidden-pool":
        pool = await catalog.published()
        await catalog.update(pool.id, visibility=ModelPoolVisibility.HIDDEN)
    elif case == "unlisted-model":
        await catalog.published({**CHAT, "listed": False})
    elif case == "ungoverned":
        # Until a pool governs access, it's reachable only through its bootstrap subscription.
        pool = await catalog.create(POOL_NAME, CHAT)
        await catalog.publish(pool.id)
    elif case == "never-applied":
        await catalog.draft()
    elif case == "unpublished":
        pool = await catalog.published()
        run = await catalog.unpublish(pool.id)
        assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    else:
        await catalog.published()
        catalog.gateway_repository.gateways.pop(catalog.gateway_id)
    _, ada = await catalog.reader("Ada")

    assert await catalog.entries(ada) == []


# -- capacity -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model", "pool_type", "capacity"),
    [
        (_gpt4o("aoai-east", "aoai-sweden"), ModelPoolType.BREAKER, "payAsYouGo"),
        (_gpt4o("aoai-ptu"), ModelPoolType.BREAKER, "provisioned"),
        (_gpt4o("aoai-ptu", "aoai-east"), ModelPoolType.PREFERENTIAL, "provisionedWithOverflow"),
        # A drained member takes no traffic, so it doesn't change what serves the model.
        (
            _model(_member("aoai-east", "gpt-4o", drained=True), _member("aoai-ptu", "gpt-4o")),
            ModelPoolType.BREAKER,
            "provisioned",
        ),
    ],
)
async def test_the_badge_says_what_capacity_serves_the_model(
    catalog: Catalog, model: dict[str, Any], pool_type: ModelPoolType, capacity: str
) -> None:
    await catalog.published(model, pool_type=pool_type)
    _, ada = await catalog.reader("Ada")

    [entry] = await catalog.entries(ada)

    assert entry.capacity == capacity


async def test_a_pool_that_hides_capacity_shows_no_badge(catalog: Catalog) -> None:
    await catalog.published(show_capacity=False)
    _, ada = await catalog.reader("Ada")

    [entry] = await catalog.entries(ada)

    assert entry.capacity is None


async def test_no_badge_while_mosaic_cant_tell_what_serves_the_model(catalog: Catalog) -> None:
    await catalog.published()
    # Inventory no longer sees one member's deployment. No badge is better than a wrong one.
    await catalog.observe("aoai-sweden", [])
    _, ada = await catalog.reader("Ada")

    [entry] = await catalog.entries(ada)

    assert entry.capacity is None


# -- the caller's position ------------------------------------------------------------------------


async def test_a_holder_sees_the_cost_centers_they_hold_the_model_under(catalog: Catalog) -> None:
    pool = await catalog.published()
    ada, ada_actor = await catalog.reader("Ada")
    _, bob = await catalog.reader("Bob")
    await catalog.grant(pool, ada)

    [held] = await catalog.entries(ada_actor)
    [other] = await catalog.entries(bob)

    assert held.entitled
    assert held.entitled_cost_center_ids == [GENERAL]
    assert not other.entitled
    assert other.entitled_cost_center_ids == []


async def test_a_request_for_a_pool_model_shows_in_the_catalog_and_my_requests(
    catalog: Catalog,
) -> None:
    pool = await catalog.published()
    _, bob = await catalog.reader("Bob")

    created = await catalog.portal.create_access_request(
        bob, AccessRequestCreate(resource=_resource(pool))
    )

    assert created.resource_display_name == "Contoso Chat"
    assert created.resource_summary is not None
    assert created.resource_summary.available
    assert created.resource_summary.environment == "development"
    [entry] = await catalog.entries(bob)
    assert entry.request_state == AccessRequestState.PENDING
    assert entry.requested_cost_center_ids == [GENERAL]

    # Once it's out of the catalog, the request shows only what it recorded when it was made.
    await catalog.update(pool.id, visibility=ModelPoolVisibility.HIDDEN)
    [request] = await catalog.portal.my_access_requests(bob)

    assert request.resource_display_name == "Contoso Chat"
    assert request.resource_summary is not None
    assert request.resource_summary.display_name == "Contoso Chat"
    assert not request.resource_summary.available


async def test_nothing_a_portal_user_reads_says_where_a_pool_model_runs(catalog: Catalog) -> None:
    pool = await catalog.published()
    ada, ada_actor = await catalog.reader("Ada")
    _, bob = await catalog.reader("Bob")
    await catalog.grant(pool, ada)
    await catalog.portal.create_access_request(bob, AccessRequestCreate(resource=_resource(pool)))

    offered = [
        *(entry.model_dump(mode="json") for entry in await catalog.entries(ada_actor)),
        *(entry.model_dump(mode="json") for entry in await catalog.entries(bob)),
        *(item.model_dump(mode="json") for item in await catalog.portal.my_access_requests(bob)),
    ]
    held = [
        item.model_dump(mode="json") for item in await catalog.portal.my_entitlements(ada_actor)
    ]
    assert len(offered) == 3
    assert len(held) == 1

    read = json.dumps([offered, held]).casefold()
    for detail in BEHIND_THE_MODEL:
        assert detail.casefold() not in read, detail
    # The catalog and requests carry the pool only as an opaque ID, and the model only as a kind.
    words = json.dumps(offered)
    for opaque in (pool.id, *(model.id for model in pool.models)):
        words = words.replace(opaque, "")
    assert "pool" not in words.replace('"poolModel"', "").casefold()
