from collections.abc import Iterator
from typing import Any

import pytest
from apim_double import RESOURCE_GROUP, RESOURCE_ID, SERVICE_NAME, SUBSCRIPTION_ID
from fastapi import Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    AccessRequestApproval,
    AccessRequestCreate,
    AuditEvent,
    Entitlement,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    EntitlementUpdate,
    Gateway,
    GatewayCapabilities,
    McpAccessGrant,
    McpAccessSnapshot,
    McpConnection,
    McpPublication,
    McpServer,
    McpServerRoute,
    McpTransportType,
    ModelApi,
    Principal,
    PrincipalKind,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    RequestEnforcement,
    TokenEnforcement,
    entitlement_id,
    mcp_publication_id,
    mcp_resource_metadata_url,
    mcp_server_id,
    mcp_server_url,
    new_id,
    subject_kind_for,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.main import create_app
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import entitlement_intent_digest
from mosaic_api.services.portal_access import PortalAccessService

TENANT = "tenant-test"
AUDIENCE = "33333333-3333-3333-3333-333333333333"
MODEL_CLIENT = "44444444-4444-4444-4444-444444444444"
USER_OID = "11111111-1111-1111-1111-111111111111"
OTHER_OID = "22222222-2222-2222-2222-222222222222"
GROUP_OID = "55555555-5555-5555-5555-555555555555"
AGENT_OID = "66666666-6666-6666-6666-666666666666"
AGENT_USER_OID = "77777777-7777-7777-7777-777777777777"
SP_OID = "88888888-8888-8888-8888-888888888888"


def _audit(actor: str = USER_OID) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="test",
        resource_id="seed",
        actor_object_id=actor,
    )


def _token_limits() -> EntitlementEnforcement:
    return EntitlementEnforcement(
        tokens=TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            tokens_per_minute=1000,
        )
    )


def _call_limits() -> EntitlementEnforcement:
    return EntitlementEnforcement(
        requests=RequestEnforcement(
            counter_key_expression="@(context.Subscription.Id)",
            calls=60,
            renewal_period_seconds=60,
        )
    )


class NullReader:
    async def read_key(self, *_args: object) -> Any:
        raise AssertionError("MCP connection details must not read APIM keys")


class Harness:
    def __init__(self) -> None:
        self.directory = InMemoryDirectoryRepository()
        self.entitlement_repository = InMemoryEntitlementRepository()
        self.gateways = InMemoryGatewayRepository()
        self.entitlements = EntitlementService(
            self.entitlement_repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=InMemoryModelEndpointRepository(),
        )
        self.portal = PortalAccessService(
            self.entitlements,
            repository=self.entitlement_repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            credential_factory=lambda _resource: NullReader(),
            model_runtime_client_id=AUDIENCE,
            model_client_id=MODEL_CLIENT,
        )
        self.gateway = Gateway(
            id="gateway",
            tenant_id=TENANT,
            name="Gateway",
            azure_resource_id=RESOURCE_ID,
            subscription_id=SUBSCRIPTION_ID,
            resource_group=RESOURCE_GROUP,
            service_name=SERVICE_NAME,
            capabilities=GatewayCapabilities.model_validate(
                {"gatewayUrl": "https://gateway.example"}
            ),
        )
        self.server = McpServer(
            id=mcp_server_id(TENANT, "gateway", "mosaic-mcp-tools"),
            tenant_id=TENANT,
            gateway_id="gateway",
            api_name="mosaic-mcp-tools",
            display_name="Tools",
            path="mosaic/mcp/tools",
            transport_type=McpTransportType.STREAMABLE,
            endpoints=[McpServerRoute(name="message", uri_template="/mcp")],
            publication_id=mcp_publication_id(TENANT, "gateway", "endpoint"),
        )
        self.publication = McpPublication(
            id=self.server.publication_id or "publication",
            tenant_id=TENANT,
            gateway_id="gateway",
            mcp_endpoint_id="endpoint",
            display_name="Tools",
            api_name=self.server.api_name,
            api_path=self.server.path,
            backend_name=self.server.api_name,
            fragment_name=self.server.api_name,
            metadata_api_name=f"{self.server.api_name}-prm",
            mcp_server_id=self.server.id,
            status=PublicationStatus.PUBLISHED,
            access_state="applied",
            resources=[
                PublishedResource(
                    kind=PublishedResourceKind.API,
                    name=self.server.api_name,
                    resource_id=f"{RESOURCE_ID}/apis/{self.server.api_name}",
                    created_by_mosaic=True,
                )
            ],
        )

    async def seed(self) -> None:
        await self.gateways.create_gateway(self.gateway, _audit())
        await self.gateways.save_mcp_server(self.server, _audit())
        await self.gateways.save_mcp_publication(self.publication, _audit())

    async def principal(
        self,
        object_id: str,
        kind: PrincipalKind = PrincipalKind.USER,
        *,
        label: str | None = None,
        identity_parent_id: str | None = None,
    ) -> Principal:
        principal = Principal(
            id=new_id("principal"),
            tenant_id=TENANT,
            object_id=object_id,
            kind=kind,
            label=label or kind.value,
            identity_parent_id=identity_parent_id,
        )
        await self.directory.create_principal(principal, _audit())
        return principal

    async def grant(
        self,
        principal: Principal,
        *,
        enabled: bool = True,
        resource: EntitlementResource | None = None,
        enforcement: EntitlementEnforcement | None = None,
    ) -> Entitlement:
        subject = EntitlementSubject(kind=subject_kind_for(principal.kind), id=principal.id)
        resource = resource or EntitlementResource(kind="mcpServer", id=self.server.id)
        entitlement = Entitlement(
            id=entitlement_id(TENANT, subject, resource),
            tenant_id=TENANT,
            subject=subject,
            resource=resource,
            enabled=enabled,
            enforcement=enforcement,
        )
        await self.entitlement_repository.create_entitlement(entitlement, _audit())
        return entitlement

    async def apply_grant(
        self,
        entitlement: Entitlement,
        principal: Principal,
        *,
        enabled: bool = True,
        intent_digest: str | None = None,
        access_state: str = "applied",
        status: PublicationStatus = PublicationStatus.PUBLISHED,
        resources: list[PublishedResource] | None = None,
    ) -> McpPublication:
        grant = McpAccessGrant(
            entitlement_id=entitlement.id,
            subject=entitlement.subject,
            object_id=principal.object_id.casefold()
            if principal.kind == PrincipalKind.SECURITY_GROUP
            else principal.object_id,
            display_name=principal.label or principal.object_id,
            enabled=enabled,
            enforcement=entitlement.enforcement,
            intent_digest=intent_digest
            if intent_digest is not None
            else entitlement_intent_digest(entitlement, principal),
        )
        self.publication = self.publication.model_copy(
            update={
                "status": status,
                "access_state": access_state,
                "resources": self.publication.resources if resources is None else resources,
                "applied_access": McpAccessSnapshot(
                    version=1, audience=AUDIENCE, grants=[grant]
                ),
            }
        )
        await self.gateways.save_mcp_publication(self.publication, _audit())
        return self.publication


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.seed()
    return built


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("unknown", "unknown"),
        ("access-applying", "applying"),
        ("failed", "failed"),
        ("no-snapshot", "pending"),
        ("unpublished", "revoked"),
        ("disabled-not-applied", "revocationPending"),
        ("digest-mismatch", "pending"),
        ("grant-disabled", "revoked"),
        ("applied", "applied"),
    ],
)
async def test_mcp_runtime_statuses(
    harness: Harness, case: str, status: str
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    if case == "unknown":
        await harness.apply_grant(entitlement, principal, access_state="unknown")
    elif case == "access-applying":
        await harness.apply_grant(entitlement, principal, access_state="applying")
    elif case == "failed":
        await harness.apply_grant(entitlement, principal, access_state="failed")
    elif case == "no-snapshot":
        harness.publication = harness.publication.model_copy(update={"applied_access": None})
        await harness.gateways.save_mcp_publication(harness.publication, _audit())
    elif case == "unpublished":
        await harness.apply_grant(
            entitlement,
            principal,
            status=PublicationStatus.DRAFT,
            resources=[],
        )
    elif case == "disabled-not-applied":
        await harness.entitlement_repository.save_entitlement(
            entitlement.model_copy(update={"enabled": False}), _audit()
        )
        await harness.apply_grant(entitlement, principal)
    elif case == "digest-mismatch":
        await harness.apply_grant(entitlement, principal, intent_digest="outdated")
    elif case == "grant-disabled":
        await harness.apply_grant(entitlement, principal, enabled=False)
    else:
        await harness.apply_grant(entitlement, principal)

    decorated = await harness.entitlements.get_entitlement(
        Actor(object_id=USER_OID, tenant_id=TENANT), entitlement.id
    )

    assert decorated.runtime is not None
    assert decorated.runtime.status == status
    assert decorated.runtime.subscription_name is None
    if case != "no-snapshot":
        assert decorated.runtime.applied_methods is not None
        assert decorated.runtime.applied_methods.keys_enabled is False
        assert decorated.runtime.applied_methods.entra_enabled is True


async def test_mcp_runtime_is_applying_while_publication_lock_is_held(
    harness: Harness,
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)
    await harness.gateways.acquire_publication_lock(TENANT, harness.publication.id, "run")
    try:
        decorated = await harness.entitlements.get_entitlement(
            Actor(object_id=USER_OID, tenant_id=TENANT), entitlement.id
        )
    finally:
        await harness.gateways.release_publication_lock(TENANT, harness.publication.id, "run")

    assert decorated.runtime is not None
    assert decorated.runtime.status == "applying"


async def test_adopted_servers_and_mosaic_groups_have_no_mcp_runtime(
    harness: Harness,
) -> None:
    adopted = harness.server.model_copy(
        update={"id": "adopted", "publication_id": None, "imported_from_snapshot_id": "snap"}
    )
    await harness.gateways.save_mcp_server(adopted, _audit())
    principal = await harness.principal(USER_OID)
    adopted_grant = await harness.grant(
        principal, resource=EntitlementResource(kind="mcpServer", id=adopted.id)
    )
    group_grant = Entitlement(
        id="group-grant",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="group", id="group"),
        resource=EntitlementResource(kind="mcpServer", id=harness.server.id),
    )
    await harness.entitlement_repository.create_entitlement(group_grant, _audit())

    actor = Actor(object_id=USER_OID, tenant_id=TENANT)
    assert (await harness.entitlements.get_entitlement(actor, adopted_grant.id)).runtime is None
    assert (await harness.entitlements.get_entitlement(actor, group_grant.id)).runtime is None


async def test_mcp_token_limits_are_rejected_on_create_update_and_approval(
    harness: Harness,
) -> None:
    principal = await harness.principal(USER_OID)
    actor = Actor(object_id=USER_OID, tenant_id=TENANT)
    payload = EntitlementCreate(
        subject=EntitlementSubject(kind="user", id=principal.id),
        resource=EntitlementResource(kind="mcpServer", id=harness.server.id),
        enforcement=_token_limits(),
    )
    with pytest.raises(ValidationError, match="limited by calls"):
        await harness.entitlements.create_entitlement(actor, payload)

    created = await harness.entitlements.create_entitlement(
        actor,
        payload.model_copy(update={"enforcement": _call_limits()}),
    )
    with pytest.raises(ValidationError, match="limited by calls"):
        await harness.entitlements.update_entitlement(
            actor, created.id, EntitlementUpdate(enforcement=_token_limits())
        )

    requester = Actor(object_id=OTHER_OID, tenant_id=TENANT)
    request = await harness.entitlements.create_access_request(
        requester,
        AccessRequestCreate(resource=EntitlementResource(kind="mcpServer", id=harness.server.id)),
    )
    with pytest.raises(ValidationError, match="limited by calls"):
        await harness.entitlements.approve_access_request(
            actor, request.id, AccessRequestApproval(enforcement=_token_limits())
        )


async def test_model_token_limits_are_still_allowed(harness: Harness) -> None:
    model = ModelApi(
        id="model",
        tenant_id=TENANT,
        gateway_id="gateway",
        api_name="model",
        display_name="Model",
        path="model",
        imported_from_snapshot_id="snapshot",
    )
    await harness.gateways.save_model_api(model, _audit())
    principal = await harness.principal(USER_OID)
    created = await harness.entitlements.create_entitlement(
        Actor(object_id=USER_OID, tenant_id=TENANT),
        EntitlementCreate(
            subject=EntitlementSubject(kind="user", id=principal.id),
            resource=EntitlementResource(kind="modelApi", id=model.id),
            enforcement=_token_limits(),
        ),
    )
    assert created.enforcement is not None
    assert created.enforcement.tokens is not None


async def test_delete_is_blocked_while_mcp_grant_is_retained_then_allowed(
    harness: Harness,
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)
    actor = Actor(object_id=USER_OID, tenant_id=TENANT)
    with pytest.raises(ConflictError) as raised:
        await harness.entitlements.delete_entitlement(actor, entitlement.id)
    assert raised.value.details["publicationId"] == harness.publication.id

    await harness.apply_grant(entitlement, principal, enabled=False)
    await harness.entitlements.delete_entitlement(actor, entitlement.id)
    assert (
        await harness.entitlement_repository.get_entitlement(TENANT, entitlement.id)
    ) is None


async def test_mcp_mutation_conflicts_with_held_publication_lock(
    harness: Harness,
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)
    await harness.gateways.acquire_publication_lock(TENANT, harness.publication.id, "run")
    try:
        with pytest.raises(ConflictError, match="already running"):
            await harness.entitlements.update_entitlement(
                Actor(object_id=USER_OID, tenant_id=TENANT),
                entitlement.id,
                EntitlementUpdate(notes="blocked"),
            )
    finally:
        await harness.gateways.release_publication_lock(TENANT, harness.publication.id, "run")


async def test_published_mcp_connection_fields_for_supported_principal_kinds(
    harness: Harness,
) -> None:
    cases = [
        (PrincipalKind.USER, USER_OID, None, MODEL_CLIENT, "delegatedScope"),
        (PrincipalKind.AGENT_IDENTITY, AGENT_OID, None, AGENT_OID, "applicationScope"),
        (PrincipalKind.AGENT_USER, AGENT_USER_OID, AGENT_OID, AGENT_OID, "delegatedScope"),
        (PrincipalKind.SERVICE_PRINCIPAL, SP_OID, None, None, "applicationScope"),
        (PrincipalKind.SECURITY_GROUP, GROUP_OID, None, MODEL_CLIENT, "both"),
    ]
    for index, (kind, object_id, parent_id, client_id, scope_kind) in enumerate(cases):
        principal = await harness.principal(
            object_id, kind, label=f"principal-{index}", identity_parent_id=parent_id
        )
        entitlement = await harness.grant(principal)
        await harness.apply_grant(entitlement, principal)
        actor = Actor(
            object_id=object_id if kind != PrincipalKind.SECURITY_GROUP else USER_OID,
            tenant_id=TENANT,
            group_ids=frozenset({GROUP_OID.casefold()})
            if kind == PrincipalKind.SECURITY_GROUP
            else frozenset(),
        )

        connection = await harness.portal.mcp_connection(actor, entitlement.id)

        assert connection.server_url == mcp_server_url(
            "https://gateway.example", harness.publication.api_path
        )
        assert connection.resource_metadata_url == mcp_resource_metadata_url(
            "https://gateway.example", harness.publication.api_path
        )
        assert connection.entra_audience == AUDIENCE
        assert connection.enforced is True
        assert connection.client_id == client_id
        assert connection.principal_kind == kind
        if scope_kind in {"delegatedScope", "both"}:
            assert connection.delegated_scope == f"api://{AUDIENCE}/Mcp.Invoke"
        if scope_kind in {"applicationScope", "both"}:
            assert connection.application_scope == f"api://{AUDIENCE}/.default"
            assert connection.required_app_role == "Mcp.Invoke.Application"
        if kind == PrincipalKind.SECURITY_GROUP:
            assert connection.via_group_id == principal.id
            assert connection.via_group_name == principal.label


async def test_adopted_mcp_connection_is_recorded_not_enforced(
    harness: Harness,
) -> None:
    adopted = harness.server.model_copy(
        update={
            "id": "adopted",
            "publication_id": None,
            "imported_from_snapshot_id": "snap",
            "path": "adopted/path",
            "transport_type": McpTransportType.SSE,
            "endpoints": [McpServerRoute(name="sse", uri_template="/sse")],
        }
    )
    await harness.gateways.save_mcp_server(adopted, _audit())
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(
        principal, resource=EntitlementResource(kind="mcpServer", id=adopted.id)
    )

    connection = await harness.portal.mcp_connection(
        Actor(object_id=USER_OID, tenant_id=TENANT), entitlement.id
    )

    assert connection.server_url == "https://gateway.example/adopted/path/sse"
    assert connection.enforced is False
    assert connection.delegated_scope is None
    assert "imported from the gateway" in connection.status_message


@pytest.mark.parametrize("state", ["unpublished", "never-applied"])
async def test_an_mcp_server_without_its_api_offers_no_connection_details(
    harness: Harness, state: str
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    if state == "unpublished":
        await harness.apply_grant(
            entitlement, principal, status=PublicationStatus.DRAFT, resources=[]
        )
    else:
        harness.publication = harness.publication.model_copy(
            update={
                "status": PublicationStatus.DRAFT,
                "access_state": "pending",
                "resources": [],
            }
        )
        await harness.gateways.save_mcp_publication(harness.publication, _audit())

    for administrator in (False, True):
        with pytest.raises(ConflictError) as refused:
            await harness.portal.mcp_connection(
                Actor(object_id=USER_OID, tenant_id=TENANT),
                entitlement.id,
                administrator=administrator,
            )
        assert refused.value.message == (
            "This MCP server isn't published in API Management right now, so there's nothing to "
            "connect to"
        )
        assert refused.value.details["reason"] == "notPublished"


async def test_mcp_connection_access_rules_and_resource_mismatches(
    harness: Harness,
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)
    with pytest.raises(NotFoundError):
        await harness.portal.mcp_connection(
            Actor(object_id=OTHER_OID, tenant_id=TENANT), entitlement.id
        )

    model = ModelApi(
        id="model",
        tenant_id=TENANT,
        gateway_id="gateway",
        api_name="model",
        display_name="Model",
        path="model",
        imported_from_snapshot_id="snapshot",
    )
    await harness.gateways.save_model_api(model, _audit())
    model_grant = await harness.grant(
        principal, resource=EntitlementResource(kind="modelApi", id=model.id)
    )
    with pytest.raises(ConflictError, match="MCP server grants"):
        await harness.portal.mcp_connection(
            Actor(object_id=USER_OID, tenant_id=TENANT), model_grant.id
        )


async def test_catalog_marks_only_applied_published_mcp_servers_as_enforced(
    harness: Harness,
) -> None:
    from mosaic_api.services.portal import PortalService

    principal = await harness.principal(USER_OID)
    await harness.grant(principal)
    service = PortalService(
        harness.entitlements,
        directory_repository=harness.directory,
        gateway_repository=harness.gateways,
    )
    entries = await service.catalog(Actor(object_id=USER_OID, tenant_id=TENANT))
    assert {entry.id: entry.enforced for entry in entries}[harness.server.id] is True

    harness.publication = harness.publication.model_copy(update={"access_state": "pending"})
    await harness.gateways.save_mcp_publication(harness.publication, _audit())
    entries = await service.catalog(Actor(object_id=USER_OID, tenant_id=TENANT))
    assert {entry.id: entry.enforced for entry in entries}[harness.server.id] is False


class Caller:
    def __init__(
        self,
        object_id: str = USER_OID,
        roles: tuple[str, ...] = ("User",),
        groups: frozenset[str] = frozenset(),
    ) -> None:
        self.object_id = object_id
        self.roles = roles
        self.groups = groups

    async def authenticate(self, _request: Request) -> AuthContext:
        return AuthContext(
            object_id=self.object_id,
            tenant_id=TENANT,
            roles=frozenset(self.roles),
            group_ids=self.groups,
        )

    async def close(self) -> None:
        return None


@pytest.fixture
def http_client(harness: Harness) -> Iterator[TestClient]:
    settings = Settings(
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.portal_access_service = harness.portal
        yield client


async def test_mcp_connection_routes_for_admin_and_portal(
    harness: Harness, http_client: TestClient
) -> None:
    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)

    http_client.app.state.authenticator = Caller(USER_OID, roles=("Admin",))
    admin = http_client.get(f"/api/v1/entitlements/{entitlement.id}/mcp-connection")
    assert admin.status_code == 200, admin.text
    assert admin.json()["serverUrl"] == mcp_server_url(
        "https://gateway.example", harness.publication.api_path
    )

    http_client.app.state.authenticator = Caller(USER_OID, roles=("User",))
    portal = http_client.get(f"/api/v1/me/entitlements/{entitlement.id}/mcp-connection")
    assert portal.status_code == 200, portal.text
    assert McpConnection.model_validate(portal.json()).enforced is True


# API Management's words for a failed apply, as the publication records them. Fictional, but shaped
# like the real thing: it names MOSAIC's policy fragment and another publication's named value.
FAILED_APPLY = (
    "policyFragment mosaic-mcp-tools: The Azure operation did not succeed (Failed). "
    "ValidationError: Named value mosaic-mcp-other-audience is not valid for this API."
)
# Every end-user route whose response carries an MCP grant's runtime state, as it is published.
# test_portal_access reads the schema and fails when a route is missing here or from its own list.
PORTAL_MCP_RUNTIME_ROUTES = ("/api/v1/me/entitlements/{entitlement_id}/mcp-connection",)


@pytest.mark.parametrize("roles", [("User",), ("Admin", "User")], ids=["user", "admin"])
@pytest.mark.parametrize("route", PORTAL_MCP_RUNTIME_ROUTES)
async def test_portal_mcp_routes_report_a_failed_apply_without_apims_error(
    harness: Harness, http_client: TestClient, route: str, roles: tuple[str, ...]
) -> None:
    """The route decides, not the role: an administrator reading their own grant here gets null."""

    principal = await harness.principal(USER_OID)
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal, access_state="failed")
    harness.publication = harness.publication.model_copy(update={"last_error": FAILED_APPLY})
    await harness.gateways.save_mcp_publication(harness.publication, _audit())

    http_client.app.state.authenticator = Caller(USER_OID, roles=roles)
    response = http_client.get(route.format(entitlement_id=entitlement.id))

    assert response.status_code == 200, response.text
    runtime = response.json()["runtime"]
    assert runtime["status"] == "failed"
    # Still sent, as null, so the response keeps its shape.
    assert "error" in runtime
    assert runtime["error"] is None
    assert runtime["publicationId"] == harness.publication.id
    assert FAILED_APPLY not in response.text

    http_client.app.state.authenticator = Caller(USER_OID, roles=("Admin",))
    administrator = http_client.get(f"/api/v1/entitlements/{entitlement.id}/mcp-connection")
    assert administrator.status_code == 200, administrator.text
    assert administrator.json()["runtime"]["error"] == FAILED_APPLY


async def test_security_group_mcp_connection_route_is_visible_only_to_members(
    harness: Harness, http_client: TestClient
) -> None:
    group = await harness.principal(
        GROUP_OID, PrincipalKind.SECURITY_GROUP, label="Security group"
    )
    entitlement = await harness.grant(group)
    await harness.apply_grant(entitlement, group)

    http_client.app.state.authenticator = Caller(
        USER_OID, roles=("User",), groups=frozenset({GROUP_OID.casefold()})
    )
    member = http_client.get(f"/api/v1/me/entitlements/{entitlement.id}/mcp-connection")
    assert member.status_code == 200, member.text
    assert member.json()["viaGroupName"] == "Security group"

    http_client.app.state.authenticator = Caller(USER_OID, roles=("User",))
    non_member = http_client.get(
        f"/api/v1/me/entitlements/{entitlement.id}/mcp-connection"
    )
    assert non_member.status_code == 404
