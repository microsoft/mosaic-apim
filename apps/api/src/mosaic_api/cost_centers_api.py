"""Cost centers, which every grant and call is charged to. See ADR 0021.

The administrator routes need ``Admin``. ``GET /api/v1/portal/cost-centers`` answers for the
caller only, from their own token: the cost centers they may charge when they ask for access.
"""

from typing import cast

from fastapi import APIRouter, Request, Response, status

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.cost_centers import (
    CostCenterCreate,
    CostCenterLimitsUpdate,
    CostCenterSettings,
    CostCenterSettingsUpdate,
    CostCenterUpdate,
    CostCenterView,
    PortalCostCenter,
)
from mosaic_api.services.cost_centers import CostCenterService

cost_centers_router = APIRouter(prefix="/api/v1", tags=["admin"])
portal_cost_centers_router = APIRouter(prefix="/api/v1/portal", tags=["portal"])


def _cost_centers(request: Request) -> CostCenterService:
    return cast(CostCenterService, request.app.state.cost_center_service)


@cost_centers_router.get("/cost-centers", response_model=list[CostCenterView])
async def list_cost_centers(request: Request, auth: Admin) -> list[CostCenterView]:
    return await _cost_centers(request).list_cost_centers(_actor(auth))


@cost_centers_router.post(
    "/cost-centers", response_model=CostCenterView, status_code=status.HTTP_201_CREATED
)
async def create_cost_center(
    request: Request, auth: Admin, payload: CostCenterCreate
) -> CostCenterView:
    return await _cost_centers(request).create_cost_center(_actor(auth), payload)


@cost_centers_router.get("/cost-centers/{cost_center_id}", response_model=CostCenterView)
async def get_cost_center(request: Request, auth: Admin, cost_center_id: str) -> CostCenterView:
    return await _cost_centers(request).get_cost_center(_actor(auth), cost_center_id)


@cost_centers_router.patch("/cost-centers/{cost_center_id}", response_model=CostCenterView)
async def update_cost_center(
    request: Request, auth: Admin, cost_center_id: str, payload: CostCenterUpdate
) -> CostCenterView:
    return await _cost_centers(request).update_cost_center(_actor(auth), cost_center_id, payload)


@cost_centers_router.delete(
    "/cost-centers/{cost_center_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_cost_center(request: Request, auth: Admin, cost_center_id: str) -> Response:
    await _cost_centers(request).delete_cost_center(_actor(auth), cost_center_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@cost_centers_router.put(
    "/cost-centers/{cost_center_id}/members/{principal_id}", response_model=CostCenterView
)
async def add_cost_center_member(
    request: Request, auth: Admin, cost_center_id: str, principal_id: str
) -> CostCenterView:
    return await _cost_centers(request).add_member(_actor(auth), cost_center_id, principal_id)


@cost_centers_router.delete(
    "/cost-centers/{cost_center_id}/members/{principal_id}", response_model=CostCenterView
)
async def remove_cost_center_member(
    request: Request, auth: Admin, cost_center_id: str, principal_id: str
) -> CostCenterView:
    return await _cost_centers(request).remove_member(_actor(auth), cost_center_id, principal_id)


@cost_centers_router.post(
    "/cost-centers/{cost_center_id}/recheck", response_model=CostCenterView
)
async def recheck_cost_center(
    request: Request, auth: Admin, cost_center_id: str
) -> CostCenterView:
    """Check again the grants the cost center's pending rechecks cover. See ADR 0021."""

    return await _cost_centers(request).recheck(_actor(auth), cost_center_id)


@cost_centers_router.put("/cost-centers/{cost_center_id}/limits", response_model=CostCenterView)
async def set_cost_center_limits(
    request: Request, auth: Admin, cost_center_id: str, payload: CostCenterLimitsUpdate
) -> CostCenterView:
    return await _cost_centers(request).set_limits(_actor(auth), cost_center_id, payload)


@cost_centers_router.get("/cost-center-settings", response_model=CostCenterSettings)
async def get_cost_center_settings(request: Request, auth: Admin) -> CostCenterSettings:
    return await _cost_centers(request).get_settings(_actor(auth))


@cost_centers_router.put("/cost-center-settings", response_model=CostCenterSettings)
async def update_cost_center_settings(
    request: Request, auth: Admin, payload: CostCenterSettingsUpdate
) -> CostCenterSettings:
    return await _cost_centers(request).update_settings(_actor(auth), payload)


@portal_cost_centers_router.get("/cost-centers", response_model=list[PortalCostCenter])
async def my_cost_centers(request: Request, auth: PortalUser) -> list[PortalCostCenter]:
    return await _cost_centers(request).chargeable_for_caller(_actor(auth))
