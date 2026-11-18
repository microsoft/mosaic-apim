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
import sys
import urllib.parse
import urllib.request
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from mosaic_api.pricing import PriceSeed, seed_json_schema
from mosaic_api.pricing_catalog import packaged_models
from mosaic_api.retail_catalog import (
    AMOUNT_FIELDS as AMOUNT_FIELDS,
)
from mosaic_api.retail_catalog import (
    CARRIED_SUFFIX as CARRIED_SUFFIX,
)
from mosaic_api.retail_catalog import (
    FIELD_ORDER as FIELD_ORDER,
)
from mosaic_api.retail_catalog import (
    GOVERNMENT_REGIONS as GOVERNMENT_REGIONS,
)
from mosaic_api.retail_catalog import (
    LEARN_BILLING as LEARN_BILLING,
)
from mosaic_api.retail_catalog import (
    LINE_FIELDS as LINE_FIELDS,
)
from mosaic_api.retail_catalog import (
    PROVISIONED_METERS as PROVISIONED_METERS,
)
from mosaic_api.retail_catalog import (
    PROVISIONED_PRODUCTS as PROVISIONED_PRODUCTS,
)
from mosaic_api.retail_catalog import (
    REGIONAL_TYPES as REGIONAL_TYPES,
)
from mosaic_api.retail_catalog import (
    RETAIL_PRICES as RETAIL_PRICES,
)
from mosaic_api.retail_catalog import (
    SERVICE as SERVICE,
)
from mosaic_api.retail_catalog import (
    Index as Index,
)
from mosaic_api.retail_catalog import (
    Meters as Meters,
)
from mosaic_api.retail_catalog import (
    Model as Model,
)
from mosaic_api.retail_catalog import (
    Row as Row,
)
from mosaic_api.retail_catalog import (
    _amounts as _amounts,
)
from mosaic_api.retail_catalog import (
    _dump as _dump,
)
from mosaic_api.retail_catalog import (
    _entry_id as _entry_id,
)
from mosaic_api.retail_catalog import (
    _grouped as _grouped,
)
from mosaic_api.retail_catalog import (
    _line as _line,
)
from mosaic_api.retail_catalog import (
    _number as _number,
)
from mosaic_api.retail_catalog import (
    _price_key as _price_key,
)
from mosaic_api.retail_catalog import (
    _same as _same,
)
from mosaic_api.retail_catalog import (
    _slug as _slug,
)
from mosaic_api.retail_catalog import (
    _start as _start,
)
from mosaic_api.retail_catalog import (
    carry_forward as carry_forward,
)
from mosaic_api.retail_catalog import (
    cloud_of as cloud_of,
)
from mosaic_api.retail_catalog import (
    differences as differences,
)
from mosaic_api.retail_catalog import (
    model_prices as model_prices,
)
from mosaic_api.retail_catalog import (
    per_hour as per_hour,
)
from mosaic_api.retail_catalog import (
    per_million as per_million,
)
from mosaic_api.retail_catalog import (
    product_filter as product_filter,
)
from mosaic_api.retail_catalog import (
    product_url as product_url,
)
from mosaic_api.retail_catalog import (
    provisioned_prices as provisioned_prices,
)
from mosaic_api.retail_catalog import (
    region_starts as region_starts,
)
from mosaic_api.retail_catalog import (
    source_id as source_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "apps" / "api" / "src" / "mosaic_api" / "data"
SEED_PATH = DATA_DIR / "model_prices.json"
SCHEMA_PATH = DATA_DIR / "model_prices.schema.json"
# Azure Government's regions. Every other region the API lists, except its legacy "Global"
# pseudo-region, is Azure Commercial's.
# Token prices by region only for these types. The others are processed outside the resource's
# region, and the API lists one price for them in every region.


# Which meters make up each price. A version is given only where the meter names one: a meter
# without a version prices the model whatever its version.
CATALOG: list[Model] = packaged_models()

# Provisioned throughput is billed per PTU an hour, the same for every model a product sells.

LEARN_SIZING = "learn-ptu-sizing"
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


def build(today: date, cache: Path | None = None) -> dict[str, Any]:
    return build_with_index(today, cache)[0]


def build_with_index(today: date, cache: Path | None = None) -> tuple[dict[str, Any], Index]:
    """The seed, and the API's rows it was built from, which a refresh reads regions' starts
    from."""

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
    return seed, index


# The order a price's fields are written in, so a carried price reads like a rebuilt one.
# What a carried price's ID adds to the ID it had when it was listed.


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
    rebuilt, index = build_with_index(today, args.cache)
    current = json.loads(SEED_PATH.read_text(encoding="utf-8")) if SEED_PATH.exists() else {}
    kept = carry_forward(current, rebuilt, index)
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
