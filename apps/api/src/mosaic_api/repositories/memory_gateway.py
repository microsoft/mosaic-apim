from datetime import datetime, timedelta

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
    utc_now,
)
from mosaic_api.errors import ConflictError
from mosaic_api.model_pools import ModelPool
from mosaic_api.observed import ObservedEntity
from mosaic_api.repositories.observation_writes import GATEWAY_AUTHORED_FIELDS, merge_observation

OBSERVATION_WRITE_ATTEMPTS = 3


class InMemoryGatewayRepository:
    """Explicit local/test persistence. Never selected in Azure environments."""

    def __init__(self) -> None:
        self.gateways: dict[str, Gateway] = {}
        self.sync_runs: dict[str, GatewaySyncRun] = {}
        self.observed: dict[str, ObservedEntity] = {}
        self.model_apis: dict[str, ModelApi] = {}
        self.mcp_servers: dict[str, McpServer] = {}
        self.publications: dict[str, Publication] = {}
        self.mcp_publications: dict[str, McpPublication] = {}
        self.model_pools: dict[str, ModelPool] = {}
        self.publish_plans: dict[str, PublishPlan] = {}
        self.publish_runs: dict[str, PublishRun] = {}
        self.publication_locks: dict[tuple[str, str], str] = {}
        self.scope_leases: dict[tuple[str, str], tuple[str, datetime]] = {}
        self.audit_events: dict[str, AuditEvent] = {}
        self._gateway_versions: dict[str, int] = {}
        self._deleted_gateways: set[str] = set()

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def list_gateways(self, tenant_id: str) -> list[Gateway]:
        return sorted(
            (item for item in self.gateways.values() if item.tenant_id == tenant_id),
            key=lambda item: item.name.casefold(),
        )

    async def get_gateway(self, tenant_id: str, gateway_id: str) -> Gateway | None:
        gateway = self.gateways.get(gateway_id)
        return gateway if gateway and gateway.tenant_id == tenant_id else None

    async def find_gateway_by_resource_id(
        self, tenant_id: str, azure_resource_id: str
    ) -> Gateway | None:
        target = azure_resource_id.casefold()
        return next(
            (
                gateway
                for gateway in self.gateways.values()
                if gateway.tenant_id == tenant_id
                and gateway.azure_resource_id.casefold() == target
            ),
            None,
        )

    async def create_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> Gateway:
        if gateway.id in self.gateways:
            raise ConflictError("This API Management service is already registered")
        return await self.save_gateway(gateway, audit_event)

    async def save_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> Gateway:
        self.gateways[gateway.id] = gateway
        self._gateway_versions[gateway.id] = self._gateway_versions.get(gateway.id, 0) + 1
        self._deleted_gateways.discard(gateway.id)
        self.audit_events[audit_event.id] = audit_event
        return gateway

    async def record_gateway_state(self, gateway: Gateway) -> Gateway | None:
        for _ in range(OBSERVATION_WRITE_ATTEMPTS):
            stored = await self.get_gateway(gateway.tenant_id, gateway.id)
            if stored is None:
                if gateway.id not in self._deleted_gateways:
                    self.gateways[gateway.id] = gateway
                    self._gateway_versions[gateway.id] = 1
                    return gateway
                return None
            version = self._gateway_versions.get(gateway.id, 0)
            await self._before_observation_replace()
            if await self.get_gateway(gateway.tenant_id, gateway.id) is None:
                return None
            if self._gateway_versions.get(gateway.id, 0) != version:
                continue
            merged = merge_observation(stored, gateway, GATEWAY_AUTHORED_FIELDS)
            self.gateways[gateway.id] = merged
            self._gateway_versions[gateway.id] = version + 1
            return merged
        raise ConflictError("The gateway changed while MOSAIC was recording observations")

    async def _before_observation_replace(self) -> None:
        return None

    async def delete_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_gateway(gateway.tenant_id, gateway.id)
        for run_id in [
            run.id
            for run in self.sync_runs.values()
            if run.tenant_id == gateway.tenant_id and run.gateway_id == gateway.id
        ]:
            self.sync_runs.pop(run_id, None)
        # Adopted records describe a gateway that no longer exists, so they go with it rather than
        # lingering as intent MOSAIC can never reconcile.
        for model_api_id in [
            item.id
            for item in self.model_apis.values()
            if item.tenant_id == gateway.tenant_id and item.gateway_id == gateway.id
        ]:
            self.model_apis.pop(model_api_id, None)
        for mcp_server_id in [
            item.id
            for item in self.mcp_servers.values()
            if item.tenant_id == gateway.tenant_id and item.gateway_id == gateway.id
        ]:
            self.mcp_servers.pop(mcp_server_id, None)
        for publication_key in [
            item.id
            for item in self.publications.values()
            if item.tenant_id == gateway.tenant_id and item.gateway_id == gateway.id
        ]:
            self.publications.pop(publication_key, None)
        for publication_key in [
            item.id
            for item in self.mcp_publications.values()
            if item.tenant_id == gateway.tenant_id and item.gateway_id == gateway.id
        ]:
            self.mcp_publications.pop(publication_key, None)
        for pool_id in [
            item.id
            for item in self.model_pools.values()
            if item.tenant_id == gateway.tenant_id and item.gateway_id == gateway.id
        ]:
            self.model_pools.pop(pool_id, None)
        self.gateways.pop(gateway.id, None)
        self._gateway_versions.pop(gateway.id, None)
        self._deleted_gateways.add(gateway.id)
        self.audit_events[audit_event.id] = audit_event

    async def save_sync_run(self, run: GatewaySyncRun) -> GatewaySyncRun:
        self.sync_runs[run.id] = run
        return run

    async def get_sync_run(self, tenant_id: str, run_id: str) -> GatewaySyncRun | None:
        run = self.sync_runs.get(run_id)
        return run if run and run.tenant_id == tenant_id else None

    async def list_sync_runs(
        self, tenant_id: str, gateway_id: str, *, limit: int = 20
    ) -> list[GatewaySyncRun]:
        runs = [
            run
            for run in self.sync_runs.values()
            if run.tenant_id == tenant_id and run.gateway_id == gateway_id
        ]
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[:limit]

    async def list_unfinished_sync_runs(self, tenant_id: str) -> list[GatewaySyncRun]:
        return [
            run
            for run in self.sync_runs.values()
            if run.tenant_id == tenant_id and run.status == GatewaySyncStatus.RUNNING
        ]

    async def replace_observed(
        self,
        tenant_id: str,
        gateway_id: str,
        entities: list[ObservedEntity],
        snapshot_id: str,
        incomplete_types: set[str] | None = None,
    ) -> int:
        untrusted = incomplete_types or set()
        for entity in entities:
            self.observed[entity.id] = entity
        stale = [
            key
            for key, entity in self.observed.items()
            if entity.tenant_id == tenant_id
            and entity.gateway_id == gateway_id
            and entity.snapshot_id != snapshot_id
            and getattr(entity, "entity_type", None) not in untrusted
        ]
        for key in stale:
            self.observed.pop(key, None)
        return len(stale)

    async def list_observed[T: ObservedEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        gateway_id: str,
        entity_type: str,
    ) -> list[T]:
        return [
            entity
            for entity in self.observed.values()
            if isinstance(entity, model_type)
            and entity.tenant_id == tenant_id
            and entity.gateway_id == gateway_id
            and getattr(entity, "entity_type", None) == entity_type
        ]

    async def delete_observed_for_gateway(self, tenant_id: str, gateway_id: str) -> int:
        stale = [
            key
            for key, entity in self.observed.items()
            if entity.tenant_id == tenant_id and entity.gateway_id == gateway_id
        ]
        for key in stale:
            self.observed.pop(key, None)
        return len(stale)

    async def list_model_apis(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[ModelApi]:
        items = [
            item
            for item in self.model_apis.values()
            if item.tenant_id == tenant_id
            and (gateway_id is None or item.gateway_id == gateway_id)
        ]
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_model_api(self, tenant_id: str, model_api_id: str) -> ModelApi | None:
        item = self.model_apis.get(model_api_id)
        return item if item and item.tenant_id == tenant_id else None

    async def save_model_api(self, model_api: ModelApi, audit_event: AuditEvent) -> ModelApi:
        self.model_apis[model_api.id] = model_api
        self.audit_events[audit_event.id] = audit_event
        return model_api

    async def delete_model_api(self, model_api: ModelApi, audit_event: AuditEvent) -> None:
        self.model_apis.pop(model_api.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def list_mcp_servers(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[McpServer]:
        items = [
            item
            for item in self.mcp_servers.values()
            if item.tenant_id == tenant_id
            and (gateway_id is None or item.gateway_id == gateway_id)
        ]
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_mcp_server(self, tenant_id: str, mcp_server_id: str) -> McpServer | None:
        item = self.mcp_servers.get(mcp_server_id)
        return item if item and item.tenant_id == tenant_id else None

    async def save_mcp_server(self, mcp_server: McpServer, audit_event: AuditEvent) -> McpServer:
        self.mcp_servers[mcp_server.id] = mcp_server
        self.audit_events[audit_event.id] = audit_event
        return mcp_server

    async def delete_mcp_server(self, mcp_server: McpServer, audit_event: AuditEvent) -> None:
        self.mcp_servers.pop(mcp_server.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def list_publications(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[Publication]:
        items = [
            item
            for item in self.publications.values()
            if item.tenant_id == tenant_id
            and (gateway_id is None or item.gateway_id == gateway_id)
        ]
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_publication(self, tenant_id: str, publication_id: str) -> Publication | None:
        item = self.publications.get(publication_id)
        return item if item and item.tenant_id == tenant_id else None

    async def save_publication(
        self, publication: Publication, audit_event: AuditEvent
    ) -> Publication:
        self.publications[publication.id] = publication
        self.audit_events[audit_event.id] = audit_event
        return publication

    async def record_publication_state(self, publication: Publication) -> Publication:
        self.publications[publication.id] = publication
        return publication

    async def delete_publication(
        self, publication: Publication, audit_event: AuditEvent
    ) -> None:
        self.publications.pop(publication.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def list_mcp_publications(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[McpPublication]:
        items = [
            item
            for item in self.mcp_publications.values()
            if item.tenant_id == tenant_id
            and (gateway_id is None or item.gateway_id == gateway_id)
        ]
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_mcp_publication(
        self, tenant_id: str, publication_id: str
    ) -> McpPublication | None:
        item = self.mcp_publications.get(publication_id)
        return item if item and item.tenant_id == tenant_id else None

    async def save_mcp_publication(
        self, publication: McpPublication, audit_event: AuditEvent
    ) -> McpPublication:
        self.mcp_publications[publication.id] = publication
        self.audit_events[audit_event.id] = audit_event
        return publication

    async def record_mcp_publication_state(self, publication: McpPublication) -> McpPublication:
        self.mcp_publications[publication.id] = publication
        return publication

    async def delete_mcp_publication(
        self, publication: McpPublication, audit_event: AuditEvent
    ) -> None:
        self.mcp_publications.pop(publication.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def list_model_pools(
        self, tenant_id: str, *, gateway_id: str | None = None
    ) -> list[ModelPool]:
        items = [
            item
            for item in self.model_pools.values()
            if item.tenant_id == tenant_id
            and (gateway_id is None or item.gateway_id == gateway_id)
        ]
        return sorted(items, key=lambda item: item.display_name.casefold())

    async def get_model_pool(self, tenant_id: str, pool_id: str) -> ModelPool | None:
        item = self.model_pools.get(pool_id)
        return item if item and item.tenant_id == tenant_id else None

    async def save_model_pool(self, pool: ModelPool, audit_event: AuditEvent) -> ModelPool:
        self.model_pools[pool.id] = pool
        self.audit_events[audit_event.id] = audit_event
        return pool

    async def record_model_pool_state(self, pool: ModelPool) -> ModelPool:
        self.model_pools[pool.id] = pool
        return pool

    async def delete_model_pool(self, pool: ModelPool, audit_event: AuditEvent) -> None:
        self.model_pools.pop(pool.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def save_publish_plan(self, plan: PublishPlan) -> PublishPlan:
        self.publish_plans[plan.id] = plan.model_copy(deep=True)
        return plan

    async def get_publish_plan(self, tenant_id: str, plan_id: str) -> PublishPlan | None:
        plan = self.publish_plans.get(plan_id)
        return plan if plan and plan.tenant_id == tenant_id else None

    async def save_publish_run(self, run: PublishRun) -> PublishRun:
        self.publish_runs[run.id] = run.model_copy(deep=True)
        return run

    async def get_publish_run(self, tenant_id: str, run_id: str) -> PublishRun | None:
        run = self.publish_runs.get(run_id)
        return run if run and run.tenant_id == tenant_id else None

    async def list_publish_runs(
        self, tenant_id: str, publication_id: str, *, limit: int = 20
    ) -> list[PublishRun]:
        runs = [
            run
            for run in self.publish_runs.values()
            if run.tenant_id == tenant_id and run.publication_id == publication_id
        ]
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[:limit]

    async def list_unfinished_publish_runs(self, tenant_id: str) -> list[PublishRun]:
        return [
            run
            for run in self.publish_runs.values()
            if run.tenant_id == tenant_id and run.status == PublishRunStatus.RUNNING
        ]

    async def acquire_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        key = (tenant_id, publication_id)
        if key in self.publication_locks:
            raise ConflictError(
                "An apply or mutation is already running for this publication. "
                "Interrupted runs require explicit recovery.",
                details={"id": publication_id, "lockOwner": self.publication_locks[key]},
            )
        self.publication_locks[key] = owner_id

    async def get_publication_lock(self, tenant_id: str, publication_id: str) -> str | None:
        return self.publication_locks.get((tenant_id, publication_id))

    async def release_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        key = (tenant_id, publication_id)
        if self.publication_locks.get(key) != owner_id:
            raise ConflictError("The publication lock is no longer owned by this operation")
        del self.publication_locks[key]

    async def acquire_scope_lease(
        self, tenant_id: str, scope: str, owner_id: str, *, lease_seconds: float
    ) -> None:
        key = (tenant_id, scope)
        now = utc_now()
        current = self.scope_leases.get(key)
        if current is not None and current[1] > now:
            raise ConflictError(
                "Another change is already in progress. Try again in a moment.",
                details={"scope": scope},
            )
        self.scope_leases[key] = (owner_id, now + timedelta(seconds=lease_seconds))

    async def release_scope_lease(self, tenant_id: str, scope: str, owner_id: str) -> None:
        key = (tenant_id, scope)
        current = self.scope_leases.get(key)
        if current is not None and current[0] == owner_id:
            del self.scope_leases[key]
