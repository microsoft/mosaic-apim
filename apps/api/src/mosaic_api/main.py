from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from azure.identity.aio import DefaultAzureCredential, ManagedIdentityCredential
from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from mosaic_api.api import router
from mosaic_api.auth import EntraAuthenticator, LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings, get_settings
from mosaic_api.errors import DomainError, domain_error_handler
from mosaic_api.observability import configure_logging, configure_telemetry
from mosaic_api.repositories import (
    CosmosDirectoryRepository,
    DirectoryRepository,
    InMemoryDirectoryRepository,
)
from mosaic_api.services import DirectoryService

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
        credential = None
        repository: DirectoryRepository
        if app_settings.repository_backend is RepositoryBackend.MEMORY:
            repository = InMemoryDirectoryRepository()
        else:
            credential = _credential(app_settings)
            repository = CosmosDirectoryRepository(
                endpoint=str(app_settings.cosmos_endpoint),
                credential=credential,
                database_name=app_settings.cosmos_database,
                desired_state_container=app_settings.cosmos_desired_state_container,
                audit_events_container=app_settings.cosmos_audit_events_container,
            )
        authenticator = (
            LocalAuthenticator(app_settings.tenant_id)
            if app_settings.auth_mode is AuthMode.LOCAL
            else EntraAuthenticator(app_settings)
        )
        app.state.repository = repository
        app.state.directory_service = DirectoryService(repository)
        app.state.authenticator = authenticator
        logger.info("application_started", environment=app_settings.environment)
        try:
            yield
        finally:
            await authenticator.close()
            await repository.close()
            if credential:
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
        is_ready = repository is not None and await repository.ready()
        return JSONResponse(
            status_code=status.HTTP_200_OK if is_ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "ready" if is_ready else "notReady"},
        )

    app.include_router(router)
    return app
