from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response

from mosaic_api.auth import AuthContext, require_admin
from mosaic_api.domain import (
    DirectoryMemberPage,
    DirectoryObject,
    DirectorySearchKind,
    DirectoryStatus,
)
from mosaic_api.services import DirectoryService
from mosaic_api.services.directory import Actor

Admin = Annotated[AuthContext, Depends(require_admin)]

directory_router = APIRouter(prefix="/api/v1", tags=["directory"])


def _service(request: Request) -> DirectoryService:
    return cast(DirectoryService, request.app.state.directory_service)


def _actor(auth: AuthContext) -> Actor:
    return Actor(
        object_id=auth.object_id,
        tenant_id=auth.tenant_id,
        group_ids=auth.group_ids,
        groups_overage=auth.groups_overage,
    )


@directory_router.get("/directory/status", response_model=DirectoryStatus)
async def directory_status(request: Request, auth: Admin) -> DirectoryStatus:
    return await _service(request).directory_status(_actor(auth))


@directory_router.get("/directory/search", response_model=list[DirectoryObject])
async def search_directory(
    request: Request,
    auth: Admin,
    kind: Annotated[DirectorySearchKind, Query()],
    q: Annotated[str, Query(min_length=1, max_length=120)],
    limit: Annotated[int, Query(ge=1, le=25)] = 20,
) -> list[DirectoryObject]:
    return await _service(request).search_directory(_actor(auth), kind, q, limit)


@directory_router.get("/directory/objects/{object_id}", response_model=DirectoryObject)
async def get_directory_object(request: Request, auth: Admin, object_id: str) -> DirectoryObject:
    return await _service(request).get_directory_object(_actor(auth), object_id)


@directory_router.get(
    "/directory/objects/{object_id}/photo",
    response_class=Response,
    responses={
        200: {"content": {"image/jpeg": {}, "image/png": {}}, "description": "Person thumbnail"},
        404: {"description": "No ordinary user photo is available"},
    },
)
async def directory_photo(request: Request, auth: Admin, object_id: str) -> Response:
    # A Graph app-only token belongs to this deployment's tenant, not a requested tenant.
    if auth.tenant_id.casefold() != request.app.state.settings.tenant_id.casefold():
        raise HTTPException(
            status_code=403, detail="Directory photos are restricted to this tenant"
        )
    photo = await _service(request).directory_photo(_actor(auth), object_id)
    return Response(
        content=photo.content,
        media_type=photo.content_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@directory_router.get("/principals/{principal_id}/members", response_model=DirectoryMemberPage)
async def principal_members(
    request: Request,
    auth: Admin,
    principal_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> DirectoryMemberPage:
    return await _service(request).security_group_members(_actor(auth), principal_id, limit)
