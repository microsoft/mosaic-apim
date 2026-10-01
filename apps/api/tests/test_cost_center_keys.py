"""Keys on request: a grant's holder, or an administrator, creates, rotates and deletes its key.

An apply never creates a key. The subscription's name is fixed by the grant, so the gateway's
policy recognizes a key the moment it's created, with nothing applied again. See ADR 0022.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from mosaic_api.cost_centers import CostCenterCreate, CostCenterUpdate
from mosaic_api.domain import (
    Entitlement,
    EntitlementCreate,
    EntitlementResource,
    EntitlementSubject,
    PrincipalCreate,
    PublishedResourceKind,
    PublishRunStatus,
    general_cost_center_id,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import grant_key_display_name
from test_governed_lifecycle import ACTOR, APPLICATION, NEW_USER, TENANT, USER, Harness

OWNER = Actor(USER, TENANT)
STRANGER = Actor(NEW_USER, TENANT)


@pytest.fixture
async def harness() -> AsyncIterator[Harness]:
    instance = Harness()
    await instance.setup()
    yield instance
    await instance.close()


async def _applied(harness: Harness) -> None:
    assert (await harness.apply()).status == PublishRunStatus.SUCCEEDED


def _path(harness: Harness, grant: Entitlement) -> str:
    return f"subscriptions/{harness.subscription(grant)}"


async def _owned(harness: Harness, grant: Entitlement) -> bool:
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    return any(
        item.kind == PublishedResourceKind.SUBSCRIPTION and item.name == harness.subscription(grant)
        for item in publication.resources
    )


async def test_an_apply_creates_no_key_and_the_holder_creates_one(harness: Harness) -> None:
    grant = await harness.grant()
    await harness.govern()
    await _applied(harness)
    assert _path(harness, grant) not in harness.apim.written
    assert not await _owned(harness, grant)

    created = await harness.keys().create_key(OWNER, grant.id)

    assert created.exists is True
    assert created.cost_center is not None and created.cost_center.code == "general"
    properties = harness.apim.written[_path(harness, grant)]["properties"]
    assert properties["state"] == "active"
    assert properties["displayName"] == f"Principal {USER} (general)"
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert properties["scope"].endswith(f"/apis/{publication.api_name}")
    assert await _owned(harness, grant)
    # Nothing is applied again: the policy already names the key's subscription.
    assert publication.access_state == "applied"
    loaded = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert loaded.runtime is not None and loaded.runtime.key_exists is True
    with pytest.raises(ConflictError, match="already has a key"):
        await harness.keys().create_key(OWNER, grant.id)
    # The next apply keeps it, suspending it only while the policy is replaced.
    await _applied(harness)
    assert harness.apim.written[_path(harness, grant)]["properties"]["state"] == "active"


async def test_the_holder_rotates_a_slot_and_deletes_the_key(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    grant = await harness.grant()
    await harness.govern()
    await _applied(harness)
    keys = harness.keys()
    await keys.create_key(OWNER, grant.id)
    original = harness.apim.handler
    actions: list[str] = []

    def regenerate(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and "/regenerate" in request.url.path:
            actions.append("/".join(request.url.path.rsplit("/", 2)[-2:]))
            return httpx.Response(204)
        return original(request)

    monkeypatch.setattr(harness.apim, "handler", regenerate)

    rotated = await keys.rotate_key(OWNER, grant.id, "secondary")
    assert rotated.rotated == "secondary"
    assert actions == [f"{harness.subscription(grant)}/regenerateSecondaryKey"]

    deleted = await keys.delete_key(OWNER, grant.id)
    assert deleted.exists is False
    assert _path(harness, grant) in harness.apim.write_paths("DELETE")
    assert not await _owned(harness, grant)
    loaded = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert loaded.runtime is not None and loaded.runtime.key_exists is False
    with pytest.raises(ConflictError, match="no key"):
        await keys.rotate_key(OWNER, grant.id, "primary")
    with pytest.raises(ConflictError, match="no key"):
        await keys.delete_key(OWNER, grant.id)
    # A new one can be created after.
    await keys.create_key(OWNER, grant.id)
    assert await _owned(harness, grant)


async def test_no_one_manages_someone_elses_key(harness: Harness) -> None:
    grant = await harness.grant()
    application = await harness.grant(APPLICATION, application=True)
    await harness.govern()
    await _applied(harness)
    keys = harness.keys()

    for attempt in (
        keys.create_key(STRANGER, grant.id),
        keys.rotate_key(STRANGER, grant.id, "primary"),
        keys.delete_key(STRANGER, grant.id),
        # An application's key is the administrator's to manage, not a person's.
        keys.create_key(OWNER, application.id),
    ):
        with pytest.raises(NotFoundError):
            await attempt
    assert not await _owned(harness, grant)
    assert not await _owned(harness, application)
    denied = [
        event
        for event in harness.entitlements.audit_events.values()
        if event.action == "credential.created.denied"
    ]
    assert {event.resource_id for event in denied} == {grant.id, application.id}

    await keys.create_key(ACTOR, application.id, administrator=True)
    assert await _owned(harness, application)


async def test_keys_need_the_publication_and_the_cost_center_to_allow_them(
    harness: Harness,
) -> None:
    grant = await harness.grant()
    await harness.govern(keys_enabled=False, entra_enabled=True)
    await _applied(harness)
    with pytest.raises(ConflictError, match="Key access requires"):
        await harness.keys().create_key(OWNER, grant.id)

    await harness.govern()
    await _applied(harness)
    await harness.keys().create_key(OWNER, grant.id)
    general = general_cost_center_id(TENANT)
    await harness.cost_centers.update_cost_center(
        ACTOR, general, CostCenterUpdate(keys_allowed=False)
    )
    # Turning keys off is a change to the grant, applied like any other.
    pending = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert pending.runtime is not None and pending.runtime.status == "pending"

    await _applied(harness)

    # The key is suspended, kept for when keys are allowed again, and no new one is made.
    assert harness.apim.written[_path(harness, grant)]["properties"]["state"] == "suspended"
    publication = await harness.service.get_publication(ACTOR, harness.publication_id)
    assert publication.applied_access is not None
    [applied] = publication.applied_access.grants
    assert applied.keys_allowed is False
    await harness.keys().delete_key(OWNER, grant.id)
    with pytest.raises(ConflictError) as refused:
        await harness.keys().create_key(OWNER, grant.id)
    assert refused.value.details["reason"] == "costCenterKeysOff"


async def test_leaving_a_cost_center_deletes_the_key_on_the_next_apply(
    harness: Harness,
) -> None:
    research = await harness.cost_centers.create_cost_center(
        ACTOR, CostCenterCreate(name="Research", code="RES")
    )
    principal = await harness.directory_service.create_principal(
        ACTOR, PrincipalCreate(object_id=USER, kind="user", label="Ana")
    )
    await harness.cost_centers.add_member(ACTOR, research.id, principal.id)
    grant = await harness.grants.create_entitlement(
        ACTOR,
        EntitlementCreate(
            subject=EntitlementSubject(kind="user", id=principal.id),
            resource=EntitlementResource(kind="modelApi", id=harness.model_id),
            cost_center_id=research.id,
        ),
    )
    await harness.govern()
    await _applied(harness)
    created = await harness.keys().create_key(OWNER, grant.id)
    assert created.cost_center is not None and created.cost_center.code == "RES"
    assert harness.apim.written[_path(harness, grant)]["properties"]["displayName"] == "Ana (RES)"

    await harness.cost_centers.remove_member(ACTOR, research.id, principal.id)
    revoked = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert revoked.revocation is not None
    assert revoked.runtime is not None and revoked.runtime.status == "revocationPending"

    await _applied(harness)

    assert _path(harness, grant) in harness.apim.write_paths("DELETE")
    assert _path(harness, grant) not in harness.apim.written
    assert not await _owned(harness, grant)
    final = await harness.grants.get_entitlement(ACTOR, grant.id)
    assert final.runtime is not None and final.runtime.status == "revoked"


def test_a_keys_name_always_ends_with_its_whole_cost_center_code() -> None:
    assert grant_key_display_name("Ada Lovelace", "RES") == "Ada Lovelace (RES)"
    assert grant_key_display_name("Ada Lovelace", "") == "Ada Lovelace"

    code = "c" * 64
    name = grant_key_display_name("x" * 200, code)
    assert len(name) == 100
    assert name.endswith(f" ({code})")
    assert grant_key_display_name("y" * 200, "") == "y" * 100
