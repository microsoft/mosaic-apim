import pytest

from m_tools.server import Settings


def test_defaults_serve_stateful_streamable_http_on_port_8000() -> None:
    assert Settings.from_env({}) == Settings("streamable-http", False, False, 8000)


def test_the_sse_only_variant_and_the_flags_come_from_the_environment() -> None:
    settings = Settings.from_env(
        {
            "MCP_TRANSPORT": " SSE ",
            "MCP_STATELESS": "true",
            "MCP_JSON_RESPONSE": "1",
            "PORT": "9000",
        }
    )
    assert settings == Settings("sse", True, True, 9000)


def test_m_protected_is_the_same_server_under_its_own_name() -> None:
    assert Settings.from_env({"MCP_SERVER_NAME": " M-protected "}) == Settings(
        server_name="M-protected"
    )
    assert Settings.from_env({"MCP_SERVER_NAME": ""}).server_name == "M-tools"
    assert Settings.from_env({"MCP_SERVER_NAME": "a" * 64}).server_name == "a" * 64


@pytest.mark.parametrize(
    "env",
    [
        {"MCP_TRANSPORT": "stdio"},
        {"MCP_TRANSPORT": "http"},
        {"MCP_STATELESS": "maybe"},
        {"MCP_JSON_RESPONSE": "2"},
        {"PORT": "0"},
        {"PORT": "65536"},
        {"PORT": "eighty"},
        {"MCP_SERVER_NAME": "-protected"},
        {"MCP_SERVER_NAME": "a" * 65},
        {"MCP_SERVER_NAME": "M\nprotected"},
        {"MCP_SERVER_NAME": "<M-protected>"},
    ],
)
def test_invalid_settings_stop_the_server_from_starting(env: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        Settings.from_env(env)
