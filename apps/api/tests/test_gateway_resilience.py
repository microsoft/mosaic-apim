"""Regression tests for concurrency and data-integrity defects found in review.

Each test here corresponds to a specific way a naive implementation silently loses or corrupts an
administrator's data. They exist because none of the happy-path tests caught them.
"""

import asyncio
import json

import pytest
from apim_double import RESOURCE_ID, FakeApim
from conftest import build_gateway_service
from mosaic_api.domain import GatewayCreate, GatewayStatus, GatewaySyncStatus, GatewayUpdate
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.integrations.apim.policy_semantics import analyze_policy, sanitize_url
from mosaic_api.repositories import InMemoryGatewayRepository
from mosaic_api.services import GatewayService
from mosaic_api.services.directory import Actor

ACTOR = Actor(object_id="admin-object-id", tenant_id="tenant-test")


async def _drain(service: GatewayService) -> None:
    await asyncio.gather(*tuple(service._tasks), return_exceptions=True)


async def test_deleting_during_a_sync_does_not_resurrect_the_gateway(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    service = build_gateway_service(fake_apim, gateway_repository)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    await service.start_sync(ACTOR, gateway.id)
    await service.delete(ACTOR, gateway.id)
    await _drain(service)

    assert gateway_repository.gateways == {}
    assert gateway_repository.observed == {}
    with pytest.raises(NotFoundError):
        await service.get_gateway(ACTOR, gateway.id)


async def test_editing_during_a_sync_is_not_reverted(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    await service.start_sync(ACTOR, gateway.id)
    await service.update(
        ACTOR, gateway.id, GatewayUpdate(name="Production gateway", environment_label="prod")
    )
    await _drain(service)

    refreshed = await service.get_gateway(ACTOR, gateway.id)
    assert refreshed.name == "Production gateway"
    assert refreshed.environment_label == "prod"
    assert refreshed.inventory.apis == 2


async def test_a_failed_section_does_not_delete_previously_observed_data(
    fake_apim: FakeApim,
) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    await service.sync_now(ACTOR, gateway.id)
    assert len(await service.list_subscriptions(ACTOR, gateway.id)) == 1

    fake_apim.fail_always("subscriptions", 500)
    run = await service.sync_now(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.PARTIAL
    # The read failed, so MOSAIC must not report the subscription as removed.
    assert len(await service.list_subscriptions(ACTOR, gateway.id)) == 1
    assert len(await service.list_apis(ACTOR, gateway.id)) == 2


async def test_a_recovered_section_sweeps_its_stale_documents(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    service = build_gateway_service(fake_apim, gateway_repository)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    await service.sync_now(ACTOR, gateway.id)

    fake_apim.fail_always("subscriptions", 500)
    await service.sync_now(ACTOR, gateway.id)
    fake_apim.persistent_failures.clear()
    run = await service.sync_now(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.SUCCEEDED
    assert len(await service.list_subscriptions(ACTOR, gateway.id)) == 1
    # Once every section reads cleanly the snapshot is authoritative again, so nothing older
    # should survive.
    snapshots = {entity.snapshot_id for entity in gateway_repository.observed.values()}
    assert len(snapshots) == 1


async def test_a_persistence_failure_marks_the_run_failed(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    service = build_gateway_service(fake_apim, gateway_repository)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    async def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("cosmos is throttling")

    gateway_repository.replace_observed = explode  # type: ignore[method-assign]

    run = await service.sync_now(ACTOR, gateway.id)

    assert run.status == GatewaySyncStatus.FAILED
    assert any("throttling" in error for error in run.errors)
    assert run.completed_at is not None
    refreshed = await service.get_gateway(ACTOR, gateway.id)
    assert refreshed.status == GatewayStatus.DEGRADED
    assert refreshed.last_sync_error is not None
    assert await gateway_repository.list_unfinished_sync_runs(ACTOR.tenant_id) == []


async def test_a_failing_sync_releases_the_gateway_for_a_retry(
    fake_apim: FakeApim, gateway_repository: InMemoryGatewayRepository
) -> None:
    service = build_gateway_service(fake_apim, gateway_repository)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    async def explode(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("cosmos is throttling")

    gateway_repository.replace_observed = explode  # type: ignore[method-assign]
    await service.sync_now(ACTOR, gateway.id)

    assert service._active == set()
    retry = await service.start_sync(ACTOR, gateway.id)
    assert retry.id


async def test_concurrent_syncs_are_admitted_once(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))

    results = await asyncio.gather(
        service.start_sync(ACTOR, gateway.id),
        service.start_sync(ACTOR, gateway.id),
        return_exceptions=True,
    )
    await _drain(service)

    conflicts = [item for item in results if isinstance(item, ConflictError)]
    accepted = [item for item in results if not isinstance(item, BaseException)]
    assert len(accepted) == 1
    assert len(conflicts) == 1
    assert len(await service.list_sync_runs(ACTOR, gateway.id)) == 1


async def test_shutdown_drains_background_work(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    await service.start_sync(ACTOR, gateway.id)

    await service.aclose()

    assert service._tasks == set()
    assert service._active == set()


async def test_backend_url_credentials_are_not_stored(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    await service.sync_now(ACTOR, gateway.id)

    backends = await service.list_backends(ACTOR, gateway.id)
    serialized = json.dumps([backend.model_dump(mode="json") for backend in backends])

    assert "SasTokenSecret" not in serialized
    assert backends[0].url is not None
    assert backends[0].url.startswith("https://contoso.openai.azure.com/openai")


async def test_routing_policy_credentials_are_not_exposed(fake_apim: FakeApim) -> None:
    service = build_gateway_service(fake_apim)
    gateway = await service.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
    await service.sync_now(ACTOR, gateway.id)

    view = await service.policy_view(ACTOR, gateway.id)
    serialized = json.dumps(view.model_dump(mode="json"))

    assert "FunctionKeySecret" not in serialized
    assert "contoso-fn.azurewebsites.net" in serialized


def test_sanitize_url_drops_only_the_credential_bearing_parts() -> None:
    assert sanitize_url(None) is None
    assert sanitize_url("https://contoso.example.com/api") == "https://contoso.example.com/api"
    sanitized = sanitize_url("https://contoso.example.com/api?code=secret#frag")
    assert sanitized is not None
    assert "secret" not in sanitized
    assert sanitized.startswith("https://contoso.example.com/api")
    assert "parameters hidden" in sanitized


def test_backend_routing_facet_hides_query_credentials() -> None:
    analysis = analyze_policy(
        '<policies><inbound><set-backend-service '
        'base-url="https://fn.azurewebsites.net/api?code=TopSecret" /></inbound></policies>'
    )
    serialized = json.dumps([facet.model_dump(mode="json") for facet in analysis.facets])

    assert "TopSecret" not in serialized
    assert "fn.azurewebsites.net" in serialized
