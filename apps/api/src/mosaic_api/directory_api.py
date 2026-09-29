from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request

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


@directory_router.get("/principals/{principal_id}/members", response_model=DirectoryMemberPage)
async def principal_members(
    request: Request,
    auth: Admin,
    principal_id: str,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> DirectoryMemberPage:
    return await _service(request).security_group_members(_actor(auth), principal_id, limit)
