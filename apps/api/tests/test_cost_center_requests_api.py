from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.cost_center_requests_api import cost_center_requests_router
from mosaic_api.errors import (
    DirectoryForbiddenError,
    DomainError,
    domain_error_handler,
    request_validation_error_handler,
)
from test_cost_center_request_repositories import GROUP, OTHER_USER, TENANT, USER
from test_cost_center_requests import ADMIN, Harness, approve_payload, make_harness


class RequestAuthenticator:
    def __init__(self) -> None:
        self.context: AuthContext | None = AuthContext(
            object_id=USER, tenant_id=TENANT, roles=frozenset({"User"})
        )

    async def authenticate(self, _request: Request) -> AuthContext:
        if self.context is None:
            raise HTTPException(status_code=401, detail="A token is required")
        return self.context

    async def close(self) -> None:
        return None

    def administrator(self) -> None:
        self.context = AuthContext(
            object_id=ADMIN.object_id, tenant_id=TENANT, roles=frozenset({"Admin"})
        )


@dataclass
class Api:
    harness: Harness
    auth: RequestAuthenticator
    client: TestClient


@pytest.fixture
def api() -> Iterator[Api]:
    harness = make_harness()
    app = FastAPI()
    auth = RequestAuthenticator()
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
    app.state.cost_center_request_service = harness.service
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.include_router(cost_center_requests_router)
    with TestClient(app) as client:
        yield Api(harness, auth, client)


def create(api: Api) -> str:
    response = api.client.post(
        "/api/v1/portal/cost-center-requests",
        json={
            "name": "Fictional research",
            "justification": "Evaluate a fictional project",
            "suggestedGroupObjectId": GROUP,
            "idempotencyKey": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["requestKind"] == "costCenter"
    assert body["suggestedGroupObjectId"] == GROUP
    api.harness.actions.request_id = body["id"]
    return str(body["id"])


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "/api/v1/request-counts", None),
        ("GET", "/api/v1/cost-center-requests", None),
        ("GET", "/api/v1/cost-center-requests/unknown", None),
        ("POST", "/api/v1/cost-center-requests/unknown/approve", {}),
        ("POST", "/api/v1/cost-center-requests/unknown/deny", {}),
    ],
)
def test_portal_role_cannot_use_administrator_routes(
    api: Api, method: str, path: str, body: dict[str, Any] | None
) -> None:
    assert api.client.request(method, path, json=body).status_code == 403
    assert api.harness.actions.created_ids == []


@pytest.mark.parametrize(
    "path",
    ["/api/v1/portal/cost-center-requests", "/api/v1/portal/request-counts"],
)
def test_new_portal_routes_require_authentication(api: Api, path: str) -> None:
    api.auth.context = None
    assert api.client.get(path).status_code == 401


def test_portal_cannot_smuggle_group_setup_or_invalid_suggestion_into_submission(api: Api) -> None:
    for extra in (
        {"groupSetup": {"mode": "create"}},
        {"suggestedGroupObjectId": "not-a-directory-object-id"},
        {"justification": "   "},
    ):
        response = api.client.post(
            "/api/v1/portal/cost-center-requests",
            json={"name": "Fictional research", "justification": "Purpose", **extra},
        )
        assert response.status_code == 422
    assert api.harness.actions.created_ids == []


def test_portal_routes_submit_list_get_withdraw_and_count_the_owned_kind(api: Api) -> None:
    request_id = create(api)
    assert create(api) == request_id
    mine = api.client.get("/api/v1/portal/cost-center-requests")
    assert mine.status_code == 200 and len(mine.json()) == 1
    counts = api.client.get("/api/v1/portal/request-counts")
    assert counts.json() == {
        "pendingResourceCount": 0,
        "pendingCostCenterCount": 1,
        "pendingMembershipCount": 0,
        "pendingCount": 1,
    }
    one = api.client.get(f"/api/v1/portal/cost-center-requests/{request_id}")
    assert one.status_code == 200 and "approval" not in one.json()
    withdrawn = api.client.post(f"/api/v1/portal/cost-center-requests/{request_id}/withdraw")
    assert withdrawn.status_code == 200 and withdrawn.json()["state"] == "withdrawn"
    assert api.client.get("/api/v1/portal/request-counts").json()["pendingCount"] == 0


def test_portal_read_and_withdraw_do_not_disclose_another_requester_or_tenant(api: Api) -> None:
    request_id = create(api)
    for object_id, tenant_id in ((OTHER_USER, TENANT), (USER, "other-tenant")):
        api.auth.context = AuthContext(
            object_id=object_id, tenant_id=tenant_id, roles=frozenset({"User"})
        )
        path = f"/api/v1/portal/cost-center-requests/{request_id}"
        assert api.client.get(path).status_code == 404
        assert api.client.post(f"{path}/withdraw").status_code == 404
        assert api.client.get("/api/v1/portal/cost-center-requests").json() == []


def test_admin_routes_list_filter_get_approve_and_report_partial_setup(api: Api) -> None:
    request_id = create(api)
    api.auth.administrator()
    assert api.client.get("/api/v1/request-counts").json()["pendingCostCenterCount"] == 1
    pending = api.client.get("/api/v1/cost-center-requests?state=pending")
    assert pending.status_code == 200 and pending.json()[0]["id"] == request_id
    api.harness.actions.group_error = DirectoryForbiddenError("Group.Create consent is missing")
    approved = api.client.post(
        f"/api/v1/cost-center-requests/{request_id}/approve",
        json=approve_payload(add_requester=True).model_dump(mode="json", by_alias=True),
    )
    assert approved.status_code == 200
    body = approved.json()
    assert body["state"] == "approved" and body["costCenter"]
    assert body["setupState"] == "setupRequired"
    assert body["needsAdministratorAttention"]
    assert body["requesterMembershipState"] == "pending"
    assert body["preparationError"] == "Group.Create consent is missing"
    assert api.client.get("/api/v1/cost-center-requests?state=pending").json() == []
    assert api.client.get(f"/api/v1/cost-center-requests/{request_id}").status_code == 200
    api.auth.context = AuthContext(object_id=USER, tenant_id=TENANT, roles=frozenset({"User"}))
    portal = api.client.get(f"/api/v1/portal/cost-center-requests/{request_id}").json()
    assert portal["needsAdministratorAttention"]
    assert "approval" not in portal and "preparationError" not in portal
    assert "Group.Create" not in str(portal)


def test_existing_group_approval_requires_explicit_acknowledgment(api: Api) -> None:
    request_id = create(api)
    api.auth.administrator()
    response = api.client.post(
        f"/api/v1/cost-center-requests/{request_id}/approve",
        json={
            "costCenter": {
                "name": "Fictional research",
                "code": "RESEARCH",
                "groupSetup": {"mode": "attach", "groupObjectId": GROUP},
            }
        },
    )
    assert response.status_code == 422
    assert api.harness.actions.created_ids == []


def test_denial_routes_keep_kinds_separate_and_reject_invalid_state_filter(api: Api) -> None:
    request_id = create(api)
    api.auth.administrator()
    denied = api.client.post(
        f"/api/v1/cost-center-requests/{request_id}/deny", json={"note": "Use an existing center"}
    )
    assert denied.status_code == 200 and denied.json()["state"] == "denied"
    assert denied.json()["decisionNote"] == "Use an existing center"
    assert api.client.get("/api/v1/cost-center-requests?state=denied").json()[0]["id"] == request_id
    assert api.client.get("/api/v1/cost-center-requests?state=invalid").status_code == 422
    assert api.client.get("/api/v1/request-counts").json()["pendingCount"] == 0
