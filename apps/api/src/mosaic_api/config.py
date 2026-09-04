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
    portal_role: str = "User"
    local_roles: list[str] = Field(default_factory=lambda: ["Admin", "User"])
    cosmos_endpoint: AnyHttpUrl | None = None
    cosmos_database: str = "mosaic"
    cosmos_desired_state_container: str = "desired-state"
    cosmos_sync_operations_container: str = "sync-operations"
    cosmos_audit_events_container: str = "audit-events"
    cosmos_observed_state_container: str = "observed-state"
    cors_origins: list[str] = Field(default_factory=list)
    applicationinsights_connection_string: str | None = None
    managed_identity_principal_id: str | None = None
    apim_subscription_id: str | None = None
    apim_resource_group: str | None = None
    apim_service_name: str | None = None
    mcp_discovery_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    mcp_allow_private_endpoints: bool = Field(
        default=False,
        description=(
            "Permit registering an MCP server on a loopback, link-local, or private address. Off "
            "by default: MOSAIC has no private network path to one, and refusing keeps a "
            "managed-identity token away from the instance metadata service. Intended for local "
            "development only."
        ),
    )
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_fail_closed_modes(self) -> "Settings":
        if self.environment is Environment.AZURE:
            if self.auth_mode is not AuthMode.ENTRA:
                raise ValueError("Azure deployments must use Entra authentication")
            if self.repository_backend is not RepositoryBackend.COSMOS:
                raise ValueError("Azure deployments must use Cosmos persistence")
            if self.mcp_allow_private_endpoints:
                raise ValueError(
                    "Private MCP endpoints are a local development affordance and must not be "
                    "enabled in Azure"
                )
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
        if not self.required_role.strip() or not self.portal_role.strip():
            raise ValueError("Administrator and portal app role names cannot be empty")
        if self.required_role.strip() == self.portal_role.strip():
            # Collapsing the two names would make require_admin pass for every portal user, so
            # an entire end-user population would silently hold the administrator surface.
            raise ValueError(
                "The administrator and portal app roles must be different; "
                f"both are set to {self.required_role.strip()!r}"
            )
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

    @property
    def apim_bootstrap_resource_id(self) -> str | None:
        """The APIM deployed alongside MOSAIC, offered as an onboarding suggestion.

        These settings are a hint for the onboarding experience, not runtime configuration. Gateways
        MOSAIC manages are registered records in Cosmos, not environment variables.
        """

        if not (self.apim_subscription_id and self.apim_resource_group and self.apim_service_name):
            return None
        return (
            f"/subscriptions/{self.apim_subscription_id}"
            f"/resourceGroups/{self.apim_resource_group}"
            f"/providers/Microsoft.ApiManagement/service/{self.apim_service_name}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


TenantId = Annotated[str, Field(min_length=1, max_length=128)]
