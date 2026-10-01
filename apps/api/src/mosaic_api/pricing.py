"""The price list that turns measured usage into cost. See ADR 0020.

MOSAIC ships a seed of public list prices, ``data/model_prices.json``, built from the Azure Retail
Prices API. Every price in it cites a source. Administrators add prices for other clouds and
providers, and override seeded ones, as dated versions kept in Cosmos. Nothing rewrites a price:
a new version takes effect on its own date, and a correction is a version with the same date.

A deployment is priced by its facts: the cloud its endpoint is in, the model's publisher, name,
and version, the deployment type, and the region. The most specific price wins. A price that
names a region beats one that doesn't, and one that names a version beats one that doesn't. A
deployment nothing matches has no cost, never a cost of zero.

Provisioned (PTU) deployments cost their capacity by the hour, or a monthly amount an
administrator entered, whether or not anyone calls them. Each month's cost is shared among the
deployment's callers by their share of its tokens.
"""

import calendar
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from functools import cache
from importlib import resources
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, field_validator, model_validator

from mosaic_api.domain import (
    DeclaredDeployment,
    Entity,
    ModelEndpoint,
    ModelProvider,
    MosaicModel,
    deterministic_id,
)
from mosaic_api.observed import ObservedModelDeployment

CURRENCY = "USD"
SEED_RESOURCE = "data/model_prices.json"
SCHEMA_RESOURCE = "data/model_prices.schema.json"
# The clouds MOSAIC recognizes by their hosts and ships prices for. Any other key is a custom
# cloud or provider an administrator names and prices.
BUILT_IN_CLOUDS: dict[str, str] = {
    "commercial": "Azure Commercial",
    "government": "Azure Government",
}
CLOUD_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_CLOUD_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
_REGION_PATTERN = r"^[a-z0-9]{1,40}$"
_SOURCE_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,79}$"
_SEED_ID_PATTERN = r"^[a-z0-9][a-z0-9.-]{0,159}$"
WILDCARD_MODEL = "*"
PER_MILLION = 1_000_000

# Azure's deployment types, as a deployment's SKU names them.
DEPLOYMENT_TYPES: tuple[str, ...] = (
    "GlobalStandard",
    "DataZoneStandard",
    "Standard",
    "GlobalBatch",
    "DataZoneBatch",
    "GlobalProvisionedManaged",
    "DataZoneProvisionedManaged",
    "ProvisionedManaged",
    "DeveloperTier",
)
PROVISIONED_TYPES = frozenset(
    {"globalprovisionedmanaged", "datazoneprovisionedmanaged", "provisionedmanaged"}
)

UnpricedReason = Literal[
    "unknownDeployment",
    "noCloud",
    "noModel",
    "noDeploymentType",
    "noCapacity",
    "noPrice",
    "notYetEffective",
    "noOutputPrice",
    "beforeDeployment",
]
PriceOrigin = Literal["seed", "admin"]
CloudSource = Literal["detected", "override"]


def cloud_label(cloud: str | None) -> str:
    if cloud is None:
        return "an unknown cloud"
    return BUILT_IN_CLOUDS.get(cloud, cloud)


def days_in_month(day: date) -> int:
    return calendar.monthrange(day.year, day.month)[1]


def month_first(day: date) -> date:
    return date(day.year, day.month, 1)


def month_last(day: date) -> date:
    return date(day.year, day.month, days_in_month(day))


def _fold(value: str | None) -> str:
    return (value or "").strip().casefold()


def _compact(value: str | None) -> str:
    """Case and punctuation folded away, so "Mistral AI" matches "MistralAI"."""

    return re.sub(r"[^a-z0-9]", "", _fold(value))


def normalize_region(value: str | None) -> str | None:
    """An Azure region as ARM names it, such as ``eastus2`` for "East US 2"."""

    compact = _compact(value)
    return compact or None


def is_provisioned(deployment_type: str | None) -> bool:
    return _compact(deployment_type) in PROVISIONED_TYPES


def canonical_deployment_type(value: str | None) -> str | None:
    """A deployment type as Azure spells it, or the value as given if Azure has no such type."""

    if value is None or not value.strip():
        return None
    folded = _compact(value)
    for known in DEPLOYMENT_TYPES:
        if _compact(known) == folded:
            return known
    return value.strip()


def detect_cloud(endpoint_url: str | None) -> str | None:
    """The cloud an endpoint's host is in, or None when the host doesn't say.

    Azure Government's AI hosts end in ``.azure.us``, and Azure Commercial's in ``.azure.com``.
    Anything else, such as an OpenAI-compatible provider or another sovereign cloud, is a custom
    cloud an administrator names.
    """

    if not endpoint_url:
        return None
    host = (urlsplit(endpoint_url).hostname or "").casefold().rstrip(".")
    if host.endswith(".azure.us") or host.endswith(".usgovcloudapi.net"):
        return "government"
    if host.endswith(".azure.com"):
        return "commercial"
    return None


# -- the seed file ---------------------------------------------------------------------------


class PriceSource(MosaicModel):
    """Where a price came from: a query of the Retail Prices API, or a documentation page."""

    id: str = Field(pattern=_SOURCE_ID_PATTERN)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(pattern=r"^https://", max_length=2000)
    retrieved_on: date


class RetailMeters(MosaicModel):
    """The Retail Prices API meters a seeded price was read from, so a refresh can find them."""

    product: str
    input: str | None = None
    cached_input: str | None = None
    output: str | None = None
    provisioned: str | None = None


class ModelPrice(MosaicModel):
    """One price in the seed. Prices are in US dollars per million tokens, or per PTU an hour."""

    id: str = Field(pattern=_SEED_ID_PATTERN)
    cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    publisher: str | None = Field(default=None, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list)
    version: str | None = Field(default=None, max_length=64)
    deployment_type: str | None = Field(default=None, max_length=64)
    regions: list[str] | None = None
    input_per_million: float | None = Field(default=None, ge=0)
    cached_input_per_million: float | None = Field(default=None, ge=0)
    output_per_million: float | None = Field(default=None, ge=0)
    ptu_hourly: float | None = Field(default=None, ge=0)
    effective_from: date
    # The last day of a price a later refresh replaced. A refresh keeps the price it replaces, so
    # the days it covered keep their cost.
    effective_until: date | None = None
    source_ids: list[str] = Field(min_length=1)
    retail_meters: RetailMeters | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("regions")
    @classmethod
    def validate_regions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("Leave regions out rather than listing none")
        for region in value:
            if not re.fullmatch(_REGION_PATTERN, region):
                raise ValueError(f"Region {region!r} isn't an ARM region name such as eastus2")
        return value

    @model_validator(mode="after")
    def validate_prices(self) -> "ModelPrice":
        if self.ptu_hourly is None and self.input_per_million is None:
            raise ValueError(f"Price {self.id} has neither a token price nor a PTU rate")
        if self.ptu_hourly is not None and not is_provisioned(self.deployment_type):
            raise ValueError(f"Price {self.id} has a PTU rate for a type that isn't provisioned")
        if self.effective_until is not None and self.effective_until < self.effective_from:
            raise ValueError(f"Price {self.id} ends before it takes effect")
        return self


class PtuThroughput(MosaicModel):
    """How many tokens a minute one PTU serves for a model, to judge a reservation's use."""

    model: str = Field(min_length=1, max_length=120)
    version: str | None = Field(default=None, max_length=64)
    input_tokens_per_minute_per_ptu: int = Field(gt=0)
    output_to_input_ratio: float = Field(gt=0)
    source_ids: list[str] = Field(min_length=1)


class PriceSeed(MosaicModel):
    """The shipped price list. ``model_prices.schema.json`` is this model's JSON Schema."""

    model_config = ConfigDict(
        json_schema_extra={
            "$id": "https://github.com/microsoft/mosaic-apim/apps/api/src/mosaic_api/data/"
            "model_prices.schema.json",
            "title": "MOSAIC model price list",
            "description": "List prices MOSAIC ships, each with the sources it came from.",
        }
    )

    schema_ref: str | None = Field(default=None, alias="$schema")
    schema_version: Literal[1]
    last_updated: date
    currency: Literal["USD"]
    sources: list[PriceSource] = Field(min_length=1)
    prices: list[ModelPrice]
    ptu_throughput: list[PtuThroughput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_sources(self) -> "PriceSeed":
        known = {source.id for source in self.sources}
        if len(known) != len(self.sources):
            raise ValueError("Two sources share an ID")
        ids = [price.id for price in self.prices]
        if len(set(ids)) != len(ids):
            raise ValueError("Two prices share an ID")
        cited = [(price.id, price.source_ids) for price in self.prices] + [
            (item.model, item.source_ids) for item in self.ptu_throughput
        ]
        for name, source_ids in cited:
            missing = [source for source in source_ids if source not in known]
            if missing:
                raise ValueError(f"{name} cites unknown sources {missing}")
        return self


def seed_json_schema() -> dict[str, Any]:
    """The JSON Schema the seed file must match, generated from :class:`PriceSeed`."""

    schema = PriceSeed.model_json_schema(by_alias=True)
    return {"$schema": "https://json-schema.org/draft/2020-12/schema", **schema}


def parse_seed(text: str) -> PriceSeed:
    return PriceSeed.model_validate(json.loads(text))


@cache
def load_seed() -> PriceSeed:
    """The seed shipped inside the package. Startup fails loudly if it doesn't validate."""

    text = resources.files("mosaic_api").joinpath(SEED_RESOURCE).read_text(encoding="utf-8")
    return parse_seed(text)


# -- administrator-authored state ------------------------------------------------------------


class PriceVersion(Entity):
    """A price an administrator added, or a new version of a price already listed.

    Versions are only ever added. A version takes effect on ``effective_from`` and lasts until
    another version of the same price takes effect. Two versions with the same date are a
    correction: an administrator's beats a seeded one, and the one recorded later beats the
    other. ``deployment`` pins a price, such as a reservation's
    monthly amount, to one deployment, keyed ``{endpointId}/{deploymentName}``.
    """

    entity_type: Literal["priceVersion"] = "priceVersion"
    cloud: str
    publisher: str | None = None
    model: str
    aliases: list[str] = Field(default_factory=list)
    version: str | None = None
    deployment_type: str | None = None
    regions: list[str] | None = None
    deployment: str | None = None
    input_per_million: float | None = None
    cached_input_per_million: float | None = None
    output_per_million: float | None = None
    ptu_hourly: float | None = None
    monthly_amount: float | None = None
    effective_from: date
    source_url: str
    note: str
    # The seeded or earlier price this version was entered as an override of, for the history.
    overrides: str | None = None
    recorded_by: str


class DeploymentPricing(MosaicModel):
    """What an administrator says about a deployment MOSAIC can't read from Azure."""

    deployment_name: str = Field(min_length=1, max_length=64)
    deployment_type: str | None = Field(default=None, max_length=64)
    # PTUs, for a provisioned deployment.
    capacity: int | None = Field(default=None, ge=1, le=100_000)


class EndpointPricing(Entity):
    """An administrator's pricing facts for one model endpoint, audited on every change."""

    entity_type: Literal["endpointPricing"] = "endpointPricing"
    endpoint_id: str
    cloud: str | None = None
    region: str | None = None
    deployments: list[DeploymentPricing] = Field(default_factory=list)
    updated_by: str | None = None


def endpoint_pricing_id(tenant_id: str, endpoint_id: str) -> str:
    return deterministic_id("endpointpricing", tenant_id, endpoint_id)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class PriceCreate(MosaicModel):
    """A new price, or a new version of a listed one. Prices are in US dollars."""

    cloud: str = Field(pattern=_CLOUD_KEY_PATTERN)
    publisher: str | None = Field(default=None, max_length=80)
    model: str = Field(min_length=1, max_length=120)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    version: str | None = Field(default=None, max_length=64)
    deployment_type: str | None = Field(default=None, max_length=64)
    regions: list[str] | None = Field(default=None, max_length=80)
    deployment: str | None = Field(default=None, max_length=300)
    input_per_million: float | None = Field(default=None, ge=0, le=100_000)
    cached_input_per_million: float | None = Field(default=None, ge=0, le=100_000)
    output_per_million: float | None = Field(default=None, ge=0, le=100_000)
    ptu_hourly: float | None = Field(default=None, ge=0, le=100_000)
    monthly_amount: float | None = Field(default=None, ge=0, le=100_000_000)
    effective_from: date
    source_url: str = Field(pattern=r"^https://\S+$", max_length=2000)
    note: str = Field(min_length=1, max_length=500)
    overrides: str | None = Field(default=None, max_length=200)

    @field_validator("publisher", "version", "deployment", "overrides")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        return _clean_optional(value)

    @field_validator("model", "note")
    @classmethod
    def strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Enter a value")
        return stripped

    @field_validator("deployment_type")
    @classmethod
    def canonical_type(cls, value: str | None) -> str | None:
        return canonical_deployment_type(value)

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        for alias in value:
            stripped = alias.strip()
            if stripped and stripped.casefold() not in {item.casefold() for item in cleaned}:
                cleaned.append(stripped)
        return cleaned

    @field_validator("regions")
    @classmethod
    def clean_regions(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = sorted({region for item in value if (region := normalize_region(item))})
        return cleaned or None

    @model_validator(mode="after")
    def validate_kind(self) -> "PriceCreate":
        token = any(
            value is not None
            for value in (
                self.input_per_million,
                self.cached_input_per_million,
                self.output_per_million,
            )
        )
        reserved = self.ptu_hourly is not None or self.monthly_amount is not None
        if token == reserved:
            raise ValueError(
                "Enter either prices per million tokens, or a PTU hourly rate or monthly amount"
            )
        if token and self.input_per_million is None:
            raise ValueError("A token price needs an input price")
        if self.monthly_amount is not None and self.deployment is None:
            raise ValueError("A monthly amount is for one deployment, so choose the deployment")
        if self.monthly_amount is not None and self.ptu_hourly is not None:
            raise ValueError("Enter a PTU hourly rate or a monthly amount, not both")
        if reserved and self.deployment is None and not is_provisioned(self.deployment_type):
            raise ValueError("A PTU rate needs a provisioned deployment type")
        if self.model == WILDCARD_MODEL and self.deployment is None and self.publisher is None:
            raise ValueError("A price for every model needs a publisher")
        return self


class DeploymentPricingUpdate(MosaicModel):
    deployment_name: str = Field(min_length=1, max_length=64)
    deployment_type: str | None = Field(default=None, max_length=64)
    capacity: int | None = Field(default=None, ge=1, le=100_000)

    @field_validator("deployment_type")
    @classmethod
    def canonical_type(cls, value: str | None) -> str | None:
        return canonical_deployment_type(value)


class EndpointPricingUpdate(MosaicModel):
    """The whole of an endpoint's pricing facts. Leave a field empty to use what MOSAIC detects."""

    cloud: str | None = Field(default=None, max_length=40)
    region: str | None = Field(default=None, max_length=60)
    deployments: list[DeploymentPricingUpdate] = Field(default_factory=list, max_length=50)

    @field_validator("cloud")
    @classmethod
    def validate_cloud(cls, value: str | None) -> str | None:
        cleaned = _clean_optional(value)
        if cleaned is None:
            return None
        folded = cleaned.casefold()
        if not CLOUD_KEY.fullmatch(folded):
            raise ValueError(
                "A cloud is up to 40 lowercase letters, digits and hyphens, such as government"
            )
        return folded

    @field_validator("region")
    @classmethod
    def validate_region(cls, value: str | None) -> str | None:
        return normalize_region(value)


# -- prices as MOSAIC matches them -----------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    title: str
    url: str
    retrieved_on: date | None = None


@dataclass(frozen=True)
class PriceEntry:
    """One version of one price, from the seed or an administrator."""

    id: str
    origin: PriceOrigin
    cloud: str
    publisher: str | None
    model: str
    aliases: tuple[str, ...]
    version: str | None
    deployment_type: str | None
    regions: tuple[str, ...] | None
    deployment: str | None
    input_per_million: float | None
    cached_input_per_million: float | None
    output_per_million: float | None
    ptu_hourly: float | None
    monthly_amount: float | None
    effective_from: date
    recorded_at: datetime
    recorded_by: str | None
    sources: tuple[SourceRef, ...]
    note: str | None = None
    overrides: str | None = None
    # Only seeded prices a refresh replaced end. Every other version lasts until the next one.
    effective_until: date | None = None

    @property
    def provisioned(self) -> bool:
        return self.ptu_hourly is not None or self.monthly_amount is not None

    def in_effect(self, day: date) -> bool:
        return self.effective_from <= day and (
            self.effective_until is None or day <= self.effective_until
        )

    @property
    def precedence(self) -> tuple[date, int, datetime]:
        """How versions of one price rank: the later date wins, then an administrator's version
        over a seeded one, then the one recorded later. So a correction keeps beating the price
        it corrects, whatever seed a later release ships."""

        return (self.effective_from, 1 if self.origin == "admin" else 0, self.recorded_at)

    @property
    def line_key(self) -> tuple[str, ...]:
        """What makes two versions the same price: everything but the amounts and the date."""

        return (
            _fold(self.cloud),
            _compact(self.publisher),
            _fold(self.model),
            _fold(self.version),
            _compact(self.deployment_type),
            ",".join(self.regions or ()),
            _fold(self.deployment),
        )

    @property
    def line_id(self) -> str:
        return deterministic_id("priceline", *self.line_key)


def seed_entries(seed: PriceSeed) -> list[PriceEntry]:
    sources = {source.id: source for source in seed.sources}
    recorded = datetime.combine(seed.last_updated, time.min, tzinfo=UTC)
    entries: list[PriceEntry] = []
    for price in seed.prices:
        entries.append(
            PriceEntry(
                id=price.id,
                origin="seed",
                cloud=price.cloud,
                publisher=price.publisher,
                model=price.model,
                aliases=tuple(price.aliases),
                version=price.version,
                deployment_type=canonical_deployment_type(price.deployment_type),
                regions=tuple(price.regions) if price.regions else None,
                deployment=None,
                input_per_million=price.input_per_million,
                cached_input_per_million=price.cached_input_per_million,
                output_per_million=price.output_per_million,
                ptu_hourly=price.ptu_hourly,
                monthly_amount=None,
                effective_from=price.effective_from,
                effective_until=price.effective_until,
                recorded_at=recorded,
                recorded_by=None,
                sources=tuple(
                    SourceRef(
                        sources[source_id].title,
                        sources[source_id].url,
                        sources[source_id].retrieved_on,
                    )
                    for source_id in price.source_ids
                ),
                note=price.notes,
            )
        )
    return entries


def version_entry(version: PriceVersion) -> PriceEntry:
    return PriceEntry(
        id=version.id,
        origin="admin",
        cloud=version.cloud,
        publisher=version.publisher,
        model=version.model,
        aliases=tuple(version.aliases),
        version=version.version,
        deployment_type=canonical_deployment_type(version.deployment_type),
        regions=tuple(version.regions) if version.regions else None,
        deployment=version.deployment,
        input_per_million=version.input_per_million,
        cached_input_per_million=version.cached_input_per_million,
        output_per_million=version.output_per_million,
        ptu_hourly=version.ptu_hourly,
        monthly_amount=version.monthly_amount,
        effective_from=version.effective_from,
        recorded_at=version.created_at,
        recorded_by=version.recorded_by,
        sources=(SourceRef("Entered by an administrator", version.source_url),),
        note=version.note,
        overrides=version.overrides,
    )


# -- deployments as MOSAIC prices them -------------------------------------------------------


@dataclass(frozen=True)
class DeploymentFacts:
    """What decides a deployment's price, and where each fact came from."""

    key: str
    endpoint_id: str
    endpoint_name: str
    deployment_name: str
    provider: str
    cloud: str | None
    cloud_source: CloudSource | None
    detected_cloud: str | None
    region: str | None
    publisher: str | None
    model: str | None
    version: str | None
    deployment_type: str | None
    deployment_type_source: Literal["observed", "admin"] | None
    capacity: int | None
    declared: bool = False
    # When Azure created the deployment, if MOSAIC read it.
    deployed_on: date | None = None

    @property
    def provisioned(self) -> bool:
        return is_provisioned(self.deployment_type)


def deployment_key(endpoint_id: str, deployment_name: str) -> str:
    return f"{endpoint_id}/{deployment_name}"


def deployment_facts(
    endpoint: ModelEndpoint,
    *,
    observed: ObservedModelDeployment | None = None,
    declared: DeclaredDeployment | None = None,
    settings: EndpointPricing | None = None,
) -> DeploymentFacts:
    """One deployment's facts: what Azure reports, then what an administrator filled in."""

    name = observed.deployment_name if observed else declared.deployment_name if declared else ""
    detected = detect_cloud(str(endpoint.endpoint))
    override = settings.cloud if settings else None
    region = (settings.region if settings else None) or normalize_region(
        endpoint.capabilities.location
    )
    authored = next(
        (
            item
            for item in (settings.deployments if settings else [])
            if item.deployment_name.casefold() == name.casefold()
        ),
        None,
    )
    deployment_type = canonical_deployment_type(observed.sku_name) if observed else None
    type_source: Literal["observed", "admin"] | None = "observed" if deployment_type else None
    capacity = observed.sku_capacity if observed else None
    if deployment_type is None and authored is not None and authored.deployment_type:
        deployment_type = canonical_deployment_type(authored.deployment_type)
        type_source = "admin"
        capacity = authored.capacity
    publisher = None
    if observed is not None and endpoint.provider != ModelProvider.OPENAI_COMPATIBLE:
        publisher = observed.model_publisher or observed.model_format
    return DeploymentFacts(
        key=deployment_key(endpoint.id, name),
        endpoint_id=endpoint.id,
        endpoint_name=endpoint.name,
        deployment_name=name,
        provider=str(endpoint.provider),
        cloud=override or detected,
        cloud_source="override" if override else "detected" if detected else None,
        detected_cloud=detected,
        region=region,
        publisher=publisher,
        model=(observed.model_name if observed else declared.model_name if declared else None),
        version=(
            observed.model_version if observed else declared.model_version if declared else None
        ),
        deployment_type=deployment_type,
        deployment_type_source=type_source,
        capacity=capacity,
        declared=declared is not None and observed is None,
        deployed_on=(
            observed.deployed_at.date() if observed and observed.deployed_at else None
        ),
    )


@dataclass(frozen=True)
class PriceMatch:
    entry: PriceEntry


@dataclass(frozen=True)
class Unpriced:
    reason: UnpricedReason
    message: str


def _describe(facts: DeploymentFacts) -> str:
    parts = [facts.model or "this model"]
    if facts.version:
        parts.append(facts.version)
    described = " ".join(parts)
    if facts.deployment_type:
        described += f" ({facts.deployment_type})"
    return described


def _model_score(entry: PriceEntry, model: str) -> int:
    folded = _fold(model)
    if _fold(entry.model) == folded:
        return 2
    if any(_fold(alias) == folded for alias in entry.aliases):
        return 1
    if entry.model == WILDCARD_MODEL:
        return 0
    return -1


def _specificity(entry: PriceEntry, model_score: int) -> tuple[int, ...]:
    return (
        1 if entry.deployment else 0,
        model_score,
        1 if entry.version else 0,
        1 if entry.regions else 0,
        1 if entry.deployment_type else 0,
        1 if entry.publisher else 0,
    )


class PriceBook:
    """Every price version, seeded and administrator-entered, ready to match deployments."""

    def __init__(
        self,
        entries: Iterable[PriceEntry],
        throughput: Iterable[PtuThroughput] = (),
    ) -> None:
        self.entries = list(entries)
        self.throughput = list(throughput)
        self._by_model: dict[str, list[PriceEntry]] = {}
        self._by_deployment: dict[str, list[PriceEntry]] = {}
        for entry in self.entries:
            if entry.deployment:
                self._by_deployment.setdefault(_fold(entry.deployment), []).append(entry)
                continue
            for name in (entry.model, *entry.aliases):
                self._by_model.setdefault(_fold(name), []).append(entry)
        changes = {entry.effective_from for entry in self.entries}
        changes |= {
            entry.effective_until + timedelta(days=1)
            for entry in self.entries
            if entry.effective_until is not None
        }
        self._changes = sorted(changes)

    def clouds(self) -> list[str]:
        custom = sorted({entry.cloud for entry in self.entries} - set(BUILT_IN_CLOUDS))
        return [*BUILT_IN_CLOUDS, *custom]

    def changes_within(self, first: date, last: date) -> bool:
        """Whether any price takes effect, or ends, after ``first`` and on or before ``last``."""

        return any(first < change <= last for change in self._changes)

    def _candidates(self, facts: DeploymentFacts) -> list[PriceEntry]:
        found = list(self._by_deployment.get(_fold(facts.key), []))
        if facts.model:
            found.extend(self._by_model.get(_fold(facts.model), []))
        found.extend(self._by_model.get(WILDCARD_MODEL, []))
        return found

    def _applies(self, entry: PriceEntry, facts: DeploymentFacts) -> int | None:
        """How well ``entry`` names this deployment's model, or None if it doesn't apply."""

        if entry.provisioned != facts.provisioned:
            return None
        if entry.deployment:
            return 3 if _fold(entry.deployment) == _fold(facts.key) else None
        if _fold(entry.cloud) != _fold(facts.cloud) or not facts.model:
            return None
        score = _model_score(entry, facts.model)
        if score < 0:
            return None
        if entry.publisher and facts.publisher and _compact(entry.publisher) != _compact(
            facts.publisher
        ):
            return None
        if entry.version and _fold(entry.version) != _fold(facts.version):
            return None
        if entry.deployment_type and _compact(entry.deployment_type) != _compact(
            facts.deployment_type
        ):
            return None
        if entry.regions and (facts.region is None or facts.region not in entry.regions):
            return None
        return score

    def match(self, facts: DeploymentFacts, day: date) -> PriceMatch | Unpriced:
        """The most specific price in effect on ``day``, or why there's none."""

        applicable: list[tuple[tuple[int, ...], PriceEntry]] = []
        for entry in self._candidates(facts):
            score = self._applies(entry, facts)
            if score is not None:
                applicable.append((_specificity(entry, score), entry))
        effective = [(rank, entry) for rank, entry in applicable if entry.in_effect(day)]
        if effective:
            _, entry = max(
                effective,
                key=lambda item: (item[0], *item[1].precedence),
            )
            if facts.provisioned and entry.monthly_amount is None and not facts.capacity:
                return Unpriced(
                    "noCapacity",
                    f"MOSAIC doesn't know how many PTUs {facts.deployment_name} has. Enter its "
                    "capacity, or a monthly amount for it.",
                )
            return PriceMatch(entry)
        if facts.cloud is None:
            return Unpriced(
                "noCloud",
                f"MOSAIC can't tell which cloud {facts.endpoint_name} is in. Set its cloud.",
            )
        if not facts.model:
            return Unpriced("noModel", f"MOSAIC doesn't know which model {facts.key} serves.")
        upcoming = [entry.effective_from for _, entry in applicable if entry.effective_from > day]
        if upcoming:
            return Unpriced(
                "notYetEffective",
                f"The first price for {_describe(facts)} takes effect on "
                f"{min(upcoming).isoformat()}.",
            )
        if applicable:
            ended = max(
                entry.effective_until for _, entry in applicable if entry.effective_until
            )
            return Unpriced(
                "noPrice",
                f"The last price for {_describe(facts)} ended on {ended.isoformat()}.",
            )
        if facts.deployment_type is None:
            return Unpriced(
                "noDeploymentType",
                f"MOSAIC doesn't know {facts.deployment_name}'s deployment type. Set it, or add a "
                "price for every deployment type.",
            )
        where = f" in {facts.region}" if facts.region else ""
        return Unpriced(
            "noPrice",
            f"No price for {_describe(facts)}{where} in {cloud_label(facts.cloud)}.",
        )

    def throughput_for(self, model: str | None, version: str | None) -> PtuThroughput | None:
        if not model:
            return None
        matches = [item for item in self.throughput if _fold(item.model) == _fold(model)]
        exact = [item for item in matches if item.version and _fold(item.version) == _fold(version)]
        if exact:
            return exact[0]
        general = [item for item in matches if not item.version]
        return general[0] if general else None


def build_book(
    versions: Iterable[PriceVersion] = (), seed: PriceSeed | None = None
) -> PriceBook:
    shipped = seed or load_seed()
    return PriceBook(
        [*seed_entries(shipped), *(version_entry(version) for version in versions)],
        shipped.ptu_throughput,
    )


# -- turning tokens into cost ----------------------------------------------------------------


@dataclass(frozen=True)
class DayRate:
    """How one deployment is charged on one day."""

    kind: Literal["tokens", "provisioned", "unpriced"]
    entry: PriceEntry | None = None
    # Provisioned only: the whole deployment's cost for the day.
    daily: float | None = None
    unpriced: Unpriced | None = None


UNKNOWN_DEPLOYMENT = Unpriced(
    "unknownDeployment", "MOSAIC doesn't know which deployment this API calls."
)


def token_amount(entry: PriceEntry, prompt: float, completion: float) -> float | None:
    """What these tokens cost at a price. Cached prompt tokens aren't logged, so none are."""

    if entry.input_per_million is None:
        return None
    if completion > 0 and entry.output_per_million is None:
        return None
    return (
        prompt * entry.input_per_million + completion * (entry.output_per_million or 0.0)
    ) / PER_MILLION


@dataclass
class Pricer:
    """Prices deployments by the day, for the deployments one request can see.

    Deployment keys are compared without case, because rollups record the key a publication was
    made with and an endpoint's sync can report its deployment's name differently.

    A provisioned deployment's reserved capacity is priced from the day Azure created it. When
    MOSAIC didn't read that, it's priced from ``since``: the day MOSAIC first saw a governed API
    fronting the deployment. Days before then have no cost, so a deployment created recently is
    never charged for the weeks before it existed.
    """

    book: PriceBook
    facts: Mapping[str, DeploymentFacts]
    since: Mapping[str, date] = field(default_factory=dict)
    _rates: dict[tuple[str, date], DayRate] = field(default_factory=dict)
    _folded: dict[str, DeploymentFacts] = field(default_factory=dict)
    _since: dict[str, date] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._folded = {key.casefold(): value for key, value in self.facts.items()}
        self._since = {key.casefold(): value for key, value in self.since.items()}

    def facts_for(self, key: str | None) -> DeploymentFacts | None:
        return None if key is None else self._folded.get(key.casefold())

    def reserved_from(self, key: str) -> date | None:
        facts = self._folded.get(key.casefold())
        if facts is not None and facts.deployed_on is not None:
            return facts.deployed_on
        return self._since.get(key.casefold())

    def rate(self, key: str | None, day: date) -> DayRate:
        if key is None:
            return DayRate("unpriced", unpriced=UNKNOWN_DEPLOYMENT)
        folded = key.casefold()
        cached = self._rates.get((folded, day))
        if cached is not None:
            return cached
        facts = self._folded.get(folded)
        rate: DayRate
        if facts is None:
            rate = DayRate(
                "unpriced",
                unpriced=Unpriced(
                    "unknownDeployment",
                    "MOSAIC has no record of this deployment on a registered endpoint.",
                ),
            )
        else:
            match = self.book.match(facts, day)
            start = self.reserved_from(key)
            if isinstance(match, Unpriced):
                rate = DayRate("unpriced", unpriced=match)
            elif match.entry.provisioned and start is not None and day < start:
                rate = DayRate(
                    "unpriced",
                    unpriced=Unpriced(
                        "beforeDeployment",
                        f"MOSAIC prices {facts.deployment_name}'s reserved capacity from "
                        f"{start.isoformat()}, when it was deployed.",
                    ),
                )
            elif match.entry.provisioned:
                entry = match.entry
                if entry.monthly_amount is not None:
                    daily = entry.monthly_amount / days_in_month(day)
                else:
                    daily = (entry.ptu_hourly or 0.0) * (facts.capacity or 0) * 24
                rate = DayRate("provisioned", entry=entry, daily=daily)
            else:
                rate = DayRate("tokens", entry=match.entry)
        self._rates[(folded, day)] = rate
        return rate

    def month_days(self, first: date, today: date) -> list[date]:
        """The days of ``first``'s month that have happened, today included."""

        last = min(month_last(first), today)
        return [first + timedelta(days=offset) for offset in range((last - first).days + 1)]

    def reserved_cost(self, key: str, month: date, today: date) -> float | None:
        """A provisioned deployment's cost for a month, or the month so far."""

        total: float | None = None
        for day in self.month_days(month_first(month), today):
            rate = self.rate(key, day)
            if rate.kind == "provisioned" and rate.daily is not None:
                total = (total or 0.0) + rate.daily
        return total

    def provisioned_keys(self) -> list[str]:
        return sorted(key.casefold() for key, facts in self.facts.items() if facts.provisioned)


def round_cost(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def sum_costs(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) if present else None
