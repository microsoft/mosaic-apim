from typing import cast

from fastapi import APIRouter, Query, Request, status

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.cost_center_membership_requests import (
    AdminMembershipRequest,
    MembershipApproval,
    MembershipDecision,
    MembershipRequestCreate,
    PortalCenter,
    PortalCenterPage,
    PortalMembershipRequest,
    SelfServiceSettings,
)
from mosaic_api.domain import AccessRequestState
from mosaic_api.services.cost_center_membership_requests import CostCenterMembershipService

membership_requests_router = APIRouter(prefix="/api/v1", tags=["cost-center self service"])


def _service(request: Request) -> CostCenterMembershipService:
    return cast(
        CostCenterMembershipService, request.app.state.cost_center_membership_request_service
    )


@membership_requests_router.get(
    "/portal/cost-center-self-service-settings", response_model=SelfServiceSettings
)
async def self_service_settings(request: Request, auth: PortalUser) -> SelfServiceSettings:
    return await _service(request).settings(_actor(auth))


@membership_requests_router.get("/portal/my-cost-centers", response_model=PortalCenterPage)
async def my_centers(
    request: Request,
    auth: PortalUser,
    q: str = Query(default="", max_length=200),
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
) -> PortalCenterPage:
    return await _service(request).list_centers(
        _actor(auth), mine=True, q=q, cursor=cursor, limit=limit
    )


@membership_requests_router.get("/portal/cost-center-directory", response_model=PortalCenterPage)
async def directory_centers(
    request: Request,
    auth: PortalUser,
    q: str = Query(default="", max_length=200),
    cursor: str | None = Query(default=None, max_length=128),
    limit: int = Query(default=50, ge=1, le=100),
) -> PortalCenterPage:
    return await _service(request).list_centers(
        _actor(auth), mine=False, q=q, cursor=cursor, limit=limit
    )


@membership_requests_router.get(
    "/portal/cost-center-directory/{center_id}", response_model=PortalCenter
)
async def directory_center(request: Request, auth: PortalUser, center_id: str) -> PortalCenter:
    return await _service(request).center(_actor(auth), center_id)


@membership_requests_router.get(
    "/portal/cost-center-membership-requests", response_model=list[PortalMembershipRequest]
)
async def my_requests(request: Request, auth: PortalUser) -> list[PortalMembershipRequest]:
    return await _service(request).list_requests(_actor(auth), administrator=False)


@membership_requests_router.post(
    "/portal/cost-center-membership-requests",
    response_model=PortalMembershipRequest,
    status_code=status.HTTP_201_CREATED,
)
async def submit(
    request: Request,
    auth: PortalUser,
    payload: MembershipRequestCreate,
) -> PortalMembershipRequest:
    return await _service(request).submit(_actor(auth), payload)


@membership_requests_router.get(
    "/portal/cost-center-membership-requests/{request_id}", response_model=PortalMembershipRequest
)
async def my_request(
    request: Request, auth: PortalUser, request_id: str
) -> PortalMembershipRequest:
    return await _service(request).get(_actor(auth), request_id, administrator=False)


@membership_requests_router.post(
    "/portal/cost-center-membership-requests/{request_id}/withdraw",
    response_model=PortalMembershipRequest,
)
async def withdraw(request: Request, auth: PortalUser, request_id: str) -> PortalMembershipRequest:
    return await _service(request).close(
        _actor(auth), request_id, MembershipDecision(), withdraw=True
    )


@membership_requests_router.get(
    "/cost-center-membership-requests", response_model=list[AdminMembershipRequest]
)
async def admin_requests(
    request: Request,
    auth: Admin,
    state: AccessRequestState | None = None,
) -> list[PortalMembershipRequest]:
    return await _service(request).list_requests(_actor(auth), administrator=True, state=state)


@membership_requests_router.get(
    "/cost-center-membership-requests/{request_id}", response_model=AdminMembershipRequest
)
async def admin_request(request: Request, auth: Admin, request_id: str) -> PortalMembershipRequest:
    return await _service(request).get(_actor(auth), request_id, administrator=True)


@membership_requests_router.post(
    "/cost-center-membership-requests/{request_id}/approve", response_model=AdminMembershipRequest
)
async def approve(
    request: Request,
    auth: Admin,
    request_id: str,
    payload: MembershipApproval,
) -> AdminMembershipRequest:
    return await _service(request).approve(_actor(auth), request_id, payload)


@membership_requests_router.post(
    "/cost-center-membership-requests/{request_id}/resume", response_model=AdminMembershipRequest
)
async def resume(
    request: Request,
    auth: Admin,
    request_id: str,
    payload: MembershipApproval,
) -> AdminMembershipRequest:
    return await _service(request).approve(_actor(auth), request_id, payload, resume=True)


@membership_requests_router.post(
    "/cost-center-membership-requests/{request_id}/deny", response_model=AdminMembershipRequest
)
async def deny(
    request: Request,
    auth: Admin,
    request_id: str,
    payload: MembershipDecision,
) -> PortalMembershipRequest:
    return await _service(request).close(_actor(auth), request_id, payload, withdraw=False)
