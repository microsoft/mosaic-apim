from mosaic_api.domain import AuditEvent, ModelEndpoint, ModelEndpointSyncRun
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.memory_endpoint_state import InMemoryEndpointStateBase
from mosaic_api.repositories.observation_writes import (
    MODEL_ENDPOINT_AUTHORED_FIELDS,
    merge_observation,
)

OBSERVATION_WRITE_ATTEMPTS = 3


class InMemoryModelEndpointRepository(InMemoryEndpointStateBase):
    """Explicit local/test persistence. Never selected in Azure environments."""

    def __init__(self) -> None:
        super().__init__()
        self.endpoints: dict[str, ModelEndpoint] = {}
        self._endpoint_versions: dict[str, int] = {}

    async def list_endpoints(self, tenant_id: str) -> list[ModelEndpoint]:
        return sorted(
            (item for item in self.endpoints.values() if item.tenant_id == tenant_id),
            key=lambda item: item.name.casefold(),
        )

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> ModelEndpoint | None:
        endpoint = self.endpoints.get(endpoint_id)
        return endpoint if endpoint and endpoint.tenant_id == tenant_id else None

    async def find_endpoint_by_resource_id(
        self, tenant_id: str, azure_resource_id: str
    ) -> ModelEndpoint | None:
        target = azure_resource_id.casefold()
        return next(
            (
                endpoint
                for endpoint in self.endpoints.values()
                if endpoint.tenant_id == tenant_id
                and (endpoint.azure_resource_id or "").casefold() == target
            ),
            None,
        )

    async def find_endpoint_by_url(self, tenant_id: str, endpoint: str) -> ModelEndpoint | None:
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

    async def create_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint:
        if endpoint.id in self.endpoints:
            raise ConflictError("This model endpoint is already registered")
        return await self.save_endpoint(endpoint, audit_event)

    async def save_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint:
        self.endpoints[endpoint.id] = endpoint
        self._endpoint_versions[endpoint.id] = self._endpoint_versions.get(endpoint.id, 0) + 1
        self.audit_events[audit_event.id] = audit_event
        return endpoint

    async def record_endpoint_state(self, endpoint: ModelEndpoint) -> ModelEndpoint | None:
        for _ in range(OBSERVATION_WRITE_ATTEMPTS):
            stored = await self.get_endpoint(endpoint.tenant_id, endpoint.id)
            if stored is None:
                return None
            version = self._endpoint_versions.get(endpoint.id, 0)
            await self._before_observation_replace()
            if await self.get_endpoint(endpoint.tenant_id, endpoint.id) is None:
                return None
            if self._endpoint_versions.get(endpoint.id, 0) != version:
                continue
            merged = merge_observation(stored, endpoint, MODEL_ENDPOINT_AUTHORED_FIELDS)
            self.endpoints[endpoint.id] = merged
            self._endpoint_versions[endpoint.id] = version + 1
            return merged
        raise ConflictError("The model endpoint changed while MOSAIC was recording observations")

    async def _before_observation_replace(self) -> None:
        return None

    async def delete_endpoint(self, endpoint: ModelEndpoint, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_endpoint(endpoint.tenant_id, endpoint.id)
        self._delete_runs_for_endpoint(endpoint.tenant_id, endpoint.id)
        self.endpoints.pop(endpoint.id, None)
        self._endpoint_versions.pop(endpoint.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def save_endpoint_sync_run(self, run: ModelEndpointSyncRun) -> ModelEndpointSyncRun:
        self.sync_runs[run.id] = run
        return run

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> ModelEndpointSyncRun | None:
        run = self.sync_runs.get(run_id)
        return run if run and run.tenant_id == tenant_id else None

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[ModelEndpointSyncRun]:
        return self._runs_for_endpoint(tenant_id, endpoint_id, limit)

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[ModelEndpointSyncRun]:
        return self._unfinished_runs(tenant_id)
