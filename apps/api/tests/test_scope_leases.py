"""Scope leases expire, can't be stolen while held, and are taken in one global order."""

import asyncio
from datetime import timedelta

import pytest
from azure.core import MatchConditions
from mosaic_api.domain import deterministic_id, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.repositories import InMemoryGatewayRepository
from mosaic_api.services import model_access
from mosaic_api.services.model_access import (
    ENVIRONMENT_BUSY_MESSAGE,
    ENVIRONMENTS_SCOPE,
    endpoint_mutation_scope,
    environment_guard,
    gateway_mutation_scope,
    scope_lease,
)
from test_model_access_repositories import Cosmos, gateway_repository


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(model_access, "SCOPE_LEASE_BACKOFF_SECONDS", 0.0)


async def test_memory_leases_are_exclusive_tenant_scoped_and_owner_released() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_scope_lease("tenant", "environments", "first", lease_seconds=60)
    await repository.acquire_scope_lease("other", "environments", "other", lease_seconds=60)
    with pytest.raises(ConflictError):
        await repository.acquire_scope_lease("tenant", "environments", "second", lease_seconds=60)
    # Releasing someone else's lease leaves it with its holder.
    await repository.release_scope_lease("tenant", "environments", "second")
    with pytest.raises(ConflictError):
        await repository.acquire_scope_lease("tenant", "environments", "second", lease_seconds=60)
    await repository.release_scope_lease("tenant", "environments", "first")
    await repository.acquire_scope_lease("tenant", "environments", "second", lease_seconds=60)
    assert repository.scope_leases[("other", "environments")][0] == "other"


async def test_memory_expired_lease_can_be_taken_over() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_scope_lease("tenant", "environments", "stalled", lease_seconds=-1)
    await repository.acquire_scope_lease("tenant", "environments", "next", lease_seconds=60)
    await repository.release_scope_lease("tenant", "environments", "stalled")
    assert repository.scope_leases[("tenant", "environments")][0] == "next"


async def test_cosmos_lease_is_exclusive_until_released() -> None:
    cosmos = Cosmos()
    first, second = gateway_repository(cosmos), gateway_repository(cosmos)
    await first.acquire_scope_lease("tenant", "environments", "first", lease_seconds=60)
    lease_id = deterministic_id("scopeLease", "tenant", "environments")
    document = cosmos.containers["desired"].items[("tenant", lease_id)]
    assert document["ownerId"] == "first"
    assert document["entityType"] == "scopeLease"
    with pytest.raises(ConflictError) as refused:
        await second.acquire_scope_lease("tenant", "environments", "second", lease_seconds=60)
    assert refused.value.details == {"scope": "environments"}
    await first.release_scope_lease("tenant", "environments", "first")
    assert ("tenant", lease_id) not in cosmos.containers["desired"].items
    assert cosmos.containers["desired"].deletes[-1]["match_condition"] == (
        MatchConditions.IfNotModified
    )
    await second.acquire_scope_lease("tenant", "environments", "second", lease_seconds=60)


async def test_cosmos_expired_lease_is_taken_over_and_old_owner_cannot_release_it() -> None:
    cosmos = Cosmos()
    stalled, next_owner = gateway_repository(cosmos), gateway_repository(cosmos)
    await stalled.acquire_scope_lease("tenant", "endpoint:e1", "stalled", lease_seconds=-1)
    await next_owner.acquire_scope_lease("tenant", "endpoint:e1", "next", lease_seconds=60)
    lease_id = deterministic_id("scopeLease", "tenant", "endpoint:e1")
    assert cosmos.containers["desired"].items[("tenant", lease_id)]["ownerId"] == "next"
    await stalled.release_scope_lease("tenant", "endpoint:e1", "stalled")
    assert cosmos.containers["desired"].items[("tenant", lease_id)]["ownerId"] == "next"


async def test_cosmos_takeover_loses_to_a_concurrent_taker() -> None:
    cosmos = Cosmos()
    repository = gateway_repository(cosmos)
    await repository.acquire_scope_lease("tenant", "environments", "stalled", lease_seconds=-1)
    # Someone else replaces the expired lease between our read and our conditional replace.
    cosmos.containers["desired"].replace_on_read = True
    with pytest.raises(ConflictError):
        await repository.acquire_scope_lease("tenant", "environments", "late", lease_seconds=60)
    lease_id = deterministic_id("scopeLease", "tenant", "environments")
    assert cosmos.containers["desired"].items[("tenant", lease_id)]["ownerId"] == "new-owner"


async def test_cosmos_unreadable_expiry_counts_as_expired() -> None:
    cosmos = Cosmos()
    repository = gateway_repository(cosmos)
    lease_id = deterministic_id("scopeLease", "tenant", "environments")
    cosmos.containers["desired"].items[("tenant", lease_id)] = {
        "id": lease_id,
        "tenantId": "tenant",
        "ownerId": "corrupt",
        "expiresAt": "not-a-date",
        "_etag": "etag-corrupt",
    }
    await repository.acquire_scope_lease("tenant", "environments", "fresh", lease_seconds=60)
    assert cosmos.containers["desired"].items[("tenant", lease_id)]["ownerId"] == "fresh"


async def test_cosmos_release_after_the_lease_is_gone_is_quiet() -> None:
    cosmos = Cosmos()
    repository = gateway_repository(cosmos)
    await repository.release_scope_lease("tenant", "environments", "nobody")


async def test_scope_lease_retries_until_the_holder_releases() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_scope_lease("tenant", ENVIRONMENTS_SCOPE, "holder", lease_seconds=60)
    attempts = 0
    acquire = repository.acquire_scope_lease

    async def counting_acquire(
        tenant_id: str, scope: str, owner_id: str, *, lease_seconds: float
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 3:
            await repository.release_scope_lease(tenant_id, scope, "holder")
        await acquire(tenant_id, scope, owner_id, lease_seconds=lease_seconds)

    repository.acquire_scope_lease = counting_acquire  # type: ignore[method-assign]
    async with scope_lease(repository, "tenant", ENVIRONMENTS_SCOPE) as owner:
        assert repository.scope_leases[("tenant", ENVIRONMENTS_SCOPE)][0] == owner
    assert attempts == 3
    assert ("tenant", ENVIRONMENTS_SCOPE) not in repository.scope_leases


async def test_scope_lease_gives_up_with_a_plain_message() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_scope_lease("tenant", ENVIRONMENTS_SCOPE, "holder", lease_seconds=60)
    with pytest.raises(ConflictError) as refused:
        async with scope_lease(repository, "tenant", ENVIRONMENTS_SCOPE):
            pytest.fail("the lease is held by someone else")
    assert refused.value.message == ENVIRONMENT_BUSY_MESSAGE
    assert refused.value.details == {"scope": ENVIRONMENTS_SCOPE}
    assert repository.scope_leases[("tenant", ENVIRONMENTS_SCOPE)][0] == "holder"


async def test_scope_lease_releases_when_the_body_raises() -> None:
    repository = InMemoryGatewayRepository()
    with pytest.raises(RuntimeError):
        async with scope_lease(repository, "tenant", "endpoint:e1"):
            raise RuntimeError("boom")
    assert repository.scope_leases == {}


class RecordingRepository(InMemoryGatewayRepository):
    def __init__(self) -> None:
        super().__init__()
        self.taken: list[str] = []

    async def acquire_scope_lease(
        self, tenant_id: str, scope: str, owner_id: str, *, lease_seconds: float
    ) -> None:
        await super().acquire_scope_lease(tenant_id, scope, owner_id, lease_seconds=lease_seconds)
        self.taken.append(f"lease {scope}")

    async def acquire_publication_lock(
        self, tenant_id: str, publication_id: str, owner_id: str
    ) -> None:
        await super().acquire_publication_lock(tenant_id, publication_id, owner_id)
        self.taken.append(f"lock {publication_id}")


async def test_environment_guard_takes_scopes_in_the_global_order() -> None:
    repository = RecordingRepository()
    async with environment_guard(
        repository,
        "tenant",
        gateway_ids=["g2", "g1", "g2"],
        endpoint_ids=["e2", "e1"],
        publication_ids=["p2", "p1"],
    ):
        assert repository.taken == [
            f"lease {ENVIRONMENTS_SCOPE}",
            f"lock {gateway_mutation_scope('g1')}",
            f"lock {gateway_mutation_scope('g2')}",
            f"lease {endpoint_mutation_scope('e1')}",
            f"lease {endpoint_mutation_scope('e2')}",
            "lock p1",
            "lock p2",
        ]
    assert repository.scope_leases == {}
    assert repository.publication_locks == {}


async def test_environment_guard_releases_what_it_took_when_a_lock_is_busy() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_publication_lock("tenant", "p1", "apply-in-progress")
    with pytest.raises(ConflictError):
        async with environment_guard(
            repository, "tenant", gateway_ids=["g1"], endpoint_ids=["e1"], publication_ids=["p1"]
        ):
            pytest.fail("the publication is locked by a running apply")
    assert repository.scope_leases == {}
    assert repository.publication_locks == {("tenant", "p1"): "apply-in-progress"}


async def test_environment_guard_can_skip_the_tenant_scope() -> None:
    repository = RecordingRepository()
    async with environment_guard(repository, "tenant", environments=False, endpoint_ids=["e1"]):
        assert repository.taken == [f"lease {endpoint_mutation_scope('e1')}"]


async def test_concurrent_guards_serialize_on_the_tenant_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real (short) backoff, so the waiting guard doesn't depend on event-loop scheduling order.
    monkeypatch.setattr(model_access, "SCOPE_LEASE_BACKOFF_SECONDS", 0.01)
    repository = InMemoryGatewayRepository()
    order: list[str] = []
    entered = asyncio.Event()

    async def first() -> None:
        async with environment_guard(repository, "tenant"):
            order.append("first in")
            entered.set()
            await asyncio.sleep(0.005)
            order.append("first out")

    async def second() -> None:
        await entered.wait()
        async with environment_guard(repository, "tenant"):
            order.append("second in")

    await asyncio.gather(first(), second())
    assert order == ["first in", "first out", "second in"]


async def test_lease_expiry_is_in_the_future() -> None:
    repository = InMemoryGatewayRepository()
    await repository.acquire_scope_lease("tenant", "environments", "owner", lease_seconds=60)
    expiry = repository.scope_leases[("tenant", "environments")][1]
    assert utc_now() + timedelta(seconds=30) < expiry <= utc_now() + timedelta(seconds=60)
