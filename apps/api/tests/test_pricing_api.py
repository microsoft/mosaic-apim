"""The price list's routes: administrators only, audited, and never rewriting history."""

from collections.abc import Iterator
from typing import Any, cast

import pytest
from azure.cosmos.aio import CosmosClient
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    DeclaredDeployment,
    EndpointAuthMode,
    ModelEndpoint,
    ModelEndpointCapabilities,
    ModelProvider,
    new_id,
)
from mosaic_api.errors import ConflictError
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment
from mosaic_api.pricing import EndpointPricing, PriceVersion, endpoint_pricing_id
from mosaic_api.repositories import CosmosPricingRepository, InMemoryPricingRepository
from test_model_access_repositories import Cosmos

TENANT = "tenant-test"


def _settings(roles: list[str]) -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        local_roles=roles,
    )


@pytest.fixture
def admin() -> Iterator[TestClient]:
    with TestClient(create_app(_settings(["Admin", "User"]))) as client:
        yield client


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="seed",
        resource_id="seed",
        actor_object_id="tester",
    )


async def _seed(client: TestClient) -> None:
    repository = client.app.state.model_endpoint_repository
    await repository.save_endpoint(
        ModelEndpoint(
            id="endpoint-aoai",
            tenant_id=TENANT,
            name="Contoso Azure OpenAI",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://contoso.openai.azure.com",
            capabilities=ModelEndpointCapabilities(location="eastus2"),
        ),
        _audit(),
    )
    await repository.replace_observed_for_endpoint(
        TENANT,
        "endpoint-aoai",
        [
            ObservedModelDeployment(
                id="obs-chat",
                tenant_id=TENANT,
                endpoint_id="endpoint-aoai",
                snapshot_id="snapshot",
                deployment_name="chat",
                model_name="gpt-4o",
                model_version="2024-11-20",
                model_format="OpenAI",
                sku_name="GlobalStandard",
                sku_capacity=100,
            ),
            ObservedModelDeployment(
                id="obs-mistral",
                tenant_id=TENANT,
                endpoint_id="endpoint-aoai",
                snapshot_id="snapshot",
                deployment_name="mistral",
                model_name="Mistral-Large-2411",
                model_version="2",
                model_format="Mistral AI",
                sku_name="GlobalStandard",
                sku_capacity=1,
            ),
        ],
        "snapshot",
    )
    await repository.save_endpoint(
        ModelEndpoint(
            id="endpoint-partner",
            tenant_id=TENANT,
            name="Fabrikam partner Foundry",
            provider=ModelProvider.AZURE_AI_FOUNDRY,
            endpoint="https://fabrikam.services.ai.azure.com",
            auth_mode=EndpointAuthMode.API_KEY,
            declared_deployments=[
                DeclaredDeployment(
                    deployment_name="gpt-4-1-mini",
                    model_name="gpt-4.1-mini",
                    api_shape=ApiShape.AZURE_OPENAI,
                )
            ],
        ),
        _audit(),
    )
    await repository.save_endpoint(
        ModelEndpoint(
            id="endpoint-compatible",
            tenant_id=TENANT,
            name="Direct models",
            provider=ModelProvider.OPENAI_COMPATIBLE,
            endpoint="https://models.example.com/v1",
        ),
        _audit(),
    )


def _price(**changes: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cloud": "commercial",
        "publisher": "OpenAI",
        "model": "gpt-4o",
        "version": "2024-11-20",
        "deploymentType": "GlobalStandard",
        "inputPerMillion": 2.0,
        "cachedInputPerMillion": 1.0,
        "outputPerMillion": 8.0,
        "effectiveFrom": "2099-01-01",
        "sourceUrl": "https://contoso.example/agreement",
        "note": "Negotiated rate",
    }
    payload.update(changes)
    return payload


def test_every_pricing_route_needs_admin() -> None:
    with TestClient(create_app(_settings(["User"]))) as client:
        for method, path, body in [
            ("get", "/api/v1/pricing", None),
            ("get", "/api/v1/pricing/prices", None),
            ("post", "/api/v1/pricing/prices", _price()),
            ("get", "/api/v1/pricing/prices/line/history", None),
            ("get", "/api/v1/pricing/unpriced", None),
            ("get", "/api/v1/pricing/endpoints", None),
            ("patch", "/api/v1/pricing/endpoints/endpoint-aoai", {"cloud": "government"}),
            ("get", "/api/v1/analytics/cost", None),
        ]:
            response = client.request(method, path, json=body)
            assert response.status_code == 403, (method, path, response.text)


def test_the_overview_lists_clouds_and_the_seed_sources(admin: TestClient) -> None:
    response = admin.get("/api/v1/pricing")

    assert response.status_code == 200, response.text
    overview = response.json()
    assert overview["currency"] == "USD"
    assert [cloud["key"] for cloud in overview["clouds"]][:2] == ["commercial", "government"]
    assert all(cloud["builtIn"] for cloud in overview["clouds"][:2])
    assert overview["sources"]
    assert all(source["url"].startswith("https://") for source in overview["sources"])
    assert "GlobalProvisionedManaged" in overview["deploymentTypes"]


def test_prices_are_listed_per_cloud_with_their_sources(admin: TestClient) -> None:
    commercial = admin.get("/api/v1/pricing/prices", params={"cloud": "commercial"}).json()
    government = admin.get("/api/v1/pricing/prices", params={"cloud": "government"}).json()

    assert commercial["cloudLabel"] == "Azure Commercial"
    assert government["cloudLabel"] == "Azure Government"
    line = next(
        line
        for line in commercial["lines"]
        if line["current"]["model"] == "gpt-4o"
        and line["current"]["version"] == "2024-11-20"
        and line["current"]["deploymentType"] == "GlobalStandard"
    )
    current = line["current"]
    assert (current["origin"], current["status"]) == ("seed", "current")
    assert (current["inputPerMillion"], current["outputPerMillion"]) == (2.5, 10.0)
    assert current["sources"][0]["url"].startswith("https://prices.azure.com/")
    assert {line["current"]["cloud"] for line in government["lines"]} == {"government"}
    assert admin.get("/api/v1/pricing/prices", params={"cloud": "Not A Cloud"}).status_code == 422


async def test_an_override_adds_a_version_and_keeps_the_history(admin: TestClient) -> None:
    prices = admin.get("/api/v1/pricing/prices", params={"cloud": "commercial"}).json()
    line = next(
        line
        for line in prices["lines"]
        if line["current"]["id"] == "commercial.openai.gpt-4o.2024-11-20.globalstandard"
    )

    response = admin.post(
        "/api/v1/pricing/prices", json=_price(overrides=line["current"]["id"])
    )

    assert response.status_code == 201, response.text
    created = response.json()
    assert (created["origin"], created["status"], created["lineId"]) == (
        "admin",
        "upcoming",
        line["lineId"],
    )
    assert created["recordedBy"] == "local-admin"
    assert created["sources"] == [
        {
            "title": "Entered by an administrator",
            "url": "https://contoso.example/agreement",
            "retrievedOn": None,
        }
    ]
    after = admin.get("/api/v1/pricing/prices", params={"cloud": "commercial"}).json()
    updated = next(item for item in after["lines"] if item["lineId"] == line["lineId"])
    # The seeded price still applies until the new one's date.
    assert updated["current"]["id"] == line["current"]["id"]
    assert updated["upcoming"]["id"] == created["id"]
    assert updated["versions"] == 2

    history = admin.get(f"/api/v1/pricing/prices/{line['lineId']}/history").json()
    assert [version["status"] for version in history["versions"]] == ["upcoming", "current"]

    # A correction dated the same day as the version it corrects replaces it.
    correction = admin.post(
        "/api/v1/pricing/prices", json=_price(inputPerMillion=1.5, overrides=created["id"])
    ).json()
    history = admin.get(f"/api/v1/pricing/prices/{line['lineId']}/history").json()
    statuses = {version["id"]: version["status"] for version in history["versions"]}
    assert statuses[correction["id"]] == "upcoming"
    assert statuses[created["id"]] == "corrected"

    repository = cast(InMemoryPricingRepository, admin.app.state.pricing_repository)
    actions = [event.action for event in repository.audit_events.values()]
    assert actions == ["pricing.priceRecorded", "pricing.priceRecorded"]
    event = next(iter(repository.audit_events.values()))
    assert event.details["sourceUrl"] == "https://contoso.example/agreement"
    assert event.actor_object_id == "local-admin"


def test_a_price_for_a_custom_cloud_adds_the_cloud(admin: TestClient) -> None:
    response = admin.post(
        "/api/v1/pricing/prices",
        json=_price(
            cloud="openai",
            publisher=None,
            version=None,
            deploymentType=None,
            effectiveFrom="2026-01-01",
            sourceUrl="https://openai.com/api/pricing",
        ),
    )
    assert response.status_code == 201, response.text
    clouds = [cloud["key"] for cloud in admin.get("/api/v1/pricing").json()["clouds"]]
    assert clouds == ["commercial", "government", "openai"]
    listed = admin.get("/api/v1/pricing/prices", params={"cloud": "openai"}).json()
    assert [line["current"]["model"] for line in listed["lines"]] == ["gpt-4o"]


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"sourceUrl": "ftp://example.com"}, 422),
        ({"note": ""}, 422),
        ({"overrides": "no-such-price"}, 422),
        ({"deployment": "endpoint-aoai/nothing", "monthlyAmount": 10.0,
          "inputPerMillion": None, "cachedInputPerMillion": None, "outputPerMillion": None}, 422),
    ],
)
async def test_bad_prices_are_refused(
    admin: TestClient, changes: dict[str, Any], status: int
) -> None:
    await _seed(admin)
    response = admin.post("/api/v1/pricing/prices", json=_price(**changes))
    assert response.status_code == status, response.text


async def test_a_monthly_amount_prices_one_provisioned_deployment(admin: TestClient) -> None:
    await _seed(admin)
    response = admin.post(
        "/api/v1/pricing/prices",
        json=_price(
            deployment="endpoint-aoai/chat",
            inputPerMillion=None,
            cachedInputPerMillion=None,
            outputPerMillion=None,
            monthlyAmount=1_200.0,
            effectiveFrom="2026-01-01",
        ),
    )
    assert response.status_code == 201, response.text
    assert response.json()["deployment"] == "endpoint-aoai/chat"


async def test_unpriced_deployments_are_listed_with_why(admin: TestClient) -> None:
    await _seed(admin)

    report = admin.get("/api/v1/pricing/unpriced").json()

    rows = {row["key"]: row for row in report["rows"]}
    assert set(rows) == {"endpoint-aoai/mistral", "endpoint-partner/gpt-4-1-mini"}
    assert rows["endpoint-aoai/mistral"]["reason"] == "noPrice"
    partner = rows["endpoint-partner/gpt-4-1-mini"]
    assert (partner["reason"], partner["declared"]) == ("noDeploymentType", True)
    assert (report["deployments"], report["pricedDeployments"]) == (3, 1)


async def test_endpoint_clouds_and_declared_types_can_be_set(admin: TestClient) -> None:
    await _seed(admin)

    endpoints = {item["endpointId"]: item for item in admin.get("/api/v1/pricing/endpoints").json()}
    aoai = endpoints["endpoint-aoai"]
    assert (aoai["detectedCloud"], aoai["cloudSource"], aoai["region"]) == (
        "commercial",
        "detected",
        "eastus2",
    )
    assert {item["deploymentName"]: item["priced"] for item in aoai["deployments"]} == {
        "chat": True,
        "mistral": False,
    }
    compatible = endpoints["endpoint-compatible"]
    assert (compatible["detectedCloud"], compatible["cloud"]) == (None, None)

    response = admin.patch(
        "/api/v1/pricing/endpoints/endpoint-partner",
        json={
            "region": "East US 2",
            "deployments": [{"deploymentName": "gpt-4-1-mini", "deploymentType": "globalstandard"}],
        },
    )
    assert response.status_code == 200, response.text
    partner = response.json()
    assert (partner["region"], partner["regionSource"]) == ("eastus2", "override")
    [declared] = partner["deployments"]
    assert (declared["deploymentType"], declared["deploymentTypeSource"], declared["priced"]) == (
        "GlobalStandard",
        "admin",
        True,
    )

    # Setting the cloud keeps the deployment types already saved.
    overridden = admin.patch(
        "/api/v1/pricing/endpoints/endpoint-partner", json={"cloud": "government"}
    ).json()
    assert (overridden["cloud"], overridden["cloudSource"]) == ("government", "override")
    assert overridden["deployments"][0]["deploymentType"] == "GlobalStandard"
    # Choosing the cloud the host already says clears the override.
    cleared = admin.patch(
        "/api/v1/pricing/endpoints/endpoint-partner", json={"cloud": "commercial"}
    ).json()
    assert cleared["cloudSource"] == "detected"

    repository = cast(InMemoryPricingRepository, admin.app.state.pricing_repository)
    assert [event.action for event in repository.audit_events.values()] == [
        "pricing.endpointUpdated"
    ] * 3


async def test_azure_reported_types_cant_be_overridden(admin: TestClient) -> None:
    await _seed(admin)

    observed = admin.patch(
        "/api/v1/pricing/endpoints/endpoint-aoai",
        json={"deployments": [{"deploymentName": "chat", "deploymentType": "Standard"}]},
    )
    assert observed.status_code == 422
    unknown = admin.patch(
        "/api/v1/pricing/endpoints/endpoint-aoai",
        json={"deployments": [{"deploymentName": "nothing", "deploymentType": "Standard"}]},
    )
    assert unknown.status_code == 422
    assert admin.patch("/api/v1/pricing/endpoints/missing", json={}).status_code == 404


async def test_memory_endpoint_pricing_saves_only_over_what_was_read() -> None:
    repository = InMemoryPricingRepository()
    settings = EndpointPricing(
        id=endpoint_pricing_id(TENANT, "endpoint"), tenant_id=TENANT, endpoint_id="endpoint"
    )
    saved = await repository.save_endpoint_pricing(settings, _audit())
    # Creating it again, or saving over a version someone has since replaced, is refused.
    with pytest.raises(ConflictError):
        await repository.save_endpoint_pricing(settings, _audit())
    await repository.save_endpoint_pricing(saved.model_copy(update={"cloud": "x"}), _audit())
    with pytest.raises(ConflictError):
        await repository.save_endpoint_pricing(saved.model_copy(update={"cloud": "y"}), _audit())


async def test_cosmos_pricing_commits_each_change_with_its_audit_event() -> None:
    cosmos = Cosmos()
    repository = CosmosPricingRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", owns_client=False
    )
    desired = cosmos.containers["desired"]
    version = PriceVersion(
        id="price_1",
        tenant_id=TENANT,
        cloud="commercial",
        model="gpt-4o",
        input_per_million=1.0,
        effective_from="2026-01-01",
        source_url="https://contoso.example/agreement",
        note="Negotiated",
        recorded_by="admin",
    )
    await repository.create_price_version(version, _audit())
    assert (TENANT, "price_1") in desired.items
    with pytest.raises(ConflictError):
        await repository.create_price_version(version, _audit())

    settings = EndpointPricing(
        id=endpoint_pricing_id(TENANT, "endpoint"), tenant_id=TENANT, endpoint_id="endpoint"
    )
    await repository.save_endpoint_pricing(settings, _audit())
    with pytest.raises(ConflictError):
        await repository.save_endpoint_pricing(settings, _audit())
    stored = await repository.get_endpoint_pricing(TENANT, "endpoint")
    assert stored is not None and stored.etag is not None
    await repository.save_endpoint_pricing(stored.model_copy(update={"cloud": "x"}), _audit())
    with pytest.raises(ConflictError):
        await repository.save_endpoint_pricing(stored.model_copy(update={"cloud": "y"}), _audit())
