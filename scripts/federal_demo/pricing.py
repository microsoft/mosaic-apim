"""Exact retail meters for the estate's bounded text-only profiles, never global aliases."""

import json
import math
from datetime import UTC, datetime
from urllib.parse import urlencode
from urllib.request import urlopen

from scripts.federal_demo.operator import OperationError

RETAIL = "https://prices.azure.com/api/retail/prices"


def meter_names(model: str, sku: str) -> tuple[str, str, str]:
    if sku not in ("GlobalStandard", "DataZoneStandard"):
        raise OperationError("Only reviewed pay-as-you-go demo SKUs are supported")
    if model in ("gpt-5.6-luna", "gpt-5.6-terra"):
        prefix = model.removeprefix("gpt-").replace("-", " ")
        zone = "Gl" if sku == "GlobalStandard" else "DZ"
        return (
            f"{prefix} ShortCo Inp Std {zone} 1M Tokens",
            f"{prefix} ShortCo Opt Std {zone} 1M Tokens",
            f"{prefix} ShortCo Cd Inp Std {zone} 1M Tokens",
        )
    if model == "gpt-5.1":
        zone = "Gl" if sku == "GlobalStandard" else "Dz"
        return (
            f"GPT 5.1 inp {zone} 1M Tokens",
            f"GPT 5.1 opt {zone} 1M Tokens",
            f"GPT 5.1 cd inp {zone} 1M Tokens",
        )
    raise OperationError("Unknown model; no similar-model pricing fallback is permitted")


def select_quote(model: str, sku: str, rows: list[dict], source_url: str) -> dict:
    names = meter_names(model, sku)
    amounts = []
    starts = []
    for name in names:
        found = [
            row
            for row in rows
            if row.get("meterName") == name
            and row.get("currencyCode") == "USD"
            and row.get("unitOfMeasure") == "1M"
            and row.get("type") == "Consumption"
            and row.get("isPrimaryMeterRegion") is True
            and row.get("productName") == "Azure OpenAI GPT5"
        ]
        if not found or len({row["retailPrice"] for row in found}) != 1:
            raise OperationError("Missing or ambiguous exact USD token meter")
        price = found[0]["retailPrice"]
        if (
            not isinstance(price, (int, float))
            or isinstance(price, bool)
            or not math.isfinite(price)
            or price <= 0
        ):
            raise OperationError("Unexpected token price; explicit review required")
        amounts.append(price)
        starts.extend(row["effectiveStartDate"] for row in found)
    return {
        "model": model,
        "deploymentType": sku,
        "currency": "USD",
        "inputPerMillion": amounts[0],
        "outputPerMillion": amounts[1],
        "cachedInputPerMillion": amounts[2],
        "meterNames": list(names),
        "sourceUrl": source_url,
        "retrievedOn": datetime.now(UTC).date().isoformat(),
        "effectiveFrom": max(starts)[:10],
        "requiresVerifiedShortContextEnvelope": model != "gpt-5.1",
    }


def fetch_quote(model: str, sku: str) -> dict:
    names = meter_names(model, sku)
    filters = " or ".join(f"meterName eq '{name}'" for name in names)
    query = urlencode(
        {
            "api-version": "2023-01-01-preview",
            "$filter": "serviceName eq 'Foundry Models' and productName eq 'Azure OpenAI GPT5' "
            f"and armRegionName eq 'eastus2' and ({filters})",
        }
    )
    source_url = f"{RETAIL}?{query}"
    with urlopen(source_url, timeout=60) as response:
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise OperationError("Retail response exceeds the bounded size")
        page = json.loads(raw)
    if page.get("NextPageLink"):
        raise OperationError("Exact-meter query unexpectedly paginated; review before using prices")
    return select_quote(model, sku, page["Items"], source_url)
