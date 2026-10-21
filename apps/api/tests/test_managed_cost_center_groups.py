"""The experimental group foundation shares the existing roster and never enables access."""

from collections.abc import Iterator
from copy import deepcopy
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from azure.cosmos.aio import CosmosClient
from fastapi.testclient import TestClient
from mosaic_api.auth import LocalAuthenticator
from mosaic_api.config import AuthMode, Environment, RepositoryBackend, Settings
from mosaic_api.cost_centers import CostCenter, CostCenterBook, CostCenterMember, CostCenterView
from mosaic_api.domain import (
    AuditEvent,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    Group,
    GroupPurpose,
    ModelApi,
    Principal,
    general_cost_center_id,
    new_id,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.main import create_app
from mosaic_api.managed_cost_center_groups import (
    cost_center_group_id,
    group_membership,
    linked_cost_center,
)
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    InMemoryCostCenterRepository,
    InMemoryDirectoryRepository,
)
from mosaic_api.repositories.cosmos import CosmosDirectoryRepository
from mosaic_api.repositories.cosmos_cost_centers import CosmosCostCenterRepository
from mosaic_api.services.model_access import cost_center_intent, entitlement_intent_digest
from test_model_access_repositories import Container, Cosmos

TENANT = "tenant-managed-groups"


def audit() -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=TENANT,
        actor_object_id="local-admin",
        action="test.managedGroup",
        resource_type="costCenter",
        resource_id="center",
    )


def center(**updates: Any) -> CostCenter:
    return CostCenter(
        id="costCenter_example", tenant_id=TENANT, name="Research", code="RND", **updates
    )


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
        managed_cost_center_groups=True,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def create_center(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/v1/cost-centers", json={"name": "Research", "code": "RND"})
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def principal(client: TestClient, kind: str = "user") -> str:
    response = client.post(
        "/api/v1/principals", json={"objectId": str(uuid4()), "kind": kind, "label": kind}
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["id"])


async def seed_model(client: TestClient) -> str:
    model = ModelApi(
        id="modelApi_managed",
        tenant_id=TENANT,
        gateway_id="gateway_seed",
        api_name="chat",
        display_name="Chat completions",
        path="chat",
        imported_from_snapshot_id="snapshot-1",
    )
    await client.app.state.gateway_repository.save_model_api(model, audit())
    return model.id


def repositories(backend: str) -> tuple[DirectoryRepository, CostCenterRepository]:
    if backend == "memory":
        directory = InMemoryDirectoryRepository()
        return directory, InMemoryCostCenterRepository(directory, managed_groups_enabled=True)
    cosmos = Cosmos()
    return (
        CosmosDirectoryRepository(cast(CosmosClient, cosmos), "mosaic", "desired", "audit"),
        CosmosCostCenterRepository(
            cast(CosmosClient, cosmos),
            "mosaic",
            "desired",
            "audit",
            managed_groups_enabled=True,
        ),
    )


def test_new_center_has_a_stable_owned_group_without_grants_or_native_keys(
    client: TestClient,
) -> None:
    created = create_center(client)
    group = client.get(f"/api/v1/groups/{created['groupId']}").json()
    assert group["purpose"] == "costCenter"
    assert group["costCenterId"] == created["id"]
    assert created["groupId"] == cost_center_group_id(TENANT, created["id"])
    assert created["membershipRevision"] == 1
    assert client.get("/api/v1/entitlements").json() == []
    assert client.get(f"/api/v1/groups/{created['groupId']}/members").json() == []


def test_both_membership_routes_share_one_roster_and_accept_entra_references(
    client: TestClient,
) -> None:
    created = create_center(client)
    user, security_group = principal(client), principal(client, "securityGroup")
    center_url = f"/api/v1/cost-centers/{created['id']}"
    group_url = f"/api/v1/groups/{created['groupId']}"
    added = client.put(f"{group_url}/members/{security_group}")
    assert added.status_code == 201, added.text
    assert client.put(f"{center_url}/members/{user}").status_code == 200
    view = client.get(center_url).json()
    assert {item["principalId"] for item in view["members"]} == {user, security_group}
    assert {item["principalId"] for item in client.get(f"{group_url}/members").json()} == {
        user,
        security_group,
    }
    revision = view["membershipRevision"]
    repeated = client.put(f"{group_url}/members/{security_group}")
    assert repeated.status_code == 200
    assert repeated.json() == added.json()
    assert client.get(center_url).json()["membershipRevision"] == revision
    assert client.delete(f"{group_url}/members/{security_group}").status_code == 204
    assert [item["principalId"] for item in client.get(center_url).json()["members"]] == [user]
    assert client.delete(f"{center_url}/members/{user}").status_code == 200
    assert client.get(f"{group_url}/members").json() == []
    directory: InMemoryDirectoryRepository = client.app.state.repository
    assert directory.memberships == {}


def test_implicit_default_charge_eligibility_is_not_managed_group_membership(
    client: TestClient,
) -> None:
    created = create_center(client)
    user = principal(client)
    response = client.patch(
        f"/api/v1/principals/{user}", json={"defaultCostCenterId": created["id"]}
    )
    assert response.status_code == 200, response.text
    assert client.get(f"/api/v1/cost-centers/{created['id']}").json()["memberDetails"]
    assert client.get(f"/api/v1/groups/{created['groupId']}/members").json() == []


def test_only_owner_can_rename_or_delete_managed_group(client: TestClient) -> None:
    created = create_center(client)
    group_url = f"/api/v1/groups/{created['groupId']}"
    changed = client.patch(group_url, json={"description": "Independent change"})
    assert changed.status_code == 409, changed.text
    assert changed.json()["details"]["costCenterId"] == created["id"]
    assert client.delete(group_url).status_code == 409
    renamed = client.patch(
        f"/api/v1/cost-centers/{created['id']}", json={"name": "New research name"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["groupId"] == created["groupId"]
    assert renamed.json()["membershipRevision"] == created["membershipRevision"]
    assert client.get(group_url).json()["name"] == "Cost center: New research name"
    assert client.delete(f"/api/v1/cost-centers/{created['id']}").status_code == 204
    assert client.get(group_url).status_code == 404


def test_generic_groups_still_reject_security_groups(client: TestClient) -> None:
    created = client.post("/api/v1/groups", json={"name": "Unmanaged"}).json()
    security_group = principal(client, "securityGroup")
    response = client.put(f"/api/v1/groups/{created['id']}/members/{security_group}")
    assert response.status_code == 422, response.text
    assert client.get(f"/api/v1/groups/{created['id']}").json()["purpose"] == "local"


async def test_legacy_linkage_is_idempotent_and_preserves_roster_and_attribution(
    client: TestClient,
) -> None:
    repository: InMemoryCostCenterRepository = client.app.state.cost_center_repository
    original = center(
        members=[CostCenterMember(principal_id="principal_legacy", added_by="original-admin")]
    )
    repository.cost_centers[(TENANT, original.id)] = original.model_copy(
        update={"etag": "legacy-etag"}, deep=True
    )
    before = deepcopy(repository.cost_centers[(TENANT, original.id)])
    first = client.post(f"/api/v1/cost-centers/{original.id}/group")
    assert first.status_code == 200, first.text
    count = len(repository.audit_events)
    repeated = client.post(f"/api/v1/cost-centers/{original.id}/group")
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["groupId"] == first.json()["groupId"]
    assert len(repository.audit_events) == count
    stored = await repository.get_cost_center(TENANT, original.id)
    assert stored is not None
    assert stored.members == before.members
    assert stored.created_at == before.created_at
    assert stored.keys_allowed == before.keys_allowed
    assert stored.pending_rechecks == before.pending_rechecks


async def test_current_500_reference_boundary_applies_to_both_membership_routes(
    client: TestClient,
) -> None:
    created = create_center(client)
    repository: InMemoryCostCenterRepository = client.app.state.cost_center_repository
    stored = await repository.get_cost_center(TENANT, created["id"])
    assert stored is not None
    await repository.save_cost_center(
        stored.model_copy(
            update={"members": [CostCenterMember(principal_id=f"p-{i}") for i in range(499)]},
            deep=True,
        ),
        audit(),
    )
    last, extra = principal(client), principal(client)
    assert client.put(f"/api/v1/groups/{created['groupId']}/members/{last}").status_code == 201
    assert client.put(f"/api/v1/groups/{created['groupId']}/members/{extra}").status_code == 422
    assert client.put(f"/api/v1/cost-centers/{created['id']}/members/{extra}").status_code == 422
    members = client.get(f"/api/v1/groups/{created['groupId']}/members").json()
    assert len(members) == 500
    assert client.delete(f"/api/v1/cost-centers/{created['id']}/members/{last}").status_code == 200
    assert client.put(f"/api/v1/cost-centers/{created['id']}/members/{last}").status_code == 200
    assert len(client.get(f"/api/v1/groups/{created['groupId']}/members").json()) == 500


async def test_memory_collision_or_stale_save_changes_neither_roster_nor_audit() -> None:
    directory = InMemoryDirectoryRepository()
    repository = InMemoryCostCenterRepository(directory, managed_groups_enabled=True)
    identifier = cost_center_group_id(TENANT, center().id)
    directory.groups[identifier] = Group(id=identifier, tenant_id=TENANT, name="Unrelated")
    event = audit()
    with pytest.raises(ConflictError):
        await repository.create_cost_center(center(), event)
    assert repository.cost_centers == {}
    assert event.id not in repository.audit_events
    assert directory.groups[identifier].purpose == GroupPurpose.LOCAL
    del directory.groups[identifier]
    saved = await repository.create_cost_center(center(), audit())
    await repository.save_cost_center(saved.model_copy(update={"name": "Renamed"}), audit())
    event = audit()
    with pytest.raises(ConflictError):
        await repository.save_cost_center(saved, event)
    assert directory.groups[identifier].name == "Cost center: Renamed"
    assert event.id not in repository.audit_events


async def test_cosmos_owns_one_atomic_group_center_audit_batch_even_at_500_members() -> None:
    cosmos = Cosmos()
    repository = CosmosCostCenterRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", managed_groups_enabled=True
    )
    original = center(members=[CostCenterMember(principal_id=f"p-{i}") for i in range(500)])
    saved = await repository.create_cost_center(original, audit())
    container = cosmos.containers["desired"]
    assert len(container.batches[0][1]) == 3
    assert saved.group_id is not None
    stored = await repository.get_cost_center(TENANT, saved.id)
    assert stored is not None
    await repository.save_cost_center(
        stored.model_copy(update={"members": stored.members[:-1]}, deep=True), audit()
    )
    assert len(container.batches[-1][1]) == 3
    latest = await repository.get_cost_center(TENANT, saved.id)
    assert latest is not None
    assert latest.membership_revision == 2
    before = deepcopy(container.items)
    event = audit()
    with pytest.raises(ConflictError):
        await repository.save_cost_center(stored, event)
    assert container.items == before


async def test_cosmos_collision_rolls_back_center_and_audit_without_adoption() -> None:
    cosmos = Cosmos()
    repository = CosmosCostCenterRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", managed_groups_enabled=True
    )
    identifier = cost_center_group_id(TENANT, center().id)
    container = cosmos.containers["desired"]
    await container.create_item(
        Group(id=identifier, tenant_id=TENANT, name="Unrelated").model_dump(
            mode="json", by_alias=True
        )
    )
    before = deepcopy(container.items)
    with pytest.raises(ConflictError):
        await repository.create_cost_center(center(), audit())
    assert container.items == before


async def test_cosmos_managed_deletion_is_etag_guarded_for_both_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cosmos = Cosmos()
    repository = CosmosCostCenterRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", managed_groups_enabled=True
    )
    await repository.create_cost_center(center(), audit())
    stored = await repository.get_cost_center(TENANT, center().id)
    assert stored is not None
    execute = AsyncMock(wraps=repository._execute_batch)
    monkeypatch.setattr(repository, "_execute_batch", execute)
    await repository.delete_cost_center(stored, audit())
    operations = execute.call_args.args[0]
    assert [operation[0] for operation in operations] == ["delete", "delete", "create"]
    assert operations[0][2]["if_match_etag"] == stored.etag
    assert operations[1][2]["if_match_etag"]
    assert await repository.get_cost_center(TENANT, stored.id) is None
    assert cosmos.containers["desired"].items == {}


def test_feature_is_default_off_and_legacy_center_has_no_automatic_access(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MOSAIC_MANAGED_COST_CENTER_GROUPS", raising=False)
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )
    assert settings.managed_cost_center_groups is False
    with TestClient(create_app(settings)) as client:
        created = create_center(client)
        assert created["groupId"] is None
        assert client.post(f"/api/v1/cost-centers/{created['id']}/group").status_code == 422
        assert client.get("/api/v1/groups").json() == []


def test_experimental_flag_can_be_enabled_with_its_documented_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MOSAIC_MANAGED_COST_CENTER_GROUPS", "true")
    settings = Settings(
        _env_file=None,
        environment=Environment.TEST,
        auth_mode=AuthMode.LOCAL,
        repository_backend=RepositoryBackend.MEMORY,
        tenant_id=TENANT,
    )
    assert settings.managed_cost_center_groups is True


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_directory_repository_cannot_edit_managed_metadata_or_create_a_second_roster(
    backend: str,
) -> None:
    directory, centers = repositories(backend)
    created = await centers.create_cost_center(center(), audit())
    assert created.group_id is not None
    group = await directory.get_group(TENANT, created.group_id)
    assert group is not None
    forged = Group(id=group.id, tenant_id=TENANT, name="Independent", etag=group.etag)
    user = Principal(id="principal_test", tenant_id=TENANT, object_id=str(uuid4()), kind="user")
    membership = group_membership(group, CostCenterMember(principal_id=user.id))
    operations = [
        lambda: directory.save_group(forged, audit()),
        lambda: directory.delete_group(forged, audit()),
        lambda: directory.create_group(group, audit()),
        lambda: directory.create_membership(membership, forged, user, audit()),
        lambda: directory.delete_membership(membership, audit()),
    ]
    for operation in operations:
        with pytest.raises(ConflictError):
            await operation()
    assert await directory.get_group(TENANT, group.id) == group
    if isinstance(directory, InMemoryDirectoryRepository):
        assert directory.memberships == {}
    else:
        assert isinstance(directory, CosmosDirectoryRepository)
        container = cast(Container, directory._desired)
        assert all(item["entityType"] != "groupMembership" for item in container.items.values())
    assert await centers.get_cost_center("another-tenant", created.id) is None
    assert await directory.get_group("another-tenant", group.id) is None


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_missing_group_does_not_silently_reappear_on_a_save(backend: str) -> None:
    directory, centers = repositories(backend)
    created = await centers.create_cost_center(center(), audit())
    stored = await centers.get_cost_center(TENANT, created.id)
    assert stored is not None and stored.group_id is not None
    if isinstance(directory, InMemoryDirectoryRepository):
        del directory.groups[stored.group_id]
    else:
        assert isinstance(directory, CosmosDirectoryRepository)
        await directory._desired.delete_item(item=stored.group_id, partition_key=TENANT)
    with pytest.raises(ConflictError, match="missing"):
        await centers.save_cost_center(stored.model_copy(update={"name": "Updated"}), audit())
    assert await centers.get_cost_center(TENANT, stored.id) == stored
    assert await directory.get_group(TENANT, stored.group_id) is None


@pytest.mark.parametrize("backend", ["memory", "cosmos"])
async def test_link_cannot_be_replaced_or_removed_even_with_a_current_etag(backend: str) -> None:
    directory, centers = repositories(backend)
    created = await centers.create_cost_center(center(), audit())
    stored = await centers.get_cost_center(TENANT, created.id)
    assert stored is not None and stored.group_id is not None
    unlinked = stored.model_copy(update={"group_id": None})
    with pytest.raises(ConflictError):
        await centers.save_cost_center(unlinked, audit())
    with pytest.raises(ConflictError):
        await centers.delete_cost_center(unlinked, audit())
    assert await centers.get_cost_center(TENANT, stored.id) == stored
    assert await directory.get_group(TENANT, stored.group_id) is not None


async def test_cosmos_legacy_linkage_keeps_500_references_in_one_three_operation_batch() -> None:
    cosmos = Cosmos()
    legacy = CosmosCostCenterRepository(cast(CosmosClient, cosmos), "mosaic", "desired", "audit")
    original = center(members=[CostCenterMember(principal_id=f"p-{i}") for i in range(500)])
    await legacy.create_cost_center(original, audit())
    stored = await legacy.get_cost_center(TENANT, original.id)
    assert stored is not None
    managed = CosmosCostCenterRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", managed_groups_enabled=True
    )
    await managed.save_cost_center(linked_cost_center(stored), audit())
    latest = await managed.get_cost_center(TENANT, original.id)
    assert latest is not None
    assert latest.members == stored.members
    assert latest.membership_revision == 1
    assert len(cosmos.containers["desired"].batches[-1][1]) == 3


async def test_cosmos_concurrent_group_update_rolls_back_center_and_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cosmos = Cosmos()
    repository = CosmosCostCenterRepository(
        cast(CosmosClient, cosmos), "mosaic", "desired", "audit", managed_groups_enabled=True
    )
    await repository.create_cost_center(center(), audit())
    stored = await repository.get_cost_center(TENANT, center().id)
    assert stored is not None and stored.group_id is not None
    container = cosmos.containers["desired"]
    execute = container.execute_item_batch

    async def concurrent_group_write(**kwargs: Any) -> list[dict[str, Any]]:
        container.items[(TENANT, stored.group_id)]["_etag"] = "concurrent-group-etag"
        return await execute(**kwargs)

    monkeypatch.setattr(container, "execute_item_batch", concurrent_group_write)
    event = audit()
    with pytest.raises(ConflictError):
        await repository.save_cost_center(stored.model_copy(update={"name": "Updated"}), event)
    assert await repository.get_cost_center(TENANT, stored.id) == stored
    assert (TENANT, event.id) not in container.items
    assert (TENANT, event.id) not in cosmos.containers["audit"].items


def test_legacy_linkage_does_not_change_existing_grant_intent() -> None:
    original = center()
    person = Principal(id="p", tenant_id=TENANT, object_id=str(uuid4()), kind="user")
    grant = Entitlement(
        id="entitlement_test",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="user", id=person.id),
        resource=EntitlementResource(kind="modelApi", id="modelApi_test"),
        cost_center_id=original.id,
    )
    before = CostCenterBook(TENANT, [original], None)
    after = CostCenterBook(TENANT, [linked_cost_center(original)], None)
    assert entitlement_intent_digest(
        grant, person, cost_center_intent(grant, person, before)
    ) == entitlement_intent_digest(grant, person, cost_center_intent(grant, person, after))


def test_matching_display_name_is_never_adopted(client: TestClient) -> None:
    unrelated = client.post("/api/v1/groups", json={"name": "Cost center: Research"}).json()
    created = create_center(client)
    assert created["groupId"] != unrelated["id"]
    assert client.get(f"/api/v1/groups/{unrelated['id']}").json()["purpose"] == "local"
    assert len(client.get("/api/v1/groups").json()) == 2


async def test_group_removal_preserves_charge_rechecks_and_resets_the_personal_default(
    client: TestClient,
) -> None:
    created, user = create_center(client), principal(client)
    resource_id = await seed_model(client)
    center_url = f"/api/v1/cost-centers/{created['id']}"
    assert client.put(f"{center_url}/members/{user}").status_code == 200
    assert (
        client.patch(
            f"/api/v1/principals/{user}", json={"defaultCostCenterId": created["id"]}
        ).status_code
        == 200
    )
    granted = client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "user", "id": user},
            "resource": {"kind": "modelApi", "id": resource_id},
            "costCenterId": created["id"],
        },
    )
    assert granted.status_code == 201, granted.text
    assert client.delete(f"/api/v1/groups/{created['groupId']}/members/{user}").status_code == 204
    revoked = client.get(f"/api/v1/entitlements/{granted.json()['id']}").json()
    assert revoked["enabled"] is False
    assert revoked["revocation"]["costCenterId"] == created["id"]
    assert client.get(f"/api/v1/principals/{user}").json()["defaultCostCenterId"] is None


async def test_group_subject_grants_under_another_center_prevent_owner_deletion(
    client: TestClient,
) -> None:
    created = create_center(client)
    resource_id = await seed_model(client)
    grant = Entitlement(
        id="entitlement_previous_binding",
        tenant_id=TENANT,
        subject=EntitlementSubject(kind="group", id=created["groupId"]),
        resource=EntitlementResource(kind="modelApi", id=resource_id),
        cost_center_id=general_cost_center_id(TENANT),
    )
    await client.app.state.entitlement_repository.create_entitlement(grant, audit())
    response = client.delete(f"/api/v1/cost-centers/{created['id']}")
    assert response.status_code == 409, response.text
    assert response.json()["details"]["reason"] == "groupHasGrants"
    assert client.get(f"/api/v1/groups/{created['groupId']}").status_code == 200


async def test_managed_group_is_not_a_backdoor_to_legacy_group_grant_resolution(
    client: TestClient,
) -> None:
    created = create_center(client)
    resource_id = await seed_model(client)
    response = client.post(
        "/api/v1/entitlements",
        json={
            "subject": {"kind": "group", "id": created["groupId"]},
            "resource": {"kind": "modelApi", "id": resource_id},
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["details"]["reason"] == "managedCostCenterAccessUnavailable"
    assert client.get("/api/v1/entitlements").json() == []


def test_linkage_and_group_membership_remain_admin_only(client: TestClient) -> None:
    created, user = create_center(client), principal(client)
    client.app.state.authenticator = LocalAuthenticator(TENANT, ["User"])
    assert client.post(f"/api/v1/cost-centers/{created['id']}/group").status_code == 403
    assert client.put(f"/api/v1/groups/{created['groupId']}/members/{user}").status_code == 403


def test_idempotent_linkage_reports_missing_metadata_instead_of_claiming_success(
    client: TestClient,
) -> None:
    created = create_center(client)
    del client.app.state.repository.groups[created["groupId"]]
    response = client.post(f"/api/v1/cost-centers/{created['id']}/group")
    assert response.status_code == 409, response.text


def test_generic_orphan_memberships_keep_the_existing_lookup_and_removal_behavior(
    client: TestClient,
) -> None:
    group = client.post("/api/v1/groups", json={"name": "Legacy group"}).json()
    user = principal(client)
    url = f"/api/v1/groups/{group['id']}/members"
    assert client.put(f"{url}/{user}").status_code == 201
    del client.app.state.repository.groups[group["id"]]
    listed = client.get(url)
    assert listed.status_code == 404
    assert client.delete(f"{url}/{user}").status_code == 204
    assert client.app.state.repository.memberships == {}


def test_concurrent_removal_is_a_conflict_not_a_missing_member_server_error(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created, user = create_center(client), principal(client)
    after_removal = CostCenterView.model_validate(created)
    monkeypatch.setattr(
        client.app.state.cost_center_service,
        "add_member_with_status",
        AsyncMock(return_value=(after_removal, True)),
    )
    response = client.put(f"/api/v1/groups/{created['groupId']}/members/{user}")
    assert response.status_code == 409, response.text
    assert response.json()["details"]["principalId"] == user


@pytest.mark.parametrize("duplicate", [False, True])
def test_legacy_linkage_rejects_oversized_or_duplicate_references(duplicate: bool) -> None:
    invalid = center(
        members=[
            CostCenterMember(principal_id="duplicate" if duplicate else f"p-{i}")
            for i in range(2 if duplicate else 501)
        ]
    )
    with pytest.raises(ConflictError if duplicate else ValidationError):
        linked_cost_center(invalid)
