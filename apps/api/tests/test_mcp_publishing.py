import asyncio
from typing import Any

import pytest
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import (
    build_arm_client,
    build_gateway_service,
    build_mcp_publishing_service,
    build_mcp_service,
    build_publishing_service,
    reviewed_unpublish,
)
from mcp_double import FakeMcpServer
from mosaic_api.cost_centers import CostCenterBook, PendingRecheck, general_cost_center
from mosaic_api.domain import (
    MCP_MESSAGE_PATH,
    MCP_METADATA_API_NAME,
    MCP_METADATA_OPERATION,
    ApimResourceId,
    AuditEvent,
    CapabilitySupport,
    Entitlement,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementResourceKind,
    EntitlementSubject,
    EntitlementSubjectKind,
    Gateway,
    GatewayCreate,
    GatewayUpdate,
    ImportRequest,
    ManagementMode,
    McpAccessGrant,
    McpAccessSnapshot,
    McpAuthMode,
    McpEndpoint,
    McpEndpointStatus,
    McpInventorySummary,
    McpModelCaller,
    McpModelCallerUpdate,
    McpPublication,
    McpPublicationCreate,
    McpPublicationUpdate,
    McpServer,
    ModelAccessSettings,
    ModelApi,
    ModelProvider,
    Principal,
    PrincipalKind,
    PrincipalUpdate,
    Publication,
    PublicationStatus,
    PublishAction,
    PublishedResource,
    PublishedResourceKind,
    PublishPlan,
    PublishRun,
    PublishRunStatus,
    PublishStepStatus,
    RequestEnforcement,
    TokenEnforcement,
    canonical_mcp_url,
    mcp_backend_url,
    mcp_metadata_url_template,
    mcp_server_id,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, UpstreamError, ValidationError
from mosaic_api.integrations.apim import ApimWriter
from mosaic_api.model_pools import ModelPool, PoolModel
from mosaic_api.observed import ObservedApi
from mosaic_api.repositories import (
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryMcpEndpointRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import McpEndpointService
from mosaic_api.services.directory import Actor, DirectoryService
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest
from mosaic_api.services.publishing import DENY_ALL_FRAGMENT, DENY_ALL_POLICY

TENANT_ID = "33333333-3333-3333-3333-333333333333"
ACTOR = Actor(object_id="admin-object-id", tenant_id=TENANT_ID)


class Harness:
    def __init__(self) -> None:
        self.apim = FakeApim(permissions=CONTRIBUTOR_PERMISSIONS)
        self.gateway_repository = InMemoryGatewayRepository()
        self.mcp_repository = InMemoryMcpEndpointRepository()
        self.directory_repository = InMemoryDirectoryRepository()
        self.entitlement_repository = InMemoryEntitlementRepository()
        self.gateways = build_gateway_service(self.apim, self.gateway_repository)
        self.service = build_mcp_publishing_service(
            self.apim,
            self.gateway_repository,
            self.mcp_repository,
            directory_repository=self.directory_repository,
            entitlement_repository=self.entitlement_repository,
        )
        self.gateway_id = ""
        self.endpoint_id = ""

    async def setup(self) -> None:
        gateway = await self.gateways.register(ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID))
        await self.gateways.sync_now(ACTOR, gateway.id)
        gateway = await self.gateways.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode=ManagementMode.MANAGE)
        )
        self.gateway_id = gateway.id
        endpoint = McpEndpoint(
            id="mcp-endpoint-orders",
            tenant_id=TENANT_ID,
            name="Orders MCP",
            endpoint="https://mcp.contoso.test/mcp",
            status=McpEndpointStatus.CONNECTED,
            inventory=McpInventorySummary(tools=2),
        )
        saved = await self.mcp_repository.save_endpoint(
            endpoint,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=TENANT_ID,
                action="mcpEndpoint.created",
                resource_type="mcpEndpoint",
                resource_id=endpoint.id,
                actor_object_id=ACTOR.object_id,
            ),
        )
        self.endpoint_id = saved.id

    async def create(self, **overrides: object) -> str:
        payload = {"gateway_id": self.gateway_id, "mcp_endpoint_id": self.endpoint_id}
        payload.update(overrides)
        publication = await self.service.create(ACTOR, McpPublicationCreate.model_validate(payload))
        return publication.id

    async def set_endpoint(self, **updates: object) -> McpEndpoint:
        endpoint = await self.mcp_repository.get_endpoint(TENANT_ID, self.endpoint_id)
        assert endpoint is not None
        updated = endpoint.model_copy(update=updates)
        return await self.mcp_repository.save_endpoint(
            updated,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=TENANT_ID,
                action="mcpEndpoint.updated",
                resource_type="mcpEndpoint",
                resource_id=updated.id,
                actor_object_id=ACTOR.object_id,
            ),
        )

    async def save_gateway(self, gateway: Gateway) -> None:
        # Management mode is authored, so an observation write would keep the stored value.
        await self.gateway_repository.save_gateway(
            gateway,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=gateway.tenant_id,
                action="gateway.updated",
                resource_type="gateway",
                resource_id=gateway.id,
                actor_object_id=ACTOR.object_id,
            ),
        )

    async def add_endpoint(self, endpoint_id: str, name: str = "Other MCP") -> str:
        endpoint = McpEndpoint(
            id=endpoint_id,
            tenant_id=TENANT_ID,
            name=name,
            endpoint=f"https://{endpoint_id}.contoso.test/mcp",
            status=McpEndpointStatus.CONNECTED,
            inventory=McpInventorySummary(tools=1),
        )
        saved = await self.mcp_repository.save_endpoint(
            endpoint,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=TENANT_ID,
                action="mcpEndpoint.created",
                resource_type="mcpEndpoint",
                resource_id=endpoint.id,
                actor_object_id=ACTOR.object_id,
            ),
        )
        return saved.id

    async def principal(
        self,
        principal_id: str,
        object_id: str,
        *,
        kind: PrincipalKind = PrincipalKind.USER,
        label: str | None = None,
    ) -> Principal:
        principal = Principal(
            id=principal_id,
            tenant_id=TENANT_ID,
            object_id=object_id,
            kind=kind,
            label=label or principal_id,
        )
        return await self.directory_repository.save_principal(
            principal,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=TENANT_ID,
                action="principal.saved",
                resource_type="principal",
                resource_id=principal.id,
                actor_object_id=ACTOR.object_id,
            ),
        )

    async def entitlement(
        self,
        entitlement_id: str,
        subject_kind: EntitlementSubjectKind,
        subject_id: str,
        resource_id: str,
        *,
        enabled: bool = True,
        enforcement: EntitlementEnforcement | None = None,
    ) -> Entitlement:
        entitlement = Entitlement(
            id=entitlement_id,
            tenant_id=TENANT_ID,
            subject=EntitlementSubject(kind=subject_kind, id=subject_id),
            resource=EntitlementResource(kind=EntitlementResourceKind.MCP_SERVER, id=resource_id),
            enabled=enabled,
            enforcement=enforcement,
        )
        return await self.entitlement_repository.save_entitlement(
            entitlement,
            AuditEvent(
                id=new_id("audit"),
                tenant_id=TENANT_ID,
                action="entitlement.saved",
                resource_type="entitlement",
                resource_id=entitlement.id,
                actor_object_id=ACTOR.object_id,
            ),
        )


def request_limits(calls: int = 10) -> EntitlementEnforcement:
    return EntitlementEnforcement(
        requests=RequestEnforcement(
            counter_key_expression="@('mcp-test')",
            calls=calls,
            renewal_period_seconds=60,
        )
    )


def token_limits() -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=TokenEnforcement(
            counter_key_expression="@('token-test')",
            tokens_per_minute=1000,
        )
    )


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.setup()
    return built


async def test_capability_reports_every_reason_and_happy_path(harness: Harness) -> None:
    assert (await harness.service.capability(ACTOR, harness.gateway_id)).supported is True

    gateway = await harness.gateway_repository.get_gateway(TENANT_ID, harness.gateway_id)
    assert gateway is not None
    base_capabilities = gateway.capabilities
    unwritable = gateway.model_copy(
        update={
            "management_mode": ManagementMode.OBSERVE,
            "access": gateway.access.model_copy(update={"can_write": False}),
            "capabilities": base_capabilities.model_copy(
                update={
                    "sku_name": "Consumption",
                    "mcp_servers": CapabilitySupport.UNAVAILABLE,
                    "gateway_url": None,
                }
            ),
        }
    )
    await harness.save_gateway(unwritable)
    no_runtime = build_mcp_publishing_service(
        harness.apim,
        harness.gateway_repository,
        harness.mcp_repository,
        directory_repository=harness.directory_repository,
        entitlement_repository=harness.entitlement_repository,
        runtime_client_id=None,
    )

    capability = await no_runtime.capability(ACTOR, harness.gateway_id)

    assert capability.supported is False
    assert any("managed mode" in reason for reason in capability.reasons)
    assert any("cannot write" in reason for reason in capability.reasons)
    assert any("Consumption" in reason for reason in capability.reasons)
    assert any("does not support MCP" in reason for reason in capability.reasons)
    assert any("Gateway URL is unknown" in reason for reason in capability.reasons)
    assert any("runtime client ID" in reason for reason in capability.reasons)

    non_guid = Actor(object_id=ACTOR.object_id, tenant_id="tenant-test")
    await harness.gateway_repository.record_gateway_state(
        unwritable.model_copy(
            update={
                "tenant_id": "tenant-test",
                "management_mode": ManagementMode.MANAGE,
                "access": gateway.access,
                "capabilities": base_capabilities,
            }
        )
    )
    invalid_tenant = await harness.service.capability(non_guid, harness.gateway_id)
    assert any("tenant ID" in reason for reason in invalid_tenant.reasons)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"auth_mode": McpAuthMode.API_KEY}, "API key"),
        ({"auth_mode": McpAuthMode.MANAGED_IDENTITY, "resource_audience": None}, "audience"),
        (
            {"capabilities": {"transport_type": "sse"}},
            "streamable",
        ),
        ({"status": McpEndpointStatus.UNSUPPORTED_TRANSPORT}, "unsupported transport"),
    ],
)
async def test_create_refuses_unsupported_endpoint_shapes(
    harness: Harness, updates: dict[str, object], message: str
) -> None:
    if "capabilities" in updates:
        endpoint = await harness.mcp_repository.get_endpoint(TENANT_ID, harness.endpoint_id)
        assert endpoint is not None
        updates = {
            "capabilities": endpoint.capabilities.model_copy(update=updates["capabilities"])
        }
    await harness.set_endpoint(**updates)

    with pytest.raises(ValidationError, match=message):
        await harness.create()


async def test_create_refuses_unmanaged_gateway(harness: Harness) -> None:
    gateway = await harness.gateway_repository.get_gateway(TENANT_ID, harness.gateway_id)
    assert gateway is not None
    await harness.save_gateway(
        gateway.model_copy(update={"management_mode": ManagementMode.OBSERVE})
    )

    with pytest.raises(ConflictError, match="managed mode"):
        await harness.create()


async def test_create_refuses_name_and_path_collisions(harness: Harness) -> None:
    model = Publication(
        id="publication-model",
        tenant_id=TENANT_ID,
        gateway_id=harness.gateway_id,
        model_endpoint_id="model-endpoint",
        deployment_name="gpt",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Model",
        api_name="mosaic-mcp-orders-mcp",
        api_path="model/path",
        backend_name="model-backend",
        fragment_name="model-fragment",
        product_name="model-product",
        subscription_name="model-sub",
        enforcement=TokenEnforcement(
            counter_key_expression="@('model')",
            tokens_per_minute=100,
        ),
        shape_version="test",
    )
    await harness.gateway_repository.save_publication(
        model,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="publication.created",
            resource_type="publication",
            resource_id=model.id,
            actor_object_id=ACTOR.object_id,
        ),
    )
    with pytest.raises(ConflictError, match="model publication"):
        await harness.create()

    await harness.gateway_repository.delete_publication(
        model,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="publication.removed",
            resource_type="publication",
            resource_id=model.id,
            actor_object_id=ACTOR.object_id,
        ),
    )
    first = await harness.create(api_name="mcp-one", api_path="mosaic/mcp/one")
    other_endpoint = await harness.add_endpoint("mcp-endpoint-other")
    with pytest.raises(ConflictError, match="MCP publication"):
        await harness.create(
            mcp_endpoint_id=other_endpoint,
            api_name="mcp-one",
            api_path="mosaic/mcp/two",
        )
    with pytest.raises(ConflictError, match="API path"):
        await harness.create(
            mcp_endpoint_id=other_endpoint,
            api_name="mcp-two",
            api_path="mosaic/mcp/one",
        )
    await harness.service.delete(ACTOR, first)


async def test_create_refuses_existing_server_observed_path_and_long_name(
    harness: Harness,
) -> None:
    deterministic = mcp_server_id(TENANT_ID, harness.gateway_id, "custom-mcp")
    await harness.gateway_repository.save_mcp_server(
        McpServer(
            id=deterministic,
            tenant_id=TENANT_ID,
            gateway_id=harness.gateway_id,
            api_name="custom-mcp",
            display_name="Existing",
            path="custom",
            imported_from_snapshot_id="snapshot",
        ),
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="mcpServer.imported",
            resource_type="mcpServer",
            resource_id=deterministic,
            actor_object_id=ACTOR.object_id,
        ),
    )
    with pytest.raises(ConflictError, match="deterministic ID"):
        await harness.create(api_name="custom-mcp")

    server = await harness.gateway_repository.get_mcp_server(TENANT_ID, deterministic)
    assert server is not None
    await harness.gateway_repository.delete_mcp_server(
        server,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="mcpServer.removed",
            resource_type="mcpServer",
            resource_id=deterministic,
            actor_object_id=ACTOR.object_id,
        ),
    )
    await harness.gateway_repository.replace_observed(
        TENANT_ID,
        harness.gateway_id,
        [
            ObservedApi(
                id="observed-api-clash",
                tenant_id=TENANT_ID,
                gateway_id=harness.gateway_id,
                snapshot_id="snapshot",
                name="customer-api",
                display_name="Customer API",
                path="mosaic/mcp/orders-mcp",
            )
        ],
        "snapshot",
    )
    with pytest.raises(ConflictError, match="already served"):
        await harness.create()

    with pytest.raises(ValidationError, match="76"):
        await harness.create(api_name="a" * 77)


async def test_create_materializes_mcp_server_record(harness: Harness) -> None:
    publication_id = await harness.create()

    publication = await harness.service.get_publication(ACTOR, publication_id)
    server = await harness.gateway_repository.get_mcp_server(TENANT_ID, publication.mcp_server_id)

    assert server is not None
    assert server.publication_id == publication.id
    assert server.api_name == publication.api_name
    assert server.tool_count == 2
    assert server.path == publication.api_path
    assert server.kind == "passthrough"
    assert server.transport_type == "streamable"
    assert server.endpoints[0].uri_template == "/mcp"
    assert server.subscription_required is False


async def test_update_renames_publication_and_server(harness: Harness) -> None:
    publication_id = await harness.create()

    updated = await harness.service.update(
        ACTOR, publication_id, McpPublicationUpdate(display_name="Renamed MCP")
    )

    server = await harness.gateway_repository.get_mcp_server(TENANT_ID, updated.mcp_server_id)
    assert updated.display_name == "Renamed MCP"
    assert server is not None
    assert server.display_name == "Renamed MCP"


async def test_delete_guards_and_success_remove_publication_and_server(harness: Harness) -> None:
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    guarded = publication.model_copy(
        update={
            "resources": [
                PublishedResource(
                    kind=PublishedResourceKind.API,
                    name=publication.api_name,
                    resource_id=f"{RESOURCE_ID}/apis/{publication.api_name}",
                    created_by_mosaic=True,
                )
            ]
        }
    )
    await harness.gateway_repository.record_mcp_publication_state(guarded)
    with pytest.raises(ConflictError, match="Unpublish"):
        await harness.service.delete(ACTOR, publication_id)

    await harness.gateway_repository.record_mcp_publication_state(
        guarded.model_copy(update={"resources": []})
    )
    await harness.entitlement(
        "entitlement-delete-guard",
        EntitlementSubjectKind.USER,
        "principal-user",
        publication.mcp_server_id,
    )
    with pytest.raises(ConflictError, match="Remove grants"):
        await harness.service.delete(ACTOR, publication_id)

    entitlement = await harness.entitlement_repository.get_entitlement(
        TENANT_ID, "entitlement-delete-guard"
    )
    assert entitlement is not None
    await harness.entitlement_repository.delete_entitlement(
        entitlement,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="entitlement.removed",
            resource_type="entitlement",
            resource_id=entitlement.id,
            actor_object_id=ACTOR.object_id,
        ),
    )
    await harness.service.delete(ACTOR, publication_id)
    assert await harness.gateway_repository.get_mcp_publication(TENANT_ID, publication_id) is None
    assert (
        await harness.gateway_repository.get_mcp_server(TENANT_ID, publication.mcp_server_id)
        is None
    )


def endpoint_service(harness: Harness) -> McpEndpointService:
    return build_mcp_service(
        FakeMcpServer(),
        repository=harness.mcp_repository,
        gateway_repository=harness.gateway_repository,
        entitlement_repository=harness.entitlement_repository,
    )


async def test_endpoint_delete_refuses_a_published_server_until_it_is_unpublished(
    harness: Harness,
) -> None:
    endpoints = endpoint_service(harness)
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    with pytest.raises(ConflictError, match="Unpublish Orders MCP") as refused:
        await endpoints.delete(ACTOR, harness.endpoint_id)
    assert [item["id"] for item in refused.value.details["publications"]] == [publication_id]
    assert await harness.mcp_repository.get_endpoint(TENANT_ID, harness.endpoint_id) is not None

    await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()
    await endpoints.delete(ACTOR, harness.endpoint_id)

    assert await harness.mcp_repository.get_endpoint(TENANT_ID, harness.endpoint_id) is None
    assert await harness.gateway_repository.get_mcp_publication(TENANT_ID, publication_id) is None
    assert (
        await harness.gateway_repository.get_mcp_server(TENANT_ID, publication.mcp_server_id)
        is None
    )


async def test_endpoint_delete_refuses_while_a_run_holds_the_publication(
    harness: Harness,
) -> None:
    endpoints = endpoint_service(harness)
    publication_id = await harness.create()
    await harness.gateway_repository.acquire_publication_lock(TENANT_ID, publication_id, "run-1")

    with pytest.raises(ConflictError, match="Unpublish Orders MCP"):
        await endpoints.delete(ACTOR, harness.endpoint_id)
    assert await harness.mcp_repository.get_endpoint(TENANT_ID, harness.endpoint_id) is not None
    assert await harness.gateway_repository.get_mcp_publication(TENANT_ID, publication_id)


async def test_endpoint_delete_keeps_a_draft_that_grants_point_at(harness: Harness) -> None:
    endpoints = endpoint_service(harness)
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    await harness.entitlement(
        "entitlement-draft",
        EntitlementSubjectKind.USER,
        "principal-user",
        publication.mcp_server_id,
    )

    with pytest.raises(ConflictError, match="Remove the grants") as refused:
        await endpoints.delete(ACTOR, harness.endpoint_id)
    assert refused.value.details["entitlements"] == {publication_id: ["entitlement-draft"]}
    assert await harness.mcp_repository.get_endpoint(TENANT_ID, harness.endpoint_id) is not None
    assert await harness.gateway_repository.get_mcp_publication(TENANT_ID, publication_id)
    assert await harness.gateway_repository.get_mcp_server(TENANT_ID, publication.mcp_server_id)


async def test_plan_snapshot_rules_and_warnings(harness: Harness) -> None:
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    user = await harness.principal(
        "principal-user",
        "44444444-4444-4444-4444-444444444444",
        label="Ada",
    )
    group_a = await harness.principal(
        "principal-group-a",
        "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA",
        kind=PrincipalKind.SECURITY_GROUP,
        label="Group A",
    )
    await harness.principal(
        "principal-group-b",
        "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        kind=PrincipalKind.SECURITY_GROUP,
        label="Group B",
    )
    direct = await harness.entitlement(
        "entitlement-a-direct",
        EntitlementSubjectKind.USER,
        user.id,
        publication.mcp_server_id,
        enforcement=request_limits(10),
    )
    await harness.entitlement(
        "entitlement-b-group",
        EntitlementSubjectKind.SECURITY_GROUP,
        group_a.id,
        publication.mcp_server_id,
        enforcement=request_limits(20),
    )
    await harness.entitlement(
        "entitlement-c-group",
        EntitlementSubjectKind.SECURITY_GROUP,
        "principal-group-b",
        publication.mcp_server_id,
        enforcement=request_limits(30),
    )
    await harness.entitlement(
        "entitlement-d-mosaic-group",
        EntitlementSubjectKind.GROUP,
        "mosaic-group",
        publication.mcp_server_id,
    )
    await harness.entitlement(
        "entitlement-e-token",
        EntitlementSubjectKind.USER,
        user.id,
        publication.mcp_server_id,
        enforcement=token_limits(),
    )
    await harness.entitlement(
        "entitlement-f-missing",
        EntitlementSubjectKind.USER,
        "missing-principal",
        publication.mcp_server_id,
    )
    disabled_previous = publication.model_copy(
        update={
            "applied_access": McpAccessSnapshot(
                version=4,
                audience="22222222-2222-2222-2222-222222222222",
                grants=[
                    McpAccessGrant(
                        entitlement_id="entitlement-z-vanished",
                        subject=EntitlementSubject(kind=EntitlementSubjectKind.USER, id=user.id),
                        object_id=user.object_id,
                        display_name="Vanished",
                        enabled=True,
                        intent_digest="old",
                    )
                ],
            )
        }
    )
    await harness.gateway_repository.record_mcp_publication_state(disabled_previous)
    harness.service._security_group_claims = False

    plan = await harness.service.plan(ACTOR, publication_id)
    snapshot = plan.mcp_access_snapshot

    assert snapshot is not None
    assert snapshot.version == 5
    assert snapshot.audience == "22222222-2222-2222-2222-222222222222"
    assert [grant.entitlement_id for grant in snapshot.grants] == sorted(
        grant.entitlement_id for grant in snapshot.grants
    )
    grants = {grant.entitlement_id: grant for grant in snapshot.grants}
    assert grants["entitlement-b-group"].object_id == group_a.object_id.casefold()
    assert grants["entitlement-z-vanished"].enabled is False
    assert grants[direct.id].intent_digest == entitlement_intent_digest(
        direct, user, cost_center_intent(direct, user, CostCenterBook(TENANT_ID, [], None))
    )
    assert any("MOSAIC group" in warning for warning in plan.warnings)
    assert any("token limits" in warning for warning in plan.warnings)
    assert any("missing or mismatched principal" in warning for warning in plan.warnings)
    assert any("Group claims" in warning for warning in plan.warnings)
    assert any("most generous" in warning for warning in plan.warnings)


async def test_plan_requires_runtime_client_id(harness: Harness) -> None:
    publication_id = await harness.create()
    no_runtime = build_mcp_publishing_service(
        harness.apim,
        harness.gateway_repository,
        harness.mcp_repository,
        directory_repository=harness.directory_repository,
        entitlement_repository=harness.entitlement_repository,
        runtime_client_id=None,
    )

    with pytest.raises(ConflictError, match="runtime client ID"):
        await no_runtime.plan(ACTOR, publication_id)


async def test_plan_new_publication_is_fail_closed_before_activation(harness: Harness) -> None:
    publication_id = await harness.create()

    plan = await harness.service.plan(ACTOR, publication_id)

    assert plan.target == "mcp"
    assert plan.mcp_access_snapshot is not None
    assert [step.kind for step in plan.steps] == [
        # The gateway's blocked list, before the fragment that reads it. See ADR 0023.
        PublishedResourceKind.NAMED_VALUE,
        PublishedResourceKind.BACKEND,
        PublishedResourceKind.POLICY_FRAGMENT,
        PublishedResourceKind.API,
        PublishedResourceKind.API_POLICY,
        PublishedResourceKind.API,
        PublishedResourceKind.API_OPERATION,
        PublishedResourceKind.API_OPERATION_POLICY,
        PublishedResourceKind.API,
    ]
    assert plan.steps[0].name == "mosaic-blocked-cost-centers"
    assert plan.steps[0].action == PublishAction.CREATE
    assert plan.steps[3].stage == "prepare"
    assert plan.steps[5].name == MCP_METADATA_API_NAME
    assert plan.steps[-1].stage == "activate"
    assert any("0 bytes" in warning for warning in plan.warnings)


async def test_plan_refuses_unowned_target_resource(harness: Harness) -> None:
    publication_id = await harness.create(api_name="owned-name")
    harness.apim.seed("apis/owned-name", {"properties": {"type": "mcp"}})

    with pytest.raises(ConflictError, match=r"does not own|will not replace"):
        await harness.service.plan(ACTOR, publication_id)


async def test_plan_backend_change_denies_first(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, publication_id, run.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    await harness.set_endpoint(endpoint="https://new-mcp.contoso.test/mcp")

    changed = await harness.service.plan(ACTOR, publication_id)

    # The blocked list the first apply created stays, so the plan only keeps it.
    assert changed.steps[0].name == "mosaic-blocked-cost-centers"
    assert changed.steps[0].action == PublishAction.NO_CHANGE
    assert changed.steps[1].kind == PublishedResourceKind.API_POLICY
    assert changed.steps[1].name == "mosaic-mcp-orders-mcp"
    assert changed.steps[1].stage == "prepare"


# -- the backend URL: API Management adds /mcp when it forwards a call --------------------------

BACKEND = "backends/mosaic-mcp-orders-mcp"
REACHABLE = [
    ("https://mcp.contoso.test/mcp", "https://mcp.contoso.test"),
    ("https://mcp.contoso.test/runtime/webhooks/mcp", "https://mcp.contoso.test/runtime/webhooks"),
    ("https://mcp.contoso.test:8443/api/mcp", "https://mcp.contoso.test:8443/api"),
]


@pytest.mark.parametrize(
    ("endpoint", "backend"),
    [
        *REACHABLE,
        # A trailing slash is ignored, as it is when a server is registered.
        ("https://mcp.contoso.test/mcp/", "https://mcp.contoso.test"),
        # Only the final segment goes.
        ("https://mcp.contoso.test/mcp/mcp", "https://mcp.contoso.test/mcp"),
    ],
)
def test_the_backend_url_is_the_endpoint_without_its_final_mcp_segment(
    endpoint: str, backend: str
) -> None:
    assert mcp_backend_url(endpoint) == backend
    # API Management calls the backend URL with /mcp added: the server MOSAIC registered.
    assert f"{backend}/{MCP_MESSAGE_PATH}" == canonical_mcp_url(endpoint)


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("https://mcp.contoso.test/api/stream", "ends in /mcp"),
        ("https://mcp.contoso.test/sse", "ends in /mcp"),
        ("https://mcp.contoso.test/", "ends in /mcp"),
        ("https://mcp.contoso.test", "ends in /mcp"),
        ("https://mcp.contoso.test/toolsmcp", "ends in /mcp"),
        ("https://mcp.contoso.test/MCP", "ends in /mcp"),
        ("https://mcp.contoso.test/mcp/tools", "ends in /mcp"),
        ("https://mcp.contoso.test/mcp?tenant=contoso", "query string"),
        ("https://mcp.contoso.test/mcp#tools", "query string or fragment"),
    ],
)
def test_no_backend_url_is_derived_for_an_endpoint_api_management_cant_reach(
    endpoint: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        mcp_backend_url(endpoint)


@pytest.mark.parametrize(("endpoint", "backend"), REACHABLE)
async def test_apply_points_the_backend_at_the_endpoint_without_its_final_mcp(
    harness: Harness, endpoint: str, backend: str
) -> None:
    """API Management forwards a call to ``/{api_path}/mcp`` to the backend URL with ``/mcp``
    added. A backend at the full endpoint sent every call to ``.../mcp/mcp``, which answered 404
    in Phase 11's M5 journey."""

    await harness.set_endpoint(endpoint=endpoint)
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    assert (await harness.service.get_run(ACTOR, publication_id, run.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    [written] = [
        call["body"]
        for call in harness.apim.http_calls
        if call["method"] == "PUT" and call["path"] == BACKEND
    ]
    assert written == {
        "properties": {
            "title": "MOSAIC backend for Orders MCP",
            "url": backend,
            "protocol": "http",
        }
    }
    assert f"{written['properties']['url']}/{MCP_MESSAGE_PATH}" == endpoint


async def test_a_backend_at_the_full_endpoint_is_denied_then_corrected(harness: Harness) -> None:
    """A publication applied before MOSAIC derived the backend URL points it at the registered
    endpoint itself. Its next plan replaces the backend, behind the deny that guards every
    backend change."""

    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    harness.apim.seed(
        BACKEND,
        {
            "properties": {
                "title": "MOSAIC backend for Orders MCP",
                "url": "https://mcp.contoso.test/mcp",
                "protocol": "http",
            }
        },
    )

    replanned = await harness.service.plan(ACTOR, publication_id)

    deny, backend = replanned.steps[1:3]
    assert (deny.kind, deny.name, deny.stage) == (
        PublishedResourceKind.API_POLICY,
        "mosaic-mcp-orders-mcp",
        "prepare",
    )
    assert deny.reason == "Deny calls before changing the MCP backend."
    assert (backend.kind, backend.name, backend.action) == (
        PublishedResourceKind.BACKEND,
        "mosaic-mcp-orders-mcp",
        PublishAction.UPDATE,
    )
    harness.apim.http_calls.clear()

    run = await harness.service.apply(ACTOR, publication_id, replanned.id)
    await harness.service.wait_for_idle()

    assert (await harness.service.get_run(ACTOR, publication_id, run.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    puts = [call for call in harness.apim.http_calls if call["method"] == "PUT"]
    paths = [call["path"] for call in puts]
    policy = paths.index("apis/mosaic-mcp-orders-mcp/policies/policy")
    assert puts[policy]["body"]["properties"]["value"] == DENY_ALL_POLICY
    assert policy < paths.index(BACKEND)
    assert harness.apim.written[BACKEND]["properties"]["url"] == "https://mcp.contoso.test"
    # Once the backend is right, a plan has nothing to deny first.
    settled = await harness.service.plan(ACTOR, publication_id)
    assert settled.steps[1].kind == PublishedResourceKind.BACKEND


@pytest.mark.parametrize(
    ("endpoint", "message"),
    [
        ("https://mcp.contoso.test/api/stream", "ends in /mcp"),
        ("https://mcp.contoso.test/mcp?tenant=contoso", "query string"),
    ],
)
async def test_create_refuses_an_endpoint_no_backend_url_reaches(
    harness: Harness, endpoint: str, message: str
) -> None:
    await harness.set_endpoint(endpoint=endpoint)

    with pytest.raises(ValidationError, match=message) as refused:
        await harness.create()

    assert refused.value.details == {"mcpEndpointId": harness.endpoint_id}
    assert await harness.service.list_publications(ACTOR, harness.gateway_id) == []


async def test_plan_and_apply_refuse_an_endpoint_no_backend_url_reaches(harness: Harness) -> None:
    """A publication created, or a plan reviewed, before MOSAIC refused such a URL goes no
    further, and nothing is written to API Management."""

    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.set_endpoint(endpoint="https://mcp.contoso.test/api/stream")
    writes = len(harness.apim.writes)

    with pytest.raises(ValidationError, match="ends in /mcp"):
        await harness.service.plan(ACTOR, publication_id)
    with pytest.raises(ValidationError, match="ends in /mcp"):
        await harness.service.apply(ACTOR, publication_id, plan.id)

    assert len(harness.apim.writes) == writes
    assert await harness.gateway_repository.get_publication_lock(TENANT_ID, publication_id) is None


async def test_unpublish_needs_no_backend_url(harness: Harness) -> None:
    """Unpublishing deletes what the publication owns by name, so a server MOSAIC can no longer
    publish can still be taken down."""

    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    await harness.set_endpoint(endpoint="https://mcp.contoso.test/api/stream")

    unpublished = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    assert (await harness.service.get_run(ACTOR, publication_id, unpublished.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    assert BACKEND not in harness.apim.written


async def test_apply_writes_mcp_api_policy_and_records_publication(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.SUCCEEDED
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.access_state == "applied"
    assert publication.applied_access is not None
    assert f"apis/{publication.api_name}/policies/policy" in harness.apim.write_paths("PUT")
    assert (
        f"apis/{MCP_METADATA_API_NAME}/operations/{publication.metadata_api_name}"
        "/policies/policy" in harness.apim.write_paths("PUT")
    )
    mcp_api_puts = [
        call
        for call in harness.apim.http_calls
        if call["method"] == "PUT" and call["path"] == f"apis/{publication.api_name}"
    ]
    assert [call["api_version"] for call in mcp_api_puts] == [
        "2025-09-01-preview",
        "2025-09-01-preview",
    ]
    assert mcp_api_puts[0]["body"]["properties"]["subscriptionRequired"] is True
    assert mcp_api_puts[-1]["body"]["properties"] == {
        "type": "mcp",
        "displayName": publication.display_name,
        "description": f"Published by MOSAIC for {publication.display_name}.",
        "path": publication.api_path,
        "protocols": ["https"],
        "backendId": publication.backend_name,
        "subscriptionRequired": False,
        "mcpProperties": {"transportType": "streamable"},
    }
    assert all(resource.created_by_mosaic for resource in publication.resources)
    assert publication.applied_access == plan.mcp_access_snapshot
    assert await harness.gateway_repository.get_publication_lock(TENANT_ID, publication_id) is None


async def test_apply_rejects_stale_plan_and_mismatched_plan(harness: Harness) -> None:
    first_id = await harness.create(api_name="first-mcp", api_path="mosaic/mcp/first")
    other_endpoint = await harness.add_endpoint("mcp-endpoint-second")
    second_id = await harness.create(
        mcp_endpoint_id=other_endpoint,
        api_name="second-mcp",
        api_path="mosaic/mcp/second",
    )
    first_plan = await harness.service.plan(ACTOR, first_id)
    await harness.service.plan(ACTOR, first_id)
    second_plan = await harness.service.plan(ACTOR, second_id)

    with pytest.raises(ConflictError, match="latest plan"):
        await harness.service.apply(ACTOR, first_id, first_plan.id)
    with pytest.raises(ConflictError, match=r"latest plan|not the latest"):
        await harness.service.apply(ACTOR, first_id, second_plan.id)

    latest = await harness.service.plan(ACTOR, first_id)
    await harness.service.update(ACTOR, first_id, McpPublicationUpdate(display_name="Changed"))
    with pytest.raises(ConflictError, match=r"changed|plan again"):
        await harness.service.apply(ACTOR, first_id, latest.id)


@pytest.mark.parametrize(
    "path",
    [
        "backends/mosaic-mcp-orders-mcp",
        "policyFragments/mosaic-mcp-orders-mcp",
        "apis/mosaic-mcp-orders-mcp",
        "apis/mosaic-mcp-orders-mcp/policies/policy",
        "apis/mosaic-mcp-metadata",
        "apis/mosaic-mcp-metadata/operations/mosaic-mcp-orders-mcp-prm",
        "apis/mosaic-mcp-metadata/operations/mosaic-mcp-orders-mcp-prm/policies/policy",
    ],
)
async def test_apply_failures_establish_fail_closed_state(
    harness: Harness, path: str
) -> None:
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    principal = await harness.principal(
        "principal-failure",
        "55555555-5555-5555-5555-555555555555",
    )
    await harness.entitlement(
        "entitlement-failure",
        EntitlementSubjectKind.USER,
        principal.id,
        publication.mcp_server_id,
        enforcement=request_limits(),
    )
    plan = await harness.service.plan(ACTOR, publication_id)
    harness.apim.fail_write(path)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    failed = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.FAILED
    assert failed.status == PublicationStatus.FAILED
    assert failed.access_state == "failed"
    assert failed.applied_access is not None
    assert all(not grant.enabled for grant in failed.applied_access.grants)
    assert await harness.gateway_repository.get_publication_lock(TENANT_ID, publication_id) is None
    policy_writes = [
        call
        for call in harness.apim.http_calls
        if call["method"] == "PUT"
        and call["path"] == "apis/mosaic-mcp-orders-mcp/policies/policy"
    ]
    fragment_writes = [
        call
        for call in harness.apim.http_calls
        if call["method"] == "PUT" and call["path"] == "policyFragments/mosaic-mcp-orders-mcp"
    ]
    if path not in {
        "backends/mosaic-mcp-orders-mcp",
        "policyFragments/mosaic-mcp-orders-mcp",
        "apis/mosaic-mcp-orders-mcp",
    }:
        policy_denied = any(
            call["body"]["properties"]["value"] == DENY_ALL_POLICY
            for call in policy_writes
        )
        fragment_denied = any(
            call["body"]["properties"]["value"] == DENY_ALL_FRAGMENT
            for call in fragment_writes
        )
        assert policy_denied or fragment_denied


async def test_apply_activate_failure_is_denied_and_not_restored(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    harness.apim.fail_write_after("apis/mosaic-mcp-orders-mcp", successful_writes=1)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.FAILED
    policy_writes = [
        call["body"]["properties"]["value"]
        for call in harness.apim.http_calls
        if call["method"] == "PUT"
        and call["path"] == "apis/mosaic-mcp-orders-mcp/policies/policy"
    ]
    assert policy_writes[-1] == DENY_ALL_POLICY


async def test_deny_failure_interrupts_and_recovery_releases_lock(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    harness.apim.fail_write("apis/mosaic-mcp-metadata/operations/mosaic-mcp-orders-mcp-prm")
    harness.apim.fail_write("apis/mosaic-mcp-orders-mcp/policies/policy")
    harness.apim.fail_write_after("policyFragments/mosaic-mcp-orders-mcp", successful_writes=1)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    interrupted = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.INTERRUPTED
    assert interrupted.access_state == "unknown"
    assert (
        await harness.gateway_repository.get_publication_lock(TENANT_ID, publication_id)
        == run.id
    )
    metadata_lock = f"mcp-metadata:{harness.gateway_id}"
    assert (
        await harness.gateway_repository.get_publication_lock(TENANT_ID, metadata_lock) == run.id
    )
    diagnostic = await harness.service.recover_interrupted(
        ACTOR, publication_id, run_id=run.id, confirm_quiesced=False
    )
    assert diagnostic.id == run.id
    assert (
        await harness.gateway_repository.get_publication_lock(TENANT_ID, publication_id)
        == run.id
    )

    harness.apim.write_failures.clear()
    harness.apim.write_failures_after.clear()
    recovered = await harness.service.recover_interrupted(
        ACTOR, publication_id, run_id=run.id, confirm_quiesced=True
    )
    recovered_publication = await harness.service.get_publication(ACTOR, publication_id)
    assert recovered.id == run.id
    assert recovered_publication.access_state == "failed"
    assert await harness.gateway_repository.get_publication_lock(TENANT_ID, publication_id) is None
    assert await harness.gateway_repository.get_publication_lock(TENANT_ID, metadata_lock) is None


async def test_unpublish_deletes_in_fail_closed_order(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, publication_id, run.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    harness.apim.writes.clear()
    harness.apim.http_calls.clear()

    unpublished = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, unpublished.id)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.SUCCEEDED
    assert publication.status == PublicationStatus.DRAFT
    assert publication.access_state == "applied"
    assert publication.resources == []
    assert publication.applied_access is not None
    assert publication.applied_access.version == (plan.mcp_access_snapshot.version + 1)
    assert all(not grant.enabled for grant in publication.applied_access.grants)
    calls = [
        (call["method"], call["path"], call["api_version"])
        for call in harness.apim.http_calls
        if call["method"] in {"PUT", "DELETE"}
    ]
    deny_index = calls.index(
        (
            "PUT",
            "apis/mosaic-mcp-orders-mcp/policies/policy",
            "2025-09-01-preview",
        )
    )
    mcp_delete = calls.index(("DELETE", "apis/mosaic-mcp-orders-mcp", "2025-09-01-preview"))
    operation_delete = calls.index(
        (
            "DELETE",
            "apis/mosaic-mcp-metadata/operations/mosaic-mcp-orders-mcp-prm",
            "2024-05-01",
        )
    )
    metadata_delete = calls.index(("DELETE", "apis/mosaic-mcp-metadata", "2024-05-01"))
    fragment_delete = calls.index(("DELETE", "policyFragments/mosaic-mcp-orders-mcp", "2024-05-01"))
    backend_delete = calls.index(("DELETE", "backends/mosaic-mcp-orders-mcp", "2024-05-01"))
    assert (
        deny_index
        < mcp_delete
        < operation_delete
        < metadata_delete
        < fragment_delete
        < backend_delete
    )
    assert (
        "DELETE",
        "apis/mosaic-mcp-orders-mcp/policies/policy",
        "2025-09-01-preview",
    ) not in calls[:mcp_delete]


async def test_unpublish_failure_leaves_denied_failed_state(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    harness.apim.fail_delete("policyFragments/mosaic-mcp-orders-mcp")

    unpublished = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, unpublished.id)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.FAILED
    assert publication.status == PublicationStatus.FAILED
    assert publication.access_state == "failed"
    deny_calls = [
        call
        for call in harness.apim.http_calls
        if call["method"] == "PUT"
        and call["path"] == "apis/mosaic-mcp-orders-mcp/policies/policy"
        and call["body"]["properties"]["value"] == DENY_ALL_POLICY
    ]
    assert deny_calls


async def test_model_and_mcp_plan_run_lookups_are_separated(harness: Harness) -> None:
    publication_id = await harness.create()
    mcp_plan = await harness.service.plan(ACTOR, publication_id)
    model_endpoints = InMemoryModelEndpointRepository()
    model_service = build_publishing_service(
        harness.apim, harness.gateway_repository, model_endpoints
    )

    with pytest.raises(NotFoundError):
        await model_service.get_plan(ACTOR, mcp_plan.id)

    model_run = await model_service.reap_stale_publish_runs(TENANT_ID)
    assert model_run == 0

    model_publication = Publication(
        id="publication-model-separation",
        tenant_id=TENANT_ID,
        gateway_id=harness.gateway_id,
        model_endpoint_id="endpoint",
        deployment_name="gpt",
        provider=ModelProvider.AZURE_OPENAI,
        display_name="Model",
        api_name="model-api",
        api_path="model/api",
        backend_name="model-backend",
        fragment_name="model-fragment",
        product_name="model-product",
        subscription_name="model-subscription",
        enforcement=TokenEnforcement(
            counter_key_expression="@('model')",
            tokens_per_minute=100,
        ),
        shape_version="test",
    )
    await harness.gateway_repository.save_publication(
        model_publication,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="publication.created",
            resource_type="publication",
            resource_id=model_publication.id,
            actor_object_id=ACTOR.object_id,
        ),
    )
    model_plan = PublishPlan(
        id="publishplan-model",
        tenant_id=TENANT_ID,
        publication_id=model_publication.id,
        gateway_id=harness.gateway_id,
        digest="digest",
    )
    await harness.gateway_repository.save_publish_plan(model_plan)
    with pytest.raises(NotFoundError):
        await harness.service.get_plan(ACTOR, model_plan.id)


async def test_gateway_import_preserves_published_mcp_server_and_delete_refuses(
    harness: Harness,
) -> None:
    record_id = mcp_server_id(TENANT_ID, harness.gateway_id, "orders-mcp")
    await harness.gateway_repository.save_mcp_server(
        McpServer(
            id=record_id,
            tenant_id=TENANT_ID,
            gateway_id=harness.gateway_id,
            api_name="orders-mcp",
            display_name="Published orders",
            path="orders-mcp",
            imported_from_snapshot_id="old",
            publication_id="mcp-publication-existing",
        ),
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="mcpServer.saved",
            resource_type="mcpServer",
            resource_id=record_id,
            actor_object_id=ACTOR.object_id,
        ),
    )

    imported = await harness.gateways.import_mcp_servers(
        ACTOR, harness.gateway_id, ImportRequest(api_names=["orders-mcp"])
    )

    assert imported[0].publication_id == "mcp-publication-existing"
    with pytest.raises(ConflictError, match="Delete its MCP publication"):
        await harness.gateways.delete_mcp_server(ACTOR, record_id)


async def test_gateway_delete_and_update_consider_mcp_publication_state(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    await harness.gateway_repository.record_mcp_publication_state(
        publication.model_copy(
            update={
                "resources": [
                    PublishedResource(
                        kind=PublishedResourceKind.API,
                        name=publication.api_name,
                        resource_id=f"{RESOURCE_ID}/apis/{publication.api_name}",
                        created_by_mosaic=True,
                    )
                ]
            }
        )
    )

    with pytest.raises(ConflictError, match="MCP servers"):
        await harness.gateways.delete(ACTOR, harness.gateway_id)

    await harness.gateway_repository.acquire_publication_lock(
        TENANT_ID, publication_id, "external-owner"
    )
    try:
        with pytest.raises(ConflictError):
            await harness.gateways.update(
                ACTOR,
                harness.gateway_id,
                GatewayUpdate(name="Blocked by publication lock"),
            )
    finally:
        await harness.gateway_repository.release_publication_lock(
            TENANT_ID, publication_id, "external-owner"
        )


async def test_model_reaper_skips_mcp_runs_and_mcp_reaper_handles_them(harness: Harness) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    run = harness.service._claim(ACTOR, publication, plan, "publishrun-stale")
    await harness.gateway_repository.save_publish_run(run)

    reaped = await harness.service.reap_stale_publish_runs(TENANT_ID)

    assert reaped == 1
    stored = await harness.gateway_repository.get_publish_run(TENANT_ID, run.id)
    assert stored is not None
    assert stored.status == PublishRunStatus.FAILED


async def test_a_pending_recheck_keeps_its_grants_out_of_the_snapshot(harness: Harness) -> None:
    """A grant whose subject may have lost the right to charge its cost center stays out of every
    apply until MOSAIC has checked it. See ADR 0022."""

    cost_centers = InMemoryCostCenterRepository()
    service = build_mcp_publishing_service(
        harness.apim,
        harness.gateway_repository,
        harness.mcp_repository,
        directory_repository=harness.directory_repository,
        entitlement_repository=harness.entitlement_repository,
        cost_center_repository=cost_centers,
    )
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    user = await harness.principal("principal-user", "44444444-4444-4444-4444-444444444444")
    other = await harness.principal("principal-other", "55555555-5555-5555-5555-555555555555")
    waiting = await harness.entitlement(
        "entitlement-waiting", EntitlementSubjectKind.USER, user.id, publication.mcp_server_id
    )
    kept = await harness.entitlement(
        "entitlement-kept", EntitlementSubjectKind.USER, other.id, publication.mcp_server_id
    )
    general = general_cost_center(TENANT_ID).model_copy(
        update={
            "pending_rechecks": [PendingRecheck(reason="defaultChanged", subject_id=user.id)]
        }
    )
    await cost_centers.create_cost_center(
        general,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="costCenter.created",
            resource_type="costCenter",
            resource_id=general.id,
            actor_object_id=ACTOR.object_id,
        ),
    )

    plan = await service.plan(ACTOR, publication_id)

    assert plan.mcp_access_snapshot is not None
    assert [grant.entitlement_id for grant in plan.mcp_access_snapshot.grants] == [kept.id]
    assert any(
        waiting.id in warning and "waiting for MOSAIC to check" in warning
        for warning in plan.warnings
    )


async def test_an_mcp_apply_creates_the_gateways_blocked_list_and_never_owns_it(
    harness: Harness,
) -> None:
    named = "namedValues/mosaic-blocked-cost-centers"
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.SUCCEEDED
    puts = harness.apim.write_paths("PUT")
    fragment = next(index for index, path in enumerate(puts) if path.startswith("policyFragments/"))
    assert puts.index(named) < fragment
    assert harness.apim.dangling_references == []
    assert harness.apim.written[named]["properties"]["value"] == "-"
    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert all(item.name != "mosaic-blocked-cost-centers" for item in publication.resources)

    await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    # Other publications' policies on the gateway still read it, so unpublishing leaves it.
    assert named not in harness.apim.write_paths("DELETE")
    assert named in harness.apim.written


async def test_an_mcp_apply_that_cant_create_the_list_writes_no_policy_that_reads_it(
    harness: Harness,
) -> None:
    named = "namedValues/mosaic-blocked-cost-centers"
    harness.apim.fail_write(named, 400)
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.FAILED
    assert not any(path.startswith("policyFragments/") for path in harness.apim.write_paths("PUT"))
    assert harness.apim.dangling_references == []


# -- the application an MCP server calls models as (ADR 0025) -----------------------------------

SEARCH_APP_OID = "AAAABBBB-CCCC-DDDD-EEEE-FFFF00001111"
FRAGMENT = "policyFragments/mosaic-mcp-orders-mcp"


def _caller_changes(harness: Harness) -> list[AuditEvent]:
    return [
        event
        for event in harness.gateway_repository.audit_events.values()
        if event.action == "mcpPublication.modelCallerChanged"
    ]


async def _search_app(
    harness: Harness, kind: PrincipalKind = PrincipalKind.SERVICE_PRINCIPAL
) -> Principal:
    return await harness.principal(
        "principal-search-app", SEARCH_APP_OID, kind=kind, label="Contoso Search App"
    )


async def _model_grant(
    harness: Harness, principal: Principal, gateway_id: str, *, enabled: bool = True
) -> None:
    model = ModelApi(
        id=f"model-api-{gateway_id}",
        tenant_id=TENANT_ID,
        gateway_id=gateway_id,
        api_name="mosaic-gpt-4o",
        display_name="GPT-4o",
        path="mosaic/gpt-4o",
        imported_from_snapshot_id="snapshot",
    )
    await harness.gateway_repository.save_model_api(
        model,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="modelApi.saved",
            resource_type="modelApi",
            resource_id=model.id,
            actor_object_id=ACTOR.object_id,
        ),
    )
    entitlement = Entitlement(
        id=f"entitlement-model-{gateway_id}",
        tenant_id=TENANT_ID,
        subject=EntitlementSubject(kind=EntitlementSubjectKind.APPLICATION, id=principal.id),
        resource=EntitlementResource(kind=EntitlementResourceKind.MODEL_API, id=model.id),
        enabled=enabled,
    )
    await harness.entitlement_repository.save_entitlement(
        entitlement,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="entitlement.saved",
            resource_type="entitlement",
            resource_id=entitlement.id,
            actor_object_id=ACTOR.object_id,
        ),
    )


async def test_a_model_caller_is_recorded_audited_and_applied_with_the_next_plan(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness)
    await _model_grant(harness, app, harness.gateway_id)
    unlinked = await harness.service.plan(ACTOR, publication_id)

    updated = await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )

    assert updated.model_caller_id == app.id
    # The saved plan no longer describes the publication, so it can't be applied.
    assert updated.last_plan_id is None and updated.last_plan_digest is None
    assert updated.status == PublicationStatus.DRAFT
    [event] = _caller_changes(harness)
    assert event.resource_id == publication_id
    assert event.actor_object_id == ACTOR.object_id
    assert event.details == {"previous": None, "modelCaller": app.id}
    with pytest.raises(ConflictError):
        await harness.service.apply(ACTOR, publication_id, unlinked.id)

    plan = await harness.service.plan(ACTOR, publication_id)
    assert plan.mcp_access_snapshot is not None
    assert plan.mcp_access_snapshot.model_caller == McpModelCaller(
        principal_id=app.id, object_id=SEARCH_APP_OID.lower(), display_name="Contoso Search App"
    )
    assert plan.digest != unlinked.digest
    assert not any("no enabled direct grant" in warning for warning in plan.warnings)
    assert any(
        facet.summary.startswith("Passes this call's reference") for facet in plan.facets
    )
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    assert (await harness.service.get_run(ACTOR, publication_id, run.id)).status == (
        PublishRunStatus.SUCCEEDED
    )
    applied = await harness.service.get_publication(ACTOR, publication_id)
    assert applied.applied_access is not None
    assert applied.applied_access.model_caller == plan.mcp_access_snapshot.model_caller
    fragment = harness.apim.written[FRAGMENT]["properties"]["value"]
    assert 'name="x-mosaic-on-behalf-of" exists-action="override"' in fragment
    assert f'" i=" + "{SEARCH_APP_OID.lower()}"' in fragment
    assert harness.apim.dangling_references == []

    # Naming the same application again changes nothing, and records nothing.
    again = await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )
    assert again.last_plan_id == applied.last_plan_id
    assert len(_caller_changes(harness)) == 1

    cleared = await harness.service.clear_model_caller(ACTOR, publication_id)
    assert cleared.model_caller_id is None
    assert cleared.status == PublicationStatus.PUBLISHED
    assert _caller_changes(harness)[-1].details == {"previous": app.id, "modelCaller": None}
    replanned = await harness.service.plan(ACTOR, publication_id)
    assert replanned.mcp_access_snapshot is not None
    assert replanned.mcp_access_snapshot.model_caller is None
    run = await harness.service.apply(ACTOR, publication_id, replanned.id)
    await harness.service.wait_for_idle()
    fragment = harness.apim.written[FRAGMENT]["properties"]["value"]
    assert 'name="x-mosaic-on-behalf-of" exists-action="override"' not in fragment
    assert 'name="x-mosaic-on-behalf-of" exists-action="delete"' in fragment


@pytest.mark.parametrize(
    "kind",
    [PrincipalKind.SERVICE_PRINCIPAL, PrincipalKind.MANAGED_IDENTITY, PrincipalKind.AGENT_IDENTITY],
)
async def test_an_mcp_server_calls_models_as_an_application(
    harness: Harness, kind: PrincipalKind
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness, kind)

    updated = await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )

    assert updated.model_caller_id == app.id


@pytest.mark.parametrize(
    ("kind", "object_id"),
    [
        (PrincipalKind.USER, SEARCH_APP_OID),
        (PrincipalKind.AGENT_USER, SEARCH_APP_OID),
        (PrincipalKind.SECURITY_GROUP, SEARCH_APP_OID),
        (PrincipalKind.SERVICE_PRINCIPAL, "search-app"),
    ],
)
async def test_a_model_caller_that_isnt_an_application_by_object_id_is_refused(
    harness: Harness, kind: PrincipalKind, object_id: str
) -> None:
    publication_id = await harness.create()
    principal = await harness.principal(
        "principal-not-an-app", object_id, kind=kind, label="Not an application"
    )

    with pytest.raises(ValidationError, match="calls models as an application"):
        await harness.service.set_model_caller(
            ACTOR, publication_id, McpModelCallerUpdate(principal_id=principal.id)
        )
    with pytest.raises(NotFoundError):
        await harness.service.set_model_caller(
            ACTOR, publication_id, McpModelCallerUpdate(principal_id="principal-missing")
        )

    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert publication.model_caller_id is None
    assert _caller_changes(harness) == []


@pytest.mark.parametrize("grant", ["elsewhere", "disabled here", "none"])
async def test_the_plan_warns_when_the_model_caller_has_no_grant_through_this_gateway(
    harness: Harness, grant: str
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness)
    if grant == "elsewhere":
        await _model_grant(harness, app, "gateway-elsewhere")
    elif grant == "disabled here":
        await _model_grant(harness, app, harness.gateway_id, enabled=False)
    await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )

    plan = await harness.service.plan(ACTOR, publication_id)

    assert plan.mcp_access_snapshot is not None
    assert plan.mcp_access_snapshot.model_caller is not None
    assert any(
        warning.startswith(
            "Contoso Search App has no enabled direct grant on a model this gateway publishes"
        )
        for warning in plan.warnings
    )


async def _pool_grant(
    harness: Harness, principal: Principal, gateway_id: str, *, governed: bool
) -> None:
    pool = ModelPool(
        id=f"pool-{gateway_id}",
        tenant_id=TENANT_ID,
        gateway_id=gateway_id,
        display_name="Anthropic",
        vendor="Anthropic",
        api_name="mosaic-pool-anthropic",
        api_path="mosaic/pool-anthropic",
        fragment_name="mosaic-pool-anthropic",
        product_name="mosaic-pool-anthropic",
        subscription_name="mosaic-pool-anthropic",
        models=[
            PoolModel(
                id="pool-model-opus",
                public_name="claude-opus-4-5",
                display_name="Claude Opus 4.5",
                backend_pool_name="mosaic-pool-anthropic-opus",
            )
        ],
        governed_access=ModelAccessSettings() if governed else None,
    )
    await harness.gateway_repository.save_model_pool(
        pool,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="modelPool.saved",
            resource_type="modelPool",
            resource_id=pool.id,
            actor_object_id=ACTOR.object_id,
        ),
    )
    entitlement = Entitlement(
        id=f"entitlement-pool-{gateway_id}",
        tenant_id=TENANT_ID,
        subject=EntitlementSubject(kind=EntitlementSubjectKind.APPLICATION, id=principal.id),
        resource=EntitlementResource(
            kind=EntitlementResourceKind.POOL_MODEL, id="pool-model-opus", scope_id=pool.id
        ),
    )
    await harness.entitlement_repository.save_entitlement(
        entitlement,
        AuditEvent(
            id=new_id("audit"),
            tenant_id=TENANT_ID,
            action="entitlement.saved",
            resource_type="entitlement",
            resource_id=entitlement.id,
            actor_object_id=ACTOR.object_id,
        ),
    )


@pytest.mark.parametrize(
    ("where", "governed", "warned"),
    [("here", True, False), ("here", False, True), ("elsewhere", True, True)],
)
async def test_a_grant_on_a_governed_pools_model_here_lets_the_model_caller_in(
    harness: Harness, where: str, governed: bool, warned: bool
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness)
    gateway_id = harness.gateway_id if where == "here" else "gateway-elsewhere"
    await _pool_grant(harness, app, gateway_id, governed=governed)
    await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )

    plan = await harness.service.plan(ACTOR, publication_id)

    # Only a governed pool's policy enforces grants and attributes the calls it lets in.
    assert any("no enabled direct grant" in warning for warning in plan.warnings) is warned


@pytest.mark.parametrize("change", ["deleted", "now a person"])
async def test_a_model_caller_mosaic_can_no_longer_name_compiles_to_nothing(
    harness: Harness, change: str
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness)
    await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )
    audit = AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT_ID,
        action="principal.changed",
        resource_type="principal",
        resource_id=app.id,
        actor_object_id=ACTOR.object_id,
    )
    if change == "deleted":
        await harness.directory_repository.delete_principal(app, audit)
    else:
        await harness.directory_repository.save_principal(
            app.model_copy(update={"kind": PrincipalKind.USER}), audit
        )

    plan = await harness.service.plan(ACTOR, publication_id)

    # Attribution is never a reason to hold back the server's own access.
    assert plan.mcp_access_snapshot is not None
    assert plan.mcp_access_snapshot.model_caller is None
    assert any("no longer one MOSAIC can name" in warning for warning in plan.warnings)


async def test_the_model_caller_waits_for_an_interrupted_apply_to_be_recovered(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness)
    publication = await harness.service.get_publication(ACTOR, publication_id)
    await harness.gateway_repository.record_mcp_publication_state(
        publication.model_copy(update={"access_state": "unknown"})
    )

    with pytest.raises(ConflictError, match="Recover"):
        await harness.service.set_model_caller(
            ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
        )
    with pytest.raises(ConflictError, match="Recover"):
        await harness.service.clear_model_caller(ACTOR, publication_id)


async def test_a_model_caller_cant_be_deleted_or_stop_being_an_application(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    app = await _search_app(harness)
    await harness.service.set_model_caller(
        ACTOR, publication_id, McpModelCallerUpdate(principal_id=app.id)
    )
    directory = DirectoryService(
        harness.directory_repository,
        gateway_repository=harness.gateway_repository,
        entitlement_repository=harness.entitlement_repository,
    )

    with pytest.raises(ConflictError, match="Calls models as") as deleting:
        await directory.delete_principal(ACTOR, app.id)
    assert deleting.value.details == {
        "reason": "mcpModelCaller",
        "mcpPublicationIds": [publication_id],
    }
    with pytest.raises(ConflictError, match="Calls models as"):
        await directory.update_principal(ACTOR, app.id, PrincipalUpdate(kind=PrincipalKind.USER))
    # Another application kind still calls models as an application.
    moved = await directory.update_principal(
        ACTOR, app.id, PrincipalUpdate(kind=PrincipalKind.MANAGED_IDENTITY)
    )
    assert moved.kind == PrincipalKind.MANAGED_IDENTITY

    await harness.service.clear_model_caller(ACTOR, publication_id)
    await directory.delete_principal(ACTOR, app.id)
    assert await harness.directory_repository.get_principal(TENANT_ID, app.id) is None


# API Management services created since about October 2026 refuse an API path that starts with
# ".", so MOSAIC serves every publication's protected resource metadata as an operation on one
# shared, blank-path API. See the ADR 0017 amendment of 2026-10-08.
SHARED_API = f"apis/{MCP_METADATA_API_NAME}"
FIRST_OPERATION = f"{SHARED_API}/operations/mosaic-mcp-orders-mcp-prm"
SECOND_OPERATION = f"{SHARED_API}/operations/second-mcp-prm"


async def publish(harness: Harness, publication_id: str) -> PublishRun:
    plan = await harness.service.plan(ACTOR, publication_id)
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    return await harness.service.get_run(ACTOR, publication_id, run.id)


async def create_second(harness: Harness) -> str:
    endpoint = await harness.add_endpoint("mcp-endpoint-second")
    return await harness.create(
        mcp_endpoint_id=endpoint, api_name="second-mcp", api_path="mosaic/mcp/second"
    )


def legacy_resources(publication: McpPublication) -> list[PublishedResource]:
    """What an apply made before the shared metadata API: a metadata API per publication."""

    segments = [
        (PublishedResourceKind.BACKEND, publication.backend_name, "backends/{}"),
        (PublishedResourceKind.POLICY_FRAGMENT, publication.fragment_name, "policyFragments/{}"),
        (PublishedResourceKind.API, publication.api_name, "apis/{}"),
        (PublishedResourceKind.API_POLICY, publication.api_name, "apis/{}/policies/policy"),
        (PublishedResourceKind.API, publication.metadata_api_name, "apis/{}"),
        (
            PublishedResourceKind.API_OPERATION,
            MCP_METADATA_OPERATION,
            f"apis/{publication.metadata_api_name}/operations/{{}}",
        ),
        (
            PublishedResourceKind.API_POLICY,
            publication.metadata_api_name,
            "apis/{}/policies/policy",
        ),
    ]
    return [
        PublishedResource(
            kind=kind,
            name=name,
            resource_id=f"{RESOURCE_ID}/{segment.format(name)}",
            created_by_mosaic=True,
        )
        for kind, name, segment in segments
    ]


async def test_fake_gateway_refuses_api_paths_that_start_with_a_dot() -> None:
    # The double reproduces what a new API Management service answered on 8 October 2026.
    apim = FakeApim()
    writer = ApimWriter(build_arm_client(apim), ApimResourceId.parse(RESOURCE_ID))

    with pytest.raises(UpstreamError) as refused:
        await writer.put_api(
            "zz-test",
            display_name="Dot path",
            path=".well-known/zz-test",
            subscription_required=False,
            description="",
        )
    assert "Invalid value of the Web API URL suffix" in str(refused.value.details)
    await writer.put_api(
        "zz-blank", display_name="Blank", path="", subscription_required=False, description=""
    )
    await writer.put_api_operation(
        "zz-blank",
        "zz",
        display_name="Dot template",
        method="GET",
        url_template="/.well-known/oauth-protected-resource/mosaic/mcp/zz/mcp",
        description="",
    )


async def test_first_publication_creates_shared_blank_path_metadata_api(
    harness: Harness,
) -> None:
    assert harness.apim.rejects_dot_paths
    publication_id = await harness.create()

    completed = await publish(harness, publication_id)

    publication = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    assert publication.status == PublicationStatus.PUBLISHED
    shared = harness.apim.written[SHARED_API]["properties"]
    assert shared["path"] == ""
    assert shared["subscriptionRequired"] is False
    assert "serviceUrl" not in shared
    operation = harness.apim.written[FIRST_OPERATION]["properties"]
    assert operation["method"] == "GET"
    assert operation["urlTemplate"] == (
        f"/.well-known/oauth-protected-resource/{publication.api_path}/mcp"
    )
    assert operation["urlTemplate"] == mcp_metadata_url_template(publication.api_path)
    policy = harness.apim.written[f"{FIRST_OPERATION}/policies/policy"]["properties"]["value"]
    assert f"/{publication.api_path}/mcp" in policy
    # Nothing is written at a path API Management now refuses.
    assert f"apis/{publication.metadata_api_name}" not in harness.apim.write_paths()
    assert not any(
        str(body.get("properties", {}).get("path", "")).startswith(".")
        for suffix, body in harness.apim.written.items()
        if suffix.startswith("apis/") and suffix.count("/") == 1
    )
    recorded = {(item.kind, item.name): item.created_by_mosaic for item in publication.resources}
    assert recorded[(PublishedResourceKind.API, MCP_METADATA_API_NAME)] is True
    assert recorded[(PublishedResourceKind.API_OPERATION, publication.metadata_api_name)] is True
    assert (
        recorded[(PublishedResourceKind.API_OPERATION_POLICY, publication.metadata_api_name)]
        is True
    )
    assert (PublishedResourceKind.API, publication.metadata_api_name) not in recorded

    replanned = await harness.service.plan(ACTOR, publication_id)
    shared_step = next(step for step in replanned.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.NO_CHANGE


async def test_second_publication_adds_only_its_operation(harness: Harness) -> None:
    first_id = await harness.create()
    assert (await publish(harness, first_id)).status == PublishRunStatus.SUCCEEDED
    second_id = await create_second(harness)
    harness.apim.writes.clear()

    plan = await harness.service.plan(ACTOR, second_id)
    shared_step = next(step for step in plan.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.NO_CHANGE
    assert "Use the gateway's shared" in shared_step.reason
    run = await harness.service.apply(ACTOR, second_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, second_id, run.id)
    second = await harness.service.get_publication(ACTOR, second_id)
    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    assert SHARED_API not in harness.apim.write_paths()
    assert SECOND_OPERATION in harness.apim.write_paths("PUT")
    assert f"{SECOND_OPERATION}/policies/policy" in harness.apim.write_paths("PUT")
    assert harness.apim.written[SECOND_OPERATION]["properties"]["urlTemplate"] == (
        "/.well-known/oauth-protected-resource/mosaic/mcp/second/mcp"
    )
    # Reusing the shared API carries MOSAIC's ownership of it forward.
    assert any(
        item.kind == PublishedResourceKind.API
        and item.name == MCP_METADATA_API_NAME
        and item.created_by_mosaic
        for item in second.resources
    )


async def test_unpublishing_keeps_shared_api_until_the_last_publication(
    harness: Harness,
) -> None:
    first_id = await harness.create()
    second_id = await create_second(harness)
    assert (await publish(harness, first_id)).status == PublishRunStatus.SUCCEEDED
    assert (await publish(harness, second_id)).status == PublishRunStatus.SUCCEEDED

    plan = await harness.service.plan_unpublish(ACTOR, first_id)
    assert any("only if no other MCP server still uses it" in item for item in plan.warnings)
    run = await harness.service.unpublish(ACTOR, first_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, first_id, run.id)
    first = await harness.service.get_publication(ACTOR, first_id)
    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    assert first.resources == []
    shared_step = next(step for step in completed.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.status == PublishStepStatus.SKIPPED
    assert ("DELETE", SHARED_API) not in harness.apim.writes
    assert FIRST_OPERATION not in harness.apim.written
    assert SHARED_API in harness.apim.written
    assert SECOND_OPERATION in harness.apim.written
    assert f"{SECOND_OPERATION}/policies/policy" in harness.apim.written

    last = await reviewed_unpublish(harness.service, ACTOR, second_id)
    await harness.service.wait_for_idle()

    finished = await harness.service.get_run(ACTOR, second_id, last.id)
    assert finished.status == PublishRunStatus.SUCCEEDED, finished.errors
    deletes = harness.apim.write_paths("DELETE")
    assert deletes.index(f"{SECOND_OPERATION}/policies/policy") < deletes.index(
        SECOND_OPERATION
    ) < deletes.index(SHARED_API)
    assert SHARED_API not in harness.apim.written


async def test_unpublishing_keeps_shared_api_with_an_operation_mosaic_did_not_add(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    harness.apim.seed(f"{SHARED_API}/operations/someone-else", {"properties": {}})

    run = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    assert SHARED_API in harness.apim.written
    assert ("DELETE", SHARED_API) not in harness.apim.writes


async def test_plan_restores_a_drifted_shared_metadata_api(harness: Harness) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    harness.apim.written[SHARED_API]["properties"]["subscriptionRequired"] = True
    harness.apim.writes.clear()

    completed = await publish(harness, publication_id)

    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    shared_step = next(step for step in completed.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.UPDATE
    assert ("PUT", SHARED_API) in harness.apim.writes
    assert harness.apim.written[SHARED_API]["properties"]["subscriptionRequired"] is False


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("protocols", ["http"]),
        ("protocols", ["https", "http"]),
        ("protocols", ["HTTPS"]),
        ("protocols", ["https", "https"]),
        ("protocols", None),
        ("protocols", "https"),
        ("serviceUrl", "https://metadata.example.test"),
    ],
)
async def test_plan_restores_routing_drift_on_shared_metadata_api(
    harness: Harness, key: str, value: object
) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    harness.apim.written[SHARED_API]["properties"][key] = value
    harness.apim.writes.clear()

    completed = await publish(harness, publication_id)

    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    shared_step = next(step for step in completed.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.UPDATE
    assert ("PUT", SHARED_API) in harness.apim.writes
    properties = harness.apim.written[SHARED_API]["properties"]
    assert properties["protocols"] == ["https"]
    assert properties.get("serviceUrl") is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("subscriptionRequired", True),
        ("protocols", ["http"]),
        ("serviceUrl", "https://metadata.example.test"),
    ],
)
async def test_apply_restores_shared_metadata_api_drift_after_no_change_plan(
    harness: Harness, key: str, value: object
) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    plan = await harness.service.plan(ACTOR, publication_id)
    shared_step = next(step for step in plan.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.NO_CHANGE
    harness.apim.written[SHARED_API]["properties"][key] = value
    harness.apim.writes.clear()

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    assert ("PUT", SHARED_API) in harness.apim.writes
    properties = harness.apim.written[SHARED_API]["properties"]
    assert properties["subscriptionRequired"] is False
    assert properties["protocols"] == ["https"]
    assert properties.get("serviceUrl") is None


async def test_plan_refuses_shared_metadata_api_scoped_policy(harness: Harness) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    harness.apim.seed(
        f"{SHARED_API}/policies/policy",
        {"properties": {"format": "rawxml", "value": "<policies/>"}},
    )

    with pytest.raises(ConflictError, match="API-scoped policy"):
        await harness.service.plan(ACTOR, publication_id)
    assert ("DELETE", f"{SHARED_API}/policies/policy") not in harness.apim.writes


async def test_apply_refuses_shared_metadata_api_policy_added_after_no_change_plan(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    plan = await harness.service.plan(ACTOR, publication_id)
    shared_step = next(step for step in plan.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.NO_CHANGE
    policy_path = f"{SHARED_API}/policies/policy"
    customer_policy = {"properties": {"format": "rawxml", "value": "<policies/>"}}
    harness.apim.seed(policy_path, customer_policy)
    harness.apim.writes.clear()

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.FAILED
    assert any("API-scoped policy" in error for error in completed.errors)
    assert harness.apim.written[policy_path] == customer_policy
    assert not any(
        path == SHARED_API or path.startswith(f"{SHARED_API}/")
        for _, path in harness.apim.writes
    )


async def test_plan_refuses_a_customer_api_on_the_blank_path(harness: Harness) -> None:
    publication_id = await harness.create()
    harness.apim.extra_apis.append(
        ({"name": "customer-root", "properties": {"displayName": "Root", "path": ""}}, [], None)
    )

    with pytest.raises(ConflictError, match="customer-root already uses this gateway's blank path"):
        await harness.service.plan(ACTOR, publication_id)
    assert harness.apim.write_paths() == []

    # Once inventory has seen it, MOSAIC refuses a new publication straight away.
    await harness.gateways.sync_now(ACTOR, harness.gateway_id)
    other = await harness.add_endpoint("mcp-endpoint-blank")
    with pytest.raises(ConflictError, match="blank path"):
        await harness.create(
            mcp_endpoint_id=other, api_name="blank-mcp", api_path="mosaic/mcp/blank"
        )


async def test_plan_refuses_an_api_that_would_take_the_metadata_url(harness: Harness) -> None:
    publication_id = await harness.create()
    harness.apim.extra_apis.append(
        (
            {
                "name": "well-known",
                "properties": {"displayName": "Discovery", "path": ".well-known"},
            },
            [],
            None,
        )
    )

    with pytest.raises(ConflictError, match="would receive requests"):
        await harness.service.plan(ACTOR, publication_id)
    assert harness.apim.write_paths() == []


async def test_plan_refuses_a_shared_metadata_api_mosaic_did_not_record(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    harness.apim.seed(
        SHARED_API, {"properties": {"path": "", "subscriptionRequired": False}}
    )

    with pytest.raises(ConflictError, match="no MOSAIC MCP publication recorded"):
        await harness.service.plan(ACTOR, publication_id)

    harness.apim.seed(SHARED_API, {"properties": {"path": "elsewhere"}})
    with pytest.raises(ConflictError, match="not at the gateway's blank path"):
        await harness.service.plan(ACTOR, publication_id)
    assert harness.apim.write_paths() == []


async def test_apply_refuses_a_shared_metadata_api_that_appears_after_planning(
    harness: Harness,
) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    harness.apim.seed(SHARED_API, {"properties": {"path": "", "subscriptionRequired": False}})

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    failed = await harness.service.get_publication(ACTOR, publication_id)
    assert completed.status == PublishRunStatus.FAILED
    assert any("no MOSAIC MCP publication recorded" in error for error in completed.errors)
    assert ("PUT", SHARED_API) not in harness.apim.writes
    assert FIRST_OPERATION not in harness.apim.written
    assert not any(item.name == MCP_METADATA_API_NAME for item in failed.resources)


async def test_create_refuses_the_shared_metadata_api_name(harness: Harness) -> None:
    with pytest.raises(ValidationError, match=MCP_METADATA_API_NAME):
        await harness.create(api_name=MCP_METADATA_API_NAME)


@pytest.mark.parametrize("path", ["", ".well-known/oauth-protected-resource"])
async def test_apply_refuses_metadata_route_conflicts_added_after_planning(
    harness: Harness, path: str
) -> None:
    publication_id = await harness.create()
    plan = await harness.service.plan(ACTOR, publication_id)
    harness.apim.seed("apis/customer-route", {"properties": {"path": path}})

    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()

    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.FAILED
    assert any("customer-route" in error for error in completed.errors)
    assert harness.apim.writes == []
    failed = await harness.service.get_publication(ACTOR, publication_id)
    assert failed.resources == []
    assert not harness.gateway_repository.publication_locks


@pytest.mark.parametrize(
    ("first_action", "second_action"),
    [
        ("publish", "publish"),
        ("unpublish", "unpublish"),
        ("unpublish", "publish"),
        ("publish", "unpublish"),
    ],
)
async def test_gateway_lock_serializes_metadata_writers_across_services(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, first_action: str, second_action: str
) -> None:
    first_id = await harness.create()
    second_id = await create_second(harness)
    for publication_id, action in [(first_id, first_action), (second_id, second_action)]:
        if action == "unpublish":
            assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    plans = []
    for publication_id, action in [(first_id, first_action), (second_id, second_action)]:
        planner = (
            harness.service.plan if action == "publish" else harness.service.plan_unpublish
        )
        plans.append(await planner(ACTOR, publication_id))

    entered = asyncio.Event()
    proceed = asyncio.Event()
    method = "_run_apply" if first_action == "publish" else "_run_unpublish"
    original = getattr(harness.service, method)

    async def paused(*args: Any) -> None:
        entered.set()
        await proceed.wait()
        await original(*args)

    monkeypatch.setattr(harness.service, method, paused)
    first_writer = (
        harness.service.apply if first_action == "publish" else harness.service.unpublish
    )
    first_run = await first_writer(ACTOR, first_id, plans[0].id)
    await asyncio.wait_for(entered.wait(), timeout=5)
    other_service = build_mcp_publishing_service(
        harness.apim,
        harness.gateway_repository,
        harness.mcp_repository,
        directory_repository=harness.directory_repository,
        entitlement_repository=harness.entitlement_repository,
    )
    second_writer = other_service.apply if second_action == "publish" else other_service.unpublish
    try:
        with pytest.raises(ConflictError, match="shared metadata write lock"):
            await second_writer(ACTOR, second_id, plans[1].id)
        assert (
            await harness.gateway_repository.get_publication_lock(TENANT_ID, second_id) is None
        )
        assert (
            await harness.gateway_repository.get_publication_lock(
                TENANT_ID, f"mcp-metadata:{harness.gateway_id}"
            )
            == first_run.id
        )
    finally:
        proceed.set()
        await harness.service.wait_for_idle()

    assert (
        await harness.service.get_run(ACTOR, first_id, first_run.id)
    ).status == PublishRunStatus.SUCCEEDED
    second_run = await second_writer(ACTOR, second_id, plans[1].id)
    await other_service.wait_for_idle()
    assert (
        await other_service.get_run(ACTOR, second_id, second_run.id)
    ).status == PublishRunStatus.SUCCEEDED
    assert not harness.gateway_repository.publication_locks
    assert (SHARED_API in harness.apim.written) == (
        first_action == "publish" or second_action == "publish"
    )


async def test_missing_shared_metadata_api_is_recreated(harness: Harness) -> None:
    publication_id = await harness.create()
    assert (await publish(harness, publication_id)).status == PublishRunStatus.SUCCEEDED
    for suffix in list(harness.apim.written):
        if suffix == SHARED_API or suffix.startswith(f"{SHARED_API}/"):
            del harness.apim.written[suffix]

    completed = await publish(harness, publication_id)

    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    shared_step = next(step for step in completed.steps if step.name == MCP_METADATA_API_NAME)
    assert shared_step.action == PublishAction.CREATE
    assert FIRST_OPERATION in harness.apim.written
    assert f"{FIRST_OPERATION}/policies/policy" in harness.apim.written


async def test_failed_apply_on_a_new_gateway_recovers_onto_the_shared_api(
    harness: Harness,
) -> None:
    # What the 8 October 2026 failure left: everything before the metadata API.
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    earlier = [
        item
        for item in legacy_resources(publication)
        if item.name not in {publication.metadata_api_name, MCP_METADATA_OPERATION}
    ]
    for item in earlier:
        harness.apim.seed(item.resource_id.removeprefix(f"{RESOURCE_ID}/"))
    await harness.gateway_repository.record_mcp_publication_state(
        publication.model_copy(
            update={"resources": earlier, "status": PublicationStatus.FAILED}
        )
    )

    completed = await publish(harness, publication_id)

    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    assert SHARED_API in harness.apim.written
    assert FIRST_OPERATION in harness.apim.written


async def test_legacy_publication_keeps_its_own_metadata_api(harness: Harness) -> None:
    # An older service still takes a ".well-known" API path, and existing publications keep it.
    harness.apim.rejects_dot_paths = False
    publication_id = await harness.create()
    publication = await harness.service.get_publication(ACTOR, publication_id)
    legacy = legacy_resources(publication)
    for item in legacy:
        harness.apim.seed(item.resource_id.removeprefix(f"{RESOURCE_ID}/"))
    await harness.gateway_repository.record_mcp_publication_state(
        publication.model_copy(
            update={"resources": legacy, "status": PublicationStatus.PUBLISHED}
        )
    )

    plan = await harness.service.plan(ACTOR, publication_id)

    names = [(step.kind, step.name) for step in plan.steps]
    assert (PublishedResourceKind.API, publication.metadata_api_name) in names
    assert (PublishedResourceKind.API_OPERATION, MCP_METADATA_OPERATION) in names
    assert (PublishedResourceKind.API_POLICY, publication.metadata_api_name) in names
    assert not any(name == MCP_METADATA_API_NAME for _, name in names)
    assert not any(kind == PublishedResourceKind.API_OPERATION_POLICY for kind, _ in names)
    run = await harness.service.apply(ACTOR, publication_id, plan.id)
    await harness.service.wait_for_idle()
    completed = await harness.service.get_run(ACTOR, publication_id, run.id)
    assert completed.status == PublishRunStatus.SUCCEEDED, completed.errors
    metadata = harness.apim.written[f"apis/{publication.metadata_api_name}"]["properties"]
    assert metadata["path"] == f".well-known/oauth-protected-resource/{publication.api_path}"
    operation = harness.apim.written[
        f"apis/{publication.metadata_api_name}/operations/{MCP_METADATA_OPERATION}"
    ]["properties"]
    assert operation["urlTemplate"] == "/mcp"
    assert not any(SHARED_API in path for path in harness.apim.write_paths())

    unpublished = await reviewed_unpublish(harness.service, ACTOR, publication_id)
    await harness.service.wait_for_idle()

    finished = await harness.service.get_run(ACTOR, publication_id, unpublished.id)
    assert finished.status == PublishRunStatus.SUCCEEDED, finished.errors
    deletes = harness.apim.write_paths("DELETE")
    assert f"apis/{publication.metadata_api_name}" in deletes
    assert f"apis/{publication.metadata_api_name}/operations/{MCP_METADATA_OPERATION}" in deletes
    assert f"apis/{publication.metadata_api_name}/policies/policy" in deletes
    assert not any(SHARED_API in path for path in harness.apim.write_paths())
