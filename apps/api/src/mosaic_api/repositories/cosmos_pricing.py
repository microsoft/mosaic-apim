from collections.abc import Sequence
from typing import Any

from azure.cosmos import exceptions

from mosaic_api.billing_pricing import (
    BillingAcquisitionRun,
    BillingConnection,
    BillingEvidenceChunk,
    BillingPublication,
)
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.pricing import EndpointPricing, PriceVersion, endpoint_pricing_id
from mosaic_api.repositories.cosmos import CosmosRepositoryBase

ENDPOINT_PRICING_CHANGED = "Pricing for this endpoint changed; reload it and try again"
BILLING_CONNECTION_CHANGED = "The billing connection changed; reload it and try again"
BILLING_RUN_CHANGED = "The billing acquisition run changed; reload it and try again"


class CosmosPricingRepository(CosmosRepositoryBase):
    """Price versions and endpoint pricing facts in ``desired-state``, each with its audit event.

    A price version is created in one transactional batch with its audit event and never
    replaced. Endpoint pricing is replaced only over the version that was read, so two
    administrators saving at once can't silently undo each other.
    """

    async def list_price_versions(self, tenant_id: str) -> list[PriceVersion]:
        items = await self._query(PriceVersion, tenant_id, "priceVersion")
        return sorted(items, key=lambda item: (item.created_at, item.id))

    async def create_price_version(
        self, version: PriceVersion, audit_event: AuditEvent
    ) -> PriceVersion:
        await self._mutate(
            version,
            None,
            audit_event,
            "create",
            conflict_message="That price version already exists",
        )
        return version

    async def list_endpoint_pricing(self, tenant_id: str) -> list[EndpointPricing]:
        return await self._query(EndpointPricing, tenant_id, "endpointPricing")

    async def get_endpoint_pricing(
        self, tenant_id: str, endpoint_id: str
    ) -> EndpointPricing | None:
        return await self._read(
            EndpointPricing, tenant_id, endpoint_pricing_id(tenant_id, endpoint_id)
        )

    async def save_endpoint_pricing(
        self, settings: EndpointPricing, audit_event: AuditEvent
    ) -> EndpointPricing:
        try:
            await self._mutate(
                settings,
                None,
                audit_event,
                "replace" if settings.etag else "create",
                conflict_message=ENDPOINT_PRICING_CHANGED,
            )
        except exceptions.CosmosResourceExistsError as exc:
            raise ConflictError(ENDPOINT_PRICING_CHANGED) from exc
        return settings

    async def list_billing_connections(self, tenant_id: str) -> list[BillingConnection]:
        items = await self._query(BillingConnection, tenant_id, "billingConnection")
        return sorted(items, key=lambda item: (item.name.casefold(), item.id))

    async def get_billing_connection(
        self, tenant_id: str, connection_id: str
    ) -> BillingConnection | None:
        return await self._read(BillingConnection, tenant_id, connection_id)

    async def create_billing_connection(
        self, connection: BillingConnection, audit_event: AuditEvent
    ) -> BillingConnection:
        await self._mutate(
            connection,
            None,
            audit_event,
            "create",
            conflict_message="A billing connection with this ID already exists",
        )
        return await self.get_billing_connection(connection.tenant_id, connection.id) or connection

    async def save_billing_connection(
        self, connection: BillingConnection, audit_event: AuditEvent
    ) -> BillingConnection:
        if not connection.etag:
            raise ConflictError(BILLING_CONNECTION_CHANGED)
        await self._mutate(
            connection,
            None,
            audit_event,
            "replace",
            conflict_message=BILLING_CONNECTION_CHANGED,
        )
        return await self.get_billing_connection(connection.tenant_id, connection.id) or connection

    async def delete_billing_connection(
        self, connection: BillingConnection, audit_event: AuditEvent
    ) -> None:
        if not connection.etag:
            raise ConflictError(BILLING_CONNECTION_CHANGED)
        await self._mutate(
            connection,
            connection.id,
            audit_event,
            "delete",
            conflict_message=BILLING_CONNECTION_CHANGED,
        )

    async def list_billing_runs(
        self, tenant_id: str, *, connection_id: str | None = None
    ) -> list[BillingAcquisitionRun]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if connection_id is not None:
            extra = " AND c.connectionId = @connectionId"
            parameters = [{"name": "@connectionId", "value": connection_id}]
        items = await self._query(
            BillingAcquisitionRun, tenant_id, "billingAcquisitionRun", extra, parameters
        )
        return sorted(items, key=lambda item: (item.started_at, item.id), reverse=True)

    async def get_billing_run(
        self, tenant_id: str, run_id: str
    ) -> BillingAcquisitionRun | None:
        return await self._read(BillingAcquisitionRun, tenant_id, run_id)

    async def create_billing_run(
        self, run: BillingAcquisitionRun, audit_event: AuditEvent
    ) -> BillingAcquisitionRun:
        await self._mutate(
            run,
            None,
            audit_event,
            "create",
            conflict_message="That billing acquisition run already exists",
        )
        return await self.get_billing_run(run.tenant_id, run.id) or run

    async def save_billing_run(
        self, run: BillingAcquisitionRun, audit_event: AuditEvent
    ) -> BillingAcquisitionRun:
        if not run.etag:
            raise ConflictError(BILLING_RUN_CHANGED)
        await self._mutate(
            run,
            None,
            audit_event,
            "replace",
            conflict_message=BILLING_RUN_CHANGED,
        )
        return await self.get_billing_run(run.tenant_id, run.id) or run

    async def save_billing_evidence_chunks(
        self, tenant_id: str, run_id: str, chunks: Sequence[BillingEvidenceChunk]
    ) -> None:
        for chunk in chunks:
            if chunk.tenant_id != tenant_id or chunk.run_id != run_id:
                raise ValueError("Evidence chunk tenant_id and run_id must match the target run")
        existing = await self.list_billing_evidence_chunks(tenant_id, run_id)
        incoming_ids = {chunk.id for chunk in chunks}
        for stale in existing:
            if stale.id not in incoming_ids:
                try:
                    await self._desired.delete_item(item=stale.id, partition_key=tenant_id)
                except exceptions.CosmosResourceNotFoundError:
                    pass
        for chunk in chunks:
            document = self._document(chunk)
            try:
                await self._desired.create_item(document)
            except exceptions.CosmosResourceExistsError:
                await self._desired.replace_item(item=chunk.id, body=document)

    async def list_billing_evidence_chunks(
        self, tenant_id: str, run_id: str
    ) -> list[BillingEvidenceChunk]:
        items = await self._query(
            BillingEvidenceChunk,
            tenant_id,
            "billingEvidenceChunk",
            " AND c.runId = @runId",
            [{"name": "@runId", "value": run_id}],
        )
        return sorted(items, key=lambda item: (item.chunk_index, item.id))

    async def list_billing_publications(self, tenant_id: str) -> list[BillingPublication]:
        items = await self._query(BillingPublication, tenant_id, "billingPublication")
        return sorted(items, key=lambda item: (item.published_at, item.id))

    async def get_billing_publication(
        self, tenant_id: str, publication_id: str
    ) -> BillingPublication | None:
        return await self._read(BillingPublication, tenant_id, publication_id)

    async def create_prepared_price_versions(self, versions: Sequence[PriceVersion]) -> None:
        for version in versions:
            if not version.publication_id:
                raise ValueError("Prepared price versions must specify a publication_id")
        for version in versions:
            try:
                await self._desired.create_item(self._document(version))
            except exceptions.CosmosResourceExistsError:
                pass

    async def create_billing_publication(
        self,
        publication: BillingPublication,
        run: BillingAcquisitionRun,
        audit_event: AuditEvent,
    ) -> BillingPublication:
        if publication.tenant_id != run.tenant_id or publication.run_id != run.id:
            raise ValueError("Publication must match the target billing acquisition run")
        if not run.etag:
            raise ConflictError(BILLING_RUN_CHANGED)
        await self._execute_batch(
            [
                ("create", (self._document(publication),)),
                self._replace_operation(run),
                ("create", (self._document(audit_event),)),
            ],
            audit_event,
            conflict_message=BILLING_RUN_CHANGED,
        )
        return (
            await self.get_billing_publication(publication.tenant_id, publication.id)
            or publication
        )
