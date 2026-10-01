"""Each managed gateway's blocked list: created before any policy reads it, never a publication's
to delete, and holding exactly the cost centers whose budgets block now. See ADR 0023."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

from conftest import reviewed_unpublish
from mosaic_api.budgets import (
    BLOCKED_COST_CENTERS_NAMED_VALUE,
    NONE_BLOCKED,
    Budget,
    BudgetState,
    budget_id,
    budget_key,
    month_of,
)
from mosaic_api.domain import (
    ApimResourceId,
    AuditEvent,
    PublishAction,
    PublishedResourceKind,
    PublishRunStatus,
    new_id,
    utc_now,
)
from mosaic_api.integrations.apim import ApimClient, ApimWriter
from mosaic_api.repositories import InMemoryBudgetRepository, InMemoryGatewayRepository
from mosaic_api.services.budget_gate import BlockedListGate
from mosaic_api.services.publishing import DENY_ALL_POLICY, PublishingService
from test_governed_lifecycle import ACTOR, AUDIENCE, TENANT, Harness, harness

__all__ = ["harness"]

NAMED = f"namedValues/{BLOCKED_COST_CENTERS_NAMED_VALUE}"
RESEARCH = "costCenter_research"


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="budget",
        resource_id="budget",
        actor_object_id="tester",
    )


async def _block(repository: InMemoryBudgetRepository, cost_center_id: str, at: datetime) -> None:
    budget = await repository.save_budget(
        Budget(
            id=budget_id(TENANT, cost_center_id),
            tenant_id=TENANT,
            cost_center_id=cost_center_id,
            amount=100,
            action="block",
        ),
        _audit(),
    )
    await repository.update_budget_state(
        TENANT,
        budget.id,
        lambda state: state.model_copy(
            update={
                "cost_center_id": cost_center_id,
                "month": month_of(at),
                "blocked": True,
                "blocked_at": at,
            }
        ),
    )


def _with_gate(harness: Harness, gate: BlockedListGate) -> PublishingService:
    return PublishingService(
        harness.gateways,
        endpoint_repository=harness.endpoints,
        directory_repository=harness.directory,
        entitlement_repository=harness.entitlements,
        client_factory=lambda resource: ApimClient(harness.arm, resource),
        writer_factory=lambda resource: ApimWriter(harness.arm, resource),
        model_runtime_client_id=AUDIENCE,
        cost_center_repository=harness.cost_center_records,
        blocked_list=gate,
    )


def _later_puts(harness: Harness, since: int) -> list[str]:
    return [suffix for verb, suffix in harness.apim.writes[since:] if verb == "PUT"]


# -- publishing ------------------------------------------------------------------------------


async def test_a_governed_plan_creates_the_list_before_the_policy_that_reads_it(
    harness: Harness,
) -> None:
    await harness.grant()
    await harness.govern()
    since = len(harness.apim.writes)

    plan = await harness.service.plan(ACTOR, harness.publication_id)
    run = await harness.service.apply(ACTOR, harness.publication_id, plan.id)
    await harness.service.wait_for_idle()

    first = plan.steps[0]
    assert (first.kind, first.name, first.action, first.stage) == (
        PublishedResourceKind.NAMED_VALUE,
        BLOCKED_COST_CENTERS_NAMED_VALUE,
        PublishAction.CREATE,
        "prepare",
    )
    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    puts = _later_puts(harness, since)
    fragment = next(index for index, path in enumerate(puts) if path.startswith("policyFragments/"))
    assert puts.index(NAMED) < fragment
    assert harness.apim.dangling_references == []
    assert harness.apim.written[NAMED]["properties"] == {
        "displayName": BLOCKED_COST_CENTERS_NAMED_VALUE,
        "value": NONE_BLOCKED,
        "secret": False,
        "tags": ["mosaic"],
    }
    # The gateway's, not the publication's: nothing records it as something to remove.
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert all(item.name != BLOCKED_COST_CENTERS_NAMED_VALUE for item in publication.resources)
    replanned = await harness.service.plan(ACTOR, harness.publication_id)
    assert replanned.steps[0].action == PublishAction.NO_CHANGE
    assert replanned.steps[0].existed


async def test_an_apply_never_overwrites_the_list_the_budget_check_keeps(
    harness: Harness,
) -> None:
    await harness.grant()
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    harness.apim.written[NAMED]["properties"]["value"] = budget_key(RESEARCH)
    since = len(harness.apim.writes)

    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED

    assert NAMED not in _later_puts(harness, since)
    assert harness.apim.written[NAMED]["properties"]["value"] == budget_key(RESEARCH)


async def test_a_new_list_holds_what_budgets_block_now(harness: Harness) -> None:
    budgets = InMemoryBudgetRepository()
    await _block(budgets, RESEARCH, utc_now())
    harness.service = _with_gate(
        harness, BlockedListGate(budgets, gateway_repository=harness.gateways)
    )
    await harness.grant()
    await harness.govern()

    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED

    assert harness.apim.written[NAMED]["properties"]["value"] == budget_key(RESEARCH)


async def test_unpublishing_never_deletes_the_gateways_list(harness: Harness) -> None:
    await harness.grant()
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED

    await reviewed_unpublish(harness.service, ACTOR, harness.publication_id)
    await harness.service.wait_for_idle()

    assert NAMED not in harness.apim.write_paths("DELETE")
    assert NAMED in harness.apim.written


async def test_an_apply_that_cant_create_the_list_writes_no_policy_that_reads_it(
    harness: Harness,
) -> None:
    harness.apim.fail_write(NAMED, 400)
    await harness.grant()
    await harness.govern()

    run = await harness.apply()

    assert run.status == PublishRunStatus.FAILED
    assert harness.apim.dangling_references == []
    fragments = [
        body
        for suffix, body in harness.apim.written.items()
        if suffix.startswith("policyFragments/")
    ]
    assert all(
        BLOCKED_COST_CENTERS_NAMED_VALUE not in body["properties"]["value"] for body in fragments
    )
    # Every call is refused until a reviewed retry succeeds: the failure fails closed.
    policy = next(
        body for suffix, body in harness.apim.written.items() if suffix.endswith("policies/policy")
    )
    assert policy["properties"]["value"] == DENY_ALL_POLICY


# -- what the list holds ---------------------------------------------------------------------


class _Gateways:
    """Just enough of the gateway repository for the list's content."""

    async def list_publications(self, _tenant_id: str) -> list[Any]:
        return []

    async def list_mcp_publications(self, _tenant_id: str) -> list[Any]:
        return []

    async def list_gateways(self, _tenant_id: str) -> list[Any]:
        return []


async def test_the_list_names_only_this_months_blocks_whose_budgets_still_block() -> None:
    now = datetime(2026, 3, 18, 12, tzinfo=UTC)
    budgets = InMemoryBudgetRepository()
    await _block(budgets, "costCenter_newer", now)
    await _block(budgets, RESEARCH, now - timedelta(days=2))
    await _block(budgets, "costCenter_lastMonth", datetime(2026, 2, 27, tzinfo=UTC))
    await _block(budgets, "costCenter_continues", now)
    continues = await budgets.get_budget(TENANT, budget_id(TENANT, "costCenter_continues"))
    assert continues is not None
    await budgets.save_budget(continues.model_copy(update={"action": "continue"}), _audit())
    await _block(budgets, "costCenter_removed", now)
    removed = await budgets.get_budget(TENANT, budget_id(TENANT, "costCenter_removed"))
    assert removed is not None
    await budgets.delete_budget(removed, _audit())
    gate = BlockedListGate(
        budgets, gateway_repository=cast(InMemoryGatewayRepository, _Gateways()), clock=lambda: now
    )

    value, left_out = await gate.value(TENANT)

    # Oldest block first.
    assert value == f"{budget_key(RESEARCH)},{budget_key('costCenter_newer')}"
    assert left_out == []


async def test_a_full_list_keeps_the_oldest_blocks_and_reports_the_rest() -> None:
    now = datetime(2026, 3, 18, 12, tzinfo=UTC)
    budgets = InMemoryBudgetRepository()
    for index in range(320):
        await _block(budgets, f"costCenter_{index:04d}", now + timedelta(seconds=index))
    gate = BlockedListGate(
        budgets, gateway_repository=cast(InMemoryGatewayRepository, _Gateways()), clock=lambda: now
    )

    value, left_out = await gate.value(TENANT)

    assert len(value) <= 4096
    assert len(value.split(",")) == 315
    assert left_out == [f"costCenter_{index:04d}" for index in range(315, 320)]


def test_the_list_state_starts_empty() -> None:
    state = BudgetState(id="s", tenant_id=TENANT, budget_id="b")

    assert (state.blocked, state.crossed, state.notifications) == (False, [], [])


# -- two writers at once ---------------------------------------------------------------------

GATEWAY_RESOURCE = (
    "/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai"
    "/providers/Microsoft.ApiManagement/service/apim-ai"
)


class _RacingGateway:
    """One gateway's named values, where another check blocks a cost center during a write.

    ``during_write`` runs once, between this writer working its list out and its write landing,
    as when another check's write lands first and this one's, worked out earlier, lands last.
    """

    def __init__(self, during_write: Callable[[], Awaitable[None]] | None = None) -> None:
        self.value: str | None = None
        self.writes: list[str] = []
        self._during_write = during_write
        self.resource = ApimResourceId.parse(GATEWAY_RESOURCE)

    async def get_named_value(self, _name: str) -> dict[str, Any] | None:
        return None if self.value is None else {"properties": {"value": self.value}}

    async def put_plain_named_value(self, _name: str, value: str) -> dict[str, Any]:
        if self._during_write is not None:
            during, self._during_write = self._during_write, None
            await during()
        self.value = value
        self.writes.append(value)
        return {}


def _gateway() -> Any:
    return SimpleNamespace(id="gateway_ai", name="AI gateway", azure_resource_id=GATEWAY_RESOURCE)


async def test_a_write_that_worked_its_list_out_before_another_block_writes_again() -> None:
    now = datetime(2026, 3, 18, 12, tzinfo=UTC)
    budgets = InMemoryBudgetRepository()
    await _block(budgets, RESEARCH, now)
    gateway = _RacingGateway(lambda: _block(budgets, "costCenter_support", now))
    gate = BlockedListGate(
        budgets, gateway_repository=cast(InMemoryGatewayRepository, _Gateways()), clock=lambda: now
    )

    synced = await gate.sync(
        TENANT,
        lambda _resource: cast(Any, gateway),
        lambda _resource: cast(Any, gateway),
        gateways=[_gateway()],
    )

    both = f"{budget_key(RESEARCH)},{budget_key('costCenter_support')}"
    # The first write carried the list from before Support's block; the second has both.
    assert gateway.writes == [budget_key(RESEARCH), both]
    assert gateway.value == both
    assert synced.value == both
    [state] = synced.states
    assert state.value == both


async def test_a_new_list_written_while_a_block_starts_holds_the_block() -> None:
    now = datetime(2026, 3, 18, 12, tzinfo=UTC)
    budgets = InMemoryBudgetRepository()
    gateway = _RacingGateway(lambda: _block(budgets, RESEARCH, now))
    gate = BlockedListGate(
        budgets, gateway_repository=cast(InMemoryGatewayRepository, _Gateways()), clock=lambda: now
    )

    assert await gate.ensure(cast(Any, gateway), cast(Any, gateway), TENANT)

    assert gateway.writes == [NONE_BLOCKED, budget_key(RESEARCH)]


async def test_with_budget_checks_off_the_list_blocks_no_one() -> None:
    now = datetime(2026, 3, 18, 12, tzinfo=UTC)
    budgets = InMemoryBudgetRepository()
    await _block(budgets, RESEARCH, now)
    gate = BlockedListGate(
        budgets,
        gateway_repository=cast(InMemoryGatewayRepository, _Gateways()),
        clock=lambda: now,
        enabled=False,
    )

    assert await gate.value(TENANT) == (NONE_BLOCKED, [])
