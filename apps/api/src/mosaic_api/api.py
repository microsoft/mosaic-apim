from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, status

from mosaic_api.auth import AuthContext, require_admin
from mosaic_api.domain import (
    Group,
    GroupCreate,
    GroupMembership,
    GroupUpdate,
    PolicyPreview,
    PolicyPreviewRequest,
    Principal,
    PrincipalCreate,
    PrincipalUpdate,
)
from mosaic_api.integrations.policy import render_policy_preview
from mosaic_api.services import DirectoryService
from mosaic_api.services.directory import Actor

Admin = Annotated[AuthContext, Depends(require_admin)]


def _service(request: Request) -> DirectoryService:
    return cast(DirectoryService, request.app.state.directory_service)


def _actor(auth: AuthContext) -> Actor:
    return Actor(object_id=auth.object_id, tenant_id=auth.tenant_id)


router = APIRouter(prefix="/api/v1", tags=["admin"])


@router.get("/principals", response_model=list[Principal])
async def list_principals(request: Request, auth: Admin) -> list[Principal]:
    return await _service(request).list_principals(_actor(auth))


@router.post("/principals", response_model=Principal, status_code=status.HTTP_201_CREATED)
async def create_principal(request: Request, auth: Admin, payload: PrincipalCreate) -> Principal:
    return await _service(request).create_principal(_actor(auth), payload)


@router.get("/principals/{principal_id}", response_model=Principal)
async def get_principal(request: Request, auth: Admin, principal_id: str) -> Principal:
    return await _service(request).get_principal(_actor(auth), principal_id)


@router.patch("/principals/{principal_id}", response_model=Principal)
async def update_principal(
    request: Request, auth: Admin, principal_id: str, payload: PrincipalUpdate
) -> Principal:
    return await _service(request).update_principal(_actor(auth), principal_id, payload)


@router.delete("/principals/{principal_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_principal(request: Request, auth: Admin, principal_id: str) -> Response:
    await _service(request).delete_principal(_actor(auth), principal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/groups", response_model=list[Group])
async def list_groups(request: Request, auth: Admin) -> list[Group]:
    return await _service(request).list_groups(_actor(auth))


@router.post("/groups", response_model=Group, status_code=status.HTTP_201_CREATED)
async def create_group(request: Request, auth: Admin, payload: GroupCreate) -> Group:
    return await _service(request).create_group(_actor(auth), payload)


@router.get("/groups/{group_id}", response_model=Group)
async def get_group(request: Request, auth: Admin, group_id: str) -> Group:
    return await _service(request).get_group(_actor(auth), group_id)


@router.patch("/groups/{group_id}", response_model=Group)
async def update_group(request: Request, auth: Admin, group_id: str, payload: GroupUpdate) -> Group:
    return await _service(request).update_group(_actor(auth), group_id, payload)


@router.delete("/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group(request: Request, auth: Admin, group_id: str) -> Response:
    await _service(request).delete_group(_actor(auth), group_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/groups/{group_id}/members", response_model=list[GroupMembership])
async def list_memberships(request: Request, auth: Admin, group_id: str) -> list[GroupMembership]:
    return await _service(request).list_memberships(_actor(auth), group_id)


@router.put("/groups/{group_id}/members/{principal_id}", response_model=GroupMembership)
async def add_membership(
    request: Request,
    auth: Admin,
    group_id: str,
    principal_id: str,
    response: Response,
) -> GroupMembership:
    membership, created = await _service(request).add_membership(
        _actor(auth), group_id, principal_id
    )
    response.status_code = status.HTTP_201_CREATED if created else status.HTTP_200_OK
    return membership


@router.delete(
    "/groups/{group_id}/members/{principal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_membership(
    request: Request, auth: Admin, group_id: str, principal_id: str
) -> Response:
    await _service(request).remove_membership(_actor(auth), group_id, principal_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/policies/preview", response_model=PolicyPreview)
async def preview_policy(payload: PolicyPreviewRequest, _auth: Admin) -> PolicyPreview:
    return render_policy_preview(payload)
