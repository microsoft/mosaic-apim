"""Administrator-only Azure deployment discovery, review, execution, and recovery."""

from typing import cast

from fastapi import APIRouter, Request, status

from mosaic_api.api import Admin, _actor
from mosaic_api.model_deployments import (
    DeploymentAccess,
    DeploymentCatalogue,
    DeploymentRecoveryRequest,
    ModelDeploymentCreate,
    ModelDeploymentPlan,
    ModelDeploymentRun,
)
from mosaic_api.services.model_deployments import ModelDeploymentService

model_deployments_router = APIRouter(prefix="/api/v1/model-endpoints", tags=["admin"])


def _deployments(request: Request) -> ModelDeploymentService:
    return cast(ModelDeploymentService, request.app.state.model_deployment_service)


@model_deployments_router.get("/{endpoint_id}/deployment-access")
async def deployment_access(request: Request, auth: Admin, endpoint_id: str) -> DeploymentAccess:
    return await _deployments(request).access(_actor(auth), endpoint_id)


@model_deployments_router.get("/{endpoint_id}/deployable-models")
async def deployable_models(request: Request, auth: Admin, endpoint_id: str) -> DeploymentCatalogue:
    return await _deployments(request).catalogue(_actor(auth), endpoint_id)


@model_deployments_router.post("/{endpoint_id}/deployment-plans")
async def plan_model_deployment(
    request: Request, auth: Admin, endpoint_id: str, payload: ModelDeploymentCreate
) -> ModelDeploymentPlan:
    return await _deployments(request).plan(_actor(auth), endpoint_id, payload)


@model_deployments_router.post(
    "/{endpoint_id}/deployment-plans/{plan_id}/apply", status_code=status.HTTP_202_ACCEPTED
)
async def apply_model_deployment(
    request: Request, auth: Admin, endpoint_id: str, plan_id: str
) -> ModelDeploymentRun:
    return await _deployments(request).apply(_actor(auth), endpoint_id, plan_id)


@model_deployments_router.get("/{endpoint_id}/deployment-runs")
async def list_model_deployment_runs(
    request: Request, auth: Admin, endpoint_id: str
) -> list[ModelDeploymentRun]:
    return await _deployments(request).list_runs(_actor(auth), endpoint_id)


@model_deployments_router.get("/{endpoint_id}/deployment-runs/{run_id}")
async def get_model_deployment_run(
    request: Request, auth: Admin, endpoint_id: str, run_id: str
) -> ModelDeploymentRun:
    return await _deployments(request).get_run(_actor(auth), endpoint_id, run_id)


@model_deployments_router.post("/{endpoint_id}/deployment-runs/{run_id}/reconcile")
async def reconcile_model_deployment(
    request: Request,
    auth: Admin,
    endpoint_id: str,
    run_id: str,
    payload: DeploymentRecoveryRequest,
) -> ModelDeploymentRun:
    return await _deployments(request).reconcile(_actor(auth), endpoint_id, run_id, payload)


@model_deployments_router.post("/{endpoint_id}/deployment-runs/{run_id}/refresh")
async def refresh_model_deployment(
    request: Request, auth: Admin, endpoint_id: str, run_id: str
) -> ModelDeploymentRun:
    return await _deployments(request).retry_refresh(_actor(auth), endpoint_id, run_id)
