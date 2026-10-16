import asyncio
from contextlib import suppress
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from mosaic_api import main
from mosaic_api.config import Settings
from mosaic_api.domain import ApimResourceId, Gateway
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.publishing import PublishingService


@pytest.mark.asyncio
async def test_automatic_keys_resume_after_publish_recovery_and_stop_on_shutdown(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()
    recovered = AsyncMock(return_value=0)
    monkeypatch.setattr(PublishingService, "reap_stale_publish_runs", recovered)

    async def resume(_service: PortalAccessService, tenant_id: str) -> None:
        assert tenant_id == settings.tenant_id
        recovered.assert_awaited_once_with(settings.tenant_id)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(PortalAccessService, "resume_automatic_keys", resume)
    app = main.create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), timeout=5)
        assert not stopped.is_set()
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_automatic_key_recovery_logs_failure_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resumed = asyncio.Event()
    attempts = 0

    async def resume(tenant_id: str) -> None:
        nonlocal attempts
        assert tenant_id == "tenant"
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary repository failure")
        resumed.set()
        await asyncio.Event().wait()

    app = FastAPI()
    app.state.portal_access_service = Mock(resume_automatic_keys=resume)
    logger = Mock()
    monkeypatch.setattr(main, "logger", logger)
    task = asyncio.create_task(main._resume_automatic_keys(app, "tenant", interval_seconds=0.001))
    try:
        await asyncio.wait_for(resumed.wait(), timeout=5)
        assert attempts == 2
        logger.exception.assert_called_once_with("automatic_key_recovery_failed")
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_budget_enforcement_clients_use_the_government_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected_arm = Mock()
    routed = Mock(return_value=selected_arm)
    reader = Mock()
    writer = Mock()
    monkeypatch.setattr(main.AzureClientRegistry, "arm_for", routed)
    monkeypatch.setattr(main, "ApimClient", reader)
    monkeypatch.setattr(main, "ApimWriter", writer)
    resource = ApimResourceId.parse(
        "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg"
        "/providers/Microsoft.ApiManagement/service/gov",
        azure_cloud="government",
        azure_connection_id="government-connection",
    )
    app = main.create_app(settings)
    async with app.router.lifespan_context(app):
        app.state.budget_service._client_factory(resource)
        app.state.budget_service._writer_factory(resource)
        reader.assert_called_once_with(selected_arm, resource)
        writer.assert_called_once_with(selected_arm, resource)
        assert routed.call_count == 2
        routed.assert_called_with(resource)


@pytest.mark.asyncio
@pytest.mark.parametrize("principal_id", ["government-principal-object-id", None])
async def test_monitoring_identity_resolvers_use_the_gateway_connection(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, principal_id: str | None
) -> None:
    selected_arm = Mock(caller_object_id=AsyncMock(return_value=principal_id))
    routed = Mock(return_value=selected_arm)
    telemetry = Mock()
    pool_health = Mock()
    monkeypatch.setattr(main.AzureClientRegistry, "arm_for", routed)
    monkeypatch.setattr(main, "TelemetryService", telemetry)
    monkeypatch.setattr(main, "PoolHealthService", pool_health)
    gateway = Gateway(
        id="gateway-government",
        tenant_id=settings.tenant_id,
        name="Government gateway",
        azure_resource_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg"
            "/providers/Microsoft.ApiManagement/service/gov"
        ),
        subscription_id="00000000-0000-0000-0000-000000000001",
        resource_group="rg",
        service_name="gov",
        azure_cloud="government",
        azure_connection_id="government-connection",
    )
    app = main.create_app(settings)
    async with app.router.lifespan_context(app):
        for service in (telemetry, pool_health):
            resolver = service.call_args.kwargs["gateway_identity_resolver"]
            assert await resolver(gateway) == principal_id
        assert routed.call_count == 2
        routed.assert_called_with(gateway)
        assert selected_arm.caller_object_id.await_count == 2
