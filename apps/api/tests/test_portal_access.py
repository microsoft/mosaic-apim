from collections.abc import Awaitable, Callable
from typing import Literal

import pytest
from apim_double import RESOURCE_GROUP, RESOURCE_ID, SERVICE_NAME, SUBSCRIPTION_ID
from fastapi import Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.domain import (
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
from mosaic_api.services.portal_access import PortalAccessService
from pydantic import SecretStr

TENANT = "tenant-test"
USER = "11111111-1111-1111-1111-111111111111"
OTHER_USER = "22222222-2222-2222-2222-222222222222"
AUDIENCE = "33333333-3333-3333-3333-333333333333"
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
        self.service = PortalAccessService(
            self.entitlements,
            repository=self.repository,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            credential_factory=lambda _resource: self.reader,
            model_runtime_client_id=AUDIENCE,
        )
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
    assert connection.subscription_header == "Ocp-Apim-Subscription-Key"
    assert harness.reader.calls == 0
    assert "key" not in connection.model_dump()


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
