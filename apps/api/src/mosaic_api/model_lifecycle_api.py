"""Administrator-only endpoint discovery and proposal context."""

from typing import cast

from fastapi import APIRouter, Request

from mosaic_api.api import Admin, _actor
from mosaic_api.model_lifecycle import (
    DiscoveryRequest,
    DiscoveryRun,
    ProposalContextUpdate,
    ProposalDetail,
    ProposalList,
)
from mosaic_api.services.model_lifecycle import ModelLifecycleService

model_lifecycle_router = APIRouter(
    prefix="/api/v1/model-endpoints/{endpoint_id}/model-lifecycle", tags=["admin"]
)


def _service(request: Request) -> ModelLifecycleService:
    return cast(ModelLifecycleService, request.app.state.model_lifecycle_service)


@model_lifecycle_router.post("/discoveries")
async def discover(
    request: Request,
    auth: Admin,
    endpoint_id: str,
    payload: DiscoveryRequest,
) -> DiscoveryRun:
    return await _service(request).discover(_actor(auth), endpoint_id, payload.request_id)


@model_lifecycle_router.get("/discoveries/{run_id}")
async def discovery(
    request: Request,
    auth: Admin,
    endpoint_id: str,
    run_id: str,
) -> DiscoveryRun:
    return await _service(request).get_run(_actor(auth), endpoint_id, run_id)


@model_lifecycle_router.get("/proposals")
async def proposals(request: Request, auth: Admin, endpoint_id: str) -> ProposalList:
    return await _service(request).list_proposals(_actor(auth), endpoint_id)


@model_lifecycle_router.get("/proposals/{proposal_id}")
async def proposal(
    request: Request,
    auth: Admin,
    endpoint_id: str,
    proposal_id: str,
) -> ProposalDetail:
    return await _service(request).get_proposal(_actor(auth), endpoint_id, proposal_id)


@model_lifecycle_router.put("/proposals/{proposal_id}/context")
async def save_context(
    request: Request,
    auth: Admin,
    endpoint_id: str,
    proposal_id: str,
    payload: ProposalContextUpdate,
) -> ProposalDetail:
    return await _service(request).save_context(_actor(auth), endpoint_id, proposal_id, payload)
