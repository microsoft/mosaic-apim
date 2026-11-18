from typing import cast

from fastapi import APIRouter, Query, Request

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.cost_center_delegation import (
    ReviewCenter,
    ReviewDecision,
    ReviewDetail,
    ReviewKind,
    ReviewOutcome,
    ReviewPage,
)
from mosaic_api.cost_center_management import CostCenterManagementView
from mosaic_api.cost_centers import PocAssignmentsUpdate
from mosaic_api.domain import AccessRequestState
from mosaic_api.services.cost_center_delegation import CostCenterDelegationService
from mosaic_api.services.cost_centers import CostCenterService

delegation_router = APIRouter(prefix="/api/v1", tags=["cost-center delegation"])


def _review(request: Request) -> CostCenterDelegationService:
    return cast(CostCenterDelegationService, request.app.state.cost_center_delegation_service)


@delegation_router.put("/cost-centers/{center_id}/pocs", response_model=CostCenterManagementView)
async def assign_pocs(
    request: Request,
    auth: Admin,
    center_id: str,
    payload: PocAssignmentsUpdate,
) -> CostCenterManagementView:
    service = cast(CostCenterService, request.app.state.cost_center_service)
    return await service.assign_pocs(_actor(auth), center_id, payload)


@delegation_router.get("/portal/approvals/centers", response_model=list[ReviewCenter])
async def review_centers(request: Request, auth: PortalUser) -> list[ReviewCenter]:
    return await _review(request).centers(_actor(auth))


@delegation_router.get("/portal/approvals", response_model=ReviewPage)
async def review_queue(
    request: Request,
    auth: PortalUser,
    center_id: str = Query(alias="centerId", max_length=128),
    state: AccessRequestState = AccessRequestState.PENDING,
    cursor: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=30, ge=1, le=100),
) -> ReviewPage:
    return await _review(request).queue(_actor(auth), center_id, state, cursor, limit)


@delegation_router.get("/portal/approvals/{kind}/{request_id}", response_model=ReviewDetail)
async def review_detail(
    request: Request,
    auth: PortalUser,
    kind: ReviewKind,
    request_id: str,
) -> ReviewDetail:
    return await _review(request).detail(_actor(auth), kind, request_id)


@delegation_router.post(
    "/portal/approvals/{kind}/{request_id}/{outcome}", response_model=ReviewDetail
)
async def review_decision(
    request: Request,
    auth: PortalUser,
    kind: ReviewKind,
    request_id: str,
    outcome: ReviewOutcome,
    payload: ReviewDecision,
) -> ReviewDetail:
    return await _review(request).decide(_actor(auth), kind, request_id, outcome, payload)
