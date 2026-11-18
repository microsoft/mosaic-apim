"""Versioned public reference data; never a model deployment or a billing approval."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime
from functools import cache
from importlib import resources
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import Field, field_validator, model_validator

from mosaic_api.domain import Entity, MosaicModel, deterministic_id
from mosaic_api.errors import ValidationError
from mosaic_api.pricing import (
    _REGION_PATTERN,
    ModelPrice,
    PriceSeed,
    PriceSource,
    RetailMeters,
    _compact,
    _fold,
    canonical_deployment_type,
    is_provisioned,
)

if TYPE_CHECKING:
    from mosaic_api.retail_catalog import Meters, Model

MAX_CATALOG_BYTES = 15 * 1024 * 1024
MAX_CATALOG_DEFINITIONS = 2_000
MAX_CATALOG_PRICES = 20_000
MAX_CATALOG_SOURCES = 256
CATALOG_CHUNK_BYTES = 256 * 1024


def canonical_json(value: MosaicModel) -> str:
    return json.dumps(
        value.model_dump(mode="json", by_alias=True, exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def content_hash(content: str | bytes) -> str:
    return hashlib.sha256(
        content.encode("utf-8") if isinstance(content, str) else content
    ).hexdigest()


def _validate_source_url(url: str) -> None:
    parts = urlsplit(url)
    secret_names = {
        "sig",
        "signature",
        "token",
        "apikey",
        "key",
        "password",
        "secret",
        "clientsecret",
        "accesstoken",
        "authorization",
        "credential",
        "sas",
    }
    query_names = {_compact(name) for name, _ in parse_qsl(parts.query)}
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or bool(query_names & secret_names)
        or any(
            name.startswith(("xamz", "xgoog")) and (
                "signature" in name or "credential" in name or "securitytoken" in name
            )
            for name in query_names
        )
    ):
        raise ValueError("Use credential-free HTTPS source URLs, not secret or signed links")


def _scope(value: ModelPrice | CatalogDefinition) -> tuple[str, ...]:
    return (
        _fold(value.cloud),
        _compact(value.publisher),
        _fold(value.model),
        _fold(value.version),
        _compact(value.deployment_type),
    )


class CatalogDefinition(MosaicModel):
    cloud: Literal["commercial", "government"]
    publisher: str = Field(min_length=1, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    version: str | None = Field(default=None, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    deployment_type: str | None = Field(default=None, max_length=64)
    regions: list[str] | None = Field(default=None, max_length=80)
    kind: Literal["tokens", "ptu"] = "tokens"
    meters: RetailMeters | None = None
    availability_source: PriceSource | None = None
    requires_output: bool = True
    notes: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def valid_dimensions(self) -> CatalogDefinition:
        if not _compact(self.publisher) or not _fold(self.model):
            raise ValueError("Use a nonempty model and a distinct normalized publisher identity")
        if self.deployment_type != canonical_deployment_type(self.deployment_type):
            raise ValueError("Use the canonical deployment type")
        if self.meters is None:
            if self.availability_source is None:
                raise ValueError(
                    "A model without public meters needs explicit availability evidence"
                )
            _validate_source_url(self.availability_source.url)
        elif self.kind == "ptu":
            if not self.meters.provisioned or not is_provisioned(self.deployment_type):
                raise ValueError("PTU mappings need a supported provisioned type and meter")
        elif (
            not self.meters.input
            or is_provisioned(self.deployment_type)
            or (self.requires_output and not self.meters.output)
        ):
            raise ValueError("Token mappings need their required input/output meters")
        if self.meters is not None and any(
            not value.strip() or len(value) > 300
            for value in self.meters.model_dump(exclude_none=True).values()
        ):
            raise ValueError("Use exact nonempty product/meter names of at most 300 characters")
        if self.regions is not None and (
            not self.regions
            or len(self.regions) != len(set(self.regions))
            or any(not re.fullmatch(_REGION_PATTERN, item) for item in self.regions)
        ):
            raise ValueError("Regions must be distinct lowercase ARM region names")
        return self

    @property
    def key(self) -> str:
        return content_hash(
            "\0".join(
                (
                    *_scope(self),
                    ",".join(sorted(self.regions or [])),
                )
            )
        )


class CatalogEvidence(MosaicModel):
    source_id: str
    cloud: Literal["commercial", "government"]
    price_ids: list[str] = Field(min_length=1, max_length=MAX_CATALOG_PRICES)
    excerpt: str = Field(min_length=20, max_length=4_000)


def validate_price_windows(prices: list[ModelPrice]) -> None:
    windows: dict[tuple[str, ...], list[ModelPrice]] = defaultdict(list)
    for price in prices:
        scope = _scope(price)
        windows[("model", *scope)].append(price)
        for alias in price.aliases:
            windows[
                (
                    "alias",
                    scope[0],
                    scope[1],
                    _fold(alias),
                    scope[3],
                    scope[4],
                )
            ].append(price)
    for versions in windows.values():
        ends: dict[str | None, date] = {}
        for price in sorted(versions, key=lambda item: item.effective_from):
            regions: list[str | None] = list(price.regions) if price.regions else [None]
            for region in regions:
                previous_end = ends.get(region)
                if previous_end is not None and previous_end >= price.effective_from:
                    raise ValueError(
                        "Source scope, date or alias windows overlap at equal region specificity. "
                        "End the earlier version or correct its alias before replacement."
                    )
                ends[region] = price.effective_until or date.max


class CatalogPackage(MosaicModel):
    format: Literal["mosaicPublicCatalog"] = "mosaicPublicCatalog"
    schema_version: Literal[1] = 1
    catalog: list[CatalogDefinition] = Field(max_length=MAX_CATALOG_DEFINITIONS)
    price_list: PriceSeed
    evidence: list[CatalogEvidence] = Field(default_factory=list, max_length=MAX_CATALOG_PRICES)

    @field_validator("price_list", mode="before")
    @classmethod
    def require_json_rate_numbers(cls, value: object) -> object:
        # PriceSeed also reads legacy state; strict wire types belong at this new boundary.
        if not isinstance(value, dict) or not isinstance(value.get("prices"), list):
            return value
        rate_fields = (
            "inputPerMillion", "input_per_million",
            "cachedInputPerMillion", "cached_input_per_million",
            "outputPerMillion", "output_per_million",
            "ptuHourly", "ptu_hourly",
        )
        for index, price in enumerate(value["prices"]):
            if not isinstance(price, dict):
                continue
            for name in rate_fields:
                number = price.get(name)
                if number is not None and type(number) not in (int, float):
                    raise ValueError(
                        f"prices[{index}].{name} must be a JSON number; "
                        "booleans and numeric strings are not rates"
                    )
        return value

    @model_validator(mode="after")
    def valid_package(self) -> CatalogPackage:
        if not self.catalog:
            raise ValueError("Include at least one model/meter definition")
        if len(self.price_list.prices) > MAX_CATALOG_PRICES:
            raise ValueError("The package exceeds 20,000 prices")
        if len(self.price_list.sources) > MAX_CATALOG_SOURCES:
            raise ValueError("The package exceeds 256 sources")
        keys = [definition.key for definition in self.catalog]
        if len(set(keys)) != len(keys):
            raise ValueError("Two definitions have the same normalized scope")
        definitions: dict[tuple[str, ...], list[CatalogDefinition]] = defaultdict(list)
        for definition in self.catalog:
            definitions[_scope(definition)].append(definition)
        source_ids = {source.id for source in self.price_list.sources}
        price_ids = {price.id for price in self.price_list.prices}
        prices_by_id = {price.id: price for price in self.price_list.prices}
        for price in self.price_list.prices:
            if (
                len(price.regions or []) > 80
                or len(price.aliases) > 20
                or len(price.source_ids) > MAX_CATALOG_SOURCES
            ):
                raise ValueError("A price exceeds the region, alias or source-reference bounds")
        numeric_government_sources = set()
        for source in self.price_list.sources:
            _validate_source_url(source.url)
            parts = urlsplit(source.url)
            availability = parts.hostname == "learn.microsoft.com" and (
                "models-sold-directly-by-azure" in parts.path
                or parts.path.rstrip("/").endswith("/openai/azure-government")
            )
            if parts.hostname != "prices.azure.com" and not availability:
                numeric_government_sources.add(source.id)
        government_evidence: dict[str, set[str]] = defaultdict(set)
        for evidence in self.evidence:
            if evidence.source_id not in source_ids or not set(evidence.price_ids) <= price_ids:
                raise ValueError("Evidence references an unknown source or price")
            for price_id in evidence.price_ids:
                price = prices_by_id[price_id]
                if evidence.cloud != price.cloud or evidence.source_id not in price.source_ids:
                    raise ValueError("Evidence must cite its exact cloud and price source")
                if (
                    evidence.cloud == "government"
                    and evidence.source_id in numeric_government_sources
                ):
                    government_evidence[price_id].add(evidence.source_id)
        scopes: set[tuple[str, date, str]] = set()
        for price in self.price_list.prices:
            for value in (
                price.input_per_million,
                price.cached_input_per_million,
                price.output_per_million,
                price.ptu_hourly,
            ):
                if value is not None and (not math.isfinite(value) or value > 100_000):
                    raise ValueError("Rates must be finite and at most 100,000 USD per stated unit")
            token = price.input_per_million is not None
            if token == (price.ptu_hourly is not None):
                raise ValueError("A rate must use token units or PTU-hours, never both")
            matches = [
                definition
                for definition in definitions.get(_scope(price), [])
                if (
                    definition.regions is None
                    or (price.regions is not None and set(price.regions) <= set(definition.regions))
                )
            ]
            if len(matches) != 1:
                raise ValueError(f"{price.id} needs one unambiguous cloud/model/type mapping")
            definition = matches[0]
            if definition.kind != ("tokens" if token else "ptu"):
                raise ValueError(f"{price.id} disagrees with its mapping's units")
            if (
                definition.kind == "tokens"
                and definition.requires_output
                and price.output_per_million is None
            ):
                raise ValueError(f"{price.id} has no required output rate")
            if price.retail_meters is not None and definition.meters is None:
                raise ValueError(f"{price.id} supplies meters absent from its definition")
            if price.retail_meters is not None and definition.meters is not None:
                for dimension in ("product", "input", "output", "provisioned", "cached_input"):
                    meter = getattr(price.retail_meters, dimension)
                    if meter is not None and meter != getattr(definition.meters, dimension):
                        raise ValueError(f"{price.id} disagrees with its exact meter mapping")
            if set(price.aliases) != set(definition.aliases):
                raise ValueError(f"{price.id} disagrees with its declared aliases")
            scope = (definition.key, price.effective_from, ",".join(sorted(price.regions or [])))
            if scope in scopes:
                raise ValueError("Two prices have the same normalized scope and effective date")
            scopes.add(scope)
            if price.cloud == "government":
                if not government_evidence.get(price.id):
                    raise ValueError(
                        f"{price.id} needs Government numeric price evidence, not Commercial "
                        "Retail or model-availability documentation"
                    )
        validate_price_windows(self.price_list.prices)
        return self


def validate_source_dates(package: CatalogPackage, today: date) -> None:
    if (
        package.price_list.last_updated > today
        or any(source.retrieved_on > today for source in package.price_list.sources)
        or any(
            item.availability_source is not None and item.availability_source.retrieved_on > today
            for item in package.catalog
        )
    ):
        raise ValidationError("A claimed source-read date cannot be in the future")


@cache
def load_catalog_definitions() -> tuple[CatalogDefinition, ...]:
    text = resources.files("mosaic_api").joinpath("data/model_catalog.json").read_text("utf-8")
    return tuple(CatalogDefinition.model_validate(item) for item in json.loads(text))


def packaged_models() -> list[Model]:
    from mosaic_api.retail_catalog import Meters, Model

    grouped: dict[
        tuple[str, str, str | None, str, tuple[str, ...], str | None],
        dict[str | None, Meters],
    ] = defaultdict(dict)
    for definition in load_catalog_definitions():
        if (
            definition.cloud != "commercial"
            or definition.kind != "tokens"
            or definition.meters is None
        ):
            continue
        key = (
            definition.publisher,
            definition.model,
            definition.version,
            definition.meters.product,
            tuple(definition.aliases),
            definition.notes,
        )
        grouped[key][definition.deployment_type] = Meters(
            definition.meters.input or "",
            definition.meters.output,
            definition.meters.cached_input,
        )
    return [
        Model(publisher, model, version, product, types, aliases, notes)
        for (publisher, model, version, product, aliases, notes), types in grouped.items()
    ]


class CatalogReceipt(MosaicModel):
    method: Literal["supplied", "retail", "packaged"]
    acquired_at: datetime
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor_id: str | None = None


class CatalogRateRecord(MosaicModel):
    recorded_at: datetime
    recorded_by: str
    draft_id: str


class CatalogContent(MosaicModel):
    baseline: PriceSeed
    public_prices: PriceSeed
    catalog: list[CatalogDefinition]
    mapping_history: list[CatalogDefinition] = Field(default_factory=list)
    receipts: dict[str, CatalogReceipt] = Field(default_factory=dict)
    records: dict[str, CatalogRateRecord] = Field(default_factory=dict)
    evidence: list[CatalogEvidence] = Field(default_factory=list)


class CatalogChunk(Entity):
    entity_type: Literal["publicCatalogChunk"] = "publicCatalogChunk"
    content: str
    digest: str


class CatalogManifest(Entity):
    entity_type: Literal["publicCatalogManifest"] = "publicCatalogManifest"
    kind: Literal["package", "book"]
    chunk_ids: list[str]
    chunk_hashes: list[str]
    digest: str


class CatalogDraft(Entity):
    entity_type: Literal["publicCatalogDraft"] = "publicCatalogDraft"
    manifest_id: str | None = None
    base_revision: str | None
    method: Literal["supplied", "retail", "recovery"]
    status: Literal["readyForReview", "invalid", "activated"] = "readyForReview"
    error: str | None = None
    receipt: CatalogReceipt
    recovery_revision: str | None = None
    activation_id: str | None = None
    created_by: str


class CatalogHead(Entity):
    entity_type: Literal["publicCatalogHead"] = "publicCatalogHead"
    revision: str


class CatalogActivation(Entity):
    entity_type: Literal["publicCatalogActivation"] = "publicCatalogActivation"
    draft_id: str
    manifest_id: str
    previous_revision: str | None
    effective_from: date
    recorded_by: str
    note: str
    preview_digest: str
    approval_hash: str
    backdated: bool
    recovered_from: str | None = None


def catalog_head_id(tenant_id: str) -> str:
    return deterministic_id("publiccataloghead", tenant_id)


class CatalogPreviewRequest(MosaicModel):
    effective_from: date


class CatalogActivateRequest(CatalogPreviewRequest):
    draft_version: str = Field(max_length=200)
    base_revision: str | None = Field(max_length=200)
    preview_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(min_length=1, max_length=500)
    acknowledge_source_and_gaps: bool
    acknowledge_backdate_impact: bool = False

    @field_validator("note")
    @classmethod
    def nonempty_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Enter an activation note")
        return value.strip()


class CatalogRefreshRequest(MosaicModel):
    draft_id: str | None = Field(default=None, max_length=200)
    base_revision: str | None = Field(max_length=200)


class CatalogRecoveryRequest(MosaicModel):
    revision: str = Field(max_length=200)
