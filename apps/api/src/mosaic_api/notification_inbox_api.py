"""Personal inboxes: object ID and persona come from existing authenticated dependencies."""

from typing import Annotated, cast

from fastapi import APIRouter, Query, Request

from mosaic_api.api import Admin, PortalUser
from mosaic_api.notifications import (
    NotificationCategory,
    NotificationInboxItem,
    NotificationInboxPage,
    NotificationReadUpdate,
)
from mosaic_api.services.notifications import NotificationService

notifications_router = APIRouter(prefix="/api/v1/notifications", tags=["admin"])
portal_notifications_router = APIRouter(prefix="/api/v1/me/notifications", tags=["portal"])


def _service(request: Request) -> NotificationService:
    return cast(NotificationService, request.app.state.notification_service)


@notifications_router.get("/me/inbox", response_model=NotificationInboxPage)
async def administrator_inbox(
    request: Request,
    auth: Admin,
    category: NotificationCategory | None = None,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    continuation_token: Annotated[str | None, Query(max_length=4096)] = None,
) -> NotificationInboxPage:
    return await _service(request).list_inbox(
        auth.tenant_id,
        auth.object_id,
        "admin",
        category=category,
        unread_only=unread_only,
        limit=limit,
        continuation_token=continuation_token,
    )


@portal_notifications_router.get("/inbox", response_model=NotificationInboxPage)
async def portal_inbox(
    request: Request,
    auth: PortalUser,
    category: NotificationCategory | None = None,
    unread_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    continuation_token: Annotated[str | None, Query(max_length=4096)] = None,
) -> NotificationInboxPage:
    return await _service(request).list_inbox(
        auth.tenant_id,
        auth.object_id,
        "portal",
        category=category,
        unread_only=unread_only,
        limit=limit,
        continuation_token=continuation_token,
    )


@notifications_router.patch("/me/inbox/{item_id}", response_model=NotificationInboxItem)
async def mark_administrator_inbox(
    request: Request,
    auth: Admin,
    item_id: str,
    payload: NotificationReadUpdate,
) -> NotificationInboxItem:
    return await _service(request).set_inbox_read(
        auth.tenant_id, auth.object_id, "admin", item_id, read=payload.read
    )

@notifications_router.get("/me/inbox/{item_id}", response_model=NotificationInboxItem)
async def get_administrator_inbox_item(
    request: Request, auth: Admin, item_id: str
) -> NotificationInboxItem:
    return await _service(request).get_inbox_item(
        auth.tenant_id, auth.object_id, "admin", item_id
    )


@portal_notifications_router.get("/inbox/{item_id}", response_model=NotificationInboxItem)
async def get_portal_inbox_item(
    request: Request, auth: PortalUser, item_id: str
) -> NotificationInboxItem:
    return await _service(request).get_inbox_item(
        auth.tenant_id, auth.object_id, "portal", item_id
    )


@portal_notifications_router.patch("/inbox/{item_id}", response_model=NotificationInboxItem)
async def mark_portal_inbox(
    request: Request,
    auth: PortalUser,
    item_id: str,
    payload: NotificationReadUpdate,
) -> NotificationInboxItem:
    return await _service(request).set_inbox_read(
        auth.tenant_id, auth.object_id, "portal", item_id, read=payload.read
    )
