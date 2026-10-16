from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, Mock

import pytest
from apim_double import RESOURCE_ID, FakeCredential
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError
from fastapi.testclient import TestClient
from mosaic_api.azure_connections import (
    AzureConnectionCreate,
    AzureConnectionSecret,
    AzureConnectionService,
    AzureConnectionUpdate,
    InMemoryAzureConnectionRepository,
    client_secret_token,
)
from mosaic_api.config import Settings
from mosaic_api.domain import ApimResourceId, KeyVaultSecretId
from mosaic_api.errors import (
    ConflictError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.apim.client import ArmClient
from mosaic_api.integrations.azure_connections import AzureClientRegistry, ConnectionCredential
from mosaic_api.main import create_app
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from pydantic import SecretStr

SECRET = "fictional-management-secret-never-returned"
TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
ACTOR = Actor(tenant_id="tenant-test", object_id="admin")


class Vault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def new_secret(self, name: str) -> KeyVaultSecretId:
        return KeyVaultSecretId.parse(f"https://isolated.vault.azure.net/secrets/{name}")

    async def put(
        self, secret: KeyVaultSecretId, value: SecretStr, *, tags: dict[str, str]
    ) -> None:
        self.values[secret.versionless] = value.get_secret_value()

    async def delete(self, secret: KeyVaultSecretId) -> bool:
        return self.values.pop(secret.versionless, None) is not None

    async def read(self, uri: str) -> str:
        return self.values[uri]


def build() -> tuple[AzureConnectionService, Vault]:
    vault = Vault()

    async def issue(connection: object, secret: str, scopes: tuple[str, ...]) -> AccessToken:
        if secret == "invalid":
            raise UpstreamAuthorizationError("Credential rejected")
        return AccessToken("fictional-access-token", 9999999999)

    return AzureConnectionService(
        InMemoryAzureConnectionRepository(),
        tenant_id=ACTOR.tenant_id,
        gateways=InMemoryGatewayRepository(),
        endpoints=InMemoryModelEndpointRepository(),
        mcps=InMemoryMcpEndpointRepository(),
        secret_reader=vault.read,
        secret_store=vault,
        token_issuer=issue,
    ), vault


def request() -> AzureConnectionCreate:
    return AzureConnectionCreate(
        name="Government estate",
        azure_cloud="government",
        azure_tenant_id=TENANT,
        client_id=CLIENT,
        client_secret=SecretStr(SECRET),
    )


async def test_create_and_test_are_secret_free_and_tenant_scoped() -> None:
    service, vault = build()
    created = await service.create(ACTOR, request())
    assert created.azure_cloud == "government"
    assert created.status == "untested"
    assert list(vault.values.values()) == [SECRET]
    assert SECRET not in created.model_dump_json()
    assert "credentialSecretUri" not in created.model_dump()
    assert "clientSecret" not in request().model_dump()
    with pytest.raises(NotFoundError):
        await service.get("another-tenant", created.id)
    tested = await service.test(ACTOR, created.id)
    assert tested.status == "ready"
    assert SECRET not in tested.model_dump_json()
    assert service.repository.audit_events  # type: ignore[attr-defined]
    assert SECRET not in str(service.repository.audit_events)  # type: ignore[attr-defined]


async def test_wrong_cloud_host_fallback_and_unapproved_audiences_are_rejected() -> None:
    service, _ = build()
    created = await service.create(ACTOR, request())
    await service.validate_resource(ACTOR.tenant_id, created.id, "government")
    await service.validate_resource(ACTOR.tenant_id, None, "commercial")
    with pytest.raises(ValidationError, match="different cloud"):
        await service.validate_resource(ACTOR.tenant_id, created.id, "commercial")
    with pytest.raises(ValidationError, match="Government"):
        await service.validate_resource(ACTOR.tenant_id, None, "government")
    with pytest.raises(ValidationError, match="audience"):
        await service.token(created.id, ("https://management.azure.com/.default",))
    token = await service.token(created.id, ("https://management.usgovcloudapi.net/.default",))
    assert token.token == "fictional-access-token"


async def test_rotation_verifies_before_activation_and_removes_retired_secret() -> None:
    service, vault = build()
    created = await service.create(ACTOR, request())
    previous = await service.get(ACTOR.tenant_id, created.id)
    with pytest.raises(UpstreamAuthorizationError):
        await service.replace_secret(
            ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("invalid"))
        )
    assert vault.values[previous.credential_secret_uri] == SECRET
    replaced = await service.replace_secret(
        ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("replacement"))
    )
    current = await service.get(ACTOR.tenant_id, created.id)
    assert replaced.revision == 2
    assert current.credential_secret_uri != previous.credential_secret_uri
    assert previous.credential_secret_uri not in vault.values
    assert list(vault.values.values()) == ["replacement"]
    assert not current.retired_secret_uris
    assert "replacement" not in replaced.model_dump_json()


async def test_failed_rotation_metadata_does_not_change_active_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, vault = build()
    created = await service.create(ACTOR, request())
    previous = await service.get(ACTOR.tenant_id, created.id)
    monkeypatch.setattr(
        service.repository,
        "save_connection",
        AsyncMock(side_effect=ConflictError("Concurrent edit")),
    )
    with pytest.raises(ConflictError):
        await service.replace_secret(
            ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("replacement"))
        )
    current = await service.get(ACTOR.tenant_id, created.id)
    assert current.credential_secret_uri == previous.credential_secret_uri
    assert list(vault.values.values()) == [SECRET]


@pytest.mark.parametrize("rotating", [False, True])
async def test_ambiguous_vault_write_is_compensated_without_switching_live_credentials(
    monkeypatch: pytest.MonkeyPatch,
    rotating: bool,
) -> None:
    service, vault = build()
    created = await service.create(ACTOR, request()) if rotating else None
    put = vault.put

    async def failed_after_write(
        secret: KeyVaultSecretId,
        value: SecretStr,
        *,
        tags: dict[str, str],
    ) -> None:
        await put(secret, value, tags=tags)
        raise UpstreamError("The vault response was lost")

    monkeypatch.setattr(vault, "put", failed_after_write)
    with pytest.raises(UpstreamError):
        if created:
            await service.replace_secret(
                ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("replacement"))
            )
        else:
            await service.create(ACTOR, request())
    assert list(vault.values.values()) == ([SECRET] if rotating else [])
    if created:
        assert (await service.get(ACTOR.tenant_id, created.id)).revision == 1


async def test_failed_retired_secret_cleanup_retains_recovery_references(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, vault = build()
    created = await service.create(ACTOR, request())
    previous = await service.get(ACTOR.tenant_id, created.id)
    delete = vault.delete
    monkeypatch.setattr(vault, "delete", AsyncMock(side_effect=UpstreamError("Vault unavailable")))
    with pytest.raises(UpstreamError):
        await service.replace_secret(
            ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("replacement"))
        )
    current = await service.get(ACTOR.tenant_id, created.id)
    assert current.retired_secret_uris == [previous.credential_secret_uri]
    assert current.credential_secret_uri != previous.credential_secret_uri
    assert "cleanup pending" in (current.status_message or "")
    monkeypatch.setattr(vault, "delete", delete)
    await service.delete(ACTOR, created.id)
    assert not vault.values


async def test_dynamic_credentials_use_current_secret_and_removal_takes_effect() -> None:
    service, _ = build()
    issuer = AsyncMock(wraps=service._issue)
    service._issue = issuer
    created = await service.create(ACTOR, request())
    credential = ConnectionCredential(service, created.id)
    scope = "https://management.usgovcloudapi.net/.default"
    assert (await credential.get_token(scope)).token == "fictional-access-token"
    assert issuer.call_args.args[1] == SECRET
    await service.replace_secret(
        ACTOR, created.id, AzureConnectionSecret(client_secret=SecretStr("replacement"))
    )
    assert (await credential.get_token(scope)).token == "fictional-access-token"
    assert issuer.call_args.args[1] == "replacement"
    await service.delete(ACTOR, created.id)
    with pytest.raises(NotFoundError):
        await credential.get_token(scope)
    await credential.close()


async def test_registry_keeps_host_and_government_clients_separate() -> None:
    service, _ = build()
    created = await service.create(ACTOR, request())
    host_credential = cast(AsyncTokenCredential, FakeCredential())
    host_arm = ArmClient(host_credential)
    registry = AzureClientRegistry(
        service, host_arm=host_arm, host_credential=host_credential, host_logs=None
    )
    commercial = ApimResourceId.parse(RESOURCE_ID)
    government = ApimResourceId.parse(
        RESOURCE_ID, azure_cloud="government", azure_connection_id=created.id
    )
    assert registry.arm_for(commercial) is host_arm
    government_arm = registry.arm_for(government)
    assert government_arm is not host_arm
    assert registry.arm_for(government) is government_arm
    with pytest.raises(ValidationError, match="Government"):
        registry.arm_for(ApimResourceId.parse(RESOURCE_ID, azure_cloud="government"))
    await registry.close()
    await host_arm.close()


async def test_no_vault_no_connection_and_in_use_connection_cannot_be_deleted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, vault = build()
    service._store = None
    with pytest.raises(ConflictError, match="separate management"):
        await service.create(ACTOR, request())
    assert not vault.values
    service._store = vault
    created = await service.create(ACTOR, request())
    monkeypatch.setattr(
        service._gateways,
        "list_gateways",
        AsyncMock(
            return_value=[
                SimpleNamespace(azure_connection_id=created.id),
            ]
        ),
    )
    with pytest.raises(ConflictError, match="registered resources"):
        await service.delete(ACTOR, created.id)
    assert len(vault.values) == 1
    monkeypatch.setattr(service._gateways, "list_gateways", AsyncMock(return_value=[]))
    renamed = await service.rename(ACTOR, created.id, AzureConnectionUpdate(name="New name"))
    assert renamed.name == "New name"
    await service.delete(ACTOR, created.id)
    assert not vault.values
    with pytest.raises(NotFoundError):
        await service.get(ACTOR.tenant_id, created.id)


def test_connection_routes_are_administrator_only_and_validation_never_echoes_secret(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/azure-connections")
    assert response.status_code == 200
    assert response.json()[0]["id"] == "host"
    assert response.headers["cache-control"] == "no-store, private"
    payload = {
        "name": "Government",
        "azureCloud": "government",
        "azureTenantId": "invalid",
        "clientId": CLIENT,
        "clientSecret": SECRET,
    }
    rejected = client.post("/api/v1/azure-connections", json=payload)
    assert rejected.status_code == 422
    assert SECRET not in rejected.text
    assert client.get("/readyz").status_code == 200


def test_users_cannot_manage_connections(settings: Settings) -> None:
    with TestClient(create_app(settings.model_copy(update={"local_roles": ["User"]}))) as user:
        for method, path, body in [
            ("GET", "", None),
            ("POST", "", {}),
            ("PATCH", "/connection", {"name": "Name"}),
            ("DELETE", "/connection", None),
            ("POST", "/connection/test", None),
            ("PUT", "/connection/secret", {"clientSecret": SECRET}),
        ]:
            response = user.request(method, f"/api/v1/azure-connections{path}", json=body)
            assert response.status_code == 403
            assert SECRET not in response.text


async def test_client_secret_authentication_uses_government_authority_and_sanitizes_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mosaic_api import azure_connections

    service, _ = build()
    created = await service.create(ACTOR, request())
    connection = await service.get(ACTOR.tenant_id, created.id)
    credential = AsyncMock()
    credential.__aenter__.return_value = credential
    credential.get_token.return_value = AccessToken("fictional-token", 9999999999)
    constructor = Mock(return_value=credential)
    monkeypatch.setattr(azure_connections, "ClientSecretCredential", constructor)
    scope = "https://management.usgovcloudapi.net/.default"
    assert (await client_secret_token(connection, SECRET, (scope,))).token == "fictional-token"
    constructor.assert_called_once_with(
        TENANT, CLIENT, SECRET, authority="https://login.microsoftonline.us"
    )
    credential.get_token.assert_awaited_once_with(scope)
    credential.__aexit__.assert_awaited_once()
    credential.get_token.side_effect = ClientAuthenticationError(message=SECRET)
    with pytest.raises(UpstreamAuthorizationError) as failure:
        await client_secret_token(connection, SECRET, (scope,))
    assert SECRET not in str(failure.value)
    assert SECRET not in str(failure.value.details)


def test_api_connection_lifecycle_returns_only_safe_metadata(client: TestClient) -> None:
    service, _ = build()
    client.app.state.azure_connection_service = service
    payload = {
        "name": "Government",
        "azureCloud": "government",
        "azureTenantId": TENANT,
        "clientId": CLIENT,
        "clientSecret": SECRET,
    }
    created = client.post("/api/v1/azure-connections", json=payload)
    assert created.status_code == 201
    connection_id = created.json()["id"]
    path = f"/api/v1/azure-connections/{connection_id}"
    assert "clientSecret" not in created.json()
    assert "credentialSecretUri" not in created.json()
    assert SECRET not in client.get("/api/v1/azure-connections").text
    assert client.patch(path, json={"name": "Renamed"}).json()["name"] == "Renamed"
    assert client.post(f"{path}/test").json()["status"] == "ready"
    replacement = client.put(f"{path}/secret", json={"clientSecret": "fictional-replacement"})
    assert replacement.status_code == 200
    assert replacement.json()["revision"] == 2
    assert "fictional-replacement" not in replacement.text
    assert client.delete(path).status_code == 204
    assert client.post(f"{path}/test").status_code == 404
