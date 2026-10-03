"""Governed access to model pools: grants on pool models and their runtime state (ADR 0024).

A grant names one pool model, scoped to its pool. Its runtime state comes from the pool's applied
snapshot, as a publication grant's does. Nothing infers its binding: the apply that issues its key
records that.
"""

from typing import Any

import pytest
from apim_double import RESOURCE_GROUP, RESOURCE_ID, SERVICE_NAME, SUBSCRIPTION_ID
from mosaic_api.cost_centers import CostCenterBook
from mosaic_api.domain import (
    AccessRequestCreate,
    AuditEvent,
    BindingSource,
    Entitlement,
    EntitlementBinding,
    EntitlementCreate,
    EntitlementResource,
    EntitlementSubject,
    EntitlementUpdate,
    Gateway,
    GatewayCapabilities,
    ModelAccessSettings,
    Principal,
    PrincipalKind,
    PublicationStatus,
    PublishedResource,
    PublishedResourceKind,
    entitlement_id,
    general_cost_center_id,
    new_id,
    subject_kind_for,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.model_pools import (
    ModelPool,
    ModelPoolVisibility,
    PoolAccessGrant,
    PoolAccessSnapshot,
    PoolModel,
    backend_pool_name,
    model_pool_id,
    pool_key_name,
    pool_model_id,
)
from mosaic_api.repositories import (
    InMemoryDirectoryRepository,
    InMemoryEntitlementRepository,
    InMemoryGatewayRepository,
    InMemoryModelEndpointRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest
from mosaic_api.services.pool_access import (
    denied_pool_access_snapshot,
    entitlement_key_name,
    safe_pool_access_snapshot,
)

TENANT = "tenant-test"
GATEWAY = "gateway"
USER_OID = "11111111-1111-1111-1111-111111111111"
OTHER_OID = "22222222-2222-2222-2222-222222222222"
GROUP_OID = "55555555-5555-5555-5555-555555555555"
ACTOR = Actor(object_id=USER_OID, tenant_id=TENANT)
REQUESTER = Actor(object_id=OTHER_OID, tenant_id=TENANT)
GENERAL = general_cost_center_id(TENANT)
API_NAME = "mosaic-pool-anthropic"
POOL_ID = model_pool_id(TENANT, GATEWAY, API_NAME)
OPUS = pool_model_id(POOL_ID, "claude-opus-4-5")
SONNET = pool_model_id(POOL_ID, "claude-sonnet-4-5")
HAIKU = pool_model_id(POOL_ID, "claude-haiku-4-5")


def _audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        action="test.seed",
        resource_type="test",
        resource_id="seed",
        actor_object_id=USER_OID,
    )


def _resource(model_id: str = OPUS, pool_id: str = POOL_ID) -> EntitlementResource:
    return EntitlementResource(kind="poolModel", id=model_id, scope_id=pool_id)


def _pool_model(model_id: str, public_name: str, display_name: str) -> PoolModel:
    return PoolModel(
        id=model_id,
        public_name=public_name,
        display_name=display_name,
        backend_pool_name=backend_pool_name(API_NAME, model_id, public_name),
    )


def _pool() -> ModelPool:
    """A governed pool whose last apply wrote its API and both its models."""

    return ModelPool(
        id=POOL_ID,
        tenant_id=TENANT,
        gateway_id=GATEWAY,
        display_name="Anthropic",
        vendor="Anthropic",
        api_name=API_NAME,
        api_path="mosaic/pool-anthropic",
        fragment_name=API_NAME,
        product_name=API_NAME,
        subscription_name=API_NAME,
        status=PublicationStatus.PUBLISHED,
        models=[
            _pool_model(OPUS, "claude-opus-4-5", "Claude Opus 4.5"),
            _pool_model(SONNET, "claude-sonnet-4-5", "Claude Sonnet 4.5"),
        ],
        resources=[
            PublishedResource(
                kind=PublishedResourceKind.API,
                name=API_NAME,
                resource_id=f"{RESOURCE_ID}/apis/{API_NAME}",
                created_by_mosaic=True,
            )
        ],
        applied_model_ids=[OPUS, SONNET],
        governed_access=ModelAccessSettings(),
        access_state="applied",
    )


def _digest(entitlement: Entitlement, principal: Principal) -> str:
    return entitlement_intent_digest(
        entitlement,
        principal,
        cost_center_intent(entitlement, principal, CostCenterBook(TENANT, [], None)),
    )


def _compiled(
    entitlement_ref: str,
    model_id: str = OPUS,
    *,
    enabled: bool = True,
    digest: str = "digest",
) -> PoolAccessGrant:
    subject_id = f"principal-{entitlement_ref}"
    return PoolAccessGrant(
        entitlement_id=entitlement_ref,
        pool_model_id=model_id,
        subject=EntitlementSubject(kind="user", id=subject_id),
        object_id=USER_OID,
        display_name="Ana",
        key_name=pool_key_name(TENANT, POOL_ID, subject_id, GENERAL),
        enabled=enabled,
        intent_digest=digest,
    )


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
        self.pool = _pool()

    async def seed(self) -> None:
        gateway = Gateway(
            id=GATEWAY,
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
        await self.gateways.create_gateway(gateway, _audit())
        await self.gateways.save_model_pool(self.pool, _audit())

    async def update_pool(self, **changes: Any) -> ModelPool:
        self.pool = self.pool.model_copy(update=changes)
        await self.gateways.save_model_pool(self.pool, _audit())
        return self.pool

    async def principal(
        self, object_id: str = USER_OID, kind: PrincipalKind = PrincipalKind.USER
    ) -> Principal:
        principal = Principal(
            id=new_id("principal"),
            tenant_id=TENANT,
            object_id=object_id,
            kind=kind,
            label=f"{kind.value} {object_id[:4]}",
        )
        await self.directory.create_principal(principal, _audit())
        return principal

    async def grant(
        self,
        principal: Principal,
        model_id: str = OPUS,
        *,
        enabled: bool = True,
        binding: EntitlementBinding | None = None,
    ) -> Entitlement:
        subject = EntitlementSubject(kind=subject_kind_for(principal.kind), id=principal.id)
        resource = _resource(model_id)
        entitlement = Entitlement(
            id=entitlement_id(TENANT, subject, resource, GENERAL),
            tenant_id=TENANT,
            subject=subject,
            resource=resource,
            enabled=enabled,
            binding=binding,
        )
        await self.entitlement_repository.create_entitlement(entitlement, _audit())
        return entitlement

    def compiled(
        self,
        entitlement: Entitlement,
        principal: Principal,
        *,
        enabled: bool = True,
        intent_digest: str | None = None,
    ) -> PoolAccessGrant:
        """The grant as an apply compiles it into the pool's policy."""

        return PoolAccessGrant(
            entitlement_id=entitlement.id,
            pool_model_id=entitlement.resource.id,
            subject=entitlement.subject,
            object_id=(
                principal.object_id.casefold()
                if principal.kind == PrincipalKind.SECURITY_GROUP
                else principal.object_id
            ),
            display_name=principal.label or principal.object_id,
            key_name=entitlement_key_name(self.pool, entitlement),
            enabled=enabled,
            intent_digest=(
                intent_digest if intent_digest is not None else _digest(entitlement, principal)
            ),
            cost_center_id=entitlement.cost_center_id,
        )

    async def apply(
        self,
        *grants: PoolAccessGrant,
        settings: ModelAccessSettings | None = None,
        **changes: Any,
    ) -> ModelPool:
        """Record what an apply would: the compiled grants, and any other pool state given."""

        snapshot = PoolAccessSnapshot(
            version=1, settings=settings or ModelAccessSettings(), grants=list(grants)
        )
        return await self.update_pool(applied_access=snapshot, **changes)

    async def apply_grant(
        self,
        entitlement: Entitlement,
        principal: Principal,
        *,
        enabled: bool = True,
        intent_digest: str | None = None,
        settings: ModelAccessSettings | None = None,
        **changes: Any,
    ) -> ModelPool:
        grant = self.compiled(
            entitlement, principal, enabled=enabled, intent_digest=intent_digest
        )
        return await self.apply(grant, settings=settings, **changes)


@pytest.fixture
async def harness() -> Harness:
    built = Harness()
    await built.seed()
    return built


def test_a_key_is_one_per_subject_pool_and_cost_center() -> None:
    key = pool_key_name(TENANT, POOL_ID, "principal-1", GENERAL)

    assert key == pool_key_name(TENANT, POOL_ID, "principal-1", GENERAL)
    # API Management names may not contain an underscore.
    assert key.startswith("mosaic-pool-key-") and "_" not in key
    assert len(key) <= 80
    assert key != pool_key_name(TENANT, POOL_ID, "principal-2", GENERAL)
    assert key != pool_key_name(TENANT, POOL_ID, "principal-1", "costCenter_research")
    assert key != pool_key_name(TENANT, "modelPool_other", "principal-1", GENERAL)


def test_a_compiled_grant_has_a_key_exactly_when_its_subject_is_direct() -> None:
    values: dict[str, Any] = {
        "entitlement_id": "grant",
        "pool_model_id": OPUS,
        "object_id": USER_OID,
        "display_name": "Ana",
        "enabled": True,
        "intent_digest": "digest",
    }

    with pytest.raises(ValueError, match="not enforced at runtime"):
        PoolAccessGrant(
            subject=EntitlementSubject(kind="group", id="group"), key_name="key", **values
        )
    with pytest.raises(ValueError, match="has no key"):
        PoolAccessGrant(
            subject=EntitlementSubject(kind="securityGroup", id="group"), key_name="key", **values
        )
    with pytest.raises(ValueError, match="needs its key"):
        PoolAccessGrant(subject=EntitlementSubject(kind="user", id="person"), **values)
    application = PoolAccessGrant(
        subject=EntitlementSubject(kind="application", id="app"), key_name="key", **values
    )
    group = PoolAccessGrant(subject=EntitlementSubject(kind="securityGroup", id="group"), **values)
    assert (application.is_group_grant, group.is_group_grant) == (False, True)


def test_a_snapshot_groups_direct_grants_by_the_key_they_share() -> None:
    shared = _compiled("one")
    snapshot = PoolAccessSnapshot(
        version=1,
        settings=ModelAccessSettings(),
        grants=[
            shared,
            shared.model_copy(update={"entitlement_id": "two", "pool_model_id": SONNET}),
            _compiled("three"),
        ],
    )

    keys = snapshot.key_grants()

    assert [grant.entitlement_id for grant in keys[shared.key_name or ""]] == ["one", "two"]
    assert len(keys) == 2
    assert [grant.entitlement_id for grant in snapshot.grants_for(SONNET)] == ["two"]


def test_recovery_may_restrict_pool_access_but_never_introduce_it() -> None:
    applied = PoolAccessSnapshot(
        version=1,
        settings=ModelAccessSettings(),
        grants=[
            _compiled("kept"),
            _compiled("changed"),
            _compiled("dropped"),
            _compiled("off", enabled=False),
            _compiled("moved"),
        ],
    )
    pool = _pool().model_copy(update={"applied_access": applied, "access_state": "failed"})
    target = PoolAccessSnapshot(
        version=2,
        settings=ModelAccessSettings(entra_enabled=False),
        grants=[
            _compiled("kept"),
            _compiled("changed", digest="changed"),
            _compiled("off"),
            _compiled("moved", SONNET),
            _compiled("new"),
        ],
    )

    safe = safe_pool_access_snapshot(pool, target)

    assert safe.version == 2
    assert {grant.entitlement_id: grant.enabled for grant in safe.grants} == {
        "kept": True,
        "changed": False,
        "dropped": False,
        "off": False,
        "moved": False,
    }
    assert safe.settings == ModelAccessSettings(keys_enabled=True, entra_enabled=False)

    # Changing whether tokens are metered changes what every grant compiles to.
    unmetered = target.model_copy(update={"token_metering": False})
    assert not any(grant.enabled for grant in safe_pool_access_snapshot(pool, unmetered).grants)

    # When an interrupted apply left the gateway's state unknown, nothing is trusted.
    unknown = pool.model_copy(update={"access_state": "unknown"})
    denied = safe_pool_access_snapshot(unknown, target)
    assert denied == denied_pool_access_snapshot(target)
    assert denied.settings == ModelAccessSettings(keys_enabled=False, entra_enabled=False)
    assert not any(grant.enabled for grant in denied.grants)


@pytest.mark.parametrize(
    ("case", "status"),
    [
        ("unknown", "unknown"),
        ("access-applying", "applying"),
        ("failed", "failed"),
        ("no-snapshot", "pending"),
        ("unpublished", "revoked"),
        ("disabled-not-applied", "revocationPending"),
        ("removed-not-applied", "revocationPending"),
        ("removed", "revoked"),
        ("never-applied", "pending"),
        ("digest-mismatch", "pending"),
        ("methods-changed", "pending"),
        ("grant-disabled", "revoked"),
        ("no-methods", "revoked"),
        ("applied", "applied"),
    ],
)
async def test_pool_model_runtime_statuses(harness: Harness, case: str, status: str) -> None:
    principal = await harness.principal()
    entitlement = await harness.grant(principal)
    sonnet_only = [model for model in harness.pool.models if model.id == SONNET]
    if case == "unknown":
        await harness.apply_grant(entitlement, principal, access_state="unknown")
    elif case == "access-applying":
        await harness.apply_grant(entitlement, principal, access_state="applying")
    elif case == "failed":
        await harness.apply_grant(entitlement, principal, access_state="failed")
    elif case == "no-snapshot":
        pass  # The pool is governed, but no apply has compiled its grants yet.
    elif case == "unpublished":
        await harness.apply_grant(
            entitlement, principal, status=PublicationStatus.DRAFT, resources=[]
        )
    elif case == "disabled-not-applied":
        await harness.entitlement_repository.save_entitlement(
            entitlement.model_copy(update={"enabled": False}), _audit()
        )
        await harness.apply_grant(entitlement, principal)
    elif case == "removed-not-applied":
        await harness.apply_grant(entitlement, principal, models=sonnet_only)
    elif case == "removed":
        await harness.apply_grant(entitlement, principal, enabled=False, models=sonnet_only)
    elif case == "never-applied":
        await harness.apply()
    elif case == "digest-mismatch":
        await harness.apply_grant(entitlement, principal, intent_digest="outdated")
    elif case == "methods-changed":
        await harness.apply_grant(
            entitlement, principal, governed_access=ModelAccessSettings(keys_enabled=False)
        )
    elif case == "grant-disabled":
        await harness.apply_grant(entitlement, principal, enabled=False)
    elif case == "no-methods":
        off = ModelAccessSettings(keys_enabled=False, entra_enabled=False)
        await harness.apply_grant(entitlement, principal, settings=off, governed_access=off)
    else:
        await harness.apply_grant(entitlement, principal)

    decorated = await harness.entitlements.get_entitlement(ACTOR, entitlement.id)

    assert decorated.runtime is not None
    assert decorated.runtime.status == status
    assert decorated.runtime.publication_id == POOL_ID
    # The pool's record names no key it created.
    assert decorated.runtime.key_exists is False
    if case in {"no-snapshot", "never-applied"}:
        assert decorated.runtime.subscription_name is None
    else:
        assert decorated.runtime.subscription_name == pool_key_name(
            TENANT, POOL_ID, principal.id, GENERAL
        )
    assert (decorated.runtime.applied_methods is None) == (case == "no-snapshot")


async def test_a_pool_model_grant_is_applying_and_locked_while_the_pool_applies(
    harness: Harness,
) -> None:
    principal = await harness.principal()
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)
    await harness.gateways.acquire_publication_lock(TENANT, POOL_ID, "run")
    try:
        decorated = await harness.entitlements.get_entitlement(ACTOR, entitlement.id)
        with pytest.raises(ConflictError, match="already running"):
            await harness.entitlements.update_entitlement(
                ACTOR, entitlement.id, EntitlementUpdate(notes="blocked")
            )
    finally:
        await harness.gateways.release_publication_lock(TENANT, POOL_ID, "run")

    assert decorated.runtime is not None
    assert decorated.runtime.status == "applying"


async def test_ungoverned_pools_and_mosaic_groups_have_no_pool_runtime(
    harness: Harness,
) -> None:
    principal = await harness.principal()
    direct = await harness.grant(principal)
    group = Entitlement(
        id="group-grant",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="group", id="group"),
        resource=_resource(),
    )
    await harness.entitlement_repository.create_entitlement(group, _audit())
    await harness.apply_grant(direct, principal)

    assert (await harness.entitlements.get_entitlement(ACTOR, direct.id)).runtime is not None
    assert (await harness.entitlements.get_entitlement(ACTOR, group.id)).runtime is None

    # Before an administrator opts a pool in, its grants aren't enforced at all.
    await harness.update_pool(governed_access=None, applied_access=None)
    assert (await harness.entitlements.get_entitlement(ACTOR, direct.id)).runtime is None


async def test_a_subjects_direct_grants_in_a_pool_share_one_key(harness: Harness) -> None:
    principal = await harness.principal()
    opus = await harness.grant(principal, OPUS)
    sonnet = await harness.grant(principal, SONNET)
    other = await harness.principal(OTHER_OID)
    theirs = await harness.grant(other, OPUS)
    key = pool_key_name(TENANT, POOL_ID, principal.id, GENERAL)
    await harness.apply(
        harness.compiled(opus, principal),
        harness.compiled(sonnet, principal),
        harness.compiled(theirs, other),
    )

    decorated = [
        await harness.entitlements.get_entitlement(ACTOR, item.id)
        for item in (opus, sonnet, theirs)
    ]

    assert [item.runtime.status if item.runtime else None for item in decorated] == [
        "applied",
        "applied",
        "applied",
    ]
    assert [item.runtime.subscription_name if item.runtime else None for item in decorated] == [
        key,
        key,
        pool_key_name(TENANT, POOL_ID, other.id, GENERAL),
    ]

    # A key exists once the pool's record says an apply created it.
    await harness.update_pool(
        resources=[
            *harness.pool.resources,
            PublishedResource(
                kind=PublishedResourceKind.SUBSCRIPTION,
                name=key,
                resource_id=f"{RESOURCE_ID}/subscriptions/{key}",
                created_by_mosaic=True,
            ),
        ]
    )
    mine = await harness.entitlements.get_entitlement(ACTOR, sonnet.id)
    their_key = await harness.entitlements.get_entitlement(ACTOR, theirs.id)
    assert mine.runtime is not None and mine.runtime.key_exists is True
    assert their_key.runtime is not None and their_key.runtime.key_exists is False


async def test_a_security_group_grant_authorizes_tokens_and_has_no_key(
    harness: Harness,
) -> None:
    group = await harness.principal(GROUP_OID, PrincipalKind.SECURITY_GROUP)
    entitlement = await harness.grant(group)
    compiled = harness.compiled(entitlement, group)
    assert (compiled.key_name, compiled.is_group_grant) == (None, True)
    await harness.apply(compiled)

    decorated = await harness.entitlements.get_entitlement(ACTOR, entitlement.id)

    assert decorated.runtime is not None
    assert (decorated.runtime.status, decorated.runtime.subscription_name) == ("applied", None)
    assert decorated.runtime.key_exists is False


async def test_an_orchestrated_binding_shows_only_while_the_pool_applies_the_grant(
    harness: Harness,
) -> None:
    principal = await harness.principal()
    key = pool_key_name(TENANT, POOL_ID, principal.id, GENERAL)
    entitlement = await harness.grant(
        principal,
        binding=EntitlementBinding(
            gateway_id=GATEWAY, apim_subscription_name=key, source=BindingSource.ORCHESTRATED
        ),
    )

    unapplied = await harness.entitlements.get_entitlement(ACTOR, entitlement.id)
    await harness.apply_grant(entitlement, principal)
    applied = await harness.entitlements.get_entitlement(ACTOR, entitlement.id)

    assert unapplied.binding is None
    assert applied.binding is not None
    assert applied.binding.apim_subscription_name == key


async def test_an_applied_pool_grant_is_kept_until_an_apply_revokes_it(
    harness: Harness,
) -> None:
    principal = await harness.principal()
    entitlement = await harness.grant(principal)
    await harness.apply_grant(entitlement, principal)

    with pytest.raises(ConflictError, match="server-managed"):
        await harness.entitlements.update_entitlement(
            ACTOR, entitlement.id, EntitlementUpdate(binding=None)
        )
    with pytest.raises(ConflictError, match="apply the pool") as refused:
        await harness.entitlements.delete_entitlement(ACTOR, entitlement.id)
    assert refused.value.details == {"modelPoolId": POOL_ID}

    # While an interrupted apply's outcome is unknown, any grant may still be live.
    await harness.apply_grant(entitlement, principal, enabled=False, access_state="unknown")
    with pytest.raises(ConflictError, match="apply the pool"):
        await harness.entitlements.delete_entitlement(ACTOR, entitlement.id)

    await harness.update_pool(access_state="applied")
    await harness.entitlements.delete_entitlement(ACTOR, entitlement.id)
    assert await harness.entitlement_repository.get_entitlement(TENANT, entitlement.id) is None


async def test_a_pool_model_grant_names_a_model_its_pool_has(harness: Harness) -> None:
    principal = await harness.principal()
    subject = EntitlementSubject(kind="user", id=principal.id)

    for resource in (_resource(HAIKU), _resource(OPUS, pool_id="modelPool_missing")):
        with pytest.raises(ValidationError, match="does not govern"):
            await harness.entitlements.create_entitlement(
                ACTOR, EntitlementCreate(subject=subject, resource=resource)
            )

    created = await harness.entitlements.create_entitlement(
        ACTOR, EntitlementCreate(subject=subject, resource=_resource())
    )

    # Only the apply that issues its key binds a pool model grant.
    assert created.binding is None
    assert created.cost_center_id == GENERAL
    assert created.runtime is not None
    assert created.runtime.status == "pending"


async def test_the_catalog_takes_requests_for_listed_models_a_governed_pool_serves(
    harness: Harness,
) -> None:
    created = await harness.entitlements.create_catalog_access_request(
        REQUESTER, AccessRequestCreate(resource=_resource())
    )

    assert created.resource == _resource()
    assert created.resource_snapshot is not None
    assert created.resource_snapshot.display_name == "Claude Opus 4.5"
    assert created.resource_snapshot.gateway_name == "Gateway"


@pytest.mark.parametrize(
    "case", ["hidden-pool", "unlisted-model", "missing-gateway", "missing-model", "missing-pool"]
)
async def test_the_catalog_has_no_pool_model_an_administrator_didnt_list(
    harness: Harness, case: str
) -> None:
    resource = _resource()
    if case == "hidden-pool":
        await harness.update_pool(visibility=ModelPoolVisibility.HIDDEN)
    elif case == "unlisted-model":
        await harness.update_pool(
            models=[
                model.model_copy(update={"listed": model.id != OPUS})
                for model in harness.pool.models
            ]
        )
    elif case == "missing-gateway":
        harness.gateways.gateways.pop(GATEWAY)
    elif case == "missing-model":
        resource = _resource(HAIKU)
    else:
        resource = _resource(OPUS, pool_id="modelPool_missing")

    with pytest.raises(NotFoundError, match="isn't in your catalog"):
        await harness.entitlements.create_catalog_access_request(
            REQUESTER, AccessRequestCreate(resource=resource)
        )


@pytest.mark.parametrize("case", ["ungoverned", "not-applied", "api-gone"])
async def test_a_listed_model_the_gateway_doesnt_serve_takes_no_requests(
    harness: Harness, case: str
) -> None:
    if case == "ungoverned":
        await harness.update_pool(governed_access=None)
    elif case == "not-applied":
        await harness.update_pool(applied_model_ids=[SONNET])
    else:
        await harness.update_pool(status=PublicationStatus.DRAFT, resources=[])

    with pytest.raises(ConflictError, match="isn't published right now") as refused:
        await harness.entitlements.create_catalog_access_request(
            REQUESTER, AccessRequestCreate(resource=_resource())
        )

    assert refused.value.details["reason"] == "notPublished"


async def test_summaries_offer_a_pool_model_only_while_a_governed_pool_serves_it(
    harness: Harness,
) -> None:
    async def available() -> list[bool]:
        summaries = await harness.entitlements.resource_summaries(
            TENANT, [_resource(OPUS), _resource(SONNET)]
        )
        return [summary.available for summary in summaries]

    [summary] = await harness.entitlements.resource_summaries(TENANT, [_resource()])
    assert (summary.display_name, summary.gateway_name, summary.scope_id) == (
        "Claude Opus 4.5",
        "Gateway",
        POOL_ID,
    )
    assert await available() == [True, True]

    await harness.update_pool(applied_model_ids=[SONNET])
    assert await available() == [False, True]

    await harness.update_pool(governed_access=None)
    assert await available() == [False, False]


async def test_pool_models_are_named_by_their_display_names(harness: Harness) -> None:
    names = await harness.entitlements.resource_display_names(
        ACTOR,
        [_resource(OPUS), _resource(SONNET), _resource(OPUS, pool_id="modelPool_other")],
    )

    assert names == ["Claude Opus 4.5", "Claude Sonnet 4.5", None]
