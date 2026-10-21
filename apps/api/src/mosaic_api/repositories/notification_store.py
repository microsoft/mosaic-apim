"""Shared notification semantics; backends implement conditional tenant transactions."""

import base64
import json
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import TypeVar
from uuid import NAMESPACE_URL, uuid4, uuid5

from mosaic_api.domain import AuditEvent, Entity, utc_now
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.notifications import (
    DigestWatermark,
    NotificationAlertIncident,
    NotificationCategory,
    NotificationChannelHealth,
    NotificationChannelSettings,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationDeliveryState,
    NotificationEvent,
    NotificationInboxItem,
    NotificationInboxPage,
    NotificationOutboxClaim,
    NotificationOutboxState,
    NotificationPersona,
    NotificationPreferences,
    NotificationRoute,
    NotificationWorkerState,
    PersonalTeamsBinding,
    PersonalTeamsSettings,
    TeamsDestination,
)
from mosaic_api.scheduling import SchedulerLease
from mosaic_api.services.notification_events import (
    notification_preferences_id,
    notification_record_id,
)

T = TypeVar("T", bound=Entity)
Mutation = tuple[Entity | None, Entity | None]
CHANGED = "The notification record changed; reload it and try again"


def inbox_after(scope: list[object], token: str | None) -> str | None:
    if token is None:
        return None
    try:
        if len(token) > 4096:
            raise ValueError
        payload = json.loads(base64.urlsafe_b64decode(token.encode()))
        after = payload["after"]
        if payload["scope"] != scope or not isinstance(after, str) or len(after) > 512:
            raise ValueError
        return after
    except (ValueError, KeyError, TypeError, UnicodeError) as error:
        raise ValidationError("The inbox continuation token is invalid for this view") from error


def inbox_token(scope: list[object], after: str) -> str:
    return base64.urlsafe_b64encode(json.dumps({"scope": scope, "after": after}).encode()).decode()


class NotificationStore:
    """All mutations compare the read ETag, including creates and deletes."""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        self._clock = clock

    async def _get(self, model: type[T], tenant: str, item_id: str) -> T | None:
        raise NotImplementedError

    async def _list(
        self, model: type[T], tenant: str, entity_type: str, *, limit: int | None = None
    ) -> list[T]:
        raise NotImplementedError

    async def _commit(self, tenant: str, mutations: Sequence[Mutation]) -> None:
        """Atomically compare all expected records then persist all replacements/deletions."""
        raise NotImplementedError

    async def _save(self, item: T, audit: AuditEvent | None = None) -> T:
        current = await self._get(type(item), item.tenant_id, item.id)
        if (current is None) != (item.etag is None) or (
            current is not None and current.etag != item.etag
        ):
            raise ConflictError(CHANGED)
        mutations: list[Mutation] = [(current, item)]
        if audit is not None:
            if audit.tenant_id != item.tenant_id:
                raise ConflictError("The audit event belongs to another tenant")
            mutations.append((None, audit))
        await self._commit(item.tenant_id, mutations)
        saved = await self._get(type(item), item.tenant_id, item.id)
        if saved is None:
            raise ConflictError(CHANGED)
        return saved

    async def _delete(self, item: Entity, audit: AuditEvent) -> None:
        if audit.tenant_id != item.tenant_id or not item.etag:
            raise ConflictError(CHANGED)
        await self._commit(item.tenant_id, [(item, None), (None, audit)])

    async def get_preferences(
        self, tenant_id: str, user_object_id: str, persona: NotificationPersona
    ) -> NotificationPreferences | None:
        item = await self._get(
            NotificationPreferences,
            tenant_id,
            notification_preferences_id(tenant_id, user_object_id, persona),
        )
        return (
            item
            if item is None
            or (
                item.user_object_id.casefold() == user_object_id.casefold()
                and item.persona == persona
            )
            else None
        )

    async def save_preferences(
        self, preferences: NotificationPreferences, audit_event: AuditEvent
    ) -> NotificationPreferences:
        if preferences.id != notification_preferences_id(
            preferences.tenant_id, preferences.user_object_id, preferences.persona
        ):
            raise ValidationError("The preferences ID must identify its tenant, user and persona")
        return await self._save(preferences, audit_event)

    async def list_preferences(self, tenant_id: str) -> list[NotificationPreferences]:
        return await self._list(NotificationPreferences, tenant_id, "notificationPreferences")

    async def list_routes(self, tenant_id: str) -> list[NotificationRoute]:
        return await self._list(NotificationRoute, tenant_id, "notificationRoute")

    async def get_route(self, tenant_id: str, route_id: str) -> NotificationRoute | None:
        return await self._get(NotificationRoute, tenant_id, route_id)

    async def save_route(
        self, route: NotificationRoute, audit_event: AuditEvent
    ) -> NotificationRoute:
        return await self._save(route, audit_event)

    async def delete_route(self, route: NotificationRoute, audit_event: AuditEvent) -> None:
        await self._delete(route, audit_event)

    async def get_channel_settings(self, tenant_id: str) -> NotificationChannelSettings | None:
        items = await self._list(
            NotificationChannelSettings, tenant_id, "notificationChannelSettings"
        )
        return items[0] if items else None

    async def save_channel_settings(
        self, settings: NotificationChannelSettings, audit_event: AuditEvent
    ) -> NotificationChannelSettings:
        return await self._save(settings, audit_event)

    async def list_teams_destinations(self, tenant_id: str) -> list[TeamsDestination]:
        return await self._list(TeamsDestination, tenant_id, "teamsDestination")

    async def get_teams_destination(
        self, tenant_id: str, destination_id: str
    ) -> TeamsDestination | None:
        return await self._get(TeamsDestination, tenant_id, destination_id)

    async def save_teams_destination(
        self, destination: TeamsDestination, audit_event: AuditEvent
    ) -> TeamsDestination:
        return await self._save(destination, audit_event)

    async def delete_teams_destination(
        self, destination: TeamsDestination, audit_event: AuditEvent
    ) -> None:
        await self._delete(destination, audit_event)

    async def get_personal_teams_settings(self, tenant_id: str) -> PersonalTeamsSettings | None:
        items = await self._list(PersonalTeamsSettings, tenant_id, "personalTeamsSettings")
        return items[0] if items else None

    async def save_personal_teams_settings(
        self, settings: PersonalTeamsSettings, audit_event: AuditEvent
    ) -> PersonalTeamsSettings:
        return await self._save(settings, audit_event)

    async def get_personal_teams_binding(
        self, tenant_id: str, user_object_id: str
    ) -> PersonalTeamsBinding | None:
        items = await self._list(PersonalTeamsBinding, tenant_id, "personalTeamsBinding")
        return next(
            (item for item in items if item.user_object_id.casefold() == user_object_id.casefold()),
            None,
        )

    async def save_personal_teams_binding(
        self, binding: PersonalTeamsBinding
    ) -> PersonalTeamsBinding:
        return await self._save(binding)

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
        if not 1 <= limit <= 100:
            raise ValidationError("Choose an inbox page size between 1 and 100")
        scope: list[object] = [tenant_id, user_object_id.casefold(), persona, category, unread_only]
        after = inbox_after(scope, continuation_token)
        all_items = [
            item
            for item in await self._list(NotificationInboxItem, tenant_id, "notificationInboxItem")
            if item.user_object_id.casefold() == user_object_id.casefold()
            and item.persona == persona
        ]
        unread_count = sum(item.read_at is None for item in all_items)
        items = sorted(
            (
                item
                for item in all_items
                if (category is None or item.category == category)
                and (not unread_only or item.read_at is None)
            ),
            key=lambda item: item.sort_key,
            reverse=True,
        )
        if after:
            items = [item for item in items if item.sort_key < after]
        page = items[:limit]
        token = None
        if len(items) > limit:
            token = inbox_token(scope, page[-1].sort_key)
        return NotificationInboxPage(
            items=page, continuation_token=token, unread_count=unread_count
        )

    async def set_inbox_read(
        self,
        tenant_id: str,
        user_object_id: str,
        persona: NotificationPersona,
        item_id: str,
        *,
        read_at: datetime | None,
    ) -> NotificationInboxItem:
        for _ in range(5):
            item = await self._get(NotificationInboxItem, tenant_id, item_id)
            if item is None or (
                item.user_object_id.casefold() != user_object_id.casefold()
                or item.persona != persona
            ):
                raise NotFoundError("The notification does not exist")
            try:
                return await self._save(item.model_copy(update={"read_at": read_at}))
            except ConflictError:
                continue
        raise ConflictError(CHANGED)

    async def get_inbox_item(
        self, tenant_id: str, user_object_id: str, persona: NotificationPersona, item_id: str
    ) -> NotificationInboxItem | None:
        item = await self._get(NotificationInboxItem, tenant_id, item_id)
        return (
            item
            if item is None
            or (
                item.user_object_id.casefold() == user_object_id.casefold()
                and item.persona == persona
            )
            else None
        )

    async def append_event(self, event: NotificationEvent) -> bool:
        try:
            await self._commit(event.tenant_id, [(None, event)])
            return True
        except ConflictError:
            stored = await self.get_event(event.tenant_id, event.id)
            excluded = {
                "etag",
                "fanout_completed_at",
                "fanout_failed_at",
                "fanout_next_attempt_at",
                "updated_at",
            }
            if stored is None or stored.model_dump(exclude=excluded) != event.model_dump(
                exclude=excluded
            ):
                raise ConflictError(
                    "This notification event ID already identifies other facts"
                ) from None
            return False

    async def get_event(self, tenant_id: str, event_id: str) -> NotificationEvent | None:
        return await self._get(NotificationEvent, tenant_id, event_id)

    async def import_budget_delivery(
        self, event: NotificationEvent, delivery: NotificationDelivery
    ) -> None:
        if (
            delivery.tenant_id != event.tenant_id
            or delivery.event_ids != [event.id]
            or (not delivery.legacy_budget_notice_id or event.origin != "budgetNotificationBridge")
        ):
            raise ConflictError(
                "A budget projection must identify its own source notice and tenant"
            )
        for _ in range(5):
            source = await self.get_event(event.tenant_id, event.id)
            current = await self.get_delivery(event.tenant_id, delivery.id)
            mutations: list[Mutation] = []
            if source is None:
                mutations.append((None, event))
            if current is None:
                mutations.append((None, delivery))
            elif current.legacy_budget_notice_id != delivery.legacy_budget_notice_id:
                raise ConflictError("A delivery ID belongs to another budget notice")
            elif (
                delivery.state == "queued"
                and current.state == "unknownOutcome"
                and current.last_error_code == "legacyUnclaimed"
            ):
                mutations.append((current, delivery.model_copy(update={"etag": current.etag})))
            elif delivery.state == "providerAccepted" and current.state != "providerAccepted":
                # A source receipt can close an ambiguous post-send crash, never initiate a send.
                if current.state != "sending" or (
                    current.lease_expires_at and current.lease_expires_at <= self._clock()
                ):
                    mutations.append(
                        (
                            current,
                            delivery.model_copy(
                                update={
                                    "etag": current.etag,
                                    "attempts": max(current.attempts, delivery.attempts),
                                }
                            ),
                        )
                    )
            if not mutations:
                return
            try:
                await self._commit(event.tenant_id, mutations)
                return
            except ConflictError:
                continue
        raise ConflictError("The budget projection kept changing")

    async def list_outbox_states(
        self, tenant_id: str, *, failed_only: bool = True, limit: int = 50
    ) -> list[NotificationOutboxState]:
        if not 1 <= limit <= 100:
            raise ValueError("Choose an outbox page size between 1 and 100")
        return [
            item
            for item in await self._list(
                NotificationOutboxState, tenant_id, "notificationOutboxState"
            )
            if not failed_only or item.state == "permanentlyFailed"
        ][:limit]

    async def requeue_event(
        self, tenant_id: str, event_id: str, audit_event: AuditEvent
    ) -> NotificationEvent:
        event = await self.get_event(tenant_id, event_id)
        if event is None:
            raise NotFoundError("The notification event does not exist")
        state = await self._get(
            NotificationOutboxState,
            tenant_id,
            notification_record_id("notificationOutbox", tenant_id, event_id),
        )
        if state is None or state.state != "permanentlyFailed":
            raise ConflictError("Only permanently failed notification events can be retried")
        changed = event.model_copy(
            update={
                "fanout_failed_at": None,
                "fanout_next_attempt_at": None,
            }
        )
        await self._commit(
            tenant_id,
            [
                (event, changed),
                (
                    state,
                    state.model_copy(
                        update={
                            "state": "queued",
                            "attempts": 0,
                            "next_attempt_at": None,
                            "last_error": None,
                            "lease_owner": None,
                            "lease_token": None,
                            "lease_expires_at": None,
                        }
                    ),
                ),
                (None, audit_event),
            ],
        )
        result = await self.get_event(tenant_id, event_id)
        assert result is not None
        return result

    async def claim_events(
        self,
        tenant_id: str,
        owner: str,
        *,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[NotificationOutboxClaim]:
        self._claim_limits(lease_seconds, limit)
        claims: list[NotificationOutboxClaim] = []
        for event in await self._list(
            NotificationEvent, tenant_id, "notificationEvent", limit=limit * 4
        ):
            if event.fanout_completed_at is not None or event.fanout_failed_at is not None:
                continue
            state_id = notification_record_id("notificationOutbox", tenant_id, event.id)
            state = await self._get(NotificationOutboxState, tenant_id, state_id)
            if state and (
                state.state in {"completed", "permanentlyFailed"}
                or (state.lease_expires_at and state.lease_expires_at > now)
                or (state.next_attempt_at and state.next_attempt_at > now)
            ):
                continue
            claimed = (
                state
                or NotificationOutboxState(id=state_id, tenant_id=tenant_id, event_id=event.id)
            ).model_copy(
                update={
                    "state": "processing",
                    "lease_owner": owner,
                    "lease_token": uuid4().hex,
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "attempts": (state.attempts if state else 0) + 1,
                    "updated_at": now,
                }
            )
            try:
                await self._commit(
                    tenant_id,
                    [
                        (state, claimed),
                        (
                            event,
                            event.model_copy(
                                update={"fanout_next_attempt_at": claimed.lease_expires_at}
                            ),
                        ),
                    ],
                )
            except ConflictError:
                continue
            stored = await self._get(NotificationOutboxState, tenant_id, state_id)
            assert stored is not None
            claims.append(NotificationOutboxClaim(event=event, state=stored))
            if len(claims) == limit:
                break
        return claims

    @staticmethod
    def _claim_limits(lease_seconds: int, limit: int) -> None:
        if not 1 <= limit <= 100 or not 10 <= lease_seconds <= 3600:
            raise ValueError("Claim bounds: 1-100 records and a 10-3600 second lease")

    async def _outbox_current(self, claim: NotificationOutboxClaim) -> NotificationOutboxState:
        current = await self._get(NotificationOutboxState, claim.event.tenant_id, claim.state.id)
        if current is None or (
            current.lease_token != claim.state.lease_token
            or current.state != "processing"
            or current.lease_expires_at is None
            or current.lease_expires_at <= self._clock()
        ):
            raise ConflictError("The notification fan-out lease expired or changed")
        return current

    async def save_fanout(
        self,
        claim: NotificationOutboxClaim,
        inbox_items: Sequence[NotificationInboxItem],
        deliveries: Sequence[NotificationDelivery],
        *,
        complete: bool,
    ) -> None:
        if len(inbox_items) + len(deliveries) > 98:
            raise ValueError("Fan-out batches may contain at most 98 records")
        current = await self._outbox_current(claim)
        mutations: list[Mutation] = []
        for item in [*inbox_items, *deliveries]:
            if (
                item.tenant_id != claim.event.tenant_id
                or (isinstance(item, NotificationInboxItem) and item.event_id != claim.event.id)
                or (isinstance(item, NotificationDelivery) and claim.event.id not in item.event_ids)
            ):
                raise ConflictError("Fan-out must reference the claimed event and tenant")
            existing = await self._get(type(item), item.tenant_id, item.id)
            if existing is None:
                mutations.append((None, item))
            elif (
                isinstance(item, NotificationInboxItem)
                and isinstance(existing, NotificationInboxItem)
                and (item.user_object_id, item.persona, item.event_id)
                != (existing.user_object_id, existing.persona, existing.event_id)
            ) or (
                isinstance(item, NotificationDelivery)
                and isinstance(existing, NotificationDelivery)
                and (item.recipient_reference, item.persona, item.channel, item.event_ids)
                != (
                    existing.recipient_reference,
                    existing.persona,
                    existing.channel,
                    existing.event_ids,
                )
            ):
                raise ConflictError("A fan-out ID already identifies another recipient")
        assert claim.state.lease_expires_at is not None
        lease_length = claim.state.lease_expires_at - claim.state.updated_at
        changed = current.model_copy(
            update={
                "updated_at": self._clock(),
                "lease_expires_at": self._clock() + lease_length,
            }
        )
        event = await self.get_event(claim.event.tenant_id, claim.event.id)
        if event is None:
            raise ConflictError("The source event no longer exists")
        if complete:
            changed = changed.model_copy(
                update={
                    "state": "completed",
                    "lease_owner": None,
                    "lease_token": None,
                    "lease_expires_at": None,
                    "last_error": None,
                }
            )
            mutations.append(
                (
                    event,
                    event.model_copy(
                        update={
                            "fanout_completed_at": self._clock(),
                            "fanout_next_attempt_at": None,
                        }
                    ),
                )
            )
        else:
            mutations.append(
                (
                    event,
                    event.model_copy(update={"fanout_next_attempt_at": changed.lease_expires_at}),
                )
            )
        mutations.append((current, changed))
        await self._commit(claim.event.tenant_id, mutations)

    async def fail_fanout(
        self,
        claim: NotificationOutboxClaim,
        *,
        error: str,
        next_attempt_at: datetime,
        permanent: bool = False,
    ) -> None:
        current = await self._outbox_current(claim)
        mutations: list[Mutation] = [
            (
                current,
                current.model_copy(
                    update={
                        "state": "permanentlyFailed" if permanent else "retrying",
                        "next_attempt_at": None if permanent else next_attempt_at,
                        "last_error": error[:1000],
                        "lease_owner": None,
                        "lease_token": None,
                        "lease_expires_at": None,
                    }
                ),
            )
        ]
        event = await self.get_event(claim.event.tenant_id, claim.event.id)
        if event is None:
            raise ConflictError("The source event no longer exists")
        mutations.append(
            (
                event,
                event.model_copy(
                    update={
                        "fanout_failed_at": self._clock() if permanent else None,
                        "fanout_next_attempt_at": None if permanent else next_attempt_at,
                    }
                ),
            )
        )
        await self._commit(current.tenant_id, mutations)

    async def claim_deliveries(
        self,
        tenant_id: str,
        owner: str,
        *,
        now: datetime,
        lease_seconds: int,
        limit: int,
        max_attempts: int = 5,
    ) -> list[NotificationDelivery]:
        self._claim_limits(lease_seconds, limit)
        if not 1 <= max_attempts <= 10:
            raise ValueError("Choose a delivery retry limit between 1 and 10")
        claims: list[NotificationDelivery] = []
        for item in await self._list(
            NotificationDelivery, tenant_id, "notificationDelivery", limit=limit * 4
        ):
            retry_limit = item.max_attempts or max_attempts
            if item.attempts >= retry_limit and (
                item.state in {"queued", "retrying", "unknownOutcome"}
            ):
                if item.state == "unknownOutcome":
                    if item.max_attempts is None:
                        try:
                            await self._save(item.model_copy(update={"max_attempts": retry_limit}))
                        except ConflictError:
                            pass
                    continue
                try:
                    await self._save(
                        item.model_copy(
                            update={
                                "state": "permanentlyFailed",
                                "last_error_code": "retryBudgetExhausted",
                                "last_error": "The configured provider retry budget is exhausted.",
                            }
                        )
                    )
                except ConflictError:
                    pass
                continue
            if item.state == "sending" and item.lease_expires_at and item.lease_expires_at <= now:
                attempt_id = notification_record_id(
                    "notificationAttempt", tenant_id, item.id, item.lease_token or ""
                )
                attempt = await self._get(NotificationDeliveryAttempt, tenant_id, attempt_id)
                mutations: list[Mutation] = []
                if attempt is not None:
                    mutations.append(
                        (
                            attempt,
                            attempt.model_copy(
                                update={
                                    "outcome": "unknownOutcome",
                                    "finished_at": now,
                                    "safe_error": "The sending worker lease expired.",
                                }
                            ),
                        )
                    )
                mutations.append(
                    (
                        item,
                        item.model_copy(
                            update={
                                "state": "retrying"
                                if item.attempts < retry_limit
                                else "unknownOutcome",
                                "max_attempts": retry_limit,
                                "next_attempt_at": now if item.attempts < retry_limit else None,
                                "last_error_code": "leaseExpired",
                                "last_error": (
                                    "Worker lease expired; acceptance is uncertain. "
                                    "MOSAIC retries within the normal attempt limit."
                                ),
                                "lease_owner": None,
                                "lease_token": None,
                                "lease_expires_at": None,
                            }
                        ),
                    )
                )
                try:
                    await self._commit(tenant_id, mutations)
                except ConflictError:
                    pass
                continue
            if item.state not in {"queued", "retrying", "unknownOutcome"} or (
                item.next_attempt_at and item.next_attempt_at > now
            ):
                continue
            try:
                token = uuid4().hex
                claimed = item.model_copy(
                    update={
                        "state": "sending",
                        "lease_owner": owner,
                        "lease_token": token,
                        "lease_expires_at": now + timedelta(seconds=lease_seconds),
                        "attempts": item.attempts + 1,
                        "max_attempts": retry_limit,
                        "updated_at": now,
                        "provider_operation_id": item.provider_operation_id
                        or str(uuid5(NAMESPACE_URL, f"mosaic-notification:{item.id}")),
                    }
                )
                attempt = NotificationDeliveryAttempt(
                    id=notification_record_id("notificationAttempt", tenant_id, item.id, token),
                    tenant_id=tenant_id,
                    delivery_id=item.id,
                    attempt=claimed.attempts,
                    started_at=now,
                    outcome="sending",
                    provider_operation_id=claimed.provider_operation_id,
                )
                await self._commit(tenant_id, [(item, claimed), (None, attempt)])
                saved = await self.get_delivery(tenant_id, item.id)
                assert saved is not None
            except ConflictError:
                continue
            claims.append(saved)
            if len(claims) == limit:
                break
        return claims

    async def save_delivery(
        self, delivery: NotificationDelivery, attempt: NotificationDeliveryAttempt | None = None
    ) -> NotificationDelivery:
        current = await self.get_delivery(delivery.tenant_id, delivery.id)
        if current is None or (
            current.state != "sending"
            or current.etag != delivery.etag
            or current.lease_token != delivery.lease_token
            or current.lease_expires_at is None
            or current.lease_expires_at <= self._clock()
        ):
            raise ConflictError("The notification delivery lease expired or changed")
        if delivery.state in {"queued", "sending"} or (
            delivery.event_ids,
            delivery.channel,
            delivery.recipient_reference,
            delivery.persona,
        ) != (current.event_ids, current.channel, current.recipient_reference, current.persona):
            raise ConflictError("A send claim may only update its own delivery outcome")
        if attempt is not None and (
            attempt.tenant_id != delivery.tenant_id
            or attempt.delivery_id != delivery.id
            or attempt.attempt != current.attempts
        ):
            raise ConflictError("The attempt does not belong to the claimed delivery")
        stored = NotificationDelivery.model_validate(
            {
                **delivery.model_dump(by_alias=False),
                "etag": current.etag,
                "lease_owner": None,
                "lease_token": None,
                "lease_expires_at": None,
                "updated_at": self._clock(),
            }
        )
        mutations: list[Mutation] = [(current, stored)]
        if attempt:
            existing_attempt = await self._get(
                NotificationDeliveryAttempt, delivery.tenant_id, attempt.id
            )
            if existing_attempt is None or (
                existing_attempt.delivery_id != delivery.id or existing_attempt.outcome != "sending"
            ):
                raise ConflictError("The notification attempt is no longer sending")
            mutations.append((existing_attempt, attempt))
        await self._commit(delivery.tenant_id, mutations)
        result = await self.get_delivery(delivery.tenant_id, delivery.id)
        assert result is not None
        return result

    async def renew_delivery_lease(
        self, delivery: NotificationDelivery, *, now: datetime, lease_seconds: int
    ) -> NotificationDelivery:
        self._claim_limits(lease_seconds, 1)
        current = await self.get_delivery(delivery.tenant_id, delivery.id)
        if current is None or (
            current.state != "sending"
            or current.etag != delivery.etag
            or current.lease_token != delivery.lease_token
            or current.lease_expires_at is None
            or current.lease_expires_at <= now
        ):
            raise ConflictError("The notification delivery lease expired or changed")
        return await self._save(
            current.model_copy(
                update={
                    "lease_expires_at": now + timedelta(seconds=lease_seconds),
                    "updated_at": now,
                }
            )
        )

    async def requeue_delivery(
        self, delivery: NotificationDelivery, audit_event: AuditEvent | None = None
    ) -> NotificationDelivery:
        if delivery.state not in {"permanentlyFailed", "unknownOutcome", "suppressed"}:
            raise ConflictError(
                "Only failed, suppressed or indeterminate deliveries can be retried"
            )
        return await self._save(
            delivery.model_copy(
                update={
                    "state": "queued",
                    "next_attempt_at": None,
                    "last_error": None,
                    "last_error_code": None,
                    "suppression_reason": None,
                    "attempts": 0,
                }
            ),
            audit_event,
        )

    async def list_deliveries(
        self,
        tenant_id: str,
        *,
        event_id: str | None = None,
        state: NotificationDeliveryState | None = None,
        limit: int = 50,
    ) -> list[NotificationDelivery]:
        if not 1 <= limit <= 100:
            raise ValueError("Choose a delivery page size between 1 and 100")
        return [
            item
            for item in await self._list(NotificationDelivery, tenant_id, "notificationDelivery")
            if (event_id is None or event_id in item.event_ids)
            and (state is None or item.state == state)
        ][:limit]

    async def get_delivery(self, tenant_id: str, delivery_id: str) -> NotificationDelivery | None:
        return await self._get(NotificationDelivery, tenant_id, delivery_id)

    async def list_delivery_attempts(
        self, tenant_id: str, delivery_id: str
    ) -> list[NotificationDeliveryAttempt]:
        return sorted(
            (
                item
                for item in await self._list(
                    NotificationDeliveryAttempt, tenant_id, "notificationDeliveryAttempt"
                )
                if item.delivery_id == delivery_id
            ),
            key=lambda item: item.started_at,
            reverse=True,
        )[:100]

    async def get_backlog(self, tenant_id: str) -> tuple[int, datetime | None]:
        queued: list[Entity] = [
            event
            for event in await self._list(NotificationEvent, tenant_id, "notificationEvent")
            if event.fanout_completed_at is None and event.fanout_failed_at is None
        ]
        queued.extend(
            item
            for item in await self._list(NotificationDelivery, tenant_id, "notificationDelivery")
            if item.state in {"queued", "retrying", "sending"}
            or (item.state == "unknownOutcome" and item.attempts < (item.max_attempts or 5))
        )
        return len(queued), min((item.created_at for item in queued), default=None)

    async def get_digest_watermark(
        self, tenant_id: str, watermark_id: str
    ) -> DigestWatermark | None:
        return await self._get(DigestWatermark, tenant_id, watermark_id)

    async def commit_source_event(
        self,
        event: NotificationEvent,
        *,
        watermark: DigestWatermark | None = None,
        incident: NotificationAlertIncident | None = None,
        checkpoint: NotificationWorkerState | None = None,
    ) -> bool:
        stored = await self.get_event(event.tenant_id, event.id)
        if stored is not None:
            excluded = {
                "etag",
                "fanout_completed_at",
                "fanout_failed_at",
                "fanout_next_attempt_at",
                "updated_at",
            }
            if stored.model_dump(exclude=excluded) != event.model_dump(exclude=excluded):
                raise ConflictError("This event ID already identifies other facts")
            return False
        mutations: list[Mutation] = [(None, event)]
        for item in (watermark, incident, checkpoint):
            if item is not None:
                if item.tenant_id != event.tenant_id:
                    raise ConflictError("Source records and events must belong to the same tenant")
                current = await self._get(type(item), item.tenant_id, item.id)
                if (current is None) != (item.etag is None) or (
                    current is not None and current.etag != item.etag
                ):
                    raise ConflictError(CHANGED)
                mutations.append((current, item))
        await self._commit(event.tenant_id, mutations)
        return True

    async def claim_lease(
        self, tenant_id: str, scope: str, owner: str, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease | None:
        self._claim_limits(lease_seconds, 1)
        identity = notification_record_id("notificationLease", tenant_id, scope)
        current = await self._get(SchedulerLease, tenant_id, identity)
        if current is not None and current.expires_at > now:
            return None
        lease = SchedulerLease(
            id=identity,
            tenant_id=tenant_id,
            scope=scope,
            owner=owner,
            token=uuid4().hex,
            expires_at=now + timedelta(seconds=lease_seconds),
        )
        try:
            await self._commit(tenant_id, [(current, lease)])
        except ConflictError:
            return None
        return await self._get(SchedulerLease, tenant_id, identity)

    async def release_lease(self, lease: SchedulerLease) -> None:
        current = await self._get(SchedulerLease, lease.tenant_id, lease.id)
        if current is None or current.token != lease.token or current.etag != lease.etag:
            raise ConflictError("The scheduler lease changed")
        await self._commit(lease.tenant_id, [(current, None)])

    async def renew_lease(
        self, lease: SchedulerLease, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease:
        self._claim_limits(lease_seconds, 1)
        current = await self._get(SchedulerLease, lease.tenant_id, lease.id)
        if current is None or (
            current.token != lease.token or current.etag != lease.etag or current.expires_at <= now
        ):
            raise ConflictError("The scheduler lease expired or changed")
        updated = current.model_copy(
            update={
                "expires_at": now + timedelta(seconds=lease_seconds),
                "updated_at": now,
            }
        )
        await self._commit(lease.tenant_id, [(current, updated)])
        saved = await self._get(SchedulerLease, lease.tenant_id, lease.id)
        assert saved is not None
        return saved

    async def save_digest_watermark(self, watermark: DigestWatermark) -> DigestWatermark:
        return await self._save(watermark)

    async def list_due_digest_watermarks(
        self, tenant_id: str, *, now: datetime, limit: int = 100
    ) -> list[DigestWatermark]:
        return sorted(
            (
                item
                for item in await self._list(DigestWatermark, tenant_id, "digestWatermark")
                if item.next_due_at is not None and item.next_due_at <= now
            ),
            key=lambda item: item.next_due_at or now,
        )[: min(max(limit, 1), 100)]

    async def get_alert_incident(
        self, tenant_id: str, incident_id: str
    ) -> NotificationAlertIncident | None:
        return await self._get(NotificationAlertIncident, tenant_id, incident_id)

    async def save_alert_incident(
        self, incident: NotificationAlertIncident
    ) -> NotificationAlertIncident:
        return await self._save(incident)

    async def list_channel_health(self, tenant_id: str) -> list[NotificationChannelHealth]:
        return await self._list(NotificationChannelHealth, tenant_id, "notificationChannelHealth")

    async def get_channel_health(
        self, tenant_id: str, identifier: str
    ) -> NotificationChannelHealth | None:
        return await self._get(NotificationChannelHealth, tenant_id, identifier)

    async def save_channel_health(self, health: NotificationChannelHealth) -> None:
        await self._save(health)

    async def get_worker_state(self, tenant_id: str, worker: str) -> NotificationWorkerState | None:
        return await self._get(
            NotificationWorkerState,
            tenant_id,
            notification_record_id("notificationWorker", tenant_id, worker),
        )

    async def update_worker_state(
        self,
        tenant_id: str,
        worker: str,
        change: Callable[[NotificationWorkerState], NotificationWorkerState],
    ) -> NotificationWorkerState:
        for _ in range(5):
            current = await self.get_worker_state(tenant_id, worker)
            state = current or NotificationWorkerState(
                id=notification_record_id("notificationWorker", tenant_id, worker),
                tenant_id=tenant_id,
                worker=worker,
            )
            updated = change(state.model_copy(deep=True)).model_copy(
                update={
                    "id": state.id,
                    "tenant_id": tenant_id,
                    "worker": worker,
                    "etag": state.etag,
                    "updated_at": self._clock(),
                }
            )
            try:
                return await self._save(updated)
            except ConflictError:
                continue
        raise ConflictError(CHANGED)
