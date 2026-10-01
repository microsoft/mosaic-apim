"""Budgets as data: validation, the gateway list, and how a check judges a budget. ADR 0023."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from mosaic_api.budgets import (
    BLOCKED_LIST_MAX_LENGTH,
    BUDGET_KEY_LENGTH,
    EMAIL_RECIPIENTS_PER_MESSAGE,
    MAX_RECIPIENTS,
    NONE_BLOCKED,
    NOTIFICATION_ATTEMPTS,
    STUCK_AFTER,
    Budget,
    BudgetNotification,
    BudgetState,
    BudgetUpdate,
    EmailSettingsUpdate,
    EmailTest,
    Evaluation,
    blocked_list,
    budget_id,
    budget_key,
    evaluate,
    is_hot,
    level_of,
    month_of,
    next_month_start,
    parse_blocked_list,
)
from mosaic_api.cost_centers import MAX_OWNERS
from pydantic import ValidationError

TENANT = "tenant-budgets"
RESEARCH = "costCenter_research"
NOW = datetime(2026, 3, 18, 15, 30, tzinfo=UTC)


def _budget(**values: object) -> Budget:
    fields: dict[str, object] = {
        "id": budget_id(TENANT, RESEARCH),
        "tenant_id": TENANT,
        "cost_center_id": RESEARCH,
        "amount": 1000.0,
    }
    fields.update(values)
    return Budget.model_validate(fields)


def _state() -> BudgetState:
    return BudgetState(
        id="state", tenant_id=TENANT, budget_id=budget_id(TENANT, RESEARCH), cost_center_id=RESEARCH
    )


def _judge(
    budget: Budget, state: BudgetState, spent: float | None, now: datetime = NOW
) -> Evaluation:
    return evaluate(
        budget,
        state,
        spent=spent,
        forecast=None if spent is None else spent * 2,
        through=now,
        unpriced_tokens=0,
        now=now,
    )


# -- validation ------------------------------------------------------------------------------


def test_a_budget_defaults_to_warning_at_80_and_100_and_letting_calls_continue() -> None:
    budget = _budget()

    assert budget.thresholds == [80, 100]
    assert budget.action == "continue"
    assert budget.notify_owners is True
    assert budget.currency == "USD"


@pytest.mark.parametrize(
    "values",
    [
        {"amount": 0},
        {"amount": -5},
        {"amount": float("inf")},
        {"amount": 2_000_000_000},
        {"thresholds": []},
        {"thresholds": [0]},
        {"thresholds": [1001]},
        {"thresholds": [10, 20, 30, 40, 50, 60]},
        {"recipients": ["not an address"]},
        {"recipients": [f"person{index}@contoso.com" for index in range(21)]},
        {"action": "pause"},
    ],
)
def test_a_budget_refuses_what_it_cant_judge(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        BudgetUpdate.model_validate({"amount": 100, **values})


def test_a_budget_tidies_what_it_keeps() -> None:
    update = BudgetUpdate.model_validate(
        {
            "amount": 1234.567,
            "thresholds": [100, 50, 80, 50],
            "recipients": [" finance@contoso.com ", "FINANCE@contoso.com", "ops@fabrikam.com"],
        }
    )

    assert update.amount == 1234.57
    assert update.thresholds == [50, 80, 100]
    assert update.recipients == ["finance@contoso.com", "ops@fabrikam.com"]


def test_every_email_fits_one_communication_services_message() -> None:
    # A budget's addresses and its cost center's owners, together, under the per-message cap, so
    # one notification is always one operation.
    assert MAX_RECIPIENTS + MAX_OWNERS <= EMAIL_RECIPIENTS_PER_MESSAGE


def test_the_organizations_budget_only_warns() -> None:
    with pytest.raises(ValidationError, match="only warns"):
        Budget(
            id=budget_id(TENANT, None),
            tenant_id=TENANT,
            scope="organization",
            amount=50_000,
            action="block",
        )
    with pytest.raises(ValidationError, match="names its cost center"):
        Budget(id="b", tenant_id=TENANT, amount=10)

    organization = Budget(
        id=budget_id(TENANT, None), tenant_id=TENANT, scope="organization", amount=50_000
    )
    assert organization.blocks is False
    assert budget_id(TENANT, None) != budget_id(TENANT, RESEARCH)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://contoso.communication.azure.com",
        "https://contoso.communication.azure.com/emails",
        "https://contoso.communication.azure.com?x=1",
        "https://contoso.example.com",
        "https://communication.azure.com",
        "https://user:pass@contoso.communication.azure.com",
        "https://contoso.communication.azure.com:8443",
    ],
)
def test_email_goes_only_to_a_communication_services_endpoint(endpoint: str) -> None:
    with pytest.raises(ValidationError):
        EmailSettingsUpdate(enabled=False, endpoint=endpoint, sender="mosaic@contoso.com")


def test_email_settings_need_an_endpoint_and_a_sender_to_be_on() -> None:
    with pytest.raises(ValidationError, match="endpoint and a sender"):
        EmailSettingsUpdate(enabled=True, endpoint="https://contoso.communication.azure.com")

    commercial = EmailSettingsUpdate(
        enabled=True,
        endpoint="https://Contoso-Mosaic.communication.azure.com/",
        sender="DoNotReply@contoso.com",
    )
    government = EmailSettingsUpdate(
        enabled=True,
        endpoint="https://contoso.communication.azure.us",
        sender="DoNotReply@contoso.com",
    )
    off = EmailSettingsUpdate(enabled=False)

    assert commercial.endpoint == "https://contoso-mosaic.communication.azure.com"
    assert government.endpoint == "https://contoso.communication.azure.us"
    assert (off.endpoint, off.sender) == (None, None)
    with pytest.raises(ValidationError):
        EmailTest(to="nobody")


# -- the gateway list ------------------------------------------------------------------------


def test_a_cost_centers_key_is_short_and_stable() -> None:
    key = budget_key(RESEARCH)

    assert len(key) == BUDGET_KEY_LENGTH
    assert key == budget_key(RESEARCH)
    assert key != budget_key("costCenter_sales")
    assert all(character in "0123456789abcdef" for character in key)


def test_the_list_is_a_dash_when_nothing_is_blocked() -> None:
    assert blocked_list([]) == (NONE_BLOCKED, [])
    assert parse_blocked_list(NONE_BLOCKED) == set()


def test_the_list_keeps_oldest_blocks_first_and_says_what_doesnt_fit() -> None:
    keys = [f"{index:012x}" for index in range(400)]

    value, left = blocked_list(keys)

    assert len(value) <= BLOCKED_LIST_MAX_LENGTH
    kept = value.split(",")
    # Twelve hex digits and a comma each: 315 fit in API Management's 4,096 characters.
    assert len(kept) == 315
    assert kept == keys[:315]
    assert left == keys[315:]
    assert parse_blocked_list(value) == set(kept)


@pytest.mark.parametrize(
    "value",
    [None, "", "*", "abc", "ABCDEF012345", "0123456789ab,", ",0123456789ab", "0123456789ab;x"],
)
def test_a_list_mosaic_didnt_write_is_unreadable(value: str | None) -> None:
    assert parse_blocked_list(value) is None


# -- judging a budget ------------------------------------------------------------------------


def test_each_threshold_is_reached_once_a_month() -> None:
    budget = _budget()

    at_70 = _judge(budget, _state(), 700)
    at_85 = _judge(budget, at_70.state, 850)
    again = _judge(budget, at_85.state, 900)

    assert at_70.due == []
    assert [(notice.kind, notice.threshold) for notice in at_85.due] == [("threshold", 80)]
    assert at_85.reached == [80]
    assert again.due == []
    assert again.state.crossed == [80]
    assert again.state.month_to_date == 900
    assert again.state.forecast == 1800


def test_a_threshold_is_reached_at_exactly_its_amount() -> None:
    assert _judge(_budget(), _state(), 800.0).reached == [80]
    assert _judge(_budget(), _state(), 799.99).reached == []


def test_thresholds_reached_at_once_send_one_email_for_the_highest() -> None:
    result = _judge(_budget(), _state(), 1200)

    assert result.reached == [80, 100]
    assert [(notice.threshold, notice.status) for notice in result.due] == [(100, "sending")]
    skipped = [notice for notice in result.state.notifications if notice.status == "skipped"]
    assert [notice.threshold for notice in skipped] == [80]
    assert result.blocked is False


def test_a_budget_that_blocks_blocks_at_100_percent() -> None:
    budget = _budget(action="block")

    near = _judge(budget, _state(), 950)
    used = _judge(budget, near.state, 1000)
    still = _judge(budget, used.state, 1100)

    assert not near.blocked and not near.state.blocked
    assert used.blocked and used.state.blocked
    assert used.state.blocked_at == NOW
    assert [notice.kind for notice in used.due] == ["threshold", "blocked"]
    assert not still.blocked and still.due == []
    assert still.state.blocked


def test_a_budget_that_continues_never_blocks() -> None:
    result = _judge(_budget(action="continue"), _state(), 5000)

    assert not result.blocked
    assert not result.state.blocked


def test_the_new_month_lifts_the_block_and_starts_the_thresholds_again() -> None:
    budget = _budget(action="block")
    blocked = _judge(budget, _state(), 1500)

    april = _judge(budget, blocked.state, 10, now=datetime(2026, 4, 1, 0, 5, tzinfo=UTC))

    assert april.unblocked == "newMonth"
    assert not april.state.blocked
    assert april.state.month == "2026-04"
    assert april.state.crossed == []
    assert [(notice.kind, notice.reason) for notice in april.due] == [("unblocked", "newMonth")]
    # Last month's notifications go with it, so a new month's thresholds email again.
    may_like = _judge(budget, april.state, 900, now=datetime(2026, 4, 20, tzinfo=UTC))
    assert may_like.reached == [80]


def test_raising_the_budget_lifts_its_block_and_rearms_thresholds_it_no_longer_reaches() -> None:
    blocked = _judge(_budget(action="block"), _state(), 1050)

    raised = _judge(_budget(action="block", amount=2000), blocked.state, 1050)

    assert raised.unblocked == "budgetRaised"
    assert not raised.state.blocked
    assert raised.state.crossed == []
    assert raised.state.amount == 2000
    assert [(notice.kind, notice.reason) for notice in raised.due] == [
        ("unblocked", "budgetRaised")
    ]
    # At 80% of the new amount, its 80% email goes again.
    later = _judge(_budget(action="block", amount=2000), raised.state, 1700)
    assert later.reached == [80]


def test_raising_the_budget_too_little_keeps_the_block() -> None:
    blocked = _judge(_budget(action="block"), _state(), 1500)

    raised = _judge(_budget(action="block", amount=1200), blocked.state, 1500)

    assert raised.unblocked is None
    assert raised.state.blocked
    assert raised.due == []


def test_spend_that_drops_with_a_corrected_price_changes_nothing() -> None:
    budget = _budget(action="block")
    blocked = _judge(budget, _state(), 1100)

    corrected = _judge(budget, blocked.state, 600)
    back = _judge(budget, corrected.state, 1100)

    assert corrected.state.blocked
    assert corrected.state.crossed == [80, 100]
    assert corrected.due == []
    assert back.due == []


def test_turning_blocking_off_lifts_the_block() -> None:
    blocked = _judge(_budget(action="block"), _state(), 1100)

    continued = _judge(_budget(action="continue"), blocked.state, 1100)

    assert continued.unblocked == "blockingOff"
    assert not continued.state.blocked
    assert [notice.kind for notice in continued.due] == ["unblocked"]


def test_a_month_mosaic_cant_price_changes_nothing() -> None:
    budget = _budget(action="block")
    blocked = _judge(budget, _state(), 1100)

    unpriced = evaluate(
        budget,
        blocked.state,
        spent=None,
        forecast=None,
        through=None,
        unpriced_tokens=500,
        now=NOW + timedelta(hours=1),
        error="MOSAIC couldn't read this month's spend.",
    )

    assert unpriced.state.blocked
    assert unpriced.unblocked is None
    assert unpriced.due == []
    assert unpriced.state.month_to_date == 1100
    assert unpriced.state.error == "MOSAIC couldn't read this month's spend."
    # Raising the budget can't lift the block either, until MOSAIC knows the spend again.
    raised = evaluate(
        _budget(action="block", amount=5000),
        blocked.state,
        spent=None,
        forecast=None,
        through=None,
        unpriced_tokens=0,
        now=NOW,
    )
    assert raised.state.blocked


def test_a_refused_email_is_due_again_until_it_has_had_its_attempts() -> None:
    budget = _budget()
    first = _judge(budget, _state(), 850)
    [notice] = first.due
    state = first.state
    for attempt in range(1, NOTIFICATION_ATTEMPTS + 1):
        for item in state.notifications:
            if item.id == notice.id:
                item.status = "failed"
                item.attempts = attempt
        retried = _judge(budget, state, 850)
        if attempt < NOTIFICATION_ATTEMPTS:
            assert [item.id for item in retried.due] == [notice.id]
        else:
            assert retried.due == []
        state = retried.state


def test_an_email_a_stopped_check_claimed_is_never_sent_twice() -> None:
    budget = _budget()
    claimed = _judge(budget, _state(), 850)

    soon = _judge(budget, claimed.state, 850, now=NOW + timedelta(minutes=5))
    later = _judge(budget, claimed.state, 850, now=NOW + STUCK_AFTER + timedelta(minutes=1))

    assert soon.due == []
    [stuck] = later.state.notifications
    assert stuck.status == "failed"
    assert stuck.attempts == NOTIFICATION_ATTEMPTS
    assert later.due == []


def test_levels_and_how_often_a_budget_is_checked() -> None:
    budget = _budget(action="block")
    month = month_of(NOW)

    assert level_of(budget, None, month) == "ok"
    assert level_of(budget, _judge(budget, _state(), 500).state, month) == "ok"
    warned = _judge(budget, _state(), 850).state
    assert level_of(budget, warned, month) == "warning"
    assert level_of(_budget(), _judge(_budget(), _state(), 1500).state, month) == "exceeded"
    blocked = _judge(budget, _state(), 1500).state
    assert level_of(budget, blocked, month) == "blocked"
    # Last month's figures don't count this month.
    assert level_of(budget, blocked, "2026-04") == "ok"

    assert not is_hot(budget, warned, month)
    assert is_hot(budget, _judge(budget, _state(), 920).state, month)
    # Once it's blocked, nothing is left to come this month.
    assert not is_hot(budget, blocked, month)
    assert next_month_start(NOW) == datetime(2026, 4, 1, tzinfo=UTC)
    assert next_month_start(datetime(2026, 12, 31, 23, 59, tzinfo=UTC)) == datetime(
        2027, 1, 1, tzinfo=UTC
    )


def test_a_notification_records_when_it_was_claimed() -> None:
    notice = BudgetNotification(kind="threshold", threshold=80, created_at=NOW)

    assert notice.status == "sending"
    assert notice.attempts == 0
    assert notice.id.startswith("notice_")
