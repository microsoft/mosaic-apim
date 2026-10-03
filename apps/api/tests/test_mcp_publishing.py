import pytest
from apim_double import CONTRIBUTOR_PERMISSIONS, RESOURCE_ID, FakeApim
from conftest import (
    build_gateway_service,
    build_mcp_publishing_service,
    build_mcp_service,
    build_publishing_service,
    reviewed_unpublish,
)
from mcp_double import FakeMcpServer
from mosaic_api.cost_centers import CostCenterBook, PendingRecheck, general_cost_center
from mosaic_api.domain import (
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
    PublishRunStatus,
    RequestEnforcement,
    TokenEnforcement,
    mcp_server_id,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
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
        PublishedResourceKind.API_POLICY,
        PublishedResourceKind.API,
    ]
    assert plan.steps[0].name == "mosaic-blocked-cost-centers"
    assert plan.steps[0].action == PublishAction.CREATE
    assert plan.steps[3].stage == "prepare"
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
        f"apis/{publication.metadata_api_name}/policies/policy"
        in harness.apim.write_paths("PUT")
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
        "apis/mosaic-mcp-orders-mcp-prm",
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
    harness.apim.fail_write("apis/mosaic-mcp-orders-mcp-prm")
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
    metadata_delete = calls.index(("DELETE", "apis/mosaic-mcp-orders-mcp-prm", "2024-05-01"))
    fragment_delete = calls.index(("DELETE", "policyFragments/mosaic-mcp-orders-mcp", "2024-05-01"))
    backend_delete = calls.index(("DELETE", "backends/mosaic-mcp-orders-mcp", "2024-05-01"))
    assert deny_index < mcp_delete < metadata_delete < fragment_delete < backend_delete
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
