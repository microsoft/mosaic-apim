"""Observed state and sync runs scoped to a *registered endpoint*, shared by Cosmos repositories.

Model endpoints and MCP endpoints ask Cosmos the same three questions — replace this endpoint's
observed documents, list them by type, delete them all — and the sweep already keys on
``endpointId``. ADR 0006 said a third observed scope should force a real generalisation rather
than a third copy; this is it, and because the key was already right it needed no migration.
"""

from operator import attrgetter
from typing import Any

from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient
from pydantic import BaseModel

from mosaic_api.domain import GatewaySyncStatus
from mosaic_api.observed import ObservedEndpointEntity
from mosaic_api.repositories.cosmos import CosmosRepositoryBase


class CosmosEndpointStateBase(CosmosRepositoryBase):
    """Adds the sync-run and observed-state containers to the shared Cosmos plumbing."""

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

    async def replace_observed_for_endpoint(
        self,
        tenant_id: str,
        endpoint_id: str,
        entities: list[ObservedEndpointEntity],
        snapshot_id: str,
        incomplete_types: set[str] | None = None,
    ) -> int:
        for entity in entities:
            await self._observed.upsert_item(self._document(entity))
        # A section MOSAIC failed to read looks empty in this snapshot. Sweeping those types would
        # turn "could not read" into "does not exist", so they keep their previous documents until
        # a clean sync supersedes them.
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
        return await self._purge(stale, tenant_id)

    async def list_observed_for_endpoint[T: ObservedEndpointEntity](
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
        return await self._purge(items, tenant_id)

    async def _purge(self, items: Any, tenant_id: str) -> int:
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

    async def _save_run(self, run: BaseModel) -> None:
        await self._sync.upsert_item(self._document(run))

    async def _read_run[R: BaseModel](
        self, run_type: type[R], tenant_id: str, run_id: str
    ) -> R | None:
        try:
            item = await self._sync.read_item(item=run_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        run = self._model(run_type, item)
        return run if getattr(run, "tenant_id", None) == tenant_id else None

    async def _query_runs[R: BaseModel](
        self,
        run_type: type[R],
        entity_type: str,
        tenant_id: str,
        extra: str,
        parameters: list[dict[str, Any]],
    ) -> list[R]:
        query = "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType"
        query += extra
        items = self._sync.query_items(
            query=query,
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": entity_type},
                *parameters,
            ],
            partition_key=tenant_id,
        )
        return [self._model(run_type, item) async for item in items]

    async def _runs_for_endpoint[R: BaseModel](
        self, run_type: type[R], entity_type: str, tenant_id: str, endpoint_id: str, limit: int
    ) -> list[R]:
        runs = await self._query_runs(
            run_type,
            entity_type,
            tenant_id,
            " AND c.endpointId = @endpointId",
            [{"name": "@endpointId", "value": endpoint_id}],
        )
        runs.sort(key=attrgetter("started_at"), reverse=True)
        return runs[:limit]

    async def _unfinished_runs[R: BaseModel](
        self, run_type: type[R], entity_type: str, tenant_id: str
    ) -> list[R]:
        return await self._query_runs(
            run_type,
            entity_type,
            tenant_id,
            " AND c.status = @status",
            [{"name": "@status", "value": GatewaySyncStatus.RUNNING.value}],
        )

    async def _delete_runs_for_endpoint[R: BaseModel](
        self, run_type: type[R], entity_type: str, tenant_id: str, endpoint_id: str
    ) -> None:
        for run in await self._runs_for_endpoint(
            run_type, entity_type, tenant_id, endpoint_id, 1000
        ):
            run_id = getattr(run, "id", None)
            if not isinstance(run_id, str):
                continue
            try:
                await self._sync.delete_item(item=run_id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                continue
