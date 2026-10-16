from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from apim_double import FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from fastapi.testclient import TestClient
from mosaic_api.azure_connections import AzureConnectionSecret
from mosaic_api.clouds import AzureResourceContext, cloud_profile
from mosaic_api.domain import McpAuthMode, McpEndpoint
from mosaic_api.errors import NotFoundError, ValidationError
from mosaic_api.integrations.apim.client import ArmClient
from mosaic_api.integrations.azure_connections import AzureClientRegistry
from pydantic import SecretStr
from test_azure_connections import ACTOR, SECRET, build, request

REPO_ROOT = Path(__file__).resolve().parents[3]


def endpoint(
    connection_id: str | None, audience: str = "api://fictional-government-mcp"
) -> McpEndpoint:
    return McpEndpoint(
        id="fictional-government-mcp", tenant_id=ACTOR.tenant_id, name="Government discovery",
        endpoint="https://fictional.azure-api.us/mcp", azure_cloud="government",
        azure_connection_id=connection_id, auth_mode=McpAuthMode.MANAGED_IDENTITY,
        resource_audience=audience,
    )


async def test_selected_mcp_tokens_use_active_secret_and_never_host_credentials() -> None:
    service, _ = build()
    created = await service.create(ACTOR, request())
    issuer = AsyncMock(wraps=service._issue)
    service._issue = issuer
    host_credential = FakeCredential()
    host_arm = ArmClient(cast(AsyncTokenCredential, host_credential))
    registry = AzureClientRegistry(
        service, host_arm=host_arm,
        host_credential=cast(AsyncTokenCredential, host_credential), host_logs=None,
    )
    target = endpoint(created.id)
    assert target.resource_audience is not None
    try:
        assert await registry.token_for_connection(
            target, target.resource_audience
        ) == "fictional-access-token"
        assert issuer.call_args.args[0].azure_cloud == "government"
        assert issuer.call_args.args[1] == SECRET
        assert issuer.call_args.args[2] == ("api://fictional-government-mcp/.default",)
        # Configured discovery audiences do not broaden the generic management credential.
        with pytest.raises(ValidationError, match="audience"):
            await service.token(created.id, ("api://fictional-government-mcp/.default",))
        await service.replace_secret(
            ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("replacement"))
        )
        await registry.token_for_connection(target, target.resource_audience)
        assert issuer.call_args.args[1] == "replacement"
        await service.delete(ACTOR, created.id)
        with pytest.raises(NotFoundError):
            await registry.token_for_connection(target, target.resource_audience)
        with pytest.raises(ValidationError, match="Government"):
            await registry.token_for_connection(endpoint(None), target.resource_audience)
        assert host_credential.token_requests == 0
    finally:
        await registry.close()
        await host_arm.close()


@pytest.mark.parametrize("audience", [
    "https://cognitiveservices.azure.com",
    "HTTPS://COGNITIVESERVICES.AZURE.COM/.default",
    "https://ai.azure.com",
    "https://management.azure.com",
    "https://management.usgovcloudapi.net",
    "https://graph.microsoft.com",
    "https://vault.usgovcloudapi.net",
    "api://configured/.default api://unapproved/.default",
])
async def test_wrong_cloud_and_management_audiences_are_not_discovery_permissions(
    audience: str,
) -> None:
    service, _ = build()
    created = await service.create(ACTOR, request())
    issuer = AsyncMock(wraps=service._issue)
    service._issue = issuer
    reader = AsyncMock(wraps=service._read)
    service._read = reader
    with pytest.raises(ValidationError):
        await service.mcp_token(endpoint(created.id, audience), audience)
    issuer.assert_not_awaited()
    reader.assert_not_awaited()


async def test_mcp_token_context_checks_tenant_cloud_and_configured_audience() -> None:
    service, _ = build()
    created = await service.create(ACTOR, request())
    target = endpoint(created.id)
    issuer = AsyncMock(wraps=service._issue)
    service._issue = issuer
    for candidate, audience in (
        (target.model_copy(update={"tenant_id": "another-tenant"}), target.resource_audience),
        (target.model_copy(update={"azure_cloud": "commercial"}), target.resource_audience),
        (target.model_copy(update={"endpoint": "https://fictional.azure-api.net/mcp"}),
         target.resource_audience),
        (target, "api://another-unconfigured-resource"),
        (endpoint(None), target.resource_audience),
    ):
        assert audience is not None
        with pytest.raises(ValidationError):
            await service.mcp_token(candidate, audience)
    issuer.assert_not_awaited()


def test_main_wires_selected_discovery_without_changing_commercial_defaults(
    client: TestClient,
) -> None:
    state = client.app.state
    endpoints = state.model_endpoint_service
    context = AzureResourceContext("government", "connection-government")
    scanner = endpoints._scanner_factory(context)
    assert scanner._arm is state.azure_clients.arm_for(context)
    assert scanner._arm._scope == cloud_profile("government").arm_scope
    commercial = endpoints._scanner_factory(AzureResourceContext())
    assert commercial._arm is endpoints._scanner._arm
    with pytest.raises(ValidationError, match="Government"):
        endpoints._scanner_factory(AzureResourceContext("government"))
    tools = state.mcp_endpoint_service
    assert tools._connection_token_resolver.__self__ is state.azure_clients
    assert tools._connection_validator.__self__ is state.azure_connection_service


async def test_demo_discovery_uses_only_government_fakes_for_selected_connection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pytest console entrypoint does not put the repository root on sys.path.
    monkeypatch.syspath_prepend(str(REPO_ROOT))
    from scripts.screenshots.demo_api import install_demo_services

    services = install_demo_services(client.app, [])
    try:
        created = await services.connections.create(ACTOR, request())
        suggestions = await services.endpoints.suggestions(
            ACTOR, azure_cloud="government", azure_connection_id=created.id
        )
        assert suggestions.suggestions
        assert all(
            suggestion.azure_cloud == "government"
            and suggestion.azure_connection_id == created.id
            and ".openai.azure.us" in str(suggestion.endpoint)
            for suggestion in suggestions.suggestions
        )
        target = endpoint(created.id)
        assert target.resource_audience is not None
        token = await services.mcp_endpoints._connection_token_resolver(
            target, target.resource_audience
        )
        assert token == "demo-management-token-not-real"
    finally:
        await services.aclose()
