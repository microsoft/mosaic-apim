"""Approving an access request creates the requester's grant intent and links it.

The grant is desired state only; nothing here changes API Management. These tests pin the
contract the admin console relies on. Approval is Admin-only and idempotent. It registers a
requester MOSAIC has never seen, refuses to shadow a grant the requester already holds, and
never leaves an approved request without its grant or a grant whose request is still pending.
"""

from collections.abc import Iterator, Sequence
from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestState,
    AuditEvent,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    ModelAccessSettings,
    ModelApi,
    ModelProvider,
    Publication,
    PublicationStatus,
    TokenEnforcement,
    deterministic_id,
    entitlement_id,
    general_cost_center_id,
    new_id,
)
from mosaic_api.errors import ConflictError
from mosaic_api.main import create_app
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService

TENANT = "tenant-test"
REQUESTER = "0f5c9a3e-1111-4c2b-9d7e-000000000001"
ADMIN = Actor(object_id="local-admin", tenant_id=TENANT)
RESOURCE = EntitlementResource(kind="modelApi", id="modelApi_seed")
LIMITS = {
    "tokens": {
        "counterKeyExpression": "@(context.Subscription.Id)",
        "tokensPerMinute": 12000,
        "estimatePromptTokens": True,
    }
}


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
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


def _model_api(publication_id: str | None = None) -> ModelApi:
    return ModelApi(
        id=RESOURCE.id,
        tenant_id=TENANT,
        gateway_id="gateway_seed",
        api_name="chat",
        display_name="Chat completions",
        path="chat",
        imported_from_snapshot_id="snapshot-1",
        publication_id=publication_id,
    )


def _publication() -> Publication:
    return Publication(
        id="publication_seed",
        tenant_id=TENANT,
        gateway_id="gateway_seed",
        model_endpoint_id="endpoint_seed",
        deployment_name="chat",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Published chat",
        api_name="chat",
        api_path="chat",
        backend_name="mosaic-chat",
        fragment_name="mosaic-chat",
        product_name="mosaic-chat",
        subscription_name="mosaic-chat-bootstrap",
        enforcement=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=12000
        ),
        shape_version="1",
        status=PublicationStatus.PUBLISHED,
        model_api_id=RESOURCE.id,
        governed_access=ModelAccessSettings(),
    )


async def _seed(client: TestClient, *, published: bool = False) -> None:
    gateways: InMemoryGatewayRepository = client.app.state.gateway_repository
    if published:
        await gateways.save_publication(_publication(), _audit())
    await gateways.save_model_api(_model_api("publication_seed" if published else None), _audit())


async def _request(client: TestClient, object_id: str = REQUESTER) -> AccessRequest:
    service: EntitlementService = client.app.state.entitlement_service
    return await service.create_access_request(
        Actor(object_id=object_id, tenant_id=TENANT),
        AccessRequestCreate(resource=RESOURCE, justification="Support bot needs chat"),
    )


def _principal_id(object_id: str = REQUESTER) -> str:
    return deterministic_id("principal", TENANT, object_id)


def _grant_id(principal_id: str, kind: str = "user") -> str:
    return entitlement_id(
        TENANT,
        EntitlementSubject(kind=kind, id=principal_id),
        RESOURCE,
        general_cost_center_id(TENANT),
    )


def _audit_actions(client: TestClient, action: str) -> list[AuditEvent]:
    repository: InMemoryEntitlementRepository = client.app.state.entitlement_repository
    return [event for event in repository.audit_events.values() if event.action == action]


def _stored(client: TestClient, request_id: str) -> AccessRequest:
    repository: InMemoryEntitlementRepository = client.app.state.entitlement_repository
    return repository.access_requests[request_id]


async def test_approval_creates_the_grant_intent_and_links_it(client: TestClient) -> None:
    await _seed(client, published=True)
    created = await _request(client)
    publication_before = await client.app.state.gateway_repository.get_publication(
        TENANT, "publication_seed"
    )

    response = client.post(
        f"/api/v1/access-requests/{created.id}/approve",
        json={"note": "Approved for Q3", "enforcement": LIMITS},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    principal_id = _principal_id()
    grant_id = _grant_id(principal_id)
    assert body["state"] == "approved"
    assert body["grantedEntitlementId"] == grant_id
    assert body["requesterPrincipalId"] == principal_id
    assert body["decidedByObjectId"] == "local-admin"
    assert body["decisionNote"] == "Approved for Q3"
    assert body["decidedAt"]

    grant = client.get(f"/api/v1/entitlements/{grant_id}")
    assert grant.status_code == 200, grant.text
    grant_body = grant.json()
    assert grant_body["subject"] == {"kind": "user", "id": principal_id}
    assert grant_body["resource"]["id"] == RESOURCE.id
    assert grant_body["enabled"] is True
    assert grant_body["enforcement"]["tokens"]["tokensPerMinute"] == 12000
    assert grant_body["enforcement"]["tokens"]["counterKeyExpression"] == (
        "@(context.Subscription.Id)"
    )
    # Intent only: the governed plan still has to be reviewed and applied.
    assert grant_body["runtime"]["status"] == "pending"
    publication_after = await client.app.state.gateway_repository.get_publication(
        TENANT, "publication_seed"
    )
    assert publication_after is not None and publication_before is not None
    assert publication_after.applied_access is None
    assert publication_after.access_state == publication_before.access_state
    assert (
        await client.app.state.gateway_repository.get_publication_lock(TENANT, "publication_seed")
        is None
    )

    [grant_audit] = _audit_actions(client, "entitlement.created")
    assert grant_audit.resource_id == grant_id
    assert grant_audit.details == {
        "accessRequestId": created.id,
        "costCenterId": general_cost_center_id(TENANT),
    }
    [approval_audit] = _audit_actions(client, "accessRequest.approved")
    assert approval_audit.resource_id == created.id
    assert approval_audit.details == {
        "grantedEntitlementId": grant_id,
        "principalId": principal_id,
        "principalCreated": True,
        "costCenterId": general_cost_center_id(TENANT),
        "requestedCostCenterId": general_cost_center_id(TENANT),
    }


async def test_approval_registers_a_requester_mosaic_has_never_seen(client: TestClient) -> None:
    await _seed(client)
    created = await _request(client)
    assert created.requester_principal_id is None
    assert client.get("/api/v1/principals").json() == []

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 200, response.text
    [principal] = client.get("/api/v1/principals").json()
    assert principal["id"] == _principal_id()
    assert principal["objectId"] == REQUESTER
    assert principal["kind"] == "user"
    assert response.json()["requesterPrincipalId"] == principal["id"]
    directory: InMemoryDirectoryRepository = client.app.state.repository
    assert [event.resource_id for event in directory.audit_events.values()] == [principal["id"]]
    assert [event.action for event in directory.audit_events.values()] == ["principal.created"]
    # No limits were confirmed, so the grant is unrestricted beyond publication safeguards.
    grant = client.get(f"/api/v1/entitlements/{response.json()['grantedEntitlementId']}")
    assert grant.json()["enforcement"] is None


async def test_approval_reuses_an_existing_principal(client: TestClient) -> None:
    await _seed(client)
    registered = client.post(
        "/api/v1/principals",
        json={"objectId": REQUESTER, "kind": "user", "label": "Ada Lovelace"},
    ).json()
    created = await _request(client)

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 200, response.text
    assert response.json()["requesterPrincipalId"] == registered["id"]
    assert [item["id"] for item in client.get("/api/v1/principals").json()] == [registered["id"]]
    [approval_audit] = _audit_actions(client, "accessRequest.approved")
    assert approval_audit.details["principalCreated"] is False


async def test_an_application_requester_receives_an_application_grant(
    client: TestClient,
) -> None:
    await _seed(client)
    registered = client.post(
        "/api/v1/principals", json={"objectId": REQUESTER, "kind": "servicePrincipal"}
    ).json()
    created = await _request(client)

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 200, response.text
    grant_id = response.json()["grantedEntitlementId"]
    assert grant_id == _grant_id(registered["id"], kind="application")
    assert client.get(f"/api/v1/entitlements/{grant_id}").json()["subject"] == {
        "kind": "application",
        "id": registered["id"],
    }


async def test_a_principal_registered_with_different_case_is_reused(client: TestClient) -> None:
    await _seed(client)
    registered = client.post(
        "/api/v1/principals", json={"objectId": REQUESTER.upper(), "kind": "user"}
    ).json()
    created = await _request(client)

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 200, response.text
    assert response.json()["requesterPrincipalId"] == registered["id"]
    assert len(client.get("/api/v1/principals").json()) == 1


async def test_approving_again_returns_the_same_grant_without_duplicating_it(
    client: TestClient,
) -> None:
    await _seed(client)
    created = await _request(client)
    first = client.post(
        f"/api/v1/access-requests/{created.id}/approve",
        json={"note": "First", "enforcement": LIMITS},
    )
    assert first.status_code == 200, first.text

    retry = client.post(
        f"/api/v1/access-requests/{created.id}/approve",
        json={"note": "Second", "enforcement": None},
    )

    assert retry.status_code == 200, retry.text
    assert retry.json() == first.json()
    assert len(client.get("/api/v1/entitlements").json()) == 1
    grant = client.get(f"/api/v1/entitlements/{first.json()['grantedEntitlementId']}").json()
    assert grant["enforcement"]["tokens"]["tokensPerMinute"] == 12000
    assert len(_audit_actions(client, "accessRequest.approved")) == 1
    assert len(_audit_actions(client, "entitlement.created")) == 1


async def test_approval_refuses_to_shadow_an_existing_grant(client: TestClient) -> None:
    await _seed(client)
    principal = client.post(
        "/api/v1/principals", json={"objectId": REQUESTER, "kind": "user"}
    ).json()
    existing = client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal["id"]},
            "resource": {"kind": "modelApi", "id": RESOURCE.id},
        },
    ).json()
    client.patch(f"/api/v1/entitlements/{existing['id']}", json={"enabled": False})
    created = await _request(client)

    response = client.post(
        f"/api/v1/access-requests/{created.id}/approve", json={"enforcement": LIMITS}
    )

    assert response.status_code == 409, response.text
    assert "already has a direct grant" in response.json()["message"]
    assert response.json()["details"] == {
        "entitlementId": existing["id"],
        "enabled": False,
        "costCenterId": general_cost_center_id(TENANT),
    }
    pending = _stored(client, created.id)
    assert pending.state == AccessRequestState.PENDING
    assert pending.granted_entitlement_id is None
    unchanged = client.get(f"/api/v1/entitlements/{existing['id']}").json()
    assert unchanged["enabled"] is False
    assert unchanged["enforcement"] is None
    # Deny stays available as the way to close the request.
    assert client.post(f"/api/v1/access-requests/{created.id}/deny", json={}).status_code == 200


async def test_deny_is_unchanged_and_final(client: TestClient) -> None:
    await _seed(client)
    created = await _request(client)

    denied = client.post(
        f"/api/v1/access-requests/{created.id}/deny", json={"note": "Use the shared pool"}
    )

    assert denied.status_code == 200, denied.text
    assert denied.json()["state"] == "denied"
    assert denied.json()["decisionNote"] == "Use the shared pool"
    assert denied.json()["grantedEntitlementId"] is None
    assert client.get("/api/v1/entitlements").json() == []
    assert client.get("/api/v1/principals").json() == []
    assert len(_audit_actions(client, "accessRequest.denied")) == 1

    approve = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})
    assert approve.status_code == 409, approve.text
    assert approve.json()["details"] == {"id": created.id, "state": "denied"}
    assert client.post(f"/api/v1/access-requests/{created.id}/deny", json={}).status_code == 409
    assert client.get("/api/v1/entitlements").json() == []


async def test_a_legacy_approval_without_a_grant_is_reported_not_repaired(
    client: TestClient,
) -> None:
    await _seed(client)
    created = await _request(client)
    repository: InMemoryEntitlementRepository = client.app.state.entitlement_repository
    legacy = AccessRequest.model_validate(
        {**created.model_dump(by_alias=False), "state": AccessRequestState.APPROVED}
    )
    await repository.save_access_request(legacy, _audit())

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 409, response.text
    assert "no grant is linked" in response.json()["message"]
    assert client.get("/api/v1/entitlements").json() == []


async def test_only_an_administrator_may_approve(client: TestClient) -> None:
    await _seed(client)
    created = await _request(client)
    client.app.state.authenticator = LocalAuthenticator(TENANT, roles=["User"])

    response = client.post(
        f"/api/v1/access-requests/{created.id}/approve", json={"enforcement": LIMITS}
    )

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "The Admin app role is required"
    assert _stored(client, created.id).state == AccessRequestState.PENDING
    repository: InMemoryEntitlementRepository = client.app.state.entitlement_repository
    assert repository.entitlements == {}
    assert client.app.state.repository.principals == {}


@pytest.mark.parametrize(
    "enforcement",
    [
        {},
        {"tokens": {"counterKeyExpression": "@(context.Subscription.Id)"}},
        {
            "tokens": {
                "counterKeyExpression": "@(context.Subscription.Id)",
                "tokenQuota": 100000,
            }
        },
        {"requests": {"counterKeyExpression": "@(context.Subscription.Id)", "calls": 60}},
        {"tokens": {"counterKeyExpression": "x", "tokensPerMinute": 0}},
        {"unknown": True},
    ],
)
async def test_limits_are_validated_like_a_direct_grant(
    client: TestClient, enforcement: dict[str, Any]
) -> None:
    await _seed(client)
    created = await _request(client)

    response = client.post(
        f"/api/v1/access-requests/{created.id}/approve", json={"enforcement": enforcement}
    )

    assert response.status_code == 422, response.text
    assert _stored(client, created.id).state == AccessRequestState.PENDING
    assert client.get("/api/v1/entitlements").json() == []
    assert client.get("/api/v1/principals").json() == []


async def test_approval_of_an_ungoverned_resource_is_refused(client: TestClient) -> None:
    await _seed(client)
    created = await _request(client)
    gateways: InMemoryGatewayRepository = client.app.state.gateway_repository
    del gateways.model_apis[RESOURCE.id]

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 422, response.text
    assert "does not govern" in response.json()["message"]
    assert _stored(client, created.id).state == AccessRequestState.PENDING
    assert client.get("/api/v1/principals").json() == []


async def test_approval_waits_for_a_running_publication_change(client: TestClient) -> None:
    await _seed(client, published=True)
    created = await _request(client)
    gateways: InMemoryGatewayRepository = client.app.state.gateway_repository
    await gateways.acquire_publication_lock(TENANT, "publication_seed", "apply-in-progress")

    response = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})

    assert response.status_code == 409, response.text
    assert "already running for this publication" in response.json()["message"]
    assert _stored(client, created.id).state == AccessRequestState.PENDING
    assert client.get("/api/v1/entitlements").json() == []

    await gateways.release_publication_lock(TENANT, "publication_seed", "apply-in-progress")
    retried = client.post(f"/api/v1/access-requests/{created.id}/approve", json={})
    assert retried.status_code == 200, retried.text


class _RacingRepository(InMemoryEntitlementRepository):
    """Commits the approval, then reports a conflict, as if a concurrent call had won."""

    async def approve_access_request(
        self,
        access_request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
    ) -> AccessRequest:
        await super().approve_access_request(access_request, entitlement, audit_events)
        raise ConflictError("The access request was decided or its grant already exists")


class _Harness:
    def __init__(self, repository: InMemoryEntitlementRepository) -> None:
        self.repository = repository
        self.directory = InMemoryDirectoryRepository()
        self.gateways = InMemoryGatewayRepository()
        self.service = EntitlementService(
            repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=InMemoryModelEndpointRepository(),
        )

    async def pending_request(self) -> AccessRequest:
        await self.gateways.save_model_api(_model_api(), _audit())
        return await self.service.create_access_request(
            Actor(object_id=REQUESTER, tenant_id=TENANT), AccessRequestCreate(resource=RESOURCE)
        )


async def test_a_concurrent_approval_converges_on_the_winning_grant() -> None:
    harness = _Harness(_RacingRepository())
    created = await harness.pending_request()

    approved = await harness.service.approve_access_request(
        ADMIN, created.id, AccessRequestApproval()
    )

    assert approved.state == AccessRequestState.APPROVED
    assert approved.granted_entitlement_id == _grant_id(_principal_id())
    assert list(harness.repository.entitlements) == [approved.granted_entitlement_id]


class _FailingRepository(InMemoryEntitlementRepository):
    """Rejects the atomic write until told otherwise, as a lost etag race would."""

    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def approve_access_request(
        self,
        access_request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
    ) -> AccessRequest:
        if self.fail:
            raise ConflictError("The access request changed; reload it and try again")
        return await super().approve_access_request(access_request, entitlement, audit_events)


async def test_a_failed_write_leaves_nothing_half_done_and_a_retry_succeeds() -> None:
    repository = _FailingRepository()
    harness = _Harness(repository)
    created = await harness.pending_request()

    with pytest.raises(ConflictError, match="reload it and try again"):
        await harness.service.approve_access_request(ADMIN, created.id, AccessRequestApproval())

    stored = repository.access_requests[created.id]
    assert stored.state == AccessRequestState.PENDING
    assert stored.granted_entitlement_id is None
    assert repository.entitlements == {}
    assert not [e for e in repository.audit_events.values() if e.action != "accessRequest.created"]
    # Registration grants nothing on its own, so it is left in place and the retry reuses it.
    assert list(harness.directory.principals) == [_principal_id()]

    repository.fail = False
    approved = await harness.service.approve_access_request(
        ADMIN, created.id, AccessRequestApproval()
    )

    assert approved.state == AccessRequestState.APPROVED
    assert approved.requester_principal_id == _principal_id()
    assert list(repository.entitlements) == [approved.granted_entitlement_id]
    assert len(harness.directory.principals) == 1
