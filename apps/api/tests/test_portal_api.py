"""The end-user portal: what a caller may see, and what they may never see.

The security property under test is that every portal route is scoped to the caller's own token.
None of them accept a subject or requester parameter, so these tests assert the *absence* of a way
for one portal user to read another's grants as much as they assert the happy path.
"""

import json
from collections import Counter
from collections.abc import Awaitable, Callable
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


def _request_for(kind: str, resource_id: str) -> AccessRequestCreate:
    return AccessRequestCreate(resource=EntitlementResource(kind=kind, id=resource_id))


def _counting(
    calls: Counter[str], name: str, method: Callable[..., Awaitable[Any]]
) -> Callable[..., Awaitable[Any]]:
    async def counted(*args: Any, **kwargs: Any) -> Any:
        calls[name] += 1
        return await method(*args, **kwargs)

    return counted


def _count_name_reads(monkeypatch: pytest.MonkeyPatch, harness: Harness) -> Counter[str]:
    """Count every repository read that could name a resource, including the per-row ones."""

    calls: Counter[str] = Counter()
    readers: tuple[tuple[object, tuple[str, ...]], ...] = (
        (
            harness.gateways,
            (
                "list_model_apis",
                "get_model_api",
                "list_mcp_servers",
                "get_mcp_server",
                "list_observed",
            ),
        ),
        (harness.endpoints, ("list_observed_for_endpoint",)),
    )
    for repository, names in readers:
        for name in names:
            monkeypatch.setattr(repository, name, _counting(calls, name, getattr(repository, name)))
    return calls


async def test_grants_are_named_the_way_the_catalog_names_them(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.add_model_api("api-direct")
    await harness.add_mcp_server("docs-mcp")
    group = Group(id=new_id("group"), tenant_id=TENANT, name="Platform engineering")
    await harness.directory.create_group(group, _audit())
    await harness.directory.create_membership(
        GroupMembership(
            id=new_id("membership"),
            tenant_id=TENANT,
            group_id=group.id,
            principal_id=principal.id,
        ),
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
        EntitlementResource(kind="mcpServer", id="docs-mcp"),
    )

    resolved = await harness.service.my_entitlements(ACTOR)
    catalog = {entry.id: entry.display_name for entry in await harness.service.catalog(ACTOR)}

    named = {item.entitlement.resource.id: item.resource_display_name for item in resolved}
    assert named == {"api-direct": catalog["api-direct"], "docs-mcp": catalog["docs-mcp"]}
    assert named == {"api-direct": "Model api-direct", "docs-mcp": "MCP docs-mcp"}


async def test_requests_are_named_the_way_the_catalog_names_them(harness: Harness) -> None:
    await harness.add_model_api("api-wanted")
    await harness.add_mcp_server("docs-mcp")

    created = await harness.service.create_access_request(
        ACTOR, _request_for("modelApi", "api-wanted")
    )
    await harness.service.create_access_request(ACTOR, _request_for("mcpServer", "docs-mcp"))
    withdrawn = await harness.service.withdraw_access_request(ACTOR, created.id)
    listed = await harness.service.my_access_requests(ACTOR)
    catalog = {entry.id: entry.display_name for entry in await harness.service.catalog(ACTOR)}

    named = {item.resource.id: item.resource_display_name for item in listed}
    assert named == {"api-wanted": catalog["api-wanted"], "docs-mcp": catalog["docs-mcp"]}
    # Every portal route that returns a request names it, not only the list.
    assert created.resource_display_name == "Model api-wanted"
    assert withdrawn.resource_display_name == "Model api-wanted"
    assert withdrawn.state == AccessRequestState.WITHDRAWN


async def test_a_resource_hidden_from_the_catalog_is_still_named(harness: Harness) -> None:
    # This is why names are resolved on the server rather than joined against the catalog in the
    # browser: hiding a resource from the catalog neither revokes a grant nor erases a request.
    principal = await harness.add_principal(CALLER_OID)
    granted = await harness.add_model_api("api-granted")
    requested = await harness.add_model_api("api-requested")
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-granted"),
    )
    await harness.service.create_access_request(ACTOR, _request_for("modelApi", "api-requested"))
    for record in (granted, requested):
        hidden = record.model_copy(update={"visibility": CatalogVisibility.PRIVATE})
        await harness.gateways.save_model_api(hidden, _audit())

    assert await harness.service.catalog(ACTOR) == []
    [grant] = await harness.service.my_entitlements(ACTOR)
    [request] = await harness.service.my_access_requests(ACTOR)
    assert grant.resource_display_name == "Model api-granted"
    assert request.resource_display_name == "Model api-requested"


async def test_a_deleted_resource_is_still_listed_without_a_name(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    granted = await harness.add_model_api("api-granted")
    requested = await harness.add_mcp_server("retired-mcp")
    await harness.grant(
        EntitlementSubject(kind="user", id=principal.id),
        EntitlementResource(kind="modelApi", id="api-granted"),
    )
    await harness.service.create_access_request(ACTOR, _request_for("mcpServer", "retired-mcp"))
    await harness.gateways.delete_model_api(granted, _audit())
    await harness.gateways.delete_mcp_server(requested, _audit())

    [grant] = await harness.service.my_entitlements(ACTOR)
    [request] = await harness.service.my_access_requests(ACTOR)

    # Still listed, so the person can see what they hold or asked for. With no name to show, the
    # portal falls back to the resource's kind and ID.
    assert grant.entitlement.resource.id == "api-granted"
    assert grant.resource_display_name is None
    assert request.resource.id == "retired-mcp"
    assert request.resource_display_name is None


async def test_observed_resources_are_named_from_what_mosaic_observed(harness: Harness) -> None:
    principal = await harness.add_principal(CALLER_OID)
    await harness.gateways.replace_observed(
        TENANT,
        "gateway_1",
        [
            ObservedProduct(
                id="observedProduct_premium",
                tenant_id=TENANT,
                gateway_id="gateway_1",
                snapshot_id="snapshot",
                name="premium",
                display_name="Premium models",
            )
        ],
        "snapshot",
    )
    await harness.endpoints.replace_observed_for_endpoint(
        TENANT,
        "endpoint_1",
        [
            ObservedModelDeployment(
                id="observedModelDeployment_chat",
                tenant_id=TENANT,
                endpoint_id="endpoint_1",
                snapshot_id="snapshot",
                deployment_name="chat-deployment",
            )
        ],
        "snapshot",
    )
    subject = EntitlementSubject(kind="user", id=principal.id)
    for kind, resource_id, scope_id in (
        ("product", "observedProduct_premium", "gateway_1"),
        ("modelDeployment", "observedModelDeployment_chat", "endpoint_1"),
        # Observed on a gateway MOSAIC holds nothing for, as after the gateway was removed.
        ("product", "observedProduct_gone", "gateway_2"),
    ):
        await harness.grant(
            subject, EntitlementResource(kind=kind, id=resource_id, scope_id=scope_id)
        )

    resolved = await harness.service.my_entitlements(ACTOR)

    assert {item.entitlement.resource.id: item.resource_display_name for item in resolved} == {
        "observedProduct_premium": "Premium models",
        "observedModelDeployment_chat": "chat-deployment",
        "observedProduct_gone": None,
    }


async def test_names_are_read_once_per_kind_rather_than_once_per_request(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    for index in range(3):
        await harness.add_model_api(f"api-{index}")
        await harness.add_mcp_server(f"mcp-{index}")
        await harness.service.create_access_request(ACTOR, _request_for("modelApi", f"api-{index}"))
        await harness.service.create_access_request(
            ACTOR, _request_for("mcpServer", f"mcp-{index}")
        )
    reads = _count_name_reads(monkeypatch, harness)

    listed = await harness.service.my_access_requests(ACTOR)

    assert sorted(item.resource_display_name or "" for item in listed) == [
        "MCP mcp-0",
        "MCP mcp-1",
        "MCP mcp-2",
        "Model api-0",
        "Model api-1",
        "Model api-2",
    ]
    assert reads == {"list_model_apis": 1, "list_mcp_servers": 1}


async def test_the_name_resolver_reads_each_kind_and_scope_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.add_model_api("api-kept")
    await harness.gateways.save_model_api(
        (await harness.add_model_api("api-blank")).model_copy(update={"display_name": "  "}),
        _audit(),
    )
    reads = _count_name_reads(monkeypatch, harness)
    resources = [
        EntitlementResource.model_validate(item)
        for item in (
            {"kind": "modelApi", "id": "api-kept"},
            {"kind": "modelApi", "id": "api-blank"},
            {"kind": "modelApi", "id": "api-deleted"},
            {"kind": "product", "id": "product-a", "scope_id": "gateway_1"},
            {"kind": "product", "id": "product-b", "scope_id": "gateway_1"},
            {"kind": "product", "id": "product-c", "scope_id": "gateway_2"},
            {"kind": "modelDeployment", "id": "deployment-a", "scope_id": "endpoint_1"},
            {"kind": "modelDeployment", "id": "deployment-b", "scope_id": "endpoint_1"},
        )
    ]

    names = await harness.entitlements.resource_display_names(ACTOR, resources)

    # In the order given; a blank name is as unhelpful as none, so the portal falls back for both.
    assert names == ["Model api-kept", None, None, None, None, None, None, None]
    assert reads == {"list_model_apis": 1, "list_observed": 2, "list_observed_for_endpoint": 1}


async def test_nothing_is_read_to_name_an_empty_list(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.add_model_api("api-unrelated")
    reads = _count_name_reads(monkeypatch, harness)

    assert await harness.service.my_entitlements(ACTOR) == []
    assert await harness.service.my_access_requests(ACTOR) == []
    assert reads == {}


async def test_only_the_callers_own_resources_are_named(harness: Harness) -> None:
    await harness.add_model_api("api-mine")
    await harness.add_model_api("api-theirs", visibility=CatalogVisibility.PRIVATE)
    await harness.service.create_access_request(ACTOR, _request_for("modelApi", "api-mine"))
    await harness.service.create_access_request(OTHER_ACTOR, _request_for("modelApi", "api-theirs"))

    mine = await harness.service.my_access_requests(ACTOR)

    assert [item.resource_display_name for item in mine] == ["Model api-mine"]
    # Resolving names reads the whole model API list, but none of it may reach the response.
    assert "api-theirs" not in json.dumps([item.model_dump(mode="json") for item in mine])


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


async def test_portal_routes_name_the_callers_requests_and_grants(settings: Settings) -> None:
    client = _portal_client(settings, roles=["User"])
    try:
        state = client.app.state
        model_api = ModelApi(
            id="api-json",
            tenant_id=TENANT,
            gateway_id="gateway_1",
            api_name="api-json",
            display_name="Chat model",
            path="api-json",
            imported_from_snapshot_id="snapshot",
        )
        await state.gateway_repository.save_model_api(model_api, _audit())

        created = client.post(
            "/api/v1/portal/access-requests",
            json={"resource": {"kind": "modelApi", "id": "api-json"}},
        )
        assert created.status_code == 201, created.text
        assert created.json()["resourceDisplayName"] == "Chat model"
        listed = client.get("/api/v1/portal/access-requests").json()
        assert [item["resourceDisplayName"] for item in listed] == ["Chat model"]
        withdrawn = client.post(f"/api/v1/portal/access-requests/{created.json()['id']}/withdraw")
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["resourceDisplayName"] == "Chat model"

        # LocalAuthenticator signs every request in as this object ID.
        principal = Principal(
            id=new_id("principal"),
            tenant_id=TENANT,
            object_id="local-admin",
            kind="user",
            label="Local person",
        )
        await state.repository.create_principal(principal, _audit())
        await state.entitlement_repository.save_entitlement(
            Entitlement(
                id=new_id("entitlement"),
                tenant_id=TENANT,
                subject=EntitlementSubject(kind="user", id=principal.id),
                resource=EntitlementResource(kind="modelApi", id="api-json"),
            ),
            _audit(),
        )
        granted = client.get("/api/v1/portal/entitlements").json()
        assert [item["resourceDisplayName"] for item in granted] == ["Chat model"]

        # Once the resource is gone the field is still sent, as null, so the portal can tell a
        # resource it cannot name from an older API that does not send names at all.
        await state.gateway_repository.delete_model_api(model_api, _audit())
        for path in ("/api/v1/portal/entitlements", "/api/v1/portal/access-requests"):
            [item] = client.get(path).json()
            assert item.get("resourceDisplayName", "absent") is None, path
    finally:
        client.__exit__(None, None, None)


def test_only_the_portal_responses_gain_a_resource_display_name(settings: Settings) -> None:
    schemas = create_app(settings).openapi()["components"]["schemas"]

    # Additive: the administrator's shapes are unchanged, and the portal's new field is optional.
    for unchanged in ("AccessRequest", "ResolvedEntitlement"):
        assert "resourceDisplayName" not in schemas[unchanged]["properties"], unchanged
    for portal in ("PortalAccessRequest", "PortalResolvedEntitlement"):
        assert "resourceDisplayName" in schemas[portal]["properties"], portal
        assert "resourceDisplayName" not in schemas[portal]["required"], portal
