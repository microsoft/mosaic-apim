from collections.abc import Sequence
from uuid import uuid4

from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingEvidenceChunk,
    BillingPublication,
)
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.pricing import EndpointPricing, PriceVersion

ENDPOINT_PRICING_CHANGED = "Pricing for this endpoint changed; reload it and try again"
BILLING_CONNECTION_CHANGED = "The billing connection changed; reload it and try again"
BILLING_RUN_CHANGED = "The billing acquisition run changed; reload it and try again"


class InMemoryPricingRepository:
    """Administrator-authored prices for local runs and tests. Every read returns a copy."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], PriceVersion] = {}
        self.endpoints: dict[tuple[str, str], EndpointPricing] = {}
        self.billing_connections: dict[tuple[str, str], BillingConnection] = {}
        self.billing_runs: dict[tuple[str, str], BillingAcquisitionRun] = {}
        self.billing_evidence_chunks: dict[tuple[str, str], BillingEvidenceChunk] = {}
        self.billing_publications: dict[tuple[str, str], BillingPublication] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def list_price_versions(self, tenant_id: str) -> list[PriceVersion]:
        return [
            version.model_copy(deep=True)
            for (tenant, _), version in sorted(self.versions.items())
            if tenant == tenant_id
        ]

    async def create_price_version(
        self, version: PriceVersion, audit_event: AuditEvent
    ) -> PriceVersion:
        key = (version.tenant_id, version.id)
        if key in self.versions:
            raise ConflictError("That price version already exists")
        stored = version.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.versions[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def list_endpoint_pricing(self, tenant_id: str) -> list[EndpointPricing]:
        return [
            settings.model_copy(deep=True)
            for (tenant, _), settings in sorted(self.endpoints.items())
            if tenant == tenant_id
        ]

    async def get_endpoint_pricing(
        self, tenant_id: str, endpoint_id: str
    ) -> EndpointPricing | None:
        settings = self.endpoints.get((tenant_id, endpoint_id))
        return None if settings is None else settings.model_copy(deep=True)

    async def save_endpoint_pricing(
        self, settings: EndpointPricing, audit_event: AuditEvent
    ) -> EndpointPricing:
        key = (settings.tenant_id, settings.endpoint_id)
        current = self.endpoints.get(key)
        if (current is None) != (settings.etag is None) or (
            current is not None and current.etag != settings.etag
        ):
            raise ConflictError(ENDPOINT_PRICING_CHANGED)
        stored = settings.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.endpoints[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def list_billing_connections(self, tenant_id: str) -> list[BillingConnection]:
        items = [
            connection.model_copy(deep=True)
            for (tenant, _), connection in self.billing_connections.items()
            if tenant == tenant_id
        ]
        return sorted(items, key=lambda item: (item.name.casefold(), item.id))

    async def get_billing_connection(
        self, tenant_id: str, connection_id: str
    ) -> BillingConnection | None:
        connection = self.billing_connections.get((tenant_id, connection_id))
        return None if connection is None else connection.model_copy(deep=True)

    async def create_billing_connection(
        self, connection: BillingConnection, audit_event: AuditEvent
    ) -> BillingConnection:
        key = (connection.tenant_id, connection.id)
        if key in self.billing_connections:
            raise ConflictError("A billing connection with this ID already exists")
        stored = connection.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.billing_connections[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def save_billing_connection(
        self, connection: BillingConnection, audit_event: AuditEvent
    ) -> BillingConnection:
        key = (connection.tenant_id, connection.id)
        current = self.billing_connections.get(key)
        if not connection.etag or current is None or current.etag != connection.etag:
            raise ConflictError(BILLING_CONNECTION_CHANGED)
        stored = connection.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.billing_connections[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def delete_billing_connection(
        self, connection: BillingConnection, audit_event: AuditEvent
    ) -> None:
        key = (connection.tenant_id, connection.id)
        current = self.billing_connections.get(key)
        if not connection.etag or current is None or current.etag != connection.etag:
            raise ConflictError(BILLING_CONNECTION_CHANGED)
        del self.billing_connections[key]
        self.audit_events[audit_event.id] = audit_event

    async def list_billing_runs(
        self, tenant_id: str, *, connection_id: str | None = None
    ) -> list[BillingAcquisitionRun]:
        items = [
            run.model_copy(deep=True)
            for (tenant, _), run in self.billing_runs.items()
            if tenant == tenant_id
            and (connection_id is None or run.connection_id == connection_id)
        ]
        return sorted(items, key=lambda item: (item.started_at, item.id), reverse=True)

    async def get_billing_run(
        self, tenant_id: str, run_id: str
    ) -> BillingAcquisitionRun | None:
        run = self.billing_runs.get((tenant_id, run_id))
        return None if run is None else run.model_copy(deep=True)

    async def create_billing_run(
        self, run: BillingAcquisitionRun, audit_event: AuditEvent
    ) -> BillingAcquisitionRun:
        key = (run.tenant_id, run.id)
        if key in self.billing_runs:
            raise ConflictError("That billing acquisition run already exists")
        stored = run.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.billing_runs[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def save_billing_run(
        self, run: BillingAcquisitionRun, audit_event: AuditEvent
    ) -> BillingAcquisitionRun:
        key = (run.tenant_id, run.id)
        current = self.billing_runs.get(key)
        if not run.etag or current is None or current.etag != run.etag:
            raise ConflictError(BILLING_RUN_CHANGED)
        stored = run.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.billing_runs[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def save_billing_evidence_chunks(
        self, tenant_id: str, run_id: str, chunks: Sequence[BillingEvidenceChunk]
    ) -> None:
        for chunk in chunks:
            if chunk.tenant_id != tenant_id or chunk.run_id != run_id:
                raise ValueError("Evidence chunk tenant_id and run_id must match the target run")
        incoming_ids = {chunk.id for chunk in chunks}
        stale_keys = [
            key
            for key, stored in self.billing_evidence_chunks.items()
            if stored.tenant_id == tenant_id
            and stored.run_id == run_id
            and stored.id not in incoming_ids
        ]
        for key in stale_keys:
            del self.billing_evidence_chunks[key]
        for chunk in chunks:
            self.billing_evidence_chunks[(tenant_id, chunk.id)] = chunk.model_copy(
                update={"etag": uuid4().hex}, deep=True
            )

    async def list_billing_evidence_chunks(
        self, tenant_id: str, run_id: str
    ) -> list[BillingEvidenceChunk]:
        items = [
            chunk.model_copy(deep=True)
            for (tenant, _), chunk in self.billing_evidence_chunks.items()
            if tenant == tenant_id and chunk.run_id == run_id
        ]
        return sorted(items, key=lambda item: (item.chunk_index, item.id))

    async def list_billing_publications(self, tenant_id: str) -> list[BillingPublication]:
        items = [
            publication.model_copy(deep=True)
            for (tenant, _), publication in self.billing_publications.items()
            if tenant == tenant_id
        ]
        return sorted(items, key=lambda item: (item.published_at, item.id))

    async def get_billing_publication(
        self, tenant_id: str, publication_id: str
    ) -> BillingPublication | None:
        publication = self.billing_publications.get((tenant_id, publication_id))
        return None if publication is None else publication.model_copy(deep=True)

    async def create_prepared_price_versions(self, versions: Sequence[PriceVersion]) -> None:
        for version in versions:
            if not version.publication_id:
                raise ValueError("Prepared price versions must specify a publication_id")
        for version in versions:
            key = (version.tenant_id, version.id)
            if key not in self.versions:
                self.versions[key] = version.model_copy(update={"etag": uuid4().hex}, deep=True)

    async def create_billing_publication(
        self,
        publication: BillingPublication,
        run: BillingAcquisitionRun,
        audit_event: AuditEvent,
    ) -> BillingPublication:
        if publication.tenant_id != run.tenant_id or publication.run_id != run.id:
            raise ValueError("Publication must match the target billing acquisition run")
        run_key = (run.tenant_id, run.id)
        pub_key = (publication.tenant_id, publication.id)
        current_run = self.billing_runs.get(run_key)
        if (
            not run.etag
            or current_run is None
            or current_run.etag != run.etag
            or pub_key in self.billing_publications
        ):
            raise ConflictError(BILLING_RUN_CHANGED)
        stored_run = run.model_copy(update={"etag": uuid4().hex}, deep=True)
        stored_pub = publication.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.billing_runs[run_key] = stored_run
        self.billing_publications[pub_key] = stored_pub
        self.audit_events[audit_event.id] = audit_event
        return stored_pub.model_copy(deep=True)
