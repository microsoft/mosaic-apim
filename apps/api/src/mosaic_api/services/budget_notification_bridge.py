"""Durable budget migration: the source outbox owns compatibility sends exclusively.

Budget state and send ownership commit together in usage-rollups. Projection into desired-state
is replayable, so a cross-container crash cannot create a second send. Existing sent/skipped
notices stay terminal; uncertain sends retry within the normal budget. Budget content and
correlation IDs are retained. General routing must not also email bridge-origin events.
"""

from collections.abc import Callable, Sequence
from datetime import datetime, timedelta

import structlog
from azure.cosmos.exceptions import CosmosHttpResponseError

from mosaic_api.budget_notifications import (
    BudgetNotificationOutbox,
    budget_notice_operation_id,
    budget_notice_outbox_id,
)
from mosaic_api.budgets import (
    NOTIFICATION_ATTEMPTS,
    Budget,
    BudgetNotification,
    BudgetState,
    budget_key,
    month_of,
    parse_blocked_list,
)
from mosaic_api.cost_centers import CostCenterBook
from mosaic_api.domain import deterministic_id, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.email import EmailSender
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationDelivery,
    NotificationDeliveryState,
    NotificationEvent,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
)
from mosaic_api.repositories.base import BudgetRepository, CostCenterRepository
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.budget_email import Enforcement, budget_email
from mosaic_api.services.budget_gate import GateSync
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.notification_events import build_notification_event, notification_record_id
from mosaic_api.services.notifications import (
    NotificationDeferredError,
    NotificationDispatcher,
    NotificationDispatchResult,
)

logger = structlog.get_logger()


class BudgetNotificationBridge:
    supports_idempotency = False

    def __init__(
        self,
        budget_repository: BudgetRepository,
        notification_repository: NotificationPersistence,
        *,
        cost_center_repository: CostCenterRepository,
        sender: EmailSender | None,
        fallback: NotificationDispatcher,
        tenant_id: str,
        enabled: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._budgets = budget_repository
        self._notifications = notification_repository
        self._cost_centers = cost_center_repository
        self._sender = sender
        self._fallback = fallback
        self._tenant_id = tenant_id
        self._enabled = enabled
        self._clock = clock

    @staticmethod
    def recipients(budget: Budget, book: CostCenterBook) -> list[str]:
        owners: list[str] = []
        center = book.get(budget.cost_center_id)
        if budget.notify_owners and center is not None:
            owners = list(center.owners)
        found: dict[str, str] = {}
        for address in [*owners, *budget.recipients]:
            found.setdefault(address.casefold(), address)
        return list(found.values())

    def sources(
        self,
        budget: Budget,
        state: BudgetState,
        book: CostCenterBook,
        notices: Sequence[BudgetNotification] | None = None,
    ) -> list[BudgetNotificationOutbox]:
        result: list[BudgetNotificationOutbox] = []
        for notice in notices if notices is not None else state.notifications:
            owner = notice.delivery_owner == "notificationPipeline"
            delivery_state: NotificationDeliveryState
            if notice.status == "sent":
                delivery_state = "providerAccepted"
            elif notice.status == "skipped":
                delivery_state = "suppressed"
            elif notice.status == "failed" and notice.attempts >= NOTIFICATION_ATTEMPTS:
                delivery_state = "permanentlyFailed"
            elif (
                owner
                and notice.status in {"sending", "failed"}
                and (notice.attempts < NOTIFICATION_ATTEMPTS)
            ):
                delivery_state = "queued"
            elif notice.status == "sending":
                delivery_state = "unknownOutcome"
            else:
                # A refused legacy notice remains with its existing owner until a judge
                # atomically transfers it. Creating an outbox now would race that transfer.
                continue
            result.append(
                BudgetNotificationOutbox(
                    id=budget_notice_outbox_id(budget.tenant_id, notice.id),
                    tenant_id=budget.tenant_id,
                    budget=budget.model_copy(deep=True),
                    notice=notice.model_copy(deep=True),
                    state_snapshot=state.model_copy(update={"notifications": []}, deep=True),
                    cost_center=book.ref(budget.cost_center_id),
                    recipients=self.recipients(budget, book),
                    ready=not owner or notice.status in {"sent", "skipped"},
                    delivery_state=delivery_state,
                    provider_accepted_at=(notice.sent_at or notice.created_at)
                    if notice.status == "sent"
                    else None,
                    created_at=notice.created_at,
                    updated_at=notice.created_at,
                )
            )
        return result

    async def requires_gateway_sync(self) -> bool:
        for item in await self._budgets.list_notification_outboxes(self._tenant_id):
            if not item.ready and item.notice.kind != "threshold" and item.budget.cost_center_id:
                if (
                    item.notice.reason == "budgetRemoved"
                    and await self._budgets.get_budget(self._tenant_id, item.budget.id) is not None
                ):
                    continue
                return True
        return False

    async def stage_removal(self, budget: Budget) -> BudgetNotification | None:
        """Journal deletion intent before the cross-container desired-budget delete."""
        current = await self._budgets.get_budget_state(budget.tenant_id, budget.id)
        if current is None or not current.blocked or current.month != month_of(self._clock()):
            return None
        notice = BudgetNotification(
            id=deterministic_id(
                "notice", budget.tenant_id, budget.id, "removed", budget.created_at.isoformat()
            ),
            kind="unblocked",
            reason="budgetRemoved",
            created_at=self._clock(),
            delivery_owner="notificationPipeline",
        )
        book = await load_book(self._cost_centers, budget.tenant_id)
        pending: list[BudgetNotificationOutbox] = []

        def journal(state: BudgetState) -> BudgetState:
            pending[:] = self.sources(budget, state, book, [notice]) if state.blocked else []
            return state

        await self._budgets.update_budget_state_with_outbox(
            budget.tenant_id, budget.id, journal, lambda _state: pending
        )
        if not pending:
            return None
        source = await self._budgets.get_notification_outbox(budget.tenant_id, pending[0].id)
        return source.notice if source else None

    async def release_pending(self, synced: GateSync | None) -> None:
        for item in await self._budgets.list_notification_outboxes(self._tenant_id):
            if item.ready:
                continue
            if (
                item.notice.reason == "budgetRemoved"
                and await self._budgets.get_budget(self._tenant_id, item.budget.id) is not None
            ):
                # A staged deletion that did not commit must never announce an unblock.
                continue
            if item.notice.kind != "threshold" and item.budget.cost_center_id and synced is None:
                continue
            updates: dict[str, object] = {"ready": True, "updated_at": self._clock()}
            if synced is not None and item.notice.kind != "threshold":
                key = budget_key(item.budget.cost_center_id or "")
                blocking = item.notice.kind == "blocked"
                done = sum(
                    state.error is None
                    and (key in (parse_blocked_list(state.value) or set())) == blocking
                    for state in synced.states
                )
                updates.update(enforcement_done=done, enforcement_total=len(synced.states))
            try:
                await self._budgets.save_notification_outbox(item.model_copy(update=updates))
            except ConflictError:
                # Another reconciler can only have advanced this same durable source row.
                current = await self._budgets.get_notification_outbox(item.tenant_id, item.id)
                if current is None or not current.ready:
                    raise

    def _projection(
        self, source: BudgetNotificationOutbox
    ) -> tuple[NotificationEvent, NotificationDelivery]:
        event_type = {
            "threshold": NotificationEventType.BUDGET_THRESHOLD,
            "blocked": NotificationEventType.BUDGET_BLOCKED,
            "unblocked": NotificationEventType.BUDGET_UNBLOCKED,
        }[source.notice.kind]
        event = build_notification_event(
            tenant_id=source.tenant_id,
            event_type=event_type,
            source_id=source.budget.id,
            source_revision=source.notice.id,
            occurred_at=source.notice.created_at,
            origin="budgetNotificationBridge",
            subject=NotificationSubject(
                budget_id=source.budget.id, cost_center_id=source.budget.cost_center_id
            ),
            audience=NotificationAudience(budget_id=source.budget.id, include_administrators=True),
            data=NotificationTemplateData(
                reason_code=source.notice.reason,
                threshold_percent=source.notice.threshold,
                period_label=source.state_snapshot.month,
                cost_usd=source.state_snapshot.month_to_date,
                coverage="incomplete" if source.state_snapshot.unpriced_tokens else "complete",
                unpriced_tokens=source.state_snapshot.unpriced_tokens,
            ),
            severity="warning" if source.notice.kind != "unblocked" else "information",
        )
        state = source.delivery_state
        if (
            state == "unknownOutcome"
            and source.notice.delivery_owner == "notificationPipeline"
            and source.notice.attempts < NOTIFICATION_ATTEMPTS
        ):
            state = "retrying"
        delivery = NotificationDelivery(
            id=notification_record_id(
                "budgetNotificationDelivery", source.tenant_id, source.notice.id
            ),
            tenant_id=source.tenant_id,
            event_ids=[event.id],
            channel="email",
            recipient_reference=source.id,
            legacy_budget_notice_id=source.notice.id,
            state=state,
            attempts=source.notice.attempts,
            max_attempts=NOTIFICATION_ATTEMPTS,
            provider_operation_id=budget_notice_operation_id(source.notice.id),
            provider_accepted_at=source.provider_accepted_at,
            has_unresolved_outcome=source.delivery_state == "unknownOutcome"
            or (source.notice.status == "sending" and source.notice.attempts > 0),
            suppression_reason=(source.notice.error or "The legacy budget notice was skipped.")[
                :1000
            ]
            if source.delivery_state == "suppressed"
            else None,
            last_error=source.notice.error[:1000] if source.notice.error else None,
            last_error_code=(
                "legacyUnclaimed"
                if source.delivery_state == "unknownOutcome"
                and source.notice.delivery_owner == "legacy"
                else None
            ),
            next_attempt_at=self._clock() if state == "retrying" else None,
            created_at=source.notice.created_at,
            updated_at=self._clock(),
        )
        return event, delivery

    async def reconcile(self) -> None:
        # Transfer uncertain legacy sends atomically; delivery is preferred to avoiding duplicates.
        book = await load_book(self._cost_centers, self._tenant_id) if self._enabled else None
        states = await self._budgets.list_budget_states(self._tenant_id) if self._enabled else []
        for state in states:
            budget = await self._budgets.get_budget(self._tenant_id, state.budget_id)
            if budget is None or book is None:
                continue
            uncertain = frozenset(
                notice.id
                for notice in state.notifications
                if notice.delivery_owner == "legacy" and notice.status == "sending"
            )
            if uncertain:
                selected_budget: Budget = budget
                selected_book: CostCenterBook = book

                def adopt(
                    current: BudgetState, identifiers: frozenset[str] = uncertain
                ) -> BudgetState:
                    updated = current.model_copy(deep=True)
                    for index, notice in enumerate(updated.notifications):
                        if (
                            notice.id in identifiers
                            and notice.delivery_owner == "legacy"
                            and (notice.status == "sending")
                        ):
                            updated.notifications[index] = notice.model_copy(
                                update={"delivery_owner": "notificationPipeline"}
                            )
                    return updated

                def project(
                    updated: BudgetState,
                    selected: Budget = selected_budget,
                    current_book: CostCenterBook = selected_book,
                    identifiers: frozenset[str] = uncertain,
                ) -> list[BudgetNotificationOutbox]:
                    return self.sources(
                        selected,
                        updated,
                        current_book,
                        [notice for notice in updated.notifications if notice.id in identifiers],
                    )

                state = await self._budgets.update_budget_state_with_outbox(
                    state.tenant_id,
                    state.budget_id,
                    adopt,
                    project,
                )
            for source in self.sources(budget, state, book):
                if source.notice.delivery_owner == "notificationPipeline":
                    continue
                if await self._budgets.get_notification_outbox(source.tenant_id, source.id) is None:
                    try:
                        await self._budgets.save_notification_outbox(source)
                    except ConflictError:
                        if (
                            await self._budgets.get_notification_outbox(source.tenant_id, source.id)
                            is None
                        ):
                            raise
        if self._enabled:
            await self.release_pending(None)
        for source in await self._budgets.list_notification_outboxes(self._tenant_id):
            if source.ready:
                event, projected = self._projection(source)
                current = await self._notifications.get_delivery(source.tenant_id, projected.id)
                if not self._enabled and (
                    current is None
                    or await self._notifications.get_event(source.tenant_id, event.id) is None
                ):
                    continue
                if (
                    source.notice.delivery_owner == "notificationPipeline"
                    and current is not None
                    and current.last_error_code != "legacyUnclaimed"
                ):
                    outcome = {
                        "providerAccepted": "providerAccepted",
                        "suppressed": "suppressed",
                        "unknownOutcome": "unknownOutcome",
                        "permanentlyFailed": "permanentFailure",
                    }.get(current.state)
                    if outcome is not None and source.delivery_state != current.state:
                        results = {
                            "providerAccepted": NotificationDispatchResult(
                                "providerAccepted", current.provider_operation_id
                            ),
                            "suppressed": NotificationDispatchResult(
                                "suppressed", safe_error=current.suppression_reason
                            ),
                            "unknownOutcome": NotificationDispatchResult(
                                "unknownOutcome", safe_error=current.last_error
                            ),
                            "permanentFailure": NotificationDispatchResult(
                                "permanentFailure", safe_error=current.last_error
                            ),
                        }
                        await self._receipt(
                            current, source, results[outcome], source.notice.recipients
                        )
                        source = (
                            await self._budgets.get_notification_outbox(source.tenant_id, source.id)
                            or source
                        )
                        event, projected = self._projection(source)
                await self._notifications.import_budget_delivery(event, projected)

    async def _eligibility(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> tuple[BudgetNotificationOutbox | None, list[str], str | None]:
        source = await self._budgets.get_notification_outbox(
            delivery.tenant_id, delivery.recipient_reference
        )
        if source is None or delivery.tenant_id != self._tenant_id:
            return None, [], "The durable budget notice does not exist."
        event, expected = self._projection(source)
        if (
            delivery.id != expected.id
            or [item.id for item in events] != [event.id]
            or delivery.legacy_budget_notice_id != source.notice.id
        ):
            return None, [], "This delivery does not identify its owned budget notice."
        if not source.ready or source.notice.delivery_owner != "notificationPipeline":
            return source, [], "The legacy sender retains this notice or gateway sync is pending."
        settings = await self._budgets.get_email_settings(delivery.tenant_id)
        if settings and settings.migration_required:
            raise NotificationDeferredError(
                self._clock() + timedelta(minutes=5),
                "Select a Graph sender mailbox in Notifications > Email; this notice will resume.",
            )
        if settings is None or not settings.ready:
            return source, [], "Budget email is disabled or not configured."
        budget = await self._budgets.get_budget(delivery.tenant_id, source.budget.id)
        if source.notice.reason == "budgetRemoved":
            if budget is not None:
                return source, [], "A replacement budget now owns this scope."
            budget = source.budget
        elif budget is None:
            return source, [], "The budget was removed before this notification was sent."
        else:
            state = await self._budgets.get_budget_state(delivery.tenant_id, budget.id)
            if state is None or not any(
                notice.id == source.notice.id and notice.delivery_owner == "notificationPipeline"
                for notice in state.notifications
            ):
                return source, [], "This budget notice belongs to a completed accounting period."
        recipients = self.recipients(
            budget, await load_book(self._cost_centers, delivery.tenant_id)
        )
        reason = None if recipients else "The budget has no configured recipients."
        return source, recipients, reason

    async def authorize(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> str | None:
        if not self._enabled:
            raise NotificationDeferredError(
                self._clock() + timedelta(minutes=5),
                "Budget processing is disabled; this owned notice remains pending for resume.",
            )
        _, _, reason = await self._eligibility(delivery, events)
        return reason

    async def _receipt(
        self,
        delivery: NotificationDelivery,
        source: BudgetNotificationOutbox,
        result: NotificationDispatchResult,
        recipient_count: int,
    ) -> None:
        outcome = (
            "unknownOutcome"
            if delivery.has_unresolved_outcome
            and result.outcome in {"retryableFailure", "permanentFailure", "suppressed"}
            else result.outcome
        )
        safe_error = (
            "An earlier budget email attempt may have been accepted. "
            f"Latest attempt: {result.safe_error or result.outcome}."
            if outcome != result.outcome
            else result.safe_error
        )
        for _ in range(3):
            current = await self._budgets.get_notification_outbox(source.tenant_id, source.id)
            if current is None:
                raise ConflictError("The durable budget outbox disappeared")
            if current.delivery_state == "providerAccepted" and outcome != "providerAccepted":
                return
            accepted_at = delivery.provider_accepted_at or self._clock()
            notice = current.notice.model_copy(
                update={
                    "status": "sent"
                    if outcome == "providerAccepted"
                    else ("skipped" if outcome == "suppressed" else "failed"),
                    "attempts": delivery.attempts,
                    "first_attempt_at": current.notice.first_attempt_at or self._clock(),
                    "sent_at": accepted_at if outcome == "providerAccepted" else None,
                    "recipients": recipient_count,
                    "error": safe_error,
                }
            )
            states: dict[str, NotificationDeliveryState] = {
                "providerAccepted": "providerAccepted",
                "unknownOutcome": "unknownOutcome",
                "retryableFailure": "retrying",
                "permanentFailure": "permanentlyFailed",
                "suppressed": "suppressed",
            }
            state = states[outcome]
            if outcome == "retryableFailure" and delivery.attempts >= NOTIFICATION_ATTEMPTS:
                state = "permanentlyFailed"
            try:
                await self._budgets.save_notification_outbox(
                    current.model_copy(
                        update={
                            "notice": notice,
                            "delivery_state": state,
                            "provider_accepted_at": accepted_at
                            if outcome == "providerAccepted"
                            else None,
                        }
                    )
                )
                break
            except ConflictError:
                continue
        else:
            raise ConflictError("The durable budget receipt kept changing")

        def record(current: BudgetState) -> BudgetState:
            updated = current.model_copy(deep=True)
            for index, original in enumerate(updated.notifications):
                if original.id == notice.id and original.delivery_owner == "notificationPipeline":
                    updated.notifications[index] = notice
                    return updated
            raise ConflictError("The budget accounting period has already completed")

        try:
            await self._budgets.update_budget_state(source.tenant_id, source.budget.id, record)
        except ConflictError:
            # The independent source receipt survives removed budgets and completed months.
            pass

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        if delivery.legacy_budget_notice_id is None:
            return await self._fallback.send(delivery, events)
        if not self._enabled:
            return NotificationDispatchResult(
                "deferred",
                next_attempt_at=self._clock() + timedelta(minutes=5),
                safe_error="Budget processing is disabled; this owned notice remains pending.",
            )
        source, recipients, reason = await self._eligibility(delivery, events)
        if reason or source is None:
            return NotificationDispatchResult("suppressed", safe_error=reason)
        if source.delivery_state == "providerAccepted":
            return NotificationDispatchResult(
                "providerAccepted", budget_notice_operation_id(source.notice.id)
            )
        settings = await self._budgets.get_email_settings(delivery.tenant_id)
        if (
            self._sender is None
            or settings is None
            or not settings.configured
            or not settings.sender_mailbox_id
        ):
            return NotificationDispatchResult(
                "permanentFailure", safe_error="This deployment cannot send budget email."
            )
        enforcement = (
            Enforcement(done=source.enforcement_done, total=source.enforcement_total)
            if source.enforcement_done is not None and source.enforcement_total is not None
            else None
        )
        outcome = await self._sender.send(
            settings.sender_mailbox_id,
            budget_email(
                source.notice,
                source.budget,
                source.state_snapshot,
                source.cost_center,
                recipients,
                enforcement,
            ),
            operation_id=budget_notice_operation_id(source.notice.id),
        )
        result = NotificationDispatchResult(
            "providerAccepted"
            if outcome.accepted
            else (
                "unknownOutcome"
                if outcome.outcome_unknown
                else ("retryableFailure" if outcome.retryable else "permanentFailure")
            ),
            provider_operation_id=outcome.operation_id,
            retry_after_seconds=outcome.retry_after_seconds,
            safe_error=None
            if outcome.accepted
            else (
                "Graph acceptance is uncertain; MOSAIC retries automatically."
                if outcome.outcome_unknown
                else f"Graph refused budget email (HTTP {outcome.status_code}). "
                "Run Check access in Notifications > Email."
            ),
            error_code=outcome.error_code,
        )
        try:
            await self._receipt(delivery, source, result, len(recipients))
        except (ConflictError, CosmosHttpResponseError) as error:
            # Provider acceptance must still reach the shared delivery log if source projection
            # is unavailable. An ambiguous double persistence failure stays unknown, not resent.
            logger.warning("budget_source_receipt_deferred", error_type=type(error).__name__)
        return result
