"""Durable notification ports, all explicitly tenant-scoped.

Personal API services infer object ID/persona from authentication, never from submitted IDs.
Repositories enforce that scope as well. Saved settings are CAS-conditional and audited; state
updates, delivery claims and lease tokens must reject stale workers.
"""

from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Protocol

from mosaic_api.domain import AuditEvent
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


class NotificationRepository(Protocol):
    async def ready(self) -> bool: ...

    async def close(self) -> None: ...

    async def get_preferences(
        self, tenant_id: str, user_object_id: str, persona: NotificationPersona
    ) -> NotificationPreferences | None: ...

    async def list_preferences(self, tenant_id: str) -> list[NotificationPreferences]: ...

    async def save_preferences(
        self, preferences: NotificationPreferences, audit_event: AuditEvent
    ) -> NotificationPreferences: ...

    async def list_routes(self, tenant_id: str) -> list[NotificationRoute]: ...

    async def get_route(self, tenant_id: str, route_id: str) -> NotificationRoute | None: ...

    async def save_route(
        self, route: NotificationRoute, audit_event: AuditEvent
    ) -> NotificationRoute: ...

    async def delete_route(self, route: NotificationRoute, audit_event: AuditEvent) -> None: ...

    async def get_channel_settings(self, tenant_id: str) -> NotificationChannelSettings | None: ...

    async def save_channel_settings(
        self, settings: NotificationChannelSettings, audit_event: AuditEvent
    ) -> NotificationChannelSettings: ...

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
        """Opaque continuation tokens must be bound to user, tenant, persona, and filters."""
        ...

    async def set_inbox_read(
        self,
        tenant_id: str,
        user_object_id: str,
        persona: NotificationPersona,
        item_id: str,
        *,
        read_at: datetime | None,
    ) -> NotificationInboxItem: ...

    async def get_inbox_item(
        self, tenant_id: str, user_object_id: str, persona: NotificationPersona, item_id: str
    ) -> NotificationInboxItem | None: ...

    async def list_teams_destinations(self, tenant_id: str) -> list[TeamsDestination]: ...

    async def get_teams_destination(
        self, tenant_id: str, destination_id: str
    ) -> TeamsDestination | None: ...

    async def save_teams_destination(
        self, destination: TeamsDestination, audit_event: AuditEvent
    ) -> TeamsDestination: ...

    async def delete_teams_destination(
        self, destination: TeamsDestination, audit_event: AuditEvent
    ) -> None: ...

    async def get_personal_teams_settings(self, tenant_id: str) -> PersonalTeamsSettings | None: ...

    async def save_personal_teams_settings(
        self, settings: PersonalTeamsSettings, audit_event: AuditEvent
    ) -> PersonalTeamsSettings: ...

    async def get_personal_teams_binding(
        self, tenant_id: str, user_object_id: str
    ) -> PersonalTeamsBinding | None: ...

    async def save_personal_teams_binding(
        self, binding: PersonalTeamsBinding
    ) -> PersonalTeamsBinding:
        """Only an authenticated Teams lifecycle handler calls this, never a portal update."""
        ...


class NotificationDeliveryRepository(Protocol):
    """Event IDs, inbox IDs and delivery IDs are deterministic; duplicate creates are no-ops.

    Approval events are written by AccessRequestEventRepository in the source transaction.
    Cross-container producers reconcile from durable checkpoints, reusing the same event ID.
    """

    async def append_event(self, event: NotificationEvent) -> bool:
        """Create if absent; return False for an identical existing event, conflict if different."""
        ...

    async def import_budget_delivery(
        self, event: NotificationEvent, delivery: NotificationDelivery
    ) -> None:
        """Idempotently project an owned source notice and its provider state, never resend."""
        ...

    async def get_event(self, tenant_id: str, event_id: str) -> NotificationEvent | None: ...

    async def list_outbox_states(
        self, tenant_id: str, *, failed_only: bool = True, limit: int = 50
    ) -> list[NotificationOutboxState]: ...

    async def requeue_event(
        self, tenant_id: str, event_id: str, audit_event: AuditEvent
    ) -> NotificationEvent: ...

    async def claim_events(
        self,
        tenant_id: str,
        owner: str,
        *,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[NotificationOutboxClaim]:
        """Include unprocessed source events and expired claims; use conditional writes."""
        ...

    async def save_fanout(
        self,
        claim: NotificationOutboxClaim,
        inbox_items: Sequence[NotificationInboxItem],
        deliveries: Sequence[NotificationDelivery],
        *,
        complete: bool,
    ) -> None:
        """Idempotently save bounded fan-out, and checkpoint only under the current lease.

        Partial fan-out may be replayed; completion is marked only after every recipient is
        durable. Recheck recipient authorization/preferences before any external send.
        """
        ...

    async def fail_fanout(
        self,
        claim: NotificationOutboxClaim,
        *,
        error: str,
        next_attempt_at: datetime,
        permanent: bool = False,
    ) -> None:
        """Release the current claim with a durable retry checkpoint and safe diagnostic."""
        ...

    async def claim_deliveries(
        self,
        tenant_id: str,
        owner: str,
        *,
        now: datetime,
        lease_seconds: int,
        limit: int,
    ) -> list[NotificationDelivery]:
        """Do not blindly resend expired unknown provider outcomes without deduplication."""
        ...

    async def save_delivery(
        self, delivery: NotificationDelivery, attempt: NotificationDeliveryAttempt | None = None
    ) -> NotificationDelivery:
        """Condition on the claim token/ETag and commit attempt/state together."""
        ...

    async def renew_delivery_lease(
        self, delivery: NotificationDelivery, *, now: datetime, lease_seconds: int
    ) -> NotificationDelivery: ...

    async def list_deliveries(
        self,
        tenant_id: str,
        *,
        event_id: str | None = None,
        state: NotificationDeliveryState | None = None,
        limit: int = 50,
    ) -> list[NotificationDelivery]: ...

    async def get_delivery(
        self, tenant_id: str, delivery_id: str
    ) -> NotificationDelivery | None: ...

    async def list_delivery_attempts(
        self, tenant_id: str, delivery_id: str
    ) -> list[NotificationDeliveryAttempt]: ...

    async def get_backlog(self, tenant_id: str) -> tuple[int, datetime | None]: ...

    async def requeue_delivery(
        self, delivery: NotificationDelivery, audit_event: AuditEvent | None = None
    ) -> NotificationDelivery:
        """CAS retry of a terminal job. Service checks ambiguous-send safety first."""
        ...

    async def commit_source_event(
        self,
        event: NotificationEvent,
        *,
        watermark: DigestWatermark | None = None,
        incident: NotificationAlertIncident | None = None,
        checkpoint: NotificationWorkerState | None = None,
    ) -> bool:
        """Atomically create an event with a source checkpoint, incident and/or digest window."""
        ...

    async def claim_lease(
        self, tenant_id: str, scope: str, owner: str, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease | None: ...

    async def release_lease(self, lease: SchedulerLease) -> None: ...

    async def renew_lease(
        self, lease: SchedulerLease, *, now: datetime, lease_seconds: int
    ) -> SchedulerLease: ...

    async def get_digest_watermark(
        self, tenant_id: str, watermark_id: str
    ) -> DigestWatermark | None: ...

    async def save_digest_watermark(self, watermark: DigestWatermark) -> DigestWatermark: ...

    async def list_due_digest_watermarks(
        self, tenant_id: str, *, now: datetime, limit: int = 100
    ) -> list[DigestWatermark]: ...

    async def get_alert_incident(
        self, tenant_id: str, incident_id: str
    ) -> NotificationAlertIncident | None: ...

    async def save_alert_incident(
        self, incident: NotificationAlertIncident
    ) -> NotificationAlertIncident: ...

    async def list_channel_health(self, tenant_id: str) -> list[NotificationChannelHealth]: ...

    async def save_channel_health(self, health: NotificationChannelHealth) -> None: ...

    async def get_worker_state(
        self, tenant_id: str, worker: str
    ) -> NotificationWorkerState | None: ...

    async def update_worker_state(
        self,
        tenant_id: str,
        worker: str,
        change: Callable[[NotificationWorkerState], NotificationWorkerState],
    ) -> NotificationWorkerState: ...


class NotificationPersistence(NotificationRepository, NotificationDeliveryRepository, Protocol):
    """One backend supplies desired settings and atomic operational state."""
