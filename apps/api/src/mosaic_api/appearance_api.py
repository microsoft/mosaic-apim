from typing import cast

from fastapi import APIRouter, Request

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.appearance import AppearanceSettings, AppearanceUpdate, AppearanceView
from mosaic_api.services.appearance import AppearanceService

appearance_router = APIRouter(prefix="/api/v1", tags=["admin"])
portal_appearance_router = APIRouter(prefix="/api/v1/portal", tags=["portal"])


def _appearance(request: Request) -> AppearanceService:
    return cast(AppearanceService, request.app.state.appearance_service)


@appearance_router.get("/appearance", response_model=AppearanceSettings)
async def get_appearance(request: Request, auth: Admin) -> AppearanceSettings:
    return await _appearance(request).get(_actor(auth))


@appearance_router.put("/appearance", response_model=AppearanceSettings)
async def update_appearance(
    request: Request, auth: Admin, payload: AppearanceUpdate
) -> AppearanceSettings:
    return await _appearance(request).update(_actor(auth), payload)


@portal_appearance_router.get("/appearance", response_model=AppearanceView)
async def portal_appearance(request: Request, auth: PortalUser) -> AppearanceView:
    """The colours to paint with. Both apps read this, so every signed-in role may."""

    return await _appearance(request).view(_actor(auth))
