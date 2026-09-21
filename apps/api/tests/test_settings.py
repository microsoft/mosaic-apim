from typing import Any

import pytest
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from pydantic import ValidationError

CONTROL_PLANE_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
MODEL_RUNTIME_CLIENT_ID = "abcdefff-2222-3333-4444-555555555555"


def local_settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        environment=Environment.LOCAL,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id="tenant",
        **overrides,
    )


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


@pytest.mark.parametrize("audience", [None, "", "   "])
def test_model_runtime_audience_is_optional(audience: str | None) -> None:
    settings = local_settings(model_runtime_client_id=audience)
    assert settings.model_runtime_client_id is None


def test_legacy_azure_settings_do_not_require_runtime_audience(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOSAIC_MODEL_RUNTIME_CLIENT_ID", raising=False)
    settings = Settings(
        _env_file=None,
        environment=Environment.AZURE,
        auth_mode=AuthMode.ENTRA,
        repository_backend=RepositoryBackend.COSMOS,
        tenant_id="tenant",
        api_client_id=CONTROL_PLANE_CLIENT_ID,
        cosmos_endpoint="https://cosmos.example.com",
    )
    assert settings.model_runtime_client_id is None
    assert settings.api_client_id == CONTROL_PLANE_CLIENT_ID


@pytest.mark.parametrize(
    "audience",
    [
        MODEL_RUNTIME_CLIENT_ID,
        MODEL_RUNTIME_CLIENT_ID.upper(),
        f"  {MODEL_RUNTIME_CLIENT_ID}  ",
    ],
)
def test_model_runtime_audience_is_a_canonical_guid(audience: str) -> None:
    settings = local_settings(
        api_client_id=CONTROL_PLANE_CLIENT_ID,
        model_runtime_client_id=audience,
    )
    assert settings.model_runtime_client_id == MODEL_RUNTIME_CLIENT_ID
    assert settings.api_client_id == CONTROL_PLANE_CLIENT_ID


@pytest.mark.parametrize(
    "audience",
    [
        "not-a-client-id",
        f"api://{MODEL_RUNTIME_CLIENT_ID}",
        f"api://{MODEL_RUNTIME_CLIENT_ID}/Models.Invoke",
        f"{MODEL_RUNTIME_CLIENT_ID},another-audience",
        "abcdefff-2222-3333-4444-55555555555z",
    ],
)
def test_model_runtime_audience_rejects_non_guids(audience: str) -> None:
    with pytest.raises(
        ValidationError, match="MOSAIC_MODEL_RUNTIME_CLIENT_ID must be a valid GUID"
    ):
        local_settings(model_runtime_client_id=audience)


@pytest.mark.parametrize(
    "control_plane_audience",
    [
        MODEL_RUNTIME_CLIENT_ID,
        MODEL_RUNTIME_CLIENT_ID.upper(),
        f"{{{MODEL_RUNTIME_CLIENT_ID}}}",
        MODEL_RUNTIME_CLIENT_ID.replace("-", ""),
    ],
)
def test_runtime_and_control_plane_audiences_must_differ(control_plane_audience: str) -> None:
    with pytest.raises(ValidationError, match="must be different from MOSAIC_API_CLIENT_ID"):
        local_settings(
            api_client_id=control_plane_audience,
            model_runtime_client_id=MODEL_RUNTIME_CLIENT_ID,
        )


def test_model_runtime_audience_environment_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOSAIC_MODEL_RUNTIME_CLIENT_ID", MODEL_RUNTIME_CLIENT_ID.upper())
    monkeypatch.setenv("MOSAIC_API_CLIENT_ID", CONTROL_PLANE_CLIENT_ID)
    settings = local_settings()
    assert settings.model_runtime_client_id == MODEL_RUNTIME_CLIENT_ID
    assert settings.api_client_id == CONTROL_PLANE_CLIENT_ID


def test_blank_runtime_audience_environment_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOSAIC_MODEL_RUNTIME_CLIENT_ID", "")
    assert local_settings().model_runtime_client_id is None


def test_runtime_audience_does_not_replace_required_control_plane_audience() -> None:
    with pytest.raises(ValidationError, match="MOSAIC_API_CLIENT_ID is required"):
        Settings(
            _env_file=None,
            environment=Environment.TEST,
            auth_mode=AuthMode.ENTRA,
            repository_backend=RepositoryBackend.MEMORY,
            tenant_id="tenant",
            api_client_id=None,
            model_runtime_client_id=MODEL_RUNTIME_CLIENT_ID,
        )
