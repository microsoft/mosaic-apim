"""A gateway's rollup state in Cosmos is saved only over the version that was read."""

from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos_usage import STATE_WRITE_ATTEMPTS, CosmosUsageRollupRepository
from mosaic_api.usage_telemetry import UsageRollupState, usage_rollup_state_id

TENANT = "tenant-usage"
GATEWAY = "gateway-usage"
REQUESTED = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)


class Container:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.generation = 0
        self.writes = 0
        # Runs once, before the next write, as another writer saving first would.
        self.before_write: Callable[[], Awaitable[object]] | None = None
        # Every replace finds the item changed since it was read.
        self.always_changed = False

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        key = (partition_key, item)
        if key not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        return deepcopy(self.items[key])

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        await self._interrupt()
        key = (body["tenantId"], body["id"])
        if key in self.items:
            raise exceptions.CosmosResourceExistsError(status_code=409)
        return self._store(key, body)

    async def replace_item(
        self, *, item: str, body: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        await self._interrupt()
        key = (body["tenantId"], item)
        current = self.items.get(key)
        if current is None:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        if self.always_changed:
            self.generation += 1
            current["_etag"] = f"changed-{self.generation}"
        if kwargs.get("etag") != current["_etag"]:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412)
        return self._store(key, body)

    async def _interrupt(self) -> None:
        hook, self.before_write = self.before_write, None
        if hook is not None:
            await hook()

    def _store(self, key: tuple[str, str], body: dict[str, Any]) -> dict[str, Any]:
        self.generation += 1
        self.writes += 1
        document = {**deepcopy(body), "_etag": f"etag-{self.generation}", "_ts": self.generation}
        self.items[key] = document
        return deepcopy(document)


class Cosmos:
    def __init__(self) -> None:
        self.container = Container()

    def get_database_client(self, _name: str) -> "Cosmos":
        return self

    def get_container_client(self, _name: str) -> Container:
        return self.container


def _repository() -> tuple[CosmosUsageRollupRepository, Container]:
    cosmos = Cosmos()
    return (
        CosmosUsageRollupRepository(cast(CosmosClient, cosmos), "mosaic", "usage-rollups"),
        cosmos.container,
    )


def _rows(count: int) -> Callable[[UsageRollupState], UsageRollupState]:
    return lambda state: state.model_copy(update={"last_rows": count})


def _refresh(state: UsageRollupState) -> UsageRollupState:
    return state.model_copy(update={"refresh_requested_at": REQUESTED})


async def test_the_first_update_creates_the_gateways_state() -> None:
    repository, container = _repository()

    saved = await repository.update_rollup_state(TENANT, GATEWAY, _rows(3))

    stored = await repository.get_rollup_state(TENANT, GATEWAY)
    assert stored is not None
    assert (stored.id, stored.gateway_id, stored.last_rows) == (
        usage_rollup_state_id(TENANT, GATEWAY),
        GATEWAY,
        3,
    )
    assert saved.last_rows == 3
    assert container.writes == 1


async def test_an_update_is_applied_again_over_a_save_made_since_it_read() -> None:
    repository, container = _repository()
    await repository.update_rollup_state(TENANT, GATEWAY, _rows(1))
    container.before_write = lambda: repository.update_rollup_state(TENANT, GATEWAY, _refresh)

    saved = await repository.update_rollup_state(TENANT, GATEWAY, _rows(5))

    stored = await repository.get_rollup_state(TENANT, GATEWAY)
    assert stored is not None
    assert (stored.last_rows, stored.refresh_requested_at) == (5, REQUESTED)
    assert (saved.last_rows, saved.refresh_requested_at) == (5, REQUESTED)


async def test_two_writers_creating_the_state_both_keep_their_change() -> None:
    repository, container = _repository()
    container.before_write = lambda: repository.update_rollup_state(TENANT, GATEWAY, _refresh)

    await repository.update_rollup_state(TENANT, GATEWAY, _rows(2))

    stored = await repository.get_rollup_state(TENANT, GATEWAY)
    assert stored is not None
    assert (stored.last_rows, stored.refresh_requested_at) == (2, REQUESTED)


async def test_a_change_that_raises_saves_nothing() -> None:
    repository, container = _repository()

    def refuse(_state: UsageRollupState) -> UsageRollupState:
        raise ConflictError("Refreshed less than a minute ago")

    with pytest.raises(ConflictError, match="less than a minute"):
        await repository.update_rollup_state(TENANT, GATEWAY, refuse)

    assert container.writes == 0
    assert await repository.get_rollup_state(TENANT, GATEWAY) is None


async def test_an_update_gives_up_when_the_state_keeps_changing() -> None:
    repository, container = _repository()
    await repository.update_rollup_state(TENANT, GATEWAY, _rows(1))
    container.always_changed = True
    applied: list[int] = []

    def change(state: UsageRollupState) -> UsageRollupState:
        applied.append(state.last_rows)
        return state.model_copy(update={"last_rows": 9})

    with pytest.raises(ConflictError, match="kept changing"):
        await repository.update_rollup_state(TENANT, GATEWAY, change)

    assert len(applied) == STATE_WRITE_ATTEMPTS
    stored = await repository.get_rollup_state(TENANT, GATEWAY)
    assert stored is not None
    assert stored.last_rows == 1
