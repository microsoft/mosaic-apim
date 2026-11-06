import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from cost_center_management_double import ADMIN, TENANT, USER, WORKLOAD, management
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.cost_center_membership_requests_api import membership_requests_router
from mosaic_api.cost_center_requests_api import cost_center_requests_router
from mosaic_api.cost_centers import CostCenterSettingsUpdate, CostCenterUpdate
from mosaic_api.errors import DomainError, domain_error_handler, request_validation_error_handler
from mosaic_api.services.cost_center_membership_requests import CostCenterMembershipService
from mosaic_api.services.cost_center_requests import CostCenterRequestService
from test_cost_center_membership_requests import published
from test_cost_center_requests_api import RequestAuthenticator


@pytest.fixture
def api() -> Iterator[Any]:
    h = management()
    service = CostCenterMembershipService(h.entitlements, h.repository, h.service, h.lookup)
    asyncio.run(published((h, h.entitlements, service)))
    auth = RequestAuthenticator()
    auth.context = AuthContext(object_id=USER, tenant_id=TENANT, roles=frozenset({"User"}))
    app = FastAPI()
    app.state.settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        tenant_id=TENANT,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        required_role="Admin",
        portal_role="User",
    )
    app.state.authenticator = auth
    app.state.cost_center_membership_request_service = service
    app.state.cost_center_request_service = CostCenterRequestService(
        h.entitlements,
        h.repository,
        h.service,
        validate_access=h.checks.validate_cost_center_access,
    )
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(membership_requests_router)
    app.include_router(cost_center_requests_router)
    with TestClient(app) as client:
        yield h, auth, client


def submit(client: TestClient) -> Any:
    return client.post(
        "/api/v1/portal/cost-center-membership-requests",
        json={
            "costCenterId": "center",
            "justification": "Fictional work",
            "idempotencyKey": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/cost-center-membership-requests",
        "/api/v1/cost-center-membership-requests/unknown",
        "/api/v1/request-counts",
    ],
)
def test_admin_routes_reject_portal_role(api: Any, path: str) -> None:
    h, _, client = api
    assert client.get(path).status_code == 403
    assert h.graph.writes == []


@pytest.mark.parametrize("action", ["approve", "deny", "resume"])
def test_admin_decisions_reject_portal_role(api: Any, action: str) -> None:
    h, _, client = api
    response = client.post(
        f"/api/v1/cost-center-membership-requests/unknown/{action}",
        json={"acknowledgeMembershipChanges": True},
    )
    assert response.status_code == 403 and h.graph.writes == []


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/portal/my-cost-centers",
        "/api/v1/portal/cost-center-directory",
        "/api/v1/portal/cost-center-self-service-settings",
        "/api/v1/portal/cost-center-membership-requests",
    ],
)
def test_portal_routes_require_authentication(api: Any, path: str) -> None:
    _, auth, client = api
    auth.context = None
    assert client.get(path).status_code == 401


def test_portal_end_to_end_contract_and_admin_approval(api: Any) -> None:
    h, auth, client = api
    policy = client.get("/api/v1/portal/cost-center-self-service-settings")
    assert policy.json() == {"creationRequestsEnabled": True, "membershipRequestsEnabled": True}
    directory = client.get("/api/v1/portal/cost-center-directory?q=fictional&limit=1")
    assert directory.status_code == 200
    assert directory.json()["items"] == [
        {
            "id": "center",
            "name": "center",
            "code": "center",
            "publicPurpose": "Fictional research",
            "builtIn": False,
            "membershipState": "notMember",
            "tokenEligible": False,
        }
    ]
    response = submit(client)
    assert response.status_code == 201
    request = response.json()
    assert request["requestKind"] == "costCenterMembership" and request["state"] == "pending"
    assert request["costCenter"] == {"id": "center", "name": "center", "code": "center"}
    assert "membershipError" not in request and "requesterObjectId" not in request
    assert submit(client).json()["id"] == request["id"]
    assert client.get("/api/v1/portal/cost-center-membership-requests").json() == [request]
    counts = client.get("/api/v1/portal/request-counts").json()
    assert counts == {
        "pendingResourceCount": 0,
        "pendingCostCenterCount": 0,
        "pendingMembershipCount": 1,
        "pendingCount": 1,
    }
    auth.context = AuthContext(
        object_id=ADMIN.object_id, tenant_id=TENANT, roles=frozenset({"Admin"})
    )
    prefix = "/api/v1/cost-center-membership-requests/" + request["id"]
    assert client.get(prefix).json()["requesterObjectId"] == USER
    assert client.post(prefix + "/approve", json={}).status_code == 422
    approved = client.post(prefix + "/approve", json={"acknowledgeMembershipChanges": True})
    assert approved.status_code == 200
    assert approved.json()["state"] == "approved"
    assert approved.json()["membershipState"] == "succeeded" and approved.json()["operationId"]
    assert not approved.json()["needsAdministratorAttention"]
    assert client.get("/api/v1/request-counts").json()["pendingCount"] == 0
    auth.context = AuthContext(object_id=USER, tenant_id=TENANT, roles=frozenset({"User"}))
    detail = client.get("/api/v1/portal/cost-center-directory/center").json()
    assert detail["membershipState"] == "member" and not detail["tokenEligible"]
    asyncio.run(h.service.update_cost_center(ADMIN, "center", CostCenterUpdate(discoverable=False)))
    assert client.get("/api/v1/portal/cost-center-directory").json()["items"] == []
    assert (
        client.get("/api/v1/portal/cost-center-directory/center").json()["membershipState"]
        == "member"
    )
    assert len(h.graph.writes) == 1


def test_deep_link_privacy_withdraw_and_payload_roles(api: Any) -> None:
    h, auth, client = api
    request = submit(client).json()
    url = "/api/v1/portal/cost-center-membership-requests/" + request["id"]
    for object_id, tenant_id in [(WORKLOAD, TENANT), (USER, "another-tenant")]:
        auth.context = AuthContext(
            object_id=object_id, tenant_id=tenant_id, roles=frozenset({"User"})
        )
        assert client.get(url).status_code == 404
        assert client.post(url + "/withdraw").status_code == 404
        assert client.get("/api/v1/portal/cost-center-membership-requests").json() == []
    auth.context = AuthContext(object_id=USER, tenant_id=TENANT, roles=frozenset({"User"}))
    assert client.post(url + "/withdraw").json()["state"] == "withdrawn"
    invalid = client.post(
        "/api/v1/portal/cost-center-membership-requests",
        json={
            "costCenterId": "center",
            "justification": "work",
            "idempotencyKey": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "requesterObjectId": WORKLOAD,
        },
    )
    assert invalid.status_code == 422 and h.graph.writes == []
    asyncio.run(
        h.service.update_settings(ADMIN, CostCenterSettingsUpdate(creation_requests_enabled=False))
    )
    assert (
        client.post(
            "/api/v1/portal/cost-center-requests",
            json={"name": "Example", "justification": "work"},
        ).status_code
        == 409
    )
