from mosaic_api.domain import AuditEvent, McpEndpoint, McpEndpointSyncRun
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.memory_endpoint_state import InMemoryEndpointStateBase


class InMemoryMcpEndpointRepository(InMemoryEndpointStateBase):
    """Explicit local/test persistence. Never selected in Azure environments."""

    def __init__(self) -> None:
        super().__init__()
        self.endpoints: dict[str, McpEndpoint] = {}

    async def list_endpoints(self, tenant_id: str) -> list[McpEndpoint]:
        return sorted(
            (item for item in self.endpoints.values() if item.tenant_id == tenant_id),
            key=lambda item: item.name.casefold(),
        )

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> McpEndpoint | None:
        endpoint = self.endpoints.get(endpoint_id)
        return endpoint if endpoint and endpoint.tenant_id == tenant_id else None

    async def find_endpoint_by_url(self, tenant_id: str, endpoint: str) -> McpEndpoint | None:
        target = endpoint.casefold().rstrip("/")
        return next(
            (
                item
                for item in self.endpoints.values()
                if item.tenant_id == tenant_id
                and str(item.endpoint).casefold().rstrip("/") == target
            ),
            None,
        )

    async def create_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> McpEndpoint:
        if endpoint.id in self.endpoints:
            raise ConflictError("This MCP server is already registered")
        return await self.save_endpoint(endpoint, audit_event)

    async def save_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> McpEndpoint:
        self.endpoints[endpoint.id] = endpoint
        self.audit_events[audit_event.id] = audit_event
        return endpoint

    async def record_endpoint_state(self, endpoint: McpEndpoint) -> McpEndpoint:
        self.endpoints[endpoint.id] = endpoint
        return endpoint

    async def delete_endpoint(self, endpoint: McpEndpoint, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_endpoint(endpoint.tenant_id, endpoint.id)
        self._delete_runs_for_endpoint(endpoint.tenant_id, endpoint.id)
        self.endpoints.pop(endpoint.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def save_endpoint_sync_run(self, run: McpEndpointSyncRun) -> McpEndpointSyncRun:
        self.sync_runs[run.id] = run
        return run

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> McpEndpointSyncRun | None:
        run = self.sync_runs.get(run_id)
        return run if run and run.tenant_id == tenant_id else None

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[McpEndpointSyncRun]:
        return self._runs_for_endpoint(tenant_id, endpoint_id, limit)

    async def list_unfinished_endpoint_sync_runs(self, tenant_id: str) -> list[McpEndpointSyncRun]:
        return self._unfinished_runs(tenant_id)
