from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Literal, NoReturn

import pytest
from apim_double import RESOURCE_GROUP, RESOURCE_ID, SERVICE_NAME, SUBSCRIPTION_ID
from fastapi import Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
    ApimResourceId,
    ApiShape,
    AuditEvent,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementResource,
    EntitlementSubject,
    Gateway,
    GatewayCapabilities,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
    ModelApi,
    ModelProvider,
    Principal,
    PrincipalKind,
    Publication,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    TokenEnforcement,
    model_access_subscription_name,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError, UpstreamError
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
from mosaic_api.services.portal import PortalService
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.publishing import PublishingService
from pydantic import SecretStr
from test_mcp_entitlements import PORTAL_MCP_RUNTIME_ROUTES

TENANT = "tenant-test"
USER = "11111111-1111-1111-1111-111111111111"
OTHER_USER = "22222222-2222-2222-2222-222222222222"
AUDIENCE = "33333333-3333-3333-3333-333333333333"
MODEL_CLIENT = "44444444-4444-4444-4444-444444444444"
ACTOR = Actor(object_id=USER, tenant_id=TENANT)
KEY = "fixture-only-primary-key"


class AuditedRepository(InMemoryEntitlementRepository):
    def __init__(self) -> None:
        super().__init__()
        self.reveals: list[AuditEvent] = []
        self.reject_outcome: str | None = None

    async def record_audit(self, event: AuditEvent) -> None:
        if event.action.endswith(self.reject_outcome or "<never>"):
            raise UpstreamError("Audit persistence unavailable")
        self.reveals.append(event)


class Reader:
    def __init__(self) -> None:
        self.calls = 0
        self.value = KEY
        self.on_read: Callable[[], Awaitable[None]] | None = None

    async def read_key(
        self, subscription_name: str, api_name: str, slot: Literal["primary", "secondary"]
    ) -> SecretStr:
        self.calls += 1
        if self.on_read:
            await self.on_read()
        return SecretStr(self.value)


class Harness:
    def __init__(self) -> None:
        self.directory = InMemoryDirectoryRepository()
        self.repository = AuditedRepository()
        self.gateways = InMemoryGatewayRepository()
        self.reader = Reader()
        self.entitlements = EntitlementService(
            self.repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=InMemoryModelEndpointRepository(),
        )
        self.service = self.build_service()
        self.principal = Principal(
            id="principal", tenant_id=TENANT, object_id=USER, kind=PrincipalKind.USER
        )
        self.entitlement = Entitlement(
            id="grant",
            tenant_id=TENANT,
            subject=EntitlementSubject(kind="user", id=self.principal.id),
            resource=EntitlementResource(kind="modelApi", id="model-api"),
        )
        self.subscription = model_access_subscription_name(TENANT, "publication", "grant")
        limits = TokenEnforcement(
            counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=10000
        )
        self.publication = Publication(
            id="publication",
            tenant_id=TENANT,
            gateway_id="gateway",
            model_endpoint_id="endpoint",
            deployment_name="chat",
            provider=ModelProvider.AZURE_OPENAI,
            display_name="Chat",
            api_name="mosaic-chat",
            api_path="chat",
            backend_name="mosaic-chat",
            fragment_name="mosaic-chat",
            product_name="mosaic-chat",
            subscription_name="legacy-bootstrap",
            enforcement=limits,
            shape_version="1",
            status=PublicationStatus.PUBLISHED,
            model_api_id="model-api",
            governed_access=ModelAccessSettings(),
            access_state="applied",
            applied_access=ModelAccessSnapshot(
                version=1,
                settings=ModelAccessSettings(),
                audience=AUDIENCE,
                publication_enforcement=limits,
                grants=[
                    ModelAccessGrant(
                        entitlement_id="grant",
                        subject=self.entitlement.subject,
                        object_id=USER,
                        display_name="User",
                        subscription_name=self.subscription,
                        enabled=True,
                        intent_digest="fixture",
                    )
                ],
            ),
            resources=[
                PublishedResource(
                    kind=PublishedResourceKind.SUBSCRIPTION,
                    name=self.subscription,
                    resource_id=f"{RESOURCE_ID}/subscriptions/{self.subscription}",
                    created_by_mosaic=True,
                )
            ],
        )

    def build_service(
        self,
        *,
        runtime_client_id: str | None = AUDIENCE,
        model_client_id: str | None = MODEL_CLIENT,
    ) -> PortalAccessService:
        return PortalAccessService(
            self.entitlements,
            repository=self.repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            credential_factory=lambda _resource: self.reader,
            model_runtime_client_id=runtime_client_id,
            model_client_id=model_client_id,
        )

    def audit(self) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"), tenant_id=TENANT, action="fixture",
            resource_type="fixture", resource_id="fixture", actor_object_id=USER,
        )

    async def seed(self) -> None:
        snapshot = self.publication.applied_access
        assert snapshot is not None
        self.publication = self.publication.model_copy(
            update={
                "applied_access": snapshot.model_copy(
                    update={
                        "grants": [
                            snapshot.grants[0].model_copy(
                                update={
                                    "intent_digest": entitlement_intent_digest(
                                        self.entitlement, self.principal
                                    )
                                }
                            )
                        ]
                    }
                )
            }
        )
        await self.directory.create_principal(self.principal, self.audit())
        await self.repository.create_entitlement(self.entitlement, self.audit())
        await self.gateways.create_gateway(
            Gateway(
                id="gateway", tenant_id=TENANT, name="Gateway", azure_resource_id=RESOURCE_ID,
                subscription_id=SUBSCRIPTION_ID, resource_group=RESOURCE_GROUP,
                service_name=SERVICE_NAME,
                capabilities=GatewayCapabilities.model_validate(
                    {"gatewayUrl": "https://gateway.example"}
                ),
            ),
            self.audit(),
        )
        await self.gateways.save_model_api(
            ModelApi(
                id="model-api", tenant_id=TENANT, gateway_id="gateway",
                api_name="mosaic-chat", display_name="Chat", path="chat",
                publication_id=self.publication.id,
            ),
            self.audit(),
        )
        await self.gateways.save_publication(self.publication, self.audit())

    async def save_publication(self, **changes: object) -> None:
        current = await self.gateways.get_publication(TENANT, self.publication.id)
        assert current is not None
        self.publication = Publication.model_validate(
            {**current.model_dump(by_alias=False), **changes, "etag": current.etag}
        )
        await self.gateways.save_publication(self.publication, self.audit())


@pytest.fixture
async def harness() -> Harness:
    result = Harness()
    await result.seed()
    return result


async def test_owner_retrieves_live_key_and_rotation_without_copy(harness: Harness) -> None:
    first = await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert first.key == KEY
    assert KEY not in repr(first)
    harness.reader.value = "rotated-fixture-key"
    second = await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert second.key == "rotated-fixture-key"
    assert harness.reader.calls == 2
    persisted = repr(harness.repository.reveals) + repr(harness.publication)
    assert KEY not in persisted
    assert "rotated-fixture-key" not in persisted
    assert [event.action for event in harness.repository.reveals] == [
        "credential.reveal.requested", "credential.reveal.succeeded",
        "credential.reveal.requested", "credential.reveal.succeeded",
    ]


@pytest.mark.parametrize("tenant,oid", [(TENANT, OTHER_USER), ("other-tenant", USER)])
async def test_other_subject_or_tenant_never_reads_keys(
    harness: Harness, tenant: str, oid: str
) -> None:
    with pytest.raises(NotFoundError):
        await harness.service.reveal_key(Actor(object_id=oid, tenant_id=tenant), "grant", "primary")
    assert harness.reader.calls == 0


@pytest.mark.parametrize("state", ["pending", "applying", "failed", "unknown"])
async def test_uncertain_or_pending_publication_cannot_reveal(harness: Harness, state: str) -> None:
    await harness.save_publication(access_state=state)
    with pytest.raises(ConflictError):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert harness.reader.calls == 0


async def test_manual_binding_does_not_prove_subscription_ownership(harness: Harness) -> None:
    await harness.save_publication(resources=[])
    entitlement = harness.entitlement.model_copy(
        update={
            "binding": EntitlementBinding(
                gateway_id="gateway",
                apim_subscription_name=harness.subscription,
                source=BindingSource.MANUAL,
            )
        }
    )
    await harness.repository.save_entitlement(entitlement, harness.audit())
    with pytest.raises(ConflictError, match="ownership"):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert harness.reader.calls == 0


async def test_disabling_keys_or_revoking_intent_blocks_reveal(harness: Harness) -> None:
    await harness.save_publication(
        governed_access=ModelAccessSettings(keys_enabled=False, entra_enabled=True)
    )
    with pytest.raises(ConflictError):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    await harness.save_publication(governed_access=ModelAccessSettings())
    await harness.repository.save_entitlement(
        harness.entitlement.model_copy(update={"enabled": False}), harness.audit()
    )
    with pytest.raises(ConflictError):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert harness.reader.calls == 0


async def test_revoke_during_arm_round_trip_does_not_release_secret(harness: Harness) -> None:
    async def revoke() -> None:
        await harness.repository.save_entitlement(
            harness.entitlement.model_copy(update={"enabled": False}), harness.audit()
        )

    harness.reader.on_read = revoke
    with pytest.raises(ConflictError):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert harness.reader.calls == 1
    assert harness.repository.reveals[-1].action == "credential.reveal.denied"


@pytest.mark.parametrize("outcome,reads", [("requested", 0), ("succeeded", 1)])
async def test_audit_failure_never_releases_key(
    harness: Harness, outcome: str, reads: int
) -> None:
    harness.repository.reject_outcome = outcome
    with pytest.raises(UpstreamError, match="Audit"):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert harness.reader.calls == reads


async def test_connection_metadata_is_not_a_key_read_or_runtime_probe(harness: Harness) -> None:
    connection = await harness.service.connection(ACTOR, "grant")
    assert connection.endpoint == "https://gateway.example/chat"
    assert connection.entra_audience == AUDIENCE
    assert connection.entra_scope == f"api://{AUDIENCE}/Models.Invoke"
    assert connection.entra_client_id == MODEL_CLIENT
    assert connection.subscription_header == "Ocp-Apim-Subscription-Key"
    assert harness.reader.calls == 0
    assert "key" not in connection.model_dump()


async def test_connection_names_the_model_client_as_entra_client_id(harness: Harness) -> None:
    connection = await harness.service.connection(ACTOR, "grant")
    payload = connection.model_dump(mode="json", by_alias=True)
    assert payload["tenantId"] == TENANT
    assert payload["entraClientId"] == MODEL_CLIENT
    assert payload["entraScope"] == f"api://{AUDIENCE}/Models.Invoke"


async def test_connection_accepts_a_differently_cased_runtime_audience(harness: Harness) -> None:
    snapshot = harness.publication.applied_access
    assert snapshot is not None
    await harness.save_publication(
        applied_access=snapshot.model_copy(update={"audience": AUDIENCE.upper()})
    )
    connection = await harness.service.connection(ACTOR, "grant")
    assert connection.entra_scope == f"api://{AUDIENCE.upper()}/Models.Invoke"
    assert connection.entra_client_id == MODEL_CLIENT


async def test_connection_omits_the_model_client_for_a_superseded_runtime_audience(
    harness: Harness,
) -> None:
    # The model client is consented for the current runtime registration only, so a token for
    # the audience this publication was applied with would need consent it does not have.
    superseded = "55555555-5555-5555-5555-555555555555"
    snapshot = harness.publication.applied_access
    assert snapshot is not None
    await harness.save_publication(
        applied_access=snapshot.model_copy(update={"audience": superseded})
    )
    connection = await harness.service.connection(ACTOR, "grant")
    assert connection.entra_scope == f"api://{superseded}/Models.Invoke"
    assert connection.entra_client_id is None


async def test_connection_omits_the_model_client_without_an_entra_scope(harness: Harness) -> None:
    snapshot = harness.publication.applied_access
    assert snapshot is not None
    await harness.save_publication(applied_access=snapshot.model_copy(update={"audience": None}))
    service = harness.build_service(runtime_client_id=None)
    connection = await service.connection(ACTOR, "grant")
    assert connection.entra_scope is None
    assert connection.entra_client_id is None


async def test_connection_omits_the_model_client_when_none_is_configured(
    harness: Harness,
) -> None:
    service = harness.build_service(model_client_id=None)
    connection = await service.connection(ACTOR, "grant")
    assert connection.entra_scope == f"api://{AUDIENCE}/Models.Invoke"
    assert connection.entra_client_id is None
    assert connection.model_dump(mode="json", by_alias=True)["entraClientId"] is None


async def test_connection_describes_the_anthropic_messages_route(harness: Harness) -> None:
    snapshot = harness.publication.applied_access
    assert snapshot is not None
    # A Claude publication on a classic tier: no publication-wide token limits to report.
    await harness.save_publication(
        provider=ModelProvider.AZURE_AI_FOUNDRY,
        api_shape=ApiShape.ANTHROPIC_MESSAGES,
        enforcement=None,
        applied_access=snapshot.model_copy(update={"publication_enforcement": None}),
    )

    connection = await harness.service.connection(ACTOR, "grant")

    assert connection.api_shape == ApiShape.ANTHROPIC_MESSAGES
    # Governed access serves only the Messages route; count_tokens stays unpublished to callers.
    assert [(item.name, item.method, item.path) for item in connection.operations] == [
        ("messages", "POST", "/anthropic/v1/messages")
    ]
    assert connection.publication_limits is None
    # The model client and scope don't depend on the route shape.
    assert connection.entra_client_id == MODEL_CLIENT
    body = connection.model_dump(by_alias=True, mode="json")
    assert body["apiShape"] == "anthropicMessages"
    assert body["publicationLimits"] is None
    assert harness.reader.calls == 0


async def test_connection_reports_the_shape_of_an_openai_publication(harness: Harness) -> None:
    connection = await harness.service.connection(ACTOR, "grant")

    assert connection.api_shape == ApiShape.AZURE_OPENAI
    assert connection.operations
    assert all(item.path.startswith("/openai/") for item in connection.operations)
    assert connection.publication_limits is not None


async def test_retained_write_lock_blocks_disclosure_even_with_applied_metadata(
    harness: Harness,
) -> None:
    await harness.gateways.acquire_publication_lock(TENANT, "publication", "unknown-run")
    with pytest.raises(ConflictError, match="recovery"):
        await harness.service.reveal_key(ACTOR, "grant", "primary")
    assert harness.reader.calls == 0


async def test_entra_object_id_casing_does_not_change_ownership() -> None:
    harness = Harness()
    object_id = "ABCDEFAB-ABCD-ABCD-ABCD-ABCDEFABCDEF"
    harness.principal = harness.principal.model_copy(update={"object_id": object_id})
    snapshot = harness.publication.applied_access
    assert snapshot is not None
    harness.publication = harness.publication.model_copy(
        update={
            "applied_access": snapshot.model_copy(
                update={
                    "grants": [
                        snapshot.grants[0].model_copy(update={"object_id": object_id})
                    ]
                }
            )
        }
    )
    await harness.seed()
    actor = Actor(object_id.casefold(), TENANT)
    assert len(await harness.service.list_for_caller(actor)) == 1
    assert (await harness.service.reveal_key(actor, "grant", "primary")).key == KEY


class Caller:
    def __init__(self, object_id: str = USER, roles: tuple[str, ...] = ("User",)) -> None:
        self.object_id = object_id
        self.roles = roles

    async def authenticate(self, _request: Request) -> AuthContext:
        return AuthContext(
            object_id=self.object_id, tenant_id=TENANT, roles=frozenset(self.roles)
        )

    async def close(self) -> None:
        pass


async def test_portal_routes_are_self_scoped_and_secret_responses_are_no_store(
    harness: Harness,
) -> None:
    settings = Settings(
        environment=Environment.TEST, auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY, tenant_id=TENANT,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.authenticator = Caller()
        app.state.portal_access_service = harness.service
        listed = client.get("/api/v1/me/entitlements")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()] == ["grant"]
        assert harness.reader.calls == 0
        connection = client.get("/api/v1/me/entitlements/grant/connection")
        assert connection.status_code == 200
        assert connection.json()["entraClientId"] == MODEL_CLIENT
        assert connection.json()["entraScope"] == f"api://{AUDIENCE}/Models.Invoke"
        assert harness.reader.calls == 0
        secret = client.post("/api/v1/me/entitlements/grant/keys/reveal", json={"slot": "primary"})
        assert secret.status_code == 200
        assert secret.json()["key"] == KEY
        assert "no-store" in secret.headers["Cache-Control"]
        assert secret.headers["Pragma"] == "no-cache"
        admin_only = client.post("/api/v1/entitlements/grant/keys/reveal", json={"slot": "primary"})
        assert admin_only.status_code == 403
        app.state.authenticator = Caller(OTHER_USER)
        denied = client.post(
            "/api/v1/me/entitlements/grant/keys/reveal?administrator=true",
            json={"slot": "primary"},
        )
        assert denied.status_code == 404
        assert "no-store" in denied.headers["Cache-Control"]


def test_app_hands_the_configured_model_client_to_portal_access() -> None:
    settings = Settings(
        environment=Environment.TEST, auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY, tenant_id=TENANT,
        model_runtime_client_id=AUDIENCE, model_client_id=MODEL_CLIENT.upper(),
    )
    app = create_app(settings)
    with TestClient(app):
        service = app.state.portal_access_service
        assert service._runtime_client_id == AUDIENCE
        assert service._model_client_id == MODEL_CLIENT


def test_admin_can_discover_a_retained_mutation_owner_without_a_publish_run(
    client: TestClient,
) -> None:
    repository = client.app.state.gateway_repository
    assert client.portal is not None
    client.portal.call(
        repository.acquire_publication_lock, TENANT, "publication", "mutation_unfinished"
    )
    response = client.get("/api/v1/publications/publication/lock")
    assert response.status_code == 200
    assert response.json() == {
        "publicationId": "publication", "ownerId": "mutation_unfinished",
    }
    assert client.portal.call(repository.get_publication_lock, TENANT, "publication") == (
        "mutation_unfinished"
    )


async def test_portal_and_credential_routes_share_the_live_entitlement_service(
    client: TestClient,
) -> None:
    repository = client.app.state.gateway_repository
    audit = AuditEvent(
        id=new_id("audit"), tenant_id=TENANT, action="fixture", resource_type="modelApi",
        resource_id="imported-model", actor_object_id="local-admin",
    )
    await repository.create_gateway(
        Gateway(
            id="gateway", tenant_id=TENANT, name="Gateway", azure_resource_id=RESOURCE_ID,
            subscription_id=SUBSCRIPTION_ID, resource_group=RESOURCE_GROUP,
            service_name=SERVICE_NAME,
        ),
        audit,
    )
    await repository.save_model_api(
        ModelApi(
            id="imported-model", tenant_id=TENANT, gateway_id="gateway", api_name="model",
            display_name="Model", path="model", imported_from_snapshot_id="snapshot",
        ),
        audit.model_copy(update={"id": new_id("audit")}),
    )
    principal = client.post(
        "/api/v1/principals", json={"objectId": USER, "kind": "user"}
    )
    assert principal.status_code == 201
    grant = client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": principal.json()["id"]},
            "resource": {"kind": "modelApi", "id": "imported-model"},
        },
    )
    assert grant.status_code == 201

    client.app.state.authenticator = Caller(USER)
    portal = client.get("/api/v1/portal/entitlements")
    current_user = client.get("/api/v1/me/entitlements")
    assert portal.status_code == current_user.status_code == 200
    assert portal.json()[0]["entitlement"]["id"] == grant.json()["id"]
    assert current_user.json()[0]["id"] == grant.json()["id"]
    assert client.get("/api/v1/portal/me").json()["entitlementCount"] == 1
    assert client.get("/api/v1/portal/catalog").json()[0]["entitled"] is True
    assert client.get("/api/v1/entitlements").status_code == 403


# API Management's words for a failed apply, as the publication records them. Fictional, but shaped
# like the real thing: it names MOSAIC's policy fragment and another grantee's subscription.
FAILED_APPLY = (
    "policyFragment mosaic-contoso-chat: The Azure operation did not succeed (Failed). "
    "ValidationError: Subscription mosaic-grant-contoso-other is not valid for this API."
)
APPLIED_AT = datetime(2026, 1, 15, 9, 30, tzinfo=UTC)


async def test_a_failed_apply_reaches_the_grantee_as_a_status_without_apims_error(
    harness: Harness,
) -> None:
    await harness.save_publication(
        access_state="failed", last_error=FAILED_APPLY, last_applied_at=APPLIED_AT
    )
    portal = PortalService(
        harness.entitlements,
        directory_repository=harness.directory,
        gateway_repository=harness.gateways,
    )

    [listed] = await harness.service.list_for_caller(ACTOR)
    connection = await harness.service.connection(ACTOR, "grant")
    [resolved] = await portal.my_entitlements(ACTOR)

    for runtime in (listed.runtime, connection.runtime, resolved.entitlement.runtime):
        assert runtime is not None
        assert runtime.status == "failed"
        assert runtime.error is None
        # Everything else about the runtime state is still reported.
        assert runtime.publication_id == "publication"
        assert runtime.subscription_name == harness.subscription
        assert runtime.applied_methods == ModelAccessSettings()
        assert runtime.applied_at == APPLIED_AT
    # The administrator's view of the same grant keeps API Management's words.
    administrator = await harness.service.connection(ACTOR, "grant", administrator=True)
    assert administrator.runtime is not None
    assert administrator.runtime.error == FAILED_APPLY
    decorated = await harness.entitlements.get_entitlement(ACTOR, "grant")
    assert decorated.runtime is not None
    assert decorated.runtime.error == FAILED_APPLY


def _no_apim(_resource: ApimResourceId) -> NoReturn:
    raise AssertionError("Reading a publication never calls API Management")


@contextmanager
def _routes(harness: Harness, caller: Caller) -> Iterator[TestClient]:
    """The real routes, reading the harness's records."""

    settings = Settings(
        environment=Environment.TEST, auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY, tenant_id=TENANT,
    )
    app = create_app(settings)
    with TestClient(app) as client:
        app.state.authenticator = caller
        app.state.entitlement_service = harness.entitlements
        app.state.portal_access_service = harness.service
        app.state.portal_service = PortalService(
            harness.entitlements,
            directory_repository=harness.directory,
            gateway_repository=harness.gateways,
        )
        app.state.publishing_service = PublishingService(
            harness.gateways,
            endpoint_repository=InMemoryModelEndpointRepository(),
            client_factory=_no_apim,
            writer_factory=_no_apim,
        )
        yield client


# Every end-user route whose response carries a grant's runtime state: the path it is published
# under, a request for the harness's grant, and where the runtime state sits in the response.
PORTAL_RUNTIME_ROUTES: dict[str, tuple[str, Callable[[Any], Any]]] = {
    "/api/v1/me/entitlements": ("/api/v1/me/entitlements", lambda body: body[0]["runtime"]),
    "/api/v1/me/entitlements/{entitlement_id}/connection": (
        "/api/v1/me/entitlements/grant/connection",
        lambda body: body["runtime"],
    ),
    "/api/v1/portal/entitlements": (
        "/api/v1/portal/entitlements",
        lambda body: body[0]["entitlement"]["runtime"],
    ),
}
ADMIN_RUNTIME_ROUTES: dict[str, Callable[[Any], Any]] = {
    "/api/v1/entitlements": lambda body: body[0]["runtime"],
    "/api/v1/entitlements/grant": lambda body: body["runtime"],
    "/api/v1/entitlements/grant/connection": lambda body: body["runtime"],
    "/api/v1/entitlements/resolve?principalId=principal": (
        lambda body: body[0]["entitlement"]["runtime"]
    ),
}


@pytest.mark.parametrize("roles", [("User",), ("Admin", "User")], ids=["user", "admin"])
@pytest.mark.parametrize("route", sorted(PORTAL_RUNTIME_ROUTES))
async def test_portal_routes_report_a_failed_apply_without_apims_error(
    harness: Harness, route: str, roles: tuple[str, ...]
) -> None:
    """The route decides, not the role: an administrator reading their own grant here gets null."""

    await harness.save_publication(
        access_state="failed", last_error=FAILED_APPLY, last_applied_at=APPLIED_AT
    )
    path, runtime_of = PORTAL_RUNTIME_ROUTES[route]
    with _routes(harness, Caller(roles=roles)) as client:
        response = client.get(path)

    assert response.status_code == 200
    runtime = runtime_of(response.json())
    assert runtime["status"] == "failed"
    # Still sent, as null, so the response keeps its shape.
    assert "error" in runtime
    assert runtime["error"] is None
    assert runtime["publicationId"] == "publication"
    assert runtime["subscriptionName"] == harness.subscription
    assert runtime["appliedMethods"] == {"keysEnabled": True, "entraEnabled": True}
    assert datetime.fromisoformat(runtime["appliedAt"]) == APPLIED_AT
    assert FAILED_APPLY not in response.text


@pytest.mark.parametrize("path", sorted(ADMIN_RUNTIME_ROUTES))
async def test_admin_routes_keep_apims_error_for_a_failed_apply(
    harness: Harness, path: str
) -> None:
    await harness.save_publication(access_state="failed", last_error=FAILED_APPLY)
    with _routes(harness, Caller(roles=("Admin", "User"))) as client:
        response = client.get(path)

    assert response.status_code == 200
    runtime = ADMIN_RUNTIME_ROUTES[path](response.json())
    assert runtime["status"] == "failed"
    assert runtime["error"] == FAILED_APPLY


async def test_the_publication_keeps_apims_error_for_administrators(harness: Harness) -> None:
    await harness.save_publication(access_state="failed", last_error=FAILED_APPLY)
    with _routes(harness, Caller(roles=("Admin",))) as client:
        response = client.get("/api/v1/publications/publication")

    assert response.status_code == 200
    assert response.json()["accessState"] == "failed"
    assert response.json()["lastError"] == FAILED_APPLY


def _refers_to(schema: object, name: str, schemas: dict[str, Any], seen: set[str]) -> bool:
    if isinstance(schema, list):
        return any(_refers_to(item, name, schemas, seen) for item in schema)
    if not isinstance(schema, dict):
        return False
    reference = schema.get("$ref")
    if isinstance(reference, str):
        target = reference.rsplit("/", 1)[-1]
        if target == name or target.startswith(f"{name}-"):
            return True
        if target in seen:
            return False
        seen.add(target)
        return _refers_to(schemas[target], name, schemas, seen)
    return any(_refers_to(value, name, schemas, seen) for value in schema.values())


def test_every_portal_route_that_returns_runtime_state_is_redacted(settings: Settings) -> None:
    """A new end-user route that returns runtime state fails here until it is redacted and tested.

    Read from the published schema, so a route is caught however its response nests the state.
    """

    openapi = create_app(settings).openapi()
    schemas = openapi["components"]["schemas"]
    carrying = {
        (method.upper(), path)
        for path, operations in openapi["paths"].items()
        if path.startswith(("/api/v1/me/", "/api/v1/portal/"))
        for method, operation in operations.items()
        if _refers_to(
            {code: body for code, body in operation["responses"].items() if code.startswith("2")},
            "EntitlementRuntime",
            schemas,
            set(),
        )
    }
    tested = (*PORTAL_RUNTIME_ROUTES, *PORTAL_MCP_RUNTIME_ROUTES)
    assert carrying == {("GET", route) for route in tested}
