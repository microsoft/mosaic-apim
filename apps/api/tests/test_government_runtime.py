"""Government policy/lifecycle contracts against fictional estates, not live sovereign parity."""

import asyncio
import inspect
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from apim_double import FakeCredential, policy_expression_error
from azure.core.credentials_async import AsyncTokenCredential
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.clouds import cloud_profile
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.cost_centers import CostCenterBook, CostCenterUpdate, PendingRecheck
from mosaic_api.domain import (
    CapabilitySupport,
    EntitlementSubjectKind,
    EntitlementUpdate,
    McpAuthMode,
    ModelAccessSettings,
    PublicationUpdate,
    PublishRunStatus,
    model_access_subscription_name,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.integrations.access_policy import (
    grant_counter_identity,
    key_mcp_call_reference,
    render_governed_policy,
)
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.credentials import ApimKeyManager
from mosaic_api.integrations.mcp_access_policy import render_mcp_policy
from mosaic_api.integrations.pool_policy import _pool_key_lookup
from mosaic_api.main import create_app
from mosaic_api.repositories import InMemoryCostCenterRepository, InMemoryModelEndpointRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest
from mosaic_api.services.pool_access import entitlement_key_name
from mosaic_api.services.portal_access import PortalAccessService
from pydantic import SecretStr
from test_access_policy import _publication, _snapshot
from test_governed_lifecycle import ACTOR, TENANT, USER, Harness, no_sleep
from test_mcp_access_policy import _grant as _mcp_grant
from test_mcp_access_policy import _publication as _mcp_publication
from test_mcp_access_policy import _snapshot as _mcp_snapshot
from test_mcp_publishing import ACTOR as MCP_ACTOR
from test_mcp_publishing import TENANT_ID as MCP_TENANT
from test_mcp_publishing import Harness as McpHarness
from test_model_pools import TENANT as POOL_TENANT
from test_model_pools import _gpt4o, _member, _model
from test_pool_keys import Portal


def _government_arm(handler: Any) -> ArmClient:
    profile = cloud_profile("government")

    async def sovereign_response(request: httpx.Request) -> httpx.Response:
        response = handler(request)
        if inspect.isawaitable(response):
            response = await response
        # This fictional ARM double otherwise advertises commercial poll/continuation URLs.
        return httpx.Response(
            response.status_code,
            headers={
                name: value.replace("https://management.azure.com", profile.arm_endpoint)
                for name, value in response.headers.items()
                if name.lower() != "content-length"
            },
            content=response.content.replace(
                b"https://management.azure.com", profile.arm_endpoint.encode()
            ),
        )

    return ArmClient(
        cast(AsyncTokenCredential, FakeCredential()),
        base_url=profile.arm_endpoint,
        scope=profile.arm_scope,
        client=httpx.AsyncClient(transport=httpx.MockTransport(sovereign_response)),
        sleep=no_sleep,
    )


@pytest.fixture
async def government() -> AsyncIterator[Harness]:
    estate = Harness()
    await estate.setup()
    publication = await estate.service.get_publication(ACTOR, estate.publication_id)
    gateway = estate.gateways.gateways[publication.gateway_id]
    estate.gateways.gateways[gateway.id] = gateway.model_copy(
        update={
            "azure_cloud": "government",
            "azure_connection_id": "fictional-government",
        }
    )
    endpoint = estate.endpoints.endpoints[publication.model_endpoint_id]
    estate.endpoints.endpoints[endpoint.id] = endpoint.model_copy(
        update={
            "azure_cloud": "government",
            "azure_connection_id": "fictional-government",
            "endpoint": str(endpoint.endpoint).replace(".azure.com", ".azure.us"),
        }
    )
    estate.gateways.publications[publication.id] = publication.model_copy(
        update={
            "azure_cloud": "government",
        }
    )
    await estate.arm.close()
    estate.arm = _government_arm(estate.apim.handle)
    await estate.govern(keys_enabled=True, entra_enabled=False)
    yield estate
    await estate.close()


def test_government_model_policy_has_no_commercial_token_path() -> None:
    publication = _publication(azure_cloud="government")
    snapshot = _snapshot(
        azure_cloud="government",
        settings=ModelAccessSettings(keys_enabled=True, entra_enabled=False),
    )
    snapshot.grants = [
        grant.model_copy(
            update={
                "subscription_name": model_access_subscription_name(
                    publication.tenant_id, publication.id, grant.entitlement_id
                )
            }
        )
        for grant in snapshot.grants
    ]
    policy = render_governed_policy(publication, snapshot)
    fragment = ET.fromstring(policy.fragment_xml)
    assert not fragment.findall(".//validate-azure-ad-token")
    assert "login.microsoftonline.com" not in policy.fragment_xml
    assert "tokens-off" in policy.fragment_xml
    assert "context.Subscription.Id" in policy.fragment_xml
    assert fragment.find(".//authentication-managed-identity").get("resource") == (
        "https://cognitiveservices.azure.us"
    )
    assert policy_expression_error(policy.fragment_xml) is None
    with pytest.raises(ValidationError, match="exact direct grant"):
        render_governed_policy(
            publication,
            snapshot.model_copy(
                update={
                    "grants": [
                        snapshot.grants[0].model_copy(update={"subscription_name": "all-apis-key"})
                    ]
                }
            ),
        )
    with pytest.raises(ValidationError, match="Government"):
        render_governed_policy(
            publication,
            snapshot.model_copy(
                update={
                    "settings": ModelAccessSettings(),
                }
            ),
        )


def test_government_mcp_validates_real_grant_subscription_and_hides_oauth() -> None:
    publication = _mcp_publication(azure_cloud="government")
    original = _mcp_grant()
    grant = original.model_copy(
        update={
            "subscription_name": model_access_subscription_name(
                publication.tenant_id, publication.id, original.entitlement_id
            ),
        }
    )
    snapshot = _mcp_snapshot(
        azure_cloud="government",
        audience="",
        settings=ModelAccessSettings(keys_enabled=True, entra_enabled=False),
        grants=[grant],
    )
    policy = render_mcp_policy(
        publication,
        snapshot,
        backend_auth=McpAuthMode.MANAGED_IDENTITY,
        backend_audience="api://fictional-government-mcp",
    )
    assert "context.Subscription.Id" in policy.fragment_xml
    assert grant.subscription_name in policy.fragment_xml
    assert "key-unknown" in policy.fragment_xml
    assert "cost-center-mismatch" in policy.fragment_xml
    assert "tokens-off" in policy.fragment_xml
    assert "validate-azure-ad-token" not in policy.fragment_xml
    assert "login.microsoftonline.com" not in policy.metadata_policy_xml
    assert "authorization_servers" not in policy.metadata_policy_xml
    assert 'code="404"' in policy.metadata_policy_xml
    assert policy_expression_error(policy.fragment_xml) is None
    with pytest.raises(ValidationError, match="exact direct grant"):
        render_mcp_policy(
            publication,
            snapshot.model_copy(
                update={
                    "grants": [grant.model_copy(update={"subscription_name": "shared-product-key"})]
                }
            ),
            backend_auth=McpAuthMode.MANAGED_IDENTITY,
            backend_audience="api://fictional-government-mcp",
        )


async def test_government_initial_key_follows_apply_and_delete_is_durable(
    government: Harness,
) -> None:
    grant = await government.grant()
    name = government.subscription(grant)
    assert f"subscriptions/{name}" not in government.apim.written
    run = await government.apply()
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "ready"
    assert government.apim.written[f"subscriptions/{name}"]["properties"]["state"] == "active"
    owner = Actor(USER, TENANT)
    connection = await government.keys().connection(owner, grant.id)
    assert connection.azure_cloud == "government"
    assert connection.key_exists and connection.runtime.key_state == "ready"
    assert connection.entra_audience is None and connection.entra_scope is None
    before = len(government.apim.payloads)
    await government.keys().resume_automatic_keys(TENANT)
    assert len(government.apim.payloads) == before
    await government.keys().delete_key(owner, grant.id)
    await government.keys().resume_automatic_keys(TENANT)
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "deleted"
    assert f"subscriptions/{name}" not in government.apim.written
    await government.keys().create_key(owner, grant.id)
    assert (await government.keys().connection(owner, grant.id)).key_exists


async def test_government_failed_initial_issue_retries_after_restart_without_rotation(
    government: Harness,
) -> None:
    grant = await government.grant()
    name = government.subscription(grant)
    government.apim.fail_activation = f"subscriptions/{name}"
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "failed"
    assert not (await government.keys().connection(Actor(USER, TENANT), grant.id)).key_exists
    government.apim.fail_activation = None
    await government.keys().resume_automatic_keys(TENANT)
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "ready"
    puts = len(government.apim.payloads)
    await government.keys().resume_automatic_keys(TENANT)
    assert len(government.apim.payloads) == puts
    assert not any("regenerate" in call["path"] for call in government.apim.http_calls)


@pytest.mark.parametrize(
    ("persisted_state", "created_before_restart"),
    [
        ("pending", False),
        ("preparing", False),
        ("failed", False),
        ("preparing", True),
        ("failed", True),
    ],
)
async def test_government_durable_resume_after_service_reconstruction(
    government: Harness,
    persisted_state: str,
    created_before_restart: bool,
) -> None:
    grant = await government.grant()
    name = government.subscription(grant)
    government.apim.fail_activation = f"subscriptions/{name}"
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "failed"
    await government.gateways.record_publication_state(
        publication.model_copy(update={"key_lifecycle": {name: persisted_state}})
    )
    if created_before_restart:
        government.apim.seed(
            f"subscriptions/{name}",
            {"properties": {"state": "active", "scope": f"/apis/{publication.api_name}"}},
        )
    await government.arm.close()
    government.arm = _government_arm(government.apim.handle)
    government.apim.fail_activation = None

    def reconstruct() -> PortalAccessService:
        def never_read(_resource: object) -> Any:
            raise AssertionError("Automatic key recovery must not reveal a credential")

        return PortalAccessService(
            EntitlementService(
                government.entitlements,
                directory_repository=government.directory,
                gateway_repository=government.gateways,
                endpoint_repository=government.endpoints,
                cost_center_repository=government.cost_center_records,
            ),
            repository=government.entitlements,
            directory_repository=government.directory,
            gateway_repository=government.gateways,
            credential_factory=never_read,
            key_manager_factory=lambda resource: ApimKeyManager(government.arm, resource),
        )

    before = len(government.apim.payloads)
    restarted = reconstruct()
    await restarted.resume_automatic_keys(TENANT)
    saved = await government.gateways.get_publication(TENANT, publication.id)
    assert saved.key_lifecycle[name] == "ready"
    assert len(government.apim.payloads) == before + (0 if created_before_restart else 1)
    assert (await restarted.connection(Actor(USER, TENANT), grant.id)).key_exists
    before = len(government.apim.payloads)
    await reconstruct().resume_automatic_keys(TENANT)
    assert len(government.apim.payloads) == before
    assert not any(
        "regenerate" in call["path"] or "listSecrets" in call["path"]
        for call in government.apim.http_calls
    )


async def test_government_preparing_journal_resumes_existing_key_without_another_put(
    government: Harness,
) -> None:
    grant = await government.grant()
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    name = government.subscription(grant)
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    await government.gateways.record_publication_state(
        publication.model_copy(update={"key_lifecycle": {name: "preparing"}})
    )
    writes = len(government.apim.payloads)
    await government.keys().resume_automatic_keys(TENANT)
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "ready"
    assert len(government.apim.payloads) == writes


async def test_government_interrupted_delete_never_recreates_the_key(
    government: Harness,
) -> None:
    grant = await government.grant()
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    name = government.subscription(grant)

    class InterruptedDelete(ApimKeyManager):
        async def delete(self, name: str) -> None:
            await super().delete(name)
            raise asyncio.CancelledError()

    keys = government.keys()
    keys._key_manager_factory = lambda resource: InterruptedDelete(government.arm, resource)
    with pytest.raises(asyncio.CancelledError):
        await keys.delete_key(Actor(USER, TENANT), grant.id)
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "deleted"
    assert f"subscriptions/{name}" not in government.apim.written
    await government.keys().resume_automatic_keys(TENANT)
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "deleted"
    assert f"subscriptions/{name}" not in government.apim.written


@pytest.mark.parametrize("explicit_delete", [False, True])
async def test_government_republish_only_reprovisions_non_deleted_lifecycles(
    government: Harness,
    explicit_delete: bool,
) -> None:
    grant = await government.grant()
    name = government.subscription(grant)
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    if explicit_delete:
        await government.keys().delete_key(Actor(USER, TENANT), grant.id)
    plan = await government.service.plan_unpublish(ACTOR, government.publication_id)
    run = await government.service.unpublish(ACTOR, government.publication_id, plan.id)
    await government.service.wait_for_idle()
    assert (await government.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    await government.keys().resume_automatic_keys(TENANT)
    assert f"subscriptions/{name}" not in government.apim.written
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == ("deleted" if explicit_delete else "ready")
    assert (f"subscriptions/{name}" in government.apim.written) is not explicit_delete


async def test_government_mcp_cost_permission_is_part_of_intent_but_commercial_stays_compatible(
    government: Harness,
) -> None:
    grant = await government.grant()
    principal = await government.directory.get_principal(TENANT, grant.subject.id)
    grant = grant.model_copy(
        update={"resource": grant.resource.model_copy(update={"kind": "mcpServer"})}
    )
    book = await government.grants.cost_center_book(TENANT)
    off = CostCenterBook(
        TENANT,
        [center.model_copy(update={"keys_allowed": False}) for center in book.by_id.values()],
        book.settings,
    )
    commercial = [
        entitlement_intent_digest(grant, principal, cost_center_intent(grant, principal, value))
        for value in (book, off)
    ]
    sovereign = [
        entitlement_intent_digest(
            grant, principal, cost_center_intent(grant, principal, value, key_capable=True)
        )
        for value in (book, off)
    ]
    assert commercial[0] == commercial[1]
    assert sovereign[0] != sovereign[1]


async def test_government_pending_cost_center_recheck_blocks_reveal_and_rotation(
    government: Harness,
) -> None:
    grant = await government.grant()
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    book = await government.grants.cost_center_book(TENANT)
    government.cost_center_records.cost_centers[(TENANT, grant.cost_center_id)] = book.get(
        grant.cost_center_id
    ).model_copy(
        update={
            "pending_rechecks": [
                PendingRecheck(reason="memberRemoved", subject_id=grant.subject.id)
            ]
        }
    )
    keys = government.keys()
    with pytest.raises(ConflictError, match="cost-center access needs review"):
        await keys.reveal_key(Actor(USER, TENANT), grant.id, "primary")
    with pytest.raises(ConflictError, match="cost-center access needs review"):
        await keys.rotate_key(Actor(USER, TENANT), grant.id, "primary")
    assert not any("regenerate" in call["path"] for call in government.apim.http_calls)


@pytest.mark.parametrize("drift", ["fragment", "subscriptionRequired"])
async def test_government_restart_rechecks_live_enforcement_before_creating(
    government: Harness,
    drift: str,
) -> None:
    grant = await government.grant()
    name = government.subscription(grant)
    government.apim.fail_activation = f"subscriptions/{name}"
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    government.apim.fail_activation = None
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    if drift == "fragment":
        government.apim.written[f"policyFragments/{publication.fragment_name}"]["properties"][
            "value"
        ] = "<fragment />"
    else:
        government.apim.written[f"apis/{publication.api_name}"]["properties"][
            "subscriptionRequired"
        ] = False
    writes = len(government.apim.payloads)
    await government.keys().resume_automatic_keys(TENANT)
    assert len(government.apim.payloads) == writes
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    assert publication.key_lifecycle[name] == "failed"
    assert f"subscriptions/{name}" not in government.apim.written
    with pytest.raises(ConflictError, match="Government"):
        await government.keys().create_key(Actor(USER, TENANT), grant.id)


async def test_government_unowned_subscription_collision_blocks_reviewed_apply(
    government: Harness,
) -> None:
    grant = await government.grant()
    name = government.subscription(grant)
    publication = await government.service.get_publication(ACTOR, government.publication_id)
    government.apim.seed(
        f"subscriptions/{name}",
        {"properties": {"state": "active", "scope": f"/apis/{publication.api_name}"}},
    )
    with pytest.raises(ConflictError, match=r"(own|created)"):
        await government.service.plan(ACTOR, government.publication_id)


def test_government_mcp_reference_uses_only_trusted_application_key_grant() -> None:
    publication = _publication(azure_cloud="government")
    person = _snapshot().grants[0]
    application = person.model_copy(
        update={
            "entitlement_id": "application-grant",
            "subject": person.subject.model_copy(
                update={"kind": EntitlementSubjectKind.APPLICATION}
            ),
        }
    )
    assert key_mcp_call_reference(publication, [person]) == ""
    expression = key_mcp_call_reference(publication, [person, application])
    assert grant_counter_identity(publication, application) in expression
    assert grant_counter_identity(publication, person) not in expression
    assert "mosaic-grant" in expression
    assert "mosaic-token" not in expression
    assert "Regex.IsMatch" in expression


async def test_government_cost_center_blocks_initial_keys_and_bearer_enable(
    government: Harness,
) -> None:
    grant = await government.grant()
    await government.cost_centers.update_cost_center(
        ACTOR, grant.cost_center_id, CostCenterUpdate(keys_allowed=False)
    )
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    assert f"subscriptions/{government.subscription(grant)}" not in government.apim.written
    connection = await government.keys().connection(Actor(USER, TENANT), grant.id)
    assert connection.keys_allowed_by_cost_center is False
    assert connection.runtime.key_state == "ineligible"
    with pytest.raises(ValidationError, match="Government"):
        await government.service.update(
            ACTOR,
            government.publication_id,
            PublicationUpdate(governed_access=ModelAccessSettings()),
        )


async def test_government_revocation_and_unpublish_remove_initial_key(government: Harness) -> None:
    grant = await government.grant()
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    await government.grants.update_entitlement(ACTOR, grant.id, EntitlementUpdate(enabled=False))
    assert (await government.apply()).status == PublishRunStatus.SUCCEEDED
    assert (
        government.apim.written[f"subscriptions/{government.subscription(grant)}"]["properties"][
            "state"
        ]
        == "suspended"
    )
    await government.keys().resume_automatic_keys(TENANT)
    assert (
        not (await government.keys().connection(Actor(USER, TENANT), grant.id)).runtime.status
        == "applied"
    )
    plan = await government.service.plan_unpublish(ACTOR, government.publication_id)
    run = await government.service.unpublish(ACTOR, government.publication_id, plan.id)
    await government.service.wait_for_idle()
    assert (await government.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    assert f"subscriptions/{government.subscription(grant)}" not in government.apim.written


async def test_government_mcp_end_to_end_key_ownership_and_cleanup() -> None:
    estate = McpHarness()
    await estate.setup()
    centers = InMemoryCostCenterRepository()
    estate.service._cost_centers = centers
    estate.service._runtime_client_id = None
    gateway = estate.gateway_repository.gateways[estate.gateway_id]
    estate.gateway_repository.gateways[gateway.id] = gateway.model_copy(
        update={
            "azure_cloud": "government",
            "azure_connection_id": "fictional-government",
        }
    )
    endpoint = estate.mcp_repository.endpoints[estate.endpoint_id]
    estate.mcp_repository.endpoints[endpoint.id] = endpoint.model_copy(
        update={
            "azure_cloud": "government",
            "azure_connection_id": "fictional-government",
            "auth_mode": McpAuthMode.MANAGED_IDENTITY,
            "resource_audience": "api://fictional-government-mcp",
        }
    )
    arm = _government_arm(estate.apim.handler)
    estate.service._client_factory = lambda resource: ApimClient(arm, resource)
    estate.service._writer_factory = lambda resource: ApimWriter(arm, resource)
    try:
        confirmed_gateway = estate.gateway_repository.gateways[gateway.id]
        estate.gateway_repository.gateways[gateway.id] = confirmed_gateway.model_copy(
            update={
                "capabilities": confirmed_gateway.capabilities.model_copy(
                    update={"mcp_servers": CapabilitySupport.UNKNOWN}
                )
            }
        )
        capability = await estate.service.capability(MCP_ACTOR, gateway.id)
        assert not capability.supported
        assert any(
            "Government MCP support must be confirmed" in item for item in capability.reasons
        )
        estate.gateway_repository.gateways[gateway.id] = confirmed_gateway
        publication_id = await estate.create()
        publication = await estate.service.get_publication(MCP_ACTOR, publication_id)
        principal = await estate.principal("alice", USER)
        grant = await estate.entitlement(
            "government-mcp-grant",
            EntitlementSubjectKind.USER,
            principal.id,
            publication.mcp_server_id,
        )
        plan = await estate.service.plan(MCP_ACTOR, publication_id)
        name = model_access_subscription_name(MCP_TENANT, publication_id, grant.id)
        assert f"subscriptions/{name}" not in estate.apim.written
        run = await estate.service.apply(MCP_ACTOR, publication_id, plan.id)
        await estate.service.wait_for_idle()
        result = await estate.service.get_run(MCP_ACTOR, publication_id, run.id)
        assert result.status == PublishRunStatus.SUCCEEDED, result.errors
        publication = await estate.service.get_publication(MCP_ACTOR, publication_id)
        assert publication.key_lifecycle[name] == "ready"
        properties = estate.apim.written[f"apis/{publication.api_name}"]["properties"]
        assert properties["subscriptionRequired"] is True
        assert properties["subscriptionKeyParameterNames"]["header"] == "Ocp-Apim-Subscription-Key"
        entitlements = EntitlementService(
            estate.entitlement_repository,
            directory_repository=estate.directory_repository,
            gateway_repository=estate.gateway_repository,
            endpoint_repository=InMemoryModelEndpointRepository(),
            cost_center_repository=centers,
        )

        class Reader:
            calls = 0

            async def read_key(self, *_args: object) -> SecretStr:
                self.calls += 1
                return SecretStr("fictional-government-key-never-persisted")

        reader = Reader()
        keys = PortalAccessService(
            entitlements,
            repository=estate.entitlement_repository,
            directory_repository=estate.directory_repository,
            gateway_repository=estate.gateway_repository,
            credential_factory=lambda resource: reader,
            key_manager_factory=lambda resource: ApimKeyManager(arm, resource),
        )
        owner = Actor(USER, MCP_TENANT)
        connection = await keys.mcp_connection(owner, grant.id)
        assert connection.enforced and connection.key_exists
        assert connection.resource_metadata_url is None
        assert connection.delegated_scope is None and connection.entra_audience is None
        assert reader.calls == 0

        class Caller:
            async def authenticate(self, _request: object) -> AuthContext:
                return AuthContext(object_id=USER, tenant_id=MCP_TENANT, roles=frozenset({"User"}))

            async def close(self) -> None:
                pass

        app = create_app(
            Settings(
                environment=Environment.TEST,
                auth_mode=AuthMode.LOCAL,
                repository_backend=RepositoryBackend.MEMORY,
                tenant_id=MCP_TENANT,
            )
        )
        with TestClient(app) as client:
            app.state.authenticator = Caller()
            app.state.portal_access_service = keys
            revealed = client.post(
                f"/api/v1/me/entitlements/{grant.id}/keys/reveal", json={"slot": "primary"}
            )
            assert revealed.status_code == 200, revealed.text
            assert revealed.json()["key"] == "fictional-government-key-never-persisted"
            assert "no-store" in revealed.headers["Cache-Control"]
            assert revealed.headers["Pragma"] == "no-cache"
        assert reader.calls == 1
        audits = list(estate.entitlement_repository.audit_events.values())
        assert any(event.action == "credential.reveal.succeeded" for event in audits)
        assert "fictional-government-key-never-persisted" not in repr(audits)
        applied = await estate.entitlement_repository.get_entitlement(MCP_TENANT, grant.id)
        assert applied.binding.apim_subscription_name == name
        book = await entitlements.cost_center_book(MCP_TENANT)
        center = book.get(grant.cost_center_id)
        centers.cost_centers[(MCP_TENANT, center.id)] = center.model_copy(
            update={"keys_allowed": False}
        )
        assert (await keys.mcp_connection(owner, grant.id)).runtime.status == "pending"
        with pytest.raises(ConflictError, match="pending changes"):
            await keys.reveal_key(owner, grant.id, "primary")
        assert reader.calls == 1
        plan = await estate.service.plan(MCP_ACTOR, publication_id)
        applied_run = await estate.service.apply(MCP_ACTOR, publication_id, plan.id)
        await estate.service.wait_for_idle()
        assert (
            await estate.service.get_run(MCP_ACTOR, publication_id, applied_run.id)
        ).status == PublishRunStatus.SUCCEEDED
        assert estate.apim.written[f"subscriptions/{name}"]["properties"]["state"] == "suspended"
        unavailable = await keys.mcp_connection(owner, grant.id)
        assert not unavailable.keys_allowed_by_cost_center
        assert not unavailable.key_exists and not unavailable.enforced
        centers.cost_centers[(MCP_TENANT, center.id)] = center
        await keys.resume_automatic_keys(MCP_TENANT)
        assert estate.apim.written[f"subscriptions/{name}"]["properties"]["state"] == "suspended"
        plan = await estate.service.plan(MCP_ACTOR, publication_id)
        applied_run = await estate.service.apply(MCP_ACTOR, publication_id, plan.id)
        await estate.service.wait_for_idle()
        assert (
            await estate.service.get_run(MCP_ACTOR, publication_id, applied_run.id)
        ).status == PublishRunStatus.SUCCEEDED
        assert estate.apim.written[f"subscriptions/{name}"]["properties"]["state"] == "active"
        with pytest.raises(NotFoundError):
            await keys.delete_key(Actor("stranger", MCP_TENANT), grant.id)
        await keys.delete_key(owner, grant.id)
        await keys.resume_automatic_keys(MCP_TENANT)
        assert f"subscriptions/{name}" not in estate.apim.written
        await keys.create_key(owner, grant.id)
        estate.apim.seed(
            f"subscriptions/{name}",
            {
                "properties": {
                    "scope": "/apis/unrelated-api",
                    "state": "active",
                }
            },
        )
        with pytest.raises(ConflictError, match="scoped"):
            await keys.delete_key(owner, grant.id)
        estate.apim.seed(
            f"subscriptions/{name}",
            {
                "properties": {
                    "scope": f"/apis/{publication.api_name}",
                    "state": "active",
                }
            },
        )
        unpublish = await estate.service.plan_unpublish(MCP_ACTOR, publication_id)
        run = await estate.service.unpublish(MCP_ACTOR, publication_id, unpublish.id)
        await estate.service.wait_for_idle()
        assert (
            await estate.service.get_run(MCP_ACTOR, publication_id, run.id)
        ).status == PublishRunStatus.SUCCEEDED
        assert f"subscriptions/{name}" not in estate.apim.written
    finally:
        await estate.service.aclose()
        await arm.close()


async def test_government_pool_initial_key_is_shared_only_after_each_model_apply() -> None:
    estate = Portal()
    await estate.setup()
    gateway = estate.gateway_repository.gateways[estate.gateway_id]
    estate.gateway_repository.gateways[gateway.id] = gateway.model_copy(
        update={
            "azure_cloud": "government",
            "azure_connection_id": "fictional-government",
        }
    )
    for name, endpoint in list(estate.endpoint_repository.endpoints.items()):
        estate.endpoint_repository.endpoints[name] = endpoint.model_copy(
            update={
                "azure_cloud": "government",
                "azure_connection_id": "fictional-government",
                "endpoint": str(endpoint.endpoint).replace(".azure.com", ".azure.us"),
            }
        )
    await estate.arm.close()
    estate.arm = _government_arm(estate._handle)
    estate.service._client_factory = lambda resource: ApimClient(estate.arm, resource)
    estate.service._writer_factory = lambda resource: ApimWriter(estate.arm, resource)
    try:
        pool = await estate.create(
            "Government OpenAI",
            _gpt4o("aoai-east"),
            _model(_member("aoai-east", "gpt-4o-mini"), display_name="GPT mini"),
        )
        assert pool.governed_access == ModelAccessSettings(keys_enabled=True, entra_enabled=False)
        principal = await estate.principal("Ada")
        first = await estate.grant_model(pool, principal, 0)
        name = entitlement_key_name(pool, first)
        assert name is not None
        await estate.publish(pool.id)
        pool = await estate.pool(pool.id)
        assert pool.key_lifecycle[name] == "ready"
        owner = Actor(principal.object_id, POOL_TENANT)
        assert (await estate.access.connection(owner, first.id)).key_exists
        second = await estate.grant_model(pool, principal, 1)
        with pytest.raises(ConflictError, match=r"pending|applied"):
            await estate.access.reveal_key(owner, second.id, "primary")
        lookup = _pool_key_lookup(pool, pool.applied_access.grants)
        assert grant_counter_identity(pool, pool.applied_access.grants[0]) in lookup
        assert second.id not in str(pool.applied_access.model_dump())
        assert '"!"' in lookup or 'return ""' in lookup
        await estate.publish(pool.id)
        second_connection = await estate.access.connection(owner, second.id)
        assert second_connection.key_exists
        assert len(second_connection.key_shared_with) == 1
        assert second_connection.entra_scope is None
        await estate.access.delete_key(owner, second.id)
        await estate.access.resume_automatic_keys(POOL_TENANT)
        await estate.publish(pool.id)
        assert f"subscriptions/{name}" not in estate.apim.written
        assert (await estate.pool(pool.id)).key_lifecycle[name] == "deleted"
        with pytest.raises(ValidationError, match="Government"):
            await estate.update(pool.id, governed_access=ModelAccessSettings())
    finally:
        await estate.service.aclose()
        await estate.arm.close()
