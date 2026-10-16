"""Authenticated, read-only setup evidence; these routes never send a test message."""

from typing import cast

from fastapi import APIRouter, Request

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.notifications import CapabilityCheck
from mosaic_api.services.feature_readiness import NotificationReadinessService

notification_readiness_router = APIRouter(prefix="/api/v1", tags=["notifications"])


def _service(request: Request) -> NotificationReadinessService:
    return cast(NotificationReadinessService, request.app.state.notification_readiness_service)


@notification_readiness_router.get("/notifications/health", response_model=list[CapabilityCheck])
async def notification_health(request: Request, auth: Admin) -> list[CapabilityCheck]:
    return await _service(request).admin_checks(_actor(auth))


@notification_readiness_router.get("/me/notifications/health", response_model=list[CapabilityCheck])
async def personal_notification_health(request: Request, auth: PortalUser) -> list[CapabilityCheck]:
    return await _service(request).personal_checks(_actor(auth))
