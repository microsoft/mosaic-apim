"""Cost centers, which every grant and call is charged to. See ADR 0022.

The administrator routes need ``Admin``. ``GET /api/v1/portal/cost-centers`` answers for the
caller only, from their own token: the cost centers they may charge when they ask for access.
"""

from typing import Annotated, cast

from fastapi import APIRouter, Header, Query, Request, Response, status

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.cost_center_directory import DirectoryOperationState
from mosaic_api.cost_center_management import (
    CostCenterDirectoryMemberAdd,
    CostCenterDirectoryMembers,
    CostCenterDirectoryOperationView,
    CostCenterGroupCapabilities,
    CostCenterManagementView,
)
from mosaic_api.cost_centers import (
    CostCenterCreate,
    CostCenterLimitsUpdate,
    CostCenterSettings,
    CostCenterSettingsUpdate,
    CostCenterUpdate,
    PortalCostCenter,
)
from mosaic_api.services.cost_centers import CostCenterService

cost_centers_router = APIRouter(prefix="/api/v1", tags=["admin"])
portal_cost_centers_router = APIRouter(prefix="/api/v1/portal", tags=["portal"])
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)]


def _cost_centers(request: Request) -> CostCenterService:
    return cast(CostCenterService, request.app.state.cost_center_service)


@cost_centers_router.get("/cost-centers", response_model=list[CostCenterManagementView])
async def list_cost_centers(request: Request, auth: Admin) -> list[CostCenterManagementView]:
    return await _cost_centers(request).list_cost_centers(_actor(auth))


@cost_centers_router.post(
    "/cost-centers", response_model=CostCenterManagementView, status_code=status.HTTP_201_CREATED
)
async def create_cost_center(
    request: Request, auth: Admin, payload: CostCenterCreate
) -> CostCenterManagementView:
    return await _cost_centers(request).create_cost_center(_actor(auth), payload)


@cost_centers_router.get(
    "/cost-centers/group-capabilities", response_model=CostCenterGroupCapabilities
)
async def cost_center_group_capabilities(
    request: Request, auth: Admin
) -> CostCenterGroupCapabilities:
    return await _cost_centers(request).group_capabilities(_actor(auth))


@cost_centers_router.get("/cost-centers/{cost_center_id}", response_model=CostCenterManagementView)
async def get_cost_center(
    request: Request, auth: Admin, cost_center_id: str
) -> CostCenterManagementView:
    return await _cost_centers(request).get_cost_center(_actor(auth), cost_center_id)


@cost_centers_router.patch(
    "/cost-centers/{cost_center_id}", response_model=CostCenterManagementView
)
async def update_cost_center(
    request: Request, auth: Admin, cost_center_id: str, payload: CostCenterUpdate
) -> CostCenterManagementView:
    return await _cost_centers(request).update_cost_center(_actor(auth), cost_center_id, payload)


@cost_centers_router.delete(
    "/cost-centers/{cost_center_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_cost_center(request: Request, auth: Admin, cost_center_id: str) -> Response:
    await _cost_centers(request).delete_cost_center(_actor(auth), cost_center_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@cost_centers_router.put(
    "/cost-centers/{cost_center_id}/members/{principal_id}", response_model=CostCenterManagementView
)
async def add_cost_center_member(
    request: Request, auth: Admin, cost_center_id: str, principal_id: str
) -> CostCenterManagementView:
    return await _cost_centers(request).add_member(_actor(auth), cost_center_id, principal_id)


@cost_centers_router.delete(
    "/cost-centers/{cost_center_id}/members/{principal_id}", response_model=CostCenterManagementView
)
async def remove_cost_center_member(
    request: Request, auth: Admin, cost_center_id: str, principal_id: str
) -> CostCenterManagementView:
    return await _cost_centers(request).remove_member(_actor(auth), cost_center_id, principal_id)


@cost_centers_router.post(
    "/cost-centers/{cost_center_id}/recheck", response_model=CostCenterManagementView
)
async def recheck_cost_center(
    request: Request, auth: Admin, cost_center_id: str
) -> CostCenterManagementView:
    """Check again the grants the cost center's pending rechecks cover. See ADR 0022."""

    return await _cost_centers(request).recheck(_actor(auth), cost_center_id)


@cost_centers_router.put(
    "/cost-centers/{cost_center_id}/limits", response_model=CostCenterManagementView
)
async def set_cost_center_limits(
    request: Request, auth: Admin, cost_center_id: str, payload: CostCenterLimitsUpdate
) -> CostCenterManagementView:
    return await _cost_centers(request).set_limits(_actor(auth), cost_center_id, payload)


@cost_centers_router.get(
    "/cost-centers/{cost_center_id}/directory-members", response_model=CostCenterDirectoryMembers
)
async def list_directory_members(
    request: Request,
    auth: Admin,
    cost_center_id: str,
    page_size: Annotated[int, Query(alias="pageSize", ge=1, le=999)] = 100,
    cursor: Annotated[str | None, Query(min_length=1, max_length=16384)] = None,
) -> CostCenterDirectoryMembers:
    return await _cost_centers(request).list_directory_members(
        _actor(auth), cost_center_id, page_size=page_size, cursor=cursor
    )


def _operation_status(
    response: Response, operation: CostCenterDirectoryOperationView
) -> CostCenterDirectoryOperationView:
    if operation.state in {DirectoryOperationState.PENDING, DirectoryOperationState.UNKNOWN}:
        response.status_code = status.HTTP_202_ACCEPTED
    return operation


@cost_centers_router.put(
    "/cost-centers/{cost_center_id}/directory-members/{object_id}",
    response_model=CostCenterDirectoryOperationView,
    response_description="Tracked operation; inspect state. HTTP 200 alone does not mean success.",
    responses={
        202: {
            "model": CostCenterDirectoryOperationView,
            "description": "Pending or unknown directory outcome, not confirmed directory success.",
        }
    },
)
async def add_directory_member(
    request: Request,
    response: Response,
    auth: Admin,
    cost_center_id: str,
    object_id: str,
    payload: CostCenterDirectoryMemberAdd,
    idempotency_key: IdempotencyKey,
) -> CostCenterDirectoryOperationView:
    operation = await _cost_centers(request).add_directory_member(
        _actor(auth),
        cost_center_id,
        object_id,
        payload.member_kind,
        idempotency_key=idempotency_key,
    )
    return _operation_status(response, operation)


@cost_centers_router.delete(
    "/cost-centers/{cost_center_id}/directory-members/{object_id}",
    response_model=CostCenterDirectoryOperationView,
    response_description="Tracked operation; inspect state. HTTP 200 alone does not mean success.",
    responses={
        202: {
            "model": CostCenterDirectoryOperationView,
            "description": "Pending or unknown directory outcome, not confirmed directory success.",
        }
    },
)
async def remove_directory_member(
    request: Request,
    response: Response,
    auth: Admin,
    cost_center_id: str,
    object_id: str,
    idempotency_key: IdempotencyKey,
) -> CostCenterDirectoryOperationView:
    operation = await _cost_centers(request).remove_directory_member(
        _actor(auth), cost_center_id, object_id, idempotency_key=idempotency_key
    )
    return _operation_status(response, operation)


@cost_centers_router.get(
    "/cost-centers/{cost_center_id}/directory-operations",
    response_model=list[CostCenterDirectoryOperationView],
)
async def list_directory_operations(
    request: Request,
    auth: Admin,
    cost_center_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[CostCenterDirectoryOperationView]:
    return await _cost_centers(request).list_directory_operations(
        _actor(auth), cost_center_id, limit=limit
    )


@cost_centers_router.get(
    "/cost-centers/{cost_center_id}/directory-operations/{operation_id}",
    response_model=CostCenterDirectoryOperationView,
)
async def get_directory_operation(
    request: Request, auth: Admin, cost_center_id: str, operation_id: str
) -> CostCenterDirectoryOperationView:
    return await _cost_centers(request).get_directory_operation(
        _actor(auth), cost_center_id, operation_id
    )


@cost_centers_router.post(
    "/cost-centers/{cost_center_id}/directory-operations/{operation_id}/retry",
    response_model=CostCenterDirectoryOperationView,
    response_description="Tracked operation; inspect state. HTTP 200 alone does not mean success.",
    responses={
        202: {
            "model": CostCenterDirectoryOperationView,
            "description": "Pending or unknown directory outcome, not confirmed directory success.",
        }
    },
)
async def retry_directory_operation(
    request: Request, response: Response, auth: Admin, cost_center_id: str, operation_id: str
) -> CostCenterDirectoryOperationView:
    operation = await _cost_centers(request).retry_directory_operation(
        _actor(auth), cost_center_id, operation_id
    )
    return _operation_status(response, operation)


@cost_centers_router.post(
    "/cost-centers/{cost_center_id}/directory-operations/{operation_id}/recover",
    response_model=CostCenterDirectoryOperationView,
    response_description="Tracked operation; inspect state. HTTP 200 alone does not mean success.",
    responses={
        202: {
            "model": CostCenterDirectoryOperationView,
            "description": "Pending or unknown directory outcome, not confirmed directory success.",
        }
    },
)
async def recover_directory_operation(
    request: Request, response: Response, auth: Admin, cost_center_id: str, operation_id: str
) -> CostCenterDirectoryOperationView:
    operation = await _cost_centers(request).recover_directory_operation(
        _actor(auth), cost_center_id, operation_id
    )
    return _operation_status(response, operation)


@cost_centers_router.get("/cost-center-settings", response_model=CostCenterSettings)
async def get_cost_center_settings(request: Request, auth: Admin) -> CostCenterSettings:
    return await _cost_centers(request).get_settings(_actor(auth))


@cost_centers_router.put("/cost-center-settings", response_model=CostCenterSettings)
async def update_cost_center_settings(
    request: Request, auth: Admin, payload: CostCenterSettingsUpdate
) -> CostCenterSettings:
    return await _cost_centers(request).update_settings(_actor(auth), payload)


@portal_cost_centers_router.get("/cost-centers", response_model=list[PortalCostCenter])
async def my_cost_centers(request: Request, auth: PortalUser) -> list[PortalCostCenter]:
    return await _cost_centers(request).chargeable_for_caller(_actor(auth))
