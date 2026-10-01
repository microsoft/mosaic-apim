"""Build MOSAIC's model price seed from the Azure Retail Prices API.

Every price in ``apps/api/src/mosaic_api/data/model_prices.json`` comes from a public query of the
Retail Prices API (https://prices.azure.com/api/retail/prices), which needs no sign-in. This script
holds which meters make up which price, runs the queries, and writes the seed and its JSON Schema.
A price the API doesn't list is left out, never guessed. The API lists only today's prices, so a
refresh keeps each price it replaces, ending the day before its replacement takes effect, and the
days it covered keep their price. See docs/pricing.md::

    python -m scripts.price_seed            # rebuild the seed from today's prices
    python -m scripts.price_seed --check    # report what changed, and write nothing

Run it from the repository root with the workspace's virtual environment, which has
``mosaic_api`` installed.
"""

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from mosaic_api.pricing import PriceSeed, seed_json_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "apps" / "api" / "src" / "mosaic_api" / "data"
SEED_PATH = DATA_DIR / "model_prices.json"
SCHEMA_PATH = DATA_DIR / "model_prices.schema.json"
RETAIL_PRICES = "https://prices.azure.com/api/retail/prices"
SERVICE = "Foundry Models"
# Azure Government's regions. Every other region the API lists, except its legacy "Global"
# pseudo-region, is Azure Commercial's.
GOVERNMENT_REGIONS = frozenset({"usgovarizona", "usgovtexas", "usgovvirginia"})
# Token prices by region only for these types. The others are processed outside the resource's
# region, and the API lists one price for them in every region.
REGIONAL_TYPES = frozenset({"Standard", "ProvisionedManaged"})

Row = dict[str, Any]


@dataclass(frozen=True)
class Meters:
    """The meters one deployment type is billed on, by their Retail Prices API meter names."""

    input: str
    output: str | None = None
    cached: str | None = None
    # When the type's meters sit under another product than the model's others.
    product: str | None = None


@dataclass(frozen=True)
class Model:
    publisher: str
    model: str
    version: str | None
    product: str
    types: dict[str | None, Meters]
    aliases: tuple[str, ...] = ()
    notes: str | None = None


def _openai(
    model: str,
    version: str | None,
    types: dict[str | None, Meters],
    *,
    product: str = "Azure OpenAI",
    aliases: tuple[str, ...] = (),
) -> Model:
    return Model("OpenAI", model, version, product, types, aliases)


def _standard(prefix: str, glbl: str, dz: str, regnl: str, *, cached: str = "cached Inp") -> dict[
    str | None, Meters
]:
    """The common "<prefix> Inp glbl Tokens" family of meter names."""

    def meters(suffix: str) -> Meters:
        return Meters(
            f"{prefix} Inp {suffix} Tokens",
            f"{prefix} Outp {suffix} Tokens",
            f"{prefix} {cached} {suffix} Tokens",
        )

    return {
        "GlobalStandard": meters(glbl),
        "DataZoneStandard": meters(dz),
        "Standard": meters(regnl),
    }


def _batch(prefix: str, glbl: str, dz: str) -> dict[str | None, Meters]:
    return {
        "GlobalBatch": Meters(
            f"{prefix} Batch Inp {glbl} Tokens", f"{prefix} Batch Outp {glbl} Tokens"
        ),
        "DataZoneBatch": Meters(
            f"{prefix} Batch Inp {dz} Tokens", f"{prefix} Batch Outp {dz} Tokens"
        ),
    }


def _gpt5(prefix: str) -> dict[str | None, Meters]:
    return {
        "GlobalStandard": Meters(
            f"{prefix} Inpt Glbl 1M Tokens",
            f"{prefix} outpt Glbl 1M Tokens",
            f"{prefix} cchd Inpt Glbl 1M Tokens",
        ),
        "DataZoneStandard": Meters(
            f"{prefix} Inpt DZone 1M Tokens",
            f"{prefix} outpt DZone 1M Tokens",
            f"{prefix} cchd Inpt DZone 1M Tokens",
        ),
        "GlobalBatch": Meters(
            f"{prefix} Batch Inpt Glbl 1M Tokens",
            f"{prefix} Batch outpt Glbl 1M Tokens",
            f"{prefix} Batch Inpt cchd Glbl 1M Tokens",
        ),
        "DataZoneBatch": Meters(
            f"{prefix} Batch Inpt DZone 1M Tokens",
            f"{prefix} Batch outpt DZone 1M Tokens",
            f"{prefix} Batch Inpt cchd Dzone 1M Tokens",
        ),
    }


# Which meters make up each price. A version is given only where the meter names one: a meter
# without a version prices the model whatever its version.
CATALOG: list[Model] = [
    _openai(
        "gpt-4o",
        "2024-11-20",
        {
            **_standard("gpt 4o 1120", "glbl", "Data Zone", "regnl"),
            **_batch("gpt 4o 1120", "glbl", "DZ"),
        },
    ),
    _openai(
        "gpt-4o",
        "2024-08-06",
        {
            "GlobalStandard": Meters(
                "gpt-4o-0806-Inp-glbl Tokens",
                "gpt-4o-0806-Outp-glbl Tokens",
                "gpt 4o 0806 cached Inp glbl Tokens",
            ),
            "DataZoneStandard": Meters(
                "gpt 4o 0806 Inp Data Zone Tokens",
                "gpt 4o 0806 Outp Data Zone Tokens",
                "gpt 4o 0806 cached Inp Data Zone Tokens",
            ),
            "Standard": Meters(
                "gpt-4o-0806-Inp-regnl Tokens",
                "gpt-4o-0806-Outp-regnl Tokens",
                "gpt 4o 0806 cached Inp regnl Tokens",
            ),
            "GlobalBatch": Meters(
                "gpt-4o-0806-Batch-Inp-glbl Tokens", "gpt-4o-0806-Batch-Outp-glbl Tokens"
            ),
            "DataZoneBatch": Meters(
                "gpt 4o 0806 Batch Inp Data Zone Tokens", "gpt 4o 0806 Batch Outp Data Zone Tokens"
            ),
        },
    ),
    _openai(
        "gpt-4o",
        "2024-05-13",
        {
            "GlobalStandard": Meters(
                "gpt 4o 0513 Input global Tokens", "gpt 4o 0513 Output global Tokens"
            ),
            "DataZoneStandard": Meters(
                "gpt 4o 0513 Input Data Zone Tokens", "gpt 4o 0513 Output Data Zone Tokens"
            ),
            "Standard": Meters(
                "gpt 4o 0513 Input regional Tokens", "gpt 4o 0513 Output regional Tokens"
            ),
            **_batch("gpt 4o 0513", "glbl", "Data Zone"),
        },
    ),
    _openai(
        "gpt-4o-mini",
        "2024-07-18",
        {
            "GlobalStandard": Meters(
                "gpt-4o-mini-0718-Inp-glbl Tokens",
                "gpt-4o-mini-0718-Outp-glbl Tokens",
                "gpt 4o mini 0718 cached Inp glbl Tokens",
            ),
            "DataZoneStandard": Meters(
                "gpt 4o mini 0718 Inp Data Zone Tokens",
                "gpt 4o mini 0718 Outp Data Zone Tokens",
                "gpt 4o mini 0718 cached Inp Data Zone Tokens",
            ),
            "Standard": Meters(
                "gpt-4o-mini-0718-Inp-regnl Tokens",
                "gpt-4o-mini-0718-Outp-regnl Tokens",
                "gpt 4o mini 0718 cached Inp regnl Tokens",
            ),
            "GlobalBatch": Meters(
                "gpt-4o-mini-0718-Batch-Inp-glbl Tokens", "gpt-4o-mini-0718-Batch-Outp-glbl Tokens"
            ),
            "DataZoneBatch": Meters(
                "gpt 4o mini 0718 Batch Inp Data Zone Tokens",
                "gpt 4o mini0718 BatchOutp DataZone Tokens",
            ),
        },
    ),
    _openai(
        "gpt-4.1",
        None,
        {
            **_standard("gpt 4.1", "glbl", "Data Zone", "regnl"),
            **_batch("gpt 4.1", "glbl", "Data Zone"),
        },
    ),
    _openai(
        "gpt-4.1-mini",
        None,
        {
            "GlobalStandard": Meters(
                "gpt 4.1 mini Inp glbl Tokens",
                "gpt 4.1 mini Outp glbl Tokens",
                "gpt 4.1 mini cached Inp glbl Tokens",
            ),
            "DataZoneStandard": Meters(
                "gpt 4.1 mini Inp Data Zone Tokens",
                "gpt 4.1 mini Outp Data Zone Tokens",
                "gpt 4.1 mini cached Inp DZone Tokens",
            ),
            "Standard": Meters(
                "gpt 4.1 mini Inp regnl Tokens",
                "gpt 4.1 mini Outp regnl Tokens",
                "gpt 4.1 mini cached Inp regnl Tokens",
            ),
            **_batch("gpt 4.1 mini", "glbl", "DZone"),
        },
    ),
    _openai(
        "gpt-4.1-nano",
        None,
        {
            "GlobalStandard": Meters(
                "gpt 4.1 nano Inp glbl Tokens",
                "gpt 4.1 nano Outp glbl Tokens",
                "gpt 4.1 nano cached Inp glbl Tokens",
            ),
            "DataZoneStandard": Meters(
                "gpt 4.1 nano Inp Data Zone Tokens",
                "gpt 4.1 nano Outp Data Zone Tokens",
                "gpt 4.1 nano cached Inp DZone Tokens",
            ),
            "Standard": Meters(
                "gpt 4.1 nano Inp regnl Tokens",
                "gpt 4.1 nano Outp regnl Tokens",
                "gpt 4.1 nano cached Inp regnl Tokens",
            ),
            **_batch("gpt 4.1 nano", "glbl", "DZone"),
        },
    ),
    _openai(
        "o1",
        "2024-12-17",
        {
            **_standard("o1 1217", "glbl", "Data Zone", "regnl"),
            **_batch("o1 1217", "glbl", "Data Zone"),
        },
    ),
    _openai(
        "o3-mini",
        "2025-01-31",
        {
            "GlobalStandard": Meters(
                "o3 mini 0131 input glbl Tokens",
                "o3 mini 0131 output glbl Tokens",
                "o3 mini 0131 cached input glbl Tokens",
            ),
            "DataZoneStandard": Meters(
                "o3 mini 0131 input Data Zone Tokens",
                "o3 mini 0131 output Data Zone Tokens",
                "o3 mini 0131 cached input Data Zone Tokens",
            ),
            "Standard": Meters(
                "o3 mini 0131 input regnl Tokens",
                "o3 mini 0131 output regnl Tokens",
                "o3 mini 0131 cached input regnl Tokens",
            ),
            **_batch("o3 mini 0131", "glbl", "Data Zone"),
        },
    ),
    _openai(
        "o3",
        "2025-04-16",
        {
            **_standard("o3 0416", "glbl", "Data Zone", "regnl"),
            **_batch("o3 0416", "glbl", "Data Zone"),
        },
    ),
    _openai(
        "o4-mini",
        "2025-04-16",
        {
            "GlobalStandard": Meters(
                "o4-mini 0416 Inp glbl Tokens",
                "o4-mini 0416 Outp glbl Tokens",
                "o4-mini 0416 cached Inp glbl Tokens",
            ),
            "DataZoneStandard": Meters(
                "o4-mini 0416 Inp Data Zone Tokens",
                "o4-mini 0416 Outp Data Zone Tokens",
                "o4-mini 0416 cached Inp DataZone Tokens",
            ),
            "Standard": Meters(
                "o4-mini 0416 Inp regnl Tokens",
                "o4-mini 0416 Outp regnl Tokens",
                "o4-mini 0416 cached Inp regnl Tokens",
            ),
            **_batch("o4-mini 0416", "glbl", "Data Zone"),
        },
        product="Azure OpenAI Reasoning",
    ),
    _openai("gpt-5", None, _gpt5("GPT 5"), product="Azure OpenAI GPT5"),
    _openai("gpt-5-mini", None, _gpt5("GPT 5 Mini"), product="Azure OpenAI GPT5"),
    _openai("gpt-5-nano", None, _gpt5("GPT 5 Nano"), product="Azure OpenAI GPT5"),
    _openai(
        "gpt-35-turbo",
        "0125",
        {
            "GlobalStandard": Meters(
                "gpt-35-turbo16K-0125 Inp-glbl Tokens", "gpt-35-turbo16K-0125 Outp-glbl Tokens"
            ),
            "Standard": Meters(
                "gpt-35-turbo-16K-0125 Input-regional Tokens",
                "gpt-35-turbo-16K-0125 Output-regional Tokens",
            ),
        },
        aliases=("gpt-3.5-turbo",),
    ),
    _openai(
        "text-embedding-3-large",
        None,
        {
            "GlobalStandard": Meters("text-embedding-3-large-glbl Tokens"),
            "DataZoneStandard": Meters(
                "text embedding 3 large DZ Tokens", product="Azure OpenAI Embedding"
            ),
            "Standard": Meters("text-embedding-3-large-regional Tokens"),
        },
    ),
    _openai(
        "text-embedding-3-small",
        None,
        {
            "GlobalStandard": Meters("text-embedding-3-small-glbl Tokens"),
            "DataZoneStandard": Meters(
                "text embedding 3 small DZ Tokens", product="Azure OpenAI Embedding"
            ),
            "Standard": Meters("text-embedding-3-small-regional Tokens"),
        },
    ),
    _openai(
        "text-embedding-ada-002",
        None,
        {
            "GlobalStandard": Meters("embedding-ada-glbl Tokens"),
            "DataZoneStandard": Meters("embedding-ada-datazone Tokens"),
            "Standard": Meters("embedding-ada-regional Tokens"),
        },
    ),
    Model(
        "Microsoft",
        "Phi-4",
        None,
        "Azure Phi Models",
        {None: Meters("Phi-4-Input Tokens", "Phi-4-Output Tokens")},
        notes=(
            "The Retail Prices API lists one pay-as-you-go price for Phi-4, for any deployment "
            "type."
        ),
    ),
    Model(
        "DeepSeek",
        "DeepSeek-R1",
        None,
        "Azure Deepseek Models",
        {
            "GlobalStandard": Meters("R1 Inp glbl Tokens", "R1 Outp glbl Tokens"),
            "DataZoneStandard": Meters("R1 Inp DZone Tokens", "R1 Outp DZone Tokens"),
            "Standard": Meters("R1 Inp regnl Tokens", "R1 Outp regnl Tokens"),
        },
    ),
    Model(
        "Meta",
        "Llama-3.3-70B-Instruct",
        None,
        "Azure Llama Models",
        {
            "GlobalStandard": Meters(
                "Llama 3.3 70B Inp glbl Tokens", "Llama 3.3 70B Outp glbl Tokens"
            ),
            "DataZoneStandard": Meters(
                "Llama 3.3 70B Inp Dzone Tokens", "Llama 3.3 70B Outp Dzone Tokens"
            ),
            "Standard": Meters("Llama 3.3 70B Inp regnl Tokens", "Llama 3.3 70B Outp regnl Tokens"),
        },
    ),
]

# Provisioned throughput is billed per PTU an hour, the same for every model a product sells.
PROVISIONED_METERS = {
    "GlobalProvisionedManaged": "Provisioned Managed Global Unit",
    "DataZoneProvisionedManaged": "Provisioned Managed Data Zone Unit",
    "ProvisionedManaged": "Provisioned Managed Regional Unit",
}
PROVISIONED_PRODUCTS = {
    "OpenAI": "Azure OpenAI",
    "DeepSeek": "Azure Deepseek Models",
    "Meta": "Azure Llama Models",
    "Mistral AI": "Azure Mistral Models",
}

LEARN_SIZING = "learn-ptu-sizing"
LEARN_BILLING = "learn-ptu-billing"
DOCUMENT_SOURCES = [
    {
        "id": LEARN_SIZING,
        "title": "Determine PTU sizing for a workload, Microsoft Learn: input TPM per PTU and "
        "output-to-input ratio by model",
        "url": "https://learn.microsoft.com/azure/foundry/openai/how-to/"
        "provisioned-throughput-sizing",
    },
    {
        "id": LEARN_BILLING,
        "title": "Provisioned throughput billing and cost management, Microsoft Learn: PTUs are "
        "billed by the hour whether or not a deployment takes calls",
        "url": "https://learn.microsoft.com/azure/foundry/openai/concepts/"
        "provisioned-throughput-billing",
    },
]
# Microsoft Learn's per-model PTU throughput, for utilization. Copied from the sizing page's tables.
THROUGHPUT = [
    ("gpt-4o", 2_500, 4),
    ("gpt-4o-mini", 37_000, 4),
    ("o3-mini", 2_500, 4),
    ("o1", 230, 4),
    ("gpt-4.1", 3_000, 4),
    ("gpt-4.1-mini", 14_900, 4),
    ("gpt-4.1-nano", 59_400, 4),
    ("o3", 3_000, 4),
    ("o4-mini", 5_400, 4),
    ("gpt-5", 4_750, 8),
    ("gpt-5-mini", 23_750, 8),
    ("Llama-3.3-70B-Instruct", 8_450, 4),
]


def product_filter(product: str) -> str:
    return (
        f"serviceName eq '{SERVICE}' and productName eq '{product}' and priceType eq 'Consumption'"
    )


def product_url(product: str) -> str:
    return f"{RETAIL_PRICES}?$filter={urllib.parse.quote(product_filter(product), safe=chr(39))}"


def source_id(product: str) -> str:
    return "retail-" + "-".join(product.casefold().split())


def fetch(product: str, cache: Path | None) -> list[Row]:
    """Every consumption meter of one product in every region, following the API's pages."""

    cached = cache / f"{source_id(product)}.json" if cache else None
    if cached and cached.exists():
        return list(json.loads(cached.read_text(encoding="utf-8")))
    url: str | None = product_url(product)
    rows: list[Row] = []
    while url:
        with urllib.request.urlopen(url, timeout=120) as response:
            page = json.load(response)
        rows.extend(page.get("Items", []))
        url = page.get("NextPageLink")
    if cached:
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_text(json.dumps(rows), encoding="utf-8")
    return rows


def per_million(row: Row) -> Decimal:
    price = Decimal(str(row["retailPrice"]))
    unit = str(row["unitOfMeasure"]).strip()
    if unit == "1K":
        return price * 1000
    if unit == "1M":
        return price
    raise ValueError(f"Unexpected token unit {unit!r} on {row['meterName']}")


def per_hour(row: Row) -> Decimal:
    unit = str(row["unitOfMeasure"]).strip()
    if unit not in {"1/Hour", "1 Hour"}:
        raise ValueError(f"Unexpected PTU unit {unit!r} on {row['meterName']}")
    return Decimal(str(row["retailPrice"]))


def cloud_of(region: str) -> str | None:
    if not region or region == "Global":
        return None
    return "government" if region in GOVERNMENT_REGIONS else "commercial"


@dataclass
class Index:
    """Each meter's current price in each region, as the API lists it on the day it's read."""

    today: date
    rows: dict[tuple[str, str, str], Row] = field(default_factory=dict)

    def add(self, product: str, rows: Iterable[Row]) -> None:
        for row in rows:
            region = str(row.get("armRegionName") or "")
            if cloud_of(region) is None or float(row.get("tierMinimumUnits") or 0) > 0:
                continue
            effective = date.fromisoformat(str(row["effectiveStartDate"])[:10])
            if effective > self.today:
                continue
            key = (product, str(row["meterName"]), region)
            current = self.rows.get(key)
            if current is None or str(row["effectiveStartDate"]) > str(
                current["effectiveStartDate"]
            ):
                self.rows[key] = row

    def regions(self, product: str, meter: str, cloud: str) -> dict[str, Row]:
        return {
            region: row
            for (row_product, name, region), row in self.rows.items()
            if row_product == product and name == meter and cloud_of(region) == cloud
        }


def _number(value: Decimal | None) -> float | None:
    if value is None:
        return None
    return float(value.quantize(Decimal("0.000001")).normalize())


def _slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch == "." else "-" for ch in value.casefold()).strip("-")


def _entry_id(cloud: str, publisher: str, model: str, version: str | None, kind: str) -> str:
    return ".".join([cloud, _slug(publisher), _slug(model), version or "any", _slug(kind)])


def _grouped(
    regions: dict[str, tuple[tuple[Any, ...], date]], regional: bool
) -> list[tuple[tuple[Any, ...], date, list[str] | None]]:
    """Regions that share a price, and the day it took effect.

    A price that's the same everywhere needs no list of regions, unless its type is regional. It
    takes effect on the earliest day any region lists it, because a region added later has no
    earlier use to price. Listed regions are also grouped by that day, so a region whose price
    changed to match others is never priced at its new price before the day it changed.
    """

    prices = {price for price, _ in regions.values()}
    if len(prices) == 1 and not regional:
        return [(next(iter(prices)), min(day for _, day in regions.values()), None)]
    groups: dict[tuple[tuple[Any, ...], date], list[str]] = defaultdict(list)
    for region, key in regions.items():
        groups[key].append(region)
    ordered = sorted(groups.items(), key=lambda item: min(item[1]))
    return [(price, day, sorted(names)) for (price, day), names in ordered]


def model_prices(model: Model, index: Index) -> list[dict[str, Any]]:
    prices: list[dict[str, Any]] = []
    for cloud in ("commercial", "government"):
        for deployment_type, meters in model.types.items():
            product = meters.product or model.product
            inputs = index.regions(product, meters.input, cloud)
            if not inputs:
                continue
            outputs = index.regions(product, meters.output, cloud) if meters.output else {}
            cached = index.regions(product, meters.cached, cloud) if meters.cached else {}
            by_region: dict[str, tuple[tuple[Any, ...], date]] = {}
            for region, row in inputs.items():
                if meters.output and region not in outputs:
                    continue
                output_row = outputs.get(region)
                cached_row = cached.get(region)
                dates = [row, *(item for item in (output_row, cached_row) if item)]
                by_region[region] = (
                    (
                        per_million(row),
                        per_million(cached_row) if cached_row else None,
                        per_million(output_row) if output_row else None,
                    ),
                    max(date.fromisoformat(str(item["effectiveStartDate"])[:10]) for item in dates),
                )
            groups = _grouped(by_region, deployment_type in REGIONAL_TYPES)
            for index_number, (price, start, regions) in enumerate(groups):
                kind = deployment_type or "any-type"
                suffix = f".r{index_number + 1}" if regions and len(groups) > 1 else ""
                entry: dict[str, Any] = {
                    "id": _entry_id(cloud, model.publisher, model.model, model.version, kind)
                    + suffix,
                    "cloud": cloud,
                    "publisher": model.publisher,
                    "model": model.model,
                }
                if model.aliases:
                    entry["aliases"] = list(model.aliases)
                if model.version:
                    entry["version"] = model.version
                if deployment_type:
                    entry["deploymentType"] = deployment_type
                if regions:
                    entry["regions"] = regions
                entry["inputPerMillion"] = _number(price[0])
                if price[1] is not None:
                    entry["cachedInputPerMillion"] = _number(price[1])
                if price[2] is not None:
                    entry["outputPerMillion"] = _number(price[2])
                entry["effectiveFrom"] = start.isoformat()
                entry["sourceIds"] = [source_id(product)]
                meters_record: dict[str, str] = {"product": product, "input": meters.input}
                if meters.cached and price[1] is not None:
                    meters_record["cachedInput"] = meters.cached
                if meters.output:
                    meters_record["output"] = meters.output
                entry["retailMeters"] = meters_record
                if model.notes:
                    entry["notes"] = model.notes
                prices.append(entry)
    return prices


def provisioned_prices(index: Index) -> list[dict[str, Any]]:
    prices: list[dict[str, Any]] = []
    for publisher, product in PROVISIONED_PRODUCTS.items():
        for cloud in ("commercial", "government"):
            for deployment_type, meter in PROVISIONED_METERS.items():
                rows = index.regions(product, meter, cloud)
                if not rows:
                    continue
                by_region = {
                    region: (
                        (per_hour(row),),
                        date.fromisoformat(str(row["effectiveStartDate"])[:10]),
                    )
                    for region, row in rows.items()
                }
                groups = _grouped(by_region, deployment_type in REGIONAL_TYPES)
                for number, (price, start, regions) in enumerate(groups):
                    suffix = f".r{number + 1}" if regions and len(groups) > 1 else ""
                    entry: dict[str, Any] = {
                        "id": _entry_id(cloud, publisher, "all-models", None, deployment_type)
                        + suffix,
                        "cloud": cloud,
                        "publisher": publisher,
                        "model": "*",
                        "deploymentType": deployment_type,
                    }
                    if regions:
                        entry["regions"] = regions
                    entry["ptuHourly"] = _number(price[0])
                    entry["effectiveFrom"] = start.isoformat()
                    entry["sourceIds"] = [source_id(product), LEARN_BILLING]
                    entry["retailMeters"] = {"product": product, "provisioned": meter}
                    entry["notes"] = (
                        "The hourly rate for one PTU, for every model this publisher sells in "
                        "Azure. A reservation costs less; enter its monthly amount as a price for "
                        "the deployment."
                    )
                    prices.append(entry)
    return prices


def build(today: date, cache: Path | None = None) -> dict[str, Any]:
    products = sorted(
        {model.product for model in CATALOG}
        | {meters.product for model in CATALOG for meters in model.types.values() if meters.product}
        | set(PROVISIONED_PRODUCTS.values())
    )
    index = Index(today)
    for product in products:
        index.add(product, fetch(product, cache))
    prices: list[dict[str, Any]] = []
    for model in CATALOG:
        prices.extend(model_prices(model, index))
    prices.extend(provisioned_prices(index))
    prices.sort(key=lambda item: item["id"])
    cited = {source for price in prices for source in price["sourceIds"]}
    sources = [
        {
            "id": source_id(product),
            "title": f"Azure Retail Prices API: {SERVICE}, {product}, every region",
            "url": product_url(product),
            "retrievedOn": today.isoformat(),
        }
        for product in products
        if source_id(product) in cited
    ]
    sources.extend({**source, "retrievedOn": today.isoformat()} for source in DOCUMENT_SOURCES)
    seed = {
        "$schema": "./model_prices.schema.json",
        "schemaVersion": 1,
        "lastUpdated": today.isoformat(),
        "currency": "USD",
        "sources": sources,
        "prices": prices,
        "ptuThroughput": [
            {
                "model": model,
                "inputTokensPerMinutePerPtu": tpm,
                "outputToInputRatio": ratio,
                "sourceIds": [LEARN_SIZING],
            }
            for model, tpm, ratio in THROUGHPUT
        ],
    }
    PriceSeed.model_validate(seed)
    return seed


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


# The order a price's fields are written in, so a carried price reads like a rebuilt one.
FIELD_ORDER = (
    "id",
    "cloud",
    "publisher",
    "model",
    "aliases",
    "version",
    "deploymentType",
    "regions",
    "inputPerMillion",
    "cachedInputPerMillion",
    "outputPerMillion",
    "ptuHourly",
    "effectiveFrom",
    "effectiveUntil",
    "sourceIds",
    "retailMeters",
    "notes",
)
LINE_FIELDS = ("cloud", "publisher", "model", "version", "deploymentType")
AMOUNT_FIELDS = ("inputPerMillion", "cachedInputPerMillion", "outputPerMillion", "ptuHourly")
# What a carried price's ID adds to the ID it had when it was listed.
CARRIED_SUFFIX = re.compile(r"\.(?:last|until-\d{4}-\d{2}-\d{2})(?:-\d+)?$")


def _line(price: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(price.get(name) for name in LINE_FIELDS)


def _amounts(price: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(price.get(name) for name in AMOUNT_FIELDS)


def _start(price: dict[str, Any]) -> date:
    return date.fromisoformat(str(price["effectiveFrom"]))


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(name) == right.get(name) for name in FIELD_ORDER if name != "id")


def carry_forward(current: dict[str, Any], rebuilt: dict[str, Any]) -> list[str]:
    """Keep each price a refresh replaces, so the days it covered keep their price.

    The Retail Prices API lists only today's prices. A replaced price is kept with
    ``effectiveUntil`` set to the day before its replacement takes effect, and cites its sources
    as they were on the day they were read. A price the API no longer lists is kept without an
    end, since nothing replaces it. The API is the record: if it dates a replacement before the
    day the old price was last read, the refresh follows it and warns. Changes ``rebuilt`` in
    place, and says what it kept and what it warns about.
    """

    fresh = list(rebuilt["prices"])
    by_line: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for price in fresh:
        by_line[_line(price)].append(price)
    old_sources = {source["id"]: source for source in current.get("sources", [])}
    sources = {source["id"]: source for source in rebuilt["sources"]}
    added_sources: list[dict[str, Any]] = []
    used = {price["id"] for price in fresh}
    notes: list[str] = []

    def note(message: str) -> None:
        if message not in notes:
            notes.append(message)

    def dated(source_id: str) -> str:
        """The source as it was read for the old seed, under an ID that names the day."""

        source = old_sources[source_id]
        if sources.get(source_id) == source:
            return source_id
        stamp = str(source["retrievedOn"]).replace("-", "")
        named = source_id if source_id.endswith(f"-{stamp}") else f"{source_id}-{stamp}"
        if named not in sources:
            copy = {**source, "id": named}
            sources[named] = copy
            added_sources.append(copy)
        return named

    def carry(old: dict[str, Any], regions: list[str] | None, until: date | None) -> None:
        original = CARRIED_SUFFIX.sub("", str(old["id"]))
        if until is not None:
            base = f"{original}.until-{until.isoformat()}"
        elif old.get("effectiveUntil"):
            base = str(old["id"])
        else:
            base = f"{original}.last"
        identifier, number = base, 2
        while identifier in used:
            identifier, number = f"{base}-{number}", number + 1
        used.add(identifier)
        values = {
            **old,
            "id": identifier,
            "regions": regions,
            "effectiveUntil": until.isoformat() if until else old.get("effectiveUntil"),
            "sourceIds": [dated(source) for source in old["sourceIds"]],
        }
        carried = {name: values[name] for name in FIELD_ORDER if values.get(name) is not None}
        rebuilt["prices"].append(carried)
        ending = f" until {carried['effectiveUntil']}" if "effectiveUntil" in carried else ""
        note(f"kept {identifier}{ending}")

    def ended(old: dict[str, Any], replaced_on: date) -> date | None:
        """The old price's last day, or None when its replacement covers every day it did."""

        read = max(
            date.fromisoformat(str(old_sources[source]["retrievedOn"]))
            for source in old["sourceIds"]
        )
        if replaced_on <= read:
            note(
                f"warning: the API dates {old['id']}'s replacement {replaced_on.isoformat()}, "
                f"but it was still listed on {read.isoformat()}"
            )
        if replaced_on <= _start(old):
            return None
        return replaced_on - timedelta(days=1)

    for old in current.get("prices", []):
        if any(_same(old, price) for price in fresh):
            continue
        if old.get("effectiveUntil"):
            # Replaced by an earlier refresh, so it already ends.
            carry(old, old.get("regions"), None)
            continue
        line = by_line.get(_line(old), [])
        start = _start(old)
        regions = old.get("regions")
        if regions is None:
            successor = next((price for price in line if price.get("regions") is None), None)
            if successor is None:
                # Retired, or now priced by region. A regional price is more specific, so it
                # takes over from its own start.
                carry(old, None, None)
            elif _amounts(successor) == _amounts(old):
                if _start(successor) > start:
                    successor["effectiveFrom"] = old["effectiveFrom"]
            elif (until := ended(old, _start(successor))) is not None:
                carry(old, None, until)
            continue
        ends: dict[date | None, list[str]] = defaultdict(list)
        for region in regions:
            covering = [
                price
                for price in line
                if price.get("regions") is None or region in price["regions"]
            ]
            if not covering:
                ends[None].append(region)
                continue
            first = min(covering, key=_start)
            if _amounts(first) == _amounts(old):
                if _start(first) > start:
                    ends[_start(first) - timedelta(days=1)].append(region)
            elif (until := ended(old, _start(first))) is not None:
                ends[until].append(region)
        for until, names in sorted(ends.items(), key=lambda item: item[0] or date.max):
            carry(old, sorted(names), until)

    rebuilt["prices"].sort(key=lambda item: item["id"])
    rebuilt["sources"].extend(sorted(added_sources, key=lambda item: item["id"]))
    return notes


def _price_key(price: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        price.get(name)
        for name in (
            "inputPerMillion",
            "cachedInputPerMillion",
            "outputPerMillion",
            "ptuHourly",
            "effectiveFrom",
            "effectiveUntil",
            "regions",
        )
    )


def differences(current: dict[str, Any], rebuilt: dict[str, Any]) -> list[str]:
    before = {price["id"]: price for price in current.get("prices", [])}
    after = {price["id"]: price for price in rebuilt["prices"]}
    changes = [f"added {key}" for key in sorted(after.keys() - before.keys())]
    changes += [f"removed {key}" for key in sorted(before.keys() - after.keys())]
    changes += [
        f"changed {key}"
        for key in sorted(before.keys() & after.keys())
        if _price_key(before[key]) != _price_key(after[key])
    ]
    return changes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="Report changes; write nothing")
    parser.add_argument(
        "--cache",
        type=Path,
        help="Keep each product's query results here, and reuse them on the next run",
    )
    parser.add_argument(
        "--date", type=date.fromisoformat, help="The day to record as retrieved, UTC today if unset"
    )
    args = parser.parse_args(argv)
    today = args.date or datetime.now(UTC).date()
    rebuilt = build(today, args.cache)
    current = json.loads(SEED_PATH.read_text(encoding="utf-8")) if SEED_PATH.exists() else {}
    kept = carry_forward(current, rebuilt)
    PriceSeed.model_validate(rebuilt)
    changes = differences(current, rebuilt)
    if args.check:
        for change in [*changes, *kept]:
            print(change)
        print(f"{len(changes)} price changes against {SEED_PATH.name}")
        return 1 if changes else 0
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEED_PATH.write_text(_dump(rebuilt), encoding="utf-8", newline="\n")
    SCHEMA_PATH.write_text(_dump(seed_json_schema()), encoding="utf-8", newline="\n")
    print(f"Wrote {len(rebuilt['prices'])} prices to {SEED_PATH.relative_to(REPO_ROOT)}")
    for change in [*changes, *kept]:
        print(f"  {change}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
