"""Lock, credential-audit, and approval guarantees are identical in memory and Cosmos."""

from copy import deepcopy
from typing import Any, cast

import pytest
from azure.core import MatchConditions
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestState,
    AuditEvent,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    deterministic_id,
    new_id,
)
from mosaic_api.errors import ConflictError
from mosaic_api.repositories import (
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
)
from mosaic_api.repositories.cosmos_entitlements import CosmosEntitlementRepository
from mosaic_api.repositories.cosmos_gateway import CosmosGatewayRepository


class Container:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.failure: Exception | None = None
        self.generation = 0
        self.replace_on_read = False
        self.deletes: list[dict[str, Any]] = []

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.failure:
            raise self.failure
        key = (body["tenantId"], body["id"])
        if key in self.items:
            raise exceptions.CosmosResourceExistsError(status_code=409)
        self.generation += 1
        document = {**deepcopy(body), "_etag": f"etag-{self.generation}"}
        self.items[key] = document
        return document

    async def execute_item_batch(
        self, *, batch_operations: list[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]:
        """All or nothing, like a Cosmos transactional batch on one partition."""

        if self.failure:
            raise self.failure
        staged = deepcopy(self.items)
        generation = self.generation
        for index, operation in enumerate(batch_operations):
            kind, arguments = operation[0], operation[1]
            options: dict[str, Any] = operation[2] if len(operation) > 2 else {}
            if kind == "create":
                body = arguments[0]
                item_id = body["id"]
                status = 409 if (partition_key, item_id) in staged else 201
            elif kind == "replace":
                item_id, body = arguments
                current = staged.get((partition_key, item_id))
                expected = options.get("if_match_etag")
                if current is None:
                    status = 404
                elif expected and expected != current["_etag"]:
                    status = 412
                else:
                    status = 200
            else:
                raise NotImplementedError(kind)
            if body["tenantId"] != partition_key:
                status = 400
            if status >= 400:
                responses = [{"statusCode": 424} for _ in batch_operations]
                responses[index] = {"statusCode": status}
                raise exceptions.CosmosBatchOperationError(
                    error_index=index,
                    headers={},
                    status_code=status,
                    message="The batch failed",
                    operation_responses=responses,
                )
            generation += 1
            staged[(partition_key, item_id)] = {**deepcopy(body), "_etag": f"etag-{generation}"}
        self.items, self.generation = staged, generation
        return [{"statusCode": 200} for _ in batch_operations]

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        key = (partition_key, item)
        if key not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        document = deepcopy(self.items[key])
        if self.replace_on_read:
            self.items[key] = {**document, "ownerId": "new-owner", "_etag": "new-etag"}
            self.replace_on_read = False
        return document

    async def delete_item(self, *, item: str, partition_key: str, **kwargs: Any) -> None:
        self.deletes.append(kwargs)
        key = (partition_key, item)
        if key not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        if kwargs.get("etag") and kwargs["etag"] != self.items[key]["_etag"]:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412)
        del self.items[key]


class Cosmos:
    def __init__(self) -> None:
        self.containers = {name: Container() for name in ("desired", "audit", "sync", "observed")}

    def get_database_client(self, _name: str) -> "Cosmos":
        return self

    def get_container_client(self, name: str) -> Container:
        return self.containers[name]


def gateway_repository(cosmos: Cosmos) -> CosmosGatewayRepository:
    return CosmosGatewayRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", "sync", "observed"
    )


def audit() -> AuditEvent:
    return AuditEvent(
        id="audit_reveal",
        tenant_id="tenant",
        action="entitlement.keyReveal",
        resource_type="entitlement",
        resource_id="grant",
        actor_object_id="actor",
    )


async def test_memory_locks_are_conditional_and_tenant_scoped() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_publication_lock("tenant", "publication", "first")
    await repository.acquire_publication_lock("other-tenant", "publication", "other")
    with pytest.raises(ConflictError):
        await repository.acquire_publication_lock("tenant", "publication", "second")
    with pytest.raises(ConflictError):
        await repository.release_publication_lock("tenant", "publication", "second")
    assert await repository.get_publication_lock("tenant", "publication") == "first"
    await repository.release_publication_lock("tenant", "publication", "first")
    assert await repository.get_publication_lock("other-tenant", "publication") == "other"
    assert await repository.get_publication_lock("tenant", "publication") is None


async def test_cosmos_locks_never_expire_and_cannot_be_stolen() -> None:
    cosmos = Cosmos()
    first, second = gateway_repository(cosmos), gateway_repository(cosmos)
    await first.acquire_publication_lock("tenant", "publication", "first")
    lock_id = deterministic_id("publicationLock", "tenant", "publication")
    assert cosmos.containers["desired"].items[("tenant", lock_id)]["ttl"] == -1
    with pytest.raises(ConflictError):
        await second.acquire_publication_lock("tenant", "publication", "second")
    with pytest.raises(ConflictError):
        await second.release_publication_lock("tenant", "publication", "second")
    assert await second.get_publication_lock("tenant", "publication") == "first"
    await first.release_publication_lock("tenant", "publication", "first")
    assert await second.get_publication_lock("tenant", "publication") is None
    assert cosmos.containers["desired"].deletes[-1]["match_condition"] == (
        MatchConditions.IfNotModified
    )


async def test_cosmos_release_checks_etag_after_reading_owner() -> None:
    cosmos = Cosmos()
    repository = gateway_repository(cosmos)
    await repository.acquire_publication_lock("tenant", "publication", "first")
    cosmos.containers["desired"].replace_on_read = True
    with pytest.raises(ConflictError):
        await repository.release_publication_lock("tenant", "publication", "first")
    assert await repository.get_publication_lock("tenant", "publication") == "new-owner"


async def test_memory_audit_does_not_mutate_entitlements() -> None:
    repository = InMemoryEntitlementRepository()
    event = audit()
    await repository.record_audit(event)
    assert repository.audit_events == {event.id: event}
    assert repository.entitlements == {}


async def test_cosmos_audit_must_commit_before_projection() -> None:
    cosmos = Cosmos()
    repository = CosmosEntitlementRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")
    cosmos.containers["desired"].failure = exceptions.CosmosHttpResponseError(status_code=503)
    with pytest.raises(exceptions.CosmosHttpResponseError):
        await repository.record_audit(audit())
    assert cosmos.containers["audit"].items == {}


async def test_cosmos_audit_projection_outage_preserves_durable_outbox() -> None:
    cosmos = Cosmos()
    repository = CosmosEntitlementRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")
    cosmos.containers["audit"].failure = exceptions.CosmosHttpResponseError(status_code=503)
    event = audit()
    await repository.record_audit(event)
    stored = cosmos.containers["desired"].items[(event.tenant_id, event.id)]
    assert stored["action"] == event.action
    assert not cosmos.containers["audit"].items
    cosmos.containers["audit"].failure = None
    await repository._project_audit_event(event)
    assert not cosmos.containers["desired"].items
    assert (event.tenant_id, event.id) in cosmos.containers["audit"].items


def _event(action: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id="tenant",
        action=action,
        resource_type="accessRequest",
        resource_id="accessRequest_1",
        actor_object_id="admin",
    )


def _pending_request() -> AccessRequest:
    return AccessRequest(
        id="accessRequest_1",
        tenant_id="tenant",
        requester_object_id="requester",
        resource=EntitlementResource(kind="modelApi", id="modelApi_1"),
        justification="Support bot",
    )


def _grant() -> Entitlement:
    return Entitlement(
        id="entitlement_1",
        tenant_id="tenant",
        subject=EntitlementSubject(kind="user", id="principal_1"),
        resource=EntitlementResource(kind="modelApi", id="modelApi_1"),
    )


def _approved(read: AccessRequest) -> AccessRequest:
    return AccessRequest.model_validate(
        {
            **read.model_dump(by_alias=False),
            "state": AccessRequestState.APPROVED,
            "requester_principal_id": "principal_1",
            "decided_by_object_id": "admin",
            "granted_entitlement_id": "entitlement_1",
            "etag": read.etag,
        }
    )


def _approval_events() -> list[AuditEvent]:
    return [_event("entitlement.created"), _event("accessRequest.approved")]


async def test_memory_approval_writes_the_grant_and_decision_together() -> None:
    repository = InMemoryEntitlementRepository()
    await repository.create_access_request(_pending_request(), _event("accessRequest.created"))
    events = _approval_events()

    await repository.approve_access_request(_approved(_pending_request()), _grant(), events)

    stored = repository.access_requests["accessRequest_1"]
    assert stored.state == AccessRequestState.APPROVED
    assert stored.granted_entitlement_id == "entitlement_1"
    assert "entitlement_1" in repository.entitlements
    assert {event.id for event in events} <= set(repository.audit_events)


async def test_memory_approval_commits_nothing_when_a_precondition_fails() -> None:
    repository = InMemoryEntitlementRepository()
    await repository.create_access_request(_pending_request(), _event("accessRequest.created"))
    await repository.create_entitlement(_grant(), _event("entitlement.created"))
    before = set(repository.audit_events)

    with pytest.raises(ConflictError):
        await repository.approve_access_request(
            _approved(_pending_request()), _grant(), _approval_events()
        )
    assert repository.access_requests["accessRequest_1"].state == AccessRequestState.PENDING
    assert set(repository.audit_events) == before

    # A request someone else already decided is not overwritten either.
    del repository.entitlements["entitlement_1"]
    denied = AccessRequest.model_validate(
        {**_pending_request().model_dump(by_alias=False), "state": AccessRequestState.DENIED}
    )
    await repository.save_access_request(denied, _event("accessRequest.denied"))
    with pytest.raises(ConflictError):
        await repository.approve_access_request(
            _approved(_pending_request()), _grant(), _approval_events()
        )
    assert repository.access_requests["accessRequest_1"].state == AccessRequestState.DENIED
    assert repository.entitlements == {}


def _entitlement_repository(cosmos: Cosmos) -> CosmosEntitlementRepository:
    return CosmosEntitlementRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")


async def test_cosmos_approval_is_one_transactional_batch() -> None:
    cosmos = Cosmos()
    repository = _entitlement_repository(cosmos)
    await repository.create_access_request(_pending_request(), _event("accessRequest.created"))
    read = await repository.get_access_request("tenant", "accessRequest_1")
    assert read is not None and read.etag
    events = _approval_events()

    await repository.approve_access_request(_approved(read), _grant(), events)

    stored = await repository.get_access_request("tenant", "accessRequest_1")
    assert stored is not None
    assert stored.state == AccessRequestState.APPROVED
    assert stored.granted_entitlement_id == "entitlement_1"
    assert await repository.get_entitlement("tenant", "entitlement_1") is not None
    audit_items = cosmos.containers["audit"].items
    assert all(("tenant", event.id) in audit_items for event in events)
    # Projected events leave the outbox; only the two documents remain as desired state.
    assert {key[1] for key in cosmos.containers["desired"].items} == {
        "accessRequest_1",
        "entitlement_1",
    }


async def test_cosmos_approval_with_a_stale_etag_commits_nothing() -> None:
    cosmos = Cosmos()
    repository = _entitlement_repository(cosmos)
    await repository.create_access_request(_pending_request(), _event("accessRequest.created"))
    read = await repository.get_access_request("tenant", "accessRequest_1")
    assert read is not None
    denied = AccessRequest.model_validate(
        {
            **read.model_dump(by_alias=False),
            "state": AccessRequestState.DENIED,
            "etag": read.etag,
        }
    )
    await repository.save_access_request(denied, _event("accessRequest.denied"))
    audited = set(cosmos.containers["audit"].items)

    with pytest.raises(ConflictError, match="decided or its grant already exists"):
        await repository.approve_access_request(_approved(read), _grant(), _approval_events())

    stored = await repository.get_access_request("tenant", "accessRequest_1")
    assert stored is not None and stored.state == AccessRequestState.DENIED
    assert await repository.get_entitlement("tenant", "entitlement_1") is None
    assert set(cosmos.containers["audit"].items) == audited


async def test_cosmos_approval_over_an_existing_grant_commits_nothing() -> None:
    cosmos = Cosmos()
    repository = _entitlement_repository(cosmos)
    await repository.create_access_request(_pending_request(), _event("accessRequest.created"))
    await repository.create_entitlement(_grant(), _event("entitlement.created"))
    read = await repository.get_access_request("tenant", "accessRequest_1")
    assert read is not None
    audited = set(cosmos.containers["audit"].items)

    with pytest.raises(ConflictError):
        await repository.approve_access_request(_approved(read), _grant(), _approval_events())

    stored = await repository.get_access_request("tenant", "accessRequest_1")
    assert stored is not None
    assert stored.state == AccessRequestState.PENDING
    assert stored.etag == read.etag
    assert set(cosmos.containers["audit"].items) == audited


async def test_cosmos_approval_requires_the_etag_it_was_read_with() -> None:
    repository = _entitlement_repository(Cosmos())
    with pytest.raises(ValueError, match="etag"):
        await repository.approve_access_request(
            _approved(_pending_request()), _grant(), _approval_events()
        )
