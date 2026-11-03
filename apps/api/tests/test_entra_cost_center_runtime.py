"""Entra-backed center service contracts; fictional directory/APIM, not live conformance."""

import hashlib
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from conftest import build_mcp_publishing_service, build_model_pool_service
from mosaic_api.cost_center_directory import EntraGroupBinding
from mosaic_api.cost_centers import (
    CostCenter,
    CostCenterBook,
    CostCenterLimit,
    CostCenterMember,
    CostCenterResourceAccess,
    CostCenterResourceAccessMode,
    CostCenterSettings,
    PendingRecheck,
    PersonLimits,
    cost_center_settings_id,
    may_charge,
)
from mosaic_api.domain import (
    AccessRequestApproval,
    AccessRequestCreate,
    AuditEvent,
    DirectoryObject,
    Entitlement,
    EntitlementCreate,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    EntitlementUpdate,
    McpAuthMode,
    McpEndpoint,
    McpServer,
    ModelAccessSettings,
    Principal,
    PrincipalKind,
    PublicationStatus,
    PublishedResource,
    RequestEnforcement,
    general_cost_center_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, DirectoryError, NotFoundError, ValidationError
from mosaic_api.integrations.access_policy import render_governed_policy
from mosaic_api.integrations.graph import FakeDirectoryLookup, GroupMembershipEvidence
from mosaic_api.integrations.mcp_access_policy import render_mcp_policy
from mosaic_api.integrations.pool_policy import render_governed_pool_policy
from mosaic_api.model_pools import ModelPool, PoolMember, PoolModel
from mosaic_api.repositories import InMemoryMcpEndpointRepository
from mosaic_api.services.automatic_keys import reconcile_initial_keys
from mosaic_api.services.cost_center_access import verified_cost_center_members
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest
from mosaic_api.services.model_pools import _routes
from mosaic_api.services.portal import PortalService
from test_governed_lifecycle import ACTOR, APPLICATION, TENANT, USER, Harness
from test_mcp_access_policy import _publication as mcp_publication

GROUP = "44444444-4444-4444-4444-444444444444"
OTHER_GROUP = "55555555-5555-5555-5555-555555555555"
CENTER = "costCenter-research"
CALLER = Actor(USER, TENANT, group_ids=frozenset({GROUP}))


def audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="test",
        resource_id="fixture",
        actor_object_id=ACTOR.object_id,
    )


class EvidenceLookup(FakeDirectoryLookup):
    def __init__(self) -> None:
        super().__init__(
            [
                DirectoryObject(object_id=GROUP, kind="securityGroup", display_name="Research"),
                DirectoryObject(object_id=USER, kind="user", display_name="Ada"),
                DirectoryObject(
                    object_id=APPLICATION, kind="servicePrincipal", display_name="Research app"
                ),
            ],
            {GROUP: [USER, APPLICATION]},
            tenant_id=TENANT,
        )
        self.calls: list[tuple[str, tuple[str, ...]]] = []
        self.bad_evidence: str | None = None

    async def verify_group_members(
        self,
        group_object_id: str,
        candidate_object_ids: Sequence[str],
        *,
        timeout_seconds: float = 60,
    ) -> GroupMembershipEvidence:
        self.calls.append((group_object_id, tuple(candidate_object_ids)))
        if self.bad_evidence == "failure":
            raise DirectoryError("Fictional Graph is unavailable")
        evidence = await super().verify_group_members(
            group_object_id, candidate_object_ids, timeout_seconds=timeout_seconds
        )
        if self.bad_evidence == "tenant":
            return replace(evidence, tenant_id=OTHER_GROUP)
        if self.bad_evidence == "group":
            return replace(evidence, group_object_id=OTHER_GROUP)
        if self.bad_evidence == "partial":
            return replace(evidence, candidate_object_ids=frozenset())
        if self.bad_evidence == "extra":
            return replace(evidence, member_object_ids=evidence.member_object_ids | {OTHER_GROUP})
        if self.bad_evidence == "stale":
            return replace(evidence, checked_at=utc_now() - timedelta(minutes=6))
        if self.bad_evidence == "future":
            return replace(evidence, checked_at=utc_now() + timedelta(minutes=6))
        if self.bad_evidence == "naive":
            return replace(evidence, checked_at=utc_now().replace(tzinfo=None))
        if self.bad_evidence == "missing":
            return None  # type: ignore[return-value]
        return evidence


class Estate(Harness):
    def __init__(self) -> None:
        super().__init__()
        self.graph = EvidenceLookup()
        self.grants = EntitlementService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateways,
            endpoint_repository=self.endpoints,
            cost_center_repository=self.cost_center_records,
            directory_lookup=self.graph,
        )
        self.mcps = build_mcp_publishing_service(
            self.apim,
            self.gateways,
            InMemoryMcpEndpointRepository(),
            directory_repository=self.directory,
            entitlement_repository=self.entitlements,
            cost_center_repository=self.cost_center_records,
        )
        self.pools = build_model_pool_service(
            self.apim,
            self.gateways,
            self.endpoints,
            directory_repository=self.directory,
            entitlement_repository=self.entitlements,
            cost_center_repository=self.cost_center_records,
        )
        self.mcp_endpoint = McpEndpoint(
            id="mcp-endpoint",
            tenant_id=TENANT,
            name="Research tools",
            endpoint="https://fictional.example.test/mcp",
        )
        self.portal = PortalService(
            self.grants, directory_repository=self.directory, gateway_repository=self.gateways
        )

    async def setup_all(self) -> None:
        await super().setup()
        await self.govern()
        await self.apply()
        publication = await self.service.get_publication(ACTOR, self.publication_id)
        self.gateway_id = publication.gateway_id
        self.mcp = mcp_publication(
            tenant_id=TENANT,
            gateway_id=self.gateway_id,
            status="published",
            resources=[
                PublishedResource(
                    kind="api",
                    name="mosaic-mcp-weather",
                    resource_id="/fictional/mcp",
                    created_by_mosaic=True,
                )
            ],
        )
        await self.gateways.save_mcp_publication(self.mcp, audit())
        await self.gateways.save_mcp_server(
            McpServer(
                id=self.mcp.mcp_server_id,
                tenant_id=TENANT,
                gateway_id=self.gateway_id,
                api_name=self.mcp.api_name,
                path=self.mcp.api_path,
                display_name="Research tools",
                publication_id=self.mcp.id,
            ),
            audit(),
        )
        self.pool = ModelPool(
            id="pool-research",
            tenant_id=TENANT,
            gateway_id=self.gateway_id,
            display_name="Research models",
            api_name="mosaic-pool-research",
            api_path="mosaic/pool-research",
            fragment_name="mosaic-pool-research",
            product_name="mosaic-pool-research",
            subscription_name="mosaic-pool-research",
            api_shape=publication.api_shape,
            governed_access=ModelAccessSettings(),
            models=[
                PoolModel(
                    id="pool-model",
                    public_name="research",
                    display_name="Research model",
                    backend_pool_name="research-backends",
                    members=[
                        PoolMember(
                            model_endpoint_id=publication.model_endpoint_id,
                            deployment_name=publication.deployment_name,
                            backend_name="research-backend",
                        )
                    ],
                )
            ],
            applied_model_ids=["pool-model"],
            resources=[
                PublishedResource(
                    kind="api",
                    name="mosaic-pool-research",
                    resource_id="/fictional/pool",
                    created_by_mosaic=True,
                )
            ],
            status="published",
        )
        await self.gateways.save_model_pool(self.pool, audit())
        self.resources = [
            EntitlementResource(kind="modelApi", id=self.model_id),
            EntitlementResource(kind="mcpServer", id=self.mcp.mcp_server_id),
            EntitlementResource(kind="poolModel", id="pool-model", scope_id=self.pool.id),
        ]
        self.person = await self.directory.create_principal(
            Principal(id="person", tenant_id=TENANT, object_id=USER, kind="user", label="Ada"),
            audit(),
        )
        self.application = await self.directory.create_principal(
            Principal(
                id="application",
                tenant_id=TENANT,
                object_id=APPLICATION,
                kind="servicePrincipal",
                label="Research app",
            ),
            audit(),
        )

    async def center(self, mode: str = "automatic", **overrides: Any) -> CostCenter:
        return await self.cost_center_records.create_cost_center(
            CostCenter(
                id=CENTER,
                tenant_id=TENANT,
                name="Research",
                code="RES",
                entra_binding=EntraGroupBinding(
                    directory_tenant_id=TENANT,
                    group_object_id=GROUP,
                    provenance="attached",
                    verification_state="verified",
                    verified_at=utc_now(),
                ),
                resource_access=[
                    CostCenterResourceAccess(resource=resource, mode=mode)
                    for resource in self.resources
                ],
                **overrides,
            ),
            audit(),
        )

    async def change_center(self, **changes: Any) -> CostCenter:
        current = await self.cost_center_records.get_cost_center(TENANT, CENTER)
        assert current is not None
        return await self.cost_center_records.save_cost_center(
            current.model_copy(update=changes), audit()
        )

    async def direct(
        self, resource: EntitlementResource, *, application: bool = False
    ) -> Entitlement:
        return await self.grants.create_entitlement(
            ACTOR,
            EntitlementCreate(
                subject=EntitlementSubject(
                    kind="application" if application else "user",
                    id=self.application.id if application else self.person.id,
                ),
                resource=resource,
                cost_center_id=CENTER,
            ),
        )

    async def snapshots(self) -> list[Any]:
        publication = await self.service.get_publication(ACTOR, self.publication_id)
        gateway = await self.gateways.get_gateway(TENANT, self.gateway_id)
        assert gateway is not None
        model, _ = await self.service._access_snapshot(publication, gateway)
        mcp, _ = await self.mcps._access_snapshot(self.mcp, self.mcp_endpoint)
        pool = await self.gateways.get_model_pool(TENANT, self.pool.id)
        assert pool is not None
        pooled, _ = await self.pools._access_snapshot(pool, gateway, str(pool.api_shape))
        return [model, mcp, pooled]

    async def fragments(self) -> list[str]:
        model, mcp, pooled = await self.snapshots()
        publication = await self.service.get_publication(ACTOR, self.publication_id)
        return [
            render_governed_policy(publication, model).fragment_xml,
            render_mcp_policy(
                self.mcp, mcp, backend_auth=McpAuthMode.NONE, backend_audience=None
            ).fragment_xml,
            render_governed_pool_policy(
                pool=self.pool,
                snapshot=pooled,
                shape=str(self.pool.api_shape),
                routes=_routes(self.pool),
                preset=self.pool.breaker_preset,
                safeguard=self.pool.safeguard,
            ).fragment_xml,
        ]

    async def apply_pool_grants(self) -> None:
        snapshot = (await self.snapshots())[2]
        await self.gateways.record_model_pool_state(
            self.pool.model_copy(update={"applied_access": snapshot, "access_state": "applied"})
        )


@pytest.fixture
async def estate() -> AsyncIterator[Estate]:
    built = Estate()
    await built.setup_all()
    yield built
    await built.mcps.aclose()
    await built.pools.aclose()
    await built.close()


async def test_automatic_grants_scale_with_exact_resources_not_members(estate: Estate) -> None:
    await estate.center()
    estate.graph.state.group_members[GROUP].extend(
        f"{number:08x}-9999-9999-9999-999999999999" for number in range(1, 2001)
    )
    before_writes = list(estate.apim.payloads)
    grants = await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    assert len(grants) == 3
    assert all(item.subject.kind == "securityGroup" for item in grants)
    assert all(item.managed_by == "costCenterAutomatic" for item in grants)
    assert (
        len({(item.resource.kind, item.resource.scope_id, item.resource.id) for item in grants})
        == 3
    )
    assert not estate.directory.groups
    assert not estate.graph.calls
    assert estate.apim.payloads == before_writes
    first = await estate.fragments()
    digests = [hashlib.sha256(value.encode()).hexdigest() for value in first]

    estate.graph.state.group_members[GROUP].remove(USER)
    await estate.grants.member_removed_from_cost_center(ACTOR, CENTER, USER, PrincipalKind.USER)
    await estate.change_center(
        membership_revision=10, pending_rechecks=[PendingRecheck(reason="memberRemoved")]
    )
    again = await estate.grants.sync_cost_center_access(ACTOR, CENTER)

    assert [item.id for item in again] == [item.id for item in grants]
    assert all(item.enabled for item in again)
    assert await estate.fragments() == first
    assert [hashlib.sha256(value.encode()).hexdigest() for value in first] == digests
    assert not estate.graph.calls
    assert estate.apim.payloads == before_writes


async def test_sync_owns_only_marked_grants_and_disables_retired_automatic_access(
    estate: Estate,
) -> None:
    await estate.center()
    automatic = await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    unmanaged = automatic[0].model_copy(update={"id": "unrelated", "managed_by": None})
    await estate.entitlements.create_entitlement(unmanaged, audit())
    direct = await estate.direct(estate.resources[0])
    await estate.change_center(
        resource_access=[
            CostCenterResourceAccess(resource=resource, mode="requestOnly")
            for resource in estate.resources
        ]
    )

    retired = await estate.grants.sync_cost_center_access(ACTOR, CENTER)

    assert len(retired) == 3 and not any(item.enabled for item in retired)
    assert (await estate.entitlements.get_entitlement(TENANT, unmanaged.id)).enabled
    assert (await estate.entitlements.get_entitlement(TENANT, direct.id)).enabled
    with pytest.raises(ConflictError, match="Automatic access"):
        await estate.grants.update_entitlement(
            ACTOR, automatic[0].id, EntitlementUpdate(enabled=True)
        )


@pytest.mark.parametrize("index", range(3), ids=["model", "mcp", "pool"])
@pytest.mark.parametrize("application", [False, True], ids=["user", "application"])
async def test_all_individual_token_grants_compile_the_primary_group_requirement(
    estate: Estate, index: int, application: bool
) -> None:
    await estate.center("requestOnly")
    grant = await estate.direct(estate.resources[index], application=application)
    snapshot = (await estate.snapshots())[index]
    [compiled] = snapshot.grants
    assert compiled.entitlement_id == grant.id
    assert compiled.required_cost_center_group_id == GROUP
    fragment = ET.fromstring((await estate.fragments())[index])
    lookup = [
        item.attrib["value"]
        for item in fragment.iter("set-variable")
        if item.attrib.get("name") == "mosaic-token-match"
    ][-1]
    assert "costCenterMember0" in lookup
    assert f'String.Equals(centerGroup, "{GROUP}",' in lookup
    expected = "application" if application else "delegated"
    assert f"if ({expected} && costCenterMember0 && String.Equals(oid," in lookup
    assert fragment.find(".//send-request") is None

    old_intent = cost_center_intent(
        grant,
        estate.application if application else estate.person,
        await estate.grants.cost_center_book(TENANT),
    )
    assert old_intent is not None
    assert old_intent.payload()["requiredCostCenterGroupId"] == GROUP
    changed = replace(old_intent, required_cost_center_group_id=OTHER_GROUP)
    principal = estate.application if application else estate.person
    assert entitlement_intent_digest(grant, principal, old_intent) != entitlement_intent_digest(
        grant, principal, changed
    )


@pytest.mark.parametrize("index", range(3), ids=["model", "mcp", "pool"])
async def test_requests_and_approvals_use_only_request_only_offerings(
    estate: Estate, index: int
) -> None:
    await estate.center()
    resource = estate.resources[index]
    request = AccessRequestCreate(resource=resource, cost_center_id=CENTER)
    with pytest.raises(ValidationError, match="Request-only"):
        await estate.grants.create_catalog_access_request(CALLER, request)
    await estate.change_center(
        resource_access=[CostCenterResourceAccess(resource=resource, mode="requestOnly")]
    )
    created = await estate.grants.create_catalog_access_request(CALLER, request)
    approved = await estate.grants.approve_access_request(
        ACTOR, created.id, AccessRequestApproval()
    )
    grant = await estate.grants.get_entitlement(ACTOR, approved.granted_entitlement_id)
    assert grant.subject.kind == "user" and grant.managed_by is None
    assert grant.runtime is not None and grant.runtime.status == "pending"
    assert (await estate.snapshots())[index].grants[0].required_cost_center_group_id == GROUP


@pytest.mark.parametrize("index", range(3), ids=["model", "mcp", "pool"])
async def test_not_available_cannot_be_bypassed_by_grant_request_approval_or_recheck(
    estate: Estate, index: int
) -> None:
    await estate.center("requestOnly")
    resource = estate.resources[index]
    created = await estate.grants.create_catalog_access_request(
        CALLER, AccessRequestCreate(resource=resource, cost_center_id=CENTER)
    )
    await estate.change_center(resource_access=[])
    with pytest.raises(ValidationError, match="Not available"):
        await estate.direct(resource)
    with pytest.raises(ValidationError, match="Not available"):
        await estate.grants.approve_access_request(ACTOR, created.id, AccessRequestApproval())
    preview = await estate.grants.preview_access_request(ACTOR, created.id, automation=True)
    assert "resourceNotAvailable" in {item.code for item in preview.blockers}
    assert not await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    assert not (await estate.snapshots())[index].grants
    assert not await estate.entitlements.list_entitlements(TENANT)


@pytest.mark.parametrize("index", range(3), ids=["model", "mcp", "pool"])
async def test_rejected_auth_settings_cannot_be_bypassed_by_individual_approval(
    estate: Estate, index: int
) -> None:
    await estate.center("requestOnly")
    resource = estate.resources[index]
    request = AccessRequestCreate(resource=resource, cost_center_id=CENTER)
    created = await estate.grants.create_catalog_access_request(CALLER, request)
    if index == 0:
        await estate.govern(keys_enabled=False, entra_enabled=False)
    elif index == 1:
        estate.grants._entra_group_claims_enabled = False
    else:
        await estate.gateways.save_model_pool(
            estate.pool.model_copy(
                update={
                    "governed_access": ModelAccessSettings(keys_enabled=False, entra_enabled=False)
                }
            ),
            audit(),
        )
    for action in (
        estate.direct(resource),
        estate.grants.create_catalog_access_request(CALLER, request),
        estate.grants.approve_access_request(ACTOR, created.id, AccessRequestApproval()),
    ):
        with pytest.raises(ValidationError, match="No supported credential"):
            await action
    assert not await estate.entitlements.list_entitlements(TENANT)


async def test_setup_required_drafts_validate_but_never_grant_access(estate: Estate) -> None:
    draft = CostCenter(
        id=CENTER,
        tenant_id=TENANT,
        name="Research",
        code="RES",
        resource_access=[
            CostCenterResourceAccess(resource=resource, mode="automatic")
            for resource in estate.resources
        ],
    )
    await estate.grants.validate_cost_center_access(ACTOR, draft)
    await estate.cost_center_records.create_cost_center(draft, audit())
    assert not await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    with pytest.raises(ValidationError, match="verified Entra group"):
        await estate.direct(estate.resources[0])
    assert all(not snapshot.grants for snapshot in await estate.snapshots())


async def test_pool_model_resource_mode_includes_its_exact_pool_scope(estate: Estate) -> None:
    await estate.center()
    await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    other = estate.pool.model_copy(update={"id": "other-pool"})
    await estate.gateways.save_model_pool(other, audit())
    same_id_elsewhere = EntitlementResource(
        kind="poolModel", id=estate.resources[2].id, scope_id=other.id
    )
    with pytest.raises(ValidationError, match="Not available"):
        await estate.direct(same_id_elsewhere)
    center = (await estate.grants.cost_center_book(TENANT)).get(CENTER)
    assert center.access_mode_for(same_id_elsewhere) == "notAvailable"
    grants = await estate.entitlements.list_entitlements(TENANT)
    assert len(grants) == 3
    assert [item.resource.scope_id for item in grants if item.resource.kind == "poolModel"] == [
        estate.pool.id
    ]


async def test_available_automatic_resources_keep_explicit_individual_quota_exceptions(
    estate: Estate,
) -> None:
    resource = estate.resources[0]
    inherited = PersonLimits(calls_per_minute=100)
    await estate.center(limits=[CostCenterLimit(resource=resource, person=inherited)])
    await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    exception = EntitlementEnforcement(
        requests=RequestEnforcement(
            counter_key_expression="@(context.Subscription.Id)", calls=2, renewal_period_seconds=60
        )
    )
    direct = await estate.grants.create_entitlement(
        ACTOR,
        EntitlementCreate(
            subject=EntitlementSubject(kind="user", id=estate.person.id),
            resource=resource,
            cost_center_id=CENTER,
            enforcement=exception,
        ),
    )
    snapshot = (await estate.snapshots())[0]
    compiled = {item.entitlement_id: item for item in snapshot.grants}
    assert compiled[direct.id].enforcement == exception
    [group] = [item for item in snapshot.grants if item.is_group_grant]
    assert group.enforcement == inherited.enforcement()
    resolved = await estate.portal.my_entitlements(CALLER)
    [winner] = [item for item in resolved if item.entitlement.resource == resource]
    assert winner.entitlement.id == direct.id
    assert winner.access_origin == "approvedIndividual"
    assert all(item.required_cost_center_group_id == GROUP for item in snapshot.grants)


async def test_automatic_requires_caller_entra_and_group_claim_support_not_backend_auth(
    estate: Estate,
) -> None:
    center = await estate.center()
    await estate.govern(keys_enabled=True, entra_enabled=False)
    with pytest.raises(ValidationError, match="caller-side Entra"):
        await estate.grants.validate_cost_center_access(ACTOR, center)
    assert not await estate.entitlements.list_entitlements(TENANT)
    await estate.govern(keys_enabled=True, entra_enabled=True)
    estate.grants._entra_group_claims_enabled = False
    with pytest.raises(ValidationError, match="group claims"):
        await estate.grants.validate_cost_center_access(ACTOR, center)
    estate.grants._entra_group_claims_enabled = True
    # Resource caller auth is authoritative; changing backend auth isn't part of this hook.
    await estate.grants.validate_cost_center_access(ACTOR, center)
    await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    estate.service._security_group_claims = False
    estate.mcps._security_group_claims = False
    estate.pools._security_group_claims = False
    assert all(not snapshot.grants for snapshot in await estate.snapshots())


async def test_defaults_and_legacy_rosters_never_make_custom_centers_chargeable(
    estate: Estate,
) -> None:
    center = await estate.center("requestOnly")
    principal = estate.person.model_copy(update={"default_cost_center_id": CENTER})
    settings = CostCenterSettings(
        id=cost_center_settings_id(TENANT), tenant_id=TENANT, default_cost_center_id=CENTER
    )
    assert not may_charge(center, principal=principal, settings=settings)
    assert not may_charge(
        center, principal=principal, settings=settings, group_principal_ids=[GROUP]
    )
    assert may_charge(center, principal=principal, settings=settings, group_object_ids=[GROUP])
    legacy = CostCenter(
        id="legacy",
        tenant_id=TENANT,
        name="Legacy",
        code="OLD",
        members=[CostCenterMember(principal_id=principal.id)],
        group_id="local-group",
    )
    assert not may_charge(legacy, principal=principal, settings=settings)
    await estate.directory.save_principal(principal, audit())
    with pytest.raises(ValidationError, match="can't charge"):
        await estate.grants.create_access_request(
            Actor(USER, TENANT), AccessRequestCreate(resource=estate.resources[0])
        )
    profile = await estate.portal.profile(Actor(USER, TENANT), roles=[], is_admin=False)
    assert profile.default_cost_center.id == CENTER
    assert not profile.default_cost_center_eligible


@pytest.mark.parametrize(
    "bad", ["tenant", "group", "partial", "extra", "stale", "future", "naive", "missing", "failure"]
)
async def test_incomplete_or_stale_graph_evidence_never_authorizes_new_individual_keys(
    estate: Estate, bad: str
) -> None:
    center = await estate.center("requestOnly")
    grant = await estate.direct(estate.resources[0])
    await estate.apply()
    before_writes = list(estate.apim.payloads)
    estate.graph.bad_evidence = bad
    with pytest.raises(DirectoryError):
        await estate.keys().create_key(CALLER, grant.id)
    with pytest.raises(DirectoryError):
        await verified_cost_center_members(center, [estate.person], estate.graph)
    assert estate.apim.payloads == before_writes


@pytest.mark.parametrize("index", [0, 2], ids=["model", "pool"])
async def test_every_supported_individual_key_surface_requires_current_membership(
    estate: Estate, index: int
) -> None:
    await estate.center("requestOnly")
    grant = await estate.direct(estate.resources[index])
    if index == 0:
        await estate.apply()
    else:
        await estate.apply_pool_grants()
    key = await estate.keys().create_key(CALLER, grant.id)
    assert key.exists
    writes = list(estate.apim.payloads)
    estate.graph.bad_evidence = "partial"
    for action in (
        estate.keys().create_key(CALLER, grant.id),
        estate.keys().rotate_key(CALLER, grant.id, "primary"),
        estate.keys().reveal_key(CALLER, grant.id, "primary"),
    ):
        with pytest.raises(DirectoryError):
            await action
    assert estate.apim.payloads == writes
    # Deletion remains available even when directory verification is broken.
    assert not (await estate.keys().delete_key(CALLER, grant.id)).exists


async def test_request_only_individual_keys_work_without_caller_entra_enabled(
    estate: Estate,
) -> None:
    await estate.center("requestOnly")
    await estate.govern(keys_enabled=True, entra_enabled=False)
    estate.grants._entra_group_claims_enabled = False
    grant = await estate.direct(estate.resources[0])
    await estate.apply()
    snapshot = (await estate.snapshots())[0]
    assert not snapshot.settings.entra_enabled
    assert snapshot.grants[0].required_cost_center_group_id == GROUP
    assert (await estate.keys().create_key(CALLER, grant.id)).exists
    fragment = (await estate.fragments())[0]
    assert "costCenterMember" not in fragment
    assert "mosaic-key-match" in fragment


@pytest.mark.parametrize("index", [0, 2], ids=["model", "pool"])
async def test_key_routes_validate_applied_primary_group_requirement(
    estate: Estate, index: int
) -> None:
    await estate.center("requestOnly")
    grant = await estate.direct(estate.resources[index])
    if index == 0:
        await estate.apply()
        owner = await estate.service.get_publication(ACTOR, estate.publication_id)
        snapshot = owner.applied_access
        await estate.gateways.record_publication_state(
            owner.model_copy(
                update={
                    "applied_access": snapshot.model_copy(
                        update={
                            "grants": [
                                item.model_copy(update={"required_cost_center_group_id": None})
                                for item in snapshot.grants
                            ]
                        }
                    )
                }
            )
        )
    else:
        await estate.apply_pool_grants()
        owner = await estate.gateways.get_model_pool(TENANT, estate.pool.id)
        snapshot = owner.applied_access
        await estate.gateways.record_model_pool_state(
            owner.model_copy(
                update={
                    "applied_access": snapshot.model_copy(
                        update={
                            "grants": [
                                item.model_copy(update={"required_cost_center_group_id": None})
                                for item in snapshot.grants
                            ]
                        }
                    )
                }
            )
        )
    with pytest.raises(ConflictError, match=r"[Pp]ending.*changes"):
        await estate.keys().create_key(CALLER, grant.id)


async def test_mcp_credential_surfaces_never_invent_unsupported_keys(estate: Estate) -> None:
    await estate.center("requestOnly")
    grant = await estate.direct(estate.resources[1])
    for action in (
        estate.keys().create_key(CALLER, grant.id),
        estate.keys().rotate_key(CALLER, grant.id, "primary"),
        estate.keys().reveal_key(CALLER, grant.id, "primary"),
    ):
        with pytest.raises(ConflictError, match="Entra tokens only"):
            await action


async def test_pool_key_cannot_unlock_a_sibling_model_with_pending_not_available_mode(
    estate: Estate,
) -> None:
    second = estate.pool.models[0].model_copy(
        update={"id": "second-model", "public_name": "second", "display_name": "Second"}
    )
    estate.pool = estate.pool.model_copy(
        update={
            "models": [*estate.pool.models, second],
            "applied_model_ids": ["pool-model", second.id],
        }
    )
    await estate.gateways.save_model_pool(estate.pool, audit())
    await estate.center("requestOnly")
    resource = EntitlementResource(kind="poolModel", id=second.id, scope_id=estate.pool.id)
    await estate.change_center(
        resource_access=[
            CostCenterResourceAccess(resource=offering, mode="requestOnly")
            for offering in [*estate.resources, resource]
        ]
    )
    first = await estate.direct(estate.resources[2])
    await estate.direct(resource)
    await estate.apply_pool_grants()
    await estate.change_center(
        resource_access=[CostCenterResourceAccess(resource=estate.resources[2], mode="requestOnly")]
    )
    with pytest.raises(ConflictError, match="sharing this key"):
        await estate.keys().create_key(CALLER, first.id)


async def test_no_graph_configuration_or_stale_request_claims_can_approve_custom_access(
    estate: Estate,
) -> None:
    await estate.center("requestOnly")
    request = await estate.grants.create_catalog_access_request(
        CALLER, AccessRequestCreate(resource=estate.resources[0], cost_center_id=CENTER)
    )
    estate.grants._directory_lookup = None
    with pytest.raises(DirectoryError, match="not configured"):
        await estate.grants.approve_access_request(ACTOR, request.id, AccessRequestApproval())
    assert not await estate.entitlements.list_entitlements(TENANT)
    preview = await estate.grants.preview_access_request(ACTOR, request.id, automation=True)
    assert "eligibilityUnverified" in {item.code for item in preview.blockers}


async def test_removal_revokes_only_affected_direct_grants_and_keys_stay_until_apply(
    estate: Estate,
) -> None:
    await estate.center()
    automatic = await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    direct = await estate.direct(estate.resources[0])
    await estate.apply()
    await estate.keys().create_key(CALLER, direct.id)
    writes = list(estate.apim.payloads)
    estate.graph.state.group_members[GROUP].remove(USER)
    estate.graph.calls.clear()

    await estate.grants.member_removed_from_cost_center(ACTOR, CENTER, USER, PrincipalKind.USER)

    assert len(estate.graph.calls) == 1
    assert estate.graph.calls[0][1] == (USER,)
    assert estate.apim.payloads == writes
    revoked = await estate.grants.get_entitlement(ACTOR, direct.id)
    assert revoked.revocation is not None
    assert revoked.runtime.status == "revocationPending"
    assert revoked.runtime.key_exists and revoked.runtime.key_revocation_pending
    assert all(
        [
            (await estate.entitlements.get_entitlement(TENANT, grant.id)).enabled
            for grant in automatic
        ]
    )
    await estate.apply()
    assert f"subscriptions/{estate.subscription(direct)}" not in estate.apim.written
    final = await estate.grants.get_entitlement(ACTOR, direct.id)
    assert final.runtime.status == "revoked" and not final.runtime.key_exists


async def test_nested_or_unknown_removal_checks_one_direct_cohort_and_keeps_failures_pending(
    estate: Estate,
) -> None:
    await estate.center()
    automatic = await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    direct = [await estate.direct(resource) for resource in estate.resources]
    application = await estate.direct(estate.resources[0], application=True)
    estate.graph.calls.clear()
    estate.graph.bad_evidence = "partial"
    await estate.grants.member_removed_from_cost_center(ACTOR, CENTER, OTHER_GROUP, None)
    assert len(estate.graph.calls) == 1
    assert set(estate.graph.calls[0][1]) == {USER, APPLICATION}
    center = await estate.cost_center_records.get_cost_center(TENANT, CENTER)
    [pending] = center.pending_rechecks
    assert pending.verification_error is not None
    assert pending.last_checked_at is not None
    assert set(pending.entitlement_ids) == {item.id for item in [*direct, application]}
    assert not set(pending.entitlement_ids) & {item.id for item in automatic}
    assert all(
        [
            (await estate.entitlements.get_entitlement(TENANT, item.id)).revocation is None
            for item in [*direct, application, *automatic]
        ]
    )
    estate.graph.bad_evidence = None
    estate.graph.state.group_members[GROUP].remove(USER)
    estate.graph.calls.clear()
    revoked = await estate.grants.resume_rechecks(ACTOR, CENTER)
    assert set(revoked) == {item.id for item in direct}
    assert len(estate.graph.calls) == 1
    assert not (await estate.cost_center_records.get_cost_center(TENANT, CENTER)).pending_rechecks


async def test_portal_modes_and_origins_are_exact_and_do_not_create_individual_grants(
    estate: Estate,
) -> None:
    await estate.center()
    await estate.change_center(
        resource_access=[
            CostCenterResourceAccess(resource=estate.resources[0], mode="automatic"),
            CostCenterResourceAccess(resource=estate.resources[1], mode="requestOnly"),
        ]
    )
    await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    inherited = await estate.portal.my_entitlements(CALLER)
    assert len(inherited) == 1 and inherited[0].access_origin == "automaticCostCenter"
    assert inherited[0].entitlement.subject.kind == "securityGroup"
    catalog = {item.kind: item for item in await estate.portal.catalog(CALLER)}
    assert [
        item.mode
        for item in catalog["modelApi"].cost_center_access
        if item.cost_center.id == CENTER
    ] == ["automatic"]
    assert [
        item.mode
        for item in catalog["mcpServer"].cost_center_access
        if item.cost_center.id == CENTER
    ] == ["requestOnly"]
    assert not any(
        item.cost_center.id == CENTER for item in catalog["poolModel"].cost_center_access
    )
    request = await estate.portal.create_access_request(
        CALLER, AccessRequestCreate(resource=estate.resources[1], cost_center_id=CENTER)
    )
    await estate.grants.approve_access_request(ACTOR, request.id, AccessRequestApproval())
    individual = await estate.portal.my_entitlements(CALLER)
    assert {item.access_origin for item in individual} == {
        "automaticCostCenter",
        "approvedIndividual",
    }
    overage = Actor(USER, TENANT, group_ids=frozenset({GROUP}), groups_overage=True)
    assert not await estate.portal.my_entitlements(overage)
    assert not any(
        choice.cost_center.id == CENTER
        for item in await estate.portal.catalog(overage)
        for choice in item.cost_center_access
    )
    assert len(await estate.entitlements.list_entitlements(TENANT)) == 2


async def test_general_catalog_key_flags_follow_resource_credential_support(
    estate: Estate,
) -> None:
    await estate.govern(keys_enabled=False, entra_enabled=True)
    server = await estate.gateways.get_mcp_server(TENANT, estate.mcp.mcp_server_id)
    assert server is not None
    await estate.gateways.save_mcp_server(
        server.model_copy(update={"publication_id": None}), audit()
    )
    book = await estate.grants.cost_center_book(TENANT)
    general = book.get(general_cost_center_id(TENANT))
    for resource in estate.resources[:2]:
        [option] = await estate.grants.cost_center_access_options(
            ACTOR,
            resource,
            centers=[general],
            default_cost_center_id=general.id,
        )
        assert option.mode == "requestOnly" and not option.keys_allowed

    gateway = await estate.gateways.get_gateway(TENANT, estate.gateway_id)
    await estate.gateways.save_gateway(
        gateway.model_copy(update={"azure_cloud": "government"}), audit()
    )
    [government] = await estate.grants.cost_center_access_options(
        ACTOR,
        estate.resources[1],
        centers=[general],
        default_cost_center_id=general.id,
    )
    assert government.keys_allowed


@pytest.mark.parametrize("index", range(3), ids=["model", "mcp", "pool"])
async def test_inherited_connections_are_token_only_and_group_overage_is_not_membership(
    estate: Estate, index: int
) -> None:
    await estate.center()
    grants = await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    grant = next(item for item in grants if item.resource == estate.resources[index])
    access = estate.keys()
    access._runtime_client_id = (await estate.snapshots())[index].audience
    connect = access.mcp_connection if index == 1 else access.connection
    writes = list(estate.apim.payloads)
    connection = await connect(CALLER, grant.id)
    assert connection.principal_kind == "securityGroup"
    assert not connection.keys_available and not connection.key_exists
    assert connection.via_group_id == grant.subject.id
    assert connection.cost_center.id == CENTER
    assert connection.runtime.status == "pending"
    assert connection.application_scope if index == 1 else connection.entra_application_scope
    assert connection.delegated_scope if index == 1 else connection.entra_scope
    assert estate.apim.payloads == writes

    overage = Actor(USER, TENANT, group_ids=frozenset({GROUP}), groups_overage=True)
    assert not await access.list_for_caller(overage)
    with pytest.raises(NotFoundError):
        await connect(overage, grant.id)
    with pytest.raises(NotFoundError):
        await connect(Actor(USER, TENANT), grant.id)


async def test_unregistered_callers_key_listing_honors_exact_automatic_modes(
    estate: Estate,
) -> None:
    await estate.center()
    await estate.grants.sync_cost_center_access(ACTOR, CENTER)
    caller = Actor(OTHER_GROUP, TENANT, group_ids=frozenset({GROUP}))
    assert len(await estate.keys().list_for_caller(caller)) == 3
    await estate.change_center(resource_access=[])
    assert not await estate.keys().list_for_caller(caller)


class NoInitialKeys:
    async def get_subscription(self, _name: str) -> Any:
        raise AssertionError("Custom Government credentials must not be inspected or issued")

    async def create(self, _name: str, **_kwargs: Any) -> None:
        raise AssertionError("Custom Government keys are excluded")

    async def delete(self, _name: str) -> None:
        raise AssertionError("No directory operation revokes a copied key")

    async def verify_runtime(self, **_kwargs: Any) -> None:
        raise AssertionError("Custom Government workflows are excluded")


async def test_government_custom_centers_cannot_receive_initial_keys_for_any_publication_type(
    estate: Estate,
) -> None:
    await estate.center("requestOnly")
    for resource in estate.resources:
        await estate.direct(resource)
    snapshots = await estate.snapshots()
    publication = await estate.service.get_publication(ACTOR, estate.publication_id)
    owners = [publication, estate.mcp, estate.pool]
    gateway = await estate.gateways.get_gateway(TENANT, estate.gateway_id)
    book = await estate.grants.cost_center_book(TENANT)
    for owner, snapshot in zip(owners, snapshots, strict=True):
        updated = owner.model_copy(
            update={
                "azure_cloud": "government",
                "status": PublicationStatus.PUBLISHED,
                "access_state": "applied",
                "governed_access": ModelAccessSettings(keys_enabled=True, entra_enabled=False),
                "applied_access": snapshot.model_copy(
                    update={
                        "azure_cloud": "government",
                        "settings": ModelAccessSettings(keys_enabled=True, entra_enabled=False),
                    }
                ),
            }
        )
        assert (
            await reconcile_initial_keys(
                updated,
                gateway.model_copy(update={"azure_cloud": "government"}),
                repository=estate.gateways,
                directory=estate.directory,
                entitlements=estate.entitlements,
                book=book,
                manager=NoInitialKeys(),
            )
            == updated
        )
    await estate.gateways.save_gateway(
        gateway.model_copy(update={"azure_cloud": "government"}), audit()
    )
    center = book.get(CENTER)
    with pytest.raises(ValidationError, match="Government"):
        await estate.grants.validate_cost_center_access(ACTOR, center)
    assert not any(
        choice.cost_center.id == CENTER
        for entry in await estate.portal.catalog(CALLER)
        for choice in entry.cost_center_access
    )


def test_general_keeps_legacy_resource_rules_and_unguarded_policy_identity() -> None:
    book = CostCenterBook(TENANT, [], None)
    general = book.get(general_cost_center_id(TENANT))
    assert general.is_general and may_charge(general, principal=None, settings=None)
    unknown = EntitlementResource(kind="modelApi", id="legacy-offering")
    assert general.access_mode_for(unknown) == CostCenterResourceAccessMode.REQUEST_ONLY
    grant = Entitlement(
        id="legacy",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="user", id="person"),
        resource=unknown,
    )
    intent = cost_center_intent(grant, None, book)
    assert intent.required_cost_center_group_id is None
    assert "requiredCostCenterGroupId" not in intent.payload()
