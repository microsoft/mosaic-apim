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
