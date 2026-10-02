from typing import Annotated, cast

from fastapi import APIRouter, Query, Request, Response, status

from mosaic_api.api import Admin, _actor
from mosaic_api.domain import (
    McpModelCallerUpdate,
    McpPublication,
    McpPublicationCreate,
    McpPublicationUpdate,
    McpPublishingCapability,
    PublicationLockInfo,
    PublishPlan,
    PublishRecoveryRequest,
    PublishRun,
)
from mosaic_api.services.mcp_publishing import McpPublishingService

mcp_publishing_router = APIRouter(prefix="/api/v1", tags=["admin"])


def _mcp_publishing(request: Request) -> McpPublishingService:
    return cast(McpPublishingService, request.app.state.mcp_publishing_service)


@mcp_publishing_router.get(
    "/gateways/{gateway_id}/mcp-publishing", response_model=McpPublishingCapability
)
async def mcp_publishing_capability(
    request: Request, auth: Admin, gateway_id: str
) -> McpPublishingCapability:
    return await _mcp_publishing(request).capability(_actor(auth), gateway_id)


@mcp_publishing_router.get("/mcp-publications", response_model=list[McpPublication])
async def list_mcp_publications(
    request: Request, auth: Admin, gateway: Annotated[str | None, Query()] = None
) -> list[McpPublication]:
    return await _mcp_publishing(request).list_publications(_actor(auth), gateway)


@mcp_publishing_router.post(
    "/mcp-publications", response_model=McpPublication, status_code=status.HTTP_201_CREATED
)
async def create_mcp_publication(
    request: Request, auth: Admin, payload: McpPublicationCreate
) -> McpPublication:
    return await _mcp_publishing(request).create(_actor(auth), payload)


@mcp_publishing_router.get("/mcp-publications/{publication_id}", response_model=McpPublication)
async def get_mcp_publication(
    request: Request, auth: Admin, publication_id: str
) -> McpPublication:
    return await _mcp_publishing(request).get_publication(_actor(auth), publication_id)


@mcp_publishing_router.patch(
    "/mcp-publications/{publication_id}", response_model=McpPublication
)
async def update_mcp_publication(
    request: Request, auth: Admin, publication_id: str, payload: McpPublicationUpdate
) -> McpPublication:
    return await _mcp_publishing(request).update(_actor(auth), publication_id, payload)


@mcp_publishing_router.put(
    "/mcp-publications/{publication_id}/model-caller", response_model=McpPublication
)
async def set_mcp_model_caller(
    request: Request, auth: Admin, publication_id: str, payload: McpModelCallerUpdate
) -> McpPublication:
    """Name the application this server's tools call models as. See ADR 0025.

    MOSAIC then attributes those model calls to the person each MCP call served. The application's
    own grants still decide access. It takes effect with the publication's next apply.
    """

    return await _mcp_publishing(request).set_model_caller(_actor(auth), publication_id, payload)


@mcp_publishing_router.delete(
    "/mcp-publications/{publication_id}/model-caller", response_model=McpPublication
)
async def clear_mcp_model_caller(
    request: Request, auth: Admin, publication_id: str
) -> McpPublication:
    """Stop attributing this server's model calls to the people it serves, from its next apply."""

    return await _mcp_publishing(request).clear_model_caller(_actor(auth), publication_id)


@mcp_publishing_router.delete(
    "/mcp-publications/{publication_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_mcp_publication(
    request: Request, auth: Admin, publication_id: str
) -> Response:
    await _mcp_publishing(request).delete(_actor(auth), publication_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@mcp_publishing_router.post("/mcp-publications/{publication_id}/plan", response_model=PublishPlan)
async def plan_mcp_publication(
    request: Request, auth: Admin, publication_id: str
) -> PublishPlan:
    return await _mcp_publishing(request).plan(_actor(auth), publication_id)


@mcp_publishing_router.post(
    "/mcp-publications/{publication_id}/apply",
    response_model=PublishRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def apply_mcp_publication(
    request: Request,
    auth: Admin,
    publication_id: str,
    plan: Annotated[str | None, Query()] = None,
) -> PublishRun:
    return await _mcp_publishing(request).apply(_actor(auth), publication_id, plan)


@mcp_publishing_router.post(
    "/mcp-publications/{publication_id}/unpublish-plan", response_model=PublishPlan
)
async def plan_unpublish_mcp_publication(
    request: Request, auth: Admin, publication_id: str
) -> PublishPlan:
    """Plan an unpublish for review: what it deletes and whose access it ends. Deletes nothing."""

    return await _mcp_publishing(request).plan_unpublish(_actor(auth), publication_id)


@mcp_publishing_router.post(
    "/mcp-publications/{publication_id}/unpublish",
    response_model=PublishRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def unpublish_mcp_publication(
    request: Request,
    auth: Admin,
    publication_id: str,
    plan: Annotated[str | None, Query()] = None,
) -> PublishRun:
    """Run the reviewed unpublish plan named by ``plan``. Without one, nothing is removed."""

    return await _mcp_publishing(request).unpublish(_actor(auth), publication_id, plan)


@mcp_publishing_router.get(
    "/mcp-publications/{publication_id}/runs", response_model=list[PublishRun]
)
async def list_mcp_publication_runs(
    request: Request, auth: Admin, publication_id: str
) -> list[PublishRun]:
    return await _mcp_publishing(request).list_runs(_actor(auth), publication_id)


@mcp_publishing_router.get(
    "/mcp-publications/{publication_id}/runs/{run_id}", response_model=PublishRun
)
async def get_mcp_publication_run(
    request: Request, auth: Admin, publication_id: str, run_id: str
) -> PublishRun:
    return await _mcp_publishing(request).get_run(_actor(auth), publication_id, run_id)


@mcp_publishing_router.post(
    "/mcp-publications/{publication_id}/recover", response_model=PublishRun
)
async def recover_mcp_publication(
    request: Request, auth: Admin, publication_id: str, payload: PublishRecoveryRequest
) -> PublishRun:
    return await _mcp_publishing(request).recover_interrupted(
        _actor(auth),
        publication_id,
        run_id=payload.run_id,
        confirm_quiesced=payload.confirm_quiesced,
    )


@mcp_publishing_router.get(
    "/mcp-publications/{publication_id}/lock", response_model=PublicationLockInfo
)
async def mcp_publication_lock(
    request: Request, auth: Admin, publication_id: str
) -> PublicationLockInfo:
    return PublicationLockInfo(
        publication_id=publication_id,
        owner_id=await _mcp_publishing(request).get_lock_owner(_actor(auth), publication_id),
    )


@mcp_publishing_router.get("/mcp-publish-plans/{plan_id}", response_model=PublishPlan)
async def get_mcp_publish_plan(request: Request, auth: Admin, plan_id: str) -> PublishPlan:
    return await _mcp_publishing(request).get_plan(_actor(auth), plan_id)
