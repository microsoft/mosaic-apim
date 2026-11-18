"""Shared Retail meter normalization and history preservation, used by CLI and runtime."""

import hashlib
import json
import re
import urllib.parse
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from mosaic_api.pricing import _compact, _fold

RETAIL_PRICES = "https://prices.azure.com/api/retail/prices"


SERVICE = "Foundry Models"


GOVERNMENT_REGIONS = frozenset({"usgovarizona", "usgovtexas", "usgovvirginia"})


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


LEARN_BILLING = "learn-ptu-billing"


def product_filter(product: str) -> str:
    product = product.replace("'", "''")
    return (
        f"serviceName eq '{SERVICE}' and productName eq '{product}' and priceType eq 'Consumption'"
    )


def product_url(product: str) -> str:
    return f"{RETAIL_PRICES}?$filter={urllib.parse.quote(product_filter(product), safe=chr(39))}"


def source_id(product: str) -> str:
    original = "retail-" + "-".join(product.casefold().split())
    if len(original) <= 80 and re.fullmatch(r"[a-z0-9-]+", original):
        return original
    slug = re.sub(r"[^a-z0-9]+", "-", product.casefold()).strip("-") or "product"
    return f"retail-{slug[:52]}-{hashlib.sha256(product.encode()).hexdigest()[:12]}"


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


def provisioned_prices(
    index: Index,
    *,
    products: dict[str, str] | None = None,
    meters: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Normalize explicit PTU product/meter mappings, retaining the CLI's default mappings."""

    products = PROVISIONED_PRODUCTS if products is None else products
    meters = PROVISIONED_METERS if meters is None else meters
    prices: list[dict[str, Any]] = []
    for publisher, product in products.items():
        for cloud in ("commercial", "government"):
            for deployment_type, meter in meters.items():
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


def region_starts(price: dict[str, Any], index: Index) -> dict[str, date]:
    """The day each region started charging a price, as the API dates its meters."""

    meters = price.get("retailMeters") or {}
    product = meters.get("product")
    starts: dict[str, date] = {}
    if not product:
        return starts
    for name in ("input", "output", "cachedInput", "provisioned"):
        meter = meters.get(name)
        if not meter:
            continue
        for region, row in index.regions(product, meter, price["cloud"]).items():
            day = date.fromisoformat(str(row["effectiveStartDate"])[:10])
            starts[region] = max(starts.get(region, day), day)
    return starts


def _dump(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False) + "\n"


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


CARRIED_SUFFIX = re.compile(r"\.(?:last|until-\d{4}-\d{2}-\d{2})(?:-\d+)?$")


def _line(price: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _fold(price.get("cloud")),
        _compact(price.get("publisher")),
        _fold(price.get("model")),
        _fold(price.get("version")),
        _compact(price.get("deploymentType")),
    )


def _amounts(price: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(price.get(name) for name in AMOUNT_FIELDS)


def _start(price: dict[str, Any]) -> date:
    return date.fromisoformat(str(price["effectiveFrom"]))


def _same(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return all(left.get(name) == right.get(name) for name in FIELD_ORDER if name != "id")


def carry_forward(
    current: dict[str, Any],
    rebuilt: dict[str, Any],
    index: Index | None = None,
    *,
    preserve_effective_dates: bool = False,
) -> list[str]:
    """Keep each price a refresh replaces, so the days it covered keep their price.

    The Retail Prices API lists only today's prices. A replaced price is kept with
    ``effectiveUntil`` set to the day before its replacement takes effect, and cites its sources
    as they were on the day they were read. A price the API no longer lists is kept without an
    end, since nothing replaces it. A price for every region can change region by region: given
    ``index``, the API's rows, each region keeps the old price until its own change. The API is
    the record: if it dates a replacement before the day the old price was last read, the
    refresh follows it and warns. Changes ``rebuilt`` in place, and says what it kept and what
    it warns about.
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
        if preserve_effective_dates and until is not None and old.get("effectiveUntil"):
            until = min(until, date.fromisoformat(old["effectiveUntil"]))
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

    starts_by_price: dict[str, dict[str, date]] = {}

    def begins(price: dict[str, Any], region: str) -> date:
        """When a price took over in one region. A regionless one starts region by region."""

        if price.get("regions") is not None or index is None:
            return _start(price)
        if price["id"] not in starts_by_price:
            starts_by_price[price["id"]] = region_starts(price, index)
        return max(_start(price), starts_by_price[price["id"]].get(region, _start(price)))

    for old in current.get("prices", []):
        if any(_same(old, price) for price in fresh):
            continue
        if old.get("effectiveUntil") and not preserve_effective_dates:
            # CLI refreshes keep ended history; reviewed runtime corrections may supersede it.
            carry(old, old.get("regions"), None)
            continue
        line = by_line.get(_line(old), [])
        start = _start(old)
        regions = old.get("regions")
        if regions is None:
            successor = min(
                (price for price in line if price.get("regions") is None),
                key=_start,
                default=None,
            )
            if successor is None:
                # Retired, or now priced by region. A regional price is more specific, so it
                # takes over from its own start.
                carry(old, None, None)
            elif _amounts(successor) == _amounts(old) and not preserve_effective_dates:
                if _start(successor) > start:
                    successor["effectiveFrom"] = old["effectiveFrom"]
            else:
                if (until := ended(old, _start(successor))) is not None:
                    carry(old, None, until)
                # The replacement starts where the first region changed. Regions that changed
                # later keep the old price until then, as regional prices that outrank it.
                later: dict[date, list[str]] = defaultdict(list)
                if index is not None:
                    for region in region_starts(successor, index):
                        if (day := begins(successor, region)) > _start(successor):
                            later[day].append(region)
                for day, names in sorted(later.items()):
                    if (until := ended(old, day)) is not None:
                        carry(old, sorted(names), until)
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
            first = min(covering, key=lambda price: begins(price, region))
            first_start = begins(first, region)
            if _amounts(first) == _amounts(old):
                if first_start > start:
                    ends[first_start - timedelta(days=1)].append(region)
            elif (until := ended(old, first_start)) is not None:
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
