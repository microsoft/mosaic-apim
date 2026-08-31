from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import AnyHttpUrl, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    AZURE = "azure"


class AuthMode(StrEnum):
    ENTRA = "entra"
    LOCAL = "local"


class RepositoryBackend(StrEnum):
    COSMOS = "cosmos"
    MEMORY = "memory"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MOSAIC_",
        env_file=".env",
        extra="ignore",
        case_sensitive=False,
    )

    environment: Environment = Environment.AZURE
    auth_mode: AuthMode = AuthMode.ENTRA
    repository_backend: RepositoryBackend = RepositoryBackend.COSMOS
    tenant_id: str
    api_client_id: str | None = None
    entra_issuer: AnyHttpUrl | None = None
    entra_discovery_url: AnyHttpUrl | None = None
    required_role: str = "Admin"
    cosmos_endpoint: AnyHttpUrl | None = None
    cosmos_database: str = "mosaic"
    cosmos_desired_state_container: str = "desired-state"
    cosmos_sync_operations_container: str = "sync-operations"
    cosmos_audit_events_container: str = "audit-events"
    cors_origins: list[str] = Field(default_factory=list)
    applicationinsights_connection_string: str | None = None
    apim_subscription_id: str | None = None
    apim_resource_group: str | None = None
    apim_service_name: str | None = None
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_fail_closed_modes(self) -> "Settings":
        if self.environment is Environment.AZURE:
            if self.auth_mode is not AuthMode.ENTRA:
                raise ValueError("Azure deployments must use Entra authentication")
            if self.repository_backend is not RepositoryBackend.COSMOS:
                raise ValueError("Azure deployments must use Cosmos persistence")
        if self.auth_mode is AuthMode.LOCAL and self.environment not in {
            Environment.LOCAL,
            Environment.TEST,
        }:
            raise ValueError("Local authentication is only valid in local or test environments")
        if self.repository_backend is RepositoryBackend.MEMORY and self.environment not in {
            Environment.LOCAL,
            Environment.TEST,
        }:
            raise ValueError("In-memory persistence is only valid in local or test environments")
        if self.auth_mode is AuthMode.ENTRA and not self.api_client_id:
            raise ValueError("MOSAIC_API_CLIENT_ID is required for Entra authentication")
        if self.repository_backend is RepositoryBackend.COSMOS and not self.cosmos_endpoint:
            raise ValueError("MOSAIC_COSMOS_ENDPOINT is required for Cosmos persistence")
        return self

    @property
    def issuer(self) -> str:
        return str(self.entra_issuer or f"https://login.microsoftonline.com/{self.tenant_id}/v2.0")

    @property
    def discovery_url(self) -> str:
        return str(
            self.entra_discovery_url
            or f"https://login.microsoftonline.com/{self.tenant_id}/v2.0/"
            ".well-known/openid-configuration"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


TenantId = Annotated[str, Field(min_length=1, max_length=128)]
