"""In-memory twin of :mod:`mosaic_api.repositories.cosmos_endpoint_state`.

Explicit local/test persistence. Never selected in Azure environments.
"""

from operator import attrgetter
from typing import Any

from mosaic_api.domain import AuditEvent, CredentialReference, GatewaySyncStatus
from mosaic_api.observed import ObservedEndpointEntity


class InMemoryEndpointStateBase:
    """Endpoint-scoped observed state, sync runs, credentials, and the audit log."""

    def __init__(self) -> None:
        self.credentials: dict[str, CredentialReference] = {}
        self.sync_runs: dict[str, Any] = {}
        self.observed: dict[str, ObservedEndpointEntity] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

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

    def _runs_for_endpoint(self, tenant_id: str, endpoint_id: str, limit: int) -> list[Any]:
        runs = [
            run
            for run in self.sync_runs.values()
            if run.tenant_id == tenant_id and run.endpoint_id == endpoint_id
        ]
        runs.sort(key=attrgetter("started_at"), reverse=True)
        return runs[:limit]

    def _unfinished_runs(self, tenant_id: str) -> list[Any]:
        return [
            run
            for run in self.sync_runs.values()
            if run.tenant_id == tenant_id and run.status == GatewaySyncStatus.RUNNING
        ]

    def _delete_runs_for_endpoint(self, tenant_id: str, endpoint_id: str) -> None:
        for run_id in [run.id for run in self._runs_for_endpoint(tenant_id, endpoint_id, 10_000)]:
            self.sync_runs.pop(run_id, None)

    async def replace_observed_for_endpoint(
        self,
        tenant_id: str,
        endpoint_id: str,
        entities: list[ObservedEndpointEntity],
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

    async def list_observed_for_endpoint[T: ObservedEndpointEntity](
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
