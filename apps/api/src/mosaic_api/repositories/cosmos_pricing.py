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
from mosaic_api.pricing_catalog import (
    CatalogActivation,
    CatalogChunk,
    CatalogDraft,
    CatalogHead,
    CatalogManifest,
    catalog_head_id,
    content_hash,
)
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

    async def get_catalog_head(self, tenant_id: str) -> CatalogHead | None:
        return await self._read(CatalogHead, tenant_id, catalog_head_id(tenant_id))

    async def has_catalog_activation(self, tenant_id: str) -> bool:
        items = self._desired.query_items(
            query=(
                "SELECT TOP 1 c.id FROM c WHERE c.tenantId = @tenantId "
                "AND c.entityType = @entityType"
            ),
            parameters=[
                {"name": "@tenantId", "value": tenant_id},
                {"name": "@entityType", "value": "publicCatalogActivation"},
            ],
            partition_key=tenant_id,
        )
        async for _ in items:
            return True
        return False

    async def get_catalog_manifest(
        self, tenant_id: str, manifest_id: str
    ) -> CatalogManifest | None:
        return await self._read(CatalogManifest, tenant_id, manifest_id)

    async def get_catalog_chunk(self, tenant_id: str, chunk_id: str) -> CatalogChunk | None:
        return await self._read(CatalogChunk, tenant_id, chunk_id)

    async def prepare_catalog(
        self, manifest: CatalogManifest, chunks: Sequence[CatalogChunk]
    ) -> None:
        for chunk in chunks:
            if chunk.tenant_id != manifest.tenant_id or content_hash(chunk.content) != chunk.digest:
                raise ValueError("Catalog chunk does not match its tenant or content hash")
            try:
                await self._desired.create_item(self._document(chunk))
            except exceptions.CosmosResourceExistsError:
                existing = await self.get_catalog_chunk(chunk.tenant_id, chunk.id)
                if not existing or existing.content != chunk.content:
                    raise ConflictError("Immutable catalog chunk changed") from None
        try:
            await self._desired.create_item(self._document(manifest))
        except exceptions.CosmosResourceExistsError:
            existing_manifest = await self.get_catalog_manifest(manifest.tenant_id, manifest.id)
            if not existing_manifest or (
                existing_manifest.digest != manifest.digest
                or existing_manifest.chunk_ids != manifest.chunk_ids
                or existing_manifest.chunk_hashes != manifest.chunk_hashes
            ):
                raise ConflictError("Immutable catalog manifest changed") from None

    async def list_catalog_drafts(self, tenant_id: str) -> list[CatalogDraft]:
        return sorted(
            await self._query(CatalogDraft, tenant_id, "publicCatalogDraft"),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    async def get_catalog_draft(self, tenant_id: str, draft_id: str) -> CatalogDraft | None:
        return await self._read(CatalogDraft, tenant_id, draft_id)

    async def create_catalog_draft(
        self, draft: CatalogDraft, audit_event: AuditEvent
    ) -> CatalogDraft:
        await self._mutate(
            draft, None, audit_event, "create", conflict_message="That catalog draft already exists"
        )
        return await self.get_catalog_draft(draft.tenant_id, draft.id) or draft

    async def list_catalog_activations(self, tenant_id: str) -> list[CatalogActivation]:
        return sorted(
            await self._query(CatalogActivation, tenant_id, "publicCatalogActivation"),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )

    async def get_catalog_activation(
        self, tenant_id: str, revision: str
    ) -> CatalogActivation | None:
        return await self._read(CatalogActivation, tenant_id, revision)

    async def activate_catalog(
        self,
        head: CatalogHead,
        draft: CatalogDraft,
        activation: CatalogActivation,
        audit_event: AuditEvent,
    ) -> CatalogActivation:
        if (
            head.tenant_id != draft.tenant_id
            or activation.tenant_id != head.tenant_id
            or activation.draft_id != draft.id
            or activation.id != head.revision
            or not draft.etag
        ):
            raise ValueError("Catalog activation must use one tenant and versioned draft")
        await self._execute_batch(
            [
                ("create", (self._document(activation),)),
                self._replace_operation(head) if head.etag else ("create", (self._document(head),)),
                self._replace_operation(draft),
                ("create", (self._document(audit_event),)),
            ],
            audit_event,
            conflict_message="The catalog or draft changed; reload and review",
        )
        saved = await self.get_catalog_activation(activation.tenant_id, activation.id)
        if saved is None:
            raise ConflictError("The activation was committed but cannot yet be read; reconcile it")
        return saved

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
