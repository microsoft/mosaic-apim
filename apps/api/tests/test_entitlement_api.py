"""Entitlements as the source of truth for who may use what.

These exercise the contract the end-user portal will depend on: a grant reaches a person directly
or through a group, catalog metadata an administrator authored survives a re-import, and a grant
naming something MOSAIC does not govern is refused rather than stored.
"""

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AuditEvent,
    CatalogVisibility,
    McpServer,
    ModelApi,
    new_id,
)
from mosaic_api.main import create_app
from mosaic_api.observed import ObservedApimUser, ObservedSubscription
from mosaic_api.repositories import InMemoryGatewayRepository

TENANT = "tenant-test"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    app: FastAPI = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="test",
        resource_id="seed",
        actor_object_id="local-admin",
    )


async def _seed_model_api(
    repository: InMemoryGatewayRepository, record_id: str = "modelApi_seed"
) -> ModelApi:
    record = ModelApi(
        id=record_id,
        tenant_id=TENANT,
        gateway_id="gateway_seed",
        api_name="chat",
        display_name="Chat completions",
        path="chat",
        product_names=["premium"],
        imported_from_snapshot_id="snapshot-1",
    )
    return await repository.save_model_api(record, _audit())


async def _seed_mcp_server(repository: InMemoryGatewayRepository) -> McpServer:
    record = McpServer(
        id="mcpServer_seed",
        tenant_id=TENANT,
        gateway_id="gateway_seed",
        api_name="tickets",
        display_name="Ticketing tools",
        path="tickets",
        imported_from_snapshot_id="snapshot-1",
    )
    return await repository.save_mcp_server(record, _audit())


def _principal(client: TestClient, object_id: str, kind: str = "user") -> dict[str, Any]:
    response = client.post(
        "/api/v1/principals", json={"objectId": object_id, "kind": kind, "label": object_id}
    )
    assert response.status_code == 201, response.text
    return response.json()


def _group(client: TestClient, name: str) -> dict[str, Any]:
    response = client.post("/api/v1/groups", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


async def test_entitlement_round_trip_and_deterministic_identity(app_client: TestClient) -> None:
    await _seed_model_api(app_client.app.state.gateway_repository)
    principal = _principal(app_client, "user-object-1")

    payload = {
        "subject": {"kind": "user", "id": principal["id"]},
        "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        "enforcement": {
            "tokens": {
                "counterKeyExpression": "@(context.Subscription?.Key)",
                "tokensPerMinute": 10000,
                "tokenQuota": 5000000,
                "tokenQuotaPeriod": "Monthly",
            },
            "requests": {
                "counterKeyExpression": "@(context.Subscription?.Key)",
                "calls": 60,
                "renewalPeriodSeconds": 60,
            },
        },
    }
    created = app_client.post("/api/v1/entitlements", json=payload)
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["subject"]["kind"] == "user"
    assert body["resource"]["kind"] == "modelApi"
    assert body["enforcement"]["tokens"]["tokensPerMinute"] == 10000
    assert body["enforcement"]["requests"]["calls"] == 60

    # Deterministic on subject and resource, so re-granting is a conflict rather than a duplicate.
    duplicate = app_client.post("/api/v1/entitlements", json=payload)
    assert duplicate.status_code == 409, duplicate.text

    listed = app_client.get("/api/v1/entitlements").json()
    assert [item["id"] for item in listed] == [body["id"]]

    patched = app_client.patch(f"/api/v1/entitlements/{body['id']}", json={"enabled": False})
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    assert app_client.delete(f"/api/v1/entitlements/{body['id']}").status_code == 204
    assert app_client.get("/api/v1/entitlements").json() == []


async def test_unrestricted_entitlement_is_allowed_but_empty_enforcement_is_not(
    app_client: TestClient,
) -> None:
    await _seed_mcp_server(app_client.app.state.gateway_repository)
    principal = _principal(app_client, "user-object-2")

    unrestricted = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "mcpServer", "id": "mcpServer_seed"},
        },
    )
    assert unrestricted.status_code == 201, unrestricted.text
    assert unrestricted.json()["enforcement"] is None

    meaningless = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
            "enforcement": {},
        },
    )
    assert meaningless.status_code == 422, meaningless.text


async def test_grant_to_ungoverned_resource_is_refused(app_client: TestClient) -> None:
    principal = _principal(app_client, "user-object-3")
    response = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_does_not_exist"},
        },
    )
    assert response.status_code == 422
    assert "does not govern" in response.json()["message"]


async def test_subject_kind_must_match_the_principal(app_client: TestClient) -> None:
    await _seed_model_api(app_client.app.state.gateway_repository)
    service_principal = _principal(app_client, "app-object-1", kind="servicePrincipal")
    mismatched = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": service_principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        },
    )
    assert mismatched.status_code == 422
    assert "servicePrincipal" in mismatched.text

    matched = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "application", "id": service_principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        },
    )
    assert matched.status_code == 201, matched.text


async def test_resolution_reports_direct_and_group_grants(app_client: TestClient) -> None:
    repository = app_client.app.state.gateway_repository
    await _seed_model_api(repository)
    await _seed_mcp_server(repository)
    principal = _principal(app_client, "user-object-4")
    group = _group(app_client, "Platform engineering")
    assert (
        app_client.put(f"/api/v1/groups/{group['id']}/members/{principal['id']}").status_code
        == 201
    )

    direct = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        },
    ).json()
    via_group = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "group", "id": group["id"]},
            "resource": {"kind": "mcpServer", "id": "mcpServer_seed"},
        },
    ).json()

    resolved = app_client.get(
        "/api/v1/entitlements/resolve", params={"principalId": principal["id"]}
    )
    assert resolved.status_code == 200, resolved.text
    by_id = {item["entitlement"]["id"]: item for item in resolved.json()}
    assert by_id[direct["id"]]["via"] == "direct"
    assert by_id[via_group["id"]]["via"] == "group"
    assert by_id[via_group["id"]]["viaGroupName"] == "Platform engineering"


async def test_resolution_omits_disabled_grants(app_client: TestClient) -> None:
    await _seed_model_api(app_client.app.state.gateway_repository)
    principal = _principal(app_client, "user-object-5")
    created = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        },
    ).json()
    app_client.patch(f"/api/v1/entitlements/{created['id']}", json={"enabled": False})

    resolved = app_client.get(
        "/api/v1/entitlements/resolve", params={"principalId": principal["id"]}
    )
    assert resolved.json() == []


async def test_catalog_visibility_is_administrator_authored(app_client: TestClient) -> None:
    repository = app_client.app.state.gateway_repository
    seeded = await _seed_model_api(repository)
    assert seeded.visibility == CatalogVisibility.CATALOG

    response = app_client.patch(
        f"/api/v1/model-apis/{seeded.id}/catalog",
        json={"visibility": "private", "summary": "Internal only"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["visibility"] == "private"
    assert response.json()["summary"] == "Internal only"


async def test_access_request_lifecycle(app_client: TestClient) -> None:
    await _seed_model_api(app_client.app.state.gateway_repository)
    service = app_client.app.state.entitlement_service
    from mosaic_api.domain import AccessRequestCreate, EntitlementResource
    from mosaic_api.services.directory import Actor

    actor = Actor(object_id="user-object-6", tenant_id=TENANT)
    request = AccessRequestCreate(
        resource=EntitlementResource(kind="modelApi", id="modelApi_seed"),
        justification="Need chat completions for the support bot",
    )
    created = await service.create_access_request(actor, request)
    assert created.state == "pending"

    with pytest.raises(Exception, match="already have an open request"):
        await service.create_access_request(actor, request)

    approved = app_client.post(
        f"/api/v1/access-requests/{created.id}/approve", json={"note": "Approved for Q3"}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["state"] == "approved"
    assert approved.json()["decisionNote"] == "Approved for Q3"

    # A decision is final; deciding twice is a conflict rather than a silent overwrite.
    again = app_client.post(f"/api/v1/access-requests/{created.id}/deny", json={})
    assert again.status_code == 409, again.text

    listed = app_client.get("/api/v1/access-requests", params={"state": "approved"})
    assert [item["id"] for item in listed.json()] == [created.id]


async def test_observed_resources_require_a_scope(app_client: TestClient) -> None:
    principal = _principal(app_client, "user-object-7")
    response = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "product", "id": "observedProduct_x"},
        },
    )
    assert response.status_code == 422
    assert "scopeId" in response.text


async def test_a_direct_grant_wins_over_a_group_grant_for_the_same_resource(
    app_client: TestClient,
) -> None:
    """Otherwise a loose group limit could silently supersede a deliberately tight direct one."""

    await _seed_model_api(app_client.app.state.gateway_repository)
    principal = _principal(app_client, "user-object-8")
    group = _group(app_client, "Everyone")
    app_client.put(f"/api/v1/groups/{group['id']}/members/{principal['id']}")

    tight = {
        "tokens": {
            "counterKeyExpression": "@(context.Subscription?.Key)",
            "tokensPerMinute": 100,
        }
    }
    loose = {
        "tokens": {
            "counterKeyExpression": "@(context.Subscription?.Key)",
            "tokensPerMinute": 999999,
        }
    }
    app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "group", "id": group["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
            "enforcement": loose,
        },
    )
    app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
            "enforcement": tight,
        },
    )

    resolved = app_client.get(
        "/api/v1/entitlements/resolve", params={"principalId": principal["id"]}
    ).json()

    assert len(resolved) == 1, resolved
    assert resolved[0]["via"] == "direct"
    assert resolved[0]["entitlement"]["enforcement"]["tokens"]["tokensPerMinute"] == 100


async def test_binding_is_inferred_from_the_subscription_the_principal_owns(
    app_client: TestClient,
) -> None:
    """APIM reports ownerId as a resource path, so matching must reduce it to the user name."""

    repository = app_client.app.state.gateway_repository
    await _seed_model_api(repository)
    principal = _principal(app_client, "entra-object-ada")

    snapshot = "snapshot-1"
    await repository.replace_observed(
        TENANT,
        "gateway_seed",
        [
            ObservedApimUser(
                id="observedApimUser_ada",
                tenant_id=TENANT,
                gateway_id="gateway_seed",
                snapshot_id=snapshot,
                name="user-ada",
                entra_object_id="entra-object-ada",
            ),
            ObservedSubscription(
                id="observedSubscription_ada",
                tenant_id=TENANT,
                gateway_id="gateway_seed",
                snapshot_id=snapshot,
                name="sub-ada",
                scope="/products/premium",
                scope_kind="product",
                scope_name="premium",
                owner_id=(
                    "/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement"
                    "/service/apim/users/user-ada"
                ),
                owner_label="user-ada",
            ),
        ],
        snapshot,
    )

    created = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        },
    )
    assert created.status_code == 201, created.text
    binding = created.json()["binding"]
    assert binding is not None, "expected the owning subscription to be inferred"
    assert binding["apimSubscriptionName"] == "sub-ada"
    assert binding["apimProductName"] == "premium"
    assert binding["source"] == "inferred"


async def test_an_explicit_null_leaves_a_non_nullable_field_alone(
    app_client: TestClient,
) -> None:
    """A generated client that serialises unset optionals as null must not get a 500."""

    seeded = await _seed_model_api(app_client.app.state.gateway_repository)
    principal = _principal(app_client, "user-object-9")
    created = app_client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": "modelApi_seed"},
        },
    ).json()

    patched = app_client.patch(
        f"/api/v1/entitlements/{created['id']}", json={"enabled": None, "notes": "still on"}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["enabled"] is True
    assert patched.json()["notes"] == "still on"

    catalog = app_client.patch(
        f"/api/v1/model-apis/{seeded.id}/catalog",
        json={"visibility": None, "summary": "Described"},
    )
    assert catalog.status_code == 200, catalog.text
    assert catalog.json()["visibility"] == "catalog"
    assert catalog.json()["summary"] == "Described"
