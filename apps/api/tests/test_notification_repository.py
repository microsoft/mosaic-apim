"""The same notification invariants against memory and a conditional Cosmos batch double."""

import asyncio
import copy
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.notifications import (
    DigestWatermark,
    NotificationAudience,
    NotificationCategory,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationInboxItem,
    NotificationPreferences,
    NotificationSubject,
)
from mosaic_api.repositories.cosmos_notifications import CosmosNotificationRepository
from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.notification_events import (
    build_notification_event,
    notification_preferences_id,
)

NOW = datetime(2026, 10, 16, 16, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class NotificationContainer:
    """An atomic batch double: failed operations must not persist earlier operations."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.version = 0
        self.conflict_next = False
        self.conflict_index: int | None = None
        self.batches: list[list[Any]] = []
        self.queries: list[str] = []

    async def read(self) -> dict[str, Any]:
        return {}

    async def read_item(self, *, item: str, partition_key: str) -> dict[str, Any]:
        if (partition_key, item) not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        return copy.deepcopy(self.items[partition_key, item])

    async def create_item(self, body: dict[str, Any]) -> dict[str, Any]:
        key = (body["tenantId"], body["id"])
        if key in self.items:
            raise exceptions.CosmosResourceExistsError(status_code=409)
        self.version += 1
        self.items[key] = {**body, "_etag": str(self.version)}
        return self.items[key]

    async def delete_item(self, *, item: str, partition_key: str) -> None:
        if (partition_key, item) not in self.items:
            raise exceptions.CosmosResourceNotFoundError(status_code=404)
        del self.items[partition_key, item]

    async def execute_item_batch(
        self, *, batch_operations: list[Any], partition_key: str
    ) -> list[dict[str, Any]]:
        self.batches.append(batch_operations)
        staged = copy.deepcopy(self.items)
        for index, operation in enumerate(batch_operations):
            verb, args = operation[:2]
            options = operation[2] if len(operation) > 2 else {}
            document = args[0] if verb == "create" else (args[1] if verb == "replace" else None)
            identity = document["id"] if verb == "create" else args[0]
            key = (partition_key, identity)
            current = staged.get(key)
            status = None
            if self.conflict_next or self.conflict_index == index:
                self.conflict_next = False
                self.conflict_index = None
                status = 412
            elif verb == "create" and current is not None:
                status = 409
            elif verb != "create" and current is None:
                status = 404
            elif verb != "create" and options.get("if_match_etag") != current["_etag"]:
                status = 412
            if status is not None:
                raise exceptions.CosmosBatchOperationError(
                    error_index=index,
                    headers={},
                    status_code=status,
                    operation_responses=[
                        {"statusCode": status if number == index else 424}
                        for number in range(len(batch_operations))
                    ],
                )
            if verb == "delete":
                staged.pop(key)
            else:
                assert document is not None and document["tenantId"] == partition_key
                self.version += 1
                staged[key] = {**document, "_etag": str(self.version)}
        self.items = staged
        return []

    def query_items(
        self,
        *,
        query: str,
        parameters: list[dict[str, Any]],
        partition_key: str | None = None,
    ) -> AsyncIterator[Any]:
        self.queries.append(query)
        values = {parameter["name"]: parameter["value"] for parameter in parameters}
        items = [
            copy.deepcopy(item)
            for (tenant, _), item in self.items.items()
            if partition_key is None or tenant == partition_key
        ]
        if "AS backlog" in query:
            queued = [
                item
                for item in items
                if (
                    item["entityType"] == "notificationEvent"
                    and not item.get("fanoutCompletedAt")
                    and not item.get("fanoutFailedAt")
                )
                or (
                    item["entityType"] == "notificationDelivery"
                    and item["state"] in {"queued", "retrying", "sending"}
                )
            ]

            async def aggregate() -> AsyncIterator[Any]:
                yield {
                    "backlog": len(queued),
                    "oldest": min((item["createdAt"] for item in queued), default=None),
                }

            return aggregate()
        if "@type" in values:
            items = [item for item in items if item["entityType"] == values["@type"]]
        elif "'notificationInboxItem'" in query:
            items = [item for item in items if item["entityType"] == "notificationInboxItem"]
        elif "@entityType" in values:
            items = [item for item in items if item["entityType"] == values["@entityType"]]
        if "@user" in values:
            items = [
                item
                for item in items
                if item["userObjectId"].casefold() == values["@user"].casefold()
                and item["persona"] == values["@persona"]
            ]
        if "@category" in values:
            items = [item for item in items if item["category"] == values["@category"]]
        if "@event" in values:
            items = [item for item in items if values["@event"] in item["eventIds"]]
        if "@state" in values:
            items = [item for item in items if item["state"] == values["@state"]]
        if "@delivery" in values:
            items = [item for item in items if item["deliveryId"] == values["@delivery"]]
        if "@after" in values:
            items = [item for item in items if item["sortKey"] < values["@after"]]
        if "c.readAt" in query:
            items = [item for item in items if item.get("readAt") is None]
        if "c.fanoutCompletedAt" in query:
            items = [
                item
                for item in items
                if not item.get("fanoutCompletedAt")
                and not item.get("fanoutFailedAt")
                and (
                    not item.get("fanoutNextAttemptAt")
                    or item["fanoutNextAttemptAt"] <= values["@now"]
                )
            ]
        if "c.state IN" in query:
            items = [
                item
                for item in items
                if item["state"] in {"queued", "retrying", "sending"}
                and (not item.get("nextAttemptAt") or item["nextAttemptAt"] <= values["@now"])
                and (item["state"] != "sending" or item["leaseExpiresAt"] <= values["@now"])
            ]
        if "ORDER BY c.sortKey DESC" in query:
            items.sort(key=lambda item: item["sortKey"], reverse=True)
        if "ORDER BY c.startedAt DESC" in query:
            items.sort(key=lambda item: item["startedAt"], reverse=True)
        if "COUNT(1)" in query:
            results: list[Any] = [len(items)]
        else:
            top = re.search(r"TOP (\d+)", query)
            results = items if top is None else items[: int(top.group(1))]

        async def iterate() -> AsyncIterator[Any]:
            for item in results:
                yield item

        return iterate()


class NotificationCosmos:
    def __init__(self) -> None:
        self.container = NotificationContainer()
        self.audit = NotificationContainer()

    def get_database_client(self, _name: str) -> "NotificationCosmos":
        return self

    def get_container_client(self, name: str) -> NotificationContainer:
        return self.audit if name == "audit" else self.container

    async def close(self) -> None:
        return None


@pytest.fixture(params=["memory", "cosmos"])
def store(request: pytest.FixtureRequest) -> tuple[NotificationPersistence, Clock]:
    clock = Clock()
    repository: NotificationPersistence
    if request.param == "memory":
        repository = InMemoryNotificationRepository(clock=clock)
    else:
        repository = CosmosNotificationRepository(
            cast(CosmosClient, NotificationCosmos()), "db", "desired", "audit", clock=clock
        )
    return repository, clock


def event(identity: str = "request", *, tenant: str = "tenant") -> NotificationEvent:
    return build_notification_event(
        tenant_id=tenant,
        event_type=NotificationEventType.REQUEST_SUBMITTED,
        source_id=identity,
        source_revision="1",
        occurred_at=NOW,
        origin="test",
        subject=NotificationSubject(request_id=identity),
        audience=NotificationAudience(user_object_ids=["user"]),
    )


def inbox(item_id: str, source: NotificationEvent, user: str = "user") -> NotificationInboxItem:
    return NotificationInboxItem(
        id=item_id,
        tenant_id=source.tenant_id,
        user_object_id=user,
        persona="portal",
        event_id=source.id,
        category=source.category,
        title="Request submitted",
        summary="Your request was submitted.",
        subject=source.subject,
        occurred_at=NOW,
    )


async def seed_fanout(
    repository: NotificationPersistence, *, identity: str = "request", user: str = "user"
) -> tuple[NotificationEvent, NotificationInboxItem, NotificationDelivery]:
    source = event(identity)
    await repository.append_event(source)
    [claim] = await repository.claim_events("tenant", "worker", now=NOW, lease_seconds=60, limit=1)
    item = inbox(f"inbox-{identity}", source, user)
    delivery = NotificationDelivery(
        id=f"delivery-{identity}",
        tenant_id="tenant",
        event_ids=[source.id],
        channel="email",
        recipient_reference=user,
        persona="portal",
    )
    await repository.save_fanout(claim, [item], [delivery], complete=True)
    return source, item, delivery


async def test_event_claim_fanout_is_atomic_and_idempotent(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, _ = store
    source = event()
    assert await repository.append_event(source)
    assert not await repository.append_event(source)
    with pytest.raises(ConflictError, match="other facts"):
        await repository.append_event(source.model_copy(update={"origin": "different"}))
    results = await asyncio.gather(
        *[
            repository.claim_events("tenant", owner, now=NOW, lease_seconds=60, limit=1)
            for owner in ("one", "two")
        ]
    )
    assert sorted(len(items) for items in results) == [0, 1]
    claim = next(items[0] for items in results if items)
    item = inbox("inbox", source)
    await repository.save_fanout(claim, [item], [], complete=False)
    await repository.set_inbox_read("tenant", "user", "portal", item.id, read_at=NOW)
    await repository.save_fanout(claim, [item], [], complete=True)
    page = await repository.list_inbox("tenant", "user", "portal")
    assert page.items[0].read_at == NOW
    assert not await repository.append_event(source)
    assert not await repository.claim_events("tenant", "three", now=NOW, lease_seconds=60, limit=1)
    with pytest.raises(ConflictError, match="lease"):
        await repository.save_fanout(claim, [], [], complete=True)


async def test_expired_fanout_claim_is_fenced(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    source = event()
    await repository.append_event(source)
    [old] = await repository.claim_events("tenant", "old", now=NOW, lease_seconds=60, limit=1)
    clock.now += timedelta(minutes=2)
    [new] = await repository.claim_events("tenant", "new", now=clock.now, lease_seconds=60, limit=1)
    assert old.state.lease_token != new.state.lease_token
    with pytest.raises(ConflictError, match="lease"):
        await repository.save_fanout(old, [inbox("leaked", source)], [], complete=True)
    assert not (await repository.list_inbox("tenant", "user", "portal")).items
    await repository.save_fanout(new, [inbox("valid", source)], [], complete=True)


async def test_fanout_and_inbox_do_not_cross_identity_or_tenant(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, _ = store
    _, item, _ = await seed_fanout(repository)
    for tenant, user, persona in (
        ("other", "user", "portal"),
        ("tenant", "other", "portal"),
        ("tenant", "user", "admin"),
    ):
        assert not (await repository.list_inbox(tenant, user, persona)).items
        with pytest.raises(NotFoundError):
            await repository.set_inbox_read(tenant, user, persona, item.id, read_at=NOW)
    assert await repository.get_event("other", event().id) is None


async def test_keyset_pagination_is_filter_and_persona_bound(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, _ = store
    for identity in ("a", "b", "c"):
        await seed_fanout(repository, identity=identity)
    first = await repository.list_inbox("tenant", "USER", "portal", limit=1)
    assert first.unread_count == 3 and first.continuation_token
    second = await repository.list_inbox(
        "tenant", "user", "portal", limit=2, continuation_token=first.continuation_token
    )
    assert len({item.id for item in first.items + second.items}) == 3
    assert second.continuation_token is None
    for arguments in (
        {"user_object_id": "other"},
        {"persona": "admin"},
        {"unread_only": True},
        {"category": NotificationCategory.ACCESS_LIFECYCLE},
    ):
        with pytest.raises(ValidationError, match="token"):
            await repository.list_inbox(
                **{
                    "tenant_id": "tenant",
                    "user_object_id": "user",
                    "persona": "portal",
                    "continuation_token": first.continuation_token,
                    **arguments,
                }
            )
    for token in ("not-base64", "[]", "!" * 5000):
        with pytest.raises(ValidationError):
            await repository.list_inbox("tenant", "user", "portal", continuation_token=token)


async def test_delivery_claim_and_crash_never_automatically_resend_unknown(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    _, _, delivery = await seed_fanout(repository)
    [claimed] = await repository.claim_deliveries(
        "tenant", "worker", now=NOW, lease_seconds=60, limit=1
    )
    assert claimed.provider_operation_id and claimed.attempts == 1
    assert not await repository.claim_deliveries(
        "tenant", "other", now=NOW, lease_seconds=60, limit=1
    )
    clock.now += timedelta(minutes=2)
    assert not await repository.claim_deliveries(
        "tenant", "other", now=clock.now, lease_seconds=60, limit=1
    )
    stored = await repository.get_delivery("tenant", delivery.id)
    assert stored and stored.state == "unknownOutcome"
    with pytest.raises(ConflictError, match="lease"):
        await repository.save_delivery(
            claimed.model_copy(
                update={"state": "providerAccepted", "provider_accepted_at": clock.now}
            )
        )
    assert not await repository.claim_deliveries(
        "tenant", "restart", now=clock.now, lease_seconds=60, limit=1
    )


async def test_source_checkpoint_and_event_commit_or_conflict_together(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, _ = store
    watermark = DigestWatermark(
        id="watermark",
        tenant_id="tenant",
        recipient_reference="user",
        persona="portal",
        channel="email",
        schedule_revision=1,
        period="daily",
        next_due_at=NOW,
    )
    saved = await repository.save_digest_watermark(watermark)
    assert (await repository.list_due_digest_watermarks("tenant", now=NOW))[0].id == "watermark"
    changed = saved.model_copy(update={"last_completed_end_at": NOW})
    source = event("digest")
    assert await repository.commit_source_event(source, watermark=changed)
    assert not await repository.commit_source_event(source, watermark=changed)
    with pytest.raises(ConflictError):
        await repository.commit_source_event(event("next"), watermark=changed)
    assert await repository.get_event("tenant", event("next").id) is None
    assert await repository.get_digest_watermark("other", "watermark") is None


async def test_preferences_and_scheduler_leases_are_conditional(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    preferences = NotificationPreferences(
        id=notification_preferences_id("tenant", "user", "portal"),
        tenant_id="tenant",
        user_object_id="user",
        persona="portal",
    )
    audit = AuditEvent(
        id="audit-one",
        tenant_id="tenant",
        action="notification.preferencesUpdated",
        resource_type="notificationPreferences",
        resource_id=preferences.id,
        actor_object_id="user",
    )
    saved = await repository.save_preferences(preferences, audit)
    assert saved.etag
    with pytest.raises(ConflictError):
        await repository.save_preferences(preferences, audit.model_copy(update={"id": "audit-two"}))
    assert await repository.get_preferences("tenant", "user", "admin") is None
    lease = await repository.claim_lease("tenant", "digest", "one", now=NOW, lease_seconds=60)
    assert lease
    assert (
        await repository.claim_lease("tenant", "digest", "two", now=NOW, lease_seconds=60) is None
    )
    clock.now += timedelta(minutes=2)
    replacement = await repository.claim_lease(
        "tenant", "digest", "two", now=clock.now, lease_seconds=60
    )
    assert replacement
    with pytest.raises(ConflictError):
        await repository.release_lease(lease)
    renewed = await repository.renew_lease(replacement, now=clock.now, lease_seconds=120)
    assert renewed.expires_at == clock.now + timedelta(seconds=120)
    with pytest.raises(ConflictError):
        await repository.release_lease(replacement)
    await repository.release_lease(renewed)
    assert len(await repository.list_preferences("tenant")) == 1
    assert await repository.list_preferences("other") == []


async def test_failed_fanout_is_visible_and_retry_is_audited_and_fenced(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    source = event()
    await repository.append_event(source)
    [claim] = await repository.claim_events(
        "tenant", "worker", now=clock.now, lease_seconds=60, limit=1
    )
    await repository.fail_fanout(
        claim,
        error="Directory permission is missing.",
        permanent=True,
        next_attempt_at=clock.now,
    )
    [dead_letter] = await repository.list_outbox_states("tenant")
    assert dead_letter.last_error == "Directory permission is missing."
    assert not await repository.list_outbox_states("other")
    audit = AuditEvent(
        id="audit-retry",
        tenant_id="tenant",
        action="notification.fanoutRetried",
        resource_type="notificationEvent",
        resource_id=source.id,
        actor_object_id="admin",
    )
    retried = await repository.requeue_event("tenant", source.id, audit)
    assert retried.fanout_failed_at is None
    assert not await repository.list_outbox_states("tenant")
    [fresh] = await repository.claim_events(
        "tenant", "new", now=clock.now, lease_seconds=60, limit=1
    )
    assert fresh.state.attempts == 1 and fresh.state.lease_token != claim.state.lease_token
    with pytest.raises(ConflictError):
        await repository.requeue_event("tenant", source.id, audit)


async def test_cosmos_transaction_conflict_leaves_no_partial_fanout_or_checkpoint() -> None:
    cosmos = NotificationCosmos()
    repository = CosmosNotificationRepository(
        cast(CosmosClient, cosmos), "db", "desired", "audit", clock=Clock()
    )
    source = event()
    await repository.append_event(source)
    [claim] = await repository.claim_events("tenant", "worker", now=NOW, lease_seconds=60, limit=1)
    cosmos.container.conflict_index = 1
    with pytest.raises(ConflictError):
        await repository.save_fanout(claim, [inbox("inbox", source)], [], complete=True)
    assert not (await repository.list_inbox("tenant", "user", "portal")).items
    stored = await repository.get_event("tenant", source.id)
    assert stored and stored.fanout_completed_at is None
    await repository.save_fanout(claim, [inbox("inbox", source)], [], complete=True)
    assert len((await repository.list_inbox("tenant", "user", "portal")).items) == 1


async def test_source_memory_events_and_audit_are_shared_without_copying_containers() -> None:
    source = event()
    events = {("tenant", source.id): source}
    audit_events: dict[str, AuditEvent] = {}
    repository = InMemoryNotificationRepository(
        events=events, audit_events=audit_events, clock=Clock()
    )
    [claim] = await repository.claim_events("tenant", "worker", now=NOW, lease_seconds=60, limit=1)
    await repository.save_fanout(claim, [inbox("inbox", source)], [], complete=True)
    assert events["tenant", source.id].fanout_completed_at == NOW
    preferences = NotificationPreferences(
        id=notification_preferences_id("tenant", "user", "portal"),
        tenant_id="tenant",
        user_object_id="user",
        persona="portal",
    )
    audit = AuditEvent(
        id="audit",
        tenant_id="tenant",
        action="notification.preferencesUpdated",
        resource_type="notificationPreferences",
        resource_id=preferences.id,
        actor_object_id="user",
    )
    await repository.save_preferences(preferences, audit)
    assert audit_events["audit"].actor_object_id == "user"
