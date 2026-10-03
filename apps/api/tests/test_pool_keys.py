"""Pool model grants in the portal: connection details, and the key a holder shares (ADR 0024).

A grant on a pool model connects to the pool's endpoint and sends the model's name. Every model a
subject holds directly in one pool, under one cost center, shares one key, so the portal names the
other models a key unlocks. Nothing the portal returns names the pool's members, their endpoints,
or their regions, and nothing it refuses with says "pool".
"""

from collections.abc import AsyncIterator, Awaitable
from typing import Any, Literal, cast
from uuid import uuid4

import httpx
import pytest
from apim_double import RESOURCE_ID, FakeCredential
from azure.core.credentials_async import AsyncTokenCredential
from mosaic_api.cost_centers import general_cost_center
from mosaic_api.domain import (
    Entitlement,
    EntitlementEnforcement,
    EntitlementResource,
    EntitlementSubject,
    ModelAccessSettings,
    Principal,
    PrincipalKind,
    PublishRunStatus,
    TokenEnforcement,
    entitlement_id,
    new_id,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.integrations.apim import ArmClient
from mosaic_api.integrations.apim.credentials import ApimKeyManager
from mosaic_api.model_pools import ModelPool, PoolSafeguard
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService
from mosaic_api.services.pool_access import entitlement_key_name
from mosaic_api.services.portal_access import PortalAccessService
from pydantic import SecretStr
from test_model_pools import ACTOR, GATEWAY_URL, TENANT, _audit, _gpt4o, _member, _model
from test_pool_governed import GENERAL, RUNTIME_CLIENT_ID, GovernedEstate

MINI = _model(_member("aoai-east", "gpt-4o-mini"), display_name="GPT-4o mini")
KEY_VALUE = "not-a-real-key"


async def _no_sleep(_seconds: float) -> None:
    return None


class Portal(GovernedEstate):
    """The governed pool estate, with the portal's access service over the same records."""

    def __init__(self) -> None:
        super().__init__()
        self.regenerated: list[str] = []
        self.reads: list[tuple[str, str, str]] = []
        self.arm = ArmClient(
            cast(AsyncTokenCredential, FakeCredential()),
            client=httpx.AsyncClient(transport=httpx.MockTransport(self._handle)),
            sleep=_no_sleep,
        )
        grants = EntitlementService(
            self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateway_repository,
            endpoint_repository=self.endpoint_repository,
            cost_center_repository=self.cost_centers,
        )
        self.access = PortalAccessService(
            grants,
            repository=self.entitlements,
            directory_repository=self.directory,
            gateway_repository=self.gateway_repository,
            credential_factory=lambda _resource: self,
            key_manager_factory=lambda resource: ApimKeyManager(self.arm, resource),
            model_runtime_client_id=RUNTIME_CLIENT_ID,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/regenerate" in request.url.path:
            self.regenerated.append("/".join(request.url.path.rsplit("/", 2)[-2:]))
            return httpx.Response(204)
        return self.apim.handler(request)

    async def read_key(
        self, subscription_name: str, api_name: str, slot: Literal["primary", "secondary"]
    ) -> SecretStr:
        self.reads.append((subscription_name, api_name, slot))
        return SecretStr(KEY_VALUE)

    async def grant_model(self, pool: ModelPool, principal: Principal, index: int) -> Entitlement:
        """A direct grant on one of the pool's models, under General."""

        subject = EntitlementSubject(kind="user", id=principal.id)
        resource = EntitlementResource(kind="poolModel", id=pool.models[index].id, scope_id=pool.id)
        grant = Entitlement(
            id=entitlement_id(TENANT, subject, resource, GENERAL),
            tenant_id=TENANT,
            subject=subject,
            resource=resource,
        )
        await self.entitlements.create_entitlement(grant, _audit())
        return grant

    async def security_group(self, label: str) -> Principal:
        group = Principal(
            id=new_id("principal"),
            tenant_id=TENANT,
            object_id=str(uuid4()),
            kind=PrincipalKind.SECURITY_GROUP,
            label=label,
        )
        await self.directory.create_principal(group, _audit())
        return group

    def owned(self, pool: ModelPool, name: str) -> bool:
        return pool.owns_key(name)

    def audits(self, action: str, grant: Entitlement) -> list[dict[str, Any]]:
        return [
            event.details
            for event in self.entitlements.audit_events.values()
            if event.action == action and event.resource_id == grant.id
        ]


@pytest.fixture
async def portal() -> AsyncIterator[Portal]:
    built = Portal()
    await built.setup()
    yield built
    await built.arm.close()


def _holder(principal: Principal) -> Actor:
    return Actor(object_id=principal.object_id, tenant_id=TENANT)


async def _published(
    portal: Portal, *, safeguard: PoolSafeguard | None = None
) -> tuple[ModelPool, Principal, Entitlement]:
    """A governed pool applied with one grant to Ada, who has no key yet."""

    pool = await portal.governed_pool()
    if safeguard is not None:
        await portal.update(pool.id, safeguard=safeguard)
    ada = await portal.principal("Ada")
    grant = await portal.grant(pool, ada)
    await portal.publish(pool.id)
    return await portal.pool(pool.id), ada, grant


async def _refused(
    attempt: Awaitable[Any], reason: str | None = None, *, match: str | None = None
) -> ConflictError:
    """The refusal a portal user sees, which never mentions the pool."""

    with pytest.raises(ConflictError, match=match) as raised:
        await attempt
    if reason is not None:
        assert raised.value.details.get("reason") == reason, raised.value.message
    assert "pool" not in raised.value.message.casefold()
    return raised.value


def _subscription(name: str) -> str:
    return f"subscriptions/{name}"


# -- connection details ---------------------------------------------------------------------------


async def test_a_pool_model_connects_to_the_pool_and_sends_the_model_name(
    portal: Portal,
) -> None:
    pool, ada, grant = await _published(portal, safeguard=PoolSafeguard(tokens_per_minute=1000))
    model = pool.models[0]

    connection = await portal.access.connection(_holder(ada), grant.id)

    assert connection.endpoint == f"{GATEWAY_URL}/{pool.api_path.strip('/')}"
    assert connection.deployment_name == "gpt-4o"
    assert connection.publication_id == pool.id
    assert connection.gateway_id == pool.gateway_id
    assert connection.api_shape == pool.api_shape
    assert connection.operations
    for operation in connection.operations:
        assert "{deployment-id}" not in operation.path
        assert "/gpt-4o/" in operation.path
    assert (connection.pool_id, connection.pool_model_id) == (pool.id, model.id)
    # The portal shows people models, never the pool that serves them.
    assert connection.pool_name is None
    assert connection.publication_limits == TokenEnforcement(
        counter_key_expression=model.id, tokens_per_minute=1000, estimate_prompt_tokens=False
    )
    assert connection.entra_scope == f"api://{RUNTIME_CLIENT_ID}/Models.Invoke"
    assert connection.applied_methods == ModelAccessSettings()
    assert connection.keys_available is True
    assert connection.keys_allowed_by_cost_center is True
    assert connection.cost_center is not None and connection.cost_center.code == "general"
    assert connection.key_exists is False
    assert connection.key_shared_with == []
    # Nothing names the members that serve the model, their endpoints, or their regions.
    body = connection.model_dump_json()
    for detail in ("aoai-east", "eastus2", "openai.azure.com"):
        assert detail not in body

    administered = await portal.access.connection(ACTOR, grant.id, administrator=True)
    assert administered.pool_name == "OpenAI"


async def test_without_a_safeguard_the_model_has_no_shared_limit(portal: Portal) -> None:
    _pool, ada, grant = await _published(portal)

    connection = await portal.access.connection(_holder(ada), grant.id)

    assert connection.publication_limits is None
    # The gateway can count the model's tokens; the pool just sets no limit on them.
    assert connection.token_metering is True


async def test_a_tier_that_cant_count_tokens_says_so(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    assert pool.applied_access is not None
    unmetered = pool.applied_access.model_copy(update={"token_metering": False})
    await portal.gateway_repository.save_model_pool(
        pool.model_copy(update={"applied_access": unmetered}), _audit()
    )

    connection = await portal.access.connection(_holder(ada), grant.id)

    assert connection.publication_limits is None
    assert connection.token_metering is False
    assert connection.model_dump(by_alias=True, mode="json")["tokenMetering"] is False


async def test_a_model_the_gateway_doesnt_serve_has_nothing_to_connect_to(
    portal: Portal,
) -> None:
    # Governed, but never applied.
    pool = await portal.governed_pool()
    ada = await portal.principal("Ada")
    grant = await portal.grant(pool, ada)
    await _refused(portal.access.connection(_holder(ada), grant.id), "notPublished")

    # Applied, then a model added that the gateway doesn't serve yet.
    await portal.publish(pool.id)
    await portal.update(pool.id, models=[_gpt4o("aoai-east"), MINI])
    pool = await portal.pool(pool.id)
    later = await portal.grant_model(pool, ada, 1)
    await _refused(portal.access.connection(_holder(ada), later.id), "notPublished")
    await _refused(portal.access.create_key(_holder(ada), later.id), "notPublished")
    assert (await portal.access.connection(_holder(ada), grant.id)).deployment_name == "gpt-4o"

    # Unpublished.
    run = await portal.unpublish(pool.id)
    assert run.status == PublishRunStatus.SUCCEEDED, run.errors
    await _refused(portal.access.connection(_holder(ada), grant.id), "notPublished")
    await _refused(portal.access.reveal_key(_holder(ada), grant.id, "primary"), "notPublished")


async def test_an_ungoverned_pool_serves_no_grants(portal: Portal) -> None:
    pool = await portal.create("OpenAI", _gpt4o("aoai-east"))
    await portal.publish(pool.id)
    ada = await portal.principal("Ada")
    grant = await portal.grant(await portal.pool(pool.id), ada)

    await _refused(portal.access.connection(_holder(ada), grant.id), "notPublished")
    await _refused(portal.access.create_key(_holder(ada), grant.id), "notPublished")


# -- keys on request ------------------------------------------------------------------------------


async def test_the_holder_creates_a_key_scoped_to_the_pool(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    key = entitlement_key_name(pool, grant)
    assert key is not None
    assert _subscription(key) not in portal.apim.written

    created = await portal.access.create_key(_holder(ada), grant.id)

    assert created.exists is True
    assert created.subscription_name == key
    assert created.pool_id == pool.id
    assert created.key_shared_with == []
    assert created.cost_center is not None and created.cost_center.code == "general"
    properties = portal.apim.written[_subscription(key)]["properties"]
    assert properties["state"] == "active"
    assert properties["displayName"] == "Ada (general)"
    assert properties["scope"].endswith(f"/apis/{pool.api_name}")
    assert portal.owned(await portal.pool(pool.id), key)
    connection = await portal.access.connection(_holder(ada), grant.id)
    assert connection.key_exists is True
    await _refused(portal.access.create_key(_holder(ada), grant.id), "keyExists")
    succeeded = portal.audits("credential.created.succeeded", grant)
    assert [item["poolId"] for item in succeeded] == [pool.id]
    assert succeeded[0]["subscriptionName"] == key
    assert succeeded[0]["costCenterId"] == GENERAL


async def test_the_holder_reveals_rotates_and_deletes_the_key(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    holder = _holder(ada)
    # There's nothing to reveal or rotate before the key exists.
    await _refused(portal.access.reveal_key(holder, grant.id, "primary"), "noKey")
    await _refused(portal.access.rotate_key(holder, grant.id, "primary"), "noKey")
    await _refused(portal.access.delete_key(holder, grant.id), "noKey")
    created = await portal.access.create_key(holder, grant.id)
    key = created.subscription_name

    revealed = await portal.access.reveal_key(holder, grant.id, "primary")

    assert revealed.key == KEY_VALUE
    assert revealed.subscription_name == key
    assert portal.reads == [(key, pool.api_name, "primary")]
    [succeeded] = portal.audits("credential.reveal.succeeded", grant)
    assert succeeded["poolId"] == pool.id and succeeded["subscriptionName"] == key

    rotated = await portal.access.rotate_key(holder, grant.id, "secondary")

    assert rotated.rotated == "secondary"
    assert portal.regenerated == [f"{key}/regenerateSecondaryKey"]

    deleted = await portal.access.delete_key(holder, grant.id)

    assert deleted.exists is False
    assert _subscription(key) in portal.apim.write_paths("DELETE")
    assert not portal.owned(await portal.pool(pool.id), key)
    assert (await portal.access.connection(holder, grant.id)).key_exists is False
    await _refused(portal.access.delete_key(holder, grant.id), "noKey")
    await _refused(portal.access.rotate_key(holder, grant.id, "primary"), "noKey")
    # A new one can be created after.
    await portal.access.create_key(holder, grant.id)
    assert portal.owned(await portal.pool(pool.id), key)


async def test_a_holder_shares_one_key_across_the_models_they_hold(portal: Portal) -> None:
    pool = await portal.create("OpenAI", _gpt4o("aoai-east"), MINI)
    pool = await portal.update(pool.id, governed_access=ModelAccessSettings())
    ada = await portal.principal("Ada")
    holder = _holder(ada)
    first = await portal.grant(pool, ada)
    second = await portal.grant_model(pool, ada, 1)
    await portal.publish(pool.id)
    pool = await portal.pool(pool.id)
    key = entitlement_key_name(pool, first)
    assert key is not None and key == entitlement_key_name(pool, second)
    before = await portal.access.connection(holder, second.id)
    assert [item.public_name for item in before.key_shared_with] == ["gpt-4o"]
    assert before.key_exists is False

    created = await portal.access.create_key(holder, first.id)

    assert created.subscription_name == key
    assert [(item.public_name, item.display_name) for item in created.key_shared_with] == [
        ("gpt-4o-mini", "GPT-4o mini")
    ]
    after = await portal.access.connection(holder, second.id)
    assert after.deployment_name == "gpt-4o-mini"
    assert after.key_exists is True
    assert [item.pool_model_id for item in after.key_shared_with] == [pool.models[0].id]
    refusal = await _refused(portal.access.create_key(holder, second.id), "keyExists")
    assert "shares with gpt-4o." in refusal.message
    # The other grant reveals the same key.
    revealed = await portal.access.reveal_key(holder, second.id, "primary")
    assert revealed.subscription_name == key
    assert portal.reads == [(key, pool.api_name, "primary")]
    assert len([path for path in portal.apim.write_paths("PUT") if path == _subscription(key)]) == 1


async def test_keys_follow_the_cost_center(portal: Portal) -> None:
    await portal.cost_centers.create_cost_center(
        general_cost_center(TENANT).model_copy(update={"keys_allowed": False}), _audit()
    )
    pool, ada, grant = await _published(portal)

    connection = await portal.access.connection(_holder(ada), grant.id)
    assert connection.keys_allowed_by_cost_center is False
    await _refused(portal.access.create_key(_holder(ada), grant.id), "costCenterKeysOff")
    key = entitlement_key_name(pool, grant)
    assert key is not None and _subscription(key) not in portal.apim.written


async def test_a_key_mosaic_didnt_create_is_left_alone(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    key = entitlement_key_name(pool, grant)
    assert key is not None
    portal.apim.seed(
        _subscription(key),
        {
            "properties": {
                "scope": f"{RESOURCE_ID}/apis/{pool.api_name}",
                "state": "active",
                "displayName": "Someone else",
            }
        },
    )

    await _refused(portal.access.create_key(_holder(ada), grant.id), "keyNotOwned")

    assert portal.apim.written[_subscription(key)]["properties"]["displayName"] == "Someone else"
    assert not portal.owned(await portal.pool(pool.id), key)


async def test_a_key_scoped_elsewhere_is_neither_rotated_nor_deleted(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    key = entitlement_key_name(pool, grant)
    assert key is not None
    await portal.issue_key(pool.id, key, scope=f"{RESOURCE_ID}/apis/another-api")

    await _refused(portal.access.rotate_key(_holder(ada), grant.id, "primary"), "keyScopeChanged")
    await _refused(portal.access.delete_key(_holder(ada), grant.id), "keyScopeChanged")

    assert portal.regenerated == []
    assert _subscription(key) not in portal.apim.write_paths("DELETE")


async def test_a_missing_key_is_deleted_from_the_record_but_not_rotated(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    key = entitlement_key_name(pool, grant)
    assert key is not None
    await portal.issue_key(pool.id, key)
    del portal.apim.written[_subscription(key)]

    await _refused(portal.access.rotate_key(_holder(ada), grant.id, "primary"), "keyMissing")
    deleted = await portal.access.delete_key(_holder(ada), grant.id)

    assert deleted.exists is False
    assert not portal.owned(await portal.pool(pool.id), key)


async def test_nothing_changes_a_key_while_the_pool_is_locked(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    holder = _holder(ada)
    created = await portal.access.create_key(holder, grant.id)
    await portal.gateway_repository.acquire_publication_lock(TENANT, pool.id, "run-elsewhere")

    await _refused(
        portal.access.reveal_key(holder, grant.id, "primary"), match="key retrieval is blocked"
    )
    for attempt in (
        portal.access.rotate_key(holder, grant.id, "primary"),
        portal.access.delete_key(holder, grant.id),
    ):
        await _refused(attempt, match="already running")
    assert portal.reads == [] and portal.regenerated == []
    # Connection details don't need the lock.
    assert (await portal.access.connection(holder, grant.id)).key_exists is True

    await portal.gateway_repository.release_publication_lock(TENANT, pool.id, "run-elsewhere")
    revealed = await portal.access.reveal_key(holder, grant.id, "primary")
    assert revealed.subscription_name == created.subscription_name


async def test_a_pending_change_blocks_the_key_until_it_is_applied(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    holder = _holder(ada)
    await portal.access.create_key(holder, grant.id)
    await portal.change_grant(
        grant,
        enforcement=EntitlementEnforcement(
            tokens=TokenEnforcement(
                counter_key_expression="@(context.Subscription.Id)", tokens_per_minute=500
            )
        ),
    )

    for attempt in (
        portal.access.reveal_key(holder, grant.id, "primary"),
        portal.access.rotate_key(holder, grant.id, "primary"),
    ):
        await _refused(attempt, match="Apply the pending changes")

    await portal.publish(pool.id)
    await portal.access.rotate_key(holder, grant.id, "primary")
    assert len(portal.regenerated) == 1


async def test_no_one_manages_someone_elses_key(portal: Portal) -> None:
    pool, ada, grant = await _published(portal)
    stranger = Actor(object_id=str(uuid4()), tenant_id=TENANT)

    for attempt in (
        portal.access.connection(stranger, grant.id),
        portal.access.create_key(stranger, grant.id),
        portal.access.rotate_key(stranger, grant.id, "primary"),
        portal.access.delete_key(stranger, grant.id),
        portal.access.reveal_key(stranger, grant.id, "primary"),
    ):
        with pytest.raises(NotFoundError):
            await attempt
    key = entitlement_key_name(pool, grant)
    assert key is not None and not portal.owned(await portal.pool(pool.id), key)
    [denied] = portal.audits("credential.created.denied", grant)
    assert denied["poolId"] == pool.id
    assert portal.audits("credential.reveal.denied", grant)[0]["poolId"] == pool.id

    # An administrator manages the key for the grant's holder.
    created = await portal.access.create_key(ACTOR, grant.id, administrator=True)
    assert created.subscription_name == key
    assert (await portal.access.connection(_holder(ada), grant.id)).key_exists is True


async def test_a_security_group_grant_uses_entra_tokens_only(portal: Portal) -> None:
    pool = await portal.governed_pool()
    researchers = await portal.security_group("Researchers")
    grant = await portal.grant(pool, researchers)
    await portal.publish(pool.id)
    member = Actor(
        object_id=str(uuid4()),
        tenant_id=TENANT,
        group_ids=frozenset({researchers.object_id.lower()}),
    )

    connection = await portal.access.connection(member, grant.id)

    assert connection.keys_available is False
    assert connection.key_exists is False
    assert connection.key_shared_with == []
    assert (connection.via_group_id, connection.via_group_name) == (researchers.id, "Researchers")
    assert connection.deployment_name == "gpt-4o"
    await _refused(portal.access.create_key(member, grant.id), match="Entra tokens only")
    await _refused(portal.access.reveal_key(member, grant.id, "primary"), match="Entra tokens only")
    outsider = Actor(object_id=str(uuid4()), tenant_id=TENANT)
    with pytest.raises(NotFoundError):
        await portal.access.connection(outsider, grant.id)
