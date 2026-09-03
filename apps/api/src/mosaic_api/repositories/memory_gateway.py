from mosaic_api.domain import AuditEvent, Gateway, GatewaySyncRun, GatewaySyncStatus
from mosaic_api.errors import ConflictError
from mosaic_api.observed import ObservedEntity


class InMemoryGatewayRepository:
    """Explicit local/test persistence. Never selected in Azure environments."""

    def __init__(self) -> None:
        self.gateways: dict[str, Gateway] = {}
        self.sync_runs: dict[str, GatewaySyncRun] = {}
        self.observed: dict[str, ObservedEntity] = {}
        self.audit_events: dict[str, AuditEvent] = {}

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
        self.audit_events[audit_event.id] = audit_event
        return gateway

    async def record_gateway_state(self, gateway: Gateway) -> Gateway:
        self.gateways[gateway.id] = gateway
        return gateway

    async def delete_gateway(self, gateway: Gateway, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_gateway(gateway.tenant_id, gateway.id)
        for run_id in [
            run.id
            for run in self.sync_runs.values()
            if run.tenant_id == gateway.tenant_id and run.gateway_id == gateway.id
        ]:
            self.sync_runs.pop(run_id, None)
        self.gateways.pop(gateway.id, None)
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
