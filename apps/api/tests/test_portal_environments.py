import pytest
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    AccessRequestCreate,
    AccessRequestResourceSnapshot,
    AuditEvent,
    CatalogVisibility,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    Group,
    GroupMembership,
    McpServer,
    ModelApi,
    ModelEndpoint,
    ModelProvider,
    Principal,
    PrincipalKind,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.observed import ObservedModelDeployment, ObservedProduct
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import EntitlementService, PortalService
from mosaic_api.services.directory import Actor

TENANT = "tenant-test"
USER = "user-object"
OTHER = "other-object"
ADMIN = Actor(object_id="admin-object", tenant_id=TENANT)
ACTOR = Actor(object_id=USER, tenant_id=TENANT)
OTHER_ACTOR = Actor(object_id=OTHER, tenant_id=TENANT)


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test",
        resource_type="test",
        resource_id="test",
        actor_object_id="admin-object",
    )


class Harness:
    def __init__(self) -> None:
        self.directory = InMemoryDirectoryRepository()
        self.gateways = InMemoryGatewayRepository()
        self.endpoints = InMemoryModelEndpointRepository()
        self.repository = InMemoryEntitlementRepository()
        self.entitlements = EntitlementService(
            self.repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=self.endpoints,
        )
        self.portal = PortalService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
        )

    async def gateway(
        self, gateway_id: str = "gateway", *, environment: str | None = "production"
    ) -> Gateway:
        record = Gateway(
            id=gateway_id,
            tenant_id=TENANT,
            name=f"{environment or 'unclassified'} gateway",
            azure_resource_id=(
                "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
                f"/providers/Microsoft.ApiManagement/service/{gateway_id}"
            ),
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name=gateway_id,
            environment=environment,
        )
        return await self.gateways.create_gateway(record, _audit())

    async def model_api(
        self,
        api_id: str = "model-api",
        *,
        gateway_id: str = "gateway",
        display_name: str = "Chat completions",
        visibility: CatalogVisibility = CatalogVisibility.CATALOG,
    ) -> ModelApi:
        record = ModelApi(
            id=api_id,
            tenant_id=TENANT,
            gateway_id=gateway_id,
            api_name=api_id,
            display_name=display_name,
            path=api_id,
            visibility=visibility,
            imported_from_snapshot_id="snapshot",
        )
        return await self.gateways.save_model_api(record, _audit())

    async def mcp_server(
        self,
        server_id: str = "mcp-server",
        *,
        gateway_id: str = "gateway",
        display_name: str = "Ticket tools",
        visibility: CatalogVisibility = CatalogVisibility.CATALOG,
    ) -> McpServer:
        record = McpServer(
            id=server_id,
            tenant_id=TENANT,
            gateway_id=gateway_id,
            api_name=server_id,
            display_name=display_name,
            path=server_id,
            visibility=visibility,
            imported_from_snapshot_id="snapshot",
        )
        return await self.gateways.save_mcp_server(record, _audit())

    async def endpoint(
        self, endpoint_id: str = "endpoint", *, environment: str | None = "test"
    ) -> ModelEndpoint:
        record = ModelEndpoint(
            id=endpoint_id,
            tenant_id=TENANT,
            name="AOAI East",
            provider=ModelProvider.AZURE_OPENAI,
            endpoint="https://aoai.example.openai.azure.com",
            environment=environment,
        )
        return await self.endpoints.create_endpoint(record, _audit())

    async def principal(self, object_id: str = USER) -> Principal:
        record = Principal(
            id=f"principal-{object_id}",
            tenant_id=TENANT,
            object_id=object_id,
            kind=PrincipalKind.USER,
        )
        return await self.directory.create_principal(record, _audit())

    async def grant(
        self,
        subject: EntitlementSubject,
        resource: EntitlementResource,
        grant_id: str,
    ) -> Entitlement:
        record = Entitlement(id=grant_id, tenant_id=TENANT, subject=subject, resource=resource)
        return await self.repository.save_entitlement(record, _audit())

    async def product(self) -> None:
        await self.gateways.replace_observed(
            TENANT,
            "gateway",
            [
                ObservedProduct(
                    id="product",
                    tenant_id=TENANT,
                    gateway_id="gateway",
                    snapshot_id="snapshot",
                    name="gold",
                    display_name="Gold product",
                )
            ],
            "snapshot",
        )

    async def deployment(self) -> None:
        await self.endpoints.replace_observed_for_endpoint(
            TENANT,
            "endpoint",
            [
                ObservedModelDeployment(
                    id="deployment",
                    tenant_id=TENANT,
                    endpoint_id="endpoint",
                    snapshot_id="snapshot",
                    deployment_name="gpt-4o",
                )
            ],
            "snapshot",
        )


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.gateway()
    await built.model_api()
    await built.mcp_server()
    await built.endpoint()
    await built.product()
    await built.deployment()
    return built


async def test_catalog_entries_carry_gateway_environment(harness: Harness) -> None:
    entries = {entry.id: entry for entry in await harness.portal.catalog(ACTOR)}

    assert entries["model-api"].environment == "production"
    assert entries["mcp-server"].environment == "production"


async def test_resolved_entitlements_carry_resource_summaries(harness: Harness) -> None:
    principal = await harness.principal()
    group = await harness.directory.create_group(
        Group(id="group", tenant_id=TENANT, name="Platform"), _audit()
    )
    await harness.directory.create_membership(
        GroupMembership(
            id="membership", tenant_id=TENANT, group_id=group.id, principal_id=principal.id
        ),
        group,
        principal,
        _audit(),
    )
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="model-api"),
        "direct",
    )
    await harness.grant(
        EntitlementSubject(kind="group", id=group.id),
        EntitlementResource(kind="mcpServer", id="mcp-server"),
        "group",
    )
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelDeployment", id="deployment", scope_id="endpoint"),
        "deployment",
    )
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="product", id="product", scope_id="gateway"),
        "product",
    )

    resolved = await harness.portal.my_entitlements(ACTOR)
    summaries = {item.entitlement.id: item.resource_summary for item in resolved}

    assert summaries["direct"] is not None
    assert summaries["direct"].display_name == "Chat completions"
    assert summaries["direct"].gateway_name == "production gateway"
    assert summaries["direct"].environment == "production"
    assert summaries["group"] is not None
    assert summaries["group"].gateway_name == "production gateway"
    assert summaries["deployment"] is not None
    assert summaries["deployment"].display_name == "gpt-4o on AOAI East"
    assert summaries["deployment"].gateway_id is None
    assert summaries["deployment"].environment == "test"
    assert summaries["product"] is not None
    assert summaries["product"].display_name == "Gold product"
    assert summaries["product"].gateway_id == "gateway"


@pytest.mark.parametrize(
    "resource",
    [
        EntitlementResource(kind="modelApi", id="private-api"),
        EntitlementResource(kind="modelApi", id="missing-api"),
        EntitlementResource(kind="product", id="product", scope_id="gateway"),
        EntitlementResource(kind="modelDeployment", id="deployment", scope_id="endpoint"),
    ],
)
async def test_portal_requests_only_accept_catalog_resources(
    harness: Harness, resource: EntitlementResource
) -> None:
    await harness.model_api(
        "private-api",
        display_name="Private model",
        visibility=CatalogVisibility.PRIVATE,
    )

    with pytest.raises(NotFoundError) as exc:
        await harness.portal.create_access_request(ACTOR, AccessRequestCreate(resource=resource))

    assert exc.value.message == "That resource isn't in your catalog."


async def test_portal_request_stores_environment_and_snapshot(harness: Harness) -> None:
    created = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )

    assert created.requested_environment == "production"
    assert created.resource_snapshot == AccessRequestResourceSnapshot(
        display_name="Chat completions",
        gateway_id="gateway",
        gateway_name="production gateway",
    )


async def test_portal_request_summary_uses_snapshot_after_resource_becomes_private(
    harness: Harness,
) -> None:
    created = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )
    current = await harness.gateways.get_model_api(TENANT, "model-api")
    assert current is not None
    await harness.gateways.save_model_api(
        current.model_copy(update={"display_name": "Secret renamed", "visibility": "private"}),
        _audit(),
    )

    listed = await harness.portal.my_access_requests(ACTOR)

    assert [item.id for item in listed] == [created.id]
    assert listed[0].resource_summary is not None
    assert listed[0].resource_summary.display_name == "Chat completions"
    assert listed[0].resource_summary.available is False


async def test_removed_resource_uses_snapshot_and_is_unavailable(harness: Harness) -> None:
    created = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )
    current = await harness.gateways.get_model_api(TENANT, "model-api")
    assert current is not None
    await harness.gateways.delete_model_api(current, _audit())

    listed = await harness.portal.my_access_requests(ACTOR)

    assert listed[0].id == created.id
    assert listed[0].resource_summary is not None
    assert listed[0].resource_summary.display_name == "Chat completions"
    assert listed[0].resource_summary.available is False


async def test_admin_access_request_list_uses_live_summary(harness: Harness) -> None:
    created = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )
    current = await harness.gateways.get_model_api(TENANT, "model-api")
    assert current is not None
    await harness.gateways.save_model_api(
        current.model_copy(update={"display_name": "Live renamed"}), _audit()
    )

    listed = await harness.entitlements.list_access_request_items(ADMIN)

    assert [item.id for item in listed] == [created.id]
    assert listed[0].resource_summary is not None
    assert listed[0].resource_summary.display_name == "Live renamed"
    assert listed[0].resource_summary.available is True


async def test_approval_unchanged_environment_needs_no_confirmation(harness: Harness) -> None:
    created = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )

    approved = await harness.entitlements.approve_access_request(
        ADMIN, created.id, AccessRequestApproval(note="approved")
    )

    assert approved.state == "approved"


async def test_approval_changed_environment_requires_matching_confirmation(
    harness: Harness,
) -> None:
    created = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )
    gateway = await harness.gateways.get_gateway(TENANT, "gateway")
    assert gateway is not None
    await harness.gateways.save_gateway(
        gateway.model_copy(update={"environment": "staging"}), _audit()
    )

    with pytest.raises(ConflictError) as exc:
        await harness.entitlements.approve_access_request(
            ADMIN, created.id, AccessRequestApproval(note="approved")
        )

    assert exc.value.details == {
        "reason": "environmentChanged",
        "requestedEnvironment": "production",
        "currentEnvironment": "staging",
    }
    approved = await harness.entitlements.approve_access_request(
        ADMIN,
        created.id,
        AccessRequestApproval(note="approved", confirmed_environment="staging"),
    )
    assert approved.state == "approved"


async def test_approval_unclassified_sentinel_and_legacy_requests(harness: Harness) -> None:
    await harness.gateway("unclassified-gateway", environment=None)
    await harness.model_api("unclassified-api", gateway_id="unclassified-gateway")
    current = await harness.gateways.get_gateway(TENANT, "unclassified-gateway")
    assert current is not None
    request = await harness.portal.create_access_request(
        ACTOR,
        AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="unclassified-api")),
    )
    bad = AccessRequestApproval(confirmed_environment="production")
    with pytest.raises(ConflictError):
        await harness.entitlements.approve_access_request(ADMIN, request.id, bad)
    approved = await harness.entitlements.approve_access_request(
        ADMIN, request.id, AccessRequestApproval(confirmed_environment="unclassified")
    )
    assert approved.state == "approved"

    legacy = await harness.repository.create_access_request(
        AccessRequest(
            id="legacy-request",
            tenant_id=TENANT,
            requester_object_id=OTHER,
            resource=EntitlementResource(kind="modelApi", id="model-api"),
        ),
        _audit(),
    )
    with pytest.raises(ConflictError) as exc:
        await harness.entitlements.approve_access_request(
            ADMIN, legacy.id, AccessRequestApproval(note="legacy")
        )
    assert exc.value.details["requestedEnvironment"] is None
    assert exc.value.details["currentEnvironment"] == "production"


async def test_portal_access_request_lists_stay_caller_scoped(harness: Harness) -> None:
    mine = await harness.portal.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api"))
    )
    theirs = await harness.portal.create_access_request(
        OTHER_ACTOR,
        AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="model-api")),
    )

    mine_list = await harness.portal.my_access_requests(ACTOR)
    their_list = await harness.portal.my_access_requests(OTHER_ACTOR)

    assert [item.id for item in mine_list] == [mine.id]
    assert [item.id for item in their_list] == [theirs.id]
