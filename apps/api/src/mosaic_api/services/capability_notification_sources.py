"""Persist capability transitions before publishing, so a crash can safely replay them."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from mosaic_api.domain import utc_now
from mosaic_api.notifications import (
    CapabilityCheck,
    NotificationAudience,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
    NotificationWorkerState,
)
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.notification_events import (
    build_notification_event,
    notification_record_id,
)


class CapabilityNotificationSource:
    def __init__(
        self,
        repository: NotificationDeliveryRepository,
        probe: Callable[[], Awaitable[list[CapabilityCheck]]],
        *,
        tenant_id: str,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._probe = probe
        self._tenant = tenant_id
        self._clock = clock

    async def reconcile(self) -> None:
        for check in await self._probe():
            # Delivery/worker health must not recursively notify about its own notification.
            if check.feature.startswith(("notification", "scheduled")):
                continue
            if check.checked_at is None:
                continue
            now = self._clock()
            if check.checked_at > now:
                continue
            if check.state == "verified" and (
                now - check.checked_at > timedelta(minutes=5)
                or (check.fresh_until is not None and check.fresh_until <= now)
            ):
                continue
            name = notification_record_id("capabilitySource", check.feature, check.scope)
            previous = await self._repository.get_worker_state(self._tenant, name)
            if previous and previous.heartbeat_at and check.checked_at < previous.heartbeat_at:
                continue
            failed = check.state in {"failed", "degraded"}
            if failed:
                if previous is None or previous.last_error is None:
                    previous = await self._repository.update_worker_state(
                        self._tenant,
                        name,
                        self._open(check),
                    )
                if not previous.checkpoint:
                    raise ValueError("A capability failure cursor needs its durable episode ID")
                await self._repository.append_event(
                    build_notification_event(
                        tenant_id=self._tenant,
                        event_type=(
                            NotificationEventType.TELEMETRY_UNAVAILABLE
                            if check.feature.startswith("gatewayTelemetry:")
                            else NotificationEventType.INTEGRATION_FAILED
                        ),
                        source_id=name,
                        source_revision=previous.checkpoint,
                        occurred_at=previous.heartbeat_at or check.checked_at,
                        origin="capabilityChecks",
                        subject=NotificationSubject(
                            integration=check.feature,
                            gateway_id=check.scope
                            if check.feature.startswith("gatewayTelemetry:")
                            else None,
                        ),
                        audience=NotificationAudience(include_administrators=True),
                        data=NotificationTemplateData(
                            summary=(
                                "A configured capability needs attention. Open notification "
                                "readiness or this gateway's Telemetry checks for remediation."
                            ),
                            reason_code=check.state,
                        ),
                        severity="warning",
                    )
                )
            elif check.state == "verified" and previous and previous.last_error:
                await self._repository.append_event(
                    build_notification_event(
                        tenant_id=self._tenant,
                        event_type=NotificationEventType.INTEGRATION_RECOVERED,
                        source_id=name,
                        source_revision=previous.checkpoint or previous.id,
                        occurred_at=check.checked_at,
                        origin="capabilityChecks",
                        subject=NotificationSubject(integration=check.feature),
                        audience=NotificationAudience(include_administrators=True),
                        data=NotificationTemplateData(
                            summary="A previously failed capability has fresh verified evidence.",
                        ),
                    )
                )
                await self._repository.update_worker_state(
                    self._tenant,
                    name,
                    self._close(check),
                )

    @staticmethod
    def _open(
        check: CapabilityCheck,
    ) -> Callable[[NotificationWorkerState], NotificationWorkerState]:
        def change(current: NotificationWorkerState) -> NotificationWorkerState:
            if current.last_error:
                return current
            if check.checked_at is None:
                raise ValueError("A capability transition needs dated evidence")
            return current.model_copy(
                update={
                    "heartbeat_at": check.checked_at,
                    "last_error": check.state,
                    "checkpoint": check.checked_at.isoformat(),
                }
            )

        return change

    @staticmethod
    def _close(
        check: CapabilityCheck,
    ) -> Callable[[NotificationWorkerState], NotificationWorkerState]:
        def change(current: NotificationWorkerState) -> NotificationWorkerState:
            return current.model_copy(
                update={
                    "heartbeat_at": check.checked_at,
                    "last_error": None,
                }
            )

        return change
