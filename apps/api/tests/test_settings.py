from typing import Any

import pytest
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from pydantic import ValidationError

CONTROL_PLANE_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
MODEL_RUNTIME_CLIENT_ID = "abcdefff-2222-3333-4444-555555555555"
MODEL_CLIENT_ID = "fedcba98-7654-3210-fedc-ba9876543210"


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


@pytest.mark.parametrize("client_id", [None, "", "   "])
def test_model_client_id_is_optional(client_id: str | None) -> None:
    assert local_settings(model_client_id=client_id).model_client_id is None


@pytest.mark.parametrize(
    "client_id",
    [
        MODEL_CLIENT_ID,
        MODEL_CLIENT_ID.upper(),
        f"  {MODEL_CLIENT_ID}  ",
        f"{{{MODEL_CLIENT_ID}}}",
    ],
)
def test_model_client_id_is_a_canonical_guid(client_id: str) -> None:
    settings = local_settings(
        api_client_id=CONTROL_PLANE_CLIENT_ID,
        model_runtime_client_id=MODEL_RUNTIME_CLIENT_ID,
        model_client_id=client_id,
    )
    assert settings.model_client_id == MODEL_CLIENT_ID
    assert settings.model_runtime_client_id == MODEL_RUNTIME_CLIENT_ID


@pytest.mark.parametrize(
    "client_id",
    [
        "not-a-client-id",
        f"api://{MODEL_CLIENT_ID}",
        f"{MODEL_CLIENT_ID},another-client",
    ],
)
def test_model_client_id_rejects_non_guids(client_id: str) -> None:
    with pytest.raises(ValidationError, match="MOSAIC_MODEL_CLIENT_ID must be a valid GUID"):
        local_settings(model_client_id=client_id)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"api_client_id": MODEL_CLIENT_ID.upper()},
            "MOSAIC_MODEL_CLIENT_ID must be different from MOSAIC_API_CLIENT_ID",
        ),
        (
            {"api_client_id": MODEL_CLIENT_ID.replace("-", "")},
            "MOSAIC_MODEL_CLIENT_ID must be different from MOSAIC_API_CLIENT_ID",
        ),
        (
            {
                "api_client_id": CONTROL_PLANE_CLIENT_ID,
                "model_runtime_client_id": MODEL_CLIENT_ID.upper(),
            },
            "MOSAIC_MODEL_CLIENT_ID must be different from MOSAIC_MODEL_RUNTIME_CLIENT_ID",
        ),
    ],
)
def test_model_client_id_must_differ_from_api_and_runtime_ids(
    overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        local_settings(model_client_id=MODEL_CLIENT_ID, **overrides)


def test_model_client_id_environment_wiring(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOSAIC_MODEL_CLIENT_ID", MODEL_CLIENT_ID.upper())
    assert local_settings().model_client_id == MODEL_CLIENT_ID
    monkeypatch.setenv("MOSAIC_MODEL_CLIENT_ID", "")
    assert local_settings().model_client_id is None


def test_directory_settings_default_to_graph_lookup_and_group_claims() -> None:
    settings = local_settings()
    assert settings.entra_directory_lookup is True
    assert settings.entra_group_claims is True
    assert str(settings.graph_endpoint).rstrip("/") == "https://graph.microsoft.com"


def test_directory_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOSAIC_ENTRA_DIRECTORY_LOOKUP", "false")
    monkeypatch.setenv("MOSAIC_ENTRA_GROUP_CLAIMS", "false")
    monkeypatch.setenv("MOSAIC_GRAPH_ENDPOINT", "https://graph.microsoft.us")
    settings = local_settings()
    assert settings.entra_directory_lookup is False
    assert settings.entra_group_claims is False
    assert str(settings.graph_endpoint).rstrip("/") == "https://graph.microsoft.us"


def test_azure_requires_https_graph_endpoint() -> None:
    with pytest.raises(ValidationError, match="MOSAIC_GRAPH_ENDPOINT must use https"):
        Settings(
            _env_file=None,
            environment=Environment.AZURE,
            auth_mode=AuthMode.ENTRA,
            repository_backend=RepositoryBackend.COSMOS,
            tenant_id="tenant",
            api_client_id=CONTROL_PLANE_CLIENT_ID,
            cosmos_endpoint="https://cosmos.example.com",
            graph_endpoint="http://graph.example.com",
        )
