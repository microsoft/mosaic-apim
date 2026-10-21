"""Budget compatibility transfers ownership and preserves existing notice identities."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.budget_notifications import budget_notice_operation_id
from mosaic_api.budgets import (
    Budget,
    BudgetNotification,
    BudgetState,
    EmailSettings,
    budget_id,
    budget_state_id,
    email_settings_id,
)
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.email import EmailMessage, EmailSendResult
from mosaic_api.repositories.base import BudgetRepository
from mosaic_api.repositories.cosmos_budgets import CosmosBudgetRepository
from mosaic_api.repositories.memory_budgets import InMemoryBudgetRepository
from mosaic_api.repositories.memory_cost_centers import InMemoryCostCenterRepository
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.analytics.models import AnalyticsSpend, BudgetSpend
from mosaic_api.services.budget_gate import GateSync
from mosaic_api.services.budget_notification_bridge import BudgetNotificationBridge
from mosaic_api.services.budgets import BudgetService
from mosaic_api.services.cost_centers import load_book
from mosaic_api.services.directory import Actor
from mosaic_api.services.notifications import NotificationDispatchResult, NotificationService
from test_notification_engine import Authorizer, Dispatcher, Resolver
from test_notification_repository import Clock, NotificationContainer
from test_notification_repository import store as store


def audit(identity: str) -> AuditEvent:
    return AuditEvent(
        id=f"audit_{identity}",
        tenant_id="tenant",
        action="test",
        resource_type="budget",
        resource_id="budget",
        actor_object_id="admin",
    )


class SourceContainer(NotificationContainer):
    async def replace_item(
        self, *, item: str, body: dict[str, Any], etag: str, match_condition: Any
    ) -> dict[str, Any]:
        current = self.items.get((body["tenantId"], item))
        if current is None:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        if current["_etag"] != etag:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412)
        self.version += 1
        self.items[body["tenantId"], item] = {**body, "_etag": str(self.version)}
        return self.items[body["tenantId"], item]

    async def delete_item(self, *, item: str, partition_key: str, **conditions: Any) -> None:
        current = self.items.get((partition_key, item))
        if current is not None and conditions.get("etag", current["_etag"]) != current["_etag"]:
            raise exceptions.CosmosAccessConditionFailedError(status_code=412)
        await super().delete_item(item=item, partition_key=partition_key)


class SourceCosmos:
    def __init__(self) -> None:
        self.containers = {name: SourceContainer() for name in ("desired", "audit", "usage")}

    def get_database_client(self, _: str) -> "SourceCosmos":
        return self

    def get_container_client(self, name: str) -> SourceContainer:
        return self.containers[name]


class Email:
    def __init__(self) -> None:
        self.sent: list[tuple[EmailMessage, str]] = []
        self.status = 202
        self.unknown = False

    async def send(
        self, sender_mailbox_id: str, message: EmailMessage, *, operation_id: str
    ) -> EmailSendResult:
        self.sent.append((message, operation_id))
        return EmailSendResult(
            self.status == 202, operation_id, self.status, outcome_unknown=self.unknown
        )

    async def close(self) -> None:
        pass

    async def check_access(self):
        from mosaic_api.integrations.email import EmailAccessResult

        return EmailAccessResult(True)


class Gateways:
    async def acquire_scope_lease(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def release_scope_lease(self, *args: Any, **kwargs: Any) -> None:
        pass


class Gate:
    def __init__(self) -> None:
        self.calls = 0

    async def sync(self, *args: Any, **kwargs: Any) -> GateSync:
        self.calls += 1
        return GateSync(value="|")


class Migration:
    def __init__(
        self, budgets: BudgetRepository, notifications: NotificationPersistence, clock: Clock
    ) -> None:
        self.budgets, self.notifications, self.clock = budgets, notifications, clock
        self.centers = InMemoryCostCenterRepository()
        self.email, self.gate = Email(), Gate()
        self.fallback = Dispatcher(NotificationDispatchResult("permanentFailure"))
        self.bridge = BudgetNotificationBridge(
            budgets,
            notifications,
            cost_center_repository=self.centers,
            sender=self.email,
            fallback=self.fallback,
            tenant_id="tenant",
            clock=clock,
        )
        self.budget = Budget(
            id=budget_id("tenant", "center"),
            tenant_id="tenant",
            scope="costCenter",
            cost_center_id="center",
            amount=100,
            thresholds=[80],
            action="block",
            recipients=["owner@example.com", "finance@example.com"],
            notify_owners=True,
        )
        self.spend = 85.0
        self.service = BudgetService(
            budgets,
            cost_center_repository=self.centers,
            gateway_repository=cast(Any, Gateways()),
            gate=cast(Any, self.gate),
            client_factory=lambda _: cast(Any, None),
            writer_factory=lambda _: cast(Any, None),
            analytics=cast(Any, SimpleNamespace(budget_spend=self.figures)),
            email=self.email,
            portal=None,
            tenant_id="tenant",
            clock=clock,
            notification_bridge=self.bridge,
        )
        self.engine = NotificationService(
            notifications,
            audience_resolver=Resolver([]),
            delivery_authorizer=Authorizer(),
            legacy_budget_authorizer=self.bridge,
            dispatchers={"email": self.bridge},
            clock=clock,
            hosting_verified=True,
        )

    async def figures(self, *_: Any, **__: Any) -> BudgetSpend:
        return BudgetSpend(
            cost_centers={
                "center": AnalyticsSpend(
                    month_start=self.clock.now.date().replace(day=1),
                    days_in_month=31,
                    days_elapsed=16,
                    month_to_date=self.spend,
                    forecast=100,
                    through=self.clock.now,
                )
            }
        )

    async def seed(self) -> None:
        await self.centers.create_cost_center(
            CostCenter(
                id="center",
                tenant_id="tenant",
                name="Research",
                code="RESEARCH",
                owners=["OWNER@example.com"],
            ),
            audit("center"),
        )
        await self.budgets.save_budget(self.budget, audit("budget"))
        self.budget = await self.budgets.get_budget("tenant", self.budget.id)
        await self.budgets.save_email_settings(
            EmailSettings(
                id=email_settings_id("tenant"),
                tenant_id="tenant",
                enabled=True,
                provider="graph",
                sender_mailbox_id="33333333-3333-4333-8333-333333333333",
                sender="sender@example.com",
            ),
            audit("settings"),
        )

    async def legacy(self, notices: list[BudgetNotification]) -> None:
        await self.budgets.update_budget_state(
            "tenant",
            self.budget.id,
            lambda state: state.model_copy(
                update={
                    "month": "2026-10",
                    "amount": 100.0,
                    "month_to_date": 85.0,
                    "cost_center_id": "center",
                    "crossed": [80],
                    "notifications": notices,
                }
            ),
        )


@pytest.fixture(params=["memory", "cosmos"])
async def migration(
    request: pytest.FixtureRequest, store: tuple[NotificationPersistence, Clock]
) -> Migration:
    notifications, clock = store
    budgets: BudgetRepository
    if request.param == "memory":
        budgets = InMemoryBudgetRepository()
    else:
        budgets = CosmosBudgetRepository(
            cast(CosmosClient, SourceCosmos()), "db", "desired", "audit", "usage"
        )
    result = Migration(budgets, notifications, clock)
    await result.seed()
    return result


async def test_owned_notice_uses_shared_worker_once_and_preserves_legacy_content(
    migration: Migration,
) -> None:
    m = migration
    await m.service.run_check()
    assert not m.email.sent
    state = await m.budgets.get_budget_state("tenant", m.budget.id)
    assert state and state.notifications[0].delivery_owner == "notificationPipeline"
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.ready
    assert await m.budgets.list_notification_outboxes("other") == []
    await asyncio.gather(m.engine.run_once("tenant"), m.bridge.reconcile())
    await m.service.run_check()
    await m.engine.run_once("tenant")
    [(message, operation)] = m.email.sent
    assert message.to == ["OWNER@example.com", "finance@example.com"]
    assert "Research" in message.subject and "80%" in message.subject
    assert operation == budget_notice_operation_id(state.notifications[0].id)
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "providerAccepted" and delivery.confirmed_delivered_at is None
    assert delivery.provider_operation_id == operation and delivery.max_attempts == 3
    state = await m.budgets.get_budget_state("tenant", m.budget.id)
    assert state and state.notifications[0].status == "sent"
    assert state.notifications[0].attempts == 1
    assert not m.fallback.sent


async def test_history_is_preserved_and_uncertain_legacy_notices_resume_automatically(
    migration: Migration,
) -> None:
    m = migration
    notices = [
        BudgetNotification(
            id=f"notice-{status}",
            kind="threshold",
            threshold=80,
            status=status,
            attempts=3 if status == "failed" else 1,
            created_at=m.clock.now,
        )
        for status in ("sent", "skipped", "sending", "failed")
    ]
    await m.legacy(notices)
    await m.service.run_check()
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == 1
    assert {item.state for item in await m.notifications.list_deliveries("tenant")} == {
        "providerAccepted",
        "suppressed",
        "permanentlyFailed",
    }
    accepted = await m.notifications.list_deliveries("tenant", state="providerAccepted")
    assert sorted(item.attempts for item in accepted) == [1, 2]


async def test_uncertain_legacy_handoff_retains_attempts_and_operation_id(
    migration: Migration,
) -> None:
    m = migration
    notice = BudgetNotification(
        id="refused",
        kind="threshold",
        threshold=80,
        status="sending",
        attempts=1,
        created_at=m.clock.now,
    )
    await m.legacy([notice])
    await m.bridge.reconcile()
    [queued] = await m.notifications.list_deliveries("tenant")
    assert queued.state == "queued" and queued.attempts == 1
    await m.engine.run_once("tenant")
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "providerAccepted" and delivery.attempts == 2
    assert m.email.sent[0][1] == budget_notice_operation_id(notice.id)


async def test_refusals_retry_only_three_times_and_are_not_reclaimed_by_budget_judge(
    migration: Migration,
) -> None:
    m = migration
    m.email.status = 503
    await m.service.run_check()
    for _ in range(5):
        await m.engine.run_once("tenant")
        await m.service.run_check()
        m.clock.now += timedelta(minutes=5)
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "permanentlyFailed" and delivery.attempts == 3
    assert len(m.email.sent) == 3 and len({operation for _, operation in m.email.sent}) == 1
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.delivery_state == "permanentlyFailed"


async def test_source_commit_replays_after_projection_crash_and_accounting_rollover_suppresses(
    migration: Migration,
) -> None:
    m = migration
    book = await load_book(m.centers, "tenant")
    figures = (await m.figures()).cost_centers["center"]
    await m.service._judge(m.budget, figures, None, m.clock.now, book)
    assert await m.notifications.list_deliveries("tenant") == []
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert not source.ready and source.notice.delivery_owner == "notificationPipeline"
    await m.bridge.release_pending(None)
    await m.bridge.reconcile()
    await m.budgets.delete_budget_state("tenant", m.budget.id)
    await m.engine.run_once("tenant")
    await m.bridge.reconcile()
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "suppressed" and not m.email.sent
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.notice.status == "skipped"


async def test_removed_budget_unblock_survives_state_deletion_and_sends_once(
    migration: Migration,
) -> None:
    m = migration
    m.spend = 110
    await m.service.run_check()
    await m.engine.run_once("tenant")
    initial = len(m.email.sent)
    assert initial == 2
    await m.service.delete_budget(Actor(tenant_id="tenant", object_id="admin"), "center")
    assert await m.budgets.get_budget_state("tenant", m.budget.id) is None
    await m.bridge.reconcile()
    await m.engine.run_once("tenant")
    await m.bridge.reconcile()
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == initial + 1
    assert "allowed again" in m.email.sent[-1][0].subject.lower()


async def test_source_receipt_repairs_shared_unknown_without_provider_resend(
    migration: Migration,
) -> None:
    m = migration
    await m.service.run_check()
    [claimed] = await m.notifications.claim_deliveries(
        "tenant", "crashed", now=m.clock.now, lease_seconds=60, limit=1
    )
    [source] = await m.budgets.list_notification_outboxes("tenant")
    await m.bridge._receipt(claimed, source, NotificationDispatchResult("providerAccepted"), 2)
    m.clock.now += timedelta(minutes=2)
    await m.bridge.reconcile()
    await m.engine.run_once("tenant")
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "providerAccepted" and not m.email.sent


async def test_removal_intent_recovers_delete_crash_but_cannot_announce_failed_delete(
    migration: Migration,
) -> None:
    m = migration
    m.spend = 110
    await m.service.run_check()
    await m.engine.run_once("tenant")
    before = len(m.email.sent)
    notice = await m.bridge.stage_removal(m.budget)
    assert notice is not None
    await m.bridge.release_pending(GateSync(value="|"))
    await m.bridge.reconcile()
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == before
    assert not await m.bridge.requires_gateway_sync()
    again = await m.bridge.stage_removal(m.budget)
    assert again and again.id == notice.id
    await m.budgets.delete_budget(m.budget, audit("delete-crash"))
    await m.budgets.delete_budget_state("tenant", m.budget.id)
    assert await m.bridge.requires_gateway_sync()
    await m.service.run_check(full=False)
    await m.engine.run_once("tenant")
    await m.service.run_check(full=False)
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == before + 1


async def test_shared_acceptance_repairs_failed_source_receipt(
    migration: Migration,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m = migration
    await m.service.run_check()
    original = m.budgets.save_notification_outbox

    async def unavailable(*args: Any) -> Any:
        raise ConflictError("Source receipt temporarily unavailable")

    monkeypatch.setattr(m.budgets, "save_notification_outbox", unavailable)
    await m.engine.run_once("tenant")
    monkeypatch.setattr(m.budgets, "save_notification_outbox", original)
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.delivery_state == "queued"
    await m.bridge.reconcile()
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.delivery_state == "providerAccepted" and source.notice.status == "sent"
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == 1


async def test_unknown_provider_outcomes_retry_with_the_budget_attempt_limit(
    migration: Migration,
) -> None:
    m = migration
    m.email.status, m.email.unknown = 504, True
    await m.service.run_check()
    await m.engine.run_once("tenant")
    m.clock.now += timedelta(hours=2)
    await m.service.run_check()
    await m.engine.run_once("tenant")
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "retrying" and len(m.email.sent) == 2
    m.clock.now += timedelta(hours=2)
    await m.engine.run_once("tenant")
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "unknownOutcome" and len(m.email.sent) == 3
    m.clock.now += timedelta(hours=2)
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == 3


@pytest.mark.parametrize(("status", "attempts"), [(429, 3), (403, 2), (202, 2)])
async def test_budget_receipts_preserve_prior_uncertainty_until_acceptance(
    migration: Migration,
    status: int,
    attempts: int,
) -> None:
    m = migration
    m.email.status, m.email.unknown = 504, True
    await m.service.run_check()
    await m.engine.run_once("tenant")
    [first] = await m.notifications.list_deliveries("tenant")
    assert first.state == "retrying" and first.has_unresolved_outcome
    m.email.status, m.email.unknown = status, False
    for _ in range(attempts - 1):
        m.clock.now += timedelta(hours=2)
        await m.engine.run_once("tenant")
    [delivery] = await m.notifications.list_deliveries("tenant")
    accepted = status == 202
    assert delivery.state == ("providerAccepted" if accepted else "unknownOutcome")
    assert delivery.has_unresolved_outcome is not accepted
    assert delivery.attempts == attempts and len(m.email.sent) == attempts
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.delivery_state == delivery.state
    assert source.notice.attempts == attempts
    if not accepted:
        assert source.notice.error and "earlier budget email attempt" in source.notice.error
    await m.bridge.reconcile()
    m.clock.now += timedelta(hours=2)
    await m.service.run_check()
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == attempts


async def test_legacy_switch_is_rechecked_before_provider_and_receipted_as_skipped(
    migration: Migration,
) -> None:
    m = migration
    await m.service.run_check()
    settings = await m.budgets.get_email_settings("tenant")
    assert settings is not None
    await m.budgets.save_email_settings(
        settings.model_copy(update={"enabled": False}), audit("disable")
    )
    await m.engine.run_once("tenant")
    await m.bridge.reconcile()
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.notice.status == "skipped" and not m.email.sent


async def test_disabled_budget_processing_defers_owned_notice_without_consuming_attempts(
    migration: Migration,
) -> None:
    m = migration
    await m.service.run_check()
    disabled = BudgetNotificationBridge(
        m.budgets,
        m.notifications,
        cost_center_repository=m.centers,
        sender=m.email,
        fallback=m.fallback,
        tenant_id="tenant",
        enabled=False,
        clock=m.clock,
    )
    engine = NotificationService(
        m.notifications,
        audience_resolver=Resolver([]),
        delivery_authorizer=Authorizer(),
        legacy_budget_authorizer=disabled,
        dispatchers={"email": disabled},
        clock=m.clock,
    )
    await disabled.reconcile()
    await engine.run_once("tenant")
    [delivery] = await m.notifications.list_deliveries("tenant")
    assert delivery.state == "retrying" and delivery.attempts == 0
    assert delivery.next_attempt_at and not m.email.sent
    [source] = await m.budgets.list_notification_outboxes("tenant")
    assert source.notice.delivery_owner == "notificationPipeline"
    assert source.notice.attempts == 0 and source.delivery_state == "queued"
    event = await m.notifications.get_event("tenant", delivery.event_ids[0])
    assert event is not None
    assert (await disabled.send(delivery, [event])).outcome == "deferred"
    assert not m.email.sent
    m.clock.now = delivery.next_attempt_at
    await m.engine.run_once("tenant")
    await m.engine.run_once("tenant")
    assert len(m.email.sent) == 1
    [accepted] = await m.notifications.list_deliveries("tenant")
    assert accepted.state == "providerAccepted" and accepted.attempts == 1


async def test_cosmos_state_and_outbox_rollback_together_on_batch_conflict() -> None:
    cosmos = SourceCosmos()
    budgets = CosmosBudgetRepository(cast(CosmosClient, cosmos), "db", "desired", "audit", "usage")
    notifications, clock = cast(Any, None), Clock()
    from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository

    notifications = InMemoryNotificationRepository(clock=clock)
    m = Migration(budgets, notifications, clock)
    await m.seed()
    book = await load_book(m.centers, "tenant")
    notice = BudgetNotification(kind="threshold", threshold=80, created_at=clock.now)
    notice.delivery_owner = "notificationPipeline"
    source = m.bridge.sources(
        m.budget,
        BudgetState(
            id=budget_state_id("tenant", m.budget.id),
            tenant_id="tenant",
            budget_id=m.budget.id,
            month="2026-10",
            notifications=[notice],
        ),
        book,
    )[0]
    container = cosmos.containers["usage"]
    original = container.execute_item_batch

    async def conflict(**kwargs: Any) -> Any:
        container.conflict_index = 1
        return await original(**kwargs)

    container.execute_item_batch = conflict
    with pytest.raises(ConflictError):
        await budgets.update_budget_state_with_outbox(
            "tenant",
            m.budget.id,
            lambda state: state.model_copy(update={"notifications": [notice]}),
            lambda state: [source],
        )
    assert await budgets.get_budget_state("tenant", m.budget.id) is None
    assert await budgets.list_notification_outboxes("tenant") == []
    assert cosmos.containers["desired"].items


@pytest.mark.parametrize("existing", [False, True])
async def test_cosmos_outbox_update_returns_its_own_committed_state_and_etag(
    existing: bool,
) -> None:
    cosmos = SourceCosmos()
    budgets = CosmosBudgetRepository(cast(CosmosClient, cosmos), "db", "desired", "audit", "usage")
    identifier = budget_id("tenant", "center")
    if existing:
        await budgets.update_budget_state("tenant", identifier, lambda state: state)
    container = cosmos.containers["usage"]
    original = container.execute_item_batch
    concurrent: list[BudgetState] = []

    async def write_between_commit_and_return(**kwargs: Any) -> Any:
        responses = await original(**kwargs)
        concurrent.append(
            await budgets.update_budget_state(
                "tenant",
                identifier,
                lambda state: state.model_copy(update={"blocked": True, "crossed": [80, 100]}),
            )
        )
        return responses

    container.execute_item_batch = write_between_commit_and_return
    saved = await budgets.update_budget_state_with_outbox(
        "tenant",
        identifier,
        lambda state: state.model_copy(update={"blocked": False, "crossed": [80]}),
        lambda state: [],
    )

    assert not saved.blocked and saved.crossed == [80]
    assert saved.etag and saved.etag != concurrent[0].etag
    assert not await budgets.delete_budget_state("tenant", identifier, etag=saved.etag)
    remaining = await budgets.get_budget_state("tenant", identifier)
    assert remaining == concurrent[0]


@pytest.mark.parametrize(
    "responses",
    [
        [],
        [{}],
        [{"eTag": None}],
        [{"eTag": ""}],
        [{"eTag": 1}],
    ],
)
async def test_cosmos_outbox_update_rejects_missing_or_invalid_committed_etag(
    responses: list[dict[str, Any]],
) -> None:
    cosmos = SourceCosmos()
    budgets = CosmosBudgetRepository(cast(CosmosClient, cosmos), "db", "desired", "audit", "usage")
    identifier = budget_id("tenant", "center")
    container = cosmos.containers["usage"]
    original = container.execute_item_batch

    async def omit_saved_version(**kwargs: Any) -> Any:
        await original(**kwargs)
        return responses

    container.execute_item_batch = omit_saved_version
    with pytest.raises(ConflictError, match="saved state version"):
        await budgets.update_budget_state_with_outbox(
            "tenant",
            identifier,
            lambda state: state.model_copy(update={"crossed": [80]}),
            lambda state: [],
        )

    assert len(container.batches) == 1
    saved = await budgets.get_budget_state("tenant", identifier)
    assert saved is not None and saved.crossed == [80] and saved.etag


async def test_budget_removal_preserves_recreated_budget_judged_before_lift_returns(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    notifications, clock = store
    cosmos = SourceCosmos()
    budgets = CosmosBudgetRepository(cast(CosmosClient, cosmos), "db", "desired", "audit", "usage")
    m = Migration(budgets, notifications, clock)
    await m.seed()
    m.spend = 110
    await m.service.run_check()
    blocked = await budgets.get_budget_state("tenant", m.budget.id)
    assert blocked is not None and blocked.blocked
    book = await load_book(m.centers, "tenant")
    figures = (await m.figures()).cost_centers["center"]
    container = cosmos.containers["usage"]
    original = container.execute_item_batch
    concurrent: list[BudgetState] = []

    async def recreate_between_commit_and_return(**kwargs: Any) -> Any:
        responses = await original(**kwargs)
        if await budgets.get_budget("tenant", m.budget.id) is not None:
            return responses
        container.execute_item_batch = original
        await budgets.save_budget(
            m.budget.model_copy(update={"etag": None, "amount": 50}), audit("recreated")
        )
        recreated = await budgets.get_budget("tenant", m.budget.id)
        assert recreated is not None
        judged = await m.service._judge(recreated, figures, None, clock.now, book)
        concurrent.append(judged.state)
        return responses

    container.execute_item_batch = recreate_between_commit_and_return
    await m.service.delete_budget(Actor(tenant_id="tenant", object_id="admin"), "center")

    [recreated_state] = concurrent
    assert recreated_state.blocked and recreated_state.amount == 50
    assert await budgets.get_budget_state("tenant", m.budget.id) == recreated_state
    recreated_budget = await budgets.get_budget("tenant", m.budget.id)
    assert recreated_budget is not None and recreated_budget.amount == 50
