"""The price list: the shipped seed, how a deployment finds its price, and what PTUs cost."""

import json
from datetime import UTC, date, datetime, timedelta
from importlib import resources
from typing import cast

import pytest
from mosaic_api.domain import (
    DeclaredDeployment,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    utc_now,
)
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import (
    DeploymentFacts,
    DeploymentPricing,
    EndpointPricing,
    EndpointPricingUpdate,
    PriceBook,
    PriceCreate,
    PriceEntry,
    PriceMatch,
    Pricer,
    PriceSeed,
    PriceVersion,
    SourceRef,
    Unpriced,
    build_book,
    deployment_facts,
    detect_cloud,
    load_seed,
    normalize_region,
    parse_seed,
    seed_json_schema,
    token_amount,
    version_entry,
)
from mosaic_api.repositories import (
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
    InMemoryPricingRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.pricing import PricingService
from pydantic import ValidationError as PydanticValidationError

TENANT = "tenant-test"


def _schema_file() -> dict[str, object]:
    text = resources.files("mosaic_api").joinpath("data/model_prices.schema.json").read_text(
        encoding="utf-8"
    )
    return dict(json.loads(text))


def _seed_file() -> dict[str, object]:
    text = resources.files("mosaic_api").joinpath("data/model_prices.json").read_text(
        encoding="utf-8"
    )
    return dict(json.loads(text))


def _facts(**changes: object) -> DeploymentFacts:
    values: dict[str, object] = {
        "key": "endpoint-1/chat",
        "endpoint_id": "endpoint-1",
        "endpoint_name": "Contoso Azure OpenAI",
        "deployment_name": "chat",
        "provider": "azureOpenAi",
        "cloud": "commercial",
        "cloud_source": "detected",
        "detected_cloud": "commercial",
        "region": "eastus2",
        "publisher": "OpenAI",
        "model": "gpt-4o",
        "version": "2024-11-20",
        "deployment_type": "GlobalStandard",
        "deployment_type_source": "observed",
        "capacity": 100,
    }
    values.update(changes)
    return DeploymentFacts(**values)  # type: ignore[arg-type]


def _entry(entry_id: str, **changes: object) -> PriceEntry:
    values: dict[str, object] = {
        "id": entry_id,
        "origin": "admin",
        "cloud": "commercial",
        "publisher": "OpenAI",
        "model": "gpt-4o",
        "aliases": (),
        "version": None,
        "deployment_type": "GlobalStandard",
        "regions": None,
        "deployment": None,
        "input_per_million": 1.0,
        "cached_input_per_million": None,
        "output_per_million": 2.0,
        "ptu_hourly": None,
        "monthly_amount": None,
        "effective_from": date(2025, 1, 1),
        "recorded_at": datetime(2025, 1, 1, tzinfo=UTC),
        "recorded_by": "admin",
        "sources": (SourceRef("Contract", "https://example.com/contract"),),
    }
    values.update(changes)
    return PriceEntry(**values)  # type: ignore[arg-type]


def _matched(book: PriceBook, facts: DeploymentFacts, day: date) -> PriceEntry:
    match = book.match(facts, day)
    assert isinstance(match, PriceMatch), match
    return match.entry


# -- the shipped seed --------------------------------------------------------------------------


def test_seed_matches_its_json_schema() -> None:
    # The committed schema is generated from the model the API validates the seed with, so the
    # two can't drift: `python -m scripts.price_seed` writes both.
    assert _schema_file() == seed_json_schema()
    seed = PriceSeed.model_validate(_seed_file(), strict=False)
    assert seed.currency == "USD"
    assert seed.prices


def test_every_seeded_price_cites_a_listed_source() -> None:
    raw = _seed_file()
    sources = {source["id"]: source for source in raw["sources"]}  # type: ignore[attr-defined]
    for source in sources.values():
        assert source["url"].startswith("https://")
        assert date.fromisoformat(source["retrievedOn"])
    for price in raw["prices"]:  # type: ignore[attr-defined]
        assert price["sourceIds"], price["id"]
        assert all(source_id in sources for source_id in price["sourceIds"]), price["id"]
    for item in raw["ptuThroughput"]:  # type: ignore[attr-defined]
        assert all(source_id in sources for source_id in item["sourceIds"]), item["model"]


def test_a_price_without_a_source_or_citing_a_missing_one_is_refused() -> None:
    raw = _seed_file()
    price = dict(raw["prices"][0])  # type: ignore[index]

    unsourced = {**raw, "prices": [{**price, "sourceIds": []}]}
    with pytest.raises(PydanticValidationError, match="sourceIds"):
        parse_seed(json.dumps(unsourced))

    missing = {**raw, "prices": [{**price, "sourceIds": ["not-a-source"]}]}
    with pytest.raises(PydanticValidationError, match="cites unknown sources"):
        parse_seed(json.dumps(missing))


def test_a_price_with_an_unknown_field_is_refused() -> None:
    raw = _seed_file()
    price = dict(raw["prices"][0])  # type: ignore[index]
    with pytest.raises(PydanticValidationError):
        parse_seed(json.dumps({**raw, "prices": [{**price, "discount": 0.5}]}))


def test_the_seed_ships_inside_the_package() -> None:
    seed = load_seed()
    assert seed.last_updated <= utc_now().date()
    # Every seeded price is a public list price in one of the two built-in clouds.
    assert {price.cloud for price in seed.prices} == {"commercial", "government"}


@pytest.mark.parametrize(
    ("model", "version", "deployment_type", "region", "price"),
    [
        # The demo estate's published models, priced from the Retail Prices API.
        ("gpt-4o", "2024-11-20", "GlobalStandard", "eastus2", (2.5, 10.0)),
        ("gpt-4o-mini", "2024-07-18", "GlobalStandard", "eastus2", (0.15, 0.6)),
        ("text-embedding-3-large", "1", "Standard", "eastus2", (0.143, None)),
        ("Phi-4", "7", "GlobalStandard", "eastus2", (0.125, 0.5)),
        # A regional price differs by region.
        ("gpt-4o", "2024-11-20", "Standard", "eastus2", (2.75, 11.0)),
        ("gpt-4o", "2024-11-20", "Standard", "swedencentral", (3.025, 12.1)),
    ],
)
def test_seed_prices_the_demo_models(
    model: str,
    version: str,
    deployment_type: str,
    region: str,
    price: tuple[float, float | None],
) -> None:
    book = build_book()
    facts = _facts(
        model=model,
        version=version,
        deployment_type=deployment_type,
        region=region,
        publisher="Microsoft" if model == "Phi-4" else "OpenAI",
    )
    entry = _matched(book, facts, date(2026, 9, 1))
    assert entry.origin == "seed"
    assert (entry.input_per_million, entry.output_per_million) == price
    assert entry.sources and all(source.url.startswith("https://") for source in entry.sources)


def test_government_prices_differ_from_commercial() -> None:
    book = build_book()
    gov = _facts(cloud="government", deployment_type="Standard", region="usgovvirginia")
    entry = _matched(book, gov, date(2026, 9, 1))
    assert entry.cloud == "government"
    assert entry.input_per_million == pytest.approx(3.438)


def test_a_model_the_api_doesnt_list_stays_unpriced() -> None:
    book = build_book()
    match = book.match(
        _facts(model="Mistral-Large-2411", publisher="Mistral AI", version="2"), date(2026, 9, 1)
    )
    assert isinstance(match, Unpriced)
    assert match.reason == "noPrice"
    assert "Mistral-Large-2411" in match.message


# -- matching ----------------------------------------------------------------------------------


def test_the_most_specific_price_wins() -> None:
    book = PriceBook(
        [
            _entry("any-version", input_per_million=1.0),
            _entry("versioned", version="2024-11-20", input_per_million=2.0),
            _entry("regional", regions=("eastus2",), input_per_million=3.0),
            _entry(
                "versioned-regional",
                version="2024-11-20",
                regions=("eastus2",),
                input_per_million=4.0,
            ),
            _entry("wildcard", model="*", input_per_million=5.0),
        ]
    )
    day = date(2026, 1, 1)
    assert _matched(book, _facts(), day).id == "versioned-regional"
    # Dropping the region falls back to the version.
    assert _matched(book, _facts(region="westus"), day).id == "versioned"
    # Then dropping the version too.
    assert _matched(book, _facts(region="westus", version="2024-08-06"), day).id == "any-version"
    # A version-specific price beats a region-specific one.
    assert _matched(book, _facts(version="2024-11-20", region="eastus2"), day).id == (
        "versioned-regional"
    )
    assert _matched(book, _facts(model="gpt-4.1"), day).id == "wildcard"


def test_a_price_for_one_deployment_beats_every_other() -> None:
    book = PriceBook(
        [
            _entry("general", version="2024-11-20", regions=("eastus2",)),
            _entry("negotiated", deployment="endpoint-1/chat", input_per_million=0.5),
        ]
    )
    assert _matched(book, _facts(), date(2026, 1, 1)).id == "negotiated"
    assert _matched(book, _facts(key="endpoint-1/other"), date(2026, 1, 1)).id == "general"


def test_aliases_and_case_match() -> None:
    book = PriceBook([_entry("alias", model="gpt-35-turbo", aliases=("gpt-3.5-turbo",))])
    assert _matched(book, _facts(model="GPT-3.5-Turbo"), date(2026, 1, 1)).id == "alias"


def test_a_regional_price_never_applies_outside_its_regions() -> None:
    book = PriceBook([_entry("east", regions=("eastus",))])
    match = book.match(_facts(region="westeurope"), date(2026, 1, 1))
    assert isinstance(match, Unpriced)
    match = book.match(_facts(region=None), date(2026, 1, 1))
    assert isinstance(match, Unpriced)


def test_a_known_publisher_must_agree() -> None:
    book = PriceBook([_entry("mistral", publisher="Mistral AI")])
    assert isinstance(book.match(_facts(publisher="OpenAI"), date(2026, 1, 1)), Unpriced)
    # A deployment whose publisher MOSAIC doesn't know can still match.
    assert _matched(book, _facts(publisher=None), date(2026, 1, 1)).id == "mistral"
    assert _matched(book, _facts(publisher="MistralAI"), date(2026, 1, 1)).id == "mistral"


def test_unpriced_reasons_say_what_to_fix() -> None:
    book = PriceBook([_entry("later", effective_from=date(2026, 6, 1))])
    no_cloud = book.match(_facts(cloud=None), date(2026, 7, 1))
    assert isinstance(no_cloud, Unpriced) and no_cloud.reason == "noCloud"
    no_model = book.match(_facts(model=None), date(2026, 7, 1))
    assert isinstance(no_model, Unpriced) and no_model.reason == "noModel"
    early = book.match(_facts(), date(2026, 5, 31))
    assert isinstance(early, Unpriced) and early.reason == "notYetEffective"
    assert "2026-06-01" in early.message
    untyped = book.match(_facts(model="gpt-5", deployment_type=None), date(2026, 7, 1))
    assert isinstance(untyped, Unpriced) and untyped.reason == "noDeploymentType"


# -- effective dates ---------------------------------------------------------------------------


def test_a_new_price_never_rewrites_the_days_before_it() -> None:
    seed = _entry("seed", origin="seed", input_per_million=2.5)
    change = _entry(
        "change",
        input_per_million=2.0,
        effective_from=date(2026, 9, 15),
        recorded_at=datetime(2026, 9, 20, tzinfo=UTC),
    )
    book = PriceBook([seed, change])
    assert _matched(book, _facts(), date(2026, 9, 14)).id == "seed"
    assert _matched(book, _facts(), date(2026, 9, 15)).id == "change"


def test_a_correction_with_the_same_date_wins_and_later_dates_still_change_it() -> None:
    first = _entry("first", input_per_million=3.0, effective_from=date(2026, 1, 1))
    correction = _entry(
        "correction",
        input_per_million=2.5,
        effective_from=date(2026, 1, 1),
        recorded_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    later = _entry("later", input_per_million=2.0, effective_from=date(2026, 6, 1))
    book = PriceBook([first, correction, later])
    assert _matched(book, _facts(), date(2026, 2, 1)).id == "correction"
    assert _matched(book, _facts(), date(2026, 7, 1)).id == "later"


async def test_a_correction_keeps_beating_the_seeded_price_a_later_release_ships_again() -> None:
    # The seed a later release ships is stamped after the administrator's correction.
    seed = PriceSeed.model_validate(
        {
            "schemaVersion": 1,
            "lastUpdated": "2027-03-31",
            "currency": "USD",
            "sources": [
                {
                    "id": "retail",
                    "title": "Retail Prices API",
                    "url": "https://prices.azure.com/api/retail/prices",
                    "retrievedOn": "2027-03-31",
                }
            ],
            "prices": [
                {
                    "id": "gpt-4o",
                    "cloud": "commercial",
                    "publisher": "OpenAI",
                    "model": "gpt-4o",
                    "deploymentType": "GlobalStandard",
                    "inputPerMillion": 2.5,
                    "outputPerMillion": 10,
                    "effectiveFrom": "2025-01-01",
                    "sourceIds": ["retail"],
                }
            ],
        }
    )
    repository = InMemoryPricingRepository()
    correction = PriceVersion(
        id="price_fix",
        tenant_id=TENANT,
        cloud="commercial",
        publisher="OpenAI",
        model="gpt-4o",
        deployment_type="GlobalStandard",
        input_per_million=2.0,
        output_per_million=8.0,
        effective_from=date(2025, 1, 1),
        source_url="https://example.com/contract",
        note="Our rate",
        overrides="gpt-4o",
        recorded_by="admin",
        created_at=datetime(2026, 10, 15, tzinfo=UTC),
    )
    repository.versions[(TENANT, correction.id)] = correction
    service = PricingService(
        repository,
        endpoint_repository=InMemoryModelEndpointRepository(),
        gateway_repository=InMemoryGatewayRepository(),
        seed=seed,
        clock=lambda: datetime(2027, 4, 1, tzinfo=UTC),
    )
    actor = Actor(object_id="admin", tenant_id=TENANT)

    book = await service.book(TENANT)
    assert _matched(book, _facts(), date(2027, 4, 1)).id == "price_fix"
    [line] = (await service.price_list(actor, "commercial")).lines
    assert line.current is not None and line.current.id == "price_fix"
    history = await service.history(actor, line.line_id)
    assert [(view.id, view.status) for view in history.versions] == [
        ("price_fix", "current"),
        ("gpt-4o", "corrected"),
    ]


def test_price_changes_within_a_month_are_found() -> None:
    book = PriceBook(
        [
            _entry("first", effective_from=date(2026, 1, 1)),
            _entry("mid", effective_from=date(2026, 3, 16)),
        ]
    )
    assert book.changes_within(date(2026, 3, 1), date(2026, 3, 31))
    assert not book.changes_within(date(2026, 1, 1), date(2026, 1, 31))
    assert not book.changes_within(date(2026, 4, 1), date(2026, 4, 30))


def test_a_replaced_seed_price_ends_the_day_before_its_replacement() -> None:
    # A seed refresh keeps the price it replaced, with the day it ended.
    old = _entry("old", origin="seed", input_per_million=2.5, effective_until=date(2026, 5, 10))
    new = _entry("new", origin="seed", input_per_million=2.0, effective_from=date(2026, 5, 11))
    book = PriceBook([old, new])
    assert _matched(book, _facts(), date(2026, 5, 10)).id == "old"
    assert _matched(book, _facts(), date(2026, 5, 11)).id == "new"
    # An end is a change too, so a month it falls in is priced by the day.
    assert PriceBook([old]).changes_within(date(2026, 5, 1), date(2026, 5, 31))


def test_a_price_that_ended_with_nothing_after_it_prices_nothing_after_its_end() -> None:
    book = PriceBook([_entry("old", effective_until=date(2026, 5, 10))])
    ended = book.match(_facts(), date(2026, 6, 1))
    assert isinstance(ended, Unpriced) and ended.reason == "noPrice"
    assert "ended on 2026-05-10" in ended.message
    early = book.match(_facts(), date(2024, 12, 31))
    assert isinstance(early, Unpriced) and early.reason == "notYetEffective"


def test_a_seeded_price_cant_end_before_it_starts() -> None:
    seed = _seed_file()
    prices = cast(list[dict[str, object]], seed["prices"])
    prices[0] = {**prices[0], "effectiveUntil": "2000-01-01"}
    with pytest.raises(PydanticValidationError, match="ends before it takes effect"):
        PriceSeed.model_validate(seed)


async def test_an_ended_price_leaves_the_list_but_keeps_its_history() -> None:
    seed = PriceSeed.model_validate(
        {
            "schemaVersion": 1,
            "lastUpdated": "2026-09-30",
            "currency": "USD",
            "sources": [
                {
                    "id": "contract",
                    "title": "Contract",
                    "url": "https://example.com/contract",
                    "retrievedOn": "2026-09-30",
                }
            ],
            "prices": [
                {
                    "id": "gpt.until-2026-05-10",
                    "cloud": "commercial",
                    "model": "gpt-4o",
                    "regions": ["eastus", "westus"],
                    "inputPerMillion": 2.5,
                    "effectiveFrom": "2025-01-01",
                    "effectiveUntil": "2026-05-10",
                    "sourceIds": ["contract"],
                },
                {
                    "id": "gpt",
                    "cloud": "commercial",
                    "model": "gpt-4o",
                    "regions": ["eastus"],
                    "inputPerMillion": 2.0,
                    "effectiveFrom": "2026-05-11",
                    "sourceIds": ["contract"],
                },
            ],
        }
    )
    service = PricingService(
        InMemoryPricingRepository(),
        endpoint_repository=InMemoryModelEndpointRepository(),
        gateway_repository=InMemoryGatewayRepository(),
        seed=seed,
        clock=lambda: datetime(2026, 9, 30, 12, tzinfo=UTC),
    )
    actor = Actor(object_id="admin", tenant_id=TENANT)

    listed = await service.price_list(actor, "commercial")

    assert [line.current.id for line in listed.lines if line.current] == ["gpt"]
    ended = next(entry for entry in (await service.book(TENANT)).entries if entry.effective_until)
    history = await service.history(actor, ended.line_id)
    assert [(view.status, view.effective_until) for view in history.versions] == [
        ("past", date(2026, 5, 10))
    ]
    overview = await service.overview(actor)
    assert next(cloud for cloud in overview.clouds if cloud.key == "commercial").prices == 1


# -- what tokens and PTUs cost -----------------------------------------------------------------


def test_tokens_are_priced_per_million_with_cached_tokens_at_the_input_price() -> None:
    entry = _entry(
        "price", input_per_million=2.5, cached_input_per_million=1.25, output_per_million=10
    )
    assert token_amount(entry, 1_000_000, 100_000) == pytest.approx(3.5)
    no_output = _entry("embeddings", output_per_million=None)
    assert token_amount(no_output, 1_000_000, 0) == pytest.approx(1.0)
    assert token_amount(no_output, 1_000_000, 10) is None


def test_ptu_cost_is_capacity_times_the_hourly_rate() -> None:
    facts = _facts(deployment_type="GlobalProvisionedManaged", capacity=15)
    rate = _entry("ptu", model="*", deployment_type="GlobalProvisionedManaged", ptu_hourly=1.0,
                  input_per_million=None, output_per_million=None)
    pricer = Pricer(PriceBook([rate]), {facts.key: facts})
    day = pricer.rate(facts.key, date(2026, 3, 18))
    assert day.kind == "provisioned"
    assert day.daily == pytest.approx(15 * 24)
    # March 2026 has 31 days; the month so far, through the 18th, is 18 of them.
    assert pricer.reserved_cost(facts.key, date(2026, 3, 1), date(2026, 3, 18)) == pytest.approx(
        15 * 24 * 18
    )
    assert pricer.reserved_cost(facts.key, date(2026, 2, 1), date(2026, 3, 18)) == pytest.approx(
        15 * 24 * 28
    )


def test_a_monthly_amount_for_one_deployment_replaces_the_hourly_rate() -> None:
    facts = _facts(deployment_type="GlobalProvisionedManaged", capacity=15)
    book = PriceBook(
        [
            _entry("ptu", model="*", deployment_type="GlobalProvisionedManaged", ptu_hourly=1.0,
                   input_per_million=None, output_per_million=None),
            _entry("reservation", deployment=facts.key, monthly_amount=3_100.0,
                   input_per_million=None, output_per_million=None,
                   effective_from=date(2026, 3, 1)),
        ]
    )
    pricer = Pricer(book, {facts.key: facts})
    assert pricer.rate(facts.key, date(2026, 3, 5)).daily == pytest.approx(100.0)
    assert pricer.rate(facts.key, date(2026, 2, 5)).daily == pytest.approx(15 * 24)


def test_a_provisioned_deployment_needs_its_capacity() -> None:
    facts = _facts(deployment_type="ProvisionedManaged", capacity=None)
    book = PriceBook(
        [_entry("ptu", model="*", deployment_type="ProvisionedManaged", ptu_hourly=2.0,
                input_per_million=None, output_per_million=None)]
    )
    match = book.match(facts, date(2026, 3, 1))
    assert isinstance(match, Unpriced) and match.reason == "noCapacity"


def test_reserved_capacity_is_priced_from_when_it_was_deployed() -> None:
    facts = _facts(
        deployment_type="GlobalProvisionedManaged", capacity=10, deployed_on=date(2026, 3, 10)
    )
    book = PriceBook(
        [_entry("ptu", model="*", deployment_type="GlobalProvisionedManaged", ptu_hourly=1.0,
                input_per_million=None, output_per_million=None)]
    )
    pricer = Pricer(book, {facts.key: facts})
    assert pricer.rate(facts.key, date(2026, 3, 9)).kind == "unpriced"
    assert pricer.reserved_cost(facts.key, date(2026, 3, 1), date(2026, 3, 18)) == pytest.approx(
        10 * 24 * 9
    )
    # Without Azure's creation time, MOSAIC counts from when it first saw the deployment governed.
    unknown = _facts(deployment_type="GlobalProvisionedManaged", capacity=10)
    later = Pricer(book, {unknown.key: unknown}, since={unknown.key: date(2026, 3, 17)})
    assert later.reserved_cost(unknown.key, date(2026, 3, 1), date(2026, 3, 18)) == pytest.approx(
        10 * 24 * 2
    )


def test_seeded_ptu_rates_apply_to_every_model_of_a_publisher() -> None:
    book = build_book()
    facts = _facts(
        deployment_type="GlobalProvisionedManaged", model="gpt-4.1", version="2025-04-14"
    )
    entry = _matched(book, facts, date(2026, 9, 1))
    assert entry.model == "*"
    assert entry.ptu_hourly == pytest.approx(1.0)
    throughput = book.throughput_for("gpt-4.1", "2025-04-14")
    assert throughput is not None and throughput.input_tokens_per_minute_per_ptu == 3_000


# -- deployments' facts ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "cloud"),
    [
        ("https://contoso.openai.azure.com/", "commercial"),
        ("https://contoso.cognitiveservices.azure.com", "commercial"),
        ("https://contoso.services.ai.azure.com/api/projects/x", "commercial"),
        ("https://contoso.openai.azure.us/", "government"),
        ("https://contoso.cognitiveservices.azure.us", "government"),
        ("https://contoso.openai.azure.cn/", None),
        ("https://api.openai.com/v1", None),
        ("https://models.example.com", None),
    ],
)
def test_the_cloud_is_detected_from_the_host(url: str, cloud: str | None) -> None:
    assert detect_cloud(url) == cloud


def test_regions_are_normalized() -> None:
    assert normalize_region("East US 2") == "eastus2"
    assert normalize_region("eastus2") == "eastus2"
    assert normalize_region(None) is None


def _endpoint(**changes: object) -> ModelEndpoint:
    values: dict[str, object] = {
        "id": "endpoint-1",
        "tenant_id": TENANT,
        "name": "Contoso Azure OpenAI",
        "provider": ModelProvider.AZURE_OPENAI,
        "endpoint": "https://contoso.openai.azure.com",
        "capabilities": ModelEndpointCapabilities(location="East US 2"),
    }
    values.update(changes)
    return ModelEndpoint(**values)  # type: ignore[arg-type]


def test_observed_deployments_bring_their_own_facts() -> None:
    observed = ObservedModelDeployment(
        id="obs",
        tenant_id=TENANT,
        endpoint_id="endpoint-1",
        snapshot_id="snapshot",
        deployment_name="chat",
        model_name="gpt-4o",
        model_version="2024-11-20",
        model_format="OpenAI",
        sku_name="globalstandard",
        sku_capacity=450,
        deployed_at=datetime(2026, 1, 2, 3, tzinfo=UTC),
    )
    facts = deployment_facts(_endpoint(), observed=observed)
    assert (facts.cloud, facts.cloud_source, facts.region) == ("commercial", "detected", "eastus2")
    assert (facts.publisher, facts.deployment_type, facts.capacity) == (
        "OpenAI",
        "GlobalStandard",
        450,
    )
    assert facts.deployed_on == date(2026, 1, 2)
    # An administrator's cloud override wins over what the host says.
    overridden = deployment_facts(
        _endpoint(),
        observed=observed,
        settings=EndpointPricing(
            id="settings", tenant_id=TENANT, endpoint_id="endpoint-1", cloud="sovereign-x"
        ),
    )
    assert (overridden.cloud, overridden.cloud_source) == ("sovereign-x", "override")


def test_a_declared_deployment_takes_its_type_from_an_administrator() -> None:
    endpoint = _endpoint(capabilities=ModelEndpointCapabilities())
    declared = DeclaredDeployment(
        deployment_name="gpt-4-1-mini", model_name="gpt-4.1-mini", api_shape="azureOpenAi"
    )
    unknown = deployment_facts(endpoint, declared=declared)
    assert (unknown.declared, unknown.deployment_type, unknown.region) == (True, None, None)

    settings = EndpointPricing(
        id="settings",
        tenant_id=TENANT,
        endpoint_id=endpoint.id,
        region="eastus2",
        deployments=[
            DeploymentPricing(deployment_name="GPT-4-1-MINI", deployment_type="GlobalStandard")
        ],
    )
    typed = deployment_facts(endpoint, declared=declared, settings=settings)
    assert (typed.deployment_type, typed.deployment_type_source, typed.region) == (
        "GlobalStandard",
        "admin",
        "eastus2",
    )
    entry = _matched(build_book(), typed, date(2026, 9, 1))
    assert (entry.model, entry.input_per_million) == ("gpt-4.1-mini", 0.4)


# -- administrators' prices --------------------------------------------------------------------


def _create(**changes: object) -> PriceCreate:
    values: dict[str, object] = {
        "cloud": "commercial",
        "publisher": "OpenAI",
        "model": "gpt-4o",
        "deploymentType": "globalstandard",
        "inputPerMillion": 2.0,
        "outputPerMillion": 8.0,
        "effectiveFrom": "2026-10-01",
        "sourceUrl": "https://contoso.example/agreement",
        "note": "Negotiated rate",
    }
    values.update(changes)
    return PriceCreate.model_validate(values)


def test_an_administrator_price_is_cleaned_up() -> None:
    created = _create(regions=["East US 2", "eastus2", "West US"], aliases=[" a ", "A", "b"])
    assert created.deployment_type == "GlobalStandard"
    assert created.regions == ["eastus2", "westus"]
    assert created.aliases == ["a", "b"]


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"sourceUrl": "http://insecure.example"}, "sourceUrl"),
        ({"note": "  "}, "note"),
        ({"inputPerMillion": None, "outputPerMillion": None}, "either prices per million"),
        ({"ptuHourly": 1.0}, "either prices per million"),
        (
            {"inputPerMillion": None, "outputPerMillion": None, "monthlyAmount": 100.0},
            "one deployment",
        ),
        (
            {"inputPerMillion": None, "outputPerMillion": None, "ptuHourly": 1.0},
            "provisioned deployment type",
        ),
        ({"cloud": "Azure Commercial"}, "cloud"),
        ({"model": "*", "publisher": None}, "needs a publisher"),
        ({"inputPerMillion": -1}, "inputPerMillion"),
    ],
)
def test_an_administrator_price_is_validated(changes: dict[str, object], message: str) -> None:
    with pytest.raises(PydanticValidationError, match=message):
        _create(**changes)


def test_an_administrator_version_is_matched_like_a_seeded_one() -> None:
    version = PriceVersion(
        id="price_1",
        tenant_id=TENANT,
        cloud="openai",
        model="gpt-4o",
        input_per_million=2.5,
        output_per_million=10.0,
        effective_from=date(2026, 1, 1),
        source_url="https://openai.com/api/pricing",
        note="Direct from the provider",
        recorded_by="admin-oid",
    )
    entry = version_entry(version)
    book = PriceBook([entry])
    facts = _facts(
        cloud="openai", publisher=None, deployment_type=None, version=None, region=None
    )
    assert _matched(book, facts, date(2026, 2, 1)).id == "price_1"
    assert entry.sources[0].url == "https://openai.com/api/pricing"
    assert entry.recorded_at <= utc_now() + timedelta(seconds=1)
    assert "openai" in book.clouds()


def test_endpoint_pricing_updates_are_cleaned_up() -> None:
    update = EndpointPricingUpdate.model_validate(
        {
            "cloud": " Government ",
            "region": "US Gov Virginia",
            "deployments": [{"deploymentName": "x", "deploymentType": "datazonestandard"}],
        }
    )
    assert (update.cloud, update.region) == ("government", "usgovvirginia")
    assert update.deployments[0].deployment_type == "DataZoneStandard"
    with pytest.raises(PydanticValidationError):
        EndpointPricingUpdate.model_validate({"cloud": "not a cloud!"})
