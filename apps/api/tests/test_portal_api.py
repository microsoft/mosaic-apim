"""The end-user portal: what a caller may see, and what they may never see.

The security property under test is that every portal route is scoped to the caller's own token.
None of them accept a subject or requester parameter, so these tests assert the *absence* of a way
for one portal user to read another's grants as much as they assert the happy path.
"""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.config import Settings
from mosaic_api.domain import (
    AccessRequestCreate,
    AccessRequestState,
    CatalogVisibility,
    Entitlement,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    Group,
    GroupMembership,
    McpServer,
    ModelApi,
    Principal,
    TokenEnforcement,
    new_id,
)
from mosaic_api.main import create_app
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services import EntitlementService, PortalService
from mosaic_api.services.directory import Actor

TENANT = "tenant-test"
CALLER_OID = "caller-object-id"
OTHER_OID = "someone-else-object-id"
ACTOR = Actor(object_id=CALLER_OID, tenant_id=TENANT)
OTHER_ACTOR = Actor(object_id=OTHER_OID, tenant_id=TENANT)


def _audit() -> Any:
    from mosaic_api.domain import AuditEvent

    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test",
        resource_type="test",
        resource_id="test",
        actor_object_id=CALLER_OID,
    )


class Harness:
    def __init__(self) -> None:
        self.directory = InMemoryDirectoryRepository()
        self.gateways = InMemoryGatewayRepository()
        self.endpoints = InMemoryModelEndpointRepository()
        self.entitlement_repository = InMemoryEntitlementRepository()
        self.entitlements = EntitlementService(
            self.entitlement_repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=self.endpoints,
        )
        self.service = PortalService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
        )

    async def add_gateway(self) -> Gateway:
        gateway = Gateway(
            id="gateway_1",
            tenant_id=TENANT,
            name="Development gateway",
            azure_resource_id=(
                "/subscriptions/00000000-0000-0000-0000-000000000000"
                "/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim"
            ),
            subscription_id="00000000-0000-0000-0000-000000000000",
            resource_group="rg",
            service_name="apim",
        )
        await self.gateways.create_gateway(gateway, _audit())
        return gateway

    async def add_model_api(
        self, api_id: str, *, visibility: CatalogVisibility = CatalogVisibility.CATALOG
    ) -> ModelApi:
        record = ModelApi(
            id=api_id,
            tenant_id=TENANT,
            gateway_id="gateway_1",
            api_name=api_id,
            display_name=f"Model {api_id}",
            path=api_id,
            visibility=visibility,
            summary="A governed model API.",
            imported_from_snapshot_id="snapshot",
        )
        await self.gateways.save_model_api(record, _audit())
        return record

    async def add_mcp_server(self, server_id: str) -> McpServer:
        record = McpServer(
            id=server_id,
            tenant_id=TENANT,
            gateway_id="gateway_1",
            api_name=server_id,
            display_name=f"MCP {server_id}",
            path=server_id,
            imported_from_snapshot_id="snapshot",
        )
        await self.gateways.save_mcp_server(record, _audit())
        return record

    async def add_principal(self, object_id: str) -> Principal:
        principal = Principal(
            id=new_id("principal"),
            tenant_id=TENANT,
            object_id=object_id,
            kind="user",
            label=f"Person {object_id}",
        )
        await self.directory.create_principal(principal, _audit())
        return principal

    async def grant(
        self,
        subject: EntitlementSubject,
        resource: EntitlementResource,
        *,
        enforcement: EntitlementEnforcement | None = None,
    ) -> Entitlement:
        entitlement = Entitlement(
            id=new_id("entitlement"),
            tenant_id=TENANT,
            subject=subject,
            resource=resource,
            enforcement=enforcement,
        )
        await self.entitlement_repository.save_entitlement(entitlement, _audit())
        return entitlement


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.add_gateway()
    return built


async def test_a_caller_mosaic_has_never_seen_has_no_entitlements(harness: Harness) -> None:
    # Authenticated, holds the role, but was never recorded as a Principal. That is a real state.
    result = await harness.service.my_entitlements(ACTOR)

    assert result == []
    profile = await harness.service.profile(ACTOR, roles=["User"], is_admin=False)
    assert profile.principal_id is None
    assert profile.entitlement_count == 0


async def test_direct_and_group_grants_report_how_they_arrived(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.add_model_api("api-direct")
    await harness.add_model_api("api-group")
    group = Group(id=new_id("group"), tenant_id=TENANT, name="Platform engineering")
    await harness.directory.create_group(group, _audit())
    await harness.directory.create_membership(
        GroupMembership(id=new_id("membership"), tenant_id=TENANT, group_id=group.id,
                        principal_id=principal.id),
        group,
        principal,
        _audit(),
    )
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-direct"),
    )
    await harness.grant(
        EntitlementSubject(kind="group", id=group.id),
        EntitlementResource(kind="modelApi", id="api-group"),
    )

    resolved = await harness.service.my_entitlements(ACTOR)

    by_resource = {item.entitlement.resource.id: item for item in resolved}
    assert by_resource["api-direct"].via == "direct"
    assert by_resource["api-group"].via == "group"
    assert by_resource["api-group"].via_group_name == "Platform engineering"


async def test_an_unrestricted_grant_is_reported_as_having_no_enforcement(
    harness: Harness,
) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.add_model_api("api-open")
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-open"),
        enforcement=None,
    )

    resolved = await harness.service.my_entitlements(ACTOR)

    # A missing limit must stay missing all the way out. Rendering it as zero would tell a user
    # they may send nothing, which is the opposite of what an unrestricted grant means.
    assert resolved[0].entitlement.enforcement is None


async def test_a_limited_grant_carries_its_limits(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.add_model_api("api-limited")
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-limited"),
        enforcement=EntitlementEnforcement(
            tokens=TokenEnforcement(
                counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=1000
            )
        ),
    )

    resolved = await harness.service.my_entitlements(ACTOR)

    enforcement = resolved[0].entitlement.enforcement
    assert enforcement is not None
    assert enforcement.tokens is not None
    assert enforcement.tokens.tokens_per_minute == 1000


async def test_the_catalog_omits_private_resources(harness: Harness) -> None:
    await harness.add_model_api("api-public")
    await harness.add_model_api("api-private", visibility=CatalogVisibility.PRIVATE)

    entries = await harness.service.catalog(ACTOR)

    assert [entry.id for entry in entries] == ["api-public"]


async def test_the_catalog_includes_mcp_servers_and_names_the_gateway(
    harness: Harness,
) -> None:
    await harness.add_mcp_server("orders-mcp")

    entries = await harness.service.catalog(ACTOR)

    assert [(entry.kind, entry.id) for entry in entries] == [("mcpServer", "orders-mcp")]
    assert entries[0].gateway_name == "Development gateway"


async def test_the_catalog_marks_what_the_caller_already_has(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.add_model_api("api-granted")
    await harness.add_model_api("api-not-granted")
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-granted"),
    )

    entries = {entry.id: entry for entry in await harness.service.catalog(ACTOR)}

    assert entries["api-granted"].entitled is True
    assert entries["api-not-granted"].entitled is False


async def test_an_open_request_shows_on_the_catalog_entry(harness: Harness) -> None:
    await harness.add_model_api("api-wanted")
    await harness.service.create_access_request(
        ACTOR,
        AccessRequestCreate(
            resource=EntitlementResource(kind="modelApi", id="api-wanted"),
            justification="I need it for the ingestion job.",
        ),
    )

    entries = {entry.id: entry for entry in await harness.service.catalog(ACTOR)}

    assert entries["api-wanted"].request_state == AccessRequestState.PENDING


async def test_a_withdrawn_request_does_not_keep_blocking_the_catalog_entry(
    harness: Harness,
) -> None:
    await harness.add_model_api("api-wanted")
    created = await harness.service.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="api-wanted"))
    )
    await harness.service.withdraw_access_request(ACTOR, created.id)

    entries = {entry.id: entry for entry in await harness.service.catalog(ACTOR)}

    # Withdrawing is how a user changes their mind; it must not permanently mark the entry.
    assert entries["api-wanted"].request_state is None


async def test_access_requests_are_scoped_to_the_caller(harness: Harness) -> None:
    await harness.add_model_api("api-shared")
    mine = await harness.service.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="api-shared"))
    )
    theirs = await harness.service.create_access_request(
        OTHER_ACTOR,
        AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="api-shared")),
    )

    mine_listed = await harness.service.my_access_requests(ACTOR)
    theirs_listed = await harness.service.my_access_requests(OTHER_ACTOR)

    assert [item.id for item in mine_listed] == [mine.id]
    assert [item.id for item in theirs_listed] == [theirs.id]


async def test_one_caller_cannot_withdraw_anothers_request(harness: Harness) -> None:
    from mosaic_api.errors import NotFoundError

    await harness.add_model_api("api-shared")
    theirs = await harness.service.create_access_request(
        OTHER_ACTOR,
        AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="api-shared")),
    )

    # Reported as not found rather than forbidden, so the response does not confirm it exists.
    with pytest.raises(NotFoundError):
        await harness.service.withdraw_access_request(ACTOR, theirs.id)

    unchanged = await harness.entitlement_repository.get_access_request(TENANT, theirs.id)
    assert unchanged is not None
    assert unchanged.state == AccessRequestState.PENDING


async def test_profile_counts_entitlements_and_open_requests(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.add_model_api("api-granted")
    await harness.add_model_api("api-wanted")
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-granted"),
    )
    await harness.service.create_access_request(
        ACTOR, AccessRequestCreate(resource=EntitlementResource(kind="modelApi", id="api-wanted"))
    )

    profile = await harness.service.profile(ACTOR, roles=["User"], is_admin=False)

    assert profile.entitlement_count == 1
    assert profile.pending_request_count == 1
    assert profile.principal_id == principal.id
    assert profile.display_label == f"Person {CALLER_OID}"
    assert profile.is_admin is False


def _portal_client(settings: Settings, roles: list[str]) -> TestClient:
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    app.state.authenticator = LocalAuthenticator(settings.tenant_id, roles=roles)
    return client


def test_portal_routes_reject_a_caller_without_the_role(settings: Settings) -> None:
    client = _portal_client(settings, roles=["Nothing"])
    try:
        for path in (
            "/api/v1/portal/me",
            "/api/v1/portal/entitlements",
            "/api/v1/portal/catalog",
            "/api/v1/portal/access-requests",
        ):
            assert client.get(path).status_code == 403, path
    finally:
        client.__exit__(None, None, None)


def test_portal_routes_admit_the_user_role(settings: Settings) -> None:
    client = _portal_client(settings, roles=["User"])
    try:
        assert client.get("/api/v1/portal/catalog").status_code == 200
        profile = client.get("/api/v1/portal/me")
        assert profile.status_code == 200
        assert profile.json()["isAdmin"] is False
    finally:
        client.__exit__(None, None, None)


def test_an_administrator_may_open_the_portal(settings: Settings) -> None:
    client = _portal_client(settings, roles=["Admin"])
    try:
        profile = client.get("/api/v1/portal/me")
        assert profile.status_code == 200
        assert profile.json()["isAdmin"] is True
    finally:
        client.__exit__(None, None, None)


def test_administrator_entitlement_routes_still_reject_a_portal_user(
    settings: Settings,
) -> None:
    client = _portal_client(settings, roles=["User"])
    try:
        # The portal role must not be a way into the administrator surface.
        assert client.get("/api/v1/entitlements").status_code == 403
        assert client.get("/api/v1/gateways").status_code == 403
    finally:
        client.__exit__(None, None, None)
