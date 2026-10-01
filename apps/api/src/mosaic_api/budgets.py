"""Monthly budgets, the email that warns about them, and the gateway list that blocks. ADR 0023.

A budget is administrator-authored desired state: a monthly amount in US dollars, for one cost
center across every gateway, or for the whole organization. MOSAIC compares each UTC month's spend
so far, priced from the rollups and the price list exactly as the Cost tab prices it, with it:

- at each threshold, 80% and 100% unless an administrator chooses others, it emails the budget's
  recipients, once a month;
- at 100%, a cost center's budget either lets calls continue or blocks them. A block puts the cost
  center's short key on the ``mosaic-blocked-cost-centers`` named value of each gateway MOSAIC
  manages, which every governed policy reads, so the gateway refuses the cost center's calls;
- the organization's budget only warns.

A block lifts at the start of the next UTC month, or as soon as an administrator raises the budget
above what's been spent, turns blocking off, or removes the budget.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from mosaic_api.domain import (
    CostCenterRef,
    Entity,
    MosaicModel,
    deterministic_id,
    new_id,
)

# The named value each managed gateway holds, and every governed policy reads. MOSAIC owns it.
BLOCKED_COST_CENTERS_NAMED_VALUE = "mosaic-blocked-cost-centers"
# API Management caps a named value at 4,096 characters.
BLOCKED_LIST_MAX_LENGTH = 4096
# API Management refuses an empty named value, so an empty list is this.
NONE_BLOCKED = "-"
# A cost center's key on the list: the first hex digits of a hash of its ID. Twelve digits leave
# room for 315 blocked cost centers in one named value.
BUDGET_KEY_LENGTH = 12
BLOCKED_LIST_PATTERN = re.compile(
    rf"-|[0-9a-f]{{{BUDGET_KEY_LENGTH}}}(?:,[0-9a-f]{{{BUDGET_KEY_LENGTH}}})*"
)
DEFAULT_THRESHOLDS = (80, 100)
MAX_THRESHOLDS = 5
MAX_THRESHOLD = 1000
# With a cost center's owners, at most 40 addresses: one message, which Communication Services caps
# at 50 recipients unless its quota is raised.
MAX_RECIPIENTS = 20
EMAIL_RECIPIENTS_PER_MESSAGE = 50
MAX_AMOUNT = 1_000_000_000.0
# A cost center this close to a threshold it hasn't reached is checked every few minutes.
HOT_PERCENT = 90.0
# The lease the evaluator and every budget change hold, so only one judges budgets at a time.
BUDGETS_SCOPE = "budgets"
BUDGETS_BUSY = "MOSAIC is checking budgets right now. Try again in a moment."
SYSTEM_ACTOR = "system:budgets"
ORGANIZATION = "organization"
# A notification that failed is tried again on later checks, up to this many attempts in all.
NOTIFICATION_ATTEMPTS = 3
# A notification still sending this long after it was claimed belongs to a check that stopped.
STUCK_AFTER = timedelta(hours=1)
# Hosts MOSAIC sends its managed identity's Communication Services token to.
EMAIL_HOST_SUFFIXES = (".communication.azure.com", ".communication.azure.us")
_EMAIL = re.compile(r"[^@\s,;<>\"']+@[^@\s,;<>\"']+\.[^@\s,;<>\"']+")

BudgetAction = Literal["block", "continue"]
BudgetScope = Literal["costCenter", "organization"]
BudgetLevel = Literal["ok", "warning", "exceeded", "blocked"]
NotificationKind = Literal["threshold", "blocked", "unblocked"]
NotificationStatus = Literal["sending", "sent", "failed", "skipped"]
BlockReason = Literal["budgetUsed"]
UnblockReason = Literal["newMonth", "budgetRaised", "blockingOff", "budgetRemoved"]


def budget_key(cost_center_id: str) -> str:
    """A cost center's short key on the gateway list. The governed policy compiles the same key."""

    return hashlib.sha256(cost_center_id.encode("utf-8")).hexdigest()[:BUDGET_KEY_LENGTH]


def budget_id(tenant_id: str, cost_center_id: str | None) -> str:
    return deterministic_id("budget", tenant_id, cost_center_id or ORGANIZATION)


def budget_state_id(tenant_id: str, budget: str) -> str:
    return deterministic_id("budgetstate", tenant_id, budget)


def gate_state_id(tenant_id: str, gateway_id: str) -> str:
    return deterministic_id("budgetgate", tenant_id, gateway_id)


def email_settings_id(tenant_id: str) -> str:
    return deterministic_id("emailSettings", tenant_id)


def month_of(moment: datetime) -> str:
    """The UTC calendar month a moment falls in, as ``YYYY-MM``."""

    current = moment.astimezone(UTC)
    return f"{current.year:04d}-{current.month:02d}"


def next_month_start(moment: datetime) -> datetime:
    current = moment.astimezone(UTC)
    if current.month == 12:
        return datetime(current.year + 1, 1, 1, tzinfo=UTC)
    return datetime(current.year, current.month + 1, 1, tzinfo=UTC)


def blocked_list(keys: Iterable[str]) -> tuple[str, list[str]]:
    """The named value for these keys, in order, and the keys that don't fit in it.

    Keys come oldest block first, so a list that's full keeps the blocks it already holds.
    """

    kept: list[str] = []
    left: list[str] = []
    length = 0
    for key in dict.fromkeys(keys):
        added = len(key) + (1 if kept else 0)
        if length + added <= BLOCKED_LIST_MAX_LENGTH:
            kept.append(key)
            length += added
        else:
            left.append(key)
    return (",".join(kept) or NONE_BLOCKED), left


def parse_blocked_list(value: str | None) -> set[str] | None:
    """The keys a gateway's list holds, or None when it isn't a list MOSAIC wrote."""

    if value is None or not BLOCKED_LIST_PATTERN.fullmatch(value):
        return None
    return set() if value == NONE_BLOCKED else set(value.split(","))


def email_address(value: str) -> str:
    candidate = value.strip()
    if len(candidate) > 254 or not _EMAIL.fullmatch(candidate):
        raise ValueError("Use an email address, such as finance-team@contoso.com")
    return candidate


def email_addresses(values: Iterable[str], *, limit: int = MAX_RECIPIENTS) -> list[str]:
    found: list[str] = []
    for value in values:
        candidate = email_address(value)
        if candidate.casefold() not in {item.casefold() for item in found}:
            found.append(candidate)
    if len(found) > limit:
        raise ValueError(f"A budget emails at most {limit} addresses")
    return found


def _thresholds(values: Sequence[int]) -> list[int]:
    found = sorted(set(values))
    if not 1 <= len(found) <= MAX_THRESHOLDS:
        raise ValueError(f"A budget warns at 1 to {MAX_THRESHOLDS} thresholds")
    if any(not 1 <= value <= MAX_THRESHOLD for value in found):
        raise ValueError(f"Each threshold is a percentage from 1 to {MAX_THRESHOLD}")
    return found


def _amount(value: float) -> float:
    return round(value, 2)


class Budget(Entity):
    """A monthly amount a cost center, or the organization, means to spend at most."""

    entity_type: Literal["budget"] = "budget"
    scope: BudgetScope = "costCenter"
    # None for the organization's budget.
    cost_center_id: str | None = None
    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    currency: Literal["USD"] = "USD"
    # Percentages of the amount at which recipients are emailed.
    thresholds: list[int] = Field(default_factory=lambda: list(DEFAULT_THRESHOLDS))
    # Addresses to email besides the cost center's owners. They need no MOSAIC account.
    recipients: list[str] = Field(default_factory=list)
    notify_owners: bool = True
    # What happens at 100%. The organization's budget only warns.
    action: BudgetAction = "continue"
    updated_by: str | None = None

    _validate_thresholds = field_validator("thresholds")(_thresholds)
    _validate_amount = field_validator("amount")(_amount)

    @field_validator("recipients")
    @classmethod
    def _validate_recipients(cls, value: list[str]) -> list[str]:
        return email_addresses(value)

    @model_validator(mode="after")
    def _validate_scope(self) -> Self:
        if self.scope == "organization":
            if self.cost_center_id is not None:
                raise ValueError("The organization's budget names no cost center")
            if self.action != "continue":
                raise ValueError("The organization's budget only warns; it can't block")
        elif not self.cost_center_id:
            raise ValueError("A cost center's budget names its cost center")
        return self

    @property
    def blocks(self) -> bool:
        return self.scope == "costCenter" and self.action == "block"

    def reached(self, spent: float, threshold: int) -> bool:
        # Compared in cents, so a spend of exactly the threshold reaches it whatever the rounding.
        return round(spent * 100) >= round(self.amount * threshold)


class BudgetUpdate(MosaicModel):
    """A budget as an administrator sets it. Replaces what was there."""

    amount: float = Field(gt=0, le=MAX_AMOUNT, allow_inf_nan=False)
    thresholds: list[int] = Field(default_factory=lambda: list(DEFAULT_THRESHOLDS))
    recipients: list[str] = Field(default_factory=list, max_length=200)
    notify_owners: bool = True
    action: BudgetAction = "continue"

    _validate_thresholds = field_validator("thresholds")(_thresholds)
    _validate_amount = field_validator("amount")(_amount)

    @field_validator("recipients")
    @classmethod
    def _validate_recipients(cls, value: list[str]) -> list[str]:
        return email_addresses(value)


class BudgetNotification(MosaicModel):
    """One email about a budget this month: a threshold reached, or a block put on or lifted.

    It's recorded, as ``sending``, before the email goes, in the same conditional write that
    decides it's due. So two checks can never both send it, and a check that stopped half way
    never sends it twice. A send ACS refused is tried again on later checks.
    """

    id: str = Field(default_factory=lambda: new_id("notice"))
    kind: NotificationKind
    threshold: int | None = None
    reason: BlockReason | UnblockReason | None = None
    status: NotificationStatus = "sending"
    recipients: int = 0
    attempts: int = 0
    created_at: datetime
    first_attempt_at: datetime | None = None
    sent_at: datetime | None = None
    error: str | None = None


class BudgetState(Entity):
    """Where one budget stands this month. Kept in ``usage-rollups``, beside the rollup state."""

    entity_type: Literal["budgetState"] = "budgetState"
    budget_id: str
    cost_center_id: str | None = None
    # The UTC month the rest describes, as YYYY-MM.
    month: str | None = None
    # The budget amount the thresholds and the block were last judged against.
    amount: float | None = None
    month_to_date: float | None = None
    forecast: float | None = None
    unpriced_tokens: int = 0
    # When this month's figures run to.
    through: datetime | None = None
    # Thresholds reached this month, each of which has its notification.
    crossed: list[int] = Field(default_factory=list)
    blocked: bool = False
    blocked_at: datetime | None = None
    unblocked_at: datetime | None = None
    unblock_reason: UnblockReason | None = None
    notifications: list[BudgetNotification] = Field(default_factory=list)
    evaluated_at: datetime | None = None
    # Why the last check couldn't judge the budget, such as usage MOSAIC can't price.
    error: str | None = None


class GateState(Entity):
    """What MOSAIC last wrote to, or found on, one gateway's blocked list."""

    entity_type: Literal["budgetGateState"] = "budgetGateState"
    gateway_id: str
    value: str | None = None
    synced_at: datetime | None = None
    error: str | None = None
    error_at: datetime | None = None
    # Blocked cost centers the list had no room for.
    left_out: list[str] = Field(default_factory=list)


def _endpoint(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip().rstrip("/")
    parts = urlsplit(candidate)
    host = (parts.hostname or "").casefold()
    if (
        parts.scheme != "https"
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or not host.endswith(EMAIL_HOST_SUFFIXES)
        or host in {suffix.lstrip(".") for suffix in EMAIL_HOST_SUFFIXES}
    ):
        raise ValueError(
            "Use your Communication Services resource's endpoint, such as "
            "https://contoso-mosaic.communication.azure.com"
        )
    return f"https://{host}"


def _sender(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return email_address(value)


class EmailSettings(Entity):
    """How MOSAIC sends email, through Azure Communication Services, with its managed identity.

    Nothing secret is kept: MOSAIC signs in to Communication Services as itself.
    """

    entity_type: Literal["emailSettings"] = "emailSettings"
    enabled: bool = False
    endpoint: str | None = None
    sender: str | None = None
    updated_by: str | None = None
    last_test_at: datetime | None = None
    last_test_error: str | None = None

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.endpoint) and bool(self.sender)


class EmailSettingsUpdate(MosaicModel):
    enabled: bool = False
    endpoint: str | None = Field(default=None, max_length=300)
    sender: str | None = Field(default=None, max_length=254)

    _validate_endpoint = field_validator("endpoint")(_endpoint)
    _validate_sender = field_validator("sender")(_sender)

    @model_validator(mode="after")
    def _validate_ready(self) -> Self:
        if self.enabled and not (self.endpoint and self.sender):
            raise ValueError("Email needs a Communication Services endpoint and a sender address")
        return self


class EmailSettingsView(MosaicModel):
    enabled: bool = False
    endpoint: str | None = None
    sender: str | None = None
    ready: bool = False
    updated_at: datetime | None = None
    updated_by: str | None = None
    last_test_at: datetime | None = None
    last_test_error: str | None = None
    # What this deployment's own Communication Services resource is, when azd deployed one.
    suggested_endpoint: str | None = None
    suggested_sender: str | None = None


class EmailTest(MosaicModel):
    to: str = Field(max_length=254)

    _validate_to = field_validator("to")(email_address)


class EmailTestResult(MosaicModel):
    sent: bool
    to: str
    operation_id: str | None = None
    error: str | None = None


class BudgetGatewayView(MosaicModel):
    """Whether one managed gateway is refusing a blocked cost center's calls yet."""

    gateway_id: str
    name: str
    enforcing: bool
    synced_at: datetime | None = None
    error: str | None = None


class BudgetStatus(MosaicModel):
    level: BudgetLevel
    month: str
    month_to_date: float | None = None
    forecast: float | None = None
    # Spend so far, and the forecast, as a share of the amount: 0.5 is half the budget.
    used: float | None = None
    forecast_used: float | None = None
    through: datetime | None = None
    unpriced_tokens: int = 0
    crossed: list[int] = Field(default_factory=list)
    blocked: bool = False
    blocked_at: datetime | None = None
    unblocked_at: datetime | None = None
    unblock_reason: UnblockReason | None = None
    notifications: list[BudgetNotification] = Field(default_factory=list)
    evaluated_at: datetime | None = None
    error: str | None = None
    gateways: list[BudgetGatewayView] = Field(default_factory=list)


class BudgetView(MosaicModel):
    id: str
    scope: BudgetScope
    cost_center: CostCenterRef | None = None
    amount: float
    currency: Literal["USD"] = "USD"
    thresholds: list[int]
    recipients: list[str]
    notify_owners: bool
    # The cost center's owners, whom the budget emails too when notify_owners is on.
    owners: list[str] = Field(default_factory=list)
    action: BudgetAction
    updated_at: datetime
    updated_by: str | None = None
    status: BudgetStatus


class EmailReadiness(MosaicModel):
    enabled: bool
    ready: bool


class BudgetOverview(MosaicModel):
    month: str
    organization: BudgetView | None = None
    cost_centers: list[BudgetView] = Field(default_factory=list)
    # Cost centers no budget covers.
    unbudgeted: int = 0
    email: EmailReadiness
    # Whether this deployment prices usage at all. Without it, no budget can be judged.
    priced: bool = True
    notes: list[str] = Field(default_factory=list)


class PortalBudgetAlert(MosaicModel):
    """One of the caller's cost centers whose budget is near, at, or past its limit.

    A total for the cost center, which everyone's calls make up. Never anyone's own use.
    """

    cost_center: CostCenterRef
    level: Literal["warning", "exceeded", "blocked"]
    month: str
    used: float
    action: BudgetAction


def level_of(budget: Budget, state: BudgetState | None, month: str) -> BudgetLevel:
    if state is None or state.month != month:
        return "ok"
    if state.blocked:
        return "blocked"
    spent = state.month_to_date
    if spent is None:
        return "ok"
    if budget.reached(spent, 100):
        return "exceeded"
    if budget.reached(spent, min(budget.thresholds)):
        return "warning"
    return "ok"


def used(amount: float, value: float | None) -> float | None:
    return None if value is None or amount <= 0 else round(value / amount, 4)


def is_hot(budget: Budget, state: BudgetState | None, month: str) -> bool:
    """Whether a budget is close enough to what it does next to be checked every few minutes."""

    if state is None or state.month != month or state.month_to_date is None:
        return False
    share = state.month_to_date / budget.amount * 100
    if share < HOT_PERCENT:
        return False
    pending = [threshold for threshold in budget.thresholds if threshold not in state.crossed]
    return bool(pending) or (budget.blocks and not state.blocked)


@dataclass
class Evaluation:
    """What one check of one budget decided: its new state, and the emails that are now due."""

    state: BudgetState
    due: list[BudgetNotification] = field(default_factory=list)
    # Thresholds this check found reached for the first time this month.
    reached: list[int] = field(default_factory=list)
    blocked: bool = False
    unblocked: UnblockReason | None = None


def evaluate(
    budget: Budget,
    current: BudgetState,
    *,
    spent: float | None,
    forecast: float | None,
    through: datetime | None,
    unpriced_tokens: int,
    now: datetime,
    error: str | None = None,
) -> Evaluation:
    """Judge a budget against this month's spend so far. Pure, so a conditional write can retry it.

    ``spent`` is None when MOSAIC can't price this month's usage. Nothing then changes on its
    account: no threshold is reached and no block is lifted, except by the month ending or by an
    administrator's change that lifts it whatever the spend.
    """

    month = month_of(now)
    state = current.model_copy(deep=True)
    result = Evaluation(state=state)
    if state.month != month:
        if state.blocked:
            result.unblocked = "newMonth"
        state.month = month
        state.amount = None
        state.crossed = []
        state.notifications = []
        state.blocked = False
        state.blocked_at = None
        state.month_to_date = None
        state.forecast = None
    if state.blocked and not budget.blocks:
        result.unblocked = "blockingOff"
        state.blocked = False
    changed = state.amount is not None and state.amount != budget.amount
    state.crossed = [threshold for threshold in state.crossed if threshold in budget.thresholds]
    if changed and spent is not None:
        # A new amount re-arms the thresholds the spend no longer reaches, and lifts a block the
        # spend no longer reaches either. Only a change of amount does: spend that drops with a
        # corrected price doesn't, so a threshold is never emailed twice for the same budget.
        state.crossed = [
            threshold for threshold in state.crossed if budget.reached(spent, threshold)
        ]
        if state.blocked and not budget.reached(spent, 100):
            result.unblocked = "budgetRaised"
            state.blocked = False
    if result.unblocked is not None and not state.blocked:
        state.blocked_at = None
        state.unblocked_at = now
        state.unblock_reason = result.unblocked
        result.due.append(
            BudgetNotification(kind="unblocked", reason=result.unblocked, created_at=now)
        )
    if spent is not None:
        reached = [
            threshold
            for threshold in budget.thresholds
            if threshold not in state.crossed and budget.reached(spent, threshold)
        ]
        result.reached = reached
        for threshold in reached:
            state.crossed.append(threshold)
            # Thresholds reached at once, such as by a budget set below what's already spent,
            # send one email, for the highest. The others are recorded, never emailed later.
            highest = threshold == reached[-1]
            notice = BudgetNotification(
                kind="threshold",
                threshold=threshold,
                created_at=now,
                status="sending" if highest else "skipped",
                error=None if highest else f"Covered by the {reached[-1]}% email.",
            )
            if highest:
                result.due.append(notice)
            else:
                state.notifications.append(notice)
        if budget.blocks and not state.blocked and budget.reached(spent, 100):
            state.blocked = True
            state.blocked_at = now
            result.blocked = True
            result.due.append(
                BudgetNotification(kind="blocked", reason="budgetUsed", created_at=now)
            )
        state.month_to_date = spent
        state.forecast = forecast
        state.through = through
        state.unpriced_tokens = unpriced_tokens
    state.crossed = sorted(state.crossed)
    state.amount = budget.amount
    state.evaluated_at = now
    state.error = error
    for notice in state.notifications:
        if notice.status == "failed" and notice.attempts < NOTIFICATION_ATTEMPTS:
            # Communication Services refused it, so it's due again until it's had its attempts.
            notice.status = "sending"
            result.due.append(notice)
        elif notice.status == "sending" and now - notice.created_at > STUCK_AFTER:
            # A check stopped after claiming it, so whether it went is unknown. It isn't sent
            # again: an email missed is better than one sent twice.
            notice.status = "failed"
            notice.attempts = NOTIFICATION_ATTEMPTS
            notice.error = (
                "MOSAIC stopped before it knew whether this email went, so it isn't sent again."
            )
    state.notifications.extend(item for item in result.due if item not in state.notifications)
    return result
