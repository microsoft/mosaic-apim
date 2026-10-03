from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from azure.core import MatchConditions
from azure.cosmos import exceptions
from azure.cosmos.aio import ContainerProxy, CosmosClient

from mosaic_api.domain import (
    AuditEvent,
    Gateway,
    GatewaySyncRun,
    GatewaySyncStatus,
    McpPublication,
    McpServer,
    ModelApi,
    Publication,
    PublishPlan,
    PublishRun,
    PublishRunStatus,
    deterministic_id,
    utc_now,
)
from mosaic_api.errors import ConflictError
from mosaic_api.model_pools import MODEL_POOL_ENTITY, ModelPool
from mosaic_api.observed import ObservedEntity
from mosaic_api.repositories.cosmos import CosmosRepositoryBase
from mosaic_api.repositories.observation_writes import GATEWAY_AUTHORED_FIELDS

logger = structlog.get_logger()

OBSERVED_DELETE_BATCH = 50


class CosmosGatewayRepository(CosmosRepositoryBase):
    """Gateways and sync runs are desired state; the inventory snapshot is observed state.

    They are kept in separate containers because observed documents churn on every sync and are
    disposable, while desired state is authored by administrators and audited.
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

    async def list_gateways(self, tenant_id: str) -> list[Gateway]:
        items = await self._query(Gateway, tenant_id, "gateway")
        return sorted(items, key=lambda item: item.name.casefold())

    async def get_gateway(self, tenant_id: str, gateway_id: str) -> Gateway | None:
        return await self._read(Gateway, tenant_id, gateway_id)

    async def find_gateway_by_resource_id(
        self, tenant_id: str, azure_resource_id: str
    ) -> Gateway | None:
        items = await self._query(
            Gateway,
            tenant_id,
            "gateway",
            " AND LOWER(c.azureResourceId) = @resourceId",
            [{"name": "@resourceId", "value": azure_resource_id.casefold()}],
        )
        return items[0] if items else None

    async def create_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> Gateway:
        await self._mutate(
            gateway,
            None,
            audit_event,
            "create",
            conflict_message="This API Management service is already registered",
        )
        return gateway

    async def save_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> Gateway:
        await self._mutate(
            gateway,
            None,
            audit_event,
            "replace",
            conflict_message="The gateway changed; reload it and try again",
        )
        return gateway

    async def record_gateway_state(self, gateway: Gateway) -> Gateway | None:
        return await self._record_observation(
            Gateway,
            gateway,
            GATEWAY_AUTHORED_FIELDS,
            conflict_message="The gateway changed while MOSAIC was recording observations",
        )

    async def delete_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_gateway(gateway.tenant_id, gateway.id)
        await self._delete_sync_runs(gateway.tenant_id, gateway.id)
        await self._delete_adopted(gateway.tenant_id, gateway.id)
        await self._mutate(
            gateway,
            gateway.id,
            audit_event,
            "delete",
            conflict_message="The gateway changed; reload it and try again",
        )

    async def _delete_adopted(self, tenant_id: str, gateway_id: str) -> None:
        """Adopted records outlive a sync but not the gateway they describe."""

        adopted: list[str] = [
            item.id for item in await self.list_model_apis(tenant_id, gateway_id=gateway_id)
        ]
        adopted.extend(
            item.id for item in await self.list_mcp_servers(tenant_id, gateway_id=gateway_id)
        )
        adopted.extend(
            item.id for item in await self.list_publications(tenant_id, gateway_id=gateway_id)
        )
        adopted.extend(
            item.id
            for item in await self.list_mcp_publications(tenant_id, gateway_id=gateway_id)
        )
        adopted.extend(
            item.id for item in await self.list_model_pools(tenant_id, gateway_id=gateway_id)
        )
        for item_id in adopted:
            try:
                await self._desired.delete_item(item=item_id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                continue

    async def save_sync_run(self, run: GatewaySyncRun) -> GatewaySyncRun:
        await self._sync.upsert_item(self._document(run))
        return run

    async def get_sync_run(self, tenant_id: str, run_id: str) -> GatewaySyncRun | None:
        try:
            item = await self._sync.read_item(item=run_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        run = self._model(GatewaySyncRun, item)
        return run if run.tenant_id == tenant_id else None

    async def _query_sync_runs(
        self, tenant_id: str, extra: str, parameters: list[dict[str, Any]]
    ) -> list[GatewaySyncRun]:
        query = "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType"
        query += extra
        items = self._sync.query_items(
            query=query,
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": "gatewaySyncRun"},
                *parameters,
            ],
            partition_key=tenant_id,
        )
        return [self._model(GatewaySyncRun, item) async for item in items]

    async def list_sync_runs(
        self, tenant_id: str, gateway_id: str, *, limit: int = 20
    ) -> list[GatewaySyncRun]:
        runs = await self._query_sync_runs(
            tenant_id,
            " AND c.gatewayId = @gatewayId",
            [{"name": "@gatewayId", "value": gateway_id}],
        )
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[:limit]

    async def list_unfinished_sync_runs(self, tenant_id: str) -> list[GatewaySyncRun]:
        return await self._query_sync_runs(
            tenant_id,
            " AND c.status = @status",
            [{"name": "@status", "value": GatewaySyncStatus.RUNNING.value}],
        )

    async def _delete_sync_runs(self, tenant_id: str, gateway_id: str) -> None:
        for run in await self.list_sync_runs(tenant_id, gateway_id, limit=1000):
            try:
                await self._sync.delete_item(item=run.id, partition_key=tenant_id)
            except exceptions.CosmosResourceNotFoundError:
                continue

    async def replace_observed(
        self,
        tenant_id: str,
        gateway_id: str,
        entities: list[ObservedEntity],
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
                "SELECT c.id FROM c WHERE c.tenantId = @tenantId AND c.gatewayId = @gatewayId "
                "AND c.snapshotId != @snapshotId "
                "AND NOT ARRAY_CONTAINS(@untrusted, c.entityType)"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@gatewayId", "value": gateway_id},
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

    async def list_observed[T: ObservedEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        gateway_id: str,
        entity_type: str,
    ) -> list[T]:
        items = self._observed.query_items(
            query=(
                "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.gatewayId = @gatewayId "
                "AND c.entityType = @entityType"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@gatewayId", "value": gateway_id},
                {"name": "@entityType", "value": entity_type},
            ],
            partition_key=tenant_id,
        )
        return [self._model(model_type, item) async for item in items]

    async def delete_observed_for_gateway(self, tenant_id: str, gateway_id: str) -> int:
        items = self._observed.query_items(
            query=(
                "SELECT c.id FROM c WHERE c.tenantId = @tenantId AND c.gatewayId = @gatewayId"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@gatewayId", "value": gateway_id},
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

    @staticmethod
    def _gateway_filter(gateway_id: str | None) -> tuple[str, list[dict[str, Any]]]:
        if gateway_id is None:
            return "", []
        return " AND c.gatewayId = @gatewayId", [{"name": "@gatewayId", "value": gateway_id}]

    async def list_model_apis(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[ModelApi]:
        extra, parameters = self._gateway_filter(gateway_id)
        items = await self._query(ModelApi, tenant_id, "modelApi", extra, parameters)
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_model_api(self, tenant_id: str, model_api_id: str) -> ModelApi | None:
        return await self._read(ModelApi, tenant_id, model_api_id)

    async def save_model_api(self, model_api: ModelApi, audit_event: AuditEvent) -> ModelApi:
        await self._mutate(model_api, None, audit_event, "upsert")
        return model_api

    async def delete_model_api(self, model_api: ModelApi, audit_event: AuditEvent) -> None:
        await self._mutate(
            model_api,
            model_api.id,
            audit_event,
            "delete",
            conflict_message="The model API changed; reload it and try again",
        )

    async def list_mcp_servers(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[McpServer]:
        extra, parameters = self._gateway_filter(gateway_id)
        items = await self._query(McpServer, tenant_id, "mcpServer", extra, parameters)
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_mcp_server(self, tenant_id: str, mcp_server_id: str) -> McpServer | None:
        return await self._read(McpServer, tenant_id, mcp_server_id)

    async def save_mcp_server(self, mcp_server: McpServer, audit_event: AuditEvent) -> McpServer:
        await self._mutate(mcp_server, None, audit_event, "upsert")
        return mcp_server

    async def delete_mcp_server(self, mcp_server: McpServer, audit_event: AuditEvent) -> None:
        await self._mutate(
            mcp_server,
            mcp_server.id,
            audit_event,
            "delete",
            conflict_message="The MCP server changed; reload it and try again",
        )

    async def list_publications(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[Publication]:
        extra, parameters = self._gateway_filter(gateway_id)
        items = await self._query(Publication, tenant_id, "publication", extra, parameters)
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_publication(self, tenant_id: str, publication_id: str) -> Publication | None:
        return await self._read(Publication, tenant_id, publication_id)

    async def save_publication(
        self, publication: Publication, audit_event: AuditEvent
    ) -> Publication:
        await self._mutate(publication, None, audit_event, "upsert")
        return publication

    async def record_publication_state(self, publication: Publication) -> Publication:
        await self._desired.upsert_item(self._document(publication))
        return publication

    async def delete_publication(
        self, publication: Publication, audit_event: AuditEvent
    ) -> None:
        await self._mutate(
            publication,
            publication.id,
            audit_event,
            "delete",
            conflict_message="The publication changed; reload it and try again",
        )

    async def list_mcp_publications(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[McpPublication]:
        extra, parameters = self._gateway_filter(gateway_id)
        items = await self._query(McpPublication, tenant_id, "mcpPublication", extra, parameters)
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_mcp_publication(
        self, tenant_id: str, publication_id: str
    ) -> McpPublication | None:
        return await self._read(McpPublication, tenant_id, publication_id)

    async def save_mcp_publication(
        self, publication: McpPublication, audit_event: AuditEvent
    ) -> McpPublication:
        await self._mutate(publication, None, audit_event, "upsert")
        return publication

    async def record_mcp_publication_state(self, publication: McpPublication) -> McpPublication:
        await self._desired.upsert_item(self._document(publication))
        return publication

    async def delete_mcp_publication(
        self, publication: McpPublication, audit_event: AuditEvent
    ) -> None:
        await self._mutate(
            publication,
            publication.id,
            audit_event,
            "delete",
            conflict_message="The MCP publication changed; reload it and try again",
        )

    async def list_model_pools(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[ModelPool]:
        extra, parameters = self._gateway_filter(gateway_id)
        items = await self._query(ModelPool, tenant_id, MODEL_POOL_ENTITY, extra, parameters)
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_model_pool(self, tenant_id: str, pool_id: str) -> ModelPool | None:
        return await self._read(ModelPool, tenant_id, pool_id)

    async def save_model_pool(self, pool: ModelPool, audit_event: AuditEvent) -> ModelPool:
        await self._mutate(pool, None, audit_event, "upsert")
        return pool

    async def record_model_pool_state(self, pool: ModelPool) -> ModelPool:
        await self._desired.upsert_item(self._document(pool))
        return pool

    async def delete_model_pool(self, pool: ModelPool, audit_event: AuditEvent) -> None:
        await self._mutate(
            pool,
            pool.id,
            audit_event,
            "delete",
            conflict_message="The model pool changed; reload it and try again",
        )

    # Plans and runs are reconciliation records rather than administrator-authored intent, so they
    # live in the sync-operations container alongside gateway sync runs. See ADR 0002.
    async def save_publish_plan(self, plan: PublishPlan) -> PublishPlan:
        await self._sync.upsert_item(self._document(plan))
        return plan

    async def get_publish_plan(self, tenant_id: str, plan_id: str) -> PublishPlan | None:
        try:
            item = await self._sync.read_item(item=plan_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        plan = self._model(PublishPlan, item)
        return plan if plan.tenant_id == tenant_id else None

    async def save_publish_run(self, run: PublishRun) -> PublishRun:
        await self._sync.upsert_item(self._document(run))
        return run

    async def get_publish_run(self, tenant_id: str, run_id: str) -> PublishRun | None:
        try:
            item = await self._sync.read_item(item=run_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        run = self._model(PublishRun, item)
        return run if run.tenant_id == tenant_id else None

    async def _query_publish_runs(
        self, tenant_id: str, extra: str, parameters: list[dict[str, Any]]
    ) -> list[PublishRun]:
        query = "SELECT * FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType"
        query += extra
        items = self._sync.query_items(
            query=query,
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": "publishRun"},
                *parameters,
            ],
            partition_key=tenant_id,
        )
        return [self._model(PublishRun, item) async for item in items]

    async def list_publish_runs(
        self, tenant_id: str, publication_id: str, *, limit: int = 20
    ) -> list[PublishRun]:
        runs = await self._query_publish_runs(
            tenant_id,
            " AND c.publicationId = @publicationId",
            [{"name": "@publicationId", "value": publication_id}],
        )
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[:limit]

    async def list_unfinished_publish_runs(self, tenant_id: str) -> list[PublishRun]:
        return await self._query_publish_runs(
            tenant_id,
            " AND c.status = @status",
            [{"name": "@status", "value": PublishRunStatus.RUNNING.value}],
        )

    async def acquire_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        # No TTL: ARM operations can outlive a process, and APIM has no fencing-token facility.
        # Expiring this document would let an unfenced old writer resume over a newer apply.
        try:
            await self._desired.create_item(
                {
                    "id": deterministic_id("publicationLock", tenant_id, publication_id),
                    "tenantId": tenant_id,
                    "entityType": "publicationLock",
                    "publicationId": publication_id,
                    "ownerId": owner_id,
                    "createdAt": utc_now().isoformat(),
                    "ttl": -1,
                }
            )
        except exceptions.CosmosResourceExistsError as error:
            owner = await self.get_publication_lock(tenant_id, publication_id)
            raise ConflictError(
                "An apply or mutation is already running for this publication. "
                "Interrupted runs require explicit recovery.",
                details={"id": publication_id, "lockOwner": owner},
            ) from error

    async def get_publication_lock(self, tenant_id: str, publication_id: str) -> str | None:
        try:
            item = await self._desired.read_item(
                item=deterministic_id("publicationLock", tenant_id, publication_id),
                partition_key=tenant_id,
            )
        except exceptions.CosmosResourceNotFoundError:
            return None
        owner = item.get("ownerId")
        if item.get("tenantId") != tenant_id or not isinstance(owner, str):
            raise ConflictError("The publication lock is invalid; operator recovery is required")
        return owner

    async def release_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        lock_id = deterministic_id("publicationLock", tenant_id, publication_id)
        try:
            item = await self._desired.read_item(item=lock_id, partition_key=tenant_id)
            if item.get("ownerId") != owner_id or item.get("tenantId") != tenant_id:
                raise ConflictError("The publication lock is no longer owned by this operation")
            await self._desired.delete_item(
                item=lock_id,
                partition_key=tenant_id,
                etag=item["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except (
            exceptions.CosmosResourceNotFoundError,
            exceptions.CosmosAccessConditionFailedError,
        ) as error:
            raise ConflictError(
                "The publication lock changed; operator recovery is required"
            ) from error

    async def acquire_scope_lease(
        self, tenant_id: str, scope: str, owner_id: str, *, lease_seconds: float
    ) -> None:
        lease_id = deterministic_id("scopeLease", tenant_id, scope)
        now = utc_now()
        document = {
            "id": lease_id,
            "tenantId": tenant_id,
            "entityType": "scopeLease",
            "scope": scope,
            "ownerId": owner_id,
            "createdAt": now.isoformat(),
            "expiresAt": (now + timedelta(seconds=lease_seconds)).isoformat(),
        }
        busy = ConflictError(
            "Another change is already in progress. Try again in a moment.",
            details={"scope": scope},
        )
        try:
            await self._desired.create_item(document)
            return
        except exceptions.CosmosResourceExistsError:
            pass
        try:
            item = await self._desired.read_item(item=lease_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError as error:
            # Released between the create and the read; the next attempt can take it.
            raise busy from error
        if _lease_expiry(item) > now:
            raise busy
        # Take over an expired lease only if nobody else took it first.
        try:
            await self._desired.replace_item(
                item=lease_id,
                body=document,
                etag=item["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except (
            exceptions.CosmosResourceNotFoundError,
            exceptions.CosmosAccessConditionFailedError,
        ) as error:
            raise busy from error

    async def release_scope_lease(self, tenant_id: str, scope: str, owner_id: str) -> None:
        lease_id = deterministic_id("scopeLease", tenant_id, scope)
        try:
            item = await self._desired.read_item(item=lease_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return
        if item.get("ownerId") != owner_id or item.get("tenantId") != tenant_id:
            logger.warning("scope_lease_lost", scope=scope, owner_id=owner_id)
            return
        try:
            await self._desired.delete_item(
                item=lease_id,
                partition_key=tenant_id,
                etag=item["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except (
            exceptions.CosmosResourceNotFoundError,
            exceptions.CosmosAccessConditionFailedError,
        ):
            logger.warning("scope_lease_lost", scope=scope, owner_id=owner_id)


def _lease_expiry(item: dict[str, Any]) -> datetime:
    """A lease document's expiry; an unreadable one counts as expired rather than held forever."""

    value = item.get("expiresAt")
    try:
        expiry = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        expiry = None
    if expiry is None or expiry.tzinfo is None:
        logger.warning("scope_lease_unreadable", lease_id=item.get("id"))
        return datetime.min.replace(tzinfo=UTC)
    return expiry
