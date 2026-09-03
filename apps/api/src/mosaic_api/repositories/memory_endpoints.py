from mosaic_api.domain import (
    AuditEvent,
    CredentialReference,
    GatewaySyncStatus,
    ModelEndpoint,
    ModelEndpointSyncRun,
)
from mosaic_api.errors import ConflictError
from mosaic_api.observed import ObservedModelEntity


class InMemoryModelEndpointRepository:
    """Explicit local/test persistence. Never selected in Azure environments."""

    def __init__(self) -> None:
        self.endpoints: dict[str, ModelEndpoint] = {}
        self.credentials: dict[str, CredentialReference] = {}
        self.sync_runs: dict[str, ModelEndpointSyncRun] = {}
        self.observed: dict[str, ObservedModelEntity] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

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
        self.audit_events[audit_event.id] = audit_event
        return endpoint

    async def record_endpoint_state(self, endpoint: ModelEndpoint) -> ModelEndpoint:
        self.endpoints[endpoint.id] = endpoint
        return endpoint

    async def delete_endpoint(self, endpoint: ModelEndpoint, audit_event: AuditEvent) -> None:
        await self.delete_observed_for_endpoint(endpoint.tenant_id, endpoint.id)
        for run_id in [
            run.id
            for run in self.sync_runs.values()
            if run.tenant_id == endpoint.tenant_id and run.endpoint_id == endpoint.id
        ]:
            self.sync_runs.pop(run_id, None)
        self.endpoints.pop(endpoint.id, None)
        self.audit_events[audit_event.id] = audit_event

    async def get_credential(
        self, tenant_id: str, credential_id: str
    ) -> CredentialReference | None:
        credential = self.credentials.get(credential_id)
        return credential if credential and credential.tenant_id == tenant_id else None

    async def save_credential(
        self, credential: CredentialReference, audit_event: AuditEvent
    ) -> CredentialReference:
        self.credentials[credential.id] = credential
        self.audit_events[audit_event.id] = audit_event
        return credential

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
        runs = [
            run
            for run in self.sync_runs.values()
            if run.tenant_id == tenant_id and run.endpoint_id == endpoint_id
        ]
        runs.sort(key=lambda run: run.started_at, reverse=True)
        return runs[:limit]

    async def list_unfinished_endpoint_sync_runs(
        self, tenant_id: str
    ) -> list[ModelEndpointSyncRun]:
        return [
            run
            for run in self.sync_runs.values()
            if run.tenant_id == tenant_id and run.status == GatewaySyncStatus.RUNNING
        ]

    async def replace_observed_models(
        self,
        tenant_id: str,
        endpoint_id: str,
        entities: list[ObservedModelEntity],
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
            and entity.endpoint_id == endpoint_id
            and entity.snapshot_id != snapshot_id
            and getattr(entity, "entity_type", None) not in untrusted
        ]
        for key in stale:
            self.observed.pop(key, None)
        return len(stale)

    async def list_observed_models[T: ObservedModelEntity](
        self,
        model_type: type[T],
        tenant_id: str,
        endpoint_id: str,
        entity_type: str,
    ) -> list[T]:
        return [
            entity
            for entity in self.observed.values()
            if isinstance(entity, model_type)
            and entity.tenant_id == tenant_id
            and entity.endpoint_id == endpoint_id
            and getattr(entity, "entity_type", None) == entity_type
        ]

    async def delete_observed_for_endpoint(self, tenant_id: str, endpoint_id: str) -> int:
        stale = [
            key
            for key, entity in self.observed.items()
            if entity.tenant_id == tenant_id and entity.endpoint_id == endpoint_id
        ]
        for key in stale:
            self.observed.pop(key, None)
        return len(stale)
