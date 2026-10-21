"""Administrator-only operational delivery diagnostics and explicitly controlled retries."""

from typing import Annotated, cast
from uuid import uuid4

from fastapi import APIRouter, Query, Request
from pydantic import Field

from mosaic_api.api import Admin
from mosaic_api.domain import AuditEvent, MosaicModel
from mosaic_api.errors import NotFoundError
from mosaic_api.notifications import (
    NotificationChannelHealth,
    NotificationDelivery,
    NotificationDeliveryAttempt,
    NotificationDeliveryState,
    NotificationEvent,
    NotificationOutboxState,
)
from mosaic_api.services.notifications import NotificationService

notification_delivery_router = APIRouter(prefix="/api/v1/notifications", tags=["admin"])


class NotificationRetryInput(MosaicModel):
    allow_unknown: bool = Field(
        default=False,
        deprecated=True,
        description="Accepted for older clients; no longer gates retrying uncertain outcomes.",
    )


def _service(request: Request) -> NotificationService:
    return cast(NotificationService, request.app.state.notification_service)


@notification_delivery_router.get("/deliveries", response_model=list[NotificationDelivery])
async def deliveries(
    request: Request,
    auth: Admin,
    event_id: str | None = None,
    state: NotificationDeliveryState | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[NotificationDelivery]:
    return await _service(request).repository.list_deliveries(
        auth.tenant_id, event_id=event_id, state=state, limit=limit
    )


@notification_delivery_router.get("/outbox", response_model=list[NotificationOutboxState])
async def outbox(
    request: Request,
    auth: Admin,
    failed_only: bool = True,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[NotificationOutboxState]:
    return await _service(request).repository.list_outbox_states(
        auth.tenant_id, failed_only=failed_only, limit=limit
    )


@notification_delivery_router.post("/events/{event_id}/retry", response_model=NotificationEvent)
async def retry_event(request: Request, auth: Admin, event_id: str) -> NotificationEvent:
    return await _service(request).repository.requeue_event(
        auth.tenant_id,
        event_id,
        AuditEvent(
            id=f"audit_{uuid4().hex}",
            tenant_id=auth.tenant_id,
            action="notification.fanoutRetried",
            resource_type="notificationEvent",
            resource_id=event_id,
            actor_object_id=auth.object_id,
        ),
    )


@notification_delivery_router.get(
    "/deliveries/{delivery_id}/attempts", response_model=list[NotificationDeliveryAttempt]
)
async def attempts(
    request: Request, auth: Admin, delivery_id: str
) -> list[NotificationDeliveryAttempt]:
    repository = _service(request).repository
    if await repository.get_delivery(auth.tenant_id, delivery_id) is None:
        raise NotFoundError("The notification delivery does not exist")
    return await repository.list_delivery_attempts(auth.tenant_id, delivery_id)


@notification_delivery_router.post(
    "/deliveries/{delivery_id}/retry", response_model=NotificationDelivery
)
async def retry_delivery(
    request: Request, auth: Admin, delivery_id: str, payload: NotificationRetryInput
) -> NotificationDelivery:
    return await _service(request).retry_delivery(
        auth.tenant_id,
        delivery_id,
        actor_object_id=auth.object_id,
        allow_unknown=payload.allow_unknown,
    )


@notification_delivery_router.get("/channel-health", response_model=list[NotificationChannelHealth])
async def channel_health(request: Request, auth: Admin) -> list[NotificationChannelHealth]:
    return [
        item
        for item in await _service(request).repository.list_channel_health(auth.tenant_id)
        if not item.id.startswith("teamsTest-")
    ]
