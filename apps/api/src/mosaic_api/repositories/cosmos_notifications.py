"""Notification events, claims and projections share the source tenant partition.

This deliberately uses desired-state: approval source events, inbox fan-out and completion
checkpoints can commit in one Cosmos transaction. No second-container outbox dual write.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Any, cast

from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient

from mosaic_api.domain import AuditEvent, utc_now
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.notifications import (
    NotificationCategory,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationDeliveryState,
    NotificationInboxItem,
    NotificationInboxPage,
    NotificationPersona,
)
from mosaic_api.repositories.cosmos import BatchOperation, CosmosRepositoryBase, logger
from mosaic_api.repositories.notification_store import (
    CHANGED,
    Mutation,
    NotificationStore,
    T,
    inbox_after,
    inbox_token,
)


class CosmosNotificationRepository(NotificationStore, CosmosRepositoryBase):
    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
        *,
        owns_client: bool = True,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        NotificationStore.__init__(self, clock=clock)
        CosmosRepositoryBase.__init__(
            self,
            client,
            database_name,
            desired_state_container,
            audit_events_container,
            owns_client=owns_client,
        )

    async def _get(self, model: type[T], tenant: str, item_id: str) -> T | None:
        try:
            item = await self._desired.read_item(item=item_id, partition_key=tenant)
        except exceptions.CosmosResourceNotFoundError:
            return None
        expected_type = model.model_fields["entity_type"].default
        if item.get("tenantId") != tenant or item.get("entityType") != expected_type:
            return None
        return self._model(model, item)

    async def _list(
        self, model: type[T], tenant: str, entity_type: str, *, limit: int | None = None
    ) -> list[T]:
        select = f"SELECT TOP {int(limit)} *" if limit is not None else "SELECT *"
        query = select + " FROM c WHERE c.tenantId = @tenant AND c.entityType = @type"
        if limit is not None and entity_type == "notificationEvent":
            query += " AND (NOT IS_DEFINED(c.fanoutCompletedAt) OR IS_NULL(c.fanoutCompletedAt))"
            query += " AND (NOT IS_DEFINED(c.fanoutFailedAt) OR IS_NULL(c.fanoutFailedAt))"
            query += (
                " AND (NOT IS_DEFINED(c.fanoutNextAttemptAt) OR IS_NULL(c.fanoutNextAttemptAt)"
                " OR c.fanoutNextAttemptAt <= @now)"
            )
        if limit is not None and entity_type == "notificationDelivery":
            query += " AND c.state IN ('queued', 'retrying', 'sending')"
        if limit is not None:
            now = self._clock().isoformat().replace("+00:00", "Z")
            if entity_type == "notificationDelivery":
                query += (
                    " AND (NOT IS_DEFINED(c.nextAttemptAt) OR IS_NULL(c.nextAttemptAt)"
                    " OR c.nextAttemptAt <= @now)"
                    " AND (c.state != 'sending' OR c.leaseExpiresAt <= @now)"
                )
        parameters: list[dict[str, Any]] = [
            {"name": "@tenant", "value": tenant},
            {"name": "@type", "value": entity_type},
        ]
        if limit is not None and entity_type in {"notificationDelivery", "notificationEvent"}:
            parameters.append({"name": "@now", "value": now})
        items = self._desired.query_items(query=query, parameters=parameters, partition_key=tenant)
        return [self._model(model, item) async for item in items]

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
        where = (
            " FROM c WHERE c.tenantId = @tenant AND c.entityType = 'notificationInboxItem'"
            " AND STRINGEQUALS(c.userObjectId, @user, true) AND c.persona = @persona"
        )
        parameters: list[dict[str, Any]] = [
            {"name": "@tenant", "value": tenant_id},
            {"name": "@user", "value": user_object_id},
            {"name": "@persona", "value": persona},
        ]
        unread = " AND (NOT IS_DEFINED(c.readAt) OR IS_NULL(c.readAt))"
        count_items = self._desired.query_items(
            query="SELECT VALUE COUNT(1)" + where + unread,
            parameters=parameters,
            partition_key=tenant_id,
        )
        counts = [cast(int, item) async for item in count_items]
        if unread_only:
            where += unread
        if category is not None:
            where += " AND c.category = @category"
            parameters.append({"name": "@category", "value": str(category)})
        if after:
            where += " AND c.sortKey < @after"
            parameters.append({"name": "@after", "value": after})
        rows = self._desired.query_items(
            query=f"SELECT TOP {limit + 1} *" + where + " ORDER BY c.sortKey DESC",
            parameters=parameters,
            partition_key=tenant_id,
        )
        items = [self._model(NotificationInboxItem, item) async for item in rows]
        page = items[:limit]
        return NotificationInboxPage(
            items=page,
            unread_count=counts[0] if counts else 0,
            continuation_token=(
                inbox_token(scope, page[-1].sort_key) if len(items) > limit else None
            ),
        )

    async def get_backlog(self, tenant_id: str) -> tuple[int, datetime | None]:
        query = (
            "SELECT COUNT(1) AS backlog, MIN(c.createdAt) AS oldest FROM c"
            " WHERE c.tenantId = @tenant AND ("
            "(c.entityType = 'notificationEvent'"
            " AND (NOT IS_DEFINED(c.fanoutCompletedAt) OR IS_NULL(c.fanoutCompletedAt))"
            " AND (NOT IS_DEFINED(c.fanoutFailedAt) OR IS_NULL(c.fanoutFailedAt)))"
            " OR (c.entityType = 'notificationDelivery'"
            " AND c.state IN ('queued', 'retrying', 'sending')))"
        )
        rows = self._desired.query_items(
            query=query,
            parameters=[{"name": "@tenant", "value": tenant_id}],
            partition_key=tenant_id,
        )
        result = [item async for item in rows]
        if not result:
            return 0, None
        oldest = result[0].get("oldest")
        return int(result[0]["backlog"]), (
            datetime.fromisoformat(oldest) if isinstance(oldest, str) else None
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
        query = f"SELECT TOP {limit} * FROM c WHERE c.tenantId = @tenant AND c.entityType = @type"
        parameters: list[dict[str, Any]] = [
            {"name": "@tenant", "value": tenant_id},
            {"name": "@type", "value": "notificationDelivery"},
        ]
        if event_id is not None:
            query += " AND ARRAY_CONTAINS(c.eventIds, @event)"
            parameters.append({"name": "@event", "value": event_id})
        if state is not None:
            query += " AND c.state = @state"
            parameters.append({"name": "@state", "value": state})
        rows = self._desired.query_items(
            query=query, parameters=parameters, partition_key=tenant_id
        )
        return [self._model(NotificationDelivery, row) async for row in rows]

    async def list_delivery_attempts(
        self, tenant_id: str, delivery_id: str
    ) -> list[NotificationDeliveryAttempt]:
        rows = self._desired.query_items(
            query=(
                "SELECT TOP 100 * FROM c WHERE c.tenantId = @tenant"
                " AND c.entityType = @type AND c.deliveryId = @delivery"
                " ORDER BY c.startedAt DESC"
            ),
            parameters=[
                {"name": "@tenant", "value": tenant_id},
                {"name": "@type", "value": "notificationDeliveryAttempt"},
                {"name": "@delivery", "value": delivery_id},
            ],
            partition_key=tenant_id,
        )
        return [self._model(NotificationDeliveryAttempt, row) async for row in rows]

    async def _commit(self, tenant: str, mutations: Sequence[Mutation]) -> None:
        if not 1 <= len(mutations) <= 100:
            raise ValueError("A notification transaction needs 1-100 mutations")
        operations: list[BatchOperation] = []
        keys: set[str] = set()
        for expected, replacement in mutations:
            identity = replacement or expected
            if identity is None or identity.tenant_id != tenant or identity.id in keys:
                raise ConflictError("A transaction must contain unique records in one tenant")
            keys.add(identity.id)
            if expected is not None and not expected.etag:
                raise ConflictError("A conditional notification write requires its read ETag")
            if replacement is None:
                assert expected is not None
                operations.append(self._delete_operation(expected.id, expected))
            elif expected is None:
                operations.append(("create", (self._document(replacement),)))
            else:
                operations.append(
                    self._replace_operation(replacement.model_copy(update={"etag": expected.etag}))
                )
        try:
            await self._desired.execute_item_batch(
                batch_operations=operations, partition_key=tenant
            )
        except exceptions.CosmosBatchOperationError as error:
            response = error.operation_responses[error.error_index]
            if response.get("statusCode") in {404, 409, 412}:
                raise ConflictError(CHANGED) from error
            raise
        for _, replacement in mutations:
            if isinstance(replacement, AuditEvent):
                try:
                    await self._project_audit_event(replacement)
                except exceptions.CosmosHttpResponseError:
                    # The audit remains durable in desired-state and ready() retries projection.
                    logger.warning(
                        "notification_audit_projection_deferred",
                        tenant_id=tenant,
                        audit_event_id=replacement.id,
                    )
