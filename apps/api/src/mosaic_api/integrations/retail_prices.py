"""Bounded public Commercial Retail acquisition. No Azure credentials or billing APIs."""

import asyncio
import json
from datetime import date
from time import monotonic
from urllib.parse import urlsplit

import httpx

from mosaic_api.errors import UpstreamError, ValidationError
from mosaic_api.integrations.email.graph import _retry_after as parse_retry_after
from mosaic_api.pricing import PriceSeed
from mosaic_api.pricing_catalog import CatalogDefinition, CatalogPackage
from mosaic_api.retail_catalog import (
    Index,
    Meters,
    Model,
    cloud_of,
    model_prices,
    product_url,
    provisioned_prices,
    source_id,
)

MAX_PRODUCTS = 16
MAX_PAGES = 100
MAX_ROWS = 100_000
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
ACQUISITION_SECONDS = 120


def validate_retail_url(url: str) -> None:
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != "prices.azure.com"
        or parts.port not in (None, 443)
        or parts.path.rstrip("/") != "/api/retail/prices"
        or parts.username
        or parts.password
        or parts.fragment
    ):
        raise UpstreamError("Retail returned an unsafe continuation URL; no catalog was activated")


class RetailPricesAdapter:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def acquire(self, definitions: list[CatalogDefinition], today: date) -> CatalogPackage:
        selected = [
            item for item in definitions if item.cloud == "commercial" and item.meters is not None
        ]
        products = sorted({item.meters.product for item in selected if item.meters is not None})
        if not products or len(products) > MAX_PRODUCTS:
            raise ValidationError("Select between one and 16 Commercial product queries")
        try:
            async with asyncio.timeout(ACQUISITION_SECONDS):
                return await self._acquire(selected, products, today)
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise UpstreamError(
                "The bounded Retail acquisition timed out; the active catalog is unchanged"
            ) from exc
        except httpx.RequestError as exc:
            raise UpstreamError(
                "Retail could not be reached; retry explicitly or import a supported package"
            ) from exc
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise UpstreamError(
                "Retail returned invalid or unsupported rate dimensions; "
                "the active catalog is unchanged"
            ) from exc

    async def _acquire(
        self, definitions: list[CatalogDefinition], products: list[str], today: date
    ) -> CatalogPackage:
        index = Index(today)
        seen: set[str] = set()
        page_count = row_count = byte_count = 0
        started = monotonic()
        meter_versions: dict[tuple[str, str, str, str, str], tuple[str, str]] = {}
        mapped_meters = {
            (item.meters.product, name)
            for item in definitions
            if item.meters is not None
            for name in (
                item.meters.input,
                item.meters.output,
                item.meters.cached_input,
                item.meters.provisioned,
            )
            if name
        }
        for product in products:
            url: str | None = product_url(product)
            while url:
                validate_retail_url(url)
                if url in seen or page_count >= MAX_PAGES:
                    raise UpstreamError("Retail paging repeated or exceeded 100 pages")
                seen.add(url)
                for attempt in range(3):
                    body = bytearray()
                    async with self._client.stream(
                        "GET", url, timeout=10, follow_redirects=False, auth=None
                    ) as response:
                        if response.status_code in {429, 500, 502, 503, 504} and attempt < 2:
                            retry = response.headers.get("retry-after", "1")
                            parsed = parse_retry_after(retry)
                            if parsed is None:
                                raise UpstreamError("Retail returned an invalid Retry-After header")
                            wait = max(1, parsed + (0 if retry.strip().isdigit() else 1))
                            if wait > ACQUISITION_SECONDS - (monotonic() - started):
                                raise UpstreamError(
                                    "Retail Retry-After exceeds the acquisition budget"
                                )
                            await asyncio.sleep(max(1, wait))
                            continue
                        if response.status_code != 200:
                            raise UpstreamError(
                                f"Retail returned HTTP {response.status_code}; "
                                "no data was activated"
                            )
                        async for part in response.aiter_bytes():
                            byte_count += len(part)
                            if byte_count > MAX_RESPONSE_BYTES:
                                raise UpstreamError("Retail exceeded the 50 MiB response limit")
                            body.extend(part)
                    break
                try:
                    page = json.loads(body)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise UpstreamError("Retail returned an invalid JSON page") from exc
                if (
                    not isinstance(page, dict)
                    or not isinstance(page.get("Items"), list)
                    or "NextPageLink" not in page
                    or (
                        page["NextPageLink"] is not None
                        and not isinstance(page["NextPageLink"], str)
                    )
                ):
                    raise UpstreamError(
                        "Retail returned an incomplete page or pagination terminator"
                    )
                rows = page["Items"]
                if not rows or any(not isinstance(row, dict) for row in rows):
                    raise UpstreamError("Retail returned an empty or invalid product page")
                page_count += 1
                row_count += len(rows)
                if row_count > MAX_ROWS:
                    raise UpstreamError("Retail exceeded 100,000 rows; narrow the catalog scope")
                if any(
                    row.get("currencyCode") != "USD"
                    or row.get("productName") != product
                    or row.get("type") != "Consumption"
                    for row in rows
                ):
                    raise UpstreamError(
                        "Retail returned a different currency, product or price type"
                    )
                for row in rows:
                    if (
                        (product, str(row["meterName"])) in mapped_meters
                        and cloud_of(str(row["armRegionName"])) == "commercial"
                        and row.get("tierMinimumUnits") != 0
                    ):
                        raise UpstreamError(
                            "A mapped meter has conditional or unknown pricing tiers that the "
                            "shared pricer cannot distinguish; the active catalog is unchanged"
                        )
                    key = (
                        product,
                        str(row["meterName"]),
                        str(row["armRegionName"]),
                        str(row["effectiveStartDate"]),
                        str(row.get("tierMinimumUnits", 0)),
                    )
                    rate = (str(row["unitOfMeasure"]), str(row["retailPrice"]))
                    if key in meter_versions and meter_versions[key] != rate:
                        raise UpstreamError(
                            "Retail returned conflicting revisions for the same meter"
                        )
                    meter_versions[key] = rate
                try:
                    index.add(product, rows)
                except (KeyError, TypeError, ValueError) as exc:
                    raise UpstreamError(
                        "Retail returned a malformed meter; import nothing from this attempt"
                    ) from exc
                url = page["NextPageLink"] or None

        for definition in definitions:
            meters = definition.meters
            if meters is not None and definition.kind == "tokens" and definition.requires_output:
                inputs = index.regions(meters.product, meters.input or "", "commercial")
                outputs = index.regions(meters.product, meters.output or "", "commercial")
                if inputs.keys() != outputs.keys():
                    raise UpstreamError(
                        "A required input/output meter pair has incomplete regional coverage; "
                        "the active catalog is unchanged"
                    )
        prices = []
        seen_prices: set[str] = set()
        for definition in definitions:
            meters = definition.meters
            if meters is None:
                continue
            if definition.kind == "ptu":
                candidates = [
                    {
                        **item,
                        "model": definition.model,
                        "version": definition.version,
                        "aliases": definition.aliases,
                        "notes": definition.notes,
                    }
                    for item in provisioned_prices(
                        index,
                        products={definition.publisher: meters.product},
                        meters={definition.deployment_type or "": meters.provisioned or ""},
                    )
                ]
            else:
                candidates = model_prices(
                    Model(
                        definition.publisher,
                        definition.model,
                        definition.version,
                        meters.product,
                        {
                            definition.deployment_type: Meters(
                                meters.input or "", meters.output, meters.cached_input
                            )
                        },
                        tuple(definition.aliases),
                        definition.notes,
                    ),
                    index,
                )
            for candidate_number, item in enumerate(candidates, 1):
                if item["cloud"] != "commercial":
                    continue
                if definition.regions is not None:
                    covered = sorted(
                        set(item.get("regions") or definition.regions) & set(definition.regions)
                    )
                    if not covered:
                        continue
                    item = {**item, "regions": covered}
                item = {**item, "id": f"retail.{definition.key}.r{candidate_number}"}
                if item["id"] not in seen_prices:
                    seen_prices.add(item["id"])
                    # PTU billing guidance is not itself a numeric rate source.
                    item = {**item, "sourceIds": [source_id(meters.product)]}
                    prices.append(item)
        if not prices:
            raise UpstreamError(
                "No complete supported rates were found; the active catalog is unchanged"
            )
        sources = [
            {
                "id": source_id(product),
                "title": f"Azure Retail Prices API: {product}, Commercial scope",
                "url": product_url(product),
                "retrievedOn": today.isoformat(),
            }
            for product in products
        ]
        return CatalogPackage(
            catalog=definitions,
            price_list=PriceSeed.model_validate(
                {
                    "schemaVersion": 1,
                    "currency": "USD",
                    "lastUpdated": today,
                    "sources": sources,
                    "prices": prices,
                }
            ),
        )
