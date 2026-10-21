"""Offline end-to-end fan-out, provider outcomes, retries and self-scoped HTTP inboxes."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta

import httpx
import pytest
import structlog
from azure.cosmos.exceptions import CosmosHttpResponseError
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext, require_admin, require_portal_user
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import (
    ConflictError,
    DirectoryError,
    DirectoryForbiddenError,
    DomainError,
    NotFoundError,
    domain_error_handler,
)
from mosaic_api.notification_delivery_api import notification_delivery_router
from mosaic_api.notification_inbox_api import notifications_router, portal_notifications_router
from mosaic_api.notifications import (
    NotificationChannelSettings,
    NotificationDelivery,
    NotificationEvent,
    NotificationOutboxClaim,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.notifications import (
    NotificationDeferredError,
    NotificationDispatchResult,
    NotificationPartialAudienceError,
    NotificationService,
    NotificationTarget,
)
from structlog.testing import capture_logs
from test_notification_repository import Clock, event, seed_fanout
from test_notification_repository import store as store


class Resolver:
    def __init__(self, targets: Sequence[NotificationTarget]) -> None:
        self.targets = targets
        self.failure: DirectoryError | None = None

    async def resolve(self, _event: NotificationEvent) -> Sequence[NotificationTarget]:
        if self.failure:
            raise self.failure
        return self.targets


class Authorizer:
    def __init__(self) -> None:
        self.reason: str | None = None
        self.sending_reason: str | None = None

    async def authorize(
        self, delivery: NotificationDelivery, _events: Sequence[NotificationEvent]
    ) -> str | None:
        return self.sending_reason if delivery.state == "sending" else self.reason


class Dispatcher:
    supports_idempotency = True

    def __init__(self, result: NotificationDispatchResult) -> None:
        self.result = result
        self.sent: list[NotificationDelivery] = []
        self.timeout = False

    async def send(
        self, delivery: NotificationDelivery, _events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult:
        self.sent.append(delivery)
        if self.timeout:
            raise httpx.ReadTimeout("an upstream response was lost")
        return self.result


async def enable_email(repository: NotificationPersistence) -> None:
    settings = NotificationChannelSettings(
        id="notification-settings", tenant_id="tenant", email_enabled=True
    )
    await repository.save_channel_settings(
        settings,
        AuditEvent(
            id="audit-enable-email",
            tenant_id="tenant",
            action="notifications.enabled",
            resource_type="notificationChannelSettings",
            resource_id=settings.id,
            actor_object_id="administrator",
        ),
    )


def service(
    repository: NotificationPersistence,
    clock: Clock,
    *,
    resolver: Resolver | None = None,
    authorizer: Authorizer | None = None,
    dispatcher: Dispatcher | None = None,
    max_attempts: int = 2,
) -> NotificationService:
    return NotificationService(
        repository,
        audience_resolver=resolver
        or Resolver(
            [
                NotificationTarget("user", "inApp", "portal"),
                NotificationTarget("user", "email", "portal"),
            ]
        ),
        delivery_authorizer=authorizer or Authorizer(),
        dispatchers={"email": dispatcher} if dispatcher else {},
        clock=clock,
        hosting_verified=True,
        max_attempts=max_attempts,
    )


async def test_defaults_materialize_inbox_but_never_enable_external_delivery(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted", "operation"))
    engine = service(repository, clock, dispatcher=dispatcher)
    await engine.publish(event())
    await asyncio.gather(engine.run_once("tenant"), engine.run_once("tenant"))
    page = await engine.list_inbox("tenant", "user", "portal")
    assert len(page.items) == 1 and page.unread_count == 1
    deliveries = await repository.list_deliveries("tenant")
    assert {item.state for item in deliveries} == {"confirmedDelivered", "suppressed"}
    assert dispatcher.sent == []
    state = await repository.get_worker_state("tenant", "notifications")
    assert state and state.last_succeeded_at == clock.now and state.hosting_verified
    assert state.backlog == 0


async def test_multiple_routes_deduplicate_and_provider_acceptance_is_not_delivery(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted", "operation"))
    engine = service(
        repository,
        clock,
        resolver=Resolver(
            [
                NotificationTarget("user", "email", "portal", route_ids=["route-one"]),
                NotificationTarget("user", "email", "portal", route_ids=["route-two"]),
            ]
        ),
        dispatcher=dispatcher,
    )
    await engine.publish(event())
    await engine.run_once("tenant")
    await service(repository, clock, dispatcher=dispatcher).run_once("tenant")
    assert len(dispatcher.sent) == 1
    [delivery] = await repository.list_deliveries("tenant")
    assert delivery.state == "providerAccepted"
    assert delivery.provider_accepted_at == clock.now
    assert delivery.confirmed_delivered_at is None
    assert delivery.route_ids == ["route-one", "route-two"]
    [attempt] = await repository.list_delivery_attempts("tenant", delivery.id)
    assert attempt.outcome == "providerAccepted"
    assert attempt.finished_at == clock.now
    [health] = await repository.list_channel_health("tenant")
    assert health.last_provider_accepted_at == clock.now
    assert health.last_confirmed_delivered_at is None
    assert "unconfirmed" in health.checks[0].evidence[0]


async def test_send_claim_rechecks_edits_and_suppresses_before_provider(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)
    authorizer = Authorizer()
    authorizer.sending_reason = "The user disabled email before the send claim."
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    engine = service(repository, clock, authorizer=authorizer, dispatcher=dispatcher)
    await engine.publish(event())
    await engine.run_once("tenant")
    assert not dispatcher.sent
    [delivery] = await repository.list_deliveries("tenant", state="suppressed")
    assert "disabled email" in (delivery.suppression_reason or "")


async def test_retry_after_is_durable_and_retry_budget_is_bounded(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)
    dispatcher = Dispatcher(
        NotificationDispatchResult(
            "retryableFailure",
            retry_after_seconds=7200,
            error_code="throttled",
            safe_error="The provider asked MOSAIC to wait.",
        )
    )
    engine = service(repository, clock, dispatcher=dispatcher)
    await engine.publish(event())
    await engine.run_once("tenant")
    [first] = await repository.list_deliveries("tenant", state="retrying")
    assert first.next_attempt_at == clock.now + timedelta(hours=2)
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1
    clock.now += timedelta(hours=3)
    await service(repository, clock, dispatcher=dispatcher).run_once("tenant")
    [failed] = await repository.list_deliveries("tenant", state="permanentlyFailed")
    assert failed.attempts == 2
    assert len(await repository.list_delivery_attempts("tenant", failed.id)) == 2
    clock.now += timedelta(days=1)
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 2


async def test_ambiguous_provider_timeouts_retry_automatically_with_a_finite_limit(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    dispatcher.timeout = True
    engine = service(repository, clock, dispatcher=dispatcher)
    await engine.publish(event())
    await engine.run_once("tenant")
    [retrying] = await repository.list_deliveries("tenant", state="retrying")
    assert retrying.provider_operation_id and retrying.attempts == 1
    assert retrying.next_attempt_at and retrying.max_attempts == 2
    dispatcher.supports_idempotency = False
    clock.now += timedelta(days=1)
    await engine.run_once("tenant")
    [unknown] = await repository.list_deliveries("tenant", state="unknownOutcome")
    assert unknown.attempts == 2 and len(dispatcher.sent) == 2
    clock.now += timedelta(days=1)
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 2
    dispatcher.timeout = False
    await engine.retry_delivery("tenant", unknown.id, actor_object_id="admin")
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 3
    assert dispatcher.sent[0].provider_operation_id == dispatcher.sent[1].provider_operation_id


async def test_slow_authorization_cannot_send_under_an_expired_claim(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)

    class SlowAuthorizer(Authorizer):
        async def authorize(
            self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
        ) -> str | None:
            if delivery.state == "sending":
                clock.now += timedelta(minutes=10)
            return None

    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    engine = service(repository, clock, authorizer=SlowAuthorizer(), dispatcher=dispatcher)
    await engine.publish(event())
    with pytest.raises(ConflictError, match="lease"):
        await engine.run_once("tenant")
    assert dispatcher.sent == []
    await repository.claim_deliveries(
        "tenant", "replacement", now=clock.now, lease_seconds=60, limit=1
    )
    assert len(await repository.list_deliveries("tenant", state="retrying")) == 1


async def test_fanout_resolution_failure_checkpoints_retry_and_exhaustion(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    resolver = Resolver([])
    resolver.failure = DirectoryError("Do not leak an upstream payload")
    engine = service(repository, clock, resolver=resolver)
    source = event()
    await engine.publish(source)
    with pytest.raises(DirectoryError):
        await engine.run_once("tenant")
    stored = await repository.get_event("tenant", source.id)
    assert stored and stored.fanout_next_attempt_at and stored.fanout_failed_at is None
    assert not await repository.claim_events(
        "tenant", "other", now=clock.now, lease_seconds=60, limit=1
    )
    clock.now += timedelta(days=1)
    with pytest.raises(DirectoryError):
        await engine.run_once("tenant")
    stored = await repository.get_event("tenant", source.id)
    assert stored and stored.fanout_failed_at == clock.now
    clock.now += timedelta(days=1)
    assert not await repository.claim_events(
        "tenant", "other", now=clock.now, lease_seconds=60, limit=1
    )


async def test_partial_audience_commits_safe_inbox_and_replays_unresolved_admins_without_duplicates(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store

    class PartialResolver(Resolver):
        unavailable = True

        async def resolve(self, _event: NotificationEvent) -> Sequence[NotificationTarget]:
            if self.unavailable:
                raise NotificationPartialAudienceError(self.targets)
            return [*self.targets, NotificationTarget("administrator", "inApp", "admin")]

    resolver = PartialResolver([NotificationTarget("user", "inApp", "portal")])
    engine = service(repository, clock, resolver=resolver)
    source = event()
    await engine.publish(source)
    await engine.run_once("tenant")
    [item] = (await engine.list_inbox("tenant", "user", "portal")).items
    await engine.set_inbox_read("tenant", "user", "portal", item.id, read=True)
    stored = await repository.get_event("tenant", source.id)
    assert stored and stored.fanout_completed_at is None and stored.fanout_next_attempt_at
    assert not (await engine.list_inbox("tenant", "administrator", "admin")).items
    clock.now = stored.fanout_next_attempt_at
    resolver.unavailable = False
    await engine.run_once("tenant")
    [original] = (await engine.list_inbox("tenant", "user", "portal")).items
    assert original.id == item.id and original.read_at is not None
    assert len((await engine.list_inbox("tenant", "administrator", "admin")).items) == 1
    assert len(await repository.list_deliveries("tenant")) == 2
    stored = await repository.get_event("tenant", source.id)
    assert stored and stored.fanout_completed_at == clock.now


async def test_partial_source_does_not_starve_bounded_fanout_or_queued_private_delivery(
    store: tuple[NotificationPersistence, Clock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, clock = store
    await enable_email(repository)
    _, _, queued = await seed_fanout(repository, identity="private")

    class PartialResolver(Resolver):
        unavailable = True

        async def resolve(self, source: NotificationEvent) -> Sequence[NotificationTarget]:
            if source.source_id.startswith("partial"):
                targets = [NotificationTarget("requester", "inApp", "portal")]
                if self.unavailable:
                    raise NotificationPartialAudienceError(targets)
                return [*targets, NotificationTarget("administrator", "inApp", "admin")]
            return [NotificationTarget("independent", "inApp", "portal")]

    resolver = PartialResolver([])
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted", "private-operation"))
    engine = NotificationService(
        repository,
        audience_resolver=resolver,
        delivery_authorizer=Authorizer(),
        dispatchers={"email": dispatcher},
        clock=clock,
        batch_size=3,
        hosting_verified=True,
    )
    sources = [
        event(identity)
        for identity in ("partial-first", "independent-next", "partial-third", "beyond-batch")
    ]
    for source in sources:
        await engine.publish(source)
    with monkeypatch.context() as scoped_patch, capture_logs() as logs:
        # Earlier host tests can cache the module logger with production JSON processors.
        scoped_patch.setattr("mosaic_api.services.notifications.logger", structlog.get_logger())
        await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1 and dispatcher.sent[0].id == queued.id
    assert len([log for log in logs if log["event"] == "notification_partial_audience"]) == 2
    assert len((await engine.list_inbox("tenant", "requester", "portal")).items) == 2
    assert len((await engine.list_inbox("tenant", "independent", "portal")).items) == 1
    for source in (sources[0], sources[2]):
        stored = await repository.get_event("tenant", source.id)
        assert stored and stored.fanout_completed_at is None and stored.fanout_next_attempt_at
    independent = [
        await repository.get_event("tenant", source.id) for source in (sources[1], sources[3])
    ]
    assert all(source is not None for source in independent)
    assert sum(bool(source.fanout_completed_at) for source in independent if source) == 1
    assert all(source.fanout_next_attempt_at is None for source in independent if source)
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and "NotificationPartialAudienceError" in (worker.last_error or "")
    assert worker.last_succeeded_at is None and worker.backlog == 3

    # A tick before the retry instant can process other work, but not erase known degradation.
    await engine.run_once("tenant")
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error and worker.last_succeeded_at is None
    assert worker.backlog == 2
    resolver.unavailable = False
    clock.now += timedelta(hours=1)
    await engine.run_once("tenant")
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error is None and worker.last_succeeded_at == clock.now
    assert worker.backlog == 0 and len(dispatcher.sent) == 1
    assert len((await engine.list_inbox("tenant", "requester", "portal")).items) == 2
    assert len((await engine.list_inbox("tenant", "administrator", "admin")).items) == 2


async def test_partial_failure_checkpoint_storage_fault_is_not_swallowed(
    store: tuple[NotificationPersistence, Clock],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, clock = store
    await enable_email(repository)
    await seed_fanout(repository, identity="private")

    class PartialResolver(Resolver):
        async def resolve(self, source: NotificationEvent) -> Sequence[NotificationTarget]:
            raise NotificationPartialAudienceError(self.targets)

    async def unavailable(
        claim: NotificationOutboxClaim, *, error: str, next_attempt_at: object, permanent: bool
    ) -> None:
        raise CosmosHttpResponseError(status_code=503)

    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    engine = service(
        repository,
        clock,
        resolver=PartialResolver([NotificationTarget("requester", "inApp", "portal")]),
        dispatcher=dispatcher,
    )
    await engine.publish(event("partial"))
    monkeypatch.setattr(repository, "fail_fanout", unavailable)
    with pytest.raises(CosmosHttpResponseError):
        await engine.run_once("tenant")
    assert not dispatcher.sent


@pytest.mark.parametrize("failure", [DirectoryError, DirectoryForbiddenError])
async def test_optional_recipient_authorization_outage_preserves_other_targets_and_retries(
    store: tuple[NotificationPersistence, Clock],
    failure: type[DomainError],
) -> None:
    repository, clock = store
    await enable_email(repository)

    class OptionalAuthorizer(Authorizer):
        unavailable = True

        async def authorize(
            self, delivery: NotificationDelivery, _events: Sequence[NotificationEvent]
        ) -> str | None:
            if self.unavailable and delivery.channel == "email":
                raise failure("The optional mailbox lookup is unavailable.")
            return None

    authorizer = OptionalAuthorizer()
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    engine = service(
        repository,
        clock,
        authorizer=authorizer,
        dispatcher=dispatcher,
        resolver=Resolver(
            [
                NotificationTarget("user", "inApp", "portal"),
                NotificationTarget("user", "email", "portal"),
                NotificationTarget("administrator", "inApp", "admin"),
            ]
        ),
    )
    source = event()
    await engine.publish(source)
    await engine.run_once("tenant")
    assert len((await engine.list_inbox("tenant", "user", "portal")).items) == 1
    assert len((await engine.list_inbox("tenant", "administrator", "admin")).items) == 1
    assert not dispatcher.sent
    authorizer.unavailable = False
    clock.now += timedelta(hours=1)
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1
    assert len(await repository.list_deliveries("tenant")) == 3


async def test_partial_audience_exhaustion_remains_visible_in_dead_letters(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store

    class PartialResolver(Resolver):
        async def resolve(self, _event: NotificationEvent) -> Sequence[NotificationTarget]:
            raise NotificationPartialAudienceError(self.targets)

    engine = service(
        repository,
        clock,
        max_attempts=2,
        resolver=PartialResolver([NotificationTarget("user", "inApp", "portal")]),
    )
    source = event()
    await engine.publish(source)
    for _ in range(2):
        await engine.run_once("tenant")
        clock.now += timedelta(hours=1)
    stored = await repository.get_event("tenant", source.id)
    assert stored and stored.fanout_failed_at and stored.fanout_completed_at is None
    [dead_letter] = await repository.list_outbox_states("tenant")
    assert dead_letter.state == "permanentlyFailed"
    assert len((await engine.list_inbox("tenant", "user", "portal")).items) == 1


async def test_paused_worker_does_not_claim_or_send(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await repository.update_worker_state(
        "tenant", "notifications", lambda current: current.model_copy(update={"paused": True})
    )
    engine = service(repository, clock)
    await engine.publish(event())
    await engine.run_once("tenant")
    assert not (await repository.list_inbox("tenant", "user", "portal")).items
    assert (await repository.get_backlog("tenant"))[0] == 1


async def test_durable_due_time_does_not_send_early(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    due = clock.now + timedelta(days=1)
    engine = service(
        repository,
        clock,
        dispatcher=dispatcher,
        resolver=Resolver([NotificationTarget("user", "email", "portal", next_attempt_at=due)]),
    )
    await engine.publish(event())
    await engine.run_once("tenant")
    assert not dispatcher.sent
    [queued] = await repository.list_deliveries("tenant", state="queued")
    assert queued.next_attempt_at == due and queued.attempts == 0
    clock.now = due
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1


async def test_quiet_hours_deferrals_do_not_consume_provider_retry_budget(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    await enable_email(repository)

    class QuietAuthorizer(Authorizer):
        quiet = True

        async def authorize(
            self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
        ) -> str | None:
            if self.quiet and delivery.state == "sending":
                raise NotificationDeferredError(clock.now + timedelta(days=1), "Quiet hours.")
            return None

    authorizer = QuietAuthorizer()
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted"))
    engine = service(repository, clock, authorizer=authorizer, dispatcher=dispatcher)
    await engine.publish(event())
    for _ in range(4):
        await engine.run_once("tenant")
        [deferred] = await repository.list_deliveries("tenant", state="retrying")
        assert deferred.attempts == 0 and deferred.next_attempt_at
        assert not dispatcher.sent
        clock.now = deferred.next_attempt_at
    authorizer.quiet = False
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1
    [accepted] = await repository.list_deliveries("tenant", state="providerAccepted")
    assert accepted.attempts == 1
    attempts = await repository.list_delivery_attempts("tenant", accepted.id)
    assert sum(attempt.outcome == "deferred" for attempt in attempts) == 4


async def test_large_fanout_commits_bounded_chunks_and_preserves_all_personalities(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    engine = service(
        repository,
        clock,
        resolver=Resolver(
            [NotificationTarget(f"user-{index}", "inApp", "portal") for index in range(150)]
        ),
    )
    await engine.publish(event())
    await engine.run_once("tenant")
    for index in (0, 43, 44, 87, 88, 149):
        page = await repository.list_inbox("tenant", f"user-{index}", "portal")
        assert len(page.items) == 1
    assert (await repository.get_backlog("tenant"))[0] == 0


async def test_inbox_read_and_listing_sanitize_revoked_privileged_references(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, item, _ = await seed_fanout(repository)
    authorizer = Authorizer()
    authorizer.reason = "Resource access was removed."
    engine = service(repository, clock, authorizer=authorizer)
    page = await engine.list_inbox("tenant", "user", "portal")
    assert page.unread_count == 1 and len(page.items) == 1
    assert page.items[0].subject.request_id is None
    read = await engine.set_inbox_read("tenant", "user", "portal", item.id, read=True)
    assert read.read_at and read.subject.request_id is None
    exact = await engine.get_inbox_item("tenant", "user", "portal", item.id)
    assert exact.read_at and exact.subject.request_id is None
    for tenant, user, persona in (
        ("other", "user", "portal"),
        ("tenant", "other", "portal"),
        ("tenant", "user", "admin"),
    ):
        with pytest.raises(NotFoundError):
            await engine.get_inbox_item(tenant, user, persona, item.id)


async def test_personal_routes_infer_identity_and_persona(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, item, _ = await seed_fanout(repository)
    app = FastAPI()
    app.state.notification_service = service(repository, clock)
    app.include_router(notifications_router)
    app.include_router(portal_notifications_router)
    app.include_router(notification_delivery_router)
    app.add_exception_handler(ConflictError, domain_error_handler)
    app.add_exception_handler(NotFoundError, domain_error_handler)
    portal_auth = AuthContext("user", "tenant", frozenset({"User"}))
    admin_auth = AuthContext("admin", "tenant", frozenset({"Admin"}))
    app.dependency_overrides[require_portal_user] = lambda: portal_auth
    app.dependency_overrides[require_admin] = lambda: admin_auth
    with TestClient(app) as client:
        response = client.get("/api/v1/me/notifications/inbox?user_object_id=someone-else")
        assert response.status_code == 200 and response.json()["items"][0]["id"] == item.id
        assert client.get("/api/v1/notifications/me/inbox").json()["items"] == []
        assert client.get(f"/api/v1/me/notifications/inbox/{item.id}").json()["id"] == item.id
        assert client.get(f"/api/v1/notifications/me/inbox/{item.id}").status_code == 404
        assert (
            client.patch(f"/api/v1/me/notifications/inbox/{item.id}", json={"read": True}).json()[
                "readAt"
            ]
            is not None
        )
        assert client.get("/api/v1/me/notifications/inbox?limit=101").status_code == 422
        assert client.get("/api/v1/notifications/deliveries").status_code == 200
