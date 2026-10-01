"""Budgets: what administrators set, the check that judges them, and who it tells. See ADR 0023.

The check is a background loop in the API process, like the usage rollup. Every
``MOSAIC_BUDGET_INTERVAL_SECONDS`` it prices the month so far for every budget at once, from the
rollups and the price list, judges each budget, writes each managed gateway's blocked list, and
then sends the emails that are due. A budget close to a threshold it hasn't reached is checked
again every ``MOSAIC_BUDGET_FAST_INTERVAL_SECONDS``, a rollup that finishes wakes it, and so does
the start of each UTC month, which lifts every block.

Each budget's state is saved only over the version that was read, and the emails a check decides
are due are recorded in that same write before any is sent. So two checks can't both send one, and
a check that stops half way never sends one twice. A lease keeps two API instances from checking at
once. A check that can't price the month changes nothing on that account: it never lifts a block.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from uuid import NAMESPACE_URL, uuid4, uuid5

import structlog

from mosaic_api.budgets import (
    BUDGETS_BUSY,
    BUDGETS_SCOPE,
    SYSTEM_ACTOR,
    Budget,
    BudgetGatewayView,
    BudgetNotification,
    BudgetOverview,
    BudgetState,
    BudgetStatus,
    BudgetUpdate,
    BudgetView,
    EmailReadiness,
    EmailSettings,
    EmailSettingsUpdate,
    EmailSettingsView,
    EmailTest,
    EmailTestResult,
    Evaluation,
    GateState,
    NotificationStatus,
    PortalBudgetAlert,
    budget_id,
    budget_key,
    email_settings_id,
    evaluate,
    is_hot,
    level_of,
    month_of,
    next_month_start,
    parse_blocked_list,
    used,
)
from mosaic_api.cost_centers import CostCenterBook
from mosaic_api.domain import AuditEvent, Gateway, new_id, utc_now
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.email import EmailSender
from mosaic_api.repositories import BudgetRepository, CostCenterRepository, GatewayRepository
from mosaic_api.services.analytics import AnalyticsService
from mosaic_api.services.analytics.models import AnalyticsSpend, BudgetSpend
from mosaic_api.services.budget_email import budget_email, trial_email
from mosaic_api.services.budget_gate import BlockedListGate, ClientFactory, WriterFactory
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.directory import Actor
from mosaic_api.services.portal import PortalService

logger = structlog.get_logger()

LEASE_SECONDS = 300
CHECK_COOLDOWN = timedelta(minutes=1)
TEST_EMAIL_COOLDOWN = timedelta(seconds=30)
# How soon the loop tries again to judge a month that has just started, while another check may
# still hold the lease.
MONTH_CHANGE_RETRY_SECONDS = 5.0
CHECKS_OFF = (
    "Budget checks are off in this deployment (MOSAIC_BUDGETS_ENABLED), so MOSAIC doesn't judge "
    "budgets, email anyone, or block calls."
)
NO_PRICES = "This deployment doesn't price usage, so MOSAIC can't judge budgets."
NO_FIGURES = "MOSAIC couldn't price any of this month's usage yet."
READ_FAILED = "MOSAIC couldn't read this month's spend. It tries again at the next check."
EMAIL_OFF = "Email isn't set up in Settings."
NOBODY = "The budget has no one to email."
NO_SENDER = "This deployment can't send email."
BUDGET_GONE = "The budget was removed before this email went."
_SEVERITY = {"blocked": 0, "exceeded": 1, "warning": 2, "ok": 3}
_ALERT_LEVELS: dict[str, Literal["warning", "exceeded", "blocked"]] = {
    "warning": "warning",
    "exceeded": "exceeded",
    "blocked": "blocked",
}


class TooManyRequestsError(ConflictError):
    status_code = 429
    code = "budget_throttled"


@dataclass
class _Judged:
    budget: Budget
    evaluation: Evaluation


@dataclass
class BudgetCheck:
    """What one check did."""

    judged: int = 0
    blocked: list[str] = field(default_factory=list)
    unblocked: list[str] = field(default_factory=list)
    emails: int = 0
    gateways: list[GateState] = field(default_factory=list)


@dataclass
class _Judging:
    """What a check decided under the lease, and what it needs to act on it afterwards."""

    result: BudgetCheck
    judged: list[_Judged]
    email: EmailSettings | None
    book: CostCenterBook


def _spent(figures: AnalyticsSpend | None) -> float | None:
    """This month's priced spend, zero when there were no calls, or None when none is priced."""

    if figures is None:
        return None
    if figures.month_to_date is not None:
        return figures.month_to_date
    if figures.unpriced_tokens == 0 and figures.unpriced_items == 0:
        return 0.0
    return None


def _record(
    state: BudgetState,
    notice_id: str,
    *,
    status: NotificationStatus,
    error: str | None,
    recipients: int,
    attempted: bool,
    now: datetime,
) -> BudgetState:
    """The state with one claimed notification's outcome. A notification since dropped, by the
    month ending, is left dropped."""

    updated = state.model_copy(deep=True)
    for notice in updated.notifications:
        if notice.id != notice_id:
            continue
        notice.status = status
        notice.error = error
        notice.recipients = recipients
        if attempted:
            notice.attempts += 1
            notice.first_attempt_at = notice.first_attempt_at or now
        if status == "sent":
            notice.sent_at = now
    return updated


def _recorder(
    notice_id: str,
    *,
    status: NotificationStatus,
    error: str | None,
    recipients: int,
    attempted: bool,
    now: datetime,
) -> Callable[[BudgetState], BudgetState]:
    def change(state: BudgetState) -> BudgetState:
        return _record(
            state,
            notice_id,
            status=status,
            error=error,
            recipients=recipients,
            attempted=attempted,
            now=now,
        )

    return change


class BudgetService:
    def __init__(
        self,
        repository: BudgetRepository,
        *,
        cost_center_repository: CostCenterRepository,
        gateway_repository: GatewayRepository,
        gate: BlockedListGate,
        client_factory: ClientFactory,
        writer_factory: WriterFactory,
        analytics: AnalyticsService | None,
        email: EmailSender | None,
        portal: PortalService | None,
        tenant_id: str,
        priced: bool = True,
        interval_seconds: float = 900,
        fast_interval_seconds: float = 300,
        clock: Callable[[], datetime] = utc_now,
        owner_id: str | None = None,
        suggested_endpoint: str | None = None,
        suggested_sender: str | None = None,
        enabled: bool = True,
    ) -> None:
        self._repository = repository
        self._cost_centers = cost_center_repository
        self._gateways = gateway_repository
        self._gate = gate
        self._client_factory = client_factory
        self._writer_factory = writer_factory
        self._analytics = analytics
        self._email = email
        self._portal = portal
        self._tenant_id = tenant_id
        self._priced = priced
        # Off, budgets are kept but nothing judges them: no emails, and no blocks.
        self._enabled = enabled
        self._interval = timedelta(seconds=interval_seconds)
        self._fast_interval = timedelta(seconds=fast_interval_seconds)
        self._clock = clock
        self._owner = owner_id or f"budgets-{uuid4().hex}"
        self._suggested_endpoint = suggested_endpoint
        self._suggested_sender = suggested_sender
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._next_full: datetime | None = None
        self._full_requested = False
        self._pending: set[str] = set()
        self._hot = False
        self._last_check: datetime | None = None
        self._last_test: datetime | None = None

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="budgets")

    async def aclose(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    def rollups_changed(self) -> None:
        """New usage is rolled up, so every budget is worth judging again now."""

        self._full_requested = True
        self.wake()

    def _delay(self, now: datetime) -> float:
        if self._last_check is not None and month_of(self._last_check) != month_of(now):
            # The last check started in the month before, so judge the new month now.
            return MONTH_CHANGE_RETRY_SECONDS
        waits = [next_month_start(now) - now + timedelta(seconds=1)]
        if self._next_full is not None:
            waits.append(self._next_full - now)
        if self._hot or self._pending:
            waits.append(self._fast_interval)
        return max(1.0, min(wait.total_seconds() for wait in waits))

    async def _loop(self) -> None:
        while True:
            self._wake.clear()
            now = self._clock()
            full = (
                self._full_requested
                or self._next_full is None
                or now >= self._next_full
                or month_of(now) != month_of(self._last_check or now)
            )
            pending, self._pending = self._pending, set()
            try:
                done = await self.run_check(full=full, only=pending or None)
                if done is None:
                    self._pending |= pending
                elif full:
                    self._full_requested = False
                    self._next_full = now + self._interval
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("budget_check_failed")
                self._pending |= pending
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._delay(self._clock()))
            except TimeoutError:
                pass

    # -- the check --------------------------------------------------------------------------

    async def run_check(
        self, *, full: bool = True, only: Collection[str] | None = None
    ) -> BudgetCheck | None:
        """Judge budgets now: every one when ``full``, otherwise ``only`` and those close to a
        threshold. None when another check holds the lease.

        The lease covers only the judging, which reads and writes MOSAIC's own records, as the
        lease's contract asks. Each email is claimed while judging, so the sends that follow the
        lease can't be repeated by another check, and the gateways' lists correct themselves when
        two checks write them at once.
        """

        tenant_id = self._tenant_id
        try:
            await self._gateways.acquire_scope_lease(
                tenant_id, BUDGETS_SCOPE, self._owner, lease_seconds=LEASE_SECONDS
            )
        except ConflictError:
            logger.info("budget_check_skipped", reason="leased")
            return None
        try:
            judging = await self._judge_all(tenant_id, full=full, only=only)
        finally:
            try:
                await self._gateways.release_scope_lease(tenant_id, BUDGETS_SCOPE, self._owner)
            except Exception:
                logger.warning("budget_lease_release_failed")
        result = judging.result
        # Gateways first, so a block notice goes once the gateways have been told to refuse.
        if full or result.blocked or result.unblocked:
            synced = await self._gate.sync(
                tenant_id,
                self._client_factory,
                self._writer_factory,
                audit=self._repository.record_audit,
            )
            result.gateways = synced.states
        for item in judging.judged:
            result.emails += await self._send_due(
                item.budget, item.evaluation, judging.email, judging.book
            )
        return result

    async def _judge_all(
        self, tenant_id: str, *, full: bool, only: Collection[str] | None
    ) -> _Judging:
        now = self._clock()
        month = month_of(now)
        self._last_check = now
        book = await load_book(self._cost_centers, tenant_id)
        states = {
            state.budget_id: state for state in await self._repository.list_budget_states(tenant_id)
        }
        live: list[Budget] = []
        for budget in await self._repository.list_budgets(tenant_id):
            if budget.scope == "costCenter" and book.get(budget.cost_center_id) is None:
                await self._remove_orphan(budget)
                continue
            live.append(budget)
        if full:
            # A budget removed while a check judged it can leave a state behind.
            kept = {budget.id for budget in live}
            for gone in set(states) - kept:
                await self._repository.delete_budget_state(tenant_id, gone)
        wanted = set(only or ())
        targets = [
            budget
            for budget in live
            if full or budget.id in wanted or is_hot(budget, states.get(budget.id), month)
        ]
        spend, error = await self._spend(tenant_id, targets)
        email = await self._repository.get_email_settings(tenant_id)
        result = BudgetCheck(judged=len(targets))
        judged: list[_Judged] = []
        for budget in targets:
            figures = None
            if spend is not None:
                figures = (
                    spend.organization
                    if budget.scope == "organization"
                    else spend.cost_centers.get(budget.cost_center_id or "")
                )
            evaluation = await self._judge(budget, figures, error, now, book)
            states[budget.id] = evaluation.state
            judged.append(_Judged(budget, evaluation))
            if evaluation.blocked:
                result.blocked.append(budget.cost_center_id or "")
            if evaluation.unblocked is not None:
                result.unblocked.append(budget.cost_center_id or "")
        self._hot = any(is_hot(budget, states.get(budget.id), month) for budget in live)
        return _Judging(result=result, judged=judged, email=email, book=book)

    async def _spend(
        self, tenant_id: str, budgets: Sequence[Budget]
    ) -> tuple[BudgetSpend | None, str | None]:
        if not budgets:
            return None, None
        if not self._priced or self._analytics is None:
            return None, NO_PRICES
        try:
            spend = await self._analytics.budget_spend(
                tenant_id,
                [budget.cost_center_id for budget in budgets if budget.cost_center_id],
                organization=any(budget.scope == "organization" for budget in budgets),
            )
        except Exception:
            logger.exception("budget_spend_failed", tenant_id=tenant_id)
            return None, READ_FAILED
        if spend is None:
            return None, NO_PRICES
        return spend, None

    async def _judge(
        self,
        budget: Budget,
        figures: AnalyticsSpend | None,
        error: str | None,
        now: datetime,
        book: CostCenterBook,
    ) -> Evaluation:
        spent = _spent(figures)
        note = error if error is not None else (NO_FIGURES if spent is None else None)
        decided: list[Evaluation] = []

        def change(current: BudgetState) -> BudgetState:
            result = evaluate(
                budget,
                current.model_copy(update={"cost_center_id": budget.cost_center_id}),
                spent=spent,
                forecast=figures.forecast if figures is not None else None,
                through=figures.through if figures is not None else None,
                unpriced_tokens=figures.unpriced_tokens if figures is not None else 0,
                now=now,
                error=note,
            )
            decided[:] = [result]
            return result.state

        saved = await self._repository.update_budget_state(budget.tenant_id, budget.id, change)
        evaluation = decided[0]
        evaluation.state = saved
        await self._audit_transitions(budget, evaluation, book)
        return evaluation

    async def _audit_transitions(
        self, budget: Budget, evaluation: Evaluation, book: CostCenterBook
    ) -> None:
        state = evaluation.state
        details: dict[str, object] = {
            "costCenterId": budget.cost_center_id,
            "month": state.month,
            "monthToDate": state.month_to_date,
            "amount": budget.amount,
        }
        events: list[tuple[str, dict[str, object]]] = []
        if evaluation.reached:
            events.append(
                ("budget.thresholdReached", {**details, "thresholds": evaluation.reached})
            )
        if evaluation.blocked:
            events.append(("budget.blocked", details))
        if evaluation.unblocked is not None:
            events.append(("budget.unblocked", {**details, "reason": evaluation.unblocked}))
        for action, values in events:
            try:
                await self._repository.record_audit(
                    AuditEvent(
                        id=new_id("audit"),
                        tenant_id=budget.tenant_id,
                        action=action,
                        resource_type="budget",
                        resource_id=budget.id,
                        actor_object_id=SYSTEM_ACTOR,
                        details=values,
                    )
                )
            except Exception:
                logger.exception("budget_audit_failed", action=action, budget_id=budget.id)
        if evaluation.blocked or evaluation.unblocked:
            name = book.get(budget.cost_center_id)
            logger.info(
                "budget_blocked" if evaluation.blocked else "budget_unblocked",
                budget_id=budget.id,
                cost_center=name.code if name else None,
            )

    def _recipients(self, budget: Budget, book: CostCenterBook) -> list[str]:
        owners: list[str] = []
        if budget.scope == "costCenter" and budget.notify_owners:
            cost_center = book.get(budget.cost_center_id)
            owners = list(cost_center.owners) if cost_center else []
        found: dict[str, str] = {}
        for address in [*owners, *budget.recipients]:
            found.setdefault(address.casefold(), address)
        return list(found.values())

    async def _send_due(
        self,
        budget: Budget,
        evaluation: Evaluation,
        email: EmailSettings | None,
        book: CostCenterBook,
    ) -> int:
        """Send each email the check claimed, then record how each went. Returns how many went."""

        if not evaluation.due:
            return 0
        recipients = self._recipients(budget, book)
        still_there = await self._repository.get_budget(budget.tenant_id, budget.id) is not None
        cost_center = book.ref(budget.cost_center_id)
        sent = 0
        for notice in evaluation.due:
            now = self._clock()
            attempted = False
            status: NotificationStatus = "skipped"
            error: str | None
            if not still_there and notice.kind != "unblocked":
                error = BUDGET_GONE
            elif email is None or not email.ready or not email.endpoint or not email.sender:
                error = EMAIL_OFF
            elif not recipients:
                error = NOBODY
            elif self._email is None:
                error = NO_SENDER
            else:
                message = budget_email(notice, budget, evaluation.state, cost_center, recipients)
                outcome = await self._email.send(
                    email.endpoint,
                    email.sender,
                    message,
                    # The same notification is the same Communication Services operation, however
                    # many times it's tried.
                    operation_id=str(uuid5(NAMESPACE_URL, f"mosaic-budget-notice:{notice.id}")),
                )
                attempted = True
                status = "sent" if outcome.accepted else "failed"
                error = None if outcome.accepted else outcome.error
                sent += 1 if outcome.accepted else 0
            try:
                await self._repository.update_budget_state(
                    budget.tenant_id,
                    budget.id,
                    _recorder(
                        notice.id,
                        status=status,
                        error=error,
                        recipients=len(recipients),
                        attempted=attempted,
                        now=now,
                    ),
                )
            except Exception:
                logger.exception("budget_notice_not_recorded", budget_id=budget.id)
        return sent

    async def _remove_orphan(self, budget: Budget) -> None:
        """Remove the budget of a cost center that no longer exists. It has no grants left."""

        try:
            await self._repository.delete_budget(
                budget,
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=budget.tenant_id,
                    action="budget.deleted",
                    resource_type="budget",
                    resource_id=budget.id,
                    actor_object_id=SYSTEM_ACTOR,
                    details={"costCenterId": budget.cost_center_id, "reason": "costCenterDeleted"},
                ),
            )
            await self._repository.delete_budget_state(budget.tenant_id, budget.id)
        except Exception:
            logger.exception("budget_orphan_not_removed", budget_id=budget.id)

    async def _check_now(self, tenant_id: str, ids: Sequence[str]) -> None:
        """Judge budgets an administrator just changed, or leave them to the loop if it's busy."""

        if not self._enabled:
            return
        try:
            done = await self.run_check(full=False, only=ids)
        except Exception:
            logger.exception("budget_check_failed", budget_ids=list(ids))
            done = None
        if done is None:
            self._pending.update(ids)
            self.wake()

    # -- administrators ---------------------------------------------------------------------

    async def overview(self, actor: Actor) -> BudgetOverview:
        tenant_id = actor.tenant_id
        now = self._clock()
        month = month_of(now)
        book = await load_book(self._cost_centers, tenant_id)
        budgets = await self._repository.list_budgets(tenant_id)
        states = {
            state.budget_id: state for state in await self._repository.list_budget_states(tenant_id)
        }
        gates = await self._gate_views(tenant_id)
        email = await self._repository.get_email_settings(tenant_id)
        views = [
            self._view(budget, states.get(budget.id), book, month, gates)
            for budget in budgets
            if budget.scope == "organization" or book.get(budget.cost_center_id) is not None
        ]
        covered = {view.cost_center.id for view in views if view.cost_center is not None}
        notes: list[str] = []
        if not self._enabled:
            notes.append(CHECKS_OFF)
        if not self._priced:
            notes.append(NO_PRICES)
        if email is None or not email.ready:
            notes.append(
                "Email is off, so budgets record when they reach a threshold but email no one. "
                "Set it up in Settings."
            )
        left_out = {item for _, gate in gates if gate is not None for item in gate.left_out}
        if left_out:
            notes.append(
                f"{len(left_out)} blocked cost "
                + ("center doesn't" if len(left_out) == 1 else "centers don't")
                + " fit in the gateways' list of blocked cost centers, so the gateways still "
                "allow their calls."
            )
        return BudgetOverview(
            month=month,
            organization=next((view for view in views if view.scope == "organization"), None),
            cost_centers=sorted(
                (view for view in views if view.scope == "costCenter"),
                key=lambda view: (
                    _SEVERITY[view.status.level],
                    -(view.status.used or 0.0),
                    view.cost_center.name.casefold() if view.cost_center else "",
                ),
            ),
            unbudgeted=len([item for item in book.by_id if item not in covered]),
            email=EmailReadiness(
                enabled=bool(email and email.enabled), ready=bool(email and email.ready)
            ),
            priced=self._priced,
            notes=notes,
        )

    async def _gate_views(self, tenant_id: str) -> list[tuple[Gateway, GateState | None]]:
        states = {
            state.gateway_id: state
            for state in await self._repository.list_gate_states(tenant_id)
        }
        return [
            (gateway, states.get(gateway.id)) for gateway in await self._gate.gateways(tenant_id)
        ]

    def _view(
        self,
        budget: Budget,
        state: BudgetState | None,
        book: CostCenterBook,
        month: str,
        gates: Sequence[tuple[Gateway, GateState | None]],
    ) -> BudgetView:
        current = state if state is not None and state.month == month else None
        status = BudgetStatus(level=level_of(budget, state, month), month=month)
        if current is not None:
            status = status.model_copy(
                update={
                    "month_to_date": current.month_to_date,
                    "forecast": current.forecast,
                    "used": used(budget.amount, current.month_to_date),
                    "forecast_used": used(budget.amount, current.forecast),
                    "through": current.through,
                    "unpriced_tokens": current.unpriced_tokens,
                    "crossed": current.crossed,
                    "blocked": current.blocked,
                    "blocked_at": current.blocked_at,
                    "unblocked_at": current.unblocked_at,
                    "unblock_reason": current.unblock_reason,
                    "notifications": current.notifications,
                    "evaluated_at": current.evaluated_at,
                    "error": current.error,
                }
            )
        elif state is not None:
            status = status.model_copy(update={"evaluated_at": state.evaluated_at})
        if budget.blocks and budget.cost_center_id:
            key = budget_key(budget.cost_center_id)
            status.gateways = [
                BudgetGatewayView(
                    gateway_id=gateway.id,
                    name=gateway.name,
                    enforcing=key in (parse_blocked_list(gate.value if gate else None) or set()),
                    synced_at=gate.synced_at if gate else None,
                    error=gate.error if gate else None,
                )
                for gateway, gate in gates
            ]
        cost_center = book.get(budget.cost_center_id)
        return BudgetView(
            id=budget.id,
            scope=budget.scope,
            cost_center=cost_center.ref() if cost_center else None,
            amount=budget.amount,
            thresholds=budget.thresholds,
            recipients=budget.recipients,
            notify_owners=budget.notify_owners,
            owners=list(cost_center.owners) if cost_center and budget.notify_owners else [],
            action=budget.action,
            updated_at=budget.updated_at,
            updated_by=budget.updated_by,
            status=status,
        )

    async def get_budget(self, actor: Actor, cost_center_id: str | None) -> BudgetView | None:
        tenant_id = actor.tenant_id
        book = await load_book(self._cost_centers, tenant_id)
        if cost_center_id is not None and book.get(cost_center_id) is None:
            raise NotFoundError("Cost center not found", details={"id": cost_center_id})
        budget = await self._repository.get_budget(tenant_id, budget_id(tenant_id, cost_center_id))
        if budget is None:
            return None
        state = await self._repository.get_budget_state(tenant_id, budget.id)
        gates = await self._gate_views(tenant_id) if budget.blocks else []
        return self._view(budget, state, book, month_of(self._clock()), gates)

    async def set_budget(
        self, actor: Actor, cost_center_id: str | None, request: BudgetUpdate
    ) -> BudgetView:
        tenant_id = actor.tenant_id
        book = await load_book(self._cost_centers, tenant_id)
        if cost_center_id is not None and book.get(cost_center_id) is None:
            raise NotFoundError("Cost center not found", details={"id": cost_center_id})
        if cost_center_id is None and request.action == "block":
            raise ValidationError(
                "The organization's budget only warns. Set blocking on each cost center's budget.",
                details={"field": "action"},
            )
        identifier = budget_id(tenant_id, cost_center_id)
        current = await self._repository.get_budget(tenant_id, identifier)
        now = self._clock()
        budget = Budget(
            id=identifier,
            tenant_id=tenant_id,
            scope="organization" if cost_center_id is None else "costCenter",
            cost_center_id=cost_center_id,
            amount=request.amount,
            thresholds=request.thresholds,
            recipients=request.recipients,
            notify_owners=request.notify_owners and cost_center_id is not None,
            action=request.action,
            created_at=current.created_at if current else now,
            updated_at=now,
            updated_by=actor.object_id,
            etag=current.etag if current else None,
        )
        await self._repository.save_budget(
            budget,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=tenant_id,
                action="budget.updated" if current else "budget.created",
                resource_type="budget",
                resource_id=identifier,
                actor_object_id=actor.object_id,
                details={
                    "costCenterId": cost_center_id,
                    "amount": budget.amount,
                    "previousAmount": current.amount if current else None,
                    "thresholds": budget.thresholds,
                    "action": budget.action,
                    "recipients": len(budget.recipients),
                    "notifyOwners": budget.notify_owners,
                },
            ),
        )
        # A raised budget lifts its block now, not at the next check.
        await self._check_now(tenant_id, [identifier])
        found = await self.get_budget(actor, cost_center_id)
        if found is None:
            raise ConflictError("The budget was removed while it was saved. Try again.")
        return found

    async def delete_budget(self, actor: Actor, cost_center_id: str | None) -> None:
        tenant_id = actor.tenant_id
        identifier = budget_id(tenant_id, cost_center_id)
        budget = await self._repository.get_budget(tenant_id, identifier)
        if budget is None:
            raise NotFoundError("No budget is set", details={"costCenterId": cost_center_id})
        state = await self._repository.get_budget_state(tenant_id, identifier)
        await self._repository.delete_budget(
            budget,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=tenant_id,
                action="budget.deleted",
                resource_type="budget",
                resource_id=identifier,
                actor_object_id=actor.object_id,
                details={"costCenterId": cost_center_id, "amount": budget.amount},
            ),
        )
        if state is not None and state.blocked and state.month == month_of(self._clock()):
            await self._lift_removed(budget)
        await self._repository.delete_budget_state(tenant_id, identifier)

    async def _lift_removed(self, budget: Budget) -> None:
        """Take a removed budget's block off the gateways now, and tell its recipients."""

        tenant_id = budget.tenant_id
        now = self._clock()
        notice = BudgetNotification(kind="unblocked", reason="budgetRemoved", created_at=now)

        def lift(state: BudgetState) -> BudgetState:
            if not state.blocked:
                raise ConflictError("Already lifted")
            return state.model_copy(
                update={
                    "blocked": False,
                    "blocked_at": None,
                    "unblocked_at": now,
                    "unblock_reason": "budgetRemoved",
                    "notifications": [*state.notifications, notice],
                }
            )

        try:
            saved = await self._repository.update_budget_state(tenant_id, budget.id, lift)
        except ConflictError:
            return
        book = await load_book(self._cost_centers, tenant_id)
        await self._gate.sync(
            tenant_id,
            self._client_factory,
            self._writer_factory,
            audit=self._repository.record_audit,
        )
        await self._send_due(
            budget,
            Evaluation(state=saved, due=[notice], unblocked="budgetRemoved"),
            await self._repository.get_email_settings(tenant_id),
            book,
        )

    async def check_now(self, actor: Actor) -> BudgetOverview:
        """Judge every budget now, at most once a minute."""

        if not self._enabled:
            raise ConflictError(CHECKS_OFF, details={"reason": "budgetChecksOff"})
        now = self._clock()
        if self._last_check and now - self._last_check < CHECK_COOLDOWN:
            raise TooManyRequestsError("Budgets were checked less than a minute ago")
        done = await self.run_check(full=True)
        if done is None:
            raise ConflictError(BUDGETS_BUSY)
        return await self.overview(actor)

    # -- email ------------------------------------------------------------------------------

    def _email_view(self, settings: EmailSettings | None) -> EmailSettingsView:
        if settings is None:
            return EmailSettingsView(
                suggested_endpoint=self._suggested_endpoint,
                suggested_sender=self._suggested_sender,
            )
        return EmailSettingsView(
            enabled=settings.enabled,
            endpoint=settings.endpoint,
            sender=settings.sender,
            ready=settings.ready,
            updated_at=settings.updated_at,
            updated_by=settings.updated_by,
            last_test_at=settings.last_test_at,
            last_test_error=settings.last_test_error,
            suggested_endpoint=self._suggested_endpoint,
            suggested_sender=self._suggested_sender,
        )

    async def email_settings(self, actor: Actor) -> EmailSettingsView:
        return self._email_view(await self._repository.get_email_settings(actor.tenant_id))

    async def save_email_settings(
        self, actor: Actor, request: EmailSettingsUpdate
    ) -> EmailSettingsView:
        tenant_id = actor.tenant_id
        current = await self._repository.get_email_settings(tenant_id)
        now = self._clock()
        settings = EmailSettings(
            id=email_settings_id(tenant_id),
            tenant_id=tenant_id,
            enabled=request.enabled,
            endpoint=request.endpoint,
            sender=request.sender,
            updated_by=actor.object_id,
            created_at=current.created_at if current else now,
            updated_at=now,
            last_test_at=current.last_test_at if current else None,
            last_test_error=current.last_test_error if current else None,
            etag=current.etag if current else None,
        )
        saved = await self._repository.save_email_settings(
            settings,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=tenant_id,
                action="email.settingsUpdated",
                resource_type="emailSettings",
                resource_id=settings.id,
                actor_object_id=actor.object_id,
                details={
                    "enabled": settings.enabled,
                    "endpoint": settings.endpoint,
                    "previouslyEnabled": bool(current and current.enabled),
                },
            ),
        )
        return self._email_view(saved)

    async def send_test_email(self, actor: Actor, request: EmailTest) -> EmailTestResult:
        tenant_id = actor.tenant_id
        settings = await self._repository.get_email_settings(tenant_id)
        if settings is None or not settings.endpoint or not settings.sender:
            raise ConflictError(
                "Save a Communication Services endpoint and a sender address before sending a "
                "test email.",
                details={"reason": "notConfigured"},
            )
        if self._email is None:
            raise ConflictError(NO_SENDER, details={"reason": "noSender"})
        now = self._clock()
        if self._last_test and now - self._last_test < TEST_EMAIL_COOLDOWN:
            raise TooManyRequestsError("A test email went less than 30 seconds ago")
        self._last_test = now
        outcome = await self._email.send(
            settings.endpoint, settings.sender, trial_email(request.to), operation_id=str(uuid4())
        )
        await self._repository.record_audit(
            AuditEvent(
                id=new_id("audit"),
                tenant_id=tenant_id,
                action="email.testSent" if outcome.accepted else "email.testFailed",
                resource_type="emailSettings",
                resource_id=settings.id,
                actor_object_id=actor.object_id,
                details={"status": outcome.status_code, "error": outcome.error},
            )
        )
        try:
            await self._repository.save_email_settings(
                settings.model_copy(
                    update={"last_test_at": now, "last_test_error": outcome.error}
                ),
                AuditEvent(
                    id=new_id("audit"),
                    tenant_id=tenant_id,
                    action="email.testRecorded",
                    resource_type="emailSettings",
                    resource_id=settings.id,
                    actor_object_id=actor.object_id,
                    details={"accepted": outcome.accepted},
                ),
            )
        except ConflictError:
            logger.info("email_test_not_recorded", reason="settingsChanged")
        return EmailTestResult(
            sent=outcome.accepted,
            to=request.to,
            operation_id=outcome.operation_id,
            error=outcome.error,
        )

    # -- the portal -------------------------------------------------------------------------

    async def alerts_for_caller(self, actor: Actor) -> list[PortalBudgetAlert]:
        """The caller's cost centers whose budgets are near, at, or past their limit.

        Only cost centers the caller holds an enabled grant under, and only the cost center's
        total against its budget: never who else called, or how much any one person spent.
        """

        if self._portal is None:
            return []
        held = await self._portal.my_entitlements(actor, include_disabled=True)
        mine = {
            item.entitlement.cost_center_id
            for item in held
            if item.entitlement.enabled and item.entitlement.revocation is None
        }
        if not mine:
            return []
        tenant_id = actor.tenant_id
        month = month_of(self._clock())
        book = await load_book(self._cost_centers, tenant_id)
        budgets = {
            budget.cost_center_id: budget
            for budget in await self._repository.list_budgets(tenant_id)
            if budget.cost_center_id in mine
        }
        states = {
            state.budget_id: state for state in await self._repository.list_budget_states(tenant_id)
        }
        alerts: list[PortalBudgetAlert] = []
        for cost_center_id, budget in budgets.items():
            state = states.get(budget.id)
            level = _ALERT_LEVELS.get(level_of(budget, state, month))
            ref = book.ref(cost_center_id)
            if level is None or ref is None or state is None:
                continue
            alerts.append(
                PortalBudgetAlert(
                    cost_center=ref,
                    level=level,
                    month=month,
                    used=used(budget.amount, state.month_to_date) or 0.0,
                    action=budget.action,
                )
            )
        return sorted(alerts, key=lambda item: (_SEVERITY[item.level], item.cost_center.name))
