"""Tenant-scoped management connections; secret values live only in the isolated vault."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from azure.core.credentials import AccessToken
from azure.core.exceptions import (
    ClientAuthenticationError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity.aio import ClientSecretCredential
from pydantic import Field, SecretStr, field_validator

from mosaic_api.clouds import cloud_profile, known_azure_host_cloud
from mosaic_api.domain import (
    AuditEvent,
    Entity,
    KeyVaultSecretId,
    McpAuthMode,
    McpEndpoint,
    MosaicModel,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    UpstreamAuthorizationError,
    UpstreamError,
    ValidationError,
)
from mosaic_api.integrations.key_vault import KeyStore
from mosaic_api.integrations.mcp.credentials import scope_for_audience
from mosaic_api.repositories import (
    GatewayRepository,
    McpEndpointRepository,
    ModelEndpointRepository,
)
from mosaic_api.repositories.cosmos import CosmosRepositoryBase
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import scope_lease

AzureCloud = Literal["commercial", "government"]
CONNECTION_SCOPE = "azure-connections"


class AzureConnection(Entity):
    entity_type: Literal["azureConnection"] = "azureConnection"
    name: str
    azure_cloud: AzureCloud
    azure_tenant_id: str
    client_id: str
    credential_secret_uri: str = Field(repr=False)
    retired_secret_uris: list[str] = Field(default_factory=list, repr=False)
    revision: int = 1
    status: Literal["untested", "ready", "failed"] = "untested"
    status_message: str | None = None
    checked_at: datetime | None = None


class AzureConnectionView(MosaicModel):
    id: str
    name: str
    azure_cloud: AzureCloud
    azure_tenant_id: str
    authentication: Literal["managedIdentity", "clientSecret"]
    client_id: str | None = None
    revision: int = 1
    status: Literal["untested", "ready", "failed"] = "untested"
    status_message: str | None = None
    checked_at: datetime | None = None
    in_use: bool = False


class AzureConnectionCreate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    azure_cloud: AzureCloud = "government"
    azure_tenant_id: str
    client_id: str
    client_secret: SecretStr = Field(exclude=True, repr=False)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter a connection name")
        return value.strip()

    @field_validator("azure_tenant_id", "client_id")
    @classmethod
    def guid(cls, value: str) -> str:
        try:
            return str(UUID(value.strip()))
        except ValueError:
            raise ValueError("Enter a specific tenant or application GUID") from None

    @field_validator("client_secret")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or len(secret) > 4096 or any(char in secret for char in "\r\n"):
            raise ValueError("Enter a client secret value without line breaks")
        return value


class AzureConnectionUpdate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        return AzureConnectionCreate.nonempty_name(value)


class AzureConnectionSecret(MosaicModel):
    client_secret: SecretStr = Field(exclude=True, repr=False)

    @field_validator("client_secret")
    @classmethod
    def nonempty_secret(cls, value: SecretStr) -> SecretStr:
        return AzureConnectionCreate.nonempty_secret(value)


class AzureConnectionRepository(Protocol):
    async def ready(self) -> bool: ...
    async def close(self) -> None: ...
    async def list_connections(self, tenant_id: str) -> list[AzureConnection]: ...
    async def get_connection(
        self, tenant_id: str, connection_id: str
    ) -> AzureConnection | None: ...
    async def create_connection(
        self, connection: AzureConnection, audit: AuditEvent
    ) -> AzureConnection: ...
    async def save_connection(
        self, connection: AzureConnection, audit: AuditEvent
    ) -> AzureConnection: ...
    async def delete_connection(self, connection: AzureConnection, audit: AuditEvent) -> None: ...


class InMemoryAzureConnectionRepository:
    def __init__(self) -> None:
        self.connections: dict[str, AzureConnection] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def list_connections(self, tenant_id: str) -> list[AzureConnection]:
        return sorted(
            [
                item.model_copy(deep=True)
                for item in self.connections.values()
                if item.tenant_id == tenant_id
            ],
            key=lambda item: item.name.casefold(),
        )

    async def get_connection(self, tenant_id: str, connection_id: str) -> AzureConnection | None:
        item = self.connections.get(connection_id)
        return item.model_copy(deep=True) if item and item.tenant_id == tenant_id else None

    async def create_connection(
        self, connection: AzureConnection, audit: AuditEvent
    ) -> AzureConnection:
        if connection.id in self.connections:
            raise ConflictError("This Azure connection already exists")
        return await self._store(connection, audit)

    async def _store(self, connection: AzureConnection, audit: AuditEvent) -> AzureConnection:
        saved = connection.model_copy(update={"etag": new_id("etag")}, deep=True)
        self.connections[saved.id] = saved
        self.audit_events[audit.id] = audit
        return saved.model_copy(deep=True)

    async def save_connection(
        self, connection: AzureConnection, audit: AuditEvent
    ) -> AzureConnection:
        current = await self.get_connection(connection.tenant_id, connection.id)
        if current is None or current.etag != connection.etag:
            raise ConflictError("The Azure connection changed; reload it and try again")
        return await self._store(connection, audit)

    async def delete_connection(self, connection: AzureConnection, audit: AuditEvent) -> None:
        current = await self.get_connection(connection.tenant_id, connection.id)
        if current is None or current.etag != connection.etag:
            raise ConflictError("The Azure connection changed; reload it and try again")
        del self.connections[connection.id]
        self.audit_events[audit.id] = audit


class CosmosAzureConnectionRepository(CosmosRepositoryBase):
    async def list_connections(self, tenant_id: str) -> list[AzureConnection]:
        return sorted(
            await self._query(AzureConnection, tenant_id, "azureConnection"),
            key=lambda item: item.name.casefold(),
        )

    async def get_connection(self, tenant_id: str, connection_id: str) -> AzureConnection | None:
        return await self._read(AzureConnection, tenant_id, connection_id)

    async def create_connection(
        self, connection: AzureConnection, audit: AuditEvent
    ) -> AzureConnection:
        await self._mutate(
            connection, None, audit, "create", conflict_message="This Azure connection exists"
        )
        saved = await self.get_connection(connection.tenant_id, connection.id)
        if saved is None:
            raise ConflictError("The Azure connection disappeared; reload it")
        return saved

    async def save_connection(
        self, connection: AzureConnection, audit: AuditEvent
    ) -> AzureConnection:
        await self._mutate(
            connection,
            None,
            audit,
            "replace",
            conflict_message="The Azure connection changed; reload it and try again",
        )
        saved = await self.get_connection(connection.tenant_id, connection.id)
        if saved is None:
            raise ConflictError("The Azure connection disappeared; reload it")
        return saved

    async def delete_connection(self, connection: AzureConnection, audit: AuditEvent) -> None:
        await self._mutate(
            connection,
            connection.id,
            audit,
            "delete",
            conflict_message="The Azure connection changed; reload it and try again",
        )


SecretReader = Callable[[str], Awaitable[str]]


async def client_secret_token(
    connection: AzureConnection, secret: str, scopes: tuple[str, ...]
) -> AccessToken:
    profile = cloud_profile(connection.azure_cloud)
    try:
        async with ClientSecretCredential(
            connection.azure_tenant_id,
            connection.client_id,
            secret,
            authority=profile.authority,
        ) as credential:
            return await credential.get_token(*scopes)
    except ClientAuthenticationError:
        raise UpstreamAuthorizationError(
            "Azure connection authentication failed. Check the target cloud, tenant, application "
            "ID, and secret expiry, then replace the secret or test the connection again."
        ) from None
    except (ServiceRequestError, ServiceResponseError):
        raise UpstreamError(
            "The Azure connection could not reach its cloud's identity service. "
            "Check outbound connectivity and retry."
        ) from None


TokenIssuer = Callable[[AzureConnection, str, tuple[str, ...]], Awaitable[AccessToken]]


class AzureConnectionService:
    def __init__(
        self,
        repository: AzureConnectionRepository,
        *,
        tenant_id: str,
        gateways: GatewayRepository,
        endpoints: ModelEndpointRepository,
        mcps: McpEndpointRepository,
        secret_reader: SecretReader,
        secret_store: KeyStore | None,
        token_issuer: TokenIssuer = client_secret_token,
    ) -> None:
        self.repository = repository
        self.tenant_id = tenant_id
        self._gateways = gateways
        self._endpoints = endpoints
        self._mcps = mcps
        self._read = secret_reader
        self._store = secret_store
        self._issue = token_issuer

    @staticmethod
    def _audit(actor: Actor, connection_id: str, action: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            resource_type="azureConnection",
            resource_id=connection_id,
            action=f"azureConnection.{action}",
            actor_object_id=actor.object_id,
        )

    async def get(self, tenant_id: str, connection_id: str) -> AzureConnection:
        item = await self.repository.get_connection(tenant_id, connection_id)
        if item is None:
            raise NotFoundError("Azure connection was not found")
        return item

    async def validate_resource(
        self, tenant_id: str, connection_id: str | None, cloud: str
    ) -> None:
        if connection_id is None or connection_id == "host":
            if cloud != "commercial":
                raise ValidationError("Choose an Azure Government service-principal connection")
            return
        item = await self.get(tenant_id, connection_id)
        if item.azure_cloud != cloud:
            raise ValidationError("The Azure connection belongs to a different cloud")

    async def _in_use(self, tenant_id: str, connection_id: str) -> bool:
        return (
            any(
                item.azure_connection_id == connection_id
                for item in await self._gateways.list_gateways(tenant_id)
            )
            or any(
                item.azure_connection_id == connection_id
                for item in await self._endpoints.list_endpoints(tenant_id)
            )
            or any(
                item.azure_connection_id == connection_id
                for item in await self._mcps.list_endpoints(tenant_id)
            )
        )

    async def view(self, connection: AzureConnection) -> AzureConnectionView:
        return AzureConnectionView(
            id=connection.id,
            name=connection.name,
            azure_cloud=connection.azure_cloud,
            azure_tenant_id=connection.azure_tenant_id,
            client_id=connection.client_id,
            authentication="clientSecret",
            revision=connection.revision,
            status=connection.status,
            status_message=connection.status_message,
            checked_at=connection.checked_at,
            in_use=await self._in_use(connection.tenant_id, connection.id),
        )

    async def list(self, actor: Actor) -> list[AzureConnectionView]:
        return [
            AzureConnectionView(
                id="host",
                name="MOSAIC managed identity",
                azure_cloud="commercial",
                azure_tenant_id=actor.tenant_id,
                authentication="managedIdentity",
                status="untested",
                status_message="Access is checked on each resource; this is the host identity.",
                in_use=True,
            ),
            *[
                await self.view(item)
                for item in await self.repository.list_connections(actor.tenant_id)
            ],
        ]

    def _require_store(self) -> KeyStore:
        if self._store is None:
            raise ConflictError(
                "Configure MOSAIC_MANAGEMENT_KEY_VAULT_URI with the separate management "
                "vault before adding a connection. Do not use APIM's backend-secret vault."
            )
        return self._store

    async def create(self, actor: Actor, request: AzureConnectionCreate) -> AzureConnectionView:
        store = self._require_store()
        async with scope_lease(self._gateways, actor.tenant_id, CONNECTION_SCOPE):
            connection_id = new_id("azureConnection")
            secret = store.new_secret(f"mosaic-management-{connection_id.replace('_', '-')}")
            connection = AzureConnection(
                id=connection_id,
                tenant_id=actor.tenant_id,
                name=request.name,
                azure_cloud=request.azure_cloud,
                azure_tenant_id=request.azure_tenant_id,
                client_id=request.client_id,
                credential_secret_uri=secret.versionless,
            )
            try:
                await store.put(secret, request.client_secret, tags={"managedBy": "MOSAIC"})
                saved = await self.repository.create_connection(
                    connection, self._audit(actor, connection_id, "created")
                )
            except Exception:
                # Compensate an ambiguous vault response or a failed document write.
                await store.delete(secret)
                raise
        return await self.view(saved)

    async def rename(
        self, actor: Actor, connection_id: str, request: AzureConnectionUpdate
    ) -> AzureConnectionView:
        async with scope_lease(self._gateways, actor.tenant_id, CONNECTION_SCOPE):
            connection = await self.get(actor.tenant_id, connection_id)
            saved = await self.repository.save_connection(
                connection.model_copy(update={"name": request.name, "updated_at": utc_now()}),
                self._audit(actor, connection_id, "updated"),
            )
        return await self.view(saved)

    async def token(self, connection_id: str, scopes: tuple[str, ...]) -> AccessToken:
        connection = await self.get(self.tenant_id, connection_id)
        profile = cloud_profile(connection.azure_cloud)
        permitted = {profile.arm_scope}
        # The query endpoint and audience belong to the same closed cloud profile.
        from mosaic_api.integrations.loganalytics.client import query_scope

        permitted.add(query_scope(profile.log_analytics_endpoint))
        permitted.add(f"{profile.cognitive_audience}/.default")
        if not scopes or any(scope not in permitted for scope in scopes):
            raise ValidationError("This Azure connection cannot issue a token for that audience")
        secret = await self._read(connection.credential_secret_uri)
        return await self._issue(connection, secret, scopes)

    async def mcp_token(self, endpoint: McpEndpoint, audience: str) -> AccessToken:
        """Issue only the discovery audience explicitly configured on this tenant's endpoint."""

        if (
            endpoint.tenant_id != self.tenant_id
            or endpoint.auth_mode != McpAuthMode.MANAGED_IDENTITY
            or endpoint.resource_audience != audience
            or not endpoint.azure_connection_id
            or endpoint.azure_connection_id == "host"
        ):
            raise ValidationError(
                "MCP discovery requires its configured Azure connection and audience"
            )
        scope = scope_for_audience(audience)
        if any(character.isspace() for character in scope):
            raise ValidationError("An MCP discovery audience must name exactly one token resource")
        resource = scope.removesuffix("/.default").rstrip("/")
        normalized_resource = resource.casefold()
        reserved = {
            "https://graph.microsoft.com", "https://graph.microsoft.us",
            "https://dod-graph.microsoft.us",
        }
        for cloud in ("commercial", "government"):
            profile = cloud_profile(cloud)
            reserved.update({
                profile.authority, profile.arm_endpoint, profile.log_analytics_endpoint,
                profile.portal_endpoint, f"https://{profile.key_vault_suffix.removeprefix('.')}",
            })
        if normalized_resource in reserved:
            raise ValidationError(
                "MCP discovery cannot request Azure management or directory tokens"
            )
        audience_cloud = known_azure_host_cloud(resource)
        if normalized_resource in {
            "https://cognitiveservices.azure.com", "https://ai.azure.com",
        }:
            audience_cloud = "commercial"
        elif normalized_resource == "https://cognitiveservices.azure.us":
            audience_cloud = "government"
        endpoint_cloud = known_azure_host_cloud(str(endpoint.endpoint))
        if (
            audience_cloud is not None and audience_cloud != endpoint.azure_cloud
        ) or (
            endpoint_cloud is not None and endpoint_cloud != endpoint.azure_cloud
        ):
            raise ValidationError(
                "The MCP discovery audience or hostname belongs to a different cloud"
            )
        connection = await self.get(self.tenant_id, endpoint.azure_connection_id)
        if connection.azure_cloud != endpoint.azure_cloud:
            raise ValidationError("The Azure connection belongs to a different cloud")
        secret = await self._read(connection.credential_secret_uri)
        return await self._issue(connection, secret, (scope,))

    async def test(self, actor: Actor, connection_id: str) -> AzureConnectionView:
        async with scope_lease(self._gateways, actor.tenant_id, CONNECTION_SCOPE):
            connection = await self.get(actor.tenant_id, connection_id)
            try:
                await self.token(connection.id, (cloud_profile(connection.azure_cloud).arm_scope,))
                state: Literal["ready", "failed"] = "ready"
                message = (
                    "Authentication succeeded. Resource and telemetry permissions are checked "
                    "separately during onboarding and access checks."
                )
            except DomainError:
                state = "failed"
                message = (
                    "Authentication failed. Check vault access, target cloud, tenant, application "
                    "ID and secret expiry. Resource permissions have not been verified."
                )
            saved = await self.repository.save_connection(
                connection.model_copy(
                    update={
                        "status": state,
                        "status_message": message,
                        "checked_at": utc_now(),
                    }
                ),
                self._audit(actor, connection_id, f"test.{state}"),
            )
        return await self.view(saved)

    async def replace_secret(
        self, actor: Actor, connection_id: str, request: AzureConnectionSecret
    ) -> AzureConnectionView:
        store = self._require_store()
        async with scope_lease(self._gateways, actor.tenant_id, CONNECTION_SCOPE):
            connection = await self.get(actor.tenant_id, connection_id)
            await self._issue(
                connection,
                request.client_secret.get_secret_value(),
                (cloud_profile(connection.azure_cloud).arm_scope,),
            )
            # A fresh secret name keeps failed metadata writes from switching live credentials.
            candidate = store.new_secret(
                f"mosaic-management-{new_id('credential').replace('_', '-')}"
            )
            try:
                await store.put(
                    candidate,
                    request.client_secret,
                    tags={"managedBy": "MOSAIC"},
                )
                saved = await self.repository.save_connection(
                    connection.model_copy(
                        update={
                            "credential_secret_uri": candidate.versionless,
                            "retired_secret_uris": [
                                *connection.retired_secret_uris,
                                connection.credential_secret_uri,
                            ],
                            "revision": connection.revision + 1,
                            "status": "ready",
                            "status_message": (
                                "Replacement active; prior secret cleanup pending. "
                                "Retry if it fails."
                            ),
                            "checked_at": utc_now(),
                            "updated_at": utc_now(),
                        }
                    ),
                    self._audit(actor, connection_id, "secretReplaced"),
                )
            except Exception:
                await store.delete(candidate)
                raise
            for uri in saved.retired_secret_uris:
                await store.delete(KeyVaultSecretId.parse(uri))
            saved = await self.repository.save_connection(
                saved.model_copy(
                    update={
                        "retired_secret_uris": [],
                        "status_message": (
                            "Replacement authenticated. Recheck resource permissions."
                        ),
                    }
                ),
                self._audit(actor, connection_id, "retiredSecretsRemoved"),
            )
        return await self.view(saved)

    async def delete(self, actor: Actor, connection_id: str) -> None:
        store = self._require_store()
        async with scope_lease(self._gateways, actor.tenant_id, CONNECTION_SCOPE):
            connection = await self.get(actor.tenant_id, connection_id)
            if await self._in_use(actor.tenant_id, connection_id):
                raise ConflictError("Remove this connection's registered resources first")
            await store.delete(KeyVaultSecretId.parse(connection.credential_secret_uri))
            for uri in connection.retired_secret_uris:
                await store.delete(KeyVaultSecretId.parse(uri))
            await self.repository.delete_connection(
                connection, self._audit(actor, connection_id, "removed")
            )
