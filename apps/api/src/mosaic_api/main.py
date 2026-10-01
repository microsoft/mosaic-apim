from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import structlog
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mosaic_api.analytics_api import analytics_router
from mosaic_api.api import portal_router, router
from mosaic_api.auth import EntraAuthenticator, LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings, get_settings
from mosaic_api.directory_api import directory_router
from mosaic_api.errors import DomainError, domain_error_handler
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.backend_key_access import KeyVaultLocator
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.aoai.key_check import EndpointKeyProbe
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.credentials import ApimCredentialClient
from mosaic_api.integrations.graph import DirectoryLookup, GraphDirectoryLookup
from mosaic_api.integrations.loganalytics import LogAnalyticsClient
from mosaic_api.integrations.mcp import EntraTokenProvider, KeyVaultSecretReader
from mosaic_api.mcp_publishing_api import mcp_publishing_router
from mosaic_api.observability import configure_logging, configure_telemetry
from mosaic_api.repositories import (
    CosmosDirectoryRepository,
    CosmosEntitlementRepository,
    CosmosEnvironmentRepository,
    CosmosGatewayRepository,
    CosmosMcpEndpointRepository,
    CosmosModelEndpointRepository,
    CosmosUsageRollupRepository,
    DirectoryRepository,
    EntitlementRepository,
    EnvironmentRepository,
    GatewayRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryEnvironmentRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
    InMemoryUsageRollupRepository,
    McpEndpointRepository,
    ModelEndpointRepository,
    UsageRollupRepository,
)
from mosaic_api.services import (
    DirectoryService,
    EntitlementService,
    EnvironmentFindingsService,
    EnvironmentService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
    PortalService,
    PublishingService,
    UsageService,
)
from mosaic_api.services.analytics import AnalyticsService
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory
from mosaic_api.services.mcp_publishing import McpPublishingService
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.telemetry import TelemetryService
from mosaic_api.services.usage import RollupUsageSource, SimulatedUsageSource, UsageSource
from mosaic_api.services.usage_rollup import UsageRollupService

logger = structlog.get_logger()


def _credential(settings: Settings) -> DefaultAzureCredential | ManagedIdentityCredential:
    if settings.environment in {Environment.LOCAL, Environment.TEST}:
        return DefaultAzureCredential()
    return ManagedIdentityCredential()


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or get_settings()
    configure_logging(app_settings)
    configure_telemetry(app_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        credential = _credential(app_settings)
        cosmos_client: CosmosClient | None = None
        repository: DirectoryRepository
        gateway_repository: GatewayRepository
        endpoint_repository: ModelEndpointRepository
        entitlement_repository: EntitlementRepository
        environment_repository: EnvironmentRepository
        mcp_repository: McpEndpointRepository
        rollup_repository: UsageRollupRepository
        if app_settings.repository_backend is RepositoryBackend.MEMORY:
            repository = InMemoryDirectoryRepository()
            gateway_repository = InMemoryGatewayRepository()
            endpoint_repository = InMemoryModelEndpointRepository()
            entitlement_repository = InMemoryEntitlementRepository()
            mcp_repository = InMemoryMcpEndpointRepository()
            environment_repository = InMemoryEnvironmentRepository(
                gateway_repository, endpoint_repository, mcp_repository
            )
            rollup_repository = InMemoryUsageRollupRepository()
        else:
            cosmos_client = CosmosClient(
                str(app_settings.cosmos_endpoint), credential=credential
            )
            rollup_repository = CosmosUsageRollupRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_usage_rollups_container,
                owns_client=False,
            )
            repository = CosmosDirectoryRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            gateway_repository = CosmosGatewayRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_sync_operations_container,
                app_settings.cosmos_observed_state_container,
                owns_client=False,
            )
            endpoint_repository = CosmosModelEndpointRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_sync_operations_container,
                app_settings.cosmos_observed_state_container,
                owns_client=False,
            )
            mcp_repository = CosmosMcpEndpointRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                app_settings.cosmos_sync_operations_container,
                app_settings.cosmos_observed_state_container,
                owns_client=False,
            )
            entitlement_repository = CosmosEntitlementRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
            environment_repository = CosmosEnvironmentRepository(
                cosmos_client,
                app_settings.cosmos_database,
                app_settings.cosmos_desired_state_container,
                app_settings.cosmos_audit_events_container,
                owns_client=False,
            )
        authenticator = (
            LocalAuthenticator(app_settings.tenant_id, app_settings.local_roles)
            if app_settings.auth_mode is AuthMode.LOCAL
            else EntraAuthenticator(app_settings)
        )
        directory_lookup: DirectoryLookup | None = (
            GraphDirectoryLookup(credential, endpoint=str(app_settings.graph_endpoint))
            if app_settings.entra_directory_lookup and app_settings.auth_mode is AuthMode.ENTRA
            else None
        )
        arm_client = ArmClient(credential)
        gateway_service = GatewayService(
            gateway_repository,
            client_factory=lambda resource: ApimClient(arm_client, resource),
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            bootstrap_resource_id=app_settings.apim_bootstrap_resource_id,
            environment_repository=environment_repository,
        )
        key_vault_reader = KeyVaultSecretReader(credential)
        # A key-authenticated endpoint's key goes only to its own Azure AI host, over a client
        # that follows no redirects and shares nothing with the ARM pool.
        endpoint_key_probe = EndpointKeyProbe()
        subscription_scanner = SubscriptionScanner(arm_client)
        model_endpoint_service = ModelEndpointService(
            endpoint_repository,
            gateway_repository=gateway_repository,
            client_factory=lambda resource: CognitiveServicesClient(arm_client, resource),
            scanner=subscription_scanner,
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            bootstrap_subscription_id=app_settings.apim_subscription_id,
            environment_repository=environment_repository,
            secret_resolver=key_vault_reader.read,
            key_probe=endpoint_key_probe.check,
            vault_locator=KeyVaultLocator(
                arm_client,
                scanner=subscription_scanner,
                known_vault_ids=(
                    [app_settings.key_vault_resource_id]
                    if app_settings.key_vault_resource_id
                    else []
                ),
            ),
        )
        publishing_service = PublishingService(
            gateway_repository,
            endpoint_repository=endpoint_repository,
            client_factory=lambda resource: ApimClient(arm_client, resource),
            writer_factory=lambda resource: ApimWriter(arm_client, resource),
            directory_repository=repository,
            entitlement_repository=entitlement_repository,
            model_runtime_client_id=app_settings.model_runtime_client_id,
            security_group_claims=app_settings.entra_group_claims,
            environment_repository=environment_repository,
        )
        mcp_publishing_service = McpPublishingService(
            gateway_repository,
            mcp_endpoint_repository=mcp_repository,
            entitlement_repository=entitlement_repository,
            directory_repository=repository,
            client_factory=lambda resource: ApimClient(arm_client, resource),
            writer_factory=lambda resource: ApimWriter(arm_client, resource),
            runtime_client_id=app_settings.model_runtime_client_id,
            security_group_claims=app_settings.entra_group_claims,
            environment_repository=environment_repository,
        )
        # A dedicated client for outbound MCP calls: redirects are refused per request, and the
        # connection pool for operator-supplied hosts is kept away from the ARM one.
        mcp_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(app_settings.mcp_discovery_timeout_seconds, connect=10.0),
            follow_redirects=False,
        )
        mcp_endpoint_service = McpEndpointService(
            mcp_repository,
            gateway_repository=gateway_repository,
            entitlement_repository=entitlement_repository,
            client_factory=build_mcp_client_factory(mcp_http_client),
            secret_resolver=key_vault_reader.read,
            token_resolver=EntraTokenProvider(credential).token_for,
            require_https=app_settings.environment is Environment.AZURE,
            allow_private_endpoints=app_settings.mcp_allow_private_endpoints,
            environment_repository=environment_repository,
        )
        app.state.repository = repository
        app.state.directory_lookup = directory_lookup
        app.state.gateway_repository = gateway_repository
        app.state.model_endpoint_repository = endpoint_repository
        app.state.entitlement_repository = entitlement_repository
        app.state.environment_repository = environment_repository
        app.state.mcp_endpoint_repository = mcp_repository
        app.state.directory_service = DirectoryService(
            repository,
            gateway_repository=gateway_repository,
            entitlement_repository=entitlement_repository,
            directory_lookup=directory_lookup,
            group_claims_enabled=app_settings.entra_group_claims,
        )
        app.state.gateway_service = gateway_service
        app.state.model_endpoint_service = model_endpoint_service
        app.state.publishing_service = publishing_service
        app.state.mcp_publishing_service = mcp_publishing_service
        app.state.mcp_endpoint_service = mcp_endpoint_service
        entitlement_service = EntitlementService(
            entitlement_repository,
            directory_repository=repository,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            directory_lookup=directory_lookup,
        )
        app.state.entitlement_service = entitlement_service
        environment_service = EnvironmentService(
            environment_repository,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            mcp_repository=mcp_repository,
            entitlement_repository=entitlement_repository,
            directory_repository=repository,
        )
        app.state.environment_service = environment_service
        app.state.environment_findings_service = EnvironmentFindingsService(
            environment_service=environment_service,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            mcp_repository=mcp_repository,
        )
        app.state.portal_access_service = PortalAccessService(
            entitlement_service,
            repository=entitlement_repository,
            directory_repository=repository,
            gateway_repository=gateway_repository,
            credential_factory=lambda resource: ApimCredentialClient(arm_client, resource),
            model_runtime_client_id=app_settings.model_runtime_client_id,
            model_client_id=app_settings.model_client_id,
        )
        app.state.portal_service = PortalService(
            entitlement_service,
            directory_repository=repository,
            gateway_repository=gateway_repository,
        )
        uses_rollups = app_settings.uses_usage_rollups
        log_client = (
            LogAnalyticsClient(credential, endpoint=str(app_settings.log_analytics_endpoint))
            if uses_rollups
            else None
        )
        telemetry_service = TelemetryService(
            gateway_repository=gateway_repository,
            rollup_repository=rollup_repository,
            entitlement_repository=entitlement_repository,
            client_factory=lambda resource: ApimClient(arm_client, resource),
            writer_factory=lambda resource: ApimWriter(arm_client, resource),
            logs=log_client,
            rollups_enabled=uses_rollups and app_settings.usage_rollup_enabled,
            interval_seconds=app_settings.usage_rollup_interval_seconds,
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
        )
        rollup_service = (
            UsageRollupService(
                rollup_repository,
                gateway_repository=gateway_repository,
                entitlement_repository=entitlement_repository,
                directory_repository=repository,
                logs=log_client,
                telemetry=telemetry_service,
                tenant_id=app_settings.tenant_id,
                interval_seconds=app_settings.usage_rollup_interval_seconds,
                retention_days=app_settings.usage_rollup_retention_days,
                backfill_max_days=app_settings.usage_rollup_backfill_max_days,
            )
            if log_client is not None and app_settings.usage_rollup_enabled
            else None
        )
        usage_source: UsageSource = (
            RollupUsageSource(
                rollup_repository, interval_seconds=app_settings.usage_rollup_interval_seconds
            )
            if uses_rollups
            else SimulatedUsageSource(environment_repository=environment_repository)
        )
        app.state.usage_rollup_repository = rollup_repository
        app.state.telemetry_service = telemetry_service
        app.state.usage_rollup_service = rollup_service
        app.state.usage_service = UsageService(
            app.state.portal_service,
            source=usage_source,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
            environment_repository=environment_repository,
        )
        app.state.analytics_service = AnalyticsService(
            rollup_repository,
            gateway_repository=gateway_repository,
            entitlement_repository=entitlement_repository,
            directory_repository=repository,
            environment_repository=environment_repository,
            endpoint_repository=endpoint_repository,
            rollups=rollup_service,
            directory_lookup=directory_lookup,
            configured=uses_rollups,
            interval_seconds=app_settings.usage_rollup_interval_seconds,
            retention_days=app_settings.usage_rollup_retention_days,
        )
        app.state.authenticator = authenticator
        try:
            reaped = await gateway_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("gateway_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("gateway_sync_reap_failed")
        try:
            reaped = await model_endpoint_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("endpoint_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("endpoint_sync_reap_failed")
        try:
            reaped = await publishing_service.reap_stale_publish_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("publish_runs_reaped", count=reaped)
        except Exception:
            logger.exception("publish_reap_failed")
        try:
            reaped = await mcp_publishing_service.reap_stale_publish_runs(
                app_settings.tenant_id
            )
            if reaped:
                logger.warning("mcp_publish_runs_reaped", count=reaped)
        except Exception:
            logger.exception("mcp_publish_reap_failed")
        try:
            reaped = await mcp_endpoint_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("mcp_endpoint_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("mcp_endpoint_sync_reap_failed")
        gateway_service.schedule_bootstrap(app_settings.tenant_id)
        if rollup_service is not None:
            rollup_service.start()
        logger.info(
            "application_started",
            environment=app_settings.environment,
            usage_source="logAnalytics" if uses_rollups else "simulated",
        )
        try:
            yield
        finally:
            if rollup_service is not None:
                await rollup_service.aclose()
            await gateway_service.aclose()
            await model_endpoint_service.aclose()
            await publishing_service.aclose()
            await mcp_publishing_service.aclose()
            await mcp_endpoint_service.aclose()
            if directory_lookup:
                await directory_lookup.close()
            await authenticator.close()
            await arm_client.close()
            await key_vault_reader.close()
            await endpoint_key_probe.close()
            await mcp_http_client.aclose()
            if log_client is not None:
                await log_client.close()
            await repository.close()
            await gateway_repository.close()
            await endpoint_repository.close()
            await entitlement_repository.close()
            await environment_repository.close()
            await mcp_repository.close()
            await rollup_repository.close()
            if cosmos_client:
                await cosmos_client.close()
            await credential.close()

    app = FastAPI(
        title="MOSAIC API",
        version="0.1.0",
        description="Desired-state control plane for Azure API Management AI gateway governance.",
        lifespan=lifespan,
    )
    app.state.settings = app_settings
    app.add_exception_handler(DomainError, domain_error_handler)
    if app_settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "X-Correlation-ID"],
            # The console saves each CSV export under the name the API gives it.
            expose_headers=["Content-Disposition"],
        )

    @app.middleware("http")
    async def correlation_id(request: Request, call_next: Any) -> Any:
        correlation = request.headers.get("X-Correlation-ID")
        response = await call_next(request)
        if request.url.path.startswith("/api/v1/"):
            response.headers["Cache-Control"] = "no-store, private"
        if request.url.path.endswith("/keys/reveal"):
            response.headers["Pragma"] = "no-cache"
        if correlation:
            response.headers["X-Correlation-ID"] = correlation
        return response

    @app.get("/healthz", tags=["health"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def ready(request: Request) -> JSONResponse:
        repository = getattr(request.app.state, "repository", None)
        gateway_repository = getattr(request.app.state, "gateway_repository", None)
        endpoint_repository = getattr(request.app.state, "model_endpoint_repository", None)
        entitlement_repository = getattr(request.app.state, "entitlement_repository", None)
        mcp_repository = getattr(request.app.state, "mcp_endpoint_repository", None)
        environment_repository = getattr(request.app.state, "environment_repository", None)
        rollup_repository = getattr(request.app.state, "usage_rollup_repository", None)
        is_ready = (
            repository is not None
            and await repository.ready()
            and gateway_repository is not None
            and await gateway_repository.ready()
            and endpoint_repository is not None
            and await endpoint_repository.ready()
            and entitlement_repository is not None
            and await entitlement_repository.ready()
            and mcp_repository is not None
            and await mcp_repository.ready()
            and environment_repository is not None
            and await environment_repository.ready()
            and rollup_repository is not None
            and await rollup_repository.ready()
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if is_ready else "notReady"},
        )

    app.include_router(router)
    app.include_router(mcp_publishing_router)
    app.include_router(directory_router)
    app.include_router(analytics_router)
    app.include_router(portal_router)
    return app
