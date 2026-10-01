"""The price seed builder turns Retail Prices API meters into sourced, dated prices."""

import unittest
from datetime import date
from typing import Any

from mosaic_api.pricing import DeploymentFacts, PriceMatch, PriceSeed, build_book

from scripts.price_seed import (
    Index,
    Meters,
    Model,
    carry_forward,
    differences,
    model_prices,
    product_url,
    provisioned_prices,
    source_id,
)


def _row(meter: str, region: str, price: float, unit: str = "1K", start: str = "2025-01-01"):
    return {
        "meterName": meter,
        "armRegionName": region,
        "retailPrice": price,
        "unitOfMeasure": unit,
        "effectiveStartDate": f"{start}T00:00:00Z",
        "tierMinimumUnits": 0.0,
    }


MODEL = Model(
    "OpenAI",
    "gpt-test",
    "2026-01-01",
    "Azure OpenAI",
    {
        "GlobalStandard": Meters("Test Inp glbl Tokens", "Test Outp glbl Tokens", "Test cd glbl"),
        "Standard": Meters("Test Inp regnl Tokens", "Test Outp regnl Tokens"),
    },
)


class PriceSeedTests(unittest.TestCase):
    def test_one_price_everywhere_needs_no_region_list(self) -> None:
        index = Index(date(2026, 9, 30))
        index.add(
            "Azure OpenAI",
            [
                _row("Test Inp glbl Tokens", "eastus", 0.0025, start="2025-02-01"),
                _row("Test Inp glbl Tokens", "westeurope", 0.0025, start="2025-03-01"),
                _row("Test Outp glbl Tokens", "eastus", 0.01),
                _row("Test Outp glbl Tokens", "westeurope", 0.01),
                _row("Test cd glbl", "eastus", 1.25, unit="1M"),
                _row("Test cd glbl", "westeurope", 1.25, unit="1M"),
            ],
        )

        [price] = model_prices(MODEL, index)

        self.assertEqual(price["id"], "commercial.openai.gpt-test.2026-01-01.globalstandard")
        self.assertNotIn("regions", price)
        self.assertEqual(
            (price["inputPerMillion"], price["cachedInputPerMillion"], price["outputPerMillion"]),
            (2.5, 1.25, 10.0),
        )
        # The earliest day any region lists every part of the price.
        self.assertEqual(price["effectiveFrom"], "2025-02-01")
        self.assertEqual(price["sourceIds"], [source_id("Azure OpenAI")])

    def test_regional_prices_are_grouped_by_region_and_clouds_kept_apart(self) -> None:
        index = Index(date(2026, 9, 30))
        index.add(
            "Azure OpenAI",
            [
                _row("Test Inp regnl Tokens", "eastus", 0.00275),
                _row("Test Inp regnl Tokens", "eastus2", 0.00275),
                _row("Test Inp regnl Tokens", "swedencentral", 0.003025),
                _row("Test Inp regnl Tokens", "usgovvirginia", 0.003438),
                _row("Test Outp regnl Tokens", "eastus", 0.011),
                _row("Test Outp regnl Tokens", "eastus2", 0.011),
                _row("Test Outp regnl Tokens", "swedencentral", 0.0121),
                _row("Test Outp regnl Tokens", "usgovvirginia", 0.01375),
                # A price that hasn't taken effect yet, and a legacy pseudo-region, are left out.
                _row("Test Inp regnl Tokens", "westus", 0.002, start="2027-01-01"),
                _row("Test Inp regnl Tokens", "Global", 0.001),
            ],
        )

        prices = {price["id"]: price for price in model_prices(MODEL, index)}

        east = prices["commercial.openai.gpt-test.2026-01-01.standard.r1"]
        self.assertEqual(east["regions"], ["eastus", "eastus2"])
        self.assertEqual((east["inputPerMillion"], east["outputPerMillion"]), (2.75, 11.0))
        sweden = prices["commercial.openai.gpt-test.2026-01-01.standard.r2"]
        self.assertEqual(sweden["regions"], ["swedencentral"])
        government = prices["government.openai.gpt-test.2026-01-01.standard"]
        self.assertEqual(government["regions"], ["usgovvirginia"])
        self.assertEqual(government["inputPerMillion"], 3.438)
        self.assertEqual(len(prices), 3)

    def test_ptu_rates_cite_the_api_and_the_billing_guidance(self) -> None:
        index = Index(date(2026, 9, 30))
        index.add(
            "Azure OpenAI",
            [
                _row("Provisioned Managed Global Unit", "eastus", 1.0, unit="1/Hour"),
                _row("Provisioned Managed Global Unit", "westus", 1.0, unit="1/Hour"),
            ],
        )

        [price] = provisioned_prices(index)

        self.assertEqual(price["model"], "*")
        self.assertEqual(price["ptuHourly"], 1.0)
        self.assertEqual(price["sourceIds"], [source_id("Azure OpenAI"), "learn-ptu-billing"])

    def test_changes_are_reported_by_price_id(self) -> None:
        before = {"prices": [{"id": "a", "inputPerMillion": 1.0}, {"id": "b"}]}
        after = {"prices": [{"id": "a", "inputPerMillion": 2.0}, {"id": "c"}]}
        self.assertEqual(differences(before, after), ["added c", "removed b", "changed a"])

    def test_listed_regions_are_grouped_by_the_day_their_price_took_effect(self) -> None:
        index = Index(date(2026, 9, 30))
        index.add(
            "Azure OpenAI",
            [
                _row("Test Inp regnl Tokens", "eastus", 0.00275, start="2025-01-01"),
                _row("Test Outp regnl Tokens", "eastus", 0.011, start="2025-01-01"),
                # Sweden's price changed to match East US's later, so it isn't backdated.
                _row("Test Inp regnl Tokens", "swedencentral", 0.00275, start="2026-03-01"),
                _row("Test Outp regnl Tokens", "swedencentral", 0.011, start="2026-03-01"),
            ],
        )

        prices = sorted(model_prices(MODEL, index), key=lambda price: price["id"])

        self.assertEqual(
            [(price["regions"], price["effectiveFrom"]) for price in prices],
            [(["eastus"], "2025-01-01"), (["swedencentral"], "2026-03-01")],
        )

    def test_each_query_is_a_public_retail_prices_url(self) -> None:
        url = product_url("Azure OpenAI")
        self.assertTrue(url.startswith("https://prices.azure.com/api/retail/prices?$filter="))
        self.assertIn("productName%20eq%20'Azure%20OpenAI'", url)


SOURCE = {
    "id": "retail-azure-openai",
    "title": "Azure Retail Prices API: Foundry Models, Azure OpenAI, every region",
    "url": product_url("Azure OpenAI"),
}


def _seed(day: str, *prices: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "lastUpdated": day,
        "currency": "USD",
        "sources": [{**SOURCE, "retrievedOn": day}],
        "prices": [dict(price) for price in prices],
    }


def _price(
    identifier: str,
    amount: float,
    start: str,
    regions: list[str] | None = None,
    deployment_type: str = "GlobalStandard",
) -> dict[str, Any]:
    price: dict[str, Any] = {
        "id": identifier,
        "cloud": "commercial",
        "publisher": "OpenAI",
        "model": "gpt-test",
        "deploymentType": deployment_type,
        "inputPerMillion": amount,
        "outputPerMillion": amount * 4,
        "effectiveFrom": start,
        "sourceIds": [SOURCE["id"]],
    }
    if regions:
        price["regions"] = regions
    return price


def _facts(
    region: str = "eastus", deployment_type: str = "GlobalStandard", version: str | None = None
) -> DeploymentFacts:
    return DeploymentFacts(
        key="endpoint/gpt-test",
        endpoint_id="endpoint",
        endpoint_name="Contoso",
        deployment_name="gpt-test",
        provider="azureOpenAi",
        cloud="commercial",
        cloud_source="detected",
        detected_cloud="commercial",
        region=region,
        publisher="OpenAI",
        model="gpt-test",
        version=version,
        deployment_type=deployment_type,
        deployment_type_source="observed",
        capacity=None,
    )


def _input_price(seed: dict[str, Any], facts: DeploymentFacts, day: date) -> float | None:
    match = build_book(seed=PriceSeed.model_validate(seed)).match(facts, day)
    return match.entry.input_per_million if isinstance(match, PriceMatch) else None


class CarryForwardTests(unittest.TestCase):
    def test_a_replaced_price_still_prices_the_days_before_its_replacement(self) -> None:
        current = _seed("2026-09-30", _price("gpt", 2.5, "2025-01-01"))
        rebuilt = _seed("2026-12-31", _price("gpt", 2.0, "2026-12-01"))

        kept = carry_forward(current, rebuilt)

        self.assertEqual(kept, ["kept gpt.until-2026-11-30 until 2026-11-30"])
        old = next(price for price in rebuilt["prices"] if price["id"] != "gpt")
        self.assertEqual(old["effectiveUntil"], "2026-11-30")
        # The old price cites the query as it was read then.
        self.assertEqual(old["sourceIds"], ["retail-azure-openai-20260930"])
        dated = next(s for s in rebuilt["sources"] if s["id"] == "retail-azure-openai-20260930")
        self.assertEqual((dated["retrievedOn"], dated["url"]), ("2026-09-30", SOURCE["url"]))
        self.assertEqual(_input_price(rebuilt, _facts(), date(2026, 11, 30)), 2.5)
        self.assertEqual(_input_price(rebuilt, _facts(), date(2026, 12, 1)), 2.0)

    def test_an_unchanged_price_keeps_its_earliest_start(self) -> None:
        current = _seed("2026-09-30", _price("gpt", 2.5, "2025-01-01"))
        # The region that listed it first no longer does.
        rebuilt = _seed("2026-12-31", _price("gpt", 2.5, "2025-06-01"))

        self.assertEqual(carry_forward(current, rebuilt), [])
        self.assertEqual(
            [(price["id"], price["effectiveFrom"]) for price in rebuilt["prices"]],
            [("gpt", "2025-01-01")],
        )

    def test_a_price_that_changed_in_one_region_is_kept_only_there(self) -> None:
        current = _seed(
            "2026-09-30",
            _price("gpt.std", 2.75, "2025-01-01", ["eastus", "swedencentral"], "Standard"),
        )
        rebuilt = _seed(
            "2026-12-31",
            _price("gpt.std.r1", 2.75, "2025-01-01", ["eastus"], "Standard"),
            _price("gpt.std.r2", 3.0, "2026-12-01", ["swedencentral"], "Standard"),
        )

        carry_forward(current, rebuilt)

        old = next(price for price in rebuilt["prices"] if price.get("effectiveUntil"))
        self.assertEqual((old["regions"], old["effectiveUntil"]), (["swedencentral"], "2026-11-30"))
        self.assertEqual(len(rebuilt["prices"]), 3)
        sweden = _facts("swedencentral", "Standard")
        self.assertEqual(_input_price(rebuilt, sweden, date(2026, 11, 30)), 2.75)
        self.assertEqual(_input_price(rebuilt, sweden, date(2026, 12, 1)), 3.0)
        east = _facts("eastus", "Standard")
        self.assertEqual(_input_price(rebuilt, east, date(2026, 12, 1)), 2.75)

    def test_a_replacement_dated_before_the_last_read_is_followed_and_reported(self) -> None:
        current = _seed("2026-09-30", _price("gpt", 2.5, "2025-01-01"))
        rebuilt = _seed("2026-12-31", _price("gpt", 2.0, "2026-08-01"))

        notes = carry_forward(current, rebuilt)

        self.assertIn(
            "warning: the API dates gpt's replacement 2026-08-01, but it was still listed on "
            "2026-09-30",
            notes,
        )
        self.assertEqual(_input_price(rebuilt, _facts(), date(2026, 7, 31)), 2.5)
        self.assertEqual(_input_price(rebuilt, _facts(), date(2026, 8, 1)), 2.0)

    def test_a_price_no_longer_listed_is_kept_and_keeps_its_id(self) -> None:
        current = _seed("2026-09-30", _price("gpt", 2.5, "2025-01-01"))
        rebuilt = _seed("2026-12-31")

        self.assertEqual(carry_forward(current, rebuilt), ["kept gpt.last"])
        again = _seed("2027-03-31")
        carry_forward(rebuilt, again)

        self.assertEqual([price["id"] for price in again["prices"]], ["gpt.last"])
        self.assertNotIn("effectiveUntil", again["prices"][0])
        self.assertEqual(again["prices"][0]["sourceIds"], ["retail-azure-openai-20260930"])
        self.assertEqual(_input_price(again, _facts(), date(2027, 1, 1)), 2.5)

    def test_a_price_that_already_ended_is_kept_as_it_is(self) -> None:
        first = _seed("2026-09-30", _price("gpt", 2.5, "2025-01-01"))
        second = _seed("2026-12-31", _price("gpt", 2.0, "2026-12-01"))
        carry_forward(first, second)
        third = _seed("2027-03-31", _price("gpt", 2.0, "2026-12-01"))

        self.assertEqual(
            carry_forward(second, third), ["kept gpt.until-2026-11-30 until 2026-11-30"]
        )

        self.assertEqual(
            sorted(price["id"] for price in third["prices"]), ["gpt", "gpt.until-2026-11-30"]
        )
        self.assertEqual(_input_price(third, _facts(), date(2026, 6, 1)), 2.5)

    def test_a_price_for_every_region_that_changed_region_by_region_keeps_each_region_s_days(
        self,
    ) -> None:
        # The API lists one new price everywhere, but East US changed first.
        index = Index(date(2026, 9, 30))
        rows = []
        for region, start in (
            ("eastus", "2025-06-01"),
            ("westus", "2025-09-01"),
            ("swedencentral", "2025-09-01"),
        ):
            rows.append(_row("Test Inp glbl Tokens", region, 0.002, start=start))
            rows.append(_row("Test Outp glbl Tokens", region, 0.008, start=start))
        index.add("Azure OpenAI", rows)
        [fresh] = [
            price
            for price in model_prices(MODEL, index)
            if price.get("deploymentType") == "GlobalStandard"
        ]
        self.assertNotIn("regions", fresh)
        self.assertEqual(fresh["effectiveFrom"], "2025-06-01")
        old = {**fresh, "inputPerMillion": 2.5, "outputPerMillion": 10.0}
        old["effectiveFrom"] = "2025-01-01"
        current = _seed("2025-05-15", old)
        rebuilt = _seed("2026-09-30", fresh)

        notes = carry_forward(current, rebuilt, index)

        self.assertFalse([note for note in notes if note.startswith("warning")], notes)
        version = MODEL.version

        def price(region: str, day: date) -> float | None:
            return _input_price(rebuilt, _facts(region, version=version), day)

        self.assertEqual(price("eastus", date(2025, 5, 31)), 2.5)
        self.assertEqual(price("eastus", date(2025, 6, 1)), 2.0)
        self.assertEqual(price("westus", date(2025, 8, 31)), 2.5)
        self.assertEqual(price("westus", date(2025, 9, 1)), 2.0)
        self.assertEqual(price("swedencentral", date(2025, 8, 31)), 2.5)
        # A region the API doesn't list follows the price for every region.
        self.assertEqual(price("japaneast", date(2025, 7, 1)), 2.0)


if __name__ == "__main__":
    unittest.main()
