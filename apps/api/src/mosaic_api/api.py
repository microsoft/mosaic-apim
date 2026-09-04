from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from mosaic_api.auth import AuthContext, require_admin
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestDecision,
    AccessRequestState,
    CatalogEntryUpdate,
    Entitlement,
    EntitlementCreate,
    EntitlementUpdate,
    Gateway,
    GatewayCreate,
    GatewayRuntimeAccess,
    GatewaySuggestion,
    GatewaySyncRun,
    GatewayUpdate,
    Group,
    GroupCreate,
    GroupMembership,
    GroupUpdate,
    ImportRequest,
    McpEndpoint,
    McpEndpointCreate,
    McpEndpointSyncRun,
    McpEndpointUpdate,
    McpServer,
    McpServerCandidateList,
    ModelApi,
    ModelApiCandidateList,
    ModelEndpoint,
    ModelEndpointCreate,
    ModelEndpointSuggestionView,
    ModelEndpointSyncRun,
    ModelEndpointUpdate,
    PolicyPreview,
    PolicyPreviewRequest,
    Principal,
    PrincipalCreate,
    PrincipalUpdate,
    ResolvedEntitlement,
)
from mosaic_api.integrations.policy import render_policy_preview
from mosaic_api.observed import (
    GatewayPolicyView,
    ObservedApi,
    ObservedApimGroup,
    ObservedApimUser,
    ObservedAvailableModel,
    ObservedBackend,
    ObservedMcpServer,
    ObservedMcpTool,
    ObservedModelDeployment,
    ObservedNamedValue,
    ObservedOperation,
    ObservedProduct,
    ObservedSubscription,
    ScopedPolicyView,
)
from mosaic_api.services import (
    DirectoryService,
    EntitlementService,
    GatewayService,
    McpEndpointService,
    ModelEndpointService,
)
from mosaic_api.services.directory import Actor

Admin = Annotated[AuthContext, Depends(require_admin)]


def _service(request: Request) -> DirectoryService:
    return cast(DirectoryService, request.app.state.directory_service)


def _gateways(request: Request) -> GatewayService:
    return cast(GatewayService, request.app.state.gateway_service)


def _endpoints(request: Request) -> ModelEndpointService:
    return cast(ModelEndpointService, request.app.state.model_endpoint_service)


def _entitlements(request: Request) -> EntitlementService:
    return cast(EntitlementService, request.app.state.entitlement_service)


def _mcp_endpoints(request: Request) -> McpEndpointService:
    return cast(McpEndpointService, request.app.state.mcp_endpoint_service)


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


@router.get("/model-endpoints", response_model=list[ModelEndpoint])
async def list_model_endpoints(request: Request, auth: Admin) -> list[ModelEndpoint]:
    return await _endpoints(request).list_endpoints(_actor(auth))


@router.post(
    "/model-endpoints", response_model=ModelEndpoint, status_code=status.HTTP_201_CREATED
)
async def register_model_endpoint(
    request: Request, auth: Admin, payload: ModelEndpointCreate
) -> ModelEndpoint:
    return await _endpoints(request).register(_actor(auth), payload)


@router.get("/model-endpoints/suggested", response_model=ModelEndpointSuggestionView)
async def suggested_model_endpoints(
    request: Request, auth: Admin
) -> ModelEndpointSuggestionView:
    return await _endpoints(request).suggestions(_actor(auth))


@router.get("/model-endpoints/{endpoint_id}", response_model=ModelEndpoint)
async def get_model_endpoint(request: Request, auth: Admin, endpoint_id: str) -> ModelEndpoint:
    return await _endpoints(request).get_endpoint(_actor(auth), endpoint_id)


@router.patch("/model-endpoints/{endpoint_id}", response_model=ModelEndpoint)
async def update_model_endpoint(
    request: Request, auth: Admin, endpoint_id: str, payload: ModelEndpointUpdate
) -> ModelEndpoint:
    return await _endpoints(request).update(_actor(auth), endpoint_id, payload)


@router.delete("/model-endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_endpoint(request: Request, auth: Admin, endpoint_id: str) -> Response:
    await _endpoints(request).delete(_actor(auth), endpoint_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/model-endpoints/{endpoint_id}/preflight", response_model=ModelEndpoint)
async def preflight_model_endpoint(
    request: Request, auth: Admin, endpoint_id: str
) -> ModelEndpoint:
    return await _endpoints(request).preflight(_actor(auth), endpoint_id)


@router.post(
    "/model-endpoints/{endpoint_id}/sync",
    response_model=ModelEndpointSyncRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_model_endpoint(
    request: Request, auth: Admin, endpoint_id: str
) -> ModelEndpointSyncRun:
    return await _endpoints(request).start_sync(_actor(auth), endpoint_id)


@router.get(
    "/model-endpoints/{endpoint_id}/sync-runs", response_model=list[ModelEndpointSyncRun]
)
async def list_model_endpoint_sync_runs(
    request: Request, auth: Admin, endpoint_id: str
) -> list[ModelEndpointSyncRun]:
    return await _endpoints(request).list_sync_runs(_actor(auth), endpoint_id)


@router.get(
    "/model-endpoints/{endpoint_id}/sync-runs/{run_id}", response_model=ModelEndpointSyncRun
)
async def get_model_endpoint_sync_run(
    request: Request, auth: Admin, endpoint_id: str, run_id: str
) -> ModelEndpointSyncRun:
    return await _endpoints(request).get_sync_run(_actor(auth), run_id)


@router.get(
    "/model-endpoints/{endpoint_id}/deployments", response_model=list[ObservedModelDeployment]
)
async def list_model_endpoint_deployments(
    request: Request, auth: Admin, endpoint_id: str
) -> list[ObservedModelDeployment]:
    return await _endpoints(request).list_deployments(_actor(auth), endpoint_id)


@router.get(
    "/model-endpoints/{endpoint_id}/available-models",
    response_model=list[ObservedAvailableModel],
)
async def list_model_endpoint_available_models(
    request: Request, auth: Admin, endpoint_id: str
) -> list[ObservedAvailableModel]:
    return await _endpoints(request).list_available_models(_actor(auth), endpoint_id)


@router.get(
    "/model-endpoints/{endpoint_id}/runtime-access", response_model=list[GatewayRuntimeAccess]
)
async def get_model_endpoint_runtime_access(
    request: Request, auth: Admin, endpoint_id: str
) -> list[GatewayRuntimeAccess]:
    return await _endpoints(request).runtime_access(_actor(auth), endpoint_id)


@router.get("/mcp-endpoints", response_model=list[McpEndpoint])
async def list_mcp_endpoints(request: Request, auth: Admin) -> list[McpEndpoint]:
    return await _mcp_endpoints(request).list_endpoints(_actor(auth))


@router.post("/mcp-endpoints", response_model=McpEndpoint, status_code=status.HTTP_201_CREATED)
async def register_mcp_endpoint(
    request: Request, auth: Admin, payload: McpEndpointCreate
) -> McpEndpoint:
    return await _mcp_endpoints(request).register(_actor(auth), payload)


@router.get("/mcp-endpoints/{endpoint_id}", response_model=McpEndpoint)
async def get_mcp_endpoint(request: Request, auth: Admin, endpoint_id: str) -> McpEndpoint:
    return await _mcp_endpoints(request).get_endpoint(_actor(auth), endpoint_id)


@router.patch("/mcp-endpoints/{endpoint_id}", response_model=McpEndpoint)
async def update_mcp_endpoint(
    request: Request, auth: Admin, endpoint_id: str, payload: McpEndpointUpdate
) -> McpEndpoint:
    return await _mcp_endpoints(request).update(_actor(auth), endpoint_id, payload)


@router.delete("/mcp-endpoints/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_endpoint(request: Request, auth: Admin, endpoint_id: str) -> Response:
    await _mcp_endpoints(request).delete(_actor(auth), endpoint_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/mcp-endpoints/{endpoint_id}/preflight", response_model=McpEndpoint)
async def preflight_mcp_endpoint(request: Request, auth: Admin, endpoint_id: str) -> McpEndpoint:
    return await _mcp_endpoints(request).preflight(_actor(auth), endpoint_id)


@router.post(
    "/mcp-endpoints/{endpoint_id}/sync",
    response_model=McpEndpointSyncRun,
    status_code=status.HTTP_202_ACCEPTED,
)
async def sync_mcp_endpoint(
    request: Request, auth: Admin, endpoint_id: str
) -> McpEndpointSyncRun:
    return await _mcp_endpoints(request).start_sync(_actor(auth), endpoint_id)


@router.get("/mcp-endpoints/{endpoint_id}/sync-runs", response_model=list[McpEndpointSyncRun])
async def list_mcp_endpoint_sync_runs(
    request: Request, auth: Admin, endpoint_id: str
) -> list[McpEndpointSyncRun]:
    return await _mcp_endpoints(request).list_sync_runs(_actor(auth), endpoint_id)


@router.get(
    "/mcp-endpoints/{endpoint_id}/sync-runs/{run_id}", response_model=McpEndpointSyncRun
)
async def get_mcp_endpoint_sync_run(
    request: Request, auth: Admin, endpoint_id: str, run_id: str
) -> McpEndpointSyncRun:
    return await _mcp_endpoints(request).get_sync_run(_actor(auth), run_id)


@router.get("/mcp-endpoints/{endpoint_id}/tools", response_model=list[ObservedMcpTool])
async def list_mcp_endpoint_tools(
    request: Request, auth: Admin, endpoint_id: str
) -> list[ObservedMcpTool]:
    return await _mcp_endpoints(request).list_tools(_actor(auth), endpoint_id)


@router.get("/gateways/{gateway_id}/mcp-servers", response_model=list[ObservedMcpServer])
async def list_gateway_mcp_servers(
    request: Request, auth: Admin, gateway_id: str
) -> list[ObservedMcpServer]:
    return await _gateways(request).list_observed_mcp_servers(_actor(auth), gateway_id)


@router.get("/gateways/{gateway_id}/importable-apis", response_model=ModelApiCandidateList)
async def list_importable_apis(
    request: Request, auth: Admin, gateway_id: str
) -> ModelApiCandidateList:
    return await _gateways(request).list_importable_apis(_actor(auth), gateway_id)


@router.get(
    "/gateways/{gateway_id}/importable-mcp-servers", response_model=McpServerCandidateList
)
async def list_importable_mcp_servers(
    request: Request, auth: Admin, gateway_id: str
) -> McpServerCandidateList:
    return await _gateways(request).list_importable_mcp_servers(_actor(auth), gateway_id)


@router.post(
    "/gateways/{gateway_id}/import-apis",
    response_model=list[ModelApi],
    status_code=status.HTTP_201_CREATED,
)
async def import_model_apis(
    request: Request, auth: Admin, gateway_id: str, payload: ImportRequest
) -> list[ModelApi]:
    return await _gateways(request).import_model_apis(_actor(auth), gateway_id, payload)


@router.post(
    "/gateways/{gateway_id}/import-mcp-servers",
    response_model=list[McpServer],
    status_code=status.HTTP_201_CREATED,
)
async def import_mcp_servers(
    request: Request, auth: Admin, gateway_id: str, payload: ImportRequest
) -> list[McpServer]:
    return await _gateways(request).import_mcp_servers(_actor(auth), gateway_id, payload)


@router.get("/model-apis", response_model=list[ModelApi])
async def list_model_apis(
    request: Request, auth: Admin, gateway: str | None = None
) -> list[ModelApi]:
    return await _gateways(request).list_model_apis(_actor(auth), gateway)


@router.get("/mcp-servers", response_model=list[McpServer])
async def list_mcp_servers(
    request: Request, auth: Admin, gateway: str | None = None
) -> list[McpServer]:
    return await _gateways(request).list_mcp_servers(_actor(auth), gateway)


@router.delete("/model-apis/{model_api_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model_api(request: Request, auth: Admin, model_api_id: str) -> Response:
    await _gateways(request).delete_model_api(_actor(auth), model_api_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/mcp-servers/{mcp_server_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_mcp_server(request: Request, auth: Admin, mcp_server_id: str) -> Response:
    await _gateways(request).delete_mcp_server(_actor(auth), mcp_server_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.patch("/model-apis/{model_api_id}/catalog", response_model=ModelApi)
async def update_model_api_catalog(
    request: Request, auth: Admin, model_api_id: str, payload: CatalogEntryUpdate
) -> ModelApi:
    return await _gateways(request).update_model_api_catalog(
        _actor(auth), model_api_id, payload
    )


@router.patch("/mcp-servers/{mcp_server_id}/catalog", response_model=McpServer)
async def update_mcp_server_catalog(
    request: Request, auth: Admin, mcp_server_id: str, payload: CatalogEntryUpdate
) -> McpServer:
    return await _gateways(request).update_mcp_server_catalog(
        _actor(auth), mcp_server_id, payload
    )


@router.get("/entitlements", response_model=list[Entitlement])
async def list_entitlements(
    request: Request,
    auth: Admin,
    subject: str | None = None,
    resource: str | None = None,
) -> list[Entitlement]:
    return await _entitlements(request).list_entitlements(
        _actor(auth), subject_id=subject, resource_id=resource
    )


@router.post("/entitlements", response_model=Entitlement, status_code=status.HTTP_201_CREATED)
async def create_entitlement(
    request: Request, auth: Admin, payload: EntitlementCreate
) -> Entitlement:
    return await _entitlements(request).create_entitlement(_actor(auth), payload)


@router.get("/entitlements/resolve", response_model=list[ResolvedEntitlement])
async def resolve_entitlements(
    request: Request,
    auth: Admin,
    principal_id: Annotated[str, Query(alias="principalId")],
) -> list[ResolvedEntitlement]:
    """Effective access for one principal, including what a group grant contributes."""

    return await _entitlements(request).resolve_for_principal(_actor(auth), principal_id)


@router.get("/entitlements/{entitlement_id}", response_model=Entitlement)
async def get_entitlement(request: Request, auth: Admin, entitlement_id: str) -> Entitlement:
    return await _entitlements(request).get_entitlement(_actor(auth), entitlement_id)


@router.patch("/entitlements/{entitlement_id}", response_model=Entitlement)
async def update_entitlement(
    request: Request, auth: Admin, entitlement_id: str, payload: EntitlementUpdate
) -> Entitlement:
    return await _entitlements(request).update_entitlement(
        _actor(auth), entitlement_id, payload
    )


@router.delete("/entitlements/{entitlement_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_entitlement(request: Request, auth: Admin, entitlement_id: str) -> Response:
    await _entitlements(request).delete_entitlement(_actor(auth), entitlement_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/access-requests", response_model=list[AccessRequest])
async def list_access_requests(
    request: Request, auth: Admin, state: str | None = None
) -> list[AccessRequest]:
    return await _entitlements(request).list_access_requests(_actor(auth), state=state)


@router.post("/access-requests/{request_id}/approve", response_model=AccessRequest)
async def approve_access_request(
    request: Request, auth: Admin, request_id: str, payload: AccessRequestDecision
) -> AccessRequest:
    return await _entitlements(request).decide_access_request(
        _actor(auth), request_id, state=AccessRequestState.APPROVED, note=payload.note
    )


@router.post("/access-requests/{request_id}/deny", response_model=AccessRequest)
async def deny_access_request(
    request: Request, auth: Admin, request_id: str, payload: AccessRequestDecision
) -> AccessRequest:
    return await _entitlements(request).decide_access_request(
        _actor(auth), request_id, state=AccessRequestState.DENIED, note=payload.note
    )
