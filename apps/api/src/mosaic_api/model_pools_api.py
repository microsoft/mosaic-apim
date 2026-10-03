"""Administrator routes for model pools (ADR 0024)."""

from typing import Annotated, cast

from fastapi import APIRouter, Query, Request, Response, status

from mosaic_api.api import Admin, _actor
from mosaic_api.domain import (
    PublicationLockInfo,
    PublishPlan,
    PublishRecoveryRequest,
    PublishRun,
)
from mosaic_api.model_pools import (
    ModelPool,
    ModelPoolCreate,
    ModelPoolDetail,
    ModelPoolSummary,
    ModelPoolUpdate,
    PoolCandidates,
    PoolHealth,
)
from mosaic_api.services.model_pools import ModelPoolService
from mosaic_api.services.pool_health import HEALTH_HOURS, MAX_HEALTH_HOURS, PoolHealthService

model_pools_router = APIRouter(prefix="/api/v1", tags=["admin"])


def _pools(request: Request) -> ModelPoolService:
    return cast(ModelPoolService, request.app.state.model_pool_service)


def _health(request: Request) -> PoolHealthService:
    return cast(PoolHealthService, request.app.state.pool_health_service)


@model_pools_router.get("/gateways/{gateway_id}/pool-candidates", response_model=PoolCandidates)
async def model_pool_candidates(request: Request, auth: Admin, gateway_id: str) -> PoolCandidates:
    """The deployments a pool on this gateway could use, grouped by model, and why others can't."""

    return await _pools(request).candidates(_actor(auth), gateway_id)


@model_pools_router.get("/model-pools", response_model=list[ModelPool])
async def list_model_pools(
    request: Request, auth: Admin, gateway: Annotated[str | None, Query()] = None
) -> list[ModelPool]:
    return await _pools(request).list_pools(_actor(auth), gateway)


@model_pools_router.get("/model-pool-summaries", response_model=list[ModelPoolSummary])
async def list_model_pool_summaries(
    request: Request, auth: Admin, gateway: Annotated[str | None, Query()] = None
) -> list[ModelPoolSummary]:
    """Every pool with its active members counted by capacity type and readiness."""

    return await _pools(request).summaries(_actor(auth), gateway)


@model_pools_router.post(
    "/model-pools", response_model=ModelPool, status_code=status.HTTP_201_CREATED
)
async def create_model_pool(request: Request, auth: Admin, payload: ModelPoolCreate) -> ModelPool:
    return await _pools(request).create(_actor(auth), payload)


@model_pools_router.get("/model-pools/{pool_id}", response_model=ModelPool)
async def get_model_pool(request: Request, auth: Admin, pool_id: str) -> ModelPool:
    return await _pools(request).get_pool(_actor(auth), pool_id)


@model_pools_router.get("/model-pools/{pool_id}/detail", response_model=ModelPoolDetail)
async def get_model_pool_detail(request: Request, auth: Admin, pool_id: str) -> ModelPoolDetail:
    """The pool with each member judged against today's inventory, environments, and gateway."""

    return await _pools(request).detail(_actor(auth), pool_id)


@model_pools_router.get("/model-pools/{pool_id}/health", response_model=PoolHealth)
async def get_model_pool_health(
    request: Request,
    auth: Admin,
    pool_id: str,
    hours: Annotated[int, Query(ge=1, le=MAX_HEALTH_HOURS)] = HEALTH_HOURS,
) -> PoolHealth:
    """How the pool's calls ended over the last hours, read from the gateway's attempt traces."""

    return await _health(request).health(_actor(auth), pool_id, hours)


@model_pools_router.patch("/model-pools/{pool_id}", response_model=ModelPool)
async def update_model_pool(
    request: Request, auth: Admin, pool_id: str, payload: ModelPoolUpdate
) -> ModelPool:
    return await _pools(request).update(_actor(auth), pool_id, payload)


@model_pools_router.delete("/model-pools/{pool_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_pool(request: Request, auth: Admin, pool_id: str) -> Response:
    await _pools(request).delete(_actor(auth), pool_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@model_pools_router.post("/model-pools/{pool_id}/plan", response_model=PublishPlan)
async def plan_model_pool(request: Request, auth: Admin, pool_id: str) -> PublishPlan:
    return await _pools(request).plan(_actor(auth), pool_id)


@model_pools_router.post(
    "/model-pools/{pool_id}/apply",
    response_model=PublishRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_model_pool(
    request: Request,
    auth: Admin,
    pool_id: str,
    plan: Annotated[str | None, Query()] = None,
) -> PublishRun:
    return await _pools(request).apply(_actor(auth), pool_id, plan)


@model_pools_router.post("/model-pools/{pool_id}/unpublish-plan", response_model=PublishPlan)
async def plan_unpublish_model_pool(request: Request, auth: Admin, pool_id: str) -> PublishPlan:
    """Plan an unpublish for review: what it deletes, in order. Deletes nothing."""

    return await _pools(request).plan_unpublish(_actor(auth), pool_id)


@model_pools_router.post(
    "/model-pools/{pool_id}/unpublish",
    response_model=PublishRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def unpublish_model_pool(
    request: Request,
    auth: Admin,
    pool_id: str,
    plan: Annotated[str | None, Query()] = None,
) -> PublishRun:
    """Run the reviewed unpublish plan named by ``plan``. Without one, nothing is removed."""

    return await _pools(request).unpublish(_actor(auth), pool_id, plan)


@model_pools_router.get("/model-pools/{pool_id}/runs", response_model=list[PublishRun])
async def list_model_pool_runs(request: Request, auth: Admin, pool_id: str) -> list[PublishRun]:
    return await _pools(request).list_runs(_actor(auth), pool_id)


@model_pools_router.get("/model-pools/{pool_id}/runs/{run_id}", response_model=PublishRun)
async def get_model_pool_run(
    request: Request, auth: Admin, pool_id: str, run_id: str
) -> PublishRun:
    return await _pools(request).get_run(_actor(auth), pool_id, run_id)


@model_pools_router.post("/model-pools/{pool_id}/recover", response_model=PublishRun)
async def recover_model_pool(
    request: Request, auth: Admin, pool_id: str, payload: PublishRecoveryRequest
) -> PublishRun:
    return await _pools(request).recover_interrupted(
        _actor(auth),
        pool_id,
        run_id=payload.run_id,
        confirm_quiesced=payload.confirm_quiesced,
    )


@model_pools_router.get("/model-pools/{pool_id}/lock", response_model=PublicationLockInfo)
async def model_pool_lock(request: Request, auth: Admin, pool_id: str) -> PublicationLockInfo:
    return PublicationLockInfo(
        publication_id=pool_id,
        owner_id=await _pools(request).get_lock_owner(_actor(auth), pool_id),
    )


@model_pools_router.get("/model-pool-plans/{plan_id}", response_model=PublishPlan)
async def get_model_pool_plan(request: Request, auth: Admin, plan_id: str) -> PublishPlan:
    return await _pools(request).get_plan(_actor(auth), plan_id)
