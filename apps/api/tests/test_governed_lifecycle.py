"""The governed-access lifecycle uses the real ARM writer without reading subscription secrets."""

import asyncio
import json
import re
from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from aoai_double import AI_RESOURCE_ID, FakeCognitiveServices
from apim_double import (
    CONTRIBUTOR_PERMISSIONS,
    RESOURCE_ID,
    FakeApim,
    FakeCredential,
    policy_expression_error,
)
from azure.core.credentials_async import AsyncTokenCredential
from conftest import build_endpoint_service, build_gateway_service, reviewed_unpublish
from mosaic_api.cost_centers import CostCenterUpdate
from mosaic_api.domain import (
    BindingSource,
    CatalogEntryUpdate,
    Entitlement,
    EntitlementBinding,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    EntitlementUpdate,
    GatewayCreate,
    GatewayUpdate,
    GroupCreate,
    ImportRequest,
    ModelAccessSettings,
    ModelEndpointCreate,
    PrincipalCreate,
    PrincipalUpdate,
    Publication,
    PublicationCreate,
    PublicationStatus,
    PublicationUpdate,
    PublishedResourceKind,
    PublishRun,
    PublishRunStatus,
    PublishStepStatus,
    RequestEnforcement,
    TokenEnforcement,
    general_cost_center_id,
    model_access_subscription_name,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations import access_policy
from mosaic_api.integrations.apim import ApimClient, ApimWriter, ArmClient
from mosaic_api.integrations.apim.client import MAX_ATTEMPTS
from mosaic_api.integrations.apim.credentials import ApimKeyManager
from mosaic_api.observed import ObservedApi
from mosaic_api.repositories import (
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor, DirectoryService
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import gateway_mutation_scope, publication_lock
from mosaic_api.services.portal_access import PortalAccessService
from mosaic_api.services.publishing import PublishingService

TENANT = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
AUDIENCE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
ACTOR = Actor("cccccccc-cccc-cccc-cccc-cccccccccccc", TENANT)
USER = "11111111-1111-1111-1111-111111111111"
APPLICATION = "22222222-2222-2222-2222-222222222222"
NEW_USER = "33333333-3333-3333-3333-333333333333"


class AccessApim(FakeApim):
    def __init__(self) -> None:
        super().__init__(permissions=CONTRIBUTOR_PERMISSIONS)
        self.payloads: list[tuple[str, dict[str, Any]]] = []
        self.fail_activation: str | None = None
        # How many activations of ``fail_activation`` fail; None fails every one.
        self.activation_failures: int | None = None
        self.pause_suffix: str | None = None
        self.paused = asyncio.Event()
        self.resume = asyncio.Event()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        suffix = request.url.path.removeprefix(RESOURCE_ID).strip("/")
        if request.method == "PUT":
            body = json.loads(request.content)
            self.payloads.append((suffix, body))
            if self.pause_suffix == suffix:
                self.paused.set()
                await self.resume.wait()
            if (
                suffix == self.fail_activation
                and body.get("properties", {}).get("state") == "active"
                and self.activation_failures != 0
            ):
                if self.activation_failures is not None:
                    self.activation_failures -= 1
                return httpx.Response(500, json={"error": {"message": "activation failed"}})
        return self.handler(request)


async def no_sleep(_seconds: float) -> None:
    return None


PUBLICATION_ENFORCEMENT = TokenEnforcement(
    counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=10000
)


class Harness:
    def __init__(self, cognitive: FakeCognitiveServices | None = None) -> None:
        self.apim = AccessApim()
        self.gateways = InMemoryGatewayRepository()
        self.directory = InMemoryDirectoryRepository()
        self.entitlements = InMemoryEntitlementRepository()
        self.endpoints = InMemoryModelEndpointRepository()
        self.cost_center_records = InMemoryCostCenterRepository()
        self.gateway_service = build_gateway_service(self.apim, self.gateways)
        self.endpoint_service = build_endpoint_service(
            cognitive or FakeCognitiveServices(),
            repository=self.endpoints,
            gateway_repository=self.gateways,
        )
        self.arm = ArmClient(
            cast(AsyncTokenCredential, FakeCredential()),
            client=httpx.AsyncClient(transport=httpx.MockTransport(self.apim.handle)),
            sleep=no_sleep,
        )
        self.service = self.other_service()
        self.directory_service = DirectoryService(
            self.directory,
            gateway_repository=self.gateways,
            entitlement_repository=self.entitlements,
            cost_center_repository=self.cost_center_records,
        )
        self.grants = EntitlementService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=self.endpoints,
            cost_center_repository=self.cost_center_records,
        )
        self.cost_centers = CostCenterService(
            self.cost_center_records,
            directory_repository=self.directory,
            entitlement_repository=self.entitlements,
            gateway_repository=self.gateways,
            entitlements=self.grants,
        )
        self.publication_id = ""
        self.model_id = ""

    def other_service(self) -> PublishingService:
        return PublishingService(
            self.gateways,
            endpoint_repository=self.endpoints,
            directory_repository=self.directory,
            entitlement_repository=self.entitlements,
            client_factory=lambda resource: ApimClient(self.arm, resource),
            writer_factory=lambda resource: ApimWriter(self.arm, resource),
            model_runtime_client_id=AUDIENCE,
            cost_center_repository=self.cost_center_records,
        )

    async def setup(
        self,
        *,
        deployment: str = "gpt-4o-prod",
        enforcement: TokenEnforcement | None = PUBLICATION_ENFORCEMENT,
    ) -> None:
        gateway = await self.gateway_service.register(
            ACTOR, GatewayCreate(azure_resource_id=RESOURCE_ID)
        )
        await self.gateway_service.sync_now(ACTOR, gateway.id)
        await self.gateway_service.update(
            ACTOR, gateway.id, GatewayUpdate(management_mode="manage")
        )
        endpoint = await self.endpoint_service.register(
            ACTOR, ModelEndpointCreate(azure_resource_id=AI_RESOURCE_ID)
        )
        await self.endpoint_service.sync_now(ACTOR, endpoint.id)
        publication = await self.service.create(
            ACTOR,
            PublicationCreate(
                gateway_id=gateway.id,
                model_endpoint_id=endpoint.id,
                deployment_name=deployment,
                enforcement=enforcement,
            ),
        )
        self.publication_id = publication.id
        assert (await self.apply()).status == PublishRunStatus.SUCCEEDED
        published = await self.service.get_publication(ACTOR, self.publication_id)
        assert published.model_api_id
        self.model_id = published.model_api_id

    async def govern(self, **methods: bool) -> None:
        await self.service.update(
            ACTOR,
            self.publication_id,
            PublicationUpdate(governed_access=ModelAccessSettings(**methods)),
        )

    async def grant(self, object_id: str = USER, *, application: bool = False) -> Entitlement:
        principal = await self.directory_service.create_principal(
            ACTOR,
            PrincipalCreate(
                object_id=object_id,
                kind="servicePrincipal" if application else "user",
                label=f"Principal {object_id}",
            ),
        )
        return await self.grants.create_entitlement(
            ACTOR,
            EntitlementCreate(
                subject=EntitlementSubject(
                    kind="application" if application else "user", id=principal.id
                ),
                resource=EntitlementResource(kind="modelApi", id=self.model_id),
            ),
        )

    async def apply(self) -> PublishRun:
        plan = await self.service.plan(ACTOR, self.publication_id)
        run = await self.service.apply(ACTOR, self.publication_id, plan.id)
        await self.service.wait_for_idle()
        return await self.service.get_run(ACTOR, run.id)

    def subscription(self, grant: Entitlement) -> str:
        return model_access_subscription_name(TENANT, self.publication_id, grant.id)

    def keys(self) -> PortalAccessService:
        """Keys on request, as the portal and the Entitlements page manage them."""

        def no_reader(_resource: object) -> Any:
            raise AssertionError("Managing a key never reads one")

        return PortalAccessService(
            self.grants,
            repository=self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            credential_factory=no_reader,
            key_manager_factory=lambda resource: ApimKeyManager(self.arm, resource),
        )

    async def key(self, *grants: Entitlement) -> None:
        """Create each grant's key, as an administrator does once its access is applied."""

        keys = self.keys()
        for grant in grants:
            await keys.create_key(ACTOR, grant.id, administrator=True)

    async def close(self) -> None:
        await self.service.aclose()
        await self.gateway_service.aclose()
        await self.endpoint_service.aclose()
        await self.arm.close()


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    instance = Harness()
    await instance.setup()
    yield instance
    await instance.close()


async def test_opt_in_is_model_wide_staged_owned_and_secret_free(harness: Harness) -> None:
    user = await harness.grant()
    application = await harness.grant(APPLICATION, application=True)
    group = await harness.directory_service.create_group(ACTOR, GroupCreate(name="unsupported"))
    group_grant = await harness.grants.create_entitlement(
        ACTOR,
        EntitlementCreate(
            subject=EntitlementSubject(kind="group", id=group.id),
            resource=EntitlementResource(kind="modelApi", id=harness.model_id),
        ),
    )
    await harness.govern()
    plan = await harness.service.plan(ACTOR, harness.publication_id)
    assert plan.access_snapshot
    assert {grant.entitlement_id for grant in plan.access_snapshot.grants} == {
        user.id,
        application.id,
    }
    assert any(group_grant.id in warning for warning in plan.warnings)
    assert any("publication-wide" in warning for warning in plan.warnings)
    assert plan.previous_access_version is None
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    # An apply never creates a grant's key; its holder or an administrator does, on request.
    for grant in (user, application):
        assert f"subscriptions/{harness.subscription(grant)}" not in harness.apim.written
        loaded = await harness.grants.get_entitlement(ACTOR, grant.id)
        assert loaded.runtime and loaded.runtime.status == "applied"
        assert loaded.runtime.key_exists is False
    await harness.key(user, application)
    harness.apim.payloads.clear()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert (
        publication.applied_access and publication.applied_access.settings == ModelAccessSettings()
    )
    assert publication.access_state == "applied"
    for grant in (user, application):
        name = harness.subscription(grant)
        payload = harness.apim.written[f"subscriptions/{name}"]["properties"]
        assert payload["scope"] == f"{RESOURCE_ID}/apis/{publication.api_name}"
        assert payload["state"] == "active"
        assert payload["displayName"].endswith("(general)")
        staged = [
            body["properties"]["state"]
            for path, body in harness.apim.payloads
            if path == f"subscriptions/{name}"
        ]
        assert staged == ["suspended", "active"]
        loaded = await harness.grants.get_entitlement(ACTOR, grant.id)
        assert loaded.runtime and loaded.runtime.status == "applied"
        assert loaded.runtime.key_exists is True
        assert loaded.binding and loaded.binding.source == BindingSource.ORCHESTRATED
    assert (
        harness.apim.written[f"subscriptions/{publication.subscription_name}"]["properties"][
            "state"
        ]
        == "suspended"
    )
    assert not harness.apim.written[f"apis/{publication.api_name}"]["properties"][
        "subscriptionRequired"
    ]
    assert harness.apim.written[f"products/{publication.product_name}"]["properties"][
        "subscriptionRequired"
    ]
    policy_index = max(
        i
        for i, (path, _) in enumerate(harness.apim.payloads)
        if path == f"apis/{publication.api_name}/policies/policy"
    )
    relaxed_index = next(
        i
        for i, (path, body) in enumerate(harness.apim.payloads)
        if path == f"apis/{publication.api_name}" and not body["properties"]["subscriptionRequired"]
    )
    activated_index = next(
        i
        for i, (_, body) in enumerate(harness.apim.payloads)
        if body.get("properties", {}).get("state") == "active"
    )
    assert policy_index < relaxed_index < activated_index
    assert not any("listSecrets" in path for path in harness.apim.requests)


@pytest.mark.parametrize(
    "change", ["grant", "identity", "kind", "label", "settings", "audience", "version", "ownership"]
)
async def test_changed_inputs_reject_stale_plans(harness: Harness, change: str) -> None:
    grant = await harness.grant()
    await harness.govern()
    if change == "version":
        assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    plan = await harness.service.plan(ACTOR, harness.publication_id)
    if change == "grant":
        await harness.grants.update_entitlement(ACTOR, grant.id, EntitlementUpdate(enabled=False))
    elif change in {"identity", "kind", "label"}:
        principal = harness.directory.principals[grant.subject.id]
        changes = {
            "identity": {"object_id": NEW_USER},
            "kind": {"kind": "servicePrincipal"},
            "label": {"label": "Changed after review"},
        }
        harness.directory.principals[principal.id] = principal.model_copy(
            update=changes[change]
        )
    elif change == "settings":
        await harness.govern(keys_enabled=False)
    elif change == "audience":
        harness.service._runtime_audience = NEW_USER
    elif change == "ownership":
        publication = await harness.service.get_publication(ACTOR, harness.publication_id)
        await harness.gateways.record_publication_state(
            publication.model_copy(update={"resources": publication.resources[:-1]})
        )
    else:
        publication = await harness.service.get_publication(ACTOR, harness.publication_id)
        assert publication.applied_access
        await harness.gateways.record_publication_state(
            publication.model_copy(
                update={
                    "applied_access": publication.applied_access.model_copy(
                        update={"version": publication.applied_access.version + 1}
                    )
                }
            )
        )
    harness.apim.writes.clear()
    with pytest.raises(ConflictError, match="changed after the plan"):
        await harness.service.apply(ACTOR, harness.publication_id, plan.id)
    assert not harness.apim.writes
    assert await harness.gateways.get_publication_lock(TENANT, harness.publication_id) is None


async def test_method_toggles_preserve_subscriptions_counters_and_ownership(
    harness: Harness,
) -> None:
    grant = await harness.grant()
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    await harness.key(grant)
    baseline = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert baseline.binding
    counter = baseline.binding.counter_key_expression
    assert counter and counter.startswith('@("mosaic:governed:publication-tokens:')
    assert counter.endswith('")')
    name = baseline.binding.apim_subscription_name
    for keys, entra in [(False, True), (True, False), (False, False), (True, True)]:
        await harness.govern(keys_enabled=keys, entra_enabled=entra)
        assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
        publication = await harness.service.get_publication(ACTOR, harness.publication_id)
        loaded = await harness.grants.get_entitlement(ACTOR, grant.id)
        assert publication.subscription_required is not entra
        assert (
            publication.applied_access and publication.applied_access.settings.keys_enabled is keys
        )
        assert harness.apim.written[f"subscriptions/{name}"]["properties"]["state"] == (
            "active" if keys else "suspended"
        )
        if keys or entra:
            assert loaded.binding and loaded.binding.counter_key_expression == counter
            assert loaded.runtime and loaded.runtime.status == "applied"
        else:
            assert loaded.runtime and loaded.runtime.status == "revoked"
        assert all(resource.created_by_mosaic for resource in publication.resources)
    assert not harness.apim.write_paths("DELETE")
    with pytest.raises(ValidationError, match="cannot be cleared"):
        await harness.service.update(
            ACTOR, harness.publication_id, PublicationUpdate(governed_access=None)
        )


async def test_disable_and_reenable_are_intent_until_applied(harness: Harness) -> None:
    grant = await harness.grant()
    await harness.govern()
    await harness.apply()
    await harness.key(grant)
    disabled = await harness.grants.update_entitlement(
        ACTOR, grant.id, EntitlementUpdate(enabled=False)
    )
    assert disabled.runtime and disabled.runtime.status == "revocationPending"
    before = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert before.applied_access and before.applied_access.grants[0].enabled
    await harness.apply()
    revoked = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert revoked.runtime and revoked.runtime.status == "revoked"
    assert revoked.binding is None
    assert (
        harness.apim.written[f"subscriptions/{harness.subscription(grant)}"]["properties"]["state"]
        == "suspended"
    )
    pending = await harness.grants.update_entitlement(
        ACTOR, grant.id, EntitlementUpdate(enabled=True)
    )
    assert pending.runtime and pending.runtime.status == "pending"
    await harness.apply()
    assert (await harness.grants.get_entitlement(ACTOR, grant.id)).runtime.status == "applied"
    assert not harness.apim.write_paths("DELETE")


async def test_failed_activation_never_retains_new_or_revoked_entra_access(
    harness: Harness,
) -> None:
    revoked = await harness.grant()
    unchanged = await harness.grant(APPLICATION, application=True)
    await harness.govern()
    await harness.apply()
    await harness.key(revoked, unchanged)
    await harness.grants.update_entitlement(ACTOR, revoked.id, EntitlementUpdate(enabled=False))
    newcomer = await harness.grant(NEW_USER)
    # Every attempt of the apply's activation fails, and the recovery's activation succeeds.
    harness.apim.fail_activation = f"subscriptions/{harness.subscription(unchanged)}"
    harness.apim.activation_failures = MAX_ATTEMPTS
    run = await harness.apply()
    assert run.status == PublishRunStatus.FAILED
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert publication.access_state == "failed"
    assert publication.applied_access
    assert {g.entitlement_id for g in publication.applied_access.grants if g.enabled} == {
        unchanged.id
    }
    policy = harness.apim.written[f"policyFragments/{publication.fragment_name}"]["properties"][
        "value"
    ]
    assert NEW_USER not in policy
    assert USER not in policy
    assert APPLICATION in policy
    assert (
        harness.apim.written[f"subscriptions/{harness.subscription(revoked)}"]["properties"][
            "state"
        ]
        == "suspended"
    )
    # The unchanged grant's key is active again with the last safe snapshot.
    assert (
        harness.apim.written[f"subscriptions/{harness.subscription(unchanged)}"]["properties"][
            "state"
        ]
        == "active"
    )
    assert f"subscriptions/{harness.subscription(newcomer)}" not in harness.apim.written
    assert not harness.apim.write_paths("DELETE")
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) is None


async def test_failed_activation_restores_the_safe_snapshot_when_grants_have_no_key(
    harness: Harness,
) -> None:
    # Keys are created on request, so recovery must not mistake a grant without one for a lost
    # key and shut the whole model off.
    keyed = await harness.grant(APPLICATION, application=True)
    keyless = await harness.grant()
    revoked = await harness.grant(NEW_USER)
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    await harness.key(keyed)
    await harness.grants.update_entitlement(ACTOR, revoked.id, EntitlementUpdate(enabled=False))
    harness.apim.fail_activation = f"subscriptions/{harness.subscription(keyed)}"
    harness.apim.activation_failures = MAX_ATTEMPTS

    assert (await harness.apply()).status == PublishRunStatus.FAILED

    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert publication.access_state == "failed"
    assert publication.applied_access
    assert publication.applied_access.settings == ModelAccessSettings()
    assert {g.entitlement_id for g in publication.applied_access.grants if g.enabled} == {
        keyed.id,
        keyless.id,
    }
    assert (
        harness.apim.written[f"subscriptions/{harness.subscription(keyed)}"]["properties"][
            "state"
        ]
        == "active"
    )
    # Recovery never creates a key.
    for grant in (keyless, revoked):
        assert f"subscriptions/{harness.subscription(grant)}" not in harness.apim.written
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) is None


async def test_recoding_a_cost_center_after_deleting_an_applied_grant_still_plans(
    harness: Harness,
) -> None:
    kept = await harness.grant()
    removed = await harness.grant(APPLICATION, application=True)
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    await harness.grants.update_entitlement(ACTOR, removed.id, EntitlementUpdate(enabled=False))
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    # Without a key it can be deleted, and later snapshots carry it forward, disabled, with the
    # code its cost center had then.
    await harness.grants.delete_entitlement(ACTOR, removed.id)
    await harness.cost_centers.update_cost_center(
        ACTOR, general_cost_center_id(TENANT), CostCenterUpdate(code="gen")
    )

    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED

    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert publication.applied_access
    grants = {
        grant.entitlement_id: (grant.enabled, grant.cost_center_code)
        for grant in publication.applied_access.grants
    }
    assert grants[kept.id] == (True, "gen")
    assert grants[removed.id] == (False, "general")


async def test_restricting_limits_does_not_restore_old_permissions_on_failure(
    harness: Harness,
) -> None:
    grant = await harness.grant()
    await harness.govern()
    await harness.apply()
    await harness.key(grant)
    await harness.grants.update_entitlement(
        ACTOR,
        grant.id,
        EntitlementUpdate(
            enforcement=EntitlementEnforcement(
                requests=RequestEnforcement(
                    counter_key_expression="@(context.Subscription.Id)",
                    calls=1,
                    renewal_period_seconds=60,
                )
            )
        ),
    )
    harness.apim.fail_activation = f"subscriptions/{harness.subscription(grant)}"
    assert (await harness.apply()).status == PublishRunStatus.FAILED
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert publication.applied_access
    assert not any(grant.enabled for grant in publication.applied_access.grants)


async def test_durable_lock_covers_other_instances_mutations_and_startup(harness: Harness) -> None:
    grant = await harness.grant()
    await harness.govern()
    plan = await harness.service.plan(ACTOR, harness.publication_id)
    run = await harness.service.apply(ACTOR, harness.publication_id, plan.id)
    other = harness.other_service()
    with pytest.raises(ConflictError, match="already running"):
        await other.apply(ACTOR, harness.publication_id, plan.id)
    with pytest.raises(ConflictError, match="already running"):
        await harness.grants.update_entitlement(ACTOR, grant.id, EntitlementUpdate(enabled=False))
    with pytest.raises(ConflictError, match="already running"):
        await other.plan_unpublish(ACTOR, harness.publication_id)
    assert await other.reap_stale_publish_runs(TENANT) == 0
    assert (await other.get_run(ACTOR, run.id)).status == PublishRunStatus.RUNNING
    await harness.service.wait_for_idle()
    assert (await other.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    await other.aclose()


async def test_cancelled_writer_retains_lock_until_explicit_quiesced_recovery(
    harness: Harness,
) -> None:
    await harness.grant()
    await harness.govern()
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    harness.apim.pause_suffix = f"backends/{publication.backend_name}"
    plan = await harness.service.plan(ACTOR, publication.id)
    run = await harness.service.apply(ACTOR, publication.id, plan.id)
    await asyncio.wait_for(harness.apim.paused.wait(), timeout=2)
    await harness.service.aclose()
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) == run.id
    other = harness.other_service()
    assert await other.reap_stale_publish_runs(TENANT) == 0
    diagnostic = await other.recover_interrupted(ACTOR, publication.id, run_id=run.id)
    assert diagnostic.status == PublishRunStatus.INTERRUPTED
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) == run.id
    recovered = await other.recover_interrupted(
        ACTOR, publication.id, run_id=run.id, confirm_quiesced=True
    )
    assert recovered.status == PublishRunStatus.INTERRUPTED
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) is None
    publication = await other.get_publication(ACTOR, publication.id)
    assert publication.applied_access
    assert not any(grant.enabled for grant in publication.applied_access.grants)
    assert publication.access_state == "failed"
    await other.aclose()


async def test_managed_objects_and_binding_cannot_be_forgotten(harness: Harness) -> None:
    grant = await harness.grant()
    await harness.govern()
    await harness.apply()
    await harness.key(grant)
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    for binding in (
        None,
        EntitlementBinding(
            gateway_id=publication.gateway_id,
            apim_subscription_name="forged",
            source=BindingSource.MANUAL,
        ),
    ):
        with pytest.raises(ConflictError, match="server-managed"):
            await harness.grants.update_entitlement(
                ACTOR, grant.id, EntitlementUpdate(binding=binding)
            )
    with pytest.raises(ConflictError):
        await harness.grants.delete_entitlement(ACTOR, grant.id)
    with pytest.raises(ConflictError):
        await harness.directory_service.delete_principal(ACTOR, grant.subject.id)
    with pytest.raises(ConflictError):
        await harness.directory_service.update_principal(
            ACTOR, grant.subject.id, PrincipalUpdate(kind="servicePrincipal")
        )
    with pytest.raises(ConflictError):
        await harness.gateway_service.delete_model_api(ACTOR, harness.model_id)
    with pytest.raises(ConflictError):
        await harness.service.delete(ACTOR, publication.id)
    with pytest.raises(ConflictError):
        await harness.gateway_service.delete(ACTOR, publication.gateway_id)
    run = await reviewed_unpublish(harness.service, ACTOR, publication.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    grant = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert grant.binding is None
    assert grant.runtime and grant.runtime.status == "revoked"
    assert f"subscriptions/{harness.subscription(grant)}" in harness.apim.write_paths("DELETE")
    await harness.grants.delete_entitlement(ACTOR, grant.id)
    await harness.directory_service.delete_principal(ACTOR, grant.subject.id)


async def test_model_materialization_linking_and_reimport_preserve_metadata(
    harness: Harness,
) -> None:
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    model = await harness.gateways.get_model_api(TENANT, harness.model_id)
    assert model and model.publication_id == publication.id
    assert model.imported_from_snapshot_id is None
    model = await harness.gateway_service.update_model_api_catalog(
        ACTOR, model.id, CatalogEntryUpdate(visibility="private", summary="Keep this description")
    )
    await harness.gateways.record_publication_state(
        publication.model_copy(update={"model_api_id": None})
    )
    harness.gateways.model_apis[model.id] = model.model_copy(update={"publication_id": None})
    harness.apim.writes.clear()
    linked = await harness.service.link_model_api(ACTOR, publication.id)
    assert linked.id == model.id and linked.visibility == "private"
    assert linked.summary == model.summary
    assert not harness.apim.writes
    observed = (
        await harness.gateways.list_observed(
            ObservedApi, TENANT, publication.gateway_id, "observedApi"
        )
    )[0].model_copy(
        update={
            "id": "observed-linked-api",
            "name": publication.api_name,
            "snapshot_id": "real-import-snapshot",
        }
    )
    harness.gateways.observed[observed.id] = observed
    imported = await harness.gateway_service.import_model_apis(
        ACTOR, publication.gateway_id, ImportRequest(api_names=[publication.api_name])
    )
    assert imported[0].publication_id == publication.id
    assert imported[0].summary == model.summary
    assert imported[0].visibility == "private"
    assert imported[0].created_at == model.created_at
    assert imported[0].imported_from_snapshot_id == "real-import-snapshot"


async def test_governed_plan_never_takes_over_customer_policy(harness: Harness) -> None:
    await harness.govern()
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    await harness.gateways.record_publication_state(
        publication.model_copy(
            update={
                "resources": [
                    item.model_copy(update={"created_by_mosaic": False})
                    if item.kind == PublishedResourceKind.POLICY_FRAGMENT
                    else item
                    for item in publication.resources
                ]
            }
        )
    )
    harness.apim.writes.clear()
    with pytest.raises(ConflictError, match="customer resources or policy"):
        await harness.service.plan(ACTOR, publication.id)
    assert not harness.apim.writes


async def test_governed_apply_creates_the_backend_before_the_fragment_that_routes_to_it(
    harness: Harness,
) -> None:
    await harness.grant()
    await harness.govern()
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    backend = f"backends/{publication.backend_name}"
    fragment = f"policyFragments/{publication.fragment_name}"
    # The backend went missing outside MOSAIC, so the governed apply has to create it again, and
    # first: API Management fails a fragment whose set-backend-service names a missing backend.
    harness.apim.written.pop(backend)
    harness.apim.writes.clear()

    plan = await harness.service.plan(ACTOR, harness.publication_id)

    stages = [(step.kind, step.stage) for step in plan.steps]
    assert stages.index((PublishedResourceKind.BACKEND, "prepare")) < stages.index(
        (PublishedResourceKind.POLICY_FRAGMENT, "policy")
    )
    run = await harness.service.apply(ACTOR, harness.publication_id, plan.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    puts = harness.apim.write_paths("PUT")
    assert puts.index(backend) < puts.index(fragment)
    assert fragment in harness.apim.written
    # Nothing written during setup or this apply named a resource that did not exist yet.
    assert harness.apim.dangling_references == []


async def test_a_governed_fragment_update_azure_rejects_is_not_applied_access(
    harness: Harness,
) -> None:
    await harness.grant()
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    newcomer = await harness.grant(NEW_USER)
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    backend = f"backends/{publication.backend_name}"
    fragment = f"policyFragments/{publication.fragment_name}"
    previous = harness.apim.written[fragment]["properties"]["value"]
    # The backend is deleted after the prepare stage rewrites it, so the policy stage's update of
    # the fragment names a missing backend. API Management answers that update 200 with a
    # Location header, then fails the operation and keeps the fragment it had.
    harness.apim.remove_before_write(fragment, backend)

    run = await harness.apply()

    assert run.status == PublishRunStatus.FAILED
    step = next(
        step
        for step in run.steps
        if step.kind == PublishedResourceKind.POLICY_FRAGMENT and step.stage == "policy"
    )
    assert step.status == PublishStepStatus.FAILED
    assert step.error and step.error.startswith("The Azure operation did not succeed (Failed).")
    assert f"Backend with id '{publication.backend_name}' could not be found." in step.error
    assert run.errors[0] == f"policyFragment {publication.fragment_name}: {step.error}"
    # Restoring the last safe snapshot rewrites the fragment too, and is refused the same way,
    # so the model is left denied rather than reported as restored.
    assert any("Restoring the last safe access snapshot failed" in error for error in run.errors)
    current = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert current.access_state == "failed"
    assert current.applied_access
    assert not any(grant.enabled for grant in current.applied_access.grants)
    assert harness.apim.written[fragment]["properties"]["value"] == previous
    assert NEW_USER not in previous
    api_policy = harness.apim.written[f"apis/{publication.api_name}/policies/policy"]
    assert "include-fragment" not in api_policy["properties"]["value"]
    # Keys are created on request, so the newcomer has none for the failed apply to leave behind.
    assert f"subscriptions/{harness.subscription(newcomer)}" not in harness.apim.written
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) is None


async def _limit_grants(harness: Harness) -> list[Entitlement]:
    """Grant two users and an application, two of them with their own limits."""

    users = [await harness.grant(), await harness.grant(NEW_USER)]
    application = await harness.grant(APPLICATION, application=True)
    counter = "@(context.Subscription.Id)"
    await harness.grants.update_entitlement(
        ACTOR,
        users[0].id,
        EntitlementUpdate(
            enforcement=EntitlementEnforcement(
                tokens=TokenEnforcement(
                    counter_key_expression=counter,
                    tokens_per_minute=1000,
                    token_quota=100000,
                    token_quota_period="Daily",
                )
            )
        ),
    )
    await harness.grants.update_entitlement(
        ACTOR,
        application.id,
        EntitlementUpdate(
            enforcement=EntitlementEnforcement(
                requests=RequestEnforcement(
                    counter_key_expression=counter,
                    calls=10,
                    renewal_period_seconds=60,
                    call_quota=1000,
                    call_quota_period="Weekly",
                )
            )
        ),
    )
    return [*users, application]


def _assert_governed_access_applied(
    harness: Harness, publication: Publication, grants: list[Entitlement]
) -> None:
    assert publication.status == PublicationStatus.PUBLISHED
    assert publication.access_state == "applied"
    written = harness.apim.written
    fragment = written[f"policyFragments/{publication.fragment_name}"]["properties"]["value"]
    api_policy = written[f"apis/{publication.api_name}/policies/policy"]["properties"]["value"]
    # The key and token lookups, the shape checks, the model check for the Responses route and
    # the weekly call quota's counter are each a multi-statement expression.
    markers = ("mosaic-key-grant", "mosaic-token-grant", "preserveContent: true", "quota-by-key")
    for marker in markers:
        assert marker in fragment
    assert fragment.count("@{") >= 6
    assert policy_expression_error(fragment) is None
    assert "include-fragment" in api_policy
    assert policy_expression_error(api_policy) is None
    # An apply creates no grant's key: they're created on request.
    for grant in grants:
        assert f"subscriptions/{harness.subscription(grant)}" not in written


async def test_governed_access_with_per_grant_limits_is_a_fragment_api_management_accepts(
    harness: Harness,
) -> None:
    grants = await _limit_grants(harness)
    await harness.govern()

    run = await harness.apply()

    assert run.status == PublishRunStatus.SUCCEEDED
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    _assert_governed_access_applied(harness, publication, grants)
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) is None


async def test_a_governed_fragment_api_management_cannot_parse_denies_and_can_be_planned_again(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    grants = await _limit_grants(harness)
    await harness.govern()
    before = await harness.service.get_publication(ACTOR, harness.publication_id)
    fragment = f"policyFragments/{before.fragment_name}"
    key_lookup = access_policy._key_lookup

    def unbraced_key_lookup(*args: Any) -> str:
        # The key lookup as MOSAIC used to write it: `if (...) return "...";` with no braces.
        return re.sub(r"\{ (return [^;]*;) \}", r"\1", key_lookup(*args))

    with monkeypatch.context() as patch:
        patch.setattr(access_policy, "_key_lookup", unbraced_key_lookup)
        run = await harness.apply()

    # API Management accepts the update, then fails the operation. This is its message, as the
    # run records it.
    assert run.status == PublishRunStatus.FAILED
    step = next(
        step
        for step in run.steps
        if step.kind == PublishedResourceKind.POLICY_FRAGMENT and step.stage == "policy"
    )
    assert step.status == PublishStepStatus.FAILED
    assert run.errors[0] == (
        f"policyFragment {before.fragment_name}: The Azure operation did not succeed (Failed). "
        "ValidationError: The policy fragment contains invalid policy expression. Expected a "
        '"{" but found a "return". Block statements must be enclosed in "{" and "}". You cannot '
        "use single-statement control-flow statements in CSHTML pages. For example, the "
        "following is not allowed: @if(isLoggedIn) [policy markup omitted]."
    )
    # Nothing was granted: the model is denied and every key MOSAIC owns is suspended.
    failed = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert failed.status == PublicationStatus.FAILED
    assert failed.access_state == "failed"
    assert failed.applied_access
    assert not any(grant.enabled for grant in failed.applied_access.grants)
    denied = harness.apim.written[fragment]["properties"]["value"]
    assert "return-response" in denied and "@{" not in denied
    assert (
        harness.apim.written[f"subscriptions/{failed.subscription_name}"]["properties"]["state"]
        == "suspended"
    )
    for grant in grants:
        assert f"subscriptions/{harness.subscription(grant)}" not in harness.apim.written
    assert await harness.gateways.get_publication_lock(TENANT, failed.id) is None

    run = await harness.apply()

    assert run.status == PublishRunStatus.SUCCEEDED
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    _assert_governed_access_applied(harness, publication, grants)
    assert await harness.gateways.get_publication_lock(TENANT, publication.id) is None


async def test_persistence_failure_after_activation_is_not_success(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.grant()
    await harness.govern()
    await harness.apply()
    newcomer = await harness.grant(NEW_USER)
    original = harness.gateways.record_publication_state
    failed = False

    async def fail_once(publication: Publication) -> Publication:
        nonlocal failed
        if (
            not failed
            and publication.access_state == "applied"
            and publication.applied_access
            and any(
                grant.entitlement_id == newcomer.id for grant in publication.applied_access.grants
            )
        ):
            failed = True
            raise RuntimeError("desired state storage unavailable")
        return await original(publication)

    monkeypatch.setattr(harness.gateways, "record_publication_state", fail_once)
    run = await harness.apply()
    assert run.status == PublishRunStatus.FAILED
    assert any("storage unavailable" in error for error in run.errors)
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert publication.access_state == "failed"
    assert publication.applied_access
    assert not any(
        grant.entitlement_id == newcomer.id and grant.enabled
        for grant in publication.applied_access.grants
    )
    policy = harness.apim.written[f"policyFragments/{publication.fragment_name}"]["properties"][
        "value"
    ]
    assert NEW_USER not in policy


async def test_lost_run_persistence_denies_and_retains_durable_lock(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    await harness.grant()
    await harness.govern()
    await harness.apply()
    await harness.grant(NEW_USER)
    original = harness.gateways.save_publish_run
    failed = False

    async def fail_from_activation(run: PublishRun) -> PublishRun:
        nonlocal failed
        if failed or any(step.stage == "activate" for step in run.steps):
            failed = True
            raise RuntimeError("run storage unavailable")
        return await original(run)

    monkeypatch.setattr(harness.gateways, "save_publish_run", fail_from_activation)
    plan = await harness.service.plan(ACTOR, harness.publication_id)
    run = await harness.service.apply(ACTOR, harness.publication_id, plan.id)
    with pytest.raises(RuntimeError, match="run storage unavailable"):
        await harness.service.wait_for_idle()
    assert await harness.gateways.get_publication_lock(TENANT, harness.publication_id) == run.id
    assert (await harness.service.get_run(ACTOR, run.id)).status != PublishRunStatus.SUCCEEDED
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    policy = harness.apim.written[f"apis/{publication.api_name}/policies/policy"]["properties"][
        "value"
    ]
    assert "return-response" in policy and "include-fragment" not in policy


async def test_failed_denial_is_unknown_and_cannot_be_replanned(harness: Harness) -> None:
    grant = await harness.grant()
    await harness.govern()
    await harness.apply()
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    await harness.grants.update_entitlement(ACTOR, grant.id, EntitlementUpdate(enabled=False))
    harness.apim.fail_write(f"apis/{publication.api_name}/policies/policy", 403)
    harness.apim.fail_write(f"policyFragments/{publication.fragment_name}", 403)
    run = await harness.apply()
    assert run.status == PublishRunStatus.INTERRUPTED
    current = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert current.access_state == "unknown"
    decorated = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert decorated.runtime and decorated.runtime.status == "unknown"
    assert await harness.gateways.get_publication_lock(TENANT, current.id) == run.id
    with pytest.raises(ConflictError):
        await harness.service.plan(ACTOR, current.id)


async def test_short_mutation_recovery_requires_a_quiesced_exact_owner(harness: Harness) -> None:
    async with publication_lock(harness.gateways, TENANT, harness.publication_id) as owner:
        assert owner
        assert await harness.service.get_lock_owner(ACTOR, harness.publication_id) == owner
        diagnostic = await harness.service.recover_interrupted(
            ACTOR, harness.publication_id, run_id=owner
        )
        assert diagnostic.status == PublishRunStatus.INTERRUPTED
        assert any("liveness is unknown" in error for error in diagnostic.errors)
        with pytest.raises(ConflictError) as blocked:
            await harness.service.update(
                ACTOR, harness.publication_id, PublicationUpdate(display_name="Concurrent edit")
            )
        assert blocked.value.details["lockOwner"] == owner
        with pytest.raises(ConflictError, match="active desired-state mutation"):
            await harness.service.recover_interrupted(
                ACTOR, harness.publication_id, run_id=owner, confirm_quiesced=True
            )
    assert await harness.service.get_lock_owner(ACTOR, harness.publication_id) is None
    owner = "mutation_interrupted"
    await harness.gateways.acquire_publication_lock(TENANT, harness.publication_id, owner)
    harness.apim.writes.clear()
    await harness.service.recover_interrupted(
        ACTOR, harness.publication_id, run_id=owner, confirm_quiesced=True
    )
    assert await harness.gateways.get_publication_lock(TENANT, harness.publication_id) is None
    assert not harness.apim.writes


async def test_gateway_admission_lock_cannot_expire_into_publication_creation(
    harness: Harness,
) -> None:
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    scope = gateway_mutation_scope(publication.gateway_id)
    owner = "mutation_gateway_interrupted"
    await harness.gateways.acquire_publication_lock(TENANT, scope, owner)
    with pytest.raises(ConflictError, match="already running"):
        await harness.service.create(
            ACTOR,
            PublicationCreate(
                gateway_id=publication.gateway_id,
                model_endpoint_id=publication.model_endpoint_id,
                deployment_name="text-embedding-3-large",
                enforcement=publication.enforcement,
            ),
        )
    with pytest.raises(ConflictError, match="already running"):
        await harness.gateway_service.delete(ACTOR, publication.gateway_id)
    await harness.service.recover_interrupted(
        ACTOR, scope, run_id=owner, confirm_quiesced=True
    )
    assert await harness.gateways.get_publication_lock(TENANT, scope) is None


async def test_governed_apply_reviews_and_resets_custom_key_parameter_names(
    harness: Harness,
) -> None:
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    api_path = f"apis/{publication.api_name}"
    # Legacy publication writes continue leaving the customer's parameter-name settings alone.
    assert "subscriptionKeyParameterNames" not in harness.apim.written[api_path]["properties"]
    harness.apim.written[api_path]["properties"]["subscriptionKeyParameterNames"] = {
        "header": "custom-key",
        "query": "custom-query-key",
    }
    grant = await harness.grant()
    await harness.govern()
    plan = await harness.service.plan(ACTOR, publication.id)
    assert any(
        "customized subscription key names will be reset" in warning for warning in plan.warnings
    )
    assert any("reset key parameter names" in step.reason for step in plan.steps)
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    expected = {"header": "Ocp-Apim-Subscription-Key", "query": "subscription-key"}
    assert harness.apim.written[api_path]["properties"]["subscriptionKeyParameterNames"] == expected
    await harness.key(grant)
    harness.apim.fail_activation = f"subscriptions/{harness.subscription(grant)}"
    assert (await harness.apply()).status == PublishRunStatus.FAILED
    assert harness.apim.written[api_path]["properties"]["subscriptionKeyParameterNames"] == expected


@pytest.mark.parametrize("relative", [False, True])
async def test_subscription_scope_accepts_only_the_expected_api(
    harness: Harness, relative: bool
) -> None:
    grant = await harness.grant()
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    await harness.key(grant)
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    name = harness.subscription(grant)
    scope = f"/apis/{publication.api_name}"
    harness.apim.written[f"subscriptions/{name}"]["properties"]["scope"] = (
        scope if relative else f"{RESOURCE_ID}{scope}"
    )
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED


@pytest.mark.parametrize("wrong_scope", ["product", "allApis", "otherApi", "otherService"])
async def test_subscription_scope_rejects_broader_and_foreign_scopes(
    harness: Harness, wrong_scope: str
) -> None:
    grant = await harness.grant()
    await harness.govern()
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
    await harness.key(grant)
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    name = harness.subscription(grant)
    scope = {
        "product": f"/products/{publication.product_name}",
        "allApis": "/apis",
        "otherApi": f"/apis/{publication.api_name}-other",
        "otherService": (
            RESOURCE_ID.replace("apim-contoso-dev", "apim-other")
            + f"/apis/{publication.api_name}"
        ),
    }[wrong_scope]
    harness.apim.written[f"subscriptions/{name}"]["properties"]["scope"] = scope
    harness.apim.writes.clear()
    with pytest.raises(ConflictError, match="live scope changed"):
        await harness.service.plan(ACTOR, harness.publication_id)
    assert not harness.apim.writes


async def test_opt_in_reviews_legacy_counter_mapping_without_rewriting_saved_intent(
    harness: Harness,
) -> None:
    grant = await harness.grant()
    legacy_counter = "@(context.Subscription?.Key)"
    await harness.grants.update_entitlement(
        ACTOR,
        grant.id,
        EntitlementUpdate(
            enforcement=EntitlementEnforcement(
                requests=RequestEnforcement(
                    counter_key_expression=legacy_counter,
                    calls=10,
                    renewal_period_seconds=60,
                )
            )
        ),
    )
    await harness.govern()
    harness.apim.writes.clear()
    plan = await harness.service.plan(ACTOR, harness.publication_id)
    assert any(
        "Legacy subscription ID/key counter defaults" in detail
        for facet in plan.facets for detail in facet.details
    )
    assert not harness.apim.writes
    run = await harness.service.apply(ACTOR, harness.publication_id, plan.id)
    await harness.service.wait_for_idle()
    assert (await harness.service.get_run(ACTOR, run.id)).status == PublishRunStatus.SUCCEEDED
    stored = await harness.entitlements.get_entitlement(TENANT, grant.id)
    assert stored and stored.enforcement and stored.enforcement.requests
    assert stored.enforcement.requests.counter_key_expression == legacy_counter
    assert await harness.service.get_lock_owner(ACTOR, harness.publication_id) is None


async def test_governed_claude_on_a_classic_tier_keeps_call_limits_and_drops_token_grants() -> None:
    cognitive = FakeCognitiveServices(kind="AIServices")
    cognitive.deployments = [
        {
            "name": "claude-sonnet-4-5",
            "sku": {"name": "GlobalStandard", "capacity": 1},
            "properties": {
                "model": {"format": "Anthropic", "name": "claude-sonnet-4-5", "version": "1"},
                "provisioningState": "Succeeded",
                "capabilities": {"chatCompletion": "true"},
            },
        }
    ]
    harness = Harness(cognitive)
    try:
        # The APIM double reports the Developer tier, where Anthropic can't be token-metered.
        await harness.setup(deployment="claude-sonnet-4-5", enforcement=None)
        token_limited = await harness.grant()
        await harness.grants.update_entitlement(
            ACTOR,
            token_limited.id,
            EntitlementUpdate(
                enforcement=EntitlementEnforcement(
                    tokens=TokenEnforcement(
                        counter_key_expression="@(context.Subscription.Id)",
                        tokens_per_minute=100,
                    )
                )
            ),
        )
        call_limited = await harness.grant(APPLICATION, application=True)
        await harness.grants.update_entitlement(
            ACTOR,
            call_limited.id,
            EntitlementUpdate(
                enforcement=EntitlementEnforcement(
                    requests=RequestEnforcement(
                        counter_key_expression="@(context.Subscription.Id)",
                        calls=10,
                        renewal_period_seconds=60,
                    )
                )
            ),
        )
        await harness.govern()

        plan = await harness.service.plan(ACTOR, harness.publication_id)

        assert plan.access_snapshot
        assert plan.access_snapshot.publication_enforcement is None
        assert [grant.entitlement_id for grant in plan.access_snapshot.grants] == [
            call_limited.id
        ]
        assert any(
            token_limited.id in warning and "call limits" in warning for warning in plan.warnings
        )
        assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED
        publication = await harness.service.get_publication(ACTOR, harness.publication_id)
        fragment = harness.apim.written[f"policyFragments/{publication.fragment_name}"][
            "properties"
        ]["value"]
        assert "rate-limit-by-key" in fragment
        assert "llm-token-limit" not in fragment
        assert "llm-emit-token-metric" not in fragment
        assert 'resource="https://ai.azure.com"' in fragment
    finally:
        await harness.close()


async def test_a_pending_recheck_keeps_its_grants_out_of_every_apply(harness: Harness) -> None:
    """Until MOSAIC has checked that a grant's subject may still charge its cost center, applies
    leave the grant out, so no apply gives back access a change took away. See ADR 0021."""

    kept = await harness.grant()
    waiting = await harness.grant(APPLICATION, application=True)
    await harness.govern()
    general = general_cost_center_id(TENANT)
    await harness.grants.request_recheck(
        ACTOR, general, reason="memberRemoved", subject_id=waiting.subject.id
    )

    plan = await harness.service.plan(ACTOR, harness.publication_id)
    assert plan.access_snapshot
    assert {g.entitlement_id for g in plan.access_snapshot.grants if g.enabled} == {kept.id}
    assert any(
        waiting.id in warning and "waiting for MOSAIC to check" in warning
        for warning in plan.warnings
    )

    # General is the tenant's default, which everyone may charge, so the recheck keeps the grant.
    assert await harness.grants.resume_rechecks(ACTOR) == []
    settled = await harness.cost_center_records.get_cost_center(TENANT, general)
    assert settled is not None and settled.pending_rechecks == []
    plan = await harness.service.plan(ACTOR, harness.publication_id)
    assert plan.access_snapshot
    assert {g.entitlement_id for g in plan.access_snapshot.grants if g.enabled} == {
        kept.id,
        waiting.id,
    }
