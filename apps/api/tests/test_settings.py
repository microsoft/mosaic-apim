import pytest
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from pydantic import ValidationError


def test_azure_rejects_local_auth_and_memory_repository() -> None:
    with pytest.raises(ValidationError, match="Azure deployments must use Entra"):
        Settings(
            environment=Environment.AZURE,
            auth_mode=AuthMode.LOCAL,
            repository_backend=RepositoryBackend.MEMORY,
            tenant_id="tenant",
        )


def test_azure_rejects_private_mcp_endpoints() -> None:
    # Allowing them would let a managed-identity token reach the instance metadata service.
    with pytest.raises(ValidationError, match="Private MCP endpoints"):
        Settings(
            environment=Environment.AZURE,
            auth_mode=AuthMode.ENTRA,
            repository_backend=RepositoryBackend.COSMOS,
            tenant_id="tenant",
            api_client_id="client",
            cosmos_endpoint="https://cosmos.example.com",
            mcp_allow_private_endpoints=True,
        )


def test_private_mcp_endpoints_are_allowed_locally() -> None:
    settings = Settings(
        environment=Environment.LOCAL,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant",
        mcp_allow_private_endpoints=True,
    )
    assert settings.mcp_allow_private_endpoints is True
