from typing import Annotated, cast

from fastapi import APIRouter, Depends, Request, Response, status

from mosaic_api.auth import AuthContext, require_admin
from mosaic_api.domain import (
    Gateway,
    GatewayCreate,
    GatewaySuggestion,
    GatewaySyncRun,
    GatewayUpdate,
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
from mosaic_api.observed import (
    GatewayPolicyView,
    ObservedApi,
    ObservedApimGroup,
    ObservedApimUser,
    ObservedBackend,
    ObservedNamedValue,
    ObservedOperation,
    ObservedProduct,
    ObservedSubscription,
    ScopedPolicyView,
)
from mosaic_api.services import DirectoryService, GatewayService
from mosaic_api.services.directory import Actor

Admin = Annotated[AuthContext, Depends(require_admin)]


def _service(request: Request) -> DirectoryService:
    return cast(DirectoryService, request.app.state.directory_service)


def _gateways(request: Request) -> GatewayService:
    return cast(GatewayService, request.app.state.gateway_service)


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


@router.get("/gateways", response_model=list[Gateway])
async def list_gateways(request: Request, auth: Admin) -> list[Gateway]:
    return await _gateways(request).list_gateways(_actor(auth))


@router.post("/gateways", response_model=Gateway, status_code=status.HTTP_201_CREATED)
async def register_gateway(request: Request, auth: Admin, payload: GatewayCreate) -> Gateway:
    return await _gateways(request).register(_actor(auth), payload)


@router.get("/gateways/suggested", response_model=list[GatewaySuggestion])
async def suggested_gateways(request: Request, auth: Admin) -> list[GatewaySuggestion]:
    return await _gateways(request).suggestions(_actor(auth))


@router.get("/gateways/{gateway_id}", response_model=Gateway)
async def get_gateway(request: Request, auth: Admin, gateway_id: str) -> Gateway:
    return await _gateways(request).get_gateway(_actor(auth), gateway_id)


@router.patch("/gateways/{gateway_id}", response_model=Gateway)
async def update_gateway(
    request: Request, auth: Admin, gateway_id: str, payload: GatewayUpdate
) -> Gateway:
    return await _gateways(request).update(_actor(auth), gateway_id, payload)


@router.delete("/gateways/{gateway_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_gateway(request: Request, auth: Admin, gateway_id: str) -> Response:
    await _gateways(request).delete(_actor(auth), gateway_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/gateways/{gateway_id}/preflight", response_model=Gateway)
async def preflight_gateway(request: Request, auth: Admin, gateway_id: str) -> Gateway:
    return await _gateways(request).preflight(_actor(auth), gateway_id)


@router.post(
    "/gateways/{gateway_id}/sync",
    response_model=GatewaySyncRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_gateway(request: Request, auth: Admin, gateway_id: str) -> GatewaySyncRun:
    return await _gateways(request).start_sync(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/sync-runs", response_model=list[GatewaySyncRun])
async def list_sync_runs(request: Request, auth: Admin, gateway_id: str) -> list[GatewaySyncRun]:
    return await _gateways(request).list_sync_runs(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/sync-runs/{run_id}", response_model=GatewaySyncRun)
async def get_sync_run(
    request: Request, auth: Admin, gateway_id: str, run_id: str
) -> GatewaySyncRun:
    return await _gateways(request).get_sync_run(_actor(auth), run_id)


@router.get("/gateways/{gateway_id}/apis", response_model=list[ObservedApi])
async def list_gateway_apis(request: Request, auth: Admin, gateway_id: str) -> list[ObservedApi]:
    return await _gateways(request).list_apis(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/operations", response_model=list[ObservedOperation])
async def list_gateway_operations(
    request: Request, auth: Admin, gateway_id: str, api: str | None = None
) -> list[ObservedOperation]:
    return await _gateways(request).list_operations(_actor(auth), gateway_id, api)


@router.get("/gateways/{gateway_id}/products", response_model=list[ObservedProduct])
async def list_gateway_products(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedProduct]:
    return await _gateways(request).list_products(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/subscriptions", response_model=list[ObservedSubscription])
async def list_gateway_subscriptions(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedSubscription]:
    return await _gateways(request).list_subscriptions(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/users", response_model=list[ObservedApimUser])
async def list_gateway_users(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedApimUser]:
    return await _gateways(request).list_users(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/groups", response_model=list[ObservedApimGroup])
async def list_gateway_groups(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedApimGroup]:
    return await _gateways(request).list_groups(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/backends", response_model=list[ObservedBackend])
async def list_gateway_backends(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedBackend]:
    return await _gateways(request).list_backends(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/named-values", response_model=list[ObservedNamedValue])
async def list_gateway_named_values(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedNamedValue]:
    return await _gateways(request).list_named_values(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/policies", response_model=GatewayPolicyView)
async def get_gateway_policies(
    request: Request, auth: Admin, gateway_id: str
) -> GatewayPolicyView:
    return await _gateways(request).policy_view(_actor(auth), gateway_id)


@router.get(
    "/gateways/{gateway_id}/apis/{api_name}/operations/{operation_name}/policy",
    response_model=ScopedPolicyView,
)
async def get_operation_policy(
    request: Request, auth: Admin, gateway_id: str, api_name: str, operation_name: str
) -> ScopedPolicyView:
    return await _gateways(request).operation_policy(
        _actor(auth), gateway_id, api_name, operation_name
    )
