import structlog

from mosaic_api.domain import AuditEvent, CredentialReference, McpEndpoint, McpEndpointSyncRun
from mosaic_api.repositories.cosmos_endpoint_state import CosmosEndpointStateBase

logger = structlog.get_logger()

SYNC_RUN_ENTITY_TYPE = "mcpEndpointSyncRun"


class CosmosMcpEndpointRepository(CosmosEndpointStateBase):
    """Registered MCP servers are desired state; their tools are observed state.

    Observed tool documents live in the same ``observed-state`` container as model deployments and
    gateway inventory, keyed on ``endpointId``. MCP endpoint IDs are prefixed differently from
    model endpoint IDs, so one endpoint's sweep can never reach another's documents.
    """

    async def list_endpoints(self, tenant_id: str) -> list[McpEndpoint]:
        items = await self._query(McpEndpoint, tenant_id, "mcpEndpoint")
        return sorted(items, key=lambda item: item.name.casefold())

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> McpEndpoint | None:
        return await self._read(McpEndpoint, tenant_id, endpoint_id)

    async def find_endpoint_by_url(self, tenant_id: str, endpoint: str) -> McpEndpoint | None:
        target = endpoint.casefold().rstrip("/")
        items = await self._query(McpEndpoint, tenant_id, "mcpEndpoint")
        return next(
            (item for item in items if str(item.endpoint).casefold().rstrip("/") == target),
            None,
        )

    async def create_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> McpEndpoint:
        await self._mutate(
            endpoint,
            None,
            audit_event,
            "create",
            conflict_message="This MCP server is already registered",
        )
        return endpoint

    async def save_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> McpEndpoint:
        await self._mutate(
            endpoint,
            None,
            audit_event,
            "replace",
            conflict_message="The MCP server changed; reload it and try again",
        )
        return endpoint

    async def record_endpoint_state(self, endpoint: McpEndpoint) -> McpEndpoint:
        await self._desired.upsert_item(self._document(endpoint))
        return endpoint

    async def delete_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_endpoint(endpoint.tenant_id, endpoint.id)
        await self._delete_runs_for_endpoint(
            McpEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, endpoint.tenant_id, endpoint.id
        )
        await self._mutate(
            endpoint,
            endpoint.id,
            audit_event,
            "delete",
            conflict_message="The MCP server changed; reload it and try again",
        )

    async def get_credential(
        self, tenant_id: str, credential_id: str
    ) -> CredentialReference | None:
        return await self._read(CredentialReference, tenant_id, credential_id)

    async def save_credential(
        self, credential: CredentialReference, audit_event: AuditEvent
    ) -> CredentialReference:
        await self._desired.upsert_item(self._document(credential))
        await self._audit.upsert_item(self._document(audit_event))
        return credential

    async def save_endpoint_sync_run(self, run: McpEndpointSyncRun) -> McpEndpointSyncRun:
        await self._save_run(run)
        return run

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> McpEndpointSyncRun | None:
        return await self._read_run(McpEndpointSyncRun, tenant_id, run_id)

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[McpEndpointSyncRun]:
        return await self._runs_for_endpoint(
            McpEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, tenant_id, endpoint_id, limit
        )

    async def list_unfinished_endpoint_sync_runs(self, tenant_id: str) -> list[McpEndpointSyncRun]:
        return await self._unfinished_runs(McpEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, tenant_id)
