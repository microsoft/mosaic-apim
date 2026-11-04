from typing import cast

from fastapi import APIRouter, Request, status

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.cost_center_requests import (
    AdminCostCenterRequest,
    CostCenterRequestApproval,
    CostCenterRequestCreate,
    CostCenterRequestDecision,
    PortalCostCenterRequest,
    RequestCounts,
)
from mosaic_api.domain import AccessRequestState
from mosaic_api.services.cost_center_requests import CostCenterRequestService

cost_center_requests_router = APIRouter(prefix="/api/v1", tags=["cost-center requests"])


def _requests(request: Request) -> CostCenterRequestService:
    return cast(CostCenterRequestService, request.app.state.cost_center_request_service)


@cost_center_requests_router.get("/request-counts", response_model=RequestCounts)
async def admin_request_counts(request: Request, auth: Admin) -> RequestCounts:
    return await _requests(request).counts(_actor(auth), administrator=True)


@cost_center_requests_router.get("/portal/request-counts", response_model=RequestCounts)
async def portal_request_counts(request: Request, auth: PortalUser) -> RequestCounts:
    return await _requests(request).counts(_actor(auth), administrator=False)


@cost_center_requests_router.get(
    "/cost-center-requests", response_model=list[AdminCostCenterRequest]
)
async def list_cost_center_requests(
    request: Request, auth: Admin, state: AccessRequestState | None = None
) -> list[AdminCostCenterRequest]:
    return await _requests(request).list_admin(_actor(auth), state=state)


@cost_center_requests_router.get(
    "/cost-center-requests/{request_id}", response_model=AdminCostCenterRequest
)
async def get_cost_center_request(
    request: Request, auth: Admin, request_id: str
) -> AdminCostCenterRequest:
    return await _requests(request).get_admin(_actor(auth), request_id)


@cost_center_requests_router.post(
    "/cost-center-requests/{request_id}/approve", response_model=AdminCostCenterRequest
)
async def approve_cost_center_request(
    request: Request, auth: Admin, request_id: str, payload: CostCenterRequestApproval
) -> AdminCostCenterRequest:
    return await _requests(request).approve(_actor(auth), request_id, payload)


@cost_center_requests_router.post(
    "/cost-center-requests/{request_id}/deny", response_model=AdminCostCenterRequest
)
async def deny_cost_center_request(
    request: Request, auth: Admin, request_id: str, payload: CostCenterRequestDecision
) -> AdminCostCenterRequest:
    return await _requests(request).deny(_actor(auth), request_id, payload)


@cost_center_requests_router.get(
    "/portal/cost-center-requests", response_model=list[PortalCostCenterRequest]
)
async def my_cost_center_requests(
    request: Request, auth: PortalUser
) -> list[PortalCostCenterRequest]:
    return await _requests(request).list_mine(_actor(auth))


@cost_center_requests_router.post(
    "/portal/cost-center-requests",
    response_model=PortalCostCenterRequest,
    status_code=status.HTTP_201_CREATED,
)
async def create_cost_center_request(
    request: Request, auth: PortalUser, payload: CostCenterRequestCreate
) -> PortalCostCenterRequest:
    return await _requests(request).submit(_actor(auth), payload)


@cost_center_requests_router.get(
    "/portal/cost-center-requests/{request_id}", response_model=PortalCostCenterRequest
)
async def get_my_cost_center_request(
    request: Request, auth: PortalUser, request_id: str
) -> PortalCostCenterRequest:
    return await _requests(request).get_mine(_actor(auth), request_id)


@cost_center_requests_router.post(
    "/portal/cost-center-requests/{request_id}/withdraw", response_model=PortalCostCenterRequest
)
async def withdraw_cost_center_request(
    request: Request, auth: PortalUser, request_id: str
) -> PortalCostCenterRequest:
    return await _requests(request).withdraw(_actor(auth), request_id)
