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

from mosaic_api.api import router
from mosaic_api.auth import EntraAuthenticator, LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings, get_settings
from mosaic_api.errors import DomainError, domain_error_handler
from mosaic_api.integrations.aoai import CognitiveServicesClient
from mosaic_api.integrations.aoai.client import SubscriptionScanner
from mosaic_api.integrations.apim import ApimClient, ArmClient
from mosaic_api.integrations.mcp import EntraTokenProvider, KeyVaultSecretReader
from mosaic_api.observability import configure_logging, configure_telemetry
from mosaic_api.repositories import (
    CosmosDirectoryRepository,
    CosmosEntitlementRepository,
    CosmosGatewayRepository,
    CosmosMcpEndpointRepository,
    CosmosModelEndpointRepository,
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
    McpEndpointRepository,
    ModelEndpointRepository,
)
from mosaic_api.services import (
    DirectoryService,
    EntitlementService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
)
from mosaic_api.services.mcp_endpoints import build_mcp_client_factory

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
        mcp_repository: McpEndpointRepository
        if app_settings.repository_backend is RepositoryBackend.MEMORY:
            repository = InMemoryDirectoryRepository()
            gateway_repository = InMemoryGatewayRepository()
            endpoint_repository = InMemoryModelEndpointRepository()
            entitlement_repository = InMemoryEntitlementRepository()
            mcp_repository = InMemoryMcpEndpointRepository()
        else:
            cosmos_client = CosmosClient(
                str(app_settings.cosmos_endpoint), credential=credential
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
        authenticator = (
            LocalAuthenticator(app_settings.tenant_id, app_settings.local_roles)
            if app_settings.auth_mode is AuthMode.LOCAL
            else EntraAuthenticator(app_settings)
        )
        arm_client = ArmClient(credential)
        gateway_service = GatewayService(
            gateway_repository,
            client_factory=lambda resource: ApimClient(arm_client, resource),
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
            bootstrap_resource_id=app_settings.apim_bootstrap_resource_id,
        )
        model_endpoint_service = ModelEndpointService(
            endpoint_repository,
            gateway_repository=gateway_repository,
            client_factory=lambda resource: CognitiveServicesClient(arm_client, resource),
            scanner=SubscriptionScanner(arm_client),
            principal_id=app_settings.managed_identity_principal_id,
            identity_resolver=arm_client.caller_object_id,
        )
        # A dedicated client for outbound MCP calls: redirects are refused per request, and the
        # connection pool for operator-supplied hosts is kept away from the ARM one.
        mcp_http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(app_settings.mcp_discovery_timeout_seconds, connect=10.0),
            follow_redirects=False,
        )
        key_vault_reader = KeyVaultSecretReader(credential)
        mcp_endpoint_service = McpEndpointService(
            mcp_repository,
            client_factory=build_mcp_client_factory(mcp_http_client),
            secret_resolver=key_vault_reader.read,
            token_resolver=EntraTokenProvider(credential).token_for,
            require_https=app_settings.environment is Environment.AZURE,
            allow_private_endpoints=app_settings.mcp_allow_private_endpoints,
        )
        app.state.repository = repository
        app.state.gateway_repository = gateway_repository
        app.state.model_endpoint_repository = endpoint_repository
        app.state.entitlement_repository = entitlement_repository
        app.state.mcp_endpoint_repository = mcp_repository
        app.state.directory_service = DirectoryService(repository)
        app.state.gateway_service = gateway_service
        app.state.model_endpoint_service = model_endpoint_service
        app.state.mcp_endpoint_service = mcp_endpoint_service
        app.state.entitlement_service = EntitlementService(
            entitlement_repository,
            directory_repository=repository,
            gateway_repository=gateway_repository,
            endpoint_repository=endpoint_repository,
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
            reaped = await mcp_endpoint_service.reap_stale_sync_runs(app_settings.tenant_id)
            if reaped:
                logger.warning("mcp_endpoint_sync_runs_reaped", count=reaped)
        except Exception:
            logger.exception("mcp_endpoint_sync_reap_failed")
        gateway_service.schedule_bootstrap(app_settings.tenant_id)
        logger.info("application_started", environment=app_settings.environment)
        try:
            yield
        finally:
            await gateway_service.aclose()
            await model_endpoint_service.aclose()
            await mcp_endpoint_service.aclose()
            await authenticator.close()
            await arm_client.close()
            await key_vault_reader.close()
            await mcp_http_client.aclose()
            await repository.close()
            await gateway_repository.close()
            await endpoint_repository.close()
            await entitlement_repository.close()
            await mcp_repository.close()
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
        )

    @app.middleware("http")
    async def correlation_id(request: Request, call_next: Any) -> Any:
        correlation = request.headers.get("X-Correlation-ID")
        response = await call_next(request)
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
        )
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if is_ready else "notReady"},
        )

    app.include_router(router)
    return app
