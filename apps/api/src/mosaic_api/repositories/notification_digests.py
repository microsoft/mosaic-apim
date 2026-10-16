"""Bounded digest checkpoints over the existing tenant-scoped atomic notification ports."""

import hashlib
from datetime import datetime, timedelta
from typing import Literal

from pydantic import Field
from pydantic import ValidationError as ModelValidationError

from mosaic_api.domain import MosaicModel
from mosaic_api.errors import ConflictError, DirectoryError
from mosaic_api.notifications import (
    DigestSchedule,
    DigestWindow,
    NotificationCategory,
    NotificationEvent,
    NotificationPersona,
    NotificationWorkerState,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.scheduling import IanaTimeZone, UtcInstant
from mosaic_api.services.notification_events import notification_record_id

MAX_PENDING = 256
MAX_RECEIPTS = 4096
MAX_WINDOW_EVENTS = 100
INDEX_BUCKETS = 64
MAX_BUCKET_STREAMS = 512


class DigestDefinition(MosaicModel):
    recipient_reference: str = Field(min_length=1, max_length=256)
    persona: NotificationPersona | None = None
    channel: Literal["email", "teamsPersonal", "teamsChannel"]
    category: NotificationCategory
    time_zone: IanaTimeZone
    schedule: DigestSchedule

    def identity(self, tenant_id: str) -> str:
        return notification_record_id("notificationDigestStream", tenant_id, self.model_dump_json())


class DigestEventReference(MosaicModel):
    event_id: str = Field(min_length=1, max_length=256)
    source_revision: str = Field(min_length=1, max_length=256)
    occurred_at: UtcInstant
    expires_at: UtcInstant
    route_ids: list[str] = Field(default_factory=list, max_length=16)


class DigestReceipt(MosaicModel):
    token: str
    expires_at: UtcInstant


class DigestStream(MosaicModel):
    definition: DigestDefinition
    pending: list[DigestEventReference] = Field(default_factory=list, max_length=MAX_PENDING)
    receipts: list[DigestReceipt] = Field(default_factory=list, max_length=MAX_RECEIPTS)


class DigestWindowRecord(MosaicModel):
    id: str
    definition: DigestDefinition
    window: DigestWindow
    references: list[DigestEventReference] = Field(min_length=1, max_length=MAX_WINDOW_EVENTS)
    created_at: UtcInstant
    ttl: int = Field(ge=86400)

    @property
    def route_ids(self) -> list[str]:
        return sorted({route for reference in self.references for route in reference.route_ids})


class DigestIndexEntry(MosaicModel):
    stream_id: str
    expires_at: UtcInstant


class DigestIndex(MosaicModel):
    streams: list[DigestIndexEntry] = Field(default_factory=list, max_length=MAX_BUCKET_STREAMS)


class NotificationDigestRepository:
    """No new Cosmos container or private backend access.

    Registration finishes before source fan-out can complete. An immutable prepared window is
    only evidence after its deterministic source event exists. Publishing that event and removing
    pending refs share commit_source_event's tenant transaction; receipts prevent late replay.
    """

    def __init__(self, notifications: NotificationPersistence) -> None:
        self.notifications = notifications

    @staticmethod
    def stream_worker(stream_id: str) -> str:
        return f"notificationDigest:stream:{stream_id}"

    @staticmethod
    def window_worker(window_id: str) -> str:
        return f"notificationDigest:window:{window_id}"

    @staticmethod
    def _bucket(stream_id: str) -> int:
        return int(hashlib.sha256(stream_id.encode()).hexdigest()[:4], 16) % INDEX_BUCKETS

    @staticmethod
    def decode_stream(state: NotificationWorkerState) -> DigestStream:
        try:
            return DigestStream.model_validate_json(state.checkpoint or "")
        except ModelValidationError as exc:
            raise DirectoryError(
                "The durable digest stream is malformed; repair its checkpoint."
            ) from exc

    async def get_stream(
        self, tenant_id: str, stream_id: str
    ) -> tuple[NotificationWorkerState, DigestStream] | None:
        state = await self.notifications.get_worker_state(tenant_id, self.stream_worker(stream_id))
        if state is None:
            return None
        stream = self.decode_stream(state)
        if stream.definition.identity(tenant_id) != stream_id:
            raise DirectoryError("The durable digest stream has a different recipient scope.")
        return state, stream

    async def register(
        self,
        tenant_id: str,
        definition: DigestDefinition,
        reference: DigestEventReference,
        *,
        now: datetime,
        ttl: int,
    ) -> None:
        identifier = definition.identity(tenant_id)
        token = hashlib.sha256(reference.event_id.encode()).hexdigest()

        def append(state: NotificationWorkerState) -> NotificationWorkerState:
            stream = (
                self.decode_stream(state)
                if state.checkpoint
                else DigestStream(definition=definition)
            )
            if stream.definition != definition:
                raise ConflictError("The digest stream identifies a different schedule.")
            receipts = [receipt for receipt in stream.receipts if receipt.expires_at > now]
            pending = list(stream.pending)
            matching = next((item for item in pending if item.event_id == reference.event_id), None)
            if matching:
                merged = sorted(set(matching.route_ids) | set(reference.route_ids))
                if len(merged) > 16:
                    raise ConflictError("A digest event exceeds its bounded route-reference limit.")
                pending = [
                    item.model_copy(update={"route_ids": merged}) if item == matching else item
                    for item in pending
                ]
            elif not any(receipt.token == token for receipt in receipts):
                if len(pending) >= MAX_PENDING or len(receipts) >= MAX_RECEIPTS:
                    raise ConflictError(
                        "The digest backlog is full; reconcile it before retrying fan-out."
                    )
                pending.append(reference)
                receipts.append(DigestReceipt(token=token, expires_at=reference.expires_at))
            return state.model_copy(
                update={
                    "checkpoint": DigestStream(
                        definition=definition, pending=pending, receipts=receipts
                    ).model_dump_json(),
                    "backlog": len(pending),
                    "oldest_queued_at": min((item.occurred_at for item in pending), default=None),
                    "ttl": max(ttl, state.ttl or 0),
                }
            )

        await self.notifications.update_worker_state(
            tenant_id, self.stream_worker(identifier), append
        )
        bucket = self._bucket(identifier)

        def index(state: NotificationWorkerState) -> NotificationWorkerState:
            current = self._index(state)
            identifiers = {
                item.stream_id: item for item in current.streams if item.expires_at > now
            }
            expiry = reference.expires_at + timedelta(days=1)
            previous = identifiers.get(identifier)
            identifiers[identifier] = DigestIndexEntry(
                stream_id=identifier,
                expires_at=max(expiry, previous.expires_at) if previous else expiry,
            )
            if len(identifiers) > MAX_BUCKET_STREAMS:
                raise ConflictError("The digest recipient index is full; expire old streams first.")
            return state.model_copy(
                update={
                    "checkpoint": DigestIndex(
                        streams=[identifiers[key] for key in sorted(identifiers)]
                    ).model_dump_json()
                }
            )

        await self.notifications.update_worker_state(
            tenant_id, f"notificationDigest:index:{bucket}", index
        )

    @staticmethod
    def _index(state: NotificationWorkerState) -> DigestIndex:
        try:
            return (
                DigestIndex.model_validate_json(state.checkpoint)
                if state.checkpoint
                else DigestIndex()
            )
        except ModelValidationError as exc:
            raise DirectoryError("The durable digest index is malformed.") from exc

    async def list_streams(self, tenant_id: str, *, now: datetime | None = None) -> list[str]:
        identifiers: set[str] = set()
        for bucket in range(INDEX_BUCKETS):
            state = await self.notifications.get_worker_state(
                tenant_id, f"notificationDigest:index:{bucket}"
            )
            if state:
                identifiers.update(
                    item.stream_id
                    for item in self._index(state).streams
                    if now is None or item.expires_at > now
                )
        return sorted(identifiers)

    async def save_window(self, tenant_id: str, record: DigestWindowRecord) -> DigestWindowRecord:
        def prepare(state: NotificationWorkerState) -> NotificationWorkerState:
            if state.checkpoint:
                old = self._window(state)
                if old.model_dump(exclude={"created_at", "ttl"}) != record.model_dump(
                    exclude={"created_at", "ttl"}
                ):
                    raise ConflictError(
                        "A digest window already identifies different source events."
                    )
                return state
            return state.model_copy(
                update={"checkpoint": record.model_dump_json(), "ttl": record.ttl}
            )

        state = await self.notifications.update_worker_state(
            tenant_id, self.window_worker(record.id), prepare
        )
        return self._window(state)

    @staticmethod
    def _window(state: NotificationWorkerState) -> DigestWindowRecord:
        try:
            return DigestWindowRecord.model_validate_json(state.checkpoint or "")
        except ModelValidationError as exc:
            raise DirectoryError("The durable digest window is malformed.") from exc

    async def get_window(self, tenant_id: str, window_id: str) -> DigestWindowRecord | None:
        state = await self.notifications.get_worker_state(tenant_id, self.window_worker(window_id))
        return self._window(state) if state else None

    async def advance(
        self,
        event: NotificationEvent,
        state: NotificationWorkerState,
        stream: DigestStream,
        consumed: set[str],
    ) -> bool:
        remaining = [
            reference for reference in stream.pending if reference.event_id not in consumed
        ]
        updated = stream.model_copy(update={"pending": remaining})
        checkpoint = state.model_copy(
            update={
                "checkpoint": updated.model_dump_json(),
                "backlog": len(remaining),
                "oldest_queued_at": min((item.occurred_at for item in remaining), default=None),
            }
        )
        return await self.notifications.commit_source_event(event, checkpoint=checkpoint)

    async def discard(
        self,
        tenant_id: str,
        stream_id: str,
        consumed: set[str],
        *,
        now: datetime,
        error: str | None,
    ) -> None:
        def remove(state: NotificationWorkerState) -> NotificationWorkerState:
            stream = self.decode_stream(state)
            remaining = [item for item in stream.pending if item.event_id not in consumed]
            receipts = [receipt for receipt in stream.receipts if receipt.expires_at > now]
            return state.model_copy(
                update={
                    "checkpoint": stream.model_copy(
                        update={"pending": remaining, "receipts": receipts}
                    ).model_dump_json(),
                    "backlog": len(remaining),
                    "last_error": error,
                }
            )

        await self.notifications.update_worker_state(
            tenant_id, self.stream_worker(stream_id), remove
        )
