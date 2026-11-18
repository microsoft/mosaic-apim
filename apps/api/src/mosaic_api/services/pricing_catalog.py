"""Public catalog acquisition and explicit, atomic, dated activation."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Literal

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from mosaic_api.domain import AuditEvent, MosaicModel, deterministic_id, new_id
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, ValidationError
from mosaic_api.integrations.retail_prices import RetailPricesAdapter
from mosaic_api.pricing import (
    DeploymentFacts,
    ModelPrice,
    PriceBook,
    PriceEntry,
    PriceMatch,
    PriceSeed,
    Unpriced,
    seed_entries,
)
from mosaic_api.pricing_catalog import (
    CATALOG_CHUNK_BYTES,
    MAX_CATALOG_BYTES,
    CatalogActivateRequest,
    CatalogActivation,
    CatalogChunk,
    CatalogContent,
    CatalogDefinition,
    CatalogDraft,
    CatalogEvidence,
    CatalogHead,
    CatalogManifest,
    CatalogPackage,
    CatalogRateRecord,
    CatalogReceipt,
    canonical_json,
    catalog_head_id,
    content_hash,
    load_catalog_definitions,
    validate_price_windows,
    validate_source_dates,
)
from mosaic_api.repositories.base import PricingRepository
from mosaic_api.retail_catalog import CARRIED_SUFFIX, carry_forward
from mosaic_api.services.directory import Actor

if TYPE_CHECKING:
    from mosaic_api.services.pricing import PricingService


class CatalogSourceView(MosaicModel):
    id: str
    title: str
    url: str
    retrieved_on: date
    method: Literal["supplied", "retail", "packaged"]
    acquired_at: datetime | None = None
    age_days: int


class CatalogDraftView(MosaicModel):
    id: str
    version: str
    base_revision: str | None
    method: Literal["supplied", "retail", "recovery"]
    status: Literal["readyForReview", "invalid", "activated"]
    error: str | None
    acquired_at: datetime
    activation_id: str | None
    definitions: int = 0
    prices: int = 0
    sources: list[CatalogSourceView] = Field(default_factory=list)
    evidence: list[CatalogEvidence] = Field(default_factory=list)
    mappings: list[CatalogDefinition] = Field(default_factory=list)
    missing_prices: list[str] = Field(default_factory=list)


class CatalogCoverageView(MosaicModel):
    cloud: str
    deployments: int
    priced_before: int
    priced_after: int


class CatalogGapView(MosaicModel):
    key: str
    cloud: str | None
    model: str | None
    region: str | None
    deployment_type: str | None
    reason: str
    message: str


class CatalogChangeView(MosaicModel):
    price: ModelPrice
    previous: ModelPrice | None
    previous_label: str
    change: Literal["added", "changed", "unchanged"]
    effective_from: date


class CatalogPreviewView(MosaicModel):
    draft: CatalogDraftView
    base_revision: str | None
    effective_from: date
    preview_digest: str
    changes: list[CatalogChangeView]
    total_changes: int
    added: int
    changed: int
    unchanged: int
    retained: int
    protected_manual: int
    protected_billing: int
    coverage: list[CatalogCoverageView]
    gaps: list[CatalogGapView]
    total_gaps: int
    backdated: bool
    source_dates_precede_activation: bool
    next_offset: int | None


class CatalogStatusView(MosaicModel):
    revision: str | None
    seed_last_updated: date
    sources: list[CatalogSourceView]
    definitions: int
    mappings: list[CatalogDefinition]
    deployments: int
    priced_deployments: int
    drafts: list[CatalogDraftView]
    history: list[CatalogActivation]
    next_offset: int | None


def catalog_book(
    content: CatalogContent, explicit: list[PriceEntry], revision: str | None
) -> PriceBook:
    public = []
    baseline = {entry.id: entry for entry in seed_entries(content.baseline)}
    for entry in seed_entries(content.public_prices):
        original_id = CARRIED_SUFFIX.sub("", entry.id)
        record = content.records.get(original_id)
        previous = baseline.get(original_id)
        public.append(
            replace(entry, recorded_at=record.recorded_at, recorded_by=record.recorded_by)
            if record
            else replace(entry, recorded_at=previous.recorded_at)
            if previous
            else entry
        )
    return PriceBook(
        [*public, *explicit],
        content.public_prices.ptu_throughput,
        protected_baseline=[*seed_entries(content.baseline), *explicit],
        catalog_definitions=[*content.catalog, *content.mapping_history],
        catalog_revision=revision,
        public_seed=content.public_prices,
    )


class PublicCatalogService:
    def __init__(
        self,
        repository: PricingRepository,
        pricing: PricingService,
        seed: PriceSeed,
        retail: RetailPricesAdapter | None = None,
    ) -> None:
        self._repository = repository
        self._pricing = pricing
        self._seed = seed
        self._retail = retail
        self._cache: dict[tuple[str, str], tuple[CatalogContent, int]] = {}

    def _now(self) -> datetime:
        return self._pricing._clock()

    def _audit(self, actor: Actor, action: str, resource_id: str, **details: object) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            actor_object_id=actor.object_id,
            action=action,
            resource_type="publicCatalog",
            resource_id=resource_id,
            details=details,
        )

    async def _write_payload(
        self,
        tenant_id: str,
        payload: CatalogPackage | CatalogContent,
        kind: Literal["package", "book"],
    ) -> CatalogManifest:
        text = canonical_json(payload)
        chunks = [
            CatalogChunk(
                id=deterministic_id("catalogchunk", tenant_id, content_hash(part)),
                tenant_id=tenant_id,
                content=part,
                digest=content_hash(part),
            )
            for offset in range(0, len(text), CATALOG_CHUNK_BYTES)
            if (part := text[offset : offset + CATALOG_CHUNK_BYTES])
        ]
        manifest = CatalogManifest(
            id=deterministic_id("catalogmanifest", tenant_id, kind, content_hash(text)),
            tenant_id=tenant_id,
            kind=kind,
            chunk_ids=[part.id for part in chunks],
            chunk_hashes=[part.digest for part in chunks],
            digest=content_hash(text),
        )
        await self._repository.prepare_catalog(manifest, chunks)
        await self._read_payload(tenant_id, manifest.id, kind)
        return manifest

    async def _read_payload(
        self, tenant_id: str, manifest_id: str, kind: Literal["package", "book"]
    ) -> CatalogPackage | CatalogContent:
        manifest = await self._repository.get_catalog_manifest(tenant_id, manifest_id)
        if (
            manifest is None
            or manifest.kind != kind
            or not manifest.chunk_ids
            or len(manifest.chunk_ids) != len(manifest.chunk_hashes)
        ):
            raise ConflictError("The committed catalog manifest is incomplete; retry or recover it")
        text_parts = []
        for chunk_id, digest in zip(manifest.chunk_ids, manifest.chunk_hashes, strict=True):
            part = await self._repository.get_catalog_chunk(tenant_id, chunk_id)
            if part is None or part.digest != digest or content_hash(part.content) != digest:
                raise ConflictError(
                    "A catalog chunk is unreadable; retry or restore verified content"
                )
            text_parts.append(part.content)
        text = "".join(text_parts)
        if content_hash(text) != manifest.digest:
            raise ConflictError("The catalog content hash does not match its committed manifest")
        try:
            return (
                CatalogPackage.model_validate_json(text)
                if kind == "package"
                else CatalogContent.model_validate_json(text)
            )
        except PydanticValidationError as exc:
            raise ConflictError(
                "Stored catalog content does not match the supported schema"
            ) from exc

    async def active_content(self, tenant_id: str) -> tuple[str | None, CatalogContent | None]:
        head = await self._repository.get_catalog_head(tenant_id)
        if head is None:
            if any(key[0] == tenant_id for key in self._cache) or (
                await self._repository.has_catalog_activation(tenant_id)
            ):
                raise ConflictError(
                    "The activated public catalog is missing its head. Restore consistent "
                    "catalog state before pricing; the packaged seed is not a recovery fallback."
                )
            return None, None
        key = (tenant_id, head.revision)
        if key in self._cache:
            return head.revision, self._cache[key][0].model_copy(deep=True)
        activation = await self._repository.get_catalog_activation(tenant_id, head.revision)
        if activation is None:
            raise ConflictError(
                "The active catalog's activation is unreadable; retry before pricing"
            )
        content = await self._read_payload(tenant_id, activation.manifest_id, "book")
        if not isinstance(content, CatalogContent):
            raise ConflictError("The active catalog does not contain a price book")
        for previous in list(self._cache):
            if previous[0] == tenant_id:
                del self._cache[previous]
        size = len(canonical_json(content))
        budget = 16 * 1024 * 1024
        if size <= budget:
            while self._cache and sum(item[1] for item in self._cache.values()) + size > budget:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = (content, size)
        return head.revision, content.model_copy(deep=True)

    def _sources(
        self,
        seed: PriceSeed,
        receipt: CatalogReceipt | None = None,
        receipts: dict[str, CatalogReceipt] | None = None,
    ) -> list[CatalogSourceView]:
        return [
            CatalogSourceView(
                id=source.id,
                title=source.title,
                url=source.url,
                retrieved_on=source.retrieved_on,
                method=(
                    found.method
                    if (found := (receipts or {}).get(source.id) or receipt)
                    else "packaged"
                ),
                acquired_at=found.acquired_at if found else None,
                age_days=max(0, (self._now().date() - source.retrieved_on).days),
            )
            for source in seed.sources
        ]

    async def _draft(self, actor: Actor, draft_id: str) -> CatalogDraft:
        draft = await self._repository.get_catalog_draft(actor.tenant_id, draft_id)
        if draft is None:
            raise NotFoundError("No catalog draft has that ID")
        return draft

    async def _draft_payload(self, draft: CatalogDraft) -> CatalogPackage | CatalogContent:
        if draft.manifest_id is None:
            raise ValidationError(draft.error or "This acquisition has no validated content")
        return await self._read_payload(
            draft.tenant_id, draft.manifest_id, "book" if draft.method == "recovery" else "package"
        )

    async def draft_view(
        self, actor: Actor, draft_id: str, *, brief: bool = False
    ) -> CatalogDraftView:
        draft = await self._draft(actor, draft_id)
        view = CatalogDraftView(
            id=draft.id,
            version=draft.etag or "",
            base_revision=draft.base_revision,
            method=draft.method,
            status=draft.status,
            error=draft.error,
            acquired_at=draft.receipt.acquired_at,
            activation_id=draft.activation_id,
        )
        if draft.status != "invalid" and not brief:
            payload = await self._draft_payload(draft)
            seed = (
                payload.price_list if isinstance(payload, CatalogPackage) else payload.public_prices
            )
            view.definitions = len(payload.catalog)
            view.prices = len(seed.prices)
            view.mappings = payload.catalog
            view.evidence = payload.evidence
            view.missing_prices = [
                f"{item.cloud}: {item.model} {item.version or ''} "
                f"({item.deployment_type or 'any supported type'})"
                for item in payload.catalog
                if not any(
                    price.cloud == item.cloud
                    and price.model.casefold() == item.model.casefold()
                    and (price.version or "").casefold() == (item.version or "").casefold()
                    and price.deployment_type == item.deployment_type
                    for price in seed.prices
                )
            ]
            view.sources = self._sources(
                seed,
                draft.receipt if isinstance(payload, CatalogPackage) else None,
                payload.receipts if isinstance(payload, CatalogContent) else None,
            )
        return view

    async def status(self, actor: Actor, offset: int = 0, limit: int = 100) -> CatalogStatusView:
        revision, content = await self.active_content(actor.tenant_id)
        book, facts, drafts, history = await asyncio.gather(
            self._pricing.book(actor.tenant_id),
            self._pricing.deployments(actor.tenant_id),
            self._repository.list_catalog_drafts(actor.tenant_id),
            self._repository.list_catalog_activations(actor.tenant_id),
        )
        today = self._now().date()
        return CatalogStatusView(
            revision=revision,
            seed_last_updated=self._seed.last_updated,
            sources=self._sources(content.public_prices, receipts=content.receipts)
            if content
            else self._sources(self._seed),
            definitions=len(content.catalog) if content else len(load_catalog_definitions()),
            mappings=list(content.catalog if content else load_catalog_definitions())[
                offset : offset + limit
            ],
            deployments=len(facts),
            priced_deployments=sum(
                isinstance(book.match(item, today), PriceMatch) for item in facts.values()
            ),
            drafts=[
                await self.draft_view(actor, item.id, brief=True)
                for item in drafts[offset : offset + limit]
            ],
            history=history[offset : offset + limit],
            next_offset=(
                offset + limit
                if max(
                    len(drafts),
                    len(history),
                    len(content.catalog) if content else len(load_catalog_definitions()),
                )
                > offset + limit
                else None
            ),
        )

    async def _base(self, actor: Actor, expected: str | None) -> CatalogContent:
        revision, content = await self.active_content(actor.tenant_id)
        if revision != expected:
            raise ConflictError("The public catalog changed; reload and review before retrying")
        return content or CatalogContent(
            baseline=self._seed,
            public_prices=self._seed,
            catalog=list(load_catalog_definitions()),
        )

    async def _record(
        self,
        actor: Actor,
        payload: CatalogPackage | CatalogContent | None,
        base: str | None,
        method: Literal["supplied", "retail", "recovery"],
        digest: str,
        error: str | None = None,
        recovered_from: str | None = None,
    ) -> CatalogDraft:
        identifier = deterministic_id("catalogdraft", actor.tenant_id, base or "", method, digest)
        existing = await self._repository.get_catalog_draft(actor.tenant_id, identifier)
        if existing:
            return existing
        manifest = (
            await self._write_payload(
                actor.tenant_id,
                payload,
                "book" if isinstance(payload, CatalogContent) else "package",
            )
            if payload is not None
            else None
        )
        receipt = CatalogReceipt(
            method="retail" if method == "retail" else "supplied",
            acquired_at=self._now(),
            content_hash=digest,
            actor_id=actor.object_id,
        )
        draft = CatalogDraft(
            id=identifier,
            tenant_id=actor.tenant_id,
            base_revision=base,
            method=method,
            status="invalid" if error else "readyForReview",
            manifest_id=manifest.id if manifest else None,
            receipt=receipt,
            error=error,
            recovery_revision=recovered_from,
            created_by=actor.object_id,
        )
        try:
            return await self._repository.create_catalog_draft(
                draft,
                self._audit(
                    actor,
                    "pricing.catalogAcquired" if not error else "pricing.catalogAcquisitionFailed",
                    draft.id,
                    method=method,
                    contentHash=digest,
                    baseRevision=base,
                    manifestId=draft.manifest_id,
                    error=error,
                ),
            )
        except ConflictError:
            concurrent = await self._repository.get_catalog_draft(actor.tenant_id, identifier)
            if concurrent is None:
                raise
            return concurrent

    async def import_bytes(
        self, actor: Actor, data: bytes, base_revision: str | None
    ) -> CatalogDraftView:
        await self._base(actor, base_revision)
        digest = content_hash(data)
        if len(data) > MAX_CATALOG_BYTES:
            raise ValidationError("The catalog JSON exceeds 15 MiB")
        try:
            package = CatalogPackage.model_validate_json(data)
            validate_source_dates(package, self._now().date())
        except PydanticValidationError as exc:
            issues = []
            for item in exc.errors(include_input=False, include_context=False)[:8]:
                location = ".".join(str(part)[:80] for part in item["loc"])[:120]
                description = item["msg"]
                if "cites unknown sources" in description:
                    description = "A price or PTU sizing entry cites an unknown source ID"
                issues.append(f"{location}: {description[:300]}")
            message = "; ".join(issues)
            draft = await self._record(actor, None, base_revision, "supplied", digest, message)
            raise ValidationError(
                "The pricing package is invalid; the active catalog is unchanged",
                details={"draftId": draft.id, "issues": message},
            ) from exc
        except ValidationError as exc:
            draft = await self._record(actor, None, base_revision, "supplied", digest, exc.message)
            raise ValidationError(exc.message, details={"draftId": draft.id}) from exc
        draft = await self._record(
            actor, package, base_revision, "supplied", content_hash(canonical_json(package))
        )
        return await self.draft_view(actor, draft.id)

    async def refresh(
        self, actor: Actor, base_revision: str | None, draft_id: str | None
    ) -> CatalogDraftView:
        content = await self._base(actor, base_revision)
        definitions = content.catalog
        if draft_id:
            mapping_draft = await self._draft(actor, draft_id)
            if (
                mapping_draft.base_revision != base_revision
                or mapping_draft.status != "readyForReview"
            ):
                raise ConflictError("The mapping draft changed; reload and review")
            payload = await self._draft_payload(mapping_draft)
            definitions = payload.catalog
        if self._retail is None:
            raise ValidationError(
                "The public Retail adapter is unavailable; import a supported package"
            )
        try:
            package = await self._retail.acquire(definitions, self._now().date())
        except DomainError as exc:
            await self._record(
                actor,
                None,
                base_revision,
                "retail",
                content_hash(f"{self._now().isoformat()}:{base_revision}:{draft_id}"),
                exc.message,
            )
            raise
        draft = await self._record(
            actor, package, base_revision, "retail", content_hash(canonical_json(package))
        )
        return await self.draft_view(actor, draft.id)

    async def recover(self, actor: Actor, revision: str) -> CatalogDraftView:
        head, _ = await self.active_content(actor.tenant_id)
        activation = await self._repository.get_catalog_activation(actor.tenant_id, revision)
        if activation is None:
            raise NotFoundError("No committed catalog activation has that ID")
        content = await self._read_payload(actor.tenant_id, activation.manifest_id, "book")
        if not isinstance(content, CatalogContent):
            raise ConflictError("The selected recovery revision is not a usable catalog")
        draft = await self._record(
            actor,
            content,
            head,
            "recovery",
            content_hash(canonical_json(content)),
            recovered_from=revision,
        )
        return await self.draft_view(actor, draft.id)

    async def _candidate(
        self, actor: Actor, draft: CatalogDraft, day: date
    ) -> tuple[CatalogContent, list[ModelPrice]]:
        current = await self._base(actor, draft.base_revision)
        payload = await self._draft_payload(draft)
        recovery = isinstance(payload, CatalogContent)
        proposed = (
            payload.public_prices if isinstance(payload, CatalogContent) else payload.price_list
        )
        if (
            proposed.ptu_throughput
            and proposed.ptu_throughput != current.public_prices.ptu_throughput
        ):
            raise ValidationError(
                "PTU sizing metadata needs a separately reviewed application change"
            )
        fresh = proposed.model_dump(mode="json", by_alias=True, exclude_none=True)
        fresh["prices"] = [
            {
                **item.model_dump(mode="json", by_alias=True, exclude_none=True),
                "id": "public." + content_hash(f"{draft.id}:{item.id}")[:32],
                "effectiveFrom": max(day, item.effective_from).isoformat(),
            }
            for item in proposed.prices
            if item.effective_until is None or item.effective_until >= day
        ]
        if not fresh["prices"] and not payload.catalog:
            raise ValidationError("There are no supported catalog changes to activate")
        source_names = {
            source.id: "catalog-" + content_hash(f"{draft.id}:{source.id}")[:32]
            for source in proposed.sources
        }
        receipts = dict(current.receipts)
        records = dict(current.records)
        for price in fresh["prices"]:
            records[price["id"]] = CatalogRateRecord(
                recorded_at=draft.created_at, recorded_by=draft.created_by, draft_id=draft.id
            )
        for item in fresh["prices"]:
            item["sourceIds"] = [source_names[name] for name in item["sourceIds"]]
        for source in fresh["sources"]:
            original_id = source["id"]
            source["id"] = source_names[original_id]
            if isinstance(payload, CatalogContent):
                receipts[source["id"]] = payload.receipts.get(original_id) or CatalogReceipt(
                    method="packaged",
                    acquired_at=draft.receipt.acquired_at,
                    content_hash=draft.receipt.content_hash,
                )
            else:
                receipts[source["id"]] = draft.receipt
        before = current.public_prices.model_dump(mode="json", by_alias=True, exclude_none=True)
        if recovery:
            before["prices"] = [
                {
                    **item,
                    "effectiveUntil": min(
                        date.fromisoformat(item.get("effectiveUntil", date.max.isoformat())),
                        day - timedelta(days=1),
                    ).isoformat(),
                }
                for item in before["prices"]
                if date.fromisoformat(item["effectiveFrom"]) < day
            ]
        known_sources = {item["id"] for item in fresh["sources"]}
        fresh["sources"].extend(
            item for item in before["sources"] if item["id"] not in known_sources
        )
        fresh["ptuThroughput"] = []
        applied_dates = {item["id"]: item["effectiveFrom"] for item in fresh["prices"]}
        carry_forward(before, fresh, preserve_effective_dates=True)
        for item in fresh["prices"]:
            if item["id"] in applied_dates:
                item["effectiveFrom"] = applied_dates[item["id"]]
        previous_ids = {
            json.dumps(
                {key: value for key, value in item.items() if key != "id"}, sort_keys=True
            ): item["id"]
            for item in before["prices"]
        }
        used_ids = {item["id"] for item in fresh["prices"]}
        for item in fresh["prices"]:
            previous_id = previous_ids.get(
                json.dumps(
                    {key: value for key, value in item.items() if key != "id"}, sort_keys=True
                )
            )
            if previous_id and previous_id not in used_ids:
                used_ids.remove(item["id"])
                item["id"] = previous_id
                used_ids.add(previous_id)
        # Throughput sizing is not a newly activated numeric price.
        fresh["ptuThroughput"] = current.public_prices.model_dump(mode="json", by_alias=True)[
            "ptuThroughput"
        ]
        merged = (
            {definition.key: definition for definition in current.catalog} if not recovery else {}
        )
        mapping_history = list(current.mapping_history)
        if recovery:
            mapping_history.extend(
                item
                for item in current.catalog
                if item not in payload.catalog and item not in mapping_history
            )
        for definition in payload.catalog:
            previous_definition = merged.get(definition.key)
            if (
                previous_definition
                and (
                    previous_definition.meters != definition.meters
                    or previous_definition.aliases != definition.aliases
                )
                and previous_definition not in mapping_history
            ):
                mapping_history.append(previous_definition)
        merged.update({definition.key: definition for definition in payload.catalog})
        public_prices = PriceSeed.model_validate(fresh)
        try:
            validate_price_windows(public_prices.prices)
        except ValueError as exc:
            raise ValidationError(
                "The proposed public catalog conflicts with retained scope, date or alias "
                "history. Correct the source mapping and review again."
            ) from exc
        return CatalogContent(
            baseline=current.baseline,
            public_prices=public_prices,
            catalog=list(merged.values()),
            mapping_history=mapping_history,
            receipts=receipts,
            records=records,
            evidence=[
                *current.evidence,
                *[
                    evidence.model_copy(
                        update={
                            "source_id": source_names[evidence.source_id],
                        }
                    )
                    for evidence in payload.evidence
                    if evidence.source_id in source_names
                ],
            ],
        ), proposed.prices

    async def preview(
        self, actor: Actor, draft_id: str, day: date, offset: int = 0, limit: int = 100
    ) -> CatalogPreviewView:
        draft = await self._draft(actor, draft_id)
        if draft.status != "readyForReview":
            raise ConflictError("This draft cannot be activated; inspect or create a new draft")
        content, proposed = await self._candidate(actor, draft, day)
        before, facts = await asyncio.gather(
            self._pricing.book(actor.tenant_id), self._pricing.deployments(actor.tenant_id)
        )
        explicit = [item for item in before.entries if item.origin in {"admin", "billing"}]
        after = catalog_book(content, explicit, draft.base_revision)
        coverage: dict[str, CatalogCoverageView] = {}
        gaps = []
        for item in facts.values():
            row = coverage.setdefault(
                item.cloud or "unknown",
                CatalogCoverageView(
                    cloud=item.cloud or "unknown", deployments=0, priced_before=0, priced_after=0
                ),
            )
            row.deployments += 1
            row.priced_before += int(isinstance(before.match(item, day), PriceMatch))
            match = after.match(item, day)
            row.priced_after += int(isinstance(match, PriceMatch))
            if isinstance(match, Unpriced):
                gaps.append(
                    CatalogGapView(
                        key=item.key,
                        cloud=item.cloud,
                        model=item.model,
                        region=item.region,
                        deployment_type=item.deployment_type,
                        reason=match.reason,
                        message=match.message,
                    )
                )
        changes = []
        public_before = PriceBook([item for item in before.entries if item.origin == "seed"])
        for price in proposed:
            if price.effective_until is not None and price.effective_until < day:
                continue
            regions: list[str | None] = list(price.regions) if price.regions else [None]
            prior = []
            for region in regions:
                match = public_before.match(
                    DeploymentFacts(
                        key="preview/price",
                        endpoint_id="preview",
                        endpoint_name="Preview",
                        deployment_name="price",
                        provider="azureOpenAi",
                        cloud=price.cloud,
                        cloud_source="detected",
                        detected_cloud=price.cloud,
                        region=region,
                        publisher=price.publisher,
                        model=price.model,
                        version=price.version,
                        deployment_type=price.deployment_type,
                        deployment_type_source="observed",
                        capacity=1 if price.ptu_hourly is not None else None,
                    ),
                    day,
                )
                if isinstance(match, PriceMatch):
                    prior.append(match.entry)
            amounts = {
                (
                    item.input_per_million,
                    item.cached_input_per_million,
                    item.output_per_million,
                    item.ptu_hourly,
                )
                for item in prior
            }
            old = prior[0] if len(amounts) == 1 and len(prior) == len(regions) else None
            previous = (
                ModelPrice(
                    id=old.id,
                    cloud=old.cloud,
                    publisher=old.publisher,
                    model=old.model,
                    aliases=list(old.aliases),
                    version=old.version,
                    deployment_type=old.deployment_type,
                    regions=list(old.regions) if old.regions else None,
                    input_per_million=old.input_per_million,
                    cached_input_per_million=old.cached_input_per_million,
                    output_per_million=old.output_per_million,
                    ptu_hourly=old.ptu_hourly,
                    effective_from=old.effective_from,
                    source_ids=["previous"],
                )
                if old
                else None
            )
            same = old is not None and (
                old.input_per_million,
                old.cached_input_per_million,
                old.output_per_million,
                old.ptu_hourly,
            ) == (
                price.input_per_million,
                price.cached_input_per_million,
                price.output_per_million,
                price.ptu_hourly,
            )
            changes.append(
                CatalogChangeView(
                    price=price,
                    previous=previous,
                    previous_label="Previous public rate"
                    if old
                    else "Varies by region"
                    if prior
                    else "No public rate",
                    change="unchanged" if same else "changed" if prior else "added",
                    effective_from=max(day, price.effective_from),
                )
            )
        explicit_lines: dict[str, list[PriceEntry]] = {}
        for entry in explicit:
            explicit_lines.setdefault(entry.line_id, []).append(entry)
        active_explicit = {
            current.id: current
            for versions in explicit_lines.values()
            if (current := before.line_current(versions, day)) is not None
            and current.origin in {"admin", "billing"}
        }
        digest = content_hash(
            json.dumps(
                {
                    "draft": draft.id,
                    "version": draft.etag,
                    "base": draft.base_revision,
                    "content": content_hash(canonical_json(content)),
                    "date": day.isoformat(),
                    "prices": [asdict(item) for item in explicit],
                    "facts": {key: asdict(value) for key, value in sorted(facts.items())},
                },
                sort_keys=True,
                default=str,
            )
        )
        return CatalogPreviewView(
            draft=await self.draft_view(actor, draft.id),
            base_revision=draft.base_revision,
            effective_from=day,
            preview_digest=digest,
            changes=changes[offset : offset + limit],
            total_changes=len(changes),
            added=sum(item.change == "added" for item in changes),
            changed=sum(item.change == "changed" for item in changes),
            unchanged=sum(item.change == "unchanged" for item in changes),
            retained=max(0, len(before.entries) - len(changes)),
            protected_manual=sum(item.origin == "admin" for item in active_explicit.values()),
            protected_billing=sum(item.origin == "billing" for item in active_explicit.values()),
            coverage=list(coverage.values()),
            gaps=gaps[offset : offset + limit],
            total_gaps=len(gaps),
            backdated=day < self._now().date(),
            source_dates_precede_activation=any(item.effective_from < day for item in proposed),
            next_offset=offset + limit if max(len(changes), len(gaps)) > offset + limit else None,
        )

    async def activate(
        self, actor: Actor, draft_id: str, request: CatalogActivateRequest
    ) -> CatalogActivation:
        draft = await self._draft(actor, draft_id)
        approval_hash = content_hash(canonical_json(request))
        if draft.status == "activated" and draft.activation_id:
            previous = await self._repository.get_catalog_activation(
                actor.tenant_id, draft.activation_id
            )
            if previous and previous.approval_hash == approval_hash:
                return previous
            raise ConflictError("This draft was activated with different approval inputs")
        if draft.etag != request.draft_version or draft.base_revision != request.base_revision:
            raise ConflictError("The catalog draft changed; reload and review")
        preview = await self.preview(actor, draft.id, request.effective_from)
        if preview.preview_digest != request.preview_digest:
            raise ConflictError("The pricing preview changed; reload and review before activation")
        if not request.acknowledge_source_and_gaps:
            raise ValidationError("Review and acknowledge source assertions and remaining gaps")
        if preview.backdated and not request.acknowledge_backdate_impact:
            raise ValidationError(
                "Backdating reprices historical reports and budgets; acknowledge the impact"
            )
        content, _ = await self._candidate(actor, draft, request.effective_from)
        manifest = await self._write_payload(actor.tenant_id, content, "book")
        old_head = await self._repository.get_catalog_head(actor.tenant_id)
        if (old_head.revision if old_head else None) != draft.base_revision:
            raise ConflictError("Another catalog was activated; reload and review")
        activation = CatalogActivation(
            id=deterministic_id("catalogactivation", actor.tenant_id, draft.id, approval_hash),
            tenant_id=actor.tenant_id,
            draft_id=draft.id,
            manifest_id=manifest.id,
            previous_revision=draft.base_revision,
            effective_from=request.effective_from,
            recorded_by=actor.object_id,
            note=request.note,
            preview_digest=request.preview_digest,
            approval_hash=approval_hash,
            backdated=preview.backdated,
            recovered_from=draft.recovery_revision,
            created_at=self._now(),
            updated_at=self._now(),
        )
        head = CatalogHead(
            id=catalog_head_id(actor.tenant_id),
            tenant_id=actor.tenant_id,
            revision=activation.id,
            etag=old_head.etag if old_head else None,
            created_at=old_head.created_at if old_head else self._now(),
            updated_at=self._now(),
        )
        activated = draft.model_copy(update={"status": "activated", "activation_id": activation.id})
        return await self._repository.activate_catalog(
            head,
            activated,
            activation,
            self._audit(
                actor,
                "pricing.catalogRecovered"
                if draft.method == "recovery"
                else "pricing.catalogActivated",
                activation.id,
                draftId=draft.id,
                manifestId=manifest.id,
                previousRevision=draft.base_revision,
                contentHash=manifest.digest,
                effectiveFrom=request.effective_from.isoformat(),
                backdated=preview.backdated,
                protectedManual=preview.protected_manual,
                protectedBilling=preview.protected_billing,
                previewDigest=preview.preview_digest,
                note=request.note,
                recoveredFrom=draft.recovery_revision,
            ),
        )
