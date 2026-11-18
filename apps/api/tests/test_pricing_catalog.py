"""Runtime public catalog visibility, dated protection, bounded acquisition and recovery."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from email.utils import format_datetime
from typing import Any, cast

import httpx
import pytest
from azure.cosmos.aio import CosmosClient
from fastapi.testclient import TestClient
from mosaic_api.billing_pricing import (
    BillingCandidateMapping,
    BillingMappingsUpdateRequest,
    BillingPublishRequest,
)
from mosaic_api.errors import ConflictError, NotFoundError, UpstreamError, ValidationError
from mosaic_api.integrations.retail_prices import RetailPricesAdapter
from mosaic_api.main import create_app
from mosaic_api.pricing import PriceCreate, PriceMatch, PriceSeed, PriceVersion
from mosaic_api.pricing_catalog import (
    MAX_CATALOG_BYTES,
    CatalogActivateRequest,
    CatalogDefinition,
    canonical_json,
)
from mosaic_api.repositories import CosmosPricingRepository
from mosaic_api.services.billing_ingestion import (
    _catalog_meter_lookup,
    _parse_azure_meter_and_model,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import PricingService
from test_model_access_repositories import Cosmos
from test_pricing_publication import (
    TENANT,
    TODAY,
    _actor,
    _build_estate,
    _sample_azure_csv,
    _settings,
)


def _package(rate: float = 1.2, cloud: str = "commercial", **changes: Any) -> dict[str, Any]:
    price = {
        "id": f"{cloud}.example",
        "cloud": cloud,
        "publisher": "OpenAI",
        "model": "gpt-4o",
        "version": "2024-11-20",
        "deploymentType": "GlobalStandard",
        "inputPerMillion": rate,
        "outputPerMillion": rate * 4,
        "effectiveFrom": "2024-01-01",
        "sourceIds": ["fictional-price-page"],
    }
    price.update(changes)
    return {
        "format": "mosaicPublicCatalog",
        "schemaVersion": 1,
        "catalog": [
            {
                "cloud": cloud,
                "publisher": "OpenAI",
                "model": price["model"],
                "version": price["version"],
                "deploymentType": price["deploymentType"],
                "kind": "tokens",
                "requiresOutput": True,
                "meters": {
                    "product": "Example product",
                    "input": "Example input",
                    "output": "Example output",
                },
            }
        ],
        "priceList": {
            "schemaVersion": 1,
            "currency": "USD",
            "lastUpdated": TODAY.isoformat(),
            "sources": [
                {
                    "id": "fictional-price-page",
                    "title": f"Fictional {cloud} numeric price evidence",
                    "url": f"https://example.invalid/{cloud}-prices",
                    "retrievedOn": TODAY.isoformat(),
                }
            ],
            "prices": [price],
        },
        "evidence": [
            {
                "sourceId": "fictional-price-page",
                "cloud": cloud,
                "priceIds": [price["id"]],
                "excerpt": (
                    f"Fictional {cloud} rate: USD {rate} input and {rate * 4} "
                    "output per million tokens."
                ),
            }
        ],
    }


async def _import(
    service: PricingService, rate: float = 1.2, cloud: str = "commercial", **changes: Any
):
    head = await service._repository.get_catalog_head(TENANT)
    return await service.catalog.import_bytes(
        _actor(),
        json.dumps(_package(rate, cloud, **changes)).encode(),
        head.revision if head else None,
    )


async def _approval(service: PricingService, draft_id: str, day=TODAY):
    preview = await service.catalog.preview(_actor(), draft_id, day)
    return CatalogActivateRequest(
        draft_version=preview.draft.version,
        base_revision=preview.base_revision,
        preview_digest=preview.preview_digest,
        effective_from=day,
        note="Reviewed fictional public rates",
        acknowledge_source_and_gaps=True,
        acknowledge_backdate_impact=day < TODAY,
    )


async def _rate(service: PricingService, day=TODAY, key="endpoint-east/chat") -> float | None:
    entry = (await service.pricer(TENANT)).rate(key, day).entry
    return entry.input_per_million if entry else None


async def test_import_activation_is_dated_durable_and_not_an_offering() -> None:
    service, repository, endpoints, _ = await _build_estate()
    facts_before = await service.deployments(TENANT)
    seed_before = canonical_json(service._seed)
    draft = await _import(service)
    assert await _rate(service) == 2.5
    assert draft.sources[0].method == "supplied"
    assert draft.sources[0].retrieved_on == TODAY
    day = TODAY + timedelta(days=1)
    approval = await _approval(service, draft.id, day)
    activation = await service.catalog.activate(_actor(), draft.id, approval)
    assert await _rate(service) == 2.5
    assert await _rate(service, day) == 1.2
    assert await service.catalog.activate(_actor(), draft.id, approval) == activation
    assert len(repository.catalog_activations) == 1
    assert await service.deployments(TENANT) == facts_before
    assert canonical_json(service._seed) == seed_before
    other_replica = PricingService(
        repository,
        endpoint_repository=endpoints,
        gateway_repository=service._gateways,
        clock=service._clock,
    )
    assert await _rate(other_replica, day) == 1.2
    assert (await other_replica.catalog.status(_actor())).revision == activation.id
    with pytest.raises(NotFoundError):
        await service.catalog.draft_view(Actor(object_id="admin", tenant_id="other"), draft.id)


def _raw_rate_package(field: str, value: object) -> bytes:
    package = _package()
    price = package["priceList"]["prices"][0]
    camel_name = {
        "input_per_million": "inputPerMillion",
        "cached_input_per_million": "cachedInputPerMillion",
        "output_per_million": "outputPerMillion",
        "ptu_hourly": "ptuHourly",
    }.get(field, field)
    if camel_name == "ptuHourly":
        definition = package["catalog"][0]
        definition.update({
            "kind": "ptu", "requiresOutput": False, "deploymentType": "ProvisionedManaged",
            "meters": {"product": "Example product", "provisioned": "Example PTU-hour"},
        })
        price["deploymentType"] = "ProvisionedManaged"
        del price["inputPerMillion"]
        del price["outputPerMillion"]
    price.pop(camel_name, None)
    price[field] = value
    return json.dumps(package).encode()


@pytest.mark.parametrize(
    "field",
    [
        "inputPerMillion", "input_per_million",
        "cachedInputPerMillion", "cached_input_per_million",
        "outputPerMillion", "output_per_million",
        "ptuHourly", "ptu_hourly",
    ],
)
@pytest.mark.parametrize("value", [False, True, "0", "1.25", [], {}])
async def test_raw_json_rates_reject_non_numbers_before_legacy_float_coercion(
    field: str, value: object,
) -> None:
    service, repository, _, _ = await _build_estate()
    before = await service.book(TENANT)
    future = TODAY + timedelta(days=1)
    with pytest.raises(ValidationError, match="invalid"):
        await service.catalog.import_bytes(_actor(), _raw_rate_package(field, value), None)
    drafts = await repository.list_catalog_drafts(TENANT)
    assert len(drafts) == 1 and drafts[0].status == "invalid"
    assert drafts[0].error and "must be a JSON number" in drafts[0].error
    with pytest.raises(ConflictError):
        await service.catalog.preview(_actor(), drafts[0].id, future)
    assert await repository.get_catalog_head(TENANT) is None
    assert not await repository.list_catalog_activations(TENANT)
    assert (await service.book(TENANT)).entries == before.entries
    assert await _rate(service, TODAY) == 2.5
    assert await _rate(service, future) == 2.5


@pytest.mark.parametrize("zero", [0, 0.0])
async def test_literal_json_zero_remains_valid_but_requires_explicit_activation(
    zero: int | float,
) -> None:
    service, repository, _, _ = await _build_estate()
    future = TODAY + timedelta(days=1)
    draft = await service.catalog.import_bytes(
        _actor(), json.dumps(_package(rate=zero)).encode(), None
    )
    assert draft.status == "readyForReview"
    assert await repository.get_catalog_head(TENANT) is None
    assert not await repository.list_catalog_activations(TENANT)
    assert await _rate(service, future) == 2.5
    preview = await service.catalog.preview(_actor(), draft.id, future)
    assert preview.changes[0].price.input_per_million == 0
    assert await repository.get_catalog_head(TENANT) is None
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id, future))
    assert await _rate(service, TODAY) == 2.5
    assert await _rate(service, future) == 0


@pytest.mark.parametrize("field", ["cachedInputPerMillion", "ptuHourly"])
async def test_raw_json_zero_is_also_valid_for_cached_input_and_ptu(field: str) -> None:
    service, repository, _, _ = await _build_estate()
    draft = await service.catalog.import_bytes(_actor(), _raw_rate_package(field, 0), None)
    assert draft.status == "readyForReview"
    assert await repository.get_catalog_head(TENANT) is None
    assert not await repository.list_catalog_activations(TENANT)
    assert await _rate(service, TODAY + timedelta(days=1)) == 2.5


def test_strict_public_boundary_does_not_change_legacy_price_readers() -> None:
    legacy = _package()
    legacy["priceList"]["prices"][0]["inputPerMillion"] = "0"
    assert PriceSeed.model_validate(legacy["priceList"]).prices[0].input_per_million == 0
    for origin in ("admin", "billing"):
        version = PriceVersion.model_validate_json(json.dumps({
            "id": f"legacy-{origin}", "tenantId": TENANT, "origin": origin,
            "cloud": "commercial", "model": "gpt-4o", "inputPerMillion": "1.25",
            "effectiveFrom": TODAY.isoformat(), "note": "Existing persisted rate",
            "recordedBy": "existing-admin",
        }))
        assert version.input_per_million == 1.25


async def test_government_import_is_separate_and_requires_numeric_evidence() -> None:
    service, _, _, _ = await _build_estate()
    missing = _package(cloud="government")
    missing["evidence"] = []
    with pytest.raises(ValidationError, match="invalid"):
        await service.catalog.import_bytes(_actor(), json.dumps(missing).encode(), None)
    availability_only = _package(cloud="government")
    availability_only["priceList"]["sources"][0]["url"] = (
        "https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/"
        "models-sold-directly-by-azure-gov"
    )
    with pytest.raises(ValidationError, match="invalid"):
        await service.catalog.import_bytes(_actor(), json.dumps(availability_only).encode(), None)
    draft = await _import(service, 4.5, "government")
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    facts = (await service.deployments(TENANT))["endpoint-east/chat"]
    match = (await service.book(TENANT)).match(replace(facts, cloud="government"), TODAY)
    assert isinstance(match, PriceMatch) and match.entry.input_per_million == 4.5
    assert await _rate(service) == 2.5
    unknown = (await service.book(TENANT)).match(
        replace(facts, cloud="government", model="not-listed"), TODAY
    )
    assert not isinstance(unknown, PriceMatch)


async def test_unchanged_amounts_do_not_backdate_new_evidence_or_rewrite_government() -> None:
    service, _, _, _ = await _build_estate()
    _, initial = await service.catalog.active_content(TENANT)
    assert initial is None
    government = [price for price in service._seed.prices if price.cloud == "government"]
    before_book = await service.book(TENANT)
    facts = (await service.deployments(TENANT))["endpoint-east/chat"]
    before = before_book.match(facts, TODAY - timedelta(days=1))
    draft = await _import(service, 2.5, cachedInputPerMillion=1.25)
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    book = await service.book(TENANT)
    earlier = book.match(facts, TODAY - timedelta(days=1))
    current = book.match(facts, TODAY)
    assert isinstance(before, PriceMatch) and isinstance(earlier, PriceMatch)
    assert earlier.entry.line_id == before.entry.line_id
    assert earlier.entry.input_per_million == before.entry.input_per_million
    assert earlier.entry.recorded_at == before.entry.recorded_at
    assert earlier.entry.effective_until == TODAY - timedelta(days=1)
    assert earlier.entry.sources == before.entry.sources
    assert isinstance(current, PriceMatch) and current.entry.effective_from == TODAY
    assert current.entry.recorded_by == _actor().object_id
    _, content = await service.catalog.active_content(TENANT)
    assert content is not None
    assert [
        price for price in content.public_prices.prices if price.cloud == "government"
    ] == government


async def test_an_expired_public_replacement_does_not_resurrect_its_previous_rate() -> None:
    service, _, _, _ = await _build_estate()
    draft = await _import(
        service,
        2.5,
        cachedInputPerMillion=1.25,
        effectiveUntil=(TODAY + timedelta(days=1)).isoformat(),
    )
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    assert await _rate(service, TODAY - timedelta(days=1)) == 2.5
    assert await _rate(service, TODAY) == 2.5
    assert await _rate(service, TODAY + timedelta(days=2)) is None


async def test_public_history_uses_the_same_normalized_identity_as_price_matching() -> None:
    service, _, _, _ = await _build_estate()
    package = _package(effectiveUntil=(TODAY + timedelta(days=1)).isoformat())
    for item in [package["catalog"][0], package["priceList"]["prices"][0]]:
        item["publisher"] = "openai"
        item["model"] = "GPT-4O"
    draft = await service.catalog.import_bytes(_actor(), json.dumps(package).encode(), None)
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    assert await _rate(service, TODAY) == 1.2
    assert await _rate(service, TODAY + timedelta(days=2)) is None


async def test_a_lost_head_never_silently_substitutes_the_packaged_seed() -> None:
    service, repository, _, _ = await _build_estate()
    draft = await _import(service)
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    repository.catalog_heads.pop(TENANT)
    service.catalog._cache.clear()
    with pytest.raises(ConflictError, match="missing its head"):
        await service.book(TENANT)


@pytest.mark.parametrize("origin", ["admin", "billing"])
async def test_public_refresh_cannot_displace_explicit_rates(origin: str) -> None:
    service, _, _, _ = await _build_estate()
    if origin == "admin":
        await service.add_price(
            _actor(),
            PriceCreate(
                cloud="commercial",
                publisher="OpenAI",
                model="gpt-4o",
                version="2024-11-20",
                deployment_type="GlobalStandard",
                input_per_million=0.7,
                output_per_million=2.8,
                effective_from=TODAY - timedelta(days=2),
                source_url="https://example.invalid/agreement",
                note="Negotiated",
            ),
        )
    else:
        run = await service.import_billing_data(
            _actor(),
            provider="azure",
            pricing_cloud="commercial",
            source_month="2026-09",
            file_name="fictional.csv",
            content=_sample_azure_csv(),
        )
        mapped = await service.update_billing_mappings(
            _actor(),
            run.id,
            BillingMappingsUpdateRequest(
                preview_revision=run.preview_revision,
                mappings=[
                    BillingCandidateMapping(
                        candidate_id=run.candidates[0].candidate_id,
                        scope="cloudModel",
                        cloud="commercial",
                        confirmed=True,
                    )
                ],
            ),
        )
        await service.publish_billing_run(
            _actor(),
            run.id,
            BillingPublishRequest(
                preview_revision=mapped.preview_revision,
                effective_from=TODAY - timedelta(days=1),
                acknowledge_backdate_impact=True,
            ),
        )
    prior = (await service.pricer(TENANT)).rate("endpoint-east/chat", TODAY).entry
    assert prior is not None and prior.origin == origin
    draft = await _import(service, 10)
    preview = await service.catalog.preview(_actor(), draft.id, TODAY)
    assert preview.protected_manual + preview.protected_billing > 0
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    after = (await service.pricer(TENANT)).rate("endpoint-east/chat", TODAY).entry
    assert after is not None and after.id == prior.id and after.origin == origin
    view = await service.price_list(_actor(), "commercial")
    assert any(line.current and line.current.id == prior.id for line in view.lines)


async def test_forward_restore_preserves_intervening_days_and_same_day_corrections() -> None:
    service, _, _, _ = await _build_estate()
    first = await _import(service, 1.2)
    a = await service.catalog.activate(_actor(), first.id, await _approval(service, first.id))
    second = await _import(service, 0.8)
    await service.catalog.activate(
        _actor(), second.id, await _approval(service, second.id, TODAY + timedelta(days=1))
    )
    assert await _rate(service, TODAY) == 1.2
    assert await _rate(service, TODAY + timedelta(days=1)) == 0.8
    recovery = await service.catalog.recover(_actor(), a.id)
    await service.catalog.activate(
        _actor(), recovery.id, await _approval(service, recovery.id, TODAY + timedelta(days=2))
    )
    assert await _rate(service, TODAY + timedelta(days=1)) == 0.8
    assert await _rate(service, TODAY + timedelta(days=2)) == 1.2
    corrected = await _import(service, 0.6)
    await service.catalog.activate(
        _actor(), corrected.id, await _approval(service, corrected.id, TODAY + timedelta(days=2))
    )
    assert await _rate(service, TODAY + timedelta(days=1)) == 0.8
    assert await _rate(service, TODAY + timedelta(days=2)) == 0.6


@pytest.mark.parametrize("expires", [False, True])
async def test_second_activation_can_correct_an_already_retained_historical_window(
    expires: bool,
) -> None:
    service, repository, _, _ = await _build_estate()
    first = await _import(service, 1.2)
    await service.catalog.activate(_actor(), first.id, await _approval(service, first.id))
    head = await repository.get_catalog_head(TENANT)
    before = await service.book(TENANT)
    correction = await _import(
        service, 0.9, **({"effectiveUntil": TODAY.isoformat()} if expires else {})
    )
    yesterday = TODAY - timedelta(days=1)
    approval = await _approval(service, correction.id, yesterday)
    assert approval.acknowledge_backdate_impact
    assert await repository.get_catalog_head(TENANT) == head
    assert (await service.book(TENANT)).entries == before.entries
    assert await _rate(service, yesterday) == 2.5
    assert await _rate(service, TODAY) == 1.2
    with pytest.raises(ValidationError, match="Backdating"):
        await service.catalog.activate(
            _actor(), correction.id,
            approval.model_copy(update={"acknowledge_backdate_impact": False}),
        )
    assert await repository.get_catalog_head(TENANT) == head
    assert (await service.book(TENANT)).entries == before.entries
    await service.catalog.activate(_actor(), correction.id, approval)
    assert await _rate(service, yesterday - timedelta(days=1)) == 2.5
    assert await _rate(service, yesterday) == 0.9
    assert await _rate(service, TODAY) == 0.9
    assert await _rate(service, TODAY + timedelta(days=1)) == (None if expires else 0.9)
    assert len(await repository.list_catalog_activations(TENANT)) == 2


async def test_backdated_regional_correction_trims_only_the_replaced_retained_scope() -> None:
    service, repository, _, _ = await _build_estate()

    async def activate_regions(rate: float, regions: list[str], day: date) -> None:
        package = _package(rate, regions=regions)
        head = await repository.get_catalog_head(TENANT)
        draft = await service.catalog.import_bytes(
            _actor(), json.dumps(package).encode(), head.revision if head else None
        )
        await service.catalog.activate(
            _actor(), draft.id, await _approval(service, draft.id, day)
        )

    await activate_regions(1.8, ["eastus2", "westeurope"], TODAY - timedelta(days=4))
    await activate_regions(1.2, ["eastus2", "westeurope"], TODAY)
    await activate_regions(0.9, ["eastus2"], TODAY - timedelta(days=1))
    book = await service.book(TENANT)
    original = (await service.deployments(TENANT))["endpoint-east/chat"]
    for day, east, west in (
        (TODAY - timedelta(days=2), 1.8, 1.8),
        (TODAY - timedelta(days=1), 0.9, 1.8),
        (TODAY, 0.9, 1.2),
    ):
        for region, expected in (("eastus2", east), ("westeurope", west), ("swedencentral", 2.5)):
            matched = book.match(replace(original, region=region), day)
            assert isinstance(matched, PriceMatch)
            assert matched.entry.input_per_million == expected


async def test_backdate_and_stale_preview_require_fresh_explicit_review() -> None:
    service, repository, _, _ = await _build_estate()
    draft = await _import(service)
    request = await _approval(service, draft.id, TODAY - timedelta(days=5))
    with pytest.raises(ValidationError, match="Backdating"):
        await service.catalog.activate(
            _actor(), draft.id, request.model_copy(update={"acknowledge_backdate_impact": False})
        )
    await service.add_price(
        _actor(),
        PriceCreate(
            cloud="commercial",
            model="different-model",
            input_per_million=1,
            effective_from=TODAY,
            source_url="https://example.invalid/manual",
            note="Concurrent price",
        ),
    )
    with pytest.raises(ConflictError, match="preview changed"):
        await service.catalog.activate(_actor(), draft.id, request)
    assert not repository.catalog_heads
    assert await _rate(service) == 2.5
    fresh = await _approval(service, draft.id, TODAY - timedelta(days=5))
    await service.catalog.activate(_actor(), draft.id, fresh)
    assert await _rate(service, TODAY - timedelta(days=6)) == 2.5
    assert await _rate(service, TODAY - timedelta(days=5)) == 1.2


@pytest.mark.parametrize("repository_kind", ["memory", "cosmos"])
async def test_catalog_commit_cas_and_partial_preparation_never_expose_rates(
    repository_kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, repository, endpoints, _ = await _build_estate()
    if repository_kind == "cosmos":
        cosmos = Cosmos()
        container = cosmos.get_database_client("db").get_container_client("desired")

        async def query_items(self, *, parameters, **kwargs):
            params = {item["name"]: item["value"] for item in parameters}
            for item in self.items.values():
                if (
                    item.get("tenantId") == params["@tenantId"]
                    and item.get("entityType") == params["@entityType"]
                ):
                    yield item

        monkeypatch.setattr(type(container), "query_items", query_items, raising=False)
        repo = CosmosPricingRepository(
            cast(CosmosClient, cosmos), "db", "desired", "audit", owns_client=False
        )
        service = PricingService(
            repo,
            endpoint_repository=endpoints,
            gateway_repository=service._gateways,
            clock=service._clock,
        )
    else:
        repo = repository
    first = await _import(service, 1.2)
    second = await _import(service, 0.8)
    first_approval = await _approval(service, first.id)
    second_approval = await _approval(service, second.id)
    prepare = repo.prepare_catalog

    async def fail_preparation(manifest, chunks):
        await prepare(manifest, chunks[:1])
        raise ConflictError("Injected preparation failure")

    monkeypatch.setattr(repo, "prepare_catalog", fail_preparation)
    with pytest.raises(ConflictError, match="Injected"):
        await service.catalog.activate(_actor(), first.id, first_approval)
    assert await repo.get_catalog_head(TENANT) is None
    assert await _rate(service) == 2.5
    monkeypatch.setattr(repo, "prepare_catalog", prepare)
    results = await asyncio.gather(
        service.catalog.activate(_actor(), first.id, first_approval),
        service.catalog.activate(_actor(), second.id, second_approval),
        return_exceptions=True,
    )
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    assert len(await repo.list_catalog_activations(TENANT)) == 1
    assert await _rate(service) in (1.2, 0.8)


async def test_new_normalizations_use_the_frozen_tenant_catalog_lookup() -> None:
    service, _, _, _ = await _build_estate()
    package = _package()
    package["catalog"][0]["model"] = "example-unreleased"
    package["priceList"]["prices"][0]["model"] = "example-unreleased"
    draft = await service.catalog.import_bytes(_actor(), json.dumps(package).encode(), None)
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    book = await service.book(TENANT)
    lookup = _catalog_meter_lookup(book, "commercial")
    result = _parse_azure_meter_and_model(
        product="Example product",
        meter_category="Foundry Models",
        meter_subcategory="",
        meter_name="Example input",
        unit_of_measure="1M",
        additional_info="",
        catalog_lookup=lookup,
    )
    assert result[:4] == ("OpenAI", "example-unreleased", "2024-11-20", "GlobalStandard")
    gov = _catalog_meter_lookup(book, "government")
    assert ("example product", "example input") not in gov[0]
    replacement = _package()
    replacement["catalog"][0]["model"] = "example-unreleased"
    replacement["priceList"]["prices"][0]["model"] = "example-unreleased"
    replacement["catalog"][0]["meters"]["input"] = "New example input"
    head = await service._repository.get_catalog_head(TENANT)
    assert head is not None
    changed = await service.catalog.import_bytes(
        _actor(), json.dumps(replacement).encode(), head.revision
    )
    await service.catalog.activate(_actor(), changed.id, await _approval(service, changed.id))
    revised = _catalog_meter_lookup(await service.book(TENANT), "commercial")
    assert ("example product", "new example input") in revised[0]
    assert (
        revised[0][("example product", "example input")]
        == lookup[0][("example product", "example input")]
    )


async def test_runtime_catalog_does_not_guess_between_publishers_when_facts_omit_one() -> None:
    service, _, _, _ = await _build_estate()
    package = _package()
    package["catalog"].append({**package["catalog"][0], "publisher": "Other publisher"})
    package["priceList"]["prices"].append(
        {
            **package["priceList"]["prices"][0],
            "id": "commercial.other-publisher",
            "publisher": "Other publisher",
            "inputPerMillion": 4,
        }
    )
    draft = await service.catalog.import_bytes(_actor(), json.dumps(package).encode(), None)
    await service.catalog.activate(_actor(), draft.id, await _approval(service, draft.id))
    facts = (await service.deployments(TENANT))["endpoint-east/chat"]
    book = await service.book(TENANT)
    unknown = book.match(replace(facts, publisher=None), TODAY)
    assert not isinstance(unknown, PriceMatch)
    assert "publisher" in unknown.message
    known = book.match(facts, TODAY)
    assert isinstance(known, PriceMatch) and known.entry.input_per_million == 1.2


def _retail_rows() -> list[dict[str, Any]]:
    return [
        {
            "meterName": meter,
            "armRegionName": "eastus2",
            "retailPrice": price,
            "unitOfMeasure": "1M",
            "effectiveStartDate": TODAY.isoformat() + "T00:00:00Z",
            "tierMinimumUnits": 0,
            "currencyCode": "USD",
            "productName": "Example product",
            "type": "Consumption",
        }
        for meter, price in (("Example input", 2), ("Example output", 8))
    ]


async def test_retail_pages_are_complete_and_only_acquire_commercial() -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "Items": _retail_rows()[:1],
                    "NextPageLink": "https://prices.azure.com/api/retail/prices?$skip=1000",
                },
            )
        return httpx.Response(200, json={"Items": _retail_rows()[1:], "NextPageLink": None})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), auth=httpx.BasicAuth("unused", "not-sent")
    ) as client:
        definition = CatalogDefinition.model_validate(_package()["catalog"][0])
        package = await RetailPricesAdapter(client).acquire([definition], TODAY)
    assert len(requests) == 2
    assert package.price_list.prices[0].input_per_million == 2
    assert {item.cloud for item in package.price_list.prices} == {"commercial"}
    assert not any("authorization" in item.headers for item in requests)


@pytest.mark.parametrize(
    "failure", ["hostile", "loop", "missing", "empty", "wrongCurrency", "conditionalTier"]
)
async def test_bad_retail_pages_fail_closed(failure: str) -> None:
    requests = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = {"Items": _retail_rows(), "NextPageLink": None}
        if failure == "hostile":
            page["NextPageLink"] = "https://127.0.0.1/private"
        elif failure == "loop":
            page["NextPageLink"] = str(request.url)
        elif failure == "missing":
            del page["NextPageLink"]
        elif failure == "empty":
            page["Items"] = []
        elif failure == "wrongCurrency":
            page["Items"][0]["currencyCode"] = "EUR"
        else:
            page["Items"].append({**page["Items"][0], "tierMinimumUnits": 128_000})
        return httpx.Response(200, json=page)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(UpstreamError):
            await RetailPricesAdapter(client).acquire(
                [CatalogDefinition.model_validate(_package()["catalog"][0])], TODAY
            )
    assert len(requests) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        "schema",
        "currency",
        "nonfinite",
        "output",
        "duplicate",
        "windows",
        "aliases",
        "region",
        "signed",
    ],
)
async def test_invalid_packages_leave_the_usable_catalog_and_record_a_bounded_issue(
    invalid: str,
) -> None:
    service, repository, _, _ = await _build_estate()
    package = _package()
    price = package["priceList"]["prices"][0]
    if invalid == "schema":
        package["schemaVersion"] = 2
    elif invalid == "currency":
        package["priceList"]["currency"] = "EUR"
    elif invalid == "nonfinite":
        price["inputPerMillion"] = float("inf")
    elif invalid == "output":
        del price["outputPerMillion"]
    elif invalid == "duplicate":
        package["catalog"].append(dict(package["catalog"][0]))
    elif invalid == "windows":
        package["priceList"]["prices"].append(
            {
                **price,
                "id": "commercial.overlap",
                "effectiveFrom": TODAY.isoformat(),
            }
        )
    elif invalid == "aliases":
        package["catalog"][0]["aliases"] = ["shared-alias"]
        price["aliases"] = ["shared-alias"]
        package["catalog"].append({**package["catalog"][0], "model": "other-model"})
        package["priceList"]["prices"].append(
            {
                **price,
                "id": "commercial.other-alias-owner",
                "model": "other-model",
            }
        )
    elif invalid == "region":
        package["catalog"][0]["regions"] = ["not-an-arm-region"]
    else:
        package["priceList"]["sources"][0]["url"] = (
            "https://example.invalid/prices?sig=private-test-value"
        )
    with pytest.raises(ValidationError):
        await service.catalog.import_bytes(_actor(), json.dumps(package).encode(), None)
    assert await repository.get_catalog_head(TENANT) is None
    assert await _rate(service) == 2.5
    failed = await repository.list_catalog_drafts(TENANT)
    assert len(failed) == 1 and failed[0].status == "invalid"
    assert failed[0].error and len(failed[0].error) < 4_000
    assert "private-test-value" not in json.dumps(
        [event.model_dump(mode="json") for event in repository.audit_events.values()]
    )


@pytest.mark.parametrize("mode", ["limit", "rows", "pages"])
async def test_actual_retail_row_and_page_limits_are_enforced_without_truncation(mode: str) -> None:
    calls = 0
    rows = _retail_rows() * 500

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        items = [*rows, rows[0]] if mode == "rows" and calls == 1 else rows
        return httpx.Response(
            200,
            json={
                "Items": items,
                "NextPageLink": (
                    f"https://prices.azure.com/api/retail/prices?$skip={calls * 1000}"
                    if calls < 100 or mode == "pages"
                    else None
                ),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        adapter = RetailPricesAdapter(client)
        definitions = [CatalogDefinition.model_validate(_package()["catalog"][0])]
        if mode == "limit":
            package = await adapter.acquire(definitions, TODAY)
            assert package.price_list.prices[0].input_per_million == 2
        else:
            with pytest.raises(UpstreamError, match=r"100,000 rows|100 pages"):
                await adapter.acquire(definitions, TODAY)
    assert calls == 100


@pytest.mark.parametrize("excess", [0, 1])
async def test_actual_retail_response_byte_limit_is_stream_bounded(excess: int) -> None:
    payload = json.dumps({"Items": _retail_rows(), "NextPageLink": None}).encode()
    payload += b" " * (50 * 1024 * 1024 - len(payload) + excess)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=payload))
    ) as client:
        adapter = RetailPricesAdapter(client)
        definitions = [CatalogDefinition.model_validate(_package()["catalog"][0])]
        if excess:
            with pytest.raises(UpstreamError, match="50 MiB"):
                await adapter.acquire(definitions, TODAY)
        else:
            assert (await adapter.acquire(definitions, TODAY)).price_list.prices


async def test_retail_honors_http_date_retry_after_within_its_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    waits = []
    after = format_datetime(datetime.now(UTC) + timedelta(seconds=20), usegmt=True)

    async def sleep(seconds):
        waits.append(seconds)

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": after})
        return httpx.Response(200, json={"Items": _retail_rows(), "NextPageLink": None})

    monkeypatch.setattr("mosaic_api.integrations.retail_prices.asyncio.sleep", sleep)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await RetailPricesAdapter(client).acquire(
            [CatalogDefinition.model_validate(_package()["catalog"][0])], TODAY
        )
    assert calls == 2 and len(waits) == 1 and 18 <= waits[0] <= 21


def test_catalog_http_authorization_bounds_and_reconciliation() -> None:
    with TestClient(create_app(_settings(["User"]))) as caller:
        assert caller.get("/api/v1/pricing/catalog").status_code == 403
        assert caller.post("/api/v1/pricing/catalog/imports", json=_package()).status_code == 403
    with TestClient(create_app(_settings(["Admin"]))) as client:
        payload = json.dumps(_package()).encode()
        exact = payload + b" " * (MAX_CATALOG_BYTES - len(payload))
        imported = client.post("/api/v1/pricing/catalog/imports", content=exact)
        assert imported.status_code == 201, imported.text
        assert (
            client.post("/api/v1/pricing/catalog/imports", content=exact + b" ").status_code == 413
        )
        draft_id = imported.json()["id"]
        preview = client.post(
            f"/api/v1/pricing/catalog/drafts/{draft_id}/preview",
            json={"effectiveFrom": "2099-01-01"},
        ).json()
        approval = {
            "draftVersion": preview["draft"]["version"],
            "baseRevision": None,
            "previewDigest": preview["previewDigest"],
            "effectiveFrom": "2099-01-01",
            "note": "Fictional HTTP review",
            "acknowledgeSourceAndGaps": True,
        }
        activated = client.post(
            f"/api/v1/pricing/catalog/drafts/{draft_id}/activate", json=approval
        )
        assert activated.status_code == 200, activated.text
        status = client.get(f"/api/v1/pricing/catalog/drafts/{draft_id}").json()
        assert status["activationId"] == activated.json()["id"]
        assert client.get("/api/v1/pricing/catalog").json()["revision"] == activated.json()["id"]
