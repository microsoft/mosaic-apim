import asyncio
from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import pytest
from azure.cosmos.aio import CosmosClient
from cost_center_management_double import ADMIN, TENANT, USER, management
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.api import router
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.cost_center_delegation import ReviewDecision
from mosaic_api.cost_center_delegation_api import delegation_router
from mosaic_api.cost_center_membership_requests import MembershipRequestCreate
from mosaic_api.cost_center_membership_requests_api import membership_requests_router
from mosaic_api.cost_center_requests import (
    CostCenterRequestApproval,
    CostCenterRequestApprovalIntent,
    CostCenterRequestCreate,
    ProposedPocs,
    requested_cost_center_id,
)
from mosaic_api.cost_center_requests_api import cost_center_requests_router
from mosaic_api.cost_centers import CostCenterCreate, CostCenterPoc
from mosaic_api.domain import AccessRequestApproval, AccessRequestCreate, DirectoryObject
from mosaic_api.errors import ConflictError, DomainError, NotFoundError, domain_error_handler
from mosaic_api.repositories import InMemoryCostCenterRepository, InMemoryModelEndpointRepository
from mosaic_api.repositories.cosmos_entitlements import CosmosEntitlementRepository
from mosaic_api.services.cost_center_delegation import CostCenterDelegationService
from mosaic_api.services.cost_center_membership_requests import CostCenterMembershipService
from mosaic_api.services.cost_center_requests import CostCenterRequestService
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from test_cost_center_membership_requests import published
from test_cost_center_requests_api import RequestAuthenticator
from test_entra_cost_center_runtime import CALLER, CENTER, Estate, audit, estate
from test_governed_lifecycle import ACTOR
from test_model_access_repositories import Cosmos

__all__ = ["estate"]
REVIEWER = "90000000-0000-4000-8000-000000000009"
OTHER_TENANT = "90000000-0000-4000-8000-000000000099"
TRACKING_KEY = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


@pytest.fixture
def api() -> Iterator[Any]:
    h = management()
    assert isinstance(h.repository, InMemoryCostCenterRepository)
    h.entitlements._cost_centers = h.repository.cost_centers
    member = CostCenterMembershipService(h.entitlements, h.repository, h.service, h.lookup)
    creations = CostCenterRequestService(
        h.entitlements,
        h.repository,
        h.service,
        validate_access=h.checks.validate_cost_center_access,
    )
    grants = EntitlementService(
        h.entitlements,
        directory_repository=h.local_directory,
        gateway_repository=h.gateways,
        endpoint_repository=InMemoryModelEndpointRepository(),
        cost_center_repository=h.repository,
        directory_lookup=h.lookup,
    )
    asyncio.run(published((h, h.entitlements, member)))
    h.lookup._objects[REVIEWER] = DirectoryObject(
        object_id=REVIEWER,
        kind="user",
        display_name="Fictional Reviewer",
    )
    auth = RequestAuthenticator()
    app = FastAPI()
    app.state.settings = Settings(
        _env_file=None, environment=Environment.TEST, auth_mode=AuthMode.LOCAL,
        tenant_id=TENANT, repository_backend=RepositoryBackend.MEMORY,
        required_role="Admin", portal_role="User",
    )
    app.state.authenticator = auth
    app.state.cost_center_service = h.service
    app.state.cost_center_request_service = creations
    app.state.cost_center_membership_request_service = member
    app.state.cost_center_delegation_service = CostCenterDelegationService(
        h.repository,
        h.entitlements,
        grants,
        member,
    )
    app.state.entitlement_service = grants
    app.add_exception_handler(DomainError, domain_error_handler)
    for api_router in [
        router,
        delegation_router,
        membership_requests_router,
        cost_center_requests_router,
    ]:
        app.include_router(api_router)
    with TestClient(app) as client:
        yield h, member, creations, auth, client


def sign_in(auth: RequestAuthenticator, object_id: str, *roles: str, tenant: str = TENANT) -> None:
    auth.context = AuthContext(
        object_id=object_id,
        tenant_id=tenant,
        roles=frozenset(roles),
        name="Fictional Person",
    )


def assign(h: Any, auth: RequestAuthenticator, client: TestClient, *roles: str) -> None:
    sign_in(auth, ADMIN.object_id, "Admin")
    center = asyncio.run(h.repository.get_cost_center(TENANT, "center"))
    response = client.put(
        "/api/v1/cost-centers/center/pocs",
        json={
            "revision": center.etag,
            "reason": "Set reviewers",
            "assignments": [{"role": role, "objectId": REVIEWER} for role in roles],
        },
    )
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("role", ["technical", "billing"])
def test_membership_review_shares_admin_outcome_and_revocation(api: Any, role: str) -> None:
    h, membership, _, auth, client = api
    record = asyncio.run(
        membership.submit(
            Actor(USER, TENANT, name="Fictional Requester"),
            MembershipRequestCreate(
                cost_center_id="center", justification="Join team", idempotency_key=TRACKING_KEY
            ),
        )
    )
    assign(h, auth, client, role)
    path = f"/api/v1/portal/approvals/costCenterMembership/{record.id}"
    sign_in(auth, REVIEWER, "User")
    assert client.get("/api/v1/cost-center-membership-requests").status_code == 403
    assert (
        client.put(
            "/api/v1/cost-centers/center/pocs",
            json={
                "revision": "not-authorized",
                "reason": "Not allowed",
                "assignments": [],
            },
        ).status_code
        == 403
    )
    assert client.get("/api/v1/portal/approvals?centerId=another").status_code == 404
    assert (
        client.get("/api/v1/portal/approvals?centerId=center").json()["items"][0]["id"] == record.id
    )
    before = client.get(path).json()
    assert before["scope"]["roles"] == [role]
    assert before["requester"] == "Fictional Requester" and "groupObjectId" not in before
    payload = {
        "revision": before["revision"],
        "reason": "Reviewed",
        "acknowledgeMembershipChanges": True,
    }
    assert client.post(f"{path}/approve", json=payload).json()["membershipState"] == "succeeded"
    assert client.post(f"{path}/approve", json=payload).status_code == 200
    assert client.post(f"{path}/deny", json=payload).status_code == 409
    assert len(h.graph.writes) == 1
    sign_in(auth, ADMIN.object_id, "Admin")
    result = client.get(f"/api/v1/cost-center-membership-requests/{record.id}").json()
    assert result["state"] == "approved" and result["decisionNote"] == "Reviewed"
    sign_in(auth, REVIEWER, "User", tenant=OTHER_TENANT)
    assert client.get(path).status_code == 404
    assign(h, auth, client)
    sign_in(auth, REVIEWER, "User")
    assert client.get(path).status_code == 404
    assert client.post(f"{path}/approve", json=payload).status_code == 404


def test_own_request_and_owner_email_do_not_authorize_review(api: Any) -> None:
    h, membership, _, auth, client = api
    record = asyncio.run(
        membership.submit(
            Actor(REVIEWER, TENANT),
            MembershipRequestCreate(
                cost_center_id="center", justification="Join team", idempotency_key=TRACKING_KEY
            ),
        )
    )
    path = f"/api/v1/portal/approvals/costCenterMembership/{record.id}"
    sign_in(auth, REVIEWER, "User")
    assert client.get("/api/v1/portal/approvals/centers").json() == []
    assign(h, auth, client, "technical", "billing")
    sign_in(auth, REVIEWER, "User")
    assert client.get("/api/v1/portal/approvals?centerId=center").json()["items"] == []
    assert client.get(path).status_code == 404
    assert (
        client.post(f"{path}/deny", json={"revision": "unknown", "reason": "Own"}).status_code
        == 404
    )
    auth.context = None
    assert client.get(path).status_code == 401


def test_creation_contacts_activate_only_after_admin_approval(api: Any) -> None:
    h, _, service, auth, client = api
    record = asyncio.run(
        service.submit(
            Actor(USER, TENANT),
            CostCenterRequestCreate(
                name="Pending",
                justification="A reason",
                proposed_pocs=ProposedPocs(
                    technical="fictional@example.invalid",
                ),
            ),
        )
    )
    center_id = requested_cost_center_id(TENANT, record.id)
    selected = CostCenterCreate.model_validate(
        {
            "name": "Pending",
            "code": "PENDING",
            "owners": ["fictional@example.invalid"],
            "pocSelections": [{"role": "technical", "objectId": REVIEWER}],
        }
    )
    approval = CostCenterRequestApproval(cost_center=selected)
    pending = asyncio.run(h.entitlements.get_cost_center_request(TENANT, record.id))
    pending.approval = CostCenterRequestApprovalIntent(
        cost_center=selected,
        cost_center_id=center_id,
        decided_by_object_id=ADMIN.object_id,
    )
    asyncio.run(
        h.entitlements.save_cost_center_request(
            pending, service._audit(ADMIN, pending, "preparationStarted"), []
        )
    )
    asyncio.run(
        h.service.create(
            ADMIN, selected, cost_center_id=center_id, poc_creation_request_id=record.id
        )
    )
    sign_in(auth, REVIEWER, "User")
    assert client.get("/api/v1/portal/approvals/centers").json() == []
    assert (
        client.post(
            f"/api/v1/cost-center-requests/{record.id}/approve",
            json=approval.model_dump(mode="json"),
        ).status_code
        == 403
    )
    asyncio.run(service.approve(ADMIN, record.id, approval))
    assert client.get("/api/v1/portal/approvals/centers").json()[0]["center"]["id"] == center_id


async def test_resource_approval_without_person_limits_and_revocation_fence(
    estate: Estate,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resource = estate.resources[0]
    center = await estate.center(
        mode="requestOnly",
        pocs=[
            CostCenterPoc(
                role="billing",
                object_id=REVIEWER,
                display_name="Fictional Reviewer",
                approved_by=ACTOR.object_id,
            )
        ],
    )
    assert isinstance(estate.cost_center_records, InMemoryCostCenterRepository)
    estate.entitlements._cost_centers = estate.cost_center_records.cost_centers
    service = CostCenterDelegationService(
        estate.cost_center_records,
        estate.entitlements,
        estate.grants,
        None,  # type: ignore[arg-type]  # Resource decisions do not use the membership port.
    )
    reviewer = Actor(REVIEWER, CALLER.tenant_id)
    record = await estate.grants.create_catalog_access_request(
        CALLER,
        AccessRequestCreate(cost_center_id=CENTER, resource=resource),
    )
    detail = await service.detail(reviewer, "resourceAccess", record.id)
    assert detail.has_person_limits is False and not detail.approval_blocked
    payload = ReviewDecision(revision=detail.revision, reason="Review completed")
    original = estate.entitlements.approve_access_request

    async def revoke_before_commit(*args: Any, **kwargs: Any) -> Any:
        current = await estate.cost_center_records.get_cost_center(center.tenant_id, center.id)
        assert current
        await estate.cost_center_records.save_cost_center(
            current.model_copy(update={"pocs": []}), audit()
        )
        return await original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(estate.entitlements, "approve_access_request", revoke_before_commit)
        with pytest.raises(ConflictError):
            await service.decide(reviewer, "resourceAccess", record.id, "approve", payload)
    assert (await estate.grants.get_access_request(ACTOR, record.id)).state == "pending"
    assert not await estate.entitlements.list_entitlements(CALLER.tenant_id)
    approved = await estate.grants.approve_access_request(ACTOR, record.id, AccessRequestApproval())
    assert approved.granted_entitlement_id


async def test_resource_approval_without_limits_uses_one_shared_grant(estate: Estate) -> None:
    center = await estate.center(
        mode="requestOnly",
        pocs=[CostCenterPoc(
            role="technical", object_id=REVIEWER, display_name="Fictional Reviewer",
            approved_by=ACTOR.object_id,
        )],
    )
    assert center.id == CENTER
    assert isinstance(estate.cost_center_records, InMemoryCostCenterRepository)
    estate.entitlements._cost_centers = estate.cost_center_records.cost_centers
    service = CostCenterDelegationService(
        estate.cost_center_records,
        estate.entitlements,
        estate.grants,
        None,  # type: ignore[arg-type]  # Resource decisions do not use the membership port.
    )
    record = await estate.grants.create_catalog_access_request(
        CALLER, AccessRequestCreate(cost_center_id=CENTER, resource=estate.resources[2]),
    )
    actor = Actor(REVIEWER, CALLER.tenant_id)
    view = await service.detail(actor, "resourceAccess", record.id)
    payload = ReviewDecision(revision=view.revision, reason="Scoped pool access")
    saved = await service.decide(actor, "resourceAccess", record.id, "approve", payload)
    assert saved.state == "approved" and saved.decided_by_you
    assert (await service.decide(actor, "resourceAccess", record.id, "approve", payload)) == saved
    admin = await estate.grants.get_access_request(ACTOR, record.id)
    assert admin.state == saved.state and admin.decision_note == payload.reason
    grants = await estate.entitlements.list_entitlements(CALLER.tenant_id)
    assert len(grants) == 1 and grants[0].resource == record.resource
    assert grants[0].cost_center_id == CENTER
    assert grants[0].enforcement is None
    assert grants[0].runtime is None and grants[0].binding is None


@pytest.mark.parametrize(
    ("kind", "entity_type", "scope"),
    [
        ("resourceAccess", "accessRequest", {"costCenterId": "foreign-center"}),
        (
            "costCenterMembership", "costCenterMembershipRequest",
            {"costCenter": {"id": "foreign-center"}},
        ),
    ],
)
async def test_malformed_foreign_cosmos_request_is_hidden_before_validation(
    estate: Estate, kind: Any, entity_type: str, scope: dict[str, Any],
) -> None:
    await estate.center(pocs=[CostCenterPoc(
        role="technical", object_id=REVIEWER, display_name="Fictional Reviewer",
        approved_by=ACTOR.object_id,
    )])
    cosmos = Cosmos()
    requests = CosmosEntitlementRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")
    await cosmos.containers["desired"].create_item({
        "id": "foreign-request", "tenantId": CALLER.tenant_id, "entityType": entity_type,
        "requesterObjectId": CALLER.object_id, "state": "not-a-valid-state", **scope,
    })
    service = CostCenterDelegationService(
        estate.cost_center_records, requests, estate.grants,
        None,  # type: ignore[arg-type]  # The scope check precedes request execution.
    )
    with pytest.raises(NotFoundError, match="Approval not found"):
        await service.detail(Actor(REVIEWER, CALLER.tenant_id), kind, "foreign-request")
