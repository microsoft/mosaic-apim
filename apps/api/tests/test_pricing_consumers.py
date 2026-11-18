"""Shared cost consumers using published billing-derived rates and provenance."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, date, datetime

import pytest
from mosaic_api.billing_pricing import (
    BillingCandidateMapping,
    BillingMappingsUpdateRequest,
    BillingPublishRequest,
)
from mosaic_api.budgets import (
    NONE_BLOCKED,
    Budget,
    BudgetNotification,
    BudgetState,
    budget_id,
    budget_key,
)
from mosaic_api.domain import CostCenterRef
from mosaic_api.pricing import PriceCreate, PriceSource
from mosaic_api.pricing_catalog import CatalogActivateRequest
from mosaic_api.services.analytics.cost import (
    BILLING_DERIVED_NOTE,
    CACHED_NOTE,
    CUSTOM_MANUAL_NOTE,
    LIST_PRICE_NOTE,
    MIXED_BILLING_NOTE,
    RESERVED_NOTE,
)
from mosaic_api.services.budget_email import budget_email
from mosaic_api.services.directory import Actor
from test_budgets import Budgets
from test_cost import (
    ALICE,
    CAROL,
    CHAT,
    CHAT_COST,
    DAILY,
    FEBRUARY_IDLE,
    MARCH_SO_FAR,
    RESERVED,
    TENANT,
    Harness,
    harness,
    rolled_up,
    settings,
)
from test_cost_center_analytics import (
    GENERAL as GENERAL_CC,
)
from test_cost_center_analytics import (
    RESEARCH as RESEARCH_CC,
)
from test_cost_center_analytics import (
    _charge_research,
)
from test_pool_usage import (
    ADA,
    EAST,
    EAST_COST,
    RESERVED_COST,
)
from test_pool_usage import (
    RESERVED as POOL_RESERVED,
)
from test_pool_usage import (
    rolled_up as _rolled_up_pool,
)

__all__ = ["harness", "settings"]


async def test_backdated_public_activation_keeps_protected_midmonth_cost_changes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await rolled_up(harness)
    original = next(
        price
        for price in harness.pricing._seed.prices
        if price.cloud == "commercial" and price.model == "gpt-4o"
        and price.version == "2024-11-20" and price.deployment_type == "GlobalStandard"
        and price.regions is None and price.effective_until is None
    )
    source = PriceSource(
        id="fictional-public", title="Fictional public price evidence",
        url="https://example.invalid/public-prices", retrieved_on=harness.now.date(),
    )
    baseline = original.model_copy(update={
        "id": "fictional-baseline", "effective_from": date(2026, 3, 14),
        "source_ids": [source.id],
    })
    seed = harness.pricing._seed.model_copy(update={
        "sources": [source], "prices": [baseline], "ptu_throughput": [],
    })
    monkeypatch.setattr(harness.pricing, "_seed", seed)
    monkeypatch.setattr(harness.pricing.catalog, "_seed", seed)
    await harness.pricing.add_price(_actor(), PriceCreate(
        cloud="commercial", publisher="OpenAI", model="gpt-4o", version="2024-11-20",
        deployment_type="GlobalStandard", input_per_million=1.25, output_per_million=5,
        effective_from=date(2026, 2, 1), source_url="https://example.invalid/agreement",
        note="Earlier fictional manual rate",
    ))
    price = baseline.model_dump(mode="json", by_alias=True, exclude_none=True)
    price.update({
        "id": "fictional-correction", "inputPerMillion": 2, "outputPerMillion": 8,
        "effectiveFrom": "2026-03-01",
    })
    package = {
        "format": "mosaicPublicCatalog", "schemaVersion": 1,
        "catalog": [{
            "cloud": "commercial", "publisher": "OpenAI", "model": price["model"],
            "version": price["version"], "deploymentType": "GlobalStandard",
            "aliases": price.get("aliases", []), "meters": price["retailMeters"],
        }],
        "priceList": {
            "schemaVersion": 1, "currency": "USD",
            "lastUpdated": harness.now.date().isoformat(),
            "sources": [source.model_dump(mode="json", by_alias=True)], "prices": [price],
        },
    }
    draft = await harness.pricing.catalog.import_bytes(
        _actor(), json.dumps(package).encode(), None
    )
    effective = date(2026, 3, 1)
    preview = await harness.pricing.catalog.preview(_actor(), draft.id, effective)
    await harness.pricing.catalog.activate(_actor(), draft.id, CatalogActivateRequest(
        draft_version=preview.draft.version, base_revision=None,
        preview_digest=preview.preview_digest, effective_from=effective,
        note="Acknowledged fictional historical correction",
        acknowledge_source_and_gaps=True, acknowledge_backdate_impact=True,
    ))
    for window in ("30d", "12m"):
        consumers = harness.get("/api/v1/analytics/consumers", range=window)
        people = {row["label"]: row["cost"] for row in consumers["people"]}
        assert people["Alice"] == pytest.approx(5 * 1.75 + 5 * 2.8), window
        models = harness.get("/api/v1/analytics/models", range=window)
        chat = next(row for row in models["deployments"] if row["deploymentName"] == "chat")
        assert chat["cost"] == pytest.approx(5 * 1.75 + 5 * 2.8 + 0.2), window


async def test_runtime_public_catalog_prices_all_existing_cost_consumers(
    harness: Harness,
) -> None:
    await _charge_research(harness)
    before_cost = harness.get("/api/v1/analytics/cost", range="30d")
    before_overview = harness.get("/api/v1/analytics/overview", range="30d")
    facts = await harness.pricing.deployments(TENANT)
    prices = []
    definitions = []
    for key in (CHAT, RESERVED):
        item = facts[key]
        definition = {
            "cloud": item.cloud,
            "publisher": item.publisher,
            "model": item.model,
            "version": item.version,
            "deploymentType": item.deployment_type,
            "regions": [item.region] if item.region else None,
            "kind": "ptu" if item.provisioned else "tokens",
            "requiresOutput": not item.provisioned,
            "meters": {
                "product": "Fictional public rate card",
                **(
                    {"provisioned": "Fictional PTU-hour"}
                    if item.provisioned
                    else {"input": "Fictional input", "output": "Fictional output"}
                ),
            },
        }
        definitions.append(definition)
        prices.append(
            {
                "id": "fictional.ptu" if item.provisioned else "fictional.tokens",
                "cloud": item.cloud,
                "publisher": item.publisher,
                "model": item.model,
                "version": item.version,
                "deploymentType": item.deployment_type,
                "regions": [item.region] if item.region else None,
                "effectiveFrom": "2025-01-01",
                "sourceIds": ["fictional-public-card"],
                **(
                    {"ptuHourly": 0.5}
                    if item.provisioned
                    else {
                        "inputPerMillion": 1.25,
                        "outputPerMillion": 5,
                    }
                ),
            }
        )
    source_day = harness.now.date().isoformat()
    package = {
        "format": "mosaicPublicCatalog",
        "schemaVersion": 1,
        "catalog": definitions,
        "priceList": {
            "schemaVersion": 1,
            "currency": "USD",
            "lastUpdated": source_day,
            "sources": [
                {
                    "id": "fictional-public-card",
                    "title": "Fictional test public rates",
                    "url": "https://example.invalid/public-rates",
                    "retrievedOn": source_day,
                }
            ],
            "prices": prices,
        },
    }
    actor = _actor()
    draft = await harness.pricing.catalog.import_bytes(actor, json.dumps(package).encode(), None)
    assert harness.get("/api/v1/analytics/cost", range="30d")["cost"] == before_cost["cost"]
    effective = date(2026, 2, 1)
    preview = await harness.pricing.catalog.preview(actor, draft.id, effective)
    activation = await harness.pricing.catalog.activate(
        actor,
        draft.id,
        CatalogActivateRequest(
            draft_version=preview.draft.version,
            base_revision=None,
            preview_digest=preview.preview_digest,
            effective_from=effective,
            note="Reviewed synthetic public token/PTU correction",
            acknowledge_source_and_gaps=True,
            acknowledge_backdate_impact=True,
        ),
    )
    cost = harness.get("/api/v1/analytics/cost", range="30d")
    assert cost["cost"]["total"] == pytest.approx(before_cost["cost"]["total"] / 2)
    assert cost["cost"]["unpricedTokens"] == before_cost["cost"]["unpricedTokens"]
    assert (
        next(item for item in cost["deployments"] if item["deploymentName"] == "mystery")["cost"]
        is None
    )
    overview = harness.get("/api/v1/analytics/overview", range="30d")
    assert overview["spend"]["monthToDate"] == pytest.approx(
        before_overview["spend"]["monthToDate"] / 2
    )
    assert overview["spend"]["forecast"] == pytest.approx(
        before_overview["spend"]["forecast"] / 2, abs=1e-3
    )
    portal = harness.get("/api/v1/me/usage", period="30d")
    assert portal["totals"]["estimatedCost"] == pytest.approx(17.5)
    assert activation.id not in json.dumps(portal)
    assert draft.id not in json.dumps(portal)
    batch = await harness.state.analytics_service.budget_spend(TENANT, [RESEARCH_CC, GENERAL_CC])
    assert batch is not None and batch.organization is not None
    assert batch.cost_centers[RESEARCH_CC].month_to_date == pytest.approx(MARCH_SO_FAR / 2)
    assert batch.organization.month_to_date == pytest.approx((CHAT_COST + MARCH_SO_FAR) / 2)
    export = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )
    assert export.status_code == 200
    rows = list(csv.DictReader(io.StringIO(export.text.lstrip("\ufeff"))))
    assert sum(float(row["Cost (USD)"] or 0) for row in rows) == pytest.approx(
        cost["cost"]["total"], abs=1e-3
    )
    assert {row["Cost center name"] for row in rows if row["Cost center name"]} == {
        "General",
        "Research",
    }


ACCOUNT_RES_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai/"
    "providers/Microsoft.CognitiveServices/accounts/contoso-aoai-secret-acct"
)


def _actor() -> Actor:
    return Actor(object_id=ALICE, tenant_id=TENANT)


def _azure_chat_billing_csv(
    *,
    input_cost: str = "1.25",
    output_cost: str = "5.00",
    resource_id: str = ACCOUNT_RES_ID,
) -> bytes:
    """1M input tokens at $1.25/M and 1M output tokens at $5.00/M (half the public seed rate)."""

    text = (
        "Date,MeterCategory,MeterName,Product,Quantity,UnitOfMeasure,EffectivePrice,"
        "CostInBillingCurrency,BillingCurrencyCode,ChargeType,PricingModel,"
        "ResourceId,ResourceLocation\n"
        f"2026-02-10,Foundry Models,gpt 4o 1120 Inp glbl Tokens,Azure OpenAI,1,1M Tokens,"
        f"{input_cost},{input_cost},USD,Usage,OnDemand,{resource_id},East US 2\n"
        f"2026-02-10,Foundry Models,gpt 4o 1120 Outp glbl Tokens,Azure OpenAI,1,1M Tokens,"
        f"{output_cost},{output_cost},USD,Usage,OnDemand,{resource_id},East US 2\n"
    )
    return text.encode("utf-8")


def _mosaic_template_ptu_csv(
    *,
    ptu_hourly: float = 0.5,
    resource_id: str = ACCOUNT_RES_ID,
    region: str = "eastus2",
) -> bytes:
    """Provisioned PTU-hour billing row at $0.50/PTU-hour (half the $1.00/PTU-hour seed rate)."""

    hours = 100
    cost = hours * ptu_hourly
    text = (
        "schema_version,provider,pricing_cloud,source_month,publisher,model,model_version,"
        "deployment_type,region,usage_category,usage_quantity,usage_unit,currency,"
        "eligible_cost_usd,amortized_benefit_cost_usd,excluded_cost_usd,excluded_reason,"
        "source_account_id,source_resource_id,source_resource_name\n"
        f"mosaic_billing_v1,azure,commercial,2026-02,OpenAI,gpt-4o,2024-11-20,"
        f"GlobalProvisionedManaged,{region},ptuHours,{hours},ptuHours,USD,{cost:.2f},"
        f"0.00,0.00,,/subscriptions/sub-1,{resource_id},contoso-aoai-secret-acct\n"
    )
    return text.encode("utf-8")


async def _publish_deployment_rate(
    harness: Harness,
    *,
    content: bytes,
    provider: str = "azure",
    deployment_key: str,
    effective_from: date = date(2026, 2, 1),
) -> str:
    actor = _actor()
    detail = await harness.pricing.import_billing_data(
        actor,
        provider=provider,  # type: ignore[arg-type]
        pricing_cloud="commercial",
        source_month="2026-02",
        file_name="billing-2026-02.csv",
        content=content,
        force=True,
    )
    assert len(detail.candidates) == 1
    candidate_id = detail.candidates[0].candidate_id
    updated = await harness.pricing.update_billing_mappings(
        actor,
        detail.id,
        BillingMappingsUpdateRequest(
            preview_revision=detail.preview_revision,
            mappings=[
                BillingCandidateMapping(
                    candidate_id=candidate_id,
                    scope="deployment",
                    deployment_key=deployment_key,
                    cloud="commercial",
                    confirmed=True,
                )
            ],
        ),
    )
    publication = await harness.pricing.publish_billing_run(
        actor,
        detail.id,
        BillingPublishRequest(
            effective_from=effective_from,
            acknowledge_backdate_impact=True,
            preview_revision=updated.preview_revision,
            note="Approved February 2026 billing rate",
        ),
    )
    return publication.id


async def test_published_billing_rates_drive_analytics_cost_spend_forecast_and_chargeback(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    # 1. Before any billing publication, all analytics cost surfaces report publicSeed + list note.
    initial_cost = harness.get("/api/v1/analytics/cost", range="30d")
    assert initial_cost["cost"]["pricingBasis"] == "publicSeed"
    assert LIST_PRICE_NOTE in initial_cost["cost"]["notes"]
    assert CACHED_NOTE in initial_cost["cost"]["notes"]
    assert RESERVED_NOTE in initial_cost["cost"]["notes"]

    # Draft import alone must not alter any cost or provenance.
    draft = await harness.pricing.import_billing_data(
        _actor(),
        provider="azure",
        pricing_cloud="commercial",
        source_month="2026-02",
        file_name="draft.csv",
        content=_azure_chat_billing_csv(),
    )
    assert draft.status == "readyForReview"
    still_seed = harness.get("/api/v1/analytics/cost", range="30d")
    assert still_seed["cost"]["pricingBasis"] == "publicSeed"
    assert still_seed["cost"]["total"] == pytest.approx(CHAT_COST + MARCH_SO_FAR + FEBRUARY_IDLE)

    # 2. Publish the billing-derived rate for `chat` (50% of list price: $1.25 in / $5.00 out).
    # `reserved` still uses the public seed ($1.00/PTU-hr), so full-estate reports are "mixed".
    await _publish_deployment_rate(
        harness,
        content=_azure_chat_billing_csv(),
        deployment_key=CHAT,
        effective_from=date(2026, 2, 1),
    )

    half_chat_cost = CHAT_COST / 2.0
    mixed_total = half_chat_cost + MARCH_SO_FAR + FEBRUARY_IDLE

    cost_view = harness.get("/api/v1/analytics/cost", range="30d")
    assert cost_view["cost"]["pricingBasis"] == "mixed"
    assert cost_view["cost"]["total"] == pytest.approx(mixed_total)
    assert MIXED_BILLING_NOTE in cost_view["cost"]["notes"]
    assert LIST_PRICE_NOTE not in cost_view["cost"]["notes"]
    assert CACHED_NOTE in cost_view["cost"]["notes"]
    assert RESERVED_NOTE in cost_view["cost"]["notes"]
    # Unpriced mystery deployment remains unpriced, never $0.
    assert cost_view["cost"]["unpricedTokens"] == 4_000

    rows_by_name = {row["deploymentName"]: row for row in cost_view["deployments"]}
    chat_row = rows_by_name["chat"]
    assert chat_row["priceOrigin"] == "billing"
    assert chat_row["priceSourceMonth"] == "2026-02"
    assert chat_row["priceBillingProvider"] == "azure"
    assert (chat_row["inputPerMillion"], chat_row["outputPerMillion"]) == (1.25, 5.0)
    assert chat_row["cost"] == pytest.approx(half_chat_cost)
    assert rows_by_name["reserved"]["priceOrigin"] == "seed"
    assert rows_by_name["mystery"]["pricing"] == "unpriced"
    assert rows_by_name["mystery"]["cost"] is None

    # Overview spend & forecast use the billing-derived rate for chat.
    overview = harness.get("/api/v1/analytics/overview", range="30d")
    assert overview["cost"]["pricingBasis"] == "mixed"
    assert overview["kpis"]["cost"] == pytest.approx(mixed_total)
    elapsed = 17 + 15.5 / 24
    assert overview["spend"]["monthToDate"] == pytest.approx(half_chat_cost + MARCH_SO_FAR)
    assert overview["spend"]["forecast"] == pytest.approx(
        half_chat_cost * 31 / elapsed + MARCH_SO_FAR * 31 / 18,
        abs=1e-3,
    )

    # Unattributed calls only hit `chat`, so unattributed report is purely "billingDerived".
    unattributed = harness.get("/api/v1/analytics/unattributed", range="30d")
    assert unattributed["cost"]["pricingBasis"] == "billingDerived"
    assert unattributed["cost"]["total"] == pytest.approx(0.125)
    assert BILLING_DERIVED_NOTE in unattributed["cost"]["notes"]

    # 3. Publish a billing-derived PTU rate for `reserved` as well ($0.50/PTU-hr = half of seed).
    await _publish_deployment_rate(
        harness,
        content=_mosaic_template_ptu_csv(ptu_hourly=0.5),
        provider="mosaicTemplate",
        deployment_key=RESERVED,
        effective_from=date(2026, 2, 1),
    )

    half_march_reserved = MARCH_SO_FAR / 2.0
    half_feb_idle = FEBRUARY_IDLE / 2.0
    all_billing_total = half_chat_cost + half_march_reserved + half_feb_idle

    all_billing_cost = harness.get("/api/v1/analytics/cost", range="30d")
    assert all_billing_cost["cost"]["pricingBasis"] == "billingDerived"
    assert all_billing_cost["cost"]["total"] == pytest.approx(all_billing_total)
    assert all_billing_cost["cost"]["reserved"] == pytest.approx(
        half_march_reserved + half_feb_idle
    )
    assert BILLING_DERIVED_NOTE in all_billing_cost["cost"]["notes"]
    assert MIXED_BILLING_NOTE not in all_billing_cost["cost"]["notes"]
    assert LIST_PRICE_NOTE not in all_billing_cost["cost"]["notes"]

    rows_by_name = {row["deploymentName"]: row for row in all_billing_cost["deployments"]}
    assert rows_by_name["reserved"]["priceOrigin"] == "billing"
    assert rows_by_name["reserved"]["ptuHourly"] == pytest.approx(0.5)
    assert rows_by_name["reserved"]["idleCost"] == pytest.approx(half_feb_idle)
    assert rows_by_name["reserved"]["monthCost"] == pytest.approx((DAILY / 2.0) * 31)

    # Chargeback CSV export reconciles with the billing-derived total, including idle PTU cost.
    csv_resp = harness.client.get(
        "/api/v1/analytics/export", params={"view": "chargeback", "range": "30d"}
    )
    assert csv_resp.status_code == 200
    csv_rows = list(csv.DictReader(io.StringIO(csv_resp.text.lstrip("\ufeff"))))
    charged = {
        (row["Month"], row["Charged to"], row["Model"]): (
            row["Cost (USD)"],
            row["Priced"],
        )
        for row in csv_rows
    }
    assert float(charged[("2026-03", "Alice", "gpt-4o")][0]) == pytest.approx(17.5)
    assert float(charged[("2026-03", "Bob", "gpt-4o")][0]) == pytest.approx(
        half_march_reserved * 0.75
    )
    assert float(charged[("2026-03", "Analysts", "gpt-4o")][0]) == pytest.approx(
        half_march_reserved * 0.25
    )
    assert charged[("2026-03", "Support bot", "contoso-llm")] == ("", "no")
    assert float(charged[("2026-03", "Unattributed calls", "gpt-4o")][0]) == pytest.approx(0.125)
    assert float(
        charged[("2026-02", "Reserved capacity with no calls", "gpt-4o")][0]
    ) == pytest.approx(half_feb_idle)
    assert sum(float(row["Cost (USD)"] or 0) for row in csv_rows) == pytest.approx(
        all_billing_total, abs=1e-3
    )


async def test_portal_usage_uses_billing_rates_and_redacts_account_evidence(
    harness: Harness,
) -> None:
    await rolled_up(harness)

    # Publish billing rate for `chat` only; `reserved` stays on seed.
    pub_id = await _publish_deployment_rate(
        harness,
        content=_azure_chat_billing_csv(),
        deployment_key=CHAT,
        effective_from=date(2026, 2, 1),
    )

    # Alice holds `grant-alice` (`chat`, billing) and `grant-analysts` (`reserved`, seed).
    alice_usage = harness.get("/api/v1/me/usage", period="30d")
    assert alice_usage["pricingBasis"] == "mixed"
    assert alice_usage["totals"]["estimatedCost"] == pytest.approx(17.5)
    assert any("approved model rates" in note for note in alice_usage["notes"])
    assert not any("before any discount" in note for note in alice_usage["notes"])
    assert any("cached prompt tokens" in note for note in alice_usage["notes"])

    # Portal usage payload must never leak account-level billing IDs, run IDs, or resource IDs.
    raw_alice = json.dumps(alice_usage)
    assert pub_id not in raw_alice
    assert "billrun" not in raw_alice
    assert "billpub" not in raw_alice
    assert "contoso-aoai-secret-acct" not in raw_alice
    assert ACCOUNT_RES_ID not in raw_alice

    # Carol only holds `grant-analysts` (`reserved`, still on public seed).
    harness.sign_in(CAROL, ["User"])
    carol_usage = harness.get("/api/v1/me/usage", period="30d")
    assert carol_usage["pricingBasis"] == "publicSeed"
    assert carol_usage["totals"]["estimatedCost"] == pytest.approx(MARCH_SO_FAR * 0.25)
    assert any("list prices" in note for note in carol_usage["notes"])

    # Once `reserved` also receives a published billing rate, Carol's report is `billingDerived`.
    harness.sign_in(ALICE, ["User", "Admin"])
    await _publish_deployment_rate(
        harness,
        content=_mosaic_template_ptu_csv(ptu_hourly=0.5),
        provider="mosaicTemplate",
        deployment_key=RESERVED,
        effective_from=date(2026, 2, 1),
    )
    harness.sign_in(CAROL, ["User"])
    carol_after = harness.get("/api/v1/me/usage", period="30d")
    assert carol_after["pricingBasis"] == "billingDerived"
    assert carol_after["totals"]["estimatedCost"] == pytest.approx((MARCH_SO_FAR / 2.0) * 0.25)
    assert any("approved model rates" in note for note in carol_after["notes"])
    assert not any("before any discount" in note for note in carol_after["notes"])


async def test_model_pool_consumers_and_portal_usage_price_members_with_billing_rates(
    harness: Harness,
) -> None:
    await _rolled_up_pool(harness)

    # Publish a billing-derived rate for the East pool member (`endpoint-east/chat-east`) at half
    # the seed rate ($1.25 in / $5.00 out), leaving Sweden and Reserved PTU on seed rates.
    await _publish_deployment_rate(
        harness,
        content=_azure_chat_billing_csv(),
        deployment_key=f"{EAST}/chat-east",
        effective_from=date(2026, 2, 1),
    )

    cost_view = harness.get("/api/v1/analytics/cost", range="30d")
    assert cost_view["cost"]["pricingBasis"] == "mixed"
    deployments = {row["deploymentName"]: row for row in cost_view["deployments"]}
    assert deployments["chat-east"]["priceOrigin"] == "billing"
    assert deployments["chat-east"]["cost"] == pytest.approx(EAST_COST / 2.0)
    assert deployments["chat-sweden"]["priceOrigin"] == "seed"
    assert deployments["chat-ptu"]["priceOrigin"] == "seed"

    # Ada's pool calls ran on `chat-east` (billing) and `chat-ptu` (seed), so her portal usage
    # reports `mixed`, then `billingDerived` once `chat-ptu` also gets a billing rate, and never
    # exposes the pool member deployments or billing account.
    harness.sign_in(ADA, ["User"], frozenset())
    ada_usage = harness.get("/api/v1/me/usage", period="30d")
    assert ada_usage["pricingBasis"] == "mixed"
    [ada_row] = ada_usage["byResource"]
    assert ada_row["estimatedCost"] == pytest.approx((EAST_COST / 2.0) + (RESERVED_COST / 4.0))
    raw_ada = json.dumps(ada_usage)
    assert "chat-east" not in raw_ada
    assert "chat-ptu" not in raw_ada
    assert "contoso-aoai-secret-acct" not in raw_ada

    harness.sign_in(ALICE, ["User", "Admin"])
    await _publish_deployment_rate(
        harness,
        content=_mosaic_template_ptu_csv(ptu_hourly=0.5, region="eastus"),
        provider="mosaicTemplate",
        deployment_key=f"{POOL_RESERVED}/chat-ptu",
        effective_from=date(2026, 2, 1),
    )
    harness.sign_in(ADA, ["User"], frozenset())
    ada_all_billing = harness.get("/api/v1/me/usage", period="30d")
    assert ada_all_billing["pricingBasis"] == "billingDerived"
    [ada_row_after] = ada_all_billing["byResource"]
    assert ada_row_after["estimatedCost"] == pytest.approx(
        (EAST_COST / 2.0) + ((RESERVED_COST / 2.0) / 4.0)
    )


async def test_budgets_evaluate_billing_derived_spend_via_existing_budget_worker(
    harness: Harness,
) -> None:
    await _charge_research(harness)
    budgets = Budgets(harness)
    await budgets.manage_gateway()
    budgets.email_on()

    # Set a blocking budget of $3,000 on Research and a warning budget of $3,500 on Organization.
    # At public seed rates, Research spent $4,320 -> blocked.
    res_view = budgets.put(
        f"/api/v1/cost-centers/{RESEARCH_CC}/budget",
        {"amount": 3000, "action": "block"},
    )
    org_view = budgets.put(
        "/api/v1/budgets/organization",
        {"amount": 3500, "recipients": ["cfo@contoso.com"]},
    )
    assert res_view["status"]["blocked"] is True
    assert res_view["status"]["monthToDate"] == pytest.approx(MARCH_SO_FAR)
    assert org_view["status"]["level"] == "exceeded"
    assert budgets.gateway.blocked == budget_key(RESEARCH_CC)
    writes_before_publish = len(budgets.gateway.writes)

    # Publishing a billing-derived PTU rate ($0.50/PTU-hr) halves Research's spend to $2,160.
    # Publication itself never writes to the APIM gateway directly; the budget worker does on check.
    await _publish_deployment_rate(
        harness,
        content=_mosaic_template_ptu_csv(ptu_hourly=0.5),
        provider="mosaicTemplate",
        deployment_key=RESERVED,
        effective_from=date(2026, 3, 1),
    )
    assert len(budgets.gateway.writes) == writes_before_publish

    batch = await harness.state.analytics_service.budget_spend(
        TENANT, [RESEARCH_CC, GENERAL_CC]
    )
    assert batch is not None
    assert batch.cost_centers[RESEARCH_CC].month_to_date == pytest.approx(MARCH_SO_FAR / 2.0)
    assert batch.organization is not None
    assert batch.organization.month_to_date == pytest.approx(CHAT_COST + MARCH_SO_FAR / 2.0)

    # Raising Research's budget or running the next month/check re-evaluates through BudgetService.
    raised = budgets.put(
        f"/api/v1/cost-centers/{RESEARCH_CC}/budget",
        {"amount": 3001, "action": "block"},
    )
    assert raised["status"]["monthToDate"] == pytest.approx(MARCH_SO_FAR / 2.0)
    assert raised["status"]["blocked"] is False
    assert budgets.gateway.blocked == NONE_BLOCKED


def test_budget_email_describes_pricing_basis_without_false_list_price_claim() -> None:
    budget = Budget(
        id=budget_id(TENANT, RESEARCH_CC),
        tenant_id=TENANT,
        cost_center_id=RESEARCH_CC,
        amount=1000.0,
    )
    state = BudgetState(
        id="state",
        tenant_id=TENANT,
        budget_id=budget.id,
        cost_center_id=RESEARCH_CC,
        month="2026-03",
        month_to_date=850.0,
        forecast=1100.0,
    )
    notice = BudgetNotification(kind="threshold", threshold=80, created_at=datetime.now(UTC))
    ref = CostCenterRef(id=RESEARCH_CC, name="Research", code="RES")

    default_msg = budget_email(notice, budget, state, ref, ["lead@contoso.com"])
    assert "priced from MOSAIC's price list" in default_msg.plain_text
    assert "before any discount" not in default_msg.plain_text

    billing_msg = budget_email(
        notice, budget, state, ref, ["lead@contoso.com"], pricing_basis="billingDerived"
    )
    assert "approved billing-derived rates" in billing_msg.plain_text
    assert "before any discount" not in billing_msg.plain_text

    seed_msg = budget_email(
        notice, budget, state, ref, ["lead@contoso.com"], pricing_basis="publicSeed"
    )
    assert "list-price estimates" in seed_msg.plain_text
    assert "before any discount" in seed_msg.plain_text
    assert CUSTOM_MANUAL_NOTE != LIST_PRICE_NOTE
