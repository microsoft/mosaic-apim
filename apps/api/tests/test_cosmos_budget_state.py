"""A budget's state in Cosmos is saved only over the version that was read. See ADR 0023.

Two checks that judge the same budget at once can't both claim the same email: the claim is part
of the conditional write, so the second applies its judgement to what the first saved.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

import pytest
from azure.cosmos.aio import CosmosClient
from mosaic_api.budgets import (
    Budget,
    BudgetState,
    Evaluation,
    budget_id,
    budget_state_id,
    evaluate,
    gate_state_id,
)
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos_budgets import STATE_WRITE_ATTEMPTS, CosmosBudgetRepository
from test_cosmos_usage_state import Container, Cosmos

TENANT = "tenant-budgets"
RESEARCH = "costCenter_research"
BUDGET = budget_id(TENANT, RESEARCH)
NOW = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)


def _repository() -> tuple[CosmosBudgetRepository, Container]:
    cosmos = Cosmos()
    repository = CosmosBudgetRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired-state", "audit-events", "usage-rollups"
    )
    return repository, cosmos.container


def _budget() -> Budget:
    return Budget(id=BUDGET, tenant_id=TENANT, cost_center_id=RESEARCH, amount=1000, action="block")


def _judging(spent: float, decided: list[Evaluation]) -> Callable[[BudgetState], BudgetState]:
    def change(state: BudgetState) -> BudgetState:
        result = evaluate(
            _budget(),
            state,
            spent=spent,
            forecast=None,
            through=NOW,
            unpriced_tokens=0,
            now=NOW,
        )
        decided.append(result)
        return result.state

    return change


async def test_the_first_check_creates_the_budgets_state() -> None:
    repository, container = _repository()
    decided: list[Evaluation] = []

    saved = await repository.update_budget_state(TENANT, BUDGET, _judging(850, decided))

    stored = await repository.get_budget_state(TENANT, BUDGET)
    assert stored is not None
    assert stored.id == budget_state_id(TENANT, BUDGET)
    assert stored.budget_id == BUDGET
    assert stored.crossed == [80]
    assert saved.crossed == [80]
    assert container.writes == 1


async def test_two_checks_at_once_claim_each_email_once() -> None:
    repository, container = _repository()
    first: list[Evaluation] = []
    second: list[Evaluation] = []
    # The second check saves between the first's read and its write.
    container.before_write = lambda: repository.update_budget_state(
        TENANT, BUDGET, _judging(1050, second)
    )

    saved = await repository.update_budget_state(TENANT, BUDGET, _judging(1050, first))

    # The second claimed the 100% email and the block; the first, judging again over what the
    # second saved, found nothing left to send.
    assert [notice.threshold for notice in second[-1].due if notice.kind == "threshold"] == [100]
    assert second[-1].blocked
    assert first[-1].due == []
    assert not first[-1].blocked
    assert saved.blocked
    threshold_notices = [notice for notice in saved.notifications if notice.kind == "threshold"]
    assert sorted(notice.threshold or 0 for notice in threshold_notices) == [80, 100]
    assert [notice.kind for notice in saved.notifications].count("blocked") == 1


async def test_a_change_that_raises_saves_nothing() -> None:
    repository, container = _repository()

    def refuse(_state: BudgetState) -> BudgetState:
        raise ConflictError("Already lifted")

    with pytest.raises(ConflictError, match="Already lifted"):
        await repository.update_budget_state(TENANT, BUDGET, refuse)

    assert container.writes == 0


async def test_an_update_gives_up_when_the_state_keeps_changing() -> None:
    repository, container = _repository()
    await repository.update_budget_state(TENANT, BUDGET, _judging(100, []))
    container.always_changed = True
    attempts: list[Evaluation] = []

    with pytest.raises(ConflictError, match="kept changing"):
        await repository.update_budget_state(TENANT, BUDGET, _judging(900, attempts))

    assert len(attempts) == STATE_WRITE_ATTEMPTS
    stored = await repository.get_budget_state(TENANT, BUDGET)
    assert stored is not None
    assert stored.crossed == []


async def test_a_gateways_list_state_is_saved_the_same_way() -> None:
    repository, container = _repository()
    container.before_write = lambda: repository.update_gate_state(
        TENANT, "gateway-a", lambda state: state.model_copy(update={"error": "busy"})
    )

    saved = await repository.update_gate_state(
        TENANT, "gateway-a", lambda state: state.model_copy(update={"value": "-"})
    )

    assert (saved.value, saved.error) == ("-", "busy")
    assert saved.id == gate_state_id(TENANT, "gateway-a")
    assert saved.gateway_id == "gateway-a"
