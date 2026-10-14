"""Portal model identity is read-only, caller-scoped, and distinct from resource naming."""

import asyncio
from collections import Counter
from unittest.mock import AsyncMock

import pytest
from mosaic_api.config import Settings
from mosaic_api.domain import (
    AccessRequestCreate,
    CatalogVisibility,
    DeclaredDeployment,
    EntitlementResource,
    EntitlementSubject,
    ModelEndpoint,
)
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from test_portal_api import ACTOR, CALLER_OID, TENANT, Harness, _audit
from test_portal_availability import _publish_model_api


async def estate() -> Harness:
    harness = Harness()
    await harness.add_gateway()
    await harness.endpoints.create_endpoint(
        ModelEndpoint(
            id="endpoint",
            tenant_id=TENANT,
            name="Endpoint",
            provider="azureOpenAi",
            endpoint="https://fictional.openai.azure.com",
        ),
        _audit(),
    )
    await harness.endpoints.replace_observed_for_endpoint(
        TENANT,
        "endpoint",
        [
            ObservedModelDeployment(
                id=f"deployment-{name}",
                tenant_id=TENANT,
                endpoint_id="endpoint",
                snapshot_id="snapshot",
                deployment_name=name,
                model_name="gpt-4o",
                model_format="OpenAI",
                model_version="2024-11-20",
            )
            for name in ("first", "second")
        ],
        "snapshot",
    )
    return harness


async def test_published_models_have_identity_without_changing_resource_names() -> None:
    harness = await estate()
    await _publish_model_api(harness, "first", applied=True)
    await _publish_model_api(harness, "second", applied=True)
    read = AsyncMock(wraps=harness.endpoints.list_observed_for_endpoint)
    harness.endpoints.list_observed_for_endpoint = read

    entries = await harness.service.catalog(ACTOR)

    assert [entry.display_name for entry in entries] == ["Model first", "Model second"]
    assert [entry.model.model_dump() for entry in entries if entry.model] == [
        {"name": "gpt-4o", "format": "OpenAI", "version": "2024-11-20"}
    ] * 2
    assert read.await_count == 1
    assert entries[0].model is not None
    assert set(entries[0].model.model_dump()) == {"name", "format", "version"}


@pytest.mark.parametrize("surface", ["catalog", "my_entitlements", "my_access_requests"])
async def test_response_loads_distinct_endpoint_inventories_concurrently(surface: str) -> None:
    harness = await estate()
    endpoint = await harness.endpoints.get_endpoint(TENANT, "endpoint")
    assert endpoint is not None
    for api_id in ("second", "third"):
        await harness.endpoints.create_endpoint(
            endpoint.model_copy(update={"id": f"endpoint-{api_id}"}), _audit()
        )
        await harness.endpoints.replace_observed_for_endpoint(
            TENANT,
            f"endpoint-{api_id}",
            [
                ObservedModelDeployment(
                    id=f"inventory-{api_id}",
                    tenant_id=TENANT,
                    endpoint_id=f"endpoint-{api_id}",
                    snapshot_id="snapshot",
                    deployment_name=api_id,
                    model_name=f"model-{api_id}",
                    model_format="OpenAI",
                )
            ],
            "snapshot",
        )
    principal = await harness.add_principal(CALLER_OID)
    for api_id in ("first", "second", "third", "fourth"):
        model_api = await _publish_model_api(harness, api_id, applied=True)
        assert model_api.publication_id is not None
        publication = await harness.gateways.get_publication(TENANT, model_api.publication_id)
        assert publication is not None
        await harness.gateways.save_publication(
            publication.model_copy(
                update={
                    "model_endpoint_id": f"endpoint-{api_id}"
                    if api_id in {"second", "third"}
                    else "endpoint",
                    "deployment_name": "first" if api_id == "fourth" else api_id,
                }
            ),
            _audit(),
        )
        resource = EntitlementResource(kind="modelApi", id=api_id)
        if surface == "my_entitlements":
            await harness.grant(EntitlementSubject(kind="user", id=principal.id), resource)
        elif surface == "my_access_requests":
            await harness.service.create_access_request(
                ACTOR, AccessRequestCreate(resource=resource)
            )
    started: set[str] = set()
    all_started = asyncio.Event()
    original = harness.endpoints.list_observed_for_endpoint

    async def read_inventory(
        model: type[ObservedModelDeployment], tenant_id: str, endpoint_id: str, entity_type: str
    ) -> list[ObservedModelDeployment]:
        started.add(endpoint_id)
        if len(started) == 3:
            all_started.set()
        await all_started.wait()
        return await original(model, tenant_id, endpoint_id, entity_type)

    read = AsyncMock(side_effect=read_inventory)
    harness.endpoints.list_observed_for_endpoint = read
    entries = await asyncio.wait_for(getattr(harness.service, surface)(ACTOR), timeout=2)

    assert sorted(entry.model.name for entry in entries if entry.model) == [
        "gpt-4o",
        "gpt-4o",
        "model-second",
        "model-third",
    ]
    assert Counter(call.args[2] for call in read.await_args_list) == {
        "endpoint": 1,
        "endpoint-second": 1,
        "endpoint-third": 1,
    }


async def test_grants_and_requests_get_the_same_identity_but_private_requests_do_not() -> None:
    harness = await estate()
    record = await _publish_model_api(harness, "first", applied=True)
    principal = await harness.add_principal(CALLER_OID)
    resource = EntitlementResource(kind="modelApi", id="first")
    await harness.grant(EntitlementSubject(kind="user", id=principal.id), resource)
    await _publish_model_api(harness, "second", applied=True)
    request = await harness.service.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="second"))
    )
    [grant] = await harness.service.my_entitlements(ACTOR)
    assert grant.model == request.model
    assert grant.resource_display_name == "Model first"
    assert request.resource_display_name == "Model second"
    second = await harness.gateways.get_model_api(TENANT, "second")
    assert second is not None
    await harness.gateways.save_model_api(
        second.model_copy(update={"visibility": CatalogVisibility.PRIVATE}), _audit()
    )
    [hidden] = await harness.service.my_access_requests(ACTOR)
    assert hidden.model is None
    await harness.gateways.save_model_api(
        record.model_copy(update={"visibility": CatalogVisibility.PRIVATE}), _audit()
    )
    [held] = await harness.service.my_entitlements(ACTOR)
    assert held.model == grant.model


@pytest.mark.parametrize("case", ["missing-endpoint", "missing-deployment", "adopted"])
async def test_unknown_inventory_never_guesses_a_model_from_a_title(case: str) -> None:
    harness = await estate()
    if case == "adopted":
        await harness.add_model_api("first")
    else:
        await _publish_model_api(harness, "first", applied=True)
        if case == "missing-endpoint":
            harness.endpoints.endpoints.clear()
        else:
            await harness.endpoints.replace_observed_for_endpoint(TENANT, "endpoint", [], "new")
    [entry] = await harness.service.catalog(ACTOR)
    assert entry.model is None
    assert entry.display_name == "Model first"


async def test_declared_deployment_identity_is_read_without_cloud_calls() -> None:
    harness = await estate()
    endpoint = await harness.endpoints.get_endpoint(TENANT, "endpoint")
    assert endpoint is not None
    await harness.endpoints.save_endpoint(
        endpoint.model_copy(
            update={
                "auth_mode": "apiKey",
                "declared_deployments": [
                    DeclaredDeployment(
                        deployment_name="first", model_name="gpt-4.1", api_shape="azureOpenAi"
                    )
                ],
            }
        ),
        _audit(),
    )
    await _publish_model_api(harness, "first", applied=True)
    [entry] = await harness.service.catalog(ACTOR)
    assert entry.model is not None
    assert entry.model.name == "gpt-4.1"
    assert entry.model.format == "azureOpenAi"


def test_model_descriptor_is_optional_and_only_on_portal_responses(settings: Settings) -> None:
    schemas = create_app(settings).openapi()["components"]["schemas"]
    for name in ("CatalogEntry", "PortalAccessRequest", "PortalResolvedEntitlement"):
        assert "model" in schemas[name]["properties"]
        assert "model" not in schemas[name]["required"]
    for name in ("AccessRequest", "ResolvedEntitlement", "ResourceSummary"):
        assert "model" not in schemas[name]["properties"]
