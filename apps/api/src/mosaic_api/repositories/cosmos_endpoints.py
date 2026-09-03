from typing import Any

import structlog
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient

from mosaic_api.domain import (
    AuditEvent,
    CredentialReference,
    GatewaySyncStatus,
    ModelEndpoint,
    ModelEndpointSyncRun,
)
from mosaic_api.observed import ObservedModelEntity
from mosaic_api.repositories.cosmos import CosmosRepositoryBase

logger = structlog.get_logger()


class CosmosModelEndpointRepository(CosmosRepositoryBase):
    """Registered endpoints are desired state; the models MOSAIC read are observed state.

    Observed model documents share the ``observed-state`` container with gateway inventory but are
    keyed on ``endpointId``, so the two sweeps never touch each other's documents.
    """

    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
        sync_operations_container: str,
        observed_state_container: str,
        *,
        owns_client: bool = False,
    ) -> None:
        super().__init__(
            client,
            database_name,
            desired_state_container,
            audit_events_container,
            owns_client=owns_client,
        )
        database = client.get_database_client(database_name)
        self._sync: ContainerProxy = database.get_container_client(sync_operations_container)
        self._observed: ContainerProxy = database.get_container_client(observed_state_container)

    async def ready(self) -> bool:
        if not await super().ready():
            return False
        try:
            await self._sync.read()
            await self._observed.read()
        except exceptions.CosmosHttpResponseError:
            return False
        return True

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
            (
                item
                for item in items
                if str(item.endpoint).casefold().rstrip("/") == target
            ),
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
        await self._delete_sync_runs(endpoint.tenant_id, endpoint.id)
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
        await self._sync.upsert_item(self._document(run))
        return run

    async def get_endpoint_sync_run(
        self, tenant_id: str, run_id: str
    ) -> ModelEndpointSyncRun | None:
        try:
            item = await self._sync.read_item(item=run_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        run = self._model(ModelEndpointSyncRun, item)
        return run if run.tenant_id == tenant_id else None

    async def _query_sync_runs(
        self, tenant_id: str, extra: str, parameters: list[dict[str, Any]]
    ) -> list[ModelEndpointSyncRun]:
        query = "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType"
        query += extra
        items = self._sync.query_items(
            query=query,
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": "modelEndpointSyncRun"},
                *parameters,
            ],
            partition_key=tenant_id,
        )
        return [self._model(ModelEndpointSyncRun, item) async for item in items]

    async def list_endpoint_sync_runs(
        self, tenant_id: str, endpoint_id: str, *, limit: int = 20
    ) -> list[ModelEndpointSyncRun]:
        runs = await self._query_sync_runs(
            tenant_id,
            " AND c.endpointId = @endpointId",
            [{"name": "@endpointId", "value": endpoint_id}],
        )
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[:limit]

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[ModelEndpointSyncRun]:
        return await self._query_sync_runs(
            tenant_id,
            " AND c.status = @status",
            [{"name": "@status", "value": GatewaySyncStatus.RUNNING.value}],
        )

    async def _delete_sync_runs(self, tenant_id: str, endpoint_id: str) -> None:
        for run in await self.list_endpoint_sync_runs(tenant_id, endpoint_id, limit=1000):
            try:
                await self._sync.delete_item(item=run.id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                continue

    async def replace_observed_models(
        self,
        tenant_id: str,
        endpoint_id: str,
        entities: list[ObservedModelEntity],
        snapshot_id: str,
        incomplete_types: set[str] | None = None,
    ) -> int:
        for entity in entities:
            await self._observed.upsert_item(self._document(entity))
        # A section MOSAIC failed to read looks empty in this snapshot. Sweeping those types would
        # turn "could not read" into "does not exist", so they keep their previous documents until a
        # clean sync supersedes them.
        untrusted = sorted(incomplete_types or set())
        stale = self._observed.query_items(
            query=(
                "SELECT c.id FROM c WHERE c.tenantId = @tenantId AND c.endpointId = @endpointId "
                "AND c.snapshotId != @snapshotId "
                "AND NOT ARRAY_CONTAINS(@untrusted, c.entityType)"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@endpointId", "value": endpoint_id},
                {"name": "@snapshotId", "value": snapshot_id},
                {"name": "@untrusted", "value": untrusted},
            ],
            partition_key=tenant_id,
        )
        removed = 0
        async for item in stale:
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            try:
                await self._observed.delete_item(item=item_id, partition_key=tenant_id)
                removed += 1
            except exceptions.CosmosResourceNotFoundError:
                continue
        return removed

    async def list_observed_models[T: ObservedModelEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        endpoint_id: str,
        entity_type: str,
    ) -> list[T]:
        items = self._observed.query_items(
            query=(
                "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.endpointId = @endpointId "
                "AND c.entityType = @entityType"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@endpointId", "value": endpoint_id},
                {"name": "@entityType", "value": entity_type},
            ],
            partition_key=tenant_id,
        )
        return [self._model(model_type, item) async for item in items]

    async def delete_observed_for_endpoint(self, tenant_id: str, endpoint_id: str) -> int:
        items = self._observed.query_items(
            query=(
                "SELECT c.id FROM c WHERE c.tenantId = @tenantId AND c.endpointId = @endpointId"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@endpointId", "value": endpoint_id},
            ],
            partition_key=tenant_id,
        )
        removed = 0
        async for item in items:
            item_id = item.get("id")
            if not isinstance(item_id, str):
                continue
            try:
                await self._observed.delete_item(item=item_id, partition_key=tenant_id)
                removed += 1
            except exceptions.CosmosResourceNotFoundError:
                continue
        return removed
