import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.cost_center_requests import (
    CostCenterRequest,
    CostCenterRequestApprovalIntent,
    CostCenterRequestCreate,
    requested_cost_center_id,
)
from mosaic_api.cost_centers import CostCenterCreate
from mosaic_api.domain import AccessRequest, AuditEvent, EntitlementResource, new_id, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.repositories import (
    EntitlementRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
)
from mosaic_api.repositories.cosmos_entitlements import CosmosEntitlementRepository
from mosaic_api.services.notification_events import (
    access_request_event,
    cost_center_request_event,
)
from mosaic_api.services.notification_visibility import NotificationVisibility
from mosaic_api.services.notifications import notification_summary, notification_title
from pydantic import ValidationError
from test_model_access_repositories import Container, Cosmos

TENANT = "fictional-request-tenant"
USER = "11111111-1111-4111-8111-111111111111"
OTHER_USER = "22222222-2222-4222-8222-222222222222"
GROUP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class RequestContainer(Container):
    async def query_items(
        self, *, query: str, parameters: list[dict[str, Any]], partition_key: str
    ) -> AsyncIterator[dict[str, Any]]:
        values = {item["name"]: item["value"] for item in parameters}
        assert partition_key == values["@tenantId"]
        assert "@entityType" in query
        if "@requesterObjectId" in values:
            assert "c.requesterObjectId = @requesterObjectId" in query
        if "@state" in values:
            assert "c.state = @state" in query
        for document in self.items.values():
            if (
                document["tenantId"] == partition_key
                and document["entityType"] == values["@entityType"]
                and (
                    "@requesterObjectId" not in values
                    or document["requesterObjectId"] == values["@requesterObjectId"]
                )
                and ("@state" not in values or document["state"] == values["@state"])
            ):
                yield deepcopy(document)


@dataclass
class Store:
    repository: EntitlementRepository
    memory: InMemoryEntitlementRepository | None = None
    cosmos: Cosmos | None = None

    def snapshot(self) -> object:
        if self.memory is not None:
            return deepcopy(
                (
                    self.memory.cost_center_requests,
                    self.memory.audit_events,
                    self.memory.notification_events,
                )
            )
        assert self.cosmos is not None
        return deepcopy(
            (self.cosmos.containers["desired"].items, self.cosmos.containers["audit"].items)
        )


def cosmos_store() -> Store:
    cosmos = Cosmos()
    cosmos.containers["desired"] = RequestContainer()
    repository = CosmosEntitlementRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")
    return Store(repository, cosmos=cosmos)


@pytest.fixture(params=["memory", "cosmos"])
def store(request: pytest.FixtureRequest) -> Store:
    if request.param == "cosmos":
        return cosmos_store()
    repository = InMemoryEntitlementRepository()
    return Store(repository, memory=repository)


def creation(request_id: str = "center-request", tenant_id: str = TENANT) -> CostCenterRequest:
    return CostCenterRequest(
        id=request_id,
        tenant_id=tenant_id,
        requester_object_id=USER,
        name="Fictional research",
        justification="Fictional model evaluation project",
        suggested_group_object_id=GROUP,
    )


def audit(request: CostCenterRequest) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=request.tenant_id,
        actor_object_id=USER,
        action="costCenterRequest.test",
        resource_type="costCenterRequest",
        resource_id=request.id,
    )


def approval(request: CostCenterRequest, **updates: Any) -> CostCenterRequestApprovalIntent:
    return CostCenterRequestApprovalIntent.model_validate(
        {
            "cost_center": CostCenterCreate(name=request.name, code="RESEARCH"),
            "cost_center_id": requested_cost_center_id(request.tenant_id, request.id),
            "decided_by_object_id": "fictional-admin",
            **updates,
        }
    )


async def submit(store: Store, request: CostCenterRequest | None = None) -> CostCenterRequest:
    pending = request or creation()
    return await store.repository.create_cost_center_request(
        pending, audit(pending), [cost_center_request_event(pending)]
    )


def decided(request: CostCenterRequest, state: str) -> CostCenterRequest:
    intent = approval(request) if state == "approved" else request.approval
    return request.model_copy(
        update={
            "state": state,
            "approval": intent,
            "decided_at": utc_now(),
            "decided_by_object_id": "fictional-admin",
        }
    )


def test_creation_request_is_not_a_resource_grant() -> None:
    request = creation()
    payload = request.model_dump(mode="json", by_alias=True)
    assert payload["requestKind"] == "costCenter"
    assert "resource" not in payload
    assert request.cost_center_id is None
    assert CostCenterRequestCreate(name=" Center ", justification=" Purpose ").name == "Center"
    assert CostCenterRequestCreate(name="Center", justification="Purpose").code is None


@pytest.mark.parametrize(
    "updates",
    [
        {"name": " "},
        {"justification": "\t"},
        {"suggestedGroupObjectId": "local-group-123"},
        {"idempotencyKey": "not-a-guid"},
    ],
)
def test_create_input_rejects_invalid_values(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CostCenterRequestCreate.model_validate(
            {"name": "Center", "justification": "Purpose", **updates}
        )


def test_group_suggestion_is_an_optional_canonical_object_id() -> None:
    request = CostCenterRequestCreate(
        name="Center", justification="Purpose", suggested_group_object_id=GROUP.upper()
    )
    assert request.suggested_group_object_id == GROUP


def test_approval_reserves_exactly_one_center_identity() -> None:
    pending = creation()
    assert requested_cost_center_id(TENANT, pending.id) != requested_cost_center_id(
        "another-tenant", pending.id
    )
    with pytest.raises(ValidationError, match="stable cost-center ID"):
        CostCenterRequest.model_validate(
            {
                **pending.model_dump(),
                "approval": approval(pending).model_copy(update={"cost_center_id": "other-center"}),
            }
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"state": "approved"},
        {"state": "denied"},
        {"state": "pending", "decided_at": utc_now()},
        {"requester_membership_state": "succeeded"},
    ],
)
def test_request_cannot_claim_an_unrecorded_decision(updates: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        CostCenterRequest.model_validate({**creation().model_dump(), **updates})


async def test_create_and_reads_are_independent_copies_with_etags(store: Store) -> None:
    saved = await submit(store)
    assert saved.etag
    saved.name = "Changed returned instance"
    loaded = await store.repository.get_cost_center_request(TENANT, saved.id)
    assert loaded is not None and loaded.name == "Fictional research"
    loaded.name = "Changed read instance"
    again = await store.repository.get_cost_center_request(TENANT, saved.id)
    assert again is not None and again.name == "Fictional research"


async def test_create_is_conditional_and_atomic_with_lifecycle_events(store: Store) -> None:
    await submit(store)
    before = store.snapshot()
    with pytest.raises(ConflictError):
        await submit(store)
    assert store.snapshot() == before


async def test_requests_and_queries_are_tenant_and_requester_scoped(store: Store) -> None:
    first = await submit(store)
    await submit(store, creation(first.id, "another-tenant"))
    other = creation("another-request").model_copy(update={"requester_object_id": OTHER_USER})
    await submit(store, other)
    assert await store.repository.get_cost_center_request("wrong-tenant", first.id) is None
    mine = await store.repository.list_cost_center_requests(TENANT, requester_object_id=USER)
    assert [item.id for item in mine] == [first.id]
    assert len(await store.repository.list_cost_center_requests(TENANT)) == 2
    assert await store.repository.list_cost_center_requests(TENANT, state="approved") == []
    assert await store.repository.list_cost_center_requests(TENANT, state="' OR true") == []


async def test_save_requires_read_etag_and_never_partially_writes(store: Store) -> None:
    saved = await submit(store)
    invalid = decided(saved, "denied").model_copy(update={"etag": None})
    before = store.snapshot()
    with pytest.raises(ValueError, match="read ETag"):
        await store.repository.save_cost_center_request(invalid, audit(invalid), [])
    assert store.snapshot() == before


async def test_stale_etag_cannot_override_a_decision_or_append_events(store: Store) -> None:
    saved = await submit(store)
    denied = decided(saved, "denied")
    await store.repository.save_cost_center_request(
        denied, audit(denied), [cost_center_request_event(denied)]
    )
    before = store.snapshot()
    stale = decided(saved, "approved")
    with pytest.raises(ConflictError):
        await store.repository.save_cost_center_request(
            stale, audit(stale), [cost_center_request_event(stale)]
        )
    assert store.snapshot() == before


async def test_only_one_concurrent_approval_intent_can_claim_the_request(store: Store) -> None:
    pending = await submit(store)
    first = pending.model_copy(update={"approval": approval(pending)})
    second = pending.model_copy(
        update={"approval": approval(pending, decided_by_object_id="another-admin")}
    )
    results = await asyncio.gather(
        store.repository.save_cost_center_request(first, audit(first), []),
        store.repository.save_cost_center_request(second, audit(second), []),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CostCenterRequest) for item in results) == 1
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    loaded = await store.repository.get_cost_center_request(TENANT, pending.id)
    assert loaded is not None and loaded.cost_center_id == requested_cost_center_id(
        TENANT, pending.id
    )


async def test_claimed_approval_configuration_cannot_be_replaced(store: Store) -> None:
    pending = await submit(store)
    claimed = pending.model_copy(update={"approval": approval(pending)})
    saved = await store.repository.save_cost_center_request(claimed, audit(claimed), [])
    assert saved.approval is not None
    changed = saved.model_copy(
        update={
            "approval": saved.approval.model_copy(
                update={"cost_center": CostCenterCreate(name="Other", code="OTHER")}
            )
        }
    )
    before = store.snapshot()
    with pytest.raises(ConflictError, match="configuration is fixed"):
        await store.repository.save_cost_center_request(changed, audit(changed), [])
    assert store.snapshot() == before


async def test_submitted_proposal_cannot_be_rewritten_while_deciding(store: Store) -> None:
    pending = await submit(store)
    changed = pending.model_copy(update={"suggested_group_object_id": OTHER_USER})
    before = store.snapshot()
    with pytest.raises(ConflictError, match="submitted request"):
        await store.repository.save_cost_center_request(changed, audit(changed), [])
    assert store.snapshot() == before


async def test_decided_requests_cannot_change_state(store: Store) -> None:
    pending = await submit(store)
    denied = decided(pending, "denied")
    saved = await store.repository.save_cost_center_request(
        denied, audit(denied), [cost_center_request_event(denied)]
    )
    changed = decided(saved, "approved")
    before = store.snapshot()
    with pytest.raises(ConflictError, match="already been decided"):
        await store.repository.save_cost_center_request(changed, audit(changed), [])
    assert store.snapshot() == before


async def test_decision_note_is_immutable_after_approval(store: Store) -> None:
    pending = await submit(store)
    approved = decided(pending, "approved")
    saved = await store.repository.save_cost_center_request(
        approved, audit(approved), [cost_center_request_event(approved)]
    )
    changed = saved.model_copy(update={"decision_note": "A different decision"})
    with pytest.raises(ConflictError, match="decision cannot be changed"):
        await store.repository.save_cost_center_request(changed, audit(changed), [])


@pytest.mark.parametrize("bad_part", ["audit", "eventTenant", "requestKind", "requestId"])
async def test_cross_source_events_and_audits_never_commit(store: Store, bad_part: str) -> None:
    pending = creation()
    audit_event = audit(pending)
    event = cost_center_request_event(pending)
    if bad_part == "audit":
        audit_event = audit_event.model_copy(update={"tenant_id": "wrong-tenant"})
    elif bad_part == "eventTenant":
        event = event.model_copy(update={"tenant_id": "wrong-tenant"})
    elif bad_part == "requestKind":
        event = event.model_copy(
            update={"subject": event.subject.model_copy(update={"request_kind": "resource"})}
        )
    else:
        event = event.model_copy(
            update={"subject": event.subject.model_copy(update={"request_id": "other-request"})}
        )
    before = store.snapshot()
    with pytest.raises(ValueError):
        await store.repository.create_cost_center_request(pending, audit_event, [event])
    assert store.snapshot() == before


async def test_duplicate_events_in_one_mutation_do_not_partially_commit(store: Store) -> None:
    pending = creation()
    event = cost_center_request_event(pending)
    before = store.snapshot()
    with pytest.raises(ValueError, match="distinct record IDs"):
        await store.repository.create_cost_center_request(pending, audit(pending), [event, event])
    assert store.snapshot() == before


async def test_successful_initial_member_add_is_not_replayed_as_pending(store: Store) -> None:
    pending = await submit(store)
    approved = decided(pending, "approved")
    approved = approved.model_copy(
        update={
            "approval": approval(pending, add_requester=True),
            "requester_membership_state": "succeeded",
        }
    )
    saved = await store.repository.save_cost_center_request(
        approved, audit(approved), [cost_center_request_event(approved)]
    )
    changed = saved.model_copy(update={"requester_membership_state": "pending"})
    with pytest.raises(ConflictError, match="already completed"):
        await store.repository.save_cost_center_request(changed, audit(changed), [])


async def test_cosmos_outage_does_not_save_a_request_without_its_events() -> None:
    store = cosmos_store()
    assert store.cosmos is not None
    store.cosmos.containers["desired"].failure = exceptions.CosmosHttpResponseError(status_code=503)
    before = store.snapshot()
    with pytest.raises(exceptions.CosmosHttpResponseError):
        await submit(store)
    assert store.snapshot() == before


async def test_cosmos_audit_projection_outage_retains_the_request_audit_and_event() -> None:
    store = cosmos_store()
    assert store.cosmos is not None
    store.cosmos.containers["audit"].failure = exceptions.CosmosHttpResponseError(status_code=503)
    saved = await submit(store)
    assert saved.etag
    documents = list(store.cosmos.containers["desired"].items.values())
    assert {item["entityType"] for item in documents} == {
        "costCenterRequest",
        "notificationEvent",
        "auditEvent",
    }


async def test_other_request_kind_is_not_read_as_a_cost_center_request(store: Store) -> None:
    resource_request = AccessRequest(
        id="resource-request",
        tenant_id=TENANT,
        requester_object_id=USER,
        resource=EntitlementResource(kind="modelApi", id="model"),
    )
    await store.repository.create_access_request(resource_request, audit(creation()))
    assert await store.repository.get_cost_center_request(TENANT, resource_request.id) is None


@pytest.mark.parametrize("state", ["pending", "approved", "denied", "withdrawn"])
def test_notification_is_kind_aware_and_contains_no_requester_free_text(state: str) -> None:
    request = creation()
    if state != "pending":
        request = decided(request, state)
    event = cost_center_request_event(request)
    assert event.subject.request_kind == "costCenter"
    assert event.subject.resource is None
    assert not event.data.requires_plan_apply
    serialized = event.model_dump_json()
    assert request.justification not in serialized
    assert request.name not in serialized
    assert GROUP not in serialized
    assert "Cost center request" in notification_title(event)
    assert "grant intent" not in notification_summary(event)


async def test_cost_center_request_notification_is_visible_only_to_its_requester(
    store: Store,
) -> None:
    saved = await submit(store)

    async def recipients(_event: object) -> list[str]:
        return []

    visibility = NotificationVisibility(
        store.repository, InMemoryDirectoryRepository(), grant_recipients=recipients
    )
    event = cost_center_request_event(saved)
    assert await visibility.authorize(event, USER, "portal")
    assert not await visibility.authorize(event, OTHER_USER, "portal")
    resource_kind = access_request_event(
        AccessRequest(
            id=saved.id,
            tenant_id=TENANT,
            requester_object_id=USER,
            resource=EntitlementResource(kind="modelApi", id="model"),
        )
    )
    assert not await visibility.authorize(resource_kind, USER, "portal")
