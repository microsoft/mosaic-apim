"""Durable fan-out and delivery. Provider acceptance is not evidence of delivery."""

import asyncio
import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol
from uuid import uuid4

import httpx
import structlog
from azure.cosmos.exceptions import CosmosHttpResponseError

from mosaic_api.domain import AuditEvent, utc_now
from mosaic_api.errors import (
    ConflictError,
    DirectoryError,
    DirectoryForbiddenError,
    DomainError,
    NotFoundError,
)
from mosaic_api.notifications import (
    CapabilityCheck,
    NotificationCategory,
    NotificationChannel,
    NotificationChannelHealth,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationEvent,
    NotificationInboxItem,
    NotificationInboxPage,
    NotificationOutboxClaim,
    NotificationPersona,
    NotificationSubject,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.notification_events import notification_record_id

logger = structlog.get_logger()


@dataclass(frozen=True)
class NotificationTarget:
    recipient_reference: str
    channel: NotificationChannel
    persona: NotificationPersona | None = None
    route_ids: Sequence[str] = field(default_factory=tuple)
    preference_revision: int | None = None
    suppression_reason: str | None = None
    digest_window_id: str | None = None
    next_attempt_at: datetime | None = None


class NotificationPartialAudienceError(DirectoryError):
    """Known-safe independent targets, with unresolved audiences left for durable replay."""

    def __init__(
        self,
        targets: Sequence[NotificationTarget],
        reason: str = "Some notification audiences could not be resolved.",
    ) -> None:
        self.targets = tuple(targets)
        super().__init__(reason)


class NotificationDeferredError(DomainError):
    """A schedule/quiet-hours deferral, not a failed provider send or an opt-out."""

    def __init__(self, defer_until: datetime, reason: str) -> None:
        if defer_until.tzinfo is None:
            raise ValueError("Notification deferrals require an aware UTC instant")
        self.defer_until = defer_until
        super().__init__(reason)


@dataclass(frozen=True)
class NotificationDispatchResult:
    outcome: Literal[
        "providerAccepted",
        "confirmedDelivered",
        "retryableFailure",
        "permanentFailure",
        "unknownOutcome",
        "suppressed",
        "deferred",
    ]
    provider_operation_id: str | None = None
    provider_message_id: str | None = None
    retry_after_seconds: int | None = None
    error_code: str | None = None
    safe_error: str | None = None
    next_attempt_at: datetime | None = None


class NotificationAudienceResolver(Protocol):
    async def resolve(self, event: NotificationEvent) -> Sequence[NotificationTarget]: ...


class NotificationDeliveryAuthorizer(Protocol):
    async def authorize(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> str | None:
        """Recheck current eligibility, resource visibility, preferences, routes and cadence.

        Return a safe suppression reason, or None when currently authorized.
        A directory outage is an exception, not evidence the user opted out.
        """
        ...


class NotificationDispatcher(Protocol):
    @property
    def supports_idempotency(self) -> bool: ...

    async def send(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> NotificationDispatchResult: ...


def notification_title(event: NotificationEvent) -> str:
    return "MOSAIC: " + str(event.event_type).replace(".", " · ").replace("_", " ")


def notification_summary(event: NotificationEvent) -> str:
    summary = event.data.summary or notification_title(event)
    if event.data.requires_plan_apply:
        summary = summary[:850] + (
            " Approval records grant intent; plan review and apply are still required."
        )
    return summary[:1000]


class NotificationService:
    def __init__(
        self,
        repository: NotificationPersistence,
        *,
        audience_resolver: NotificationAudienceResolver,
        delivery_authorizer: NotificationDeliveryAuthorizer,
        legacy_budget_authorizer: NotificationDeliveryAuthorizer | None = None,
        dispatchers: Mapping[NotificationChannel, NotificationDispatcher] | None = None,
        clock: Callable[[], datetime] = utc_now,
        owner: str | None = None,
        hosting_verified: bool = False,
        lease_seconds: int = 180,
        send_timeout_seconds: float = 30,
        max_attempts: int = 5,
        batch_size: int = 25,
    ) -> None:
        if not (
            10 <= lease_seconds <= 3600
            and 1 <= batch_size <= 100
            and 1 <= max_attempts <= 10
            and 0 < send_timeout_seconds < lease_seconds / 2
        ):
            raise ValueError("Invalid notification lease, send timeout, retry or batch bounds")
        self.repository = repository
        self._audience = audience_resolver
        self._authorizer = delivery_authorizer
        self._legacy_budget_authorizer = legacy_budget_authorizer
        self._dispatchers = dict(dispatchers or {})
        self._clock = clock
        self._owner = owner or uuid4().hex
        self._hosting_verified = hosting_verified
        self._lease_seconds = lease_seconds
        self._send_timeout = send_timeout_seconds
        self._max_attempts = max_attempts
        self._batch_size = batch_size

    async def publish(self, event: NotificationEvent) -> bool:
        return await self.repository.append_event(event)

    async def _channel_suppression(self, delivery: NotificationDelivery) -> str | None:
        settings = await self.repository.get_channel_settings(delivery.tenant_id)
        enabled = {
            "inApp": settings.in_app_enabled if settings else True,
            "email": settings.email_enabled if settings else False,
            "teamsPersonal": settings.teams_personal_enabled if settings else False,
            "teamsChannel": settings.teams_channel_enabled if settings else False,
        }
        return None if enabled[delivery.channel] else "This tenant has not enabled this channel."

    async def _suppression(
        self, delivery: NotificationDelivery, events: Sequence[NotificationEvent]
    ) -> str | None:
        if delivery.legacy_budget_notice_id is not None:
            if self._legacy_budget_authorizer is None:
                return "The durable budget notification bridge is not configured."
            if delivery.channel != "email" or any(
                event.origin != "budgetNotificationBridge" for event in events
            ):
                return "This delivery is not an owned legacy budget email."
            return await self._legacy_budget_authorizer.authorize(delivery, events)
        return await self._channel_suppression(delivery) or await self._authorizer.authorize(
            delivery, events
        )

    async def _fanout(self, claim: NotificationOutboxClaim) -> None:
        event = claim.event
        partial_failure: NotificationPartialAudienceError | None = None
        try:
            targets = await self._audience.resolve(event)
        except NotificationPartialAudienceError as error:
            targets = error.targets
            partial_failure = error
        by_id: dict[str, tuple[NotificationTarget, NotificationDelivery]] = {}
        for target in targets:
            identity = notification_record_id(
                "notificationDelivery",
                event.tenant_id,
                event.id,
                target.recipient_reference.casefold(),
                target.persona or "",
                target.channel,
                target.digest_window_id or "",
            )
            existing = by_id.get(identity)
            routes = sorted(
                set(target.route_ids) | (set(existing[1].route_ids) if existing else set())
            )
            delivery = NotificationDelivery(
                id=identity,
                tenant_id=event.tenant_id,
                event_ids=[event.id],
                channel=target.channel,
                recipient_reference=target.recipient_reference,
                persona=target.persona,
                route_ids=routes,
                preference_revision=target.preference_revision,
                digest_window_id=target.digest_window_id,
                next_attempt_at=target.next_attempt_at,
                created_at=self._clock(),
                updated_at=self._clock(),
            )
            # An eligible route wins over an ineligible route for the same person/channel.
            if existing and existing[0].suppression_reason is None:
                target = existing[0]
            by_id[identity] = (target, delivery)
        inbox: list[NotificationInboxItem] = []
        deliveries: list[NotificationDelivery] = []
        settings = await self.repository.get_channel_settings(event.tenant_id)
        retention_seconds = (settings.retention_days if settings else 90) * 86400
        for target, delivery in by_id.values():
            delivery = delivery.model_copy(update={"ttl": retention_seconds})
            try:
                reason = target.suppression_reason or await self._suppression(delivery, [event])
            except NotificationDeferredError as deferred:
                reason = None
                delivery = delivery.model_copy(
                    update={
                        "next_attempt_at": max(
                            deferred.defer_until, self._clock() + timedelta(seconds=30)
                        )
                    }
                )
            except (DirectoryError, DirectoryForbiddenError, httpx.HTTPError):
                partial_failure = partial_failure or NotificationPartialAudienceError(
                    (), "Some recipients could not be authorized; their audiences remain pending."
                )
                continue
            if reason:
                delivery = delivery.model_copy(
                    update={
                        "state": "suppressed",
                        "suppression_reason": reason[:1000],
                    }
                )
            elif delivery.channel == "inApp":
                if delivery.persona is None:
                    raise ValueError("An in-app recipient needs a persona")
                inbox.append(
                    NotificationInboxItem(
                        id=notification_record_id(
                            "notificationInbox",
                            event.tenant_id,
                            event.id,
                            delivery.recipient_reference.casefold(),
                            delivery.persona,
                        ),
                        tenant_id=event.tenant_id,
                        user_object_id=delivery.recipient_reference,
                        persona=delivery.persona,
                        event_id=event.id,
                        category=event.category,
                        severity=event.severity,
                        title=notification_title(event),
                        summary=notification_summary(event),
                        subject=event.subject,
                        occurred_at=event.occurred_at,
                        ttl=retention_seconds,
                    )
                )
                delivery = delivery.model_copy(
                    update={
                        "state": "confirmedDelivered",
                        "confirmed_delivered_at": self._clock(),
                    }
                )
            deliveries.append(delivery)
            # Renew a fenced claim as each bounded chunk commits. An inbox item and its
            # in-app delivery are always in the same transaction.
            if len(inbox) + len(deliveries) >= 88:
                await self.repository.save_fanout(claim, inbox, deliveries, complete=False)
                inbox, deliveries = [], []
        await self.repository.save_fanout(
            claim, inbox, deliveries, complete=partial_failure is None
        )
        if partial_failure is not None:
            raise partial_failure

    def _retry_time(self, identity: str, attempts: int, retry_after: int | None = None) -> datetime:
        # Stable jitter avoids synchronized replicas, while preserving reproducible diagnostics.
        jitter = int(hashlib.sha256(f"{identity}:{attempts}".encode()).hexdigest()[:4], 16) % 17
        seconds = min(3600, 30 * 2 ** min(max(attempts - 1, 0), 7)) + jitter
        if retry_after is not None:
            seconds = max(seconds, min(max(retry_after, 0), 86400))
        return self._clock() + timedelta(seconds=seconds)

    async def _deliver(self, delivery: NotificationDelivery) -> None:
        started = self._clock()
        events: list[NotificationEvent] = []
        for event_id in delivery.event_ids:
            event = await self.repository.get_event(delivery.tenant_id, event_id)
            if event is None:
                result = NotificationDispatchResult(
                    "suppressed", safe_error="The source notification has expired."
                )
                break
            events.append(event)
        else:
            try:
                reason = await self._suppression(delivery, events)
            except NotificationDeferredError as deferred:
                result = NotificationDispatchResult(
                    "deferred",
                    safe_error=deferred.message,
                    next_attempt_at=max(
                        deferred.defer_until, self._clock() + timedelta(seconds=30)
                    ),
                )
                reason = None
                eligibility_failed = True
            except (DomainError, httpx.HTTPError) as error:
                result = NotificationDispatchResult(
                    "retryableFailure",
                    error_code="eligibilityUnavailable",
                    safe_error=(
                        f"Send-time eligibility could not be checked ({type(error).__name__})."
                    ),
                )
                reason = None
                eligibility_failed = True
            else:
                eligibility_failed = False
            dispatcher = self._dispatchers.get(delivery.channel)
            if eligibility_failed:
                pass
            elif reason:
                result = NotificationDispatchResult("suppressed", safe_error=reason)
            elif dispatcher is None:
                result = NotificationDispatchResult(
                    "permanentFailure",
                    error_code="channelUnavailable",
                    safe_error="No delivery provider is configured for this channel.",
                )
            else:
                delivery = await self.repository.renew_delivery_lease(
                    delivery, now=self._clock(), lease_seconds=self._lease_seconds
                )
                try:
                    async with asyncio.timeout(self._send_timeout):
                        result = await dispatcher.send(delivery, events)
                except (TimeoutError, httpx.HTTPError):
                    # Delivery is preferred over duplicate avoidance; the attempt stays honest.
                    result = NotificationDispatchResult(
                        "unknownOutcome",
                        error_code="providerOutcomeUnknown",
                        safe_error="The provider did not return a conclusive delivery outcome.",
                    )
        updates: dict[str, object] = {
            "provider_operation_id": result.provider_operation_id or delivery.provider_operation_id,
            "provider_message_id": result.provider_message_id or delivery.provider_message_id,
            "last_error_code": result.error_code[:100] if result.error_code else None,
            "last_error": result.safe_error[:1000] if result.safe_error else None,
            "next_attempt_at": None,
            "has_unresolved_outcome": delivery.has_unresolved_outcome
            or result.outcome == "unknownOutcome",
        }
        if result.outcome in {"providerAccepted", "confirmedDelivered"}:
            updates.update(
                state=result.outcome,
                provider_accepted_at=self._clock(),
                has_unresolved_outcome=False,
            )
            if result.outcome == "confirmedDelivered":
                updates["confirmed_delivered_at"] = self._clock()
        elif result.outcome == "suppressed":
            updates.update(
                state="unknownOutcome" if delivery.has_unresolved_outcome else "suppressed",
                suppression_reason=(result.safe_error or "Delivery is no longer eligible.")[:1000],
            )
        elif result.outcome == "deferred":
            if result.next_attempt_at is None:
                raise ValueError("A notification schedule deferral needs its next-attempt instant")
            updates.update(
                state="retrying",
                next_attempt_at=result.next_attempt_at,
                attempts=max(delivery.attempts - 1, 0),
            )
        elif result.outcome in {"retryableFailure", "unknownOutcome"} and delivery.attempts < (
            delivery.max_attempts or self._max_attempts
        ):
            updates.update(
                state="retrying",
                next_attempt_at=self._retry_time(
                    delivery.id, delivery.attempts, result.retry_after_seconds
                ),
            )
        elif updates["has_unresolved_outcome"]:
            updates["state"] = "unknownOutcome"
        else:
            updates["state"] = "permanentlyFailed"
        if updates["state"] == "unknownOutcome" and result.outcome != "unknownOutcome":
            updates["last_error"] = (
                "An earlier attempt may have been accepted; its outcome remains unknown. "
                f"Latest attempt: {result.safe_error or result.outcome}."
            )[:1000]
        saved = await self.repository.save_delivery(
            delivery.model_copy(update=updates),
            NotificationDeliveryAttempt(
                id=notification_record_id(
                    "notificationAttempt",
                    delivery.tenant_id,
                    delivery.id,
                    delivery.lease_token or "",
                ),
                tenant_id=delivery.tenant_id,
                delivery_id=delivery.id,
                attempt=delivery.attempts,
                started_at=started,
                finished_at=self._clock(),
                outcome=result.outcome,
                provider_operation_id=result.provider_operation_id,
                retry_after_seconds=(
                    min(max(result.retry_after_seconds, 0), 86400)
                    if result.retry_after_seconds is not None
                    else None
                ),
                safe_error=result.safe_error[:1000] if result.safe_error else None,
            ),
        )
        if result.outcome not in {"deferred", "suppressed"} or saved.state == "unknownOutcome":
            await self._record_health(saved)

    async def _record_health(self, delivery: NotificationDelivery) -> None:
        for _ in range(3):
            identity = notification_record_id(
                "notificationChannelHealth",
                delivery.tenant_id,
                delivery.channel,
                delivery.recipient_reference if delivery.channel == "teamsChannel" else "",
            )
            current = next(
                (
                    item
                    for item in await self.repository.list_channel_health(delivery.tenant_id)
                    if item.id == identity
                ),
                None,
            )
            health = current or NotificationChannelHealth(
                id=identity,
                tenant_id=delivery.tenant_id,
                channel=delivery.channel,
                destination_id=(
                    delivery.recipient_reference if delivery.channel == "teamsChannel" else None
                ),
            )
            failure = delivery.state in {"retrying", "permanentlyFailed", "unknownOutcome"}
            now = self._clock()
            check = CapabilityCheck(
                feature=f"notification.{delivery.channel}",
                scope=delivery.tenant_id,
                state="failed"
                if failure
                else ("verified" if delivery.provider_accepted_at else "unknown"),
                checked_at=now,
                fresh_until=now + timedelta(hours=1),
                evidence=["Provider accepted a notification; actual receipt is unconfirmed."]
                if delivery.provider_accepted_at
                else [],
                reason=delivery.last_error if failure else delivery.suppression_reason,
            )
            updates: dict[str, object] = {"checks": [check], "updated_at": now}
            if failure:
                updates["last_failure_at"] = now
            if delivery.provider_accepted_at:
                updates["last_provider_accepted_at"] = delivery.provider_accepted_at
            if delivery.confirmed_delivered_at:
                updates["last_confirmed_delivered_at"] = delivery.confirmed_delivered_at
            try:
                await self.repository.save_channel_health(health.model_copy(update=updates))
                return
            except ConflictError:
                continue
        raise ConflictError("Notification channel health changed concurrently")

    async def run_once(self, tenant_id: str) -> None:
        backlog, oldest = await self.repository.get_backlog(tenant_id)
        state = await self.repository.update_worker_state(
            tenant_id,
            "notifications",
            lambda current: current.model_copy(
                update={
                    "heartbeat_at": self._clock(),
                    "hosting_verified": self._hosting_verified,
                    "backlog": backlog,
                    "oldest_queued_at": oldest,
                }
            ),
        )
        if state.paused:
            return
        partial_failures = 0
        for _ in range(self._batch_size):
            claims = await self.repository.claim_events(
                tenant_id,
                self._owner,
                now=self._clock(),
                lease_seconds=self._lease_seconds,
                limit=1,
            )
            if not claims:
                break
            claim = claims[0]
            try:
                await self._fanout(claim)
            except (DomainError, httpx.HTTPError, CosmosHttpResponseError) as error:
                partial = isinstance(error, NotificationPartialAudienceError)
                if partial:
                    partial_failures += 1
                    logger.warning(
                        "notification_partial_audience",
                        event_id=claim.event.id,
                        attempt=claim.state.attempts,
                        error_type=type(error).__name__,
                    )
                try:
                    await self.repository.fail_fanout(
                        claim,
                        error=f"Recipient resolution failed ({type(error).__name__}).",
                        next_attempt_at=self._retry_time(claim.event.id, claim.state.attempts),
                        permanent=claim.state.attempts >= self._max_attempts,
                    )
                except ConflictError:
                    # A different worker owns the newer claim; it alone may checkpoint it.
                    continue
                if partial:
                    continue
                raise
        for _ in range(self._batch_size):
            deliveries = await self.repository.claim_deliveries(
                tenant_id,
                self._owner,
                now=self._clock(),
                lease_seconds=self._lease_seconds,
                limit=1,
                max_attempts=self._max_attempts,
            )
            if not deliveries:
                break
            await self._deliver(deliveries[0])
        backlog, oldest = await self.repository.get_backlog(tenant_id)
        partial_error = (
            (
                "Unresolved notification audiences (NotificationPartialAudienceError); "
                "safe targets continued. Inspect outbox retries and dead letters."
            )
            if partial_failures
            else (
                state.last_error
                if backlog
                and (state.last_error or "").startswith("Unresolved notification audiences")
                else None
            )
        )
        await self.repository.update_worker_state(
            tenant_id,
            "notifications",
            lambda current: current.model_copy(
                update={
                    "last_succeeded_at": current.last_succeeded_at
                    if partial_error
                    else self._clock(),
                    "last_error": partial_error,
                    "heartbeat_at": self._clock(),
                    "hosting_verified": self._hosting_verified,
                    "backlog": backlog,
                    "oldest_queued_at": oldest,
                }
            ),
        )

    async def run(self, tenant_id: str, *, interval_seconds: float = 15) -> None:
        if not 1 <= interval_seconds <= 3600:
            raise ValueError("Notification polling interval must be between 1 and 3600 seconds")
        while True:
            try:
                await self.run_once(tenant_id)
            except (DomainError, httpx.HTTPError, CosmosHttpResponseError) as error:
                logger.warning("notification_worker_failed", error_type=type(error).__name__)
                try:
                    await self._worker_failure(tenant_id, type(error).__name__)
                except (DomainError, CosmosHttpResponseError) as state_error:
                    logger.warning(
                        "notification_worker_state_unavailable",
                        error_type=type(state_error).__name__,
                    )
            await asyncio.sleep(interval_seconds)

    async def _worker_failure(self, tenant_id: str, error_type: str) -> None:
        await self.repository.update_worker_state(
            tenant_id,
            "notifications",
            lambda current: current.model_copy(
                update={
                    "last_error": f"Worker failed ({error_type}); inspect channel health.",
                    "heartbeat_at": self._clock(),
                }
            ),
        )

    async def retry_delivery(
        self,
        tenant_id: str,
        delivery_id: str,
        *,
        actor_object_id: str,
        allow_unknown: bool = False,
    ) -> NotificationDelivery:
        delivery = await self.repository.get_delivery(tenant_id, delivery_id)
        if delivery is None:
            raise NotFoundError("The notification delivery does not exist")
        return await self.repository.requeue_delivery(
            delivery,
            AuditEvent(
                id=f"audit_{uuid4().hex}",
                tenant_id=tenant_id,
                action="notification.deliveryRetried",
                resource_type="notificationDelivery",
                resource_id=delivery.id,
                actor_object_id=actor_object_id,
                details={"previousState": delivery.state},
            ),
        )

    async def list_inbox(
        self,
        tenant_id: str,
        user_object_id: str,
        persona: NotificationPersona,
        *,
        category: NotificationCategory | None = None,
        unread_only: bool = False,
        limit: int = 50,
        continuation_token: str | None = None,
    ) -> NotificationInboxPage:
        page = await self.repository.list_inbox(
            tenant_id,
            user_object_id,
            persona,
            category=category,
            unread_only=unread_only,
            limit=limit,
            continuation_token=continuation_token,
        )
        visible = [await self._inbox_view(item) for item in page.items]
        return page.model_copy(update={"items": visible})

    async def _inbox_view(self, item: NotificationInboxItem) -> NotificationInboxItem:
        event = await self.repository.get_event(item.tenant_id, item.event_id)
        candidate = NotificationDelivery(
            id=item.id,
            tenant_id=item.tenant_id,
            event_ids=[item.event_id],
            channel="inApp",
            recipient_reference=item.user_object_id,
            persona=item.persona,
        )
        if event is not None and await self._authorizer.authorize(candidate, [event]) is None:
            return item
        # Keep read state and counts stable without returning stale privileged references.
        return item.model_copy(
            update={
                "title": "MOSAIC notification",
                "subject": NotificationSubject(),
                "summary": "This notification is no longer available with your current access.",
            }
        )

    async def get_inbox_item(
        self, tenant_id: str, user_object_id: str, persona: NotificationPersona, item_id: str
    ) -> NotificationInboxItem:
        item = await self.repository.get_inbox_item(tenant_id, user_object_id, persona, item_id)
        if item is None:
            raise NotFoundError("The notification does not exist")
        return await self._inbox_view(item)

    async def set_inbox_read(
        self,
        tenant_id: str,
        user_object_id: str,
        persona: NotificationPersona,
        item_id: str,
        *,
        read: bool,
    ) -> NotificationInboxItem:
        item = await self.repository.set_inbox_read(
            tenant_id, user_object_id, persona, item_id, read_at=self._clock() if read else None
        )
        return await self._inbox_view(item)
