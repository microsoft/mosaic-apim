import structlog

from mosaic_api.domain import (
    AuditEvent,
    CredentialReference,
    ModelEndpoint,
    ModelEndpointSyncRun,
)
from mosaic_api.repositories.cosmos_endpoint_state import CosmosEndpointStateBase

logger = structlog.get_logger()

SYNC_RUN_ENTITY_TYPE = "modelEndpointSyncRun"


class CosmosModelEndpointRepository(CosmosEndpointStateBase):
    """Registered endpoints are desired state; the models MOSAIC read are observed state.

    Observed model documents share the ``observed-state`` container with gateway inventory but are
    keyed on ``endpointId``, so the two sweeps never touch each other's documents. That
    endpoint-scoped half is inherited from
    :class:`~mosaic_api.repositories.cosmos_endpoint_state.CosmosEndpointStateBase`, which MCP
    endpoints share.
    """

    async def list_endpoints(self, tenant_id: str) -> list[ModelEndpoint]:
        items = await self._query(ModelEndpoint, tenant_id, "modelEndpoint")
        return sorted(items, key=lambda item: item.name.casefold())

    async def get_endpoint(self, tenant_id: str, endpoint_id: str) -> ModelEndpoint | None:
        return await self._read(ModelEndpoint, tenant_id, endpoint_id)

    async def find_endpoint_by_resource_id(
        self, tenant_id: str, azure_resource_id: str
    ) -> ModelEndpoint | None:
        items = await self._query(
            ModelEndpoint,
            tenant_id,
            "modelEndpoint",
            " AND LOWER(c.azureResourceId) = @resourceId",
            [{"name": "@resourceId", "value": azure_resource_id.casefold()}],
        )
        return items[0] if items else None

    async def find_endpoint_by_url(self, tenant_id: str, endpoint: str) -> ModelEndpoint | None:
        target = endpoint.casefold().rstrip("/")
        items = await self._query(ModelEndpoint, tenant_id, "modelEndpoint")
        return next(
            (item for item in items if str(item.endpoint).casefold().rstrip("/") == target),
            None,
        )

    async def create_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint:
        await self._mutate(
            endpoint,
            None,
            audit_event,
            "create",
            conflict_message="This model endpoint is already registered",
        )
        return endpoint

    async def save_endpoint(
        self, endpoint: ModelEndpoint, audit_event: AuditEvent
    ) -> ModelEndpoint:
        await self._mutate(
            endpoint,
            None,
            audit_event,
            "replace",
            conflict_message="The endpoint changed; reload it and try again",
        )
        return endpoint

    async def record_endpoint_state(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        await self._desired.upsert_item(self._document(endpoint))
        return endpoint

    async def delete_endpoint(self, endpoint: ModelEndpoint, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_endpoint(endpoint.tenant_id, endpoint.id)
        await self._delete_runs_for_endpoint(
            ModelEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, endpoint.tenant_id, endpoint.id
        )
        await self._mutate(
            endpoint,
            endpoint.id,
            audit_event,
            "delete",
            conflict_message="The endpoint changed; reload it and try again",
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

    async def save_endpoint_sync_run(self, run: ModelEndpointSyncRun) -> ModelEndpointSyncRun:
        await self._save_run(run)
        return run

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> ModelEndpointSyncRun | None:
        return await self._read_run(ModelEndpointSyncRun, tenant_id, run_id)

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[ModelEndpointSyncRun]:
        return await self._runs_for_endpoint(
            ModelEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, tenant_id, endpoint_id, limit
        )

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[ModelEndpointSyncRun]:
        return await self._unfinished_runs(ModelEndpointSyncRun, SYNC_RUN_ENTITY_TYPE, tenant_id)
