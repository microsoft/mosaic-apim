import pytest

from m_agent.gateway import ModelSettings
from m_agent.server import Settings

VALID = {
    "MOSAIC_MODEL_ENDPOINT": "https://gateway.example.test/models/chat/",
    "MOSAIC_MODEL_DEPLOYMENT": "gpt-test",
    "MOSAIC_MODEL_API_VERSION": "2024-10-21",
    "MOSAIC_RUNTIME_SCOPE": "api://11111111-1111-1111-1111-111111111111/.default",
}


def test_the_model_settings_come_from_the_environment() -> None:
    settings = ModelSettings.from_env(
        VALID
        | {
            "MOSAIC_COST_CENTER": "research",
            "MOSAIC_MODEL_TOKEN_PARAMETER": "max_completion_tokens",
            "MOSAIC_MODEL_MAX_TOKENS": "12",
        }
    )
    assert settings == ModelSettings(
        endpoint="https://gateway.example.test/models/chat",
        deployment="gpt-test",
        api_version="2024-10-21",
        runtime_scope="api://11111111-1111-1111-1111-111111111111/.default",
        cost_center="research",
        token_parameter="max_completion_tokens",
        max_tokens=12,
    )
    assert settings.chat_url == (
        "https://gateway.example.test/models/chat/openai/deployments/gpt-test/chat/completions"
    )


def test_defaults_keep_each_call_cheap_and_name_no_cost_center() -> None:
    settings = ModelSettings.from_env(VALID)
    assert (settings.cost_center, settings.token_parameter, settings.max_tokens) == (
        None,
        "max_tokens",
        16,
    )


def test_server_settings_default_to_stateful_streamable_http_on_port_8000() -> None:
    settings = Settings.from_env(VALID)
    assert (settings.stateless, settings.json_response, settings.port) == (False, False, 8000)


@pytest.mark.parametrize(
    "change",
    [
        {"MOSAIC_MODEL_ENDPOINT": ""},
        {"MOSAIC_MODEL_ENDPOINT": "http://gateway.example.test/models/chat"},
        {"MOSAIC_MODEL_ENDPOINT": "https://user:secret@gateway.example.test/models/chat"},
        {"MOSAIC_MODEL_ENDPOINT": "https://gateway.example.test/models/chat?api-version=1"},
        {"MOSAIC_MODEL_DEPLOYMENT": "gpt test"},
        {"MOSAIC_MODEL_DEPLOYMENT": "../other"},
        {"MOSAIC_MODEL_API_VERSION": "2024-10-21&x=1"},
        {"MOSAIC_RUNTIME_SCOPE": "api://11111111-1111-1111-1111-111111111111/Models.Invoke"},
        {"MOSAIC_RUNTIME_SCOPE": "https://management.azure.com/.default"},
        {"MOSAIC_COST_CENTER": "research and development"},
        {"MOSAIC_MODEL_TOKEN_PARAMETER": "max_output_tokens"},
        {"MOSAIC_MODEL_MAX_TOKENS": "0"},
        {"MOSAIC_MODEL_MAX_TOKENS": "257"},
        {"MOSAIC_MODEL_MAX_TOKENS": "lots"},
    ],
)
def test_invalid_model_settings_stop_the_server_from_starting(change: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        ModelSettings.from_env(VALID | change)


@pytest.mark.parametrize("change", [{"PORT": "0"}, {"MCP_STATELESS": "sometimes"}])
def test_invalid_server_settings_stop_the_server_from_starting(change: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(VALID | change)
