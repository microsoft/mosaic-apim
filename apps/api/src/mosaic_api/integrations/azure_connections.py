"""Explicit management client routing, independent of MOSAIC's host identity."""

from types import TracebackType
from typing import Any, Protocol, Self

from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential

from mosaic_api.azure_connections import AzureCloud, AzureConnectionService
from mosaic_api.clouds import cloud_profile
from mosaic_api.domain import Gateway, McpAuthMode, McpEndpoint
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations.apim.client import ArmClient
from mosaic_api.integrations.loganalytics import LogAnalyticsClient
from mosaic_api.integrations.mcp.credentials import EntraTokenProvider

MAX_CONNECTION_CLIENTS = 128


class AzureResourceContext(Protocol):
    @property
    def azure_cloud(self) -> AzureCloud: ...

    @property
    def azure_connection_id(self) -> str | None: ...


class ConnectionCredential:
    def __init__(self, service: AzureConnectionService, connection_id: str) -> None:
        self._service = service
        self._connection_id = connection_id

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        # Re-read the connection on every request: secret rotation and removal take effect
        # without an application restart or a process-local credential cache.
        return await self._service.token(self._connection_id, scopes)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.close()


class AzureClientRegistry:
    def __init__(
        self,
        service: AzureConnectionService,
        *,
        host_arm: ArmClient,
        host_credential: AsyncTokenCredential,
        host_logs: LogAnalyticsClient | None,
    ) -> None:
        self._service = service
        self._host_arm = host_arm
        self._host_credential = host_credential
        self._host_logs = host_logs
        self._arms: dict[tuple[AzureCloud, str], ArmClient] = {}
        self._logs: dict[tuple[AzureCloud, str], LogAnalyticsClient] = {}

    @staticmethod
    def _key(resource: AzureResourceContext) -> tuple[AzureCloud, str]:
        cloud_profile(resource.azure_cloud)
        connection_id = resource.azure_connection_id
        if connection_id is None or connection_id == "host":
            if resource.azure_cloud != "commercial":
                raise ValidationError("Government resources require a Government Azure connection")
            return "commercial", "host"
        return resource.azure_cloud, connection_id

    def _credential(self, key: tuple[AzureCloud, str]) -> AsyncTokenCredential:
        if key[1] == "host":
            return self._host_credential
        return ConnectionCredential(self._service, key[1])

    def arm_for(self, resource: AzureResourceContext) -> ArmClient:
        key = self._key(resource)
        if key == ("commercial", "host"):
            return self._host_arm
        if key not in self._arms:
            if len(self._arms) >= MAX_CONNECTION_CLIENTS:
                raise ConflictError("Too many Azure connections are active in this process")
            profile = cloud_profile(key[0])
            self._arms[key] = ArmClient(
                self._credential(key),
                base_url=profile.arm_endpoint,
                scope=profile.arm_scope,
            )
        return self._arms[key]

    def logs_for(self, gateway: Gateway) -> LogAnalyticsClient:
        key = self._key(gateway)
        if key == ("commercial", "host") and self._host_logs is not None:
            return self._host_logs
        if key not in self._logs:
            if len(self._logs) >= MAX_CONNECTION_CLIENTS:
                raise ConflictError(
                    "Too many Azure telemetry connections are active in this process"
                )
            self._logs[key] = LogAnalyticsClient(
                self._credential(key),
                endpoint=cloud_profile(key[0]).log_analytics_endpoint,
            )
        return self._logs[key]

    async def token_for_connection(self, endpoint: McpEndpoint, audience: str) -> str:
        if (
            endpoint.tenant_id != self._service.tenant_id
            or endpoint.auth_mode != McpAuthMode.MANAGED_IDENTITY
            or endpoint.resource_audience != audience
        ):
            raise ValidationError("MCP discovery must use its configured tenant and audience")
        key = self._key(endpoint)
        if key == ("commercial", "host"):
            return await EntraTokenProvider(self._host_credential).token_for(audience)
        return (await self._service.mcp_token(endpoint, audience)).token

    async def close(self) -> None:
        for arm in self._arms.values():
            await arm.close()
        for logs in self._logs.values():
            await logs.close()
        self._arms.clear()
        self._logs.clear()
