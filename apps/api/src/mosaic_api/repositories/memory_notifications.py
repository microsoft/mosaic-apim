"""Copy-on-read notification storage with the same CAS transactions as Cosmos."""

from collections.abc import Callable, Sequence
from datetime import datetime
from uuid import uuid4

from mosaic_api.domain import AuditEvent, Entity, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import NotificationDelivery, NotificationEvent
from mosaic_api.repositories.notification_store import CHANGED, Mutation, NotificationStore, T


class InMemoryNotificationRepository(NotificationStore):
    def __init__(
        self,
        *,
        events: dict[tuple[str, str], NotificationEvent] | None = None,
        audit_events: dict[str, AuditEvent] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(clock=clock)
        self.events = events if events is not None else {}
        self.audit_events = audit_events if audit_events is not None else {}
        self.records: dict[tuple[str, str], Entity] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    def _current(self, tenant: str, item_id: str) -> Entity | None:
        return self.events.get((tenant, item_id)) or self.records.get((tenant, item_id))

    async def _get(self, model: type[T], tenant: str, item_id: str) -> T | None:
        item = self._current(tenant, item_id)
        if not isinstance(item, model) or item.tenant_id != tenant:
            return None
        return item.model_copy(deep=True)

    async def _list(
        self, model: type[T], tenant: str, entity_type: str, *, limit: int | None = None
    ) -> list[T]:
        source = self.events if model is NotificationEvent else self.records
        items = [
            item.model_copy(deep=True)
            for (partition, _), item in source.items()
            if partition == tenant
            and isinstance(item, model)
            and (
                limit is None
                or not isinstance(item, NotificationEvent)
                or (
                    item.fanout_completed_at is None
                    and item.fanout_failed_at is None
                    and (
                        item.fanout_next_attempt_at is None
                        or item.fanout_next_attempt_at <= self._clock()
                    )
                )
            )
            and (
                limit is None
                or not isinstance(item, NotificationDelivery)
                or (
                    item.state in {"queued", "retrying", "sending"}
                    and (item.next_attempt_at is None or item.next_attempt_at <= self._clock())
                    and (
                        item.state != "sending"
                        or (
                            item.lease_expires_at is not None
                            and item.lease_expires_at <= self._clock()
                        )
                    )
                )
            )
        ]
        items.sort(key=lambda item: (item.created_at, item.id))
        return items if limit is None else items[:limit]

    async def _commit(self, tenant: str, mutations: Sequence[Mutation]) -> None:
        if not 1 <= len(mutations) <= 100:
            raise ValueError("A notification transaction needs 1-100 mutations")
        keys: set[str] = set()
        for expected, replacement in mutations:
            identity = replacement or expected
            if identity is None or identity.tenant_id != tenant or identity.id in keys:
                raise ConflictError("A transaction must contain unique records in one tenant")
            keys.add(identity.id)
            current = self._current(tenant, identity.id)
            if (expected is None) != (current is None) or (
                expected is not None and current is not None and expected.etag != current.etag
            ):
                raise ConflictError(CHANGED)
        for expected, replacement in mutations:
            identity = replacement or expected
            assert identity is not None
            key = (tenant, identity.id)
            if replacement is None:
                self.events.pop(key, None)
                self.records.pop(key, None)
            else:
                stored = replacement.model_copy(update={"etag": uuid4().hex}, deep=True)
                if isinstance(stored, NotificationEvent):
                    self.events[key] = stored
                else:
                    self.records[key] = stored
                    if isinstance(stored, AuditEvent):
                        self.audit_events[stored.id] = stored.model_copy(deep=True)
