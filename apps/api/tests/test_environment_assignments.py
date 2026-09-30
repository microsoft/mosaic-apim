from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.config import Settings
from mosaic_api.domain import (
    ApiShape,
    AuditEvent,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    ModelApi,
    ModelEndpoint,
    ModelProvider,
    Publication,
    PublishedResource,
    PublishedResourceKind,
    new_id,
)
from mosaic_api.environments import BlockedPublication
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedModelDeployment, ObservedProduct
from mosaic_api.repositories import InMemoryEntitlementRepository, InMemoryGatewayRepository
from mosaic_api.services.environments import (
    PUBLICATION_BUSY_MESSAGE,
    AssignmentRecord,
    AssignmentScopes,
    EnvironmentService,
)

TENANT = "tenant-test"


def _audit(resource: str = "test") -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action=f"{resource}.updated",
        resource_type=resource,
        resource_id=resource,
        actor_object_id="admin",
    )


@pytest.fixture
def assignment_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as client:
        yield client


async def _seed_gateway(
    client: TestClient, gateway_id: str = "gateway-1", *, environment: str | None = None
) -> Gateway:
    gateway = Gateway(
        id=gateway_id,
        tenant_id=TENANT,
        name=f"Gateway {gateway_id}",
        azure_resource_id=(
            "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
            f"/providers/Microsoft.ApiManagement/service/{gateway_id}"
        ),
        subscription_id="00000000-0000-0000-0000-000000000000",
        resource_group="rg",
        service_name=gateway_id,
        environment=environment,
    )
    return await client.app.state.gateway_repository.save_gateway(gateway, _audit("gateway"))


async def _seed_endpoint(
    client: TestClient, endpoint_id: str = "endpoint-1", *, environment: str | None = None
) -> ModelEndpoint:
    endpoint = ModelEndpoint(
        id=endpoint_id,
        tenant_id=TENANT,
        name=f"Endpoint {endpoint_id}",
        provider=ModelProvider.AZURE_OPENAI,
        endpoint=f"https://{endpoint_id}.example.com/",
        environment=environment,
    )
    return await client.app.state.model_endpoint_repository.save_endpoint(
        endpoint, _audit("endpoint")
    )


async def _seed_applied_publication(
    client: TestClient, gateway_id: str, endpoint_id: str, publication_id: str = "pub-1"
) -> Publication:
    publication = Publication(
        id=publication_id,
        tenant_id=TENANT,
        gateway_id=gateway_id,
        model_endpoint_id=endpoint_id,
        deployment_name="gpt-4o",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Chat",
        api_name="chat",
        api_path="chat",
        backend_name="chat",
        fragment_name="chat",
        product_name="chat",
        subscription_name="chat",
        shape_version="v1",
        api_shape=ApiShape.AZURE_OPENAI,
        resources=[
            PublishedResource(
                kind=PublishedResourceKind.API,
                name="chat",
                resource_id="apis/chat",
                created_by_mosaic=True,
            )
        ],
    )
    return await client.app.state.gateway_repository.save_publication(
        publication, _audit("publication")
    )


async def test_environment_suggestions_prefer_azure_tags_and_list_only_unclassified(
    assignment_client: TestClient,
) -> None:
    gateways: InMemoryGatewayRepository = assignment_client.app.state.gateway_repository
    gateway = await _seed_gateway(assignment_client, "gateway-prod")
    await gateways.save_gateway(
        gateway.model_copy(
            update={
                "azure_environment_tag": "prod",
                "environment_label": "Pre Prod",
            }
        ),
        _audit("gateway"),
    )
    classified = await _seed_gateway(assignment_client, "gateway-classified", environment="test")
    await gateways.save_gateway(
        classified.model_copy(update={"azure_environment_tag": "prod"}), _audit("gateway")
    )
    endpoint = await _seed_endpoint(assignment_client, "endpoint-stage")
    await assignment_client.app.state.model_endpoint_repository.save_endpoint(
        endpoint.model_copy(update={"environment_label": "Pre Prod"}), _audit("endpoint")
    )

    response = assignment_client.get("/api/v1/environment-suggestions")

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [item["resourceId"] for item in items] == ["gateway-prod", "endpoint-stage"]
    assert items[0]["suggestedEnvironment"] == "production"
    assert items[0]["source"] == "azureTag"
    assert items[1]["suggestedEnvironment"] == "staging"
    assert items[1]["source"] == "legacyLabel"


def test_environment_suggestions_are_admin_only(settings: Settings) -> None:
    settings.local_roles = ["User"]
    app = create_app(settings)
    app.state.authenticator = LocalAuthenticator(settings.tenant_id, roles=["User"])
    with TestClient(app) as client:
        assert client.get("/api/v1/environment-suggestions").status_code == 403


async def test_assignment_shape_errors_and_unclassify(
    assignment_client: TestClient,
) -> None:
    gateway = await _seed_gateway(assignment_client, "gateway-shape", environment="test")

    empty = assignment_client.post("/api/v1/environment-assignments", json={"assignments": []})
    assert empty.status_code == 422
    assert empty.json()["details"]["reason"] == "invalidAssignment"

    duplicate = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {"resourceKind": "gateway", "resourceId": gateway.id, "environment": "test"},
                {"resourceKind": "gateway", "resourceId": gateway.id, "environment": "test"},
            ]
        },
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["details"]["reason"] == "invalidAssignment"

    missing = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": "missing",
                    "environment": "missing-env",
                }
            ]
        },
    )
    assert missing.status_code == 404
    assert missing.json()["details"]["reason"] == "resourceNotFound"

    unknown = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": gateway.id,
                    "environment": "missing-env",
                }
            ]
        },
    )
    assert unknown.status_code == 422
    assert unknown.json()["details"]["reason"] == "unknownEnvironment"

    unclassified = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {"resourceKind": "gateway", "resourceId": gateway.id, "environment": None}
            ]
        },
    )
    assert unclassified.status_code == 200, unclassified.text
    assert unclassified.json()["results"][0]["previousEnvironment"] == "test"
    assert unclassified.json()["results"][0]["environment"] is None


async def test_assignment_batch_allows_linked_gateway_and_endpoint_to_move_together(
    assignment_client: TestClient,
) -> None:
    await _seed_gateway(assignment_client, "gateway-prod")
    await _seed_endpoint(assignment_client, "endpoint-prod")
    await _seed_applied_publication(assignment_client, "gateway-prod", "endpoint-prod")

    refused = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": "gateway-prod",
                    "environment": "production",
                }
            ]
        },
    )

    assert refused.status_code == 409
    details = refused.json()["details"]
    assert details["reason"] == "publicationsBlocked"
    assert details["suggestedAssignments"] == [
        {
            "resourceKind": "modelEndpoint",
            "resourceId": "endpoint-prod",
            "environment": "production",
        }
    ]

    accepted = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": "gateway-prod",
                    "environment": "production",
                },
                {
                    "resourceKind": "modelEndpoint",
                    "resourceId": "endpoint-prod",
                    "environment": "production",
                },
            ]
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert [item["status"] for item in accepted.json()["results"]] == ["applied", "applied"]
    audits = assignment_client.app.state.environment_repository.audit_events
    assignment_audits = [
        event for event in audits.values() if event.action == "environment.assigned"
    ]
    assert len(assignment_audits) == 1


async def test_gateway_grants_need_acknowledgment_when_crossing_production(
    assignment_client: TestClient,
) -> None:
    gateway = await _seed_gateway(assignment_client, "gateway-grants", environment="development")
    gateways: InMemoryGatewayRepository = assignment_client.app.state.gateway_repository
    entitlements: InMemoryEntitlementRepository = assignment_client.app.state.entitlement_repository
    await gateways.save_model_api(
        ModelApi(
            id="api-1",
            tenant_id=TENANT,
            gateway_id=gateway.id,
            api_name="chat",
            display_name="Chat",
            path="chat",
            imported_from_snapshot_id="snapshot",
        ),
        _audit("modelApi"),
    )
    grant = Entitlement(
        id="grant-1",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="user", id="principal-1"),
        resource=EntitlementResource(kind="modelApi", id="api-1"),
        enabled=True,
    )
    disabled = grant.model_copy(
        update={"id": "grant-disabled", "enabled": False, "resource": grant.resource}
    )
    await entitlements.save_entitlement(grant, _audit("entitlement"))
    await entitlements.save_entitlement(disabled, _audit("entitlement"))

    refused = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": gateway.id,
                    "environment": "production",
                }
            ]
        },
    )

    assert refused.status_code == 409
    details = refused.json()["details"]
    assert details["reason"] == "grantsAcknowledgmentRequired"
    assert details["grantCount"] == 1
    assert details["grants"][0]["entitlementId"] == "grant-1"

    accepted = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "acknowledgeGrants": True,
            "assignments": [
                {
                    "resourceKind": "gateway",
                    "resourceId": gateway.id,
                    "environment": "production",
                }
            ],
        },
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["grantsCarried"] == 1


async def test_grant_acknowledgment_names_products_and_deployments(
    assignment_client: TestClient,
) -> None:
    gateway = await _seed_gateway(assignment_client, "gateway-named", environment="development")
    endpoint = await _seed_endpoint(assignment_client, "endpoint-named", environment="development")
    gateways: InMemoryGatewayRepository = assignment_client.app.state.gateway_repository
    endpoints = assignment_client.app.state.model_endpoint_repository
    entitlements: InMemoryEntitlementRepository = assignment_client.app.state.entitlement_repository
    await gateways.replace_observed(
        TENANT,
        gateway.id,
        [
            ObservedProduct(
                id="obsproduct-1",
                tenant_id=TENANT,
                gateway_id=gateway.id,
                snapshot_id="snapshot-1",
                name="starter",
                display_name="Starter",
            )
        ],
        "snapshot-1",
    )
    await endpoints.replace_observed_for_endpoint(
        TENANT,
        endpoint.id,
        [
            ObservedModelDeployment(
                id="obsdeployment-1",
                tenant_id=TENANT,
                endpoint_id=endpoint.id,
                snapshot_id="snapshot-2",
                deployment_name="gpt-4o",
            )
        ],
        "snapshot-2",
    )
    for grant_id, resource in [
        (
            "grant-product",
            EntitlementResource(kind="product", id="obsproduct-1", scope_id=gateway.id),
        ),
        (
            "grant-deployment",
            EntitlementResource(kind="modelDeployment", id="obsdeployment-1", scope_id=endpoint.id),
        ),
    ]:
        await entitlements.save_entitlement(
            Entitlement(
                id=grant_id,
                tenant_id=TENANT,
                subject=EntitlementSubject(kind="user", id="principal-1"),
                resource=resource,
                enabled=True,
            ),
            _audit("entitlement"),
        )

    refused = assignment_client.post(
        "/api/v1/environment-assignments",
        json={
            "assignments": [
                {"resourceKind": "gateway", "resourceId": gateway.id, "environment": "production"},
                {
                    "resourceKind": "modelEndpoint",
                    "resourceId": endpoint.id,
                    "environment": "production",
                },
            ]
        },
    )

    assert refused.status_code == 409, refused.text
    details = refused.json()["details"]
    assert details["reason"] == "grantsAcknowledgmentRequired"
    names = {grant["entitlementId"]: grant["resourceName"] for grant in details["grants"]}
    assert names == {
        "grant-product": "Starter",
        "grant-deployment": "gpt-4o on Endpoint endpoint-named",
    }
    assert details["principalCount"] == 1


async def test_catalog_edit_judges_applied_publications_under_their_locks(
    assignment_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_gateway(assignment_client, "gateway-dev", environment="development")
    await _seed_endpoint(assignment_client, "endpoint-test", environment="test")
    allowed = assignment_client.patch(
        "/api/v1/environment-catalog/environments/development",
        json={"acceptsEndpointsFrom": ["test"]},
    )
    assert allowed.status_code == 200, allowed.text
    applied = await _seed_applied_publication(
        assignment_client, "gateway-dev", "endpoint-test", "pub-racing"
    )
    gateways: InMemoryGatewayRepository = assignment_client.app.state.gateway_repository
    await gateways.save_publication(
        applied.model_copy(update={"resources": []}), _audit("publication")
    )
    service: EnvironmentService = assignment_client.app.state.environment_service
    original = service.blocked_publications
    calls = 0

    async def apply_finishes_after_first_read(
        tenant_id: str, **kwargs: Any
    ) -> list[BlockedPublication]:
        nonlocal calls
        calls += 1
        result = await original(tenant_id, **kwargs)
        if calls == 1:
            # The draft's apply completes after the edit first reads publications but before it
            # holds any publication lock.
            await gateways.save_publication(applied, _audit("publication"))
        return result

    monkeypatch.setattr(service, "blocked_publications", apply_finishes_after_first_read)

    refused = assignment_client.patch(
        "/api/v1/environment-catalog/environments/development",
        json={"acceptsEndpointsFrom": []},
    )

    assert refused.status_code == 409, refused.text
    details = refused.json()["details"]
    assert details["reason"] == "publicationsBlocked"
    assert [item["publicationId"] for item in details["publications"]] == ["pub-racing"]
    catalog = assignment_client.get("/api/v1/environment-catalog").json()
    development = next(
        item for item in catalog["environments"] if item["key"] == "development"
    )
    assert development["acceptsEndpointsFrom"] == ["test"]


async def test_assignment_locks_publications_created_before_its_scopes_are_held(
    assignment_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_gateway(assignment_client, "gateway-late")
    await _seed_endpoint(assignment_client, "endpoint-late", environment="development")
    gateways: InMemoryGatewayRepository = assignment_client.app.state.gateway_repository
    service: EnvironmentService = assignment_client.app.state.environment_service
    original = service._assignment_scopes
    calls = 0

    async def publication_appears_after_first_scopes(
        tenant_id: str, records: Sequence[AssignmentRecord]
    ) -> AssignmentScopes:
        nonlocal calls
        calls += 1
        scopes = await original(tenant_id, records)
        if calls == 1:
            # A draft is created and starts applying between the batch's first read and its
            # scopes being held.
            draft = await _seed_applied_publication(
                assignment_client, "gateway-late", "endpoint-late", "pub-late"
            )
            await gateways.save_publication(
                draft.model_copy(update={"resources": []}), _audit("publication")
            )
            await gateways.acquire_publication_lock(TENANT, "pub-late", "apply-owner")
        return scopes

    monkeypatch.setattr(service, "_assignment_scopes", publication_appears_after_first_scopes)
    assignment = {
        "assignments": [
            {"resourceKind": "gateway", "resourceId": "gateway-late", "environment": "production"}
        ]
    }

    busy = assignment_client.post("/api/v1/environment-assignments", json=assignment)

    assert busy.status_code == 409, busy.text
    assert busy.json()["message"] == PUBLICATION_BUSY_MESSAGE
    gateway = await gateways.get_gateway(TENANT, "gateway-late")
    assert gateway is not None and gateway.environment is None

    await gateways.release_publication_lock(TENANT, "pub-late", "apply-owner")
    accepted = assignment_client.post("/api/v1/environment-assignments", json=assignment)

    # The publication is still a draft, which may be left blocked; it fails at plan time.
    assert accepted.status_code == 200, accepted.text
    assert [item["status"] for item in accepted.json()["results"]] == ["applied"]
