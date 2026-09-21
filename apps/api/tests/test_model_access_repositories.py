"""Conditional lock and credential-audit guarantees are identical in memory and Cosmos."""

from copy import deepcopy
from typing import Any, cast

import pytest
from azure.core import MatchConditions
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import AuditEvent, deterministic_id
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
