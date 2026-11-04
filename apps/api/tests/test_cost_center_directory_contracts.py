"""Cost-center Entra identities and conditional writes, without directory/network calls."""

import asyncio
from collections.abc import AsyncIterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

import pytest
from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient
from mosaic_api.cost_center_directory import (
    AttachCostCenterGroup,
    CostCenterDirectoryOperation,
    CostCenterGroupReservation,
    CreateCostCenterGroup,
    EntraGroupBinding,
    cost_center_group_reservation_id,
    cost_center_member_operation_id,
    cost_center_provisioning_operation_id,
)
from mosaic_api.cost_centers import (
    CostCenter,
    CostCenterCreate,
    CostCenterLimit,
    CostCenterLimitsUpdate,
    CostCenterResourceAccess,
    CostCenterUpdate,
    PooledQuota,
    general_cost_center,
)
from mosaic_api.domain import AuditEvent, EntitlementResource, new_id, utc_now
from mosaic_api.errors import ConflictError
from mosaic_api.repositories import CostCenterRepository, InMemoryCostCenterRepository
from mosaic_api.repositories.cosmos_cost_centers import CosmosCostCenterRepository
from test_model_access_repositories import Container, Cosmos

TENANT = "mosaic-directory-tests"
DIRECTORY = "11111111-1111-4111-8111-111111111111"
OTHER_DIRECTORY = "22222222-2222-4222-8222-222222222222"
GROUP = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER_GROUP = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
MEMBER = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"


def audit(tenant_id: str = TENANT) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=tenant_id,
        actor_object_id="fictional-admin",
        action="costCenter.directoryTest",
        resource_type="costCenter",
        resource_id="center",
    )


def unavailable() -> exceptions.CosmosHttpResponseError:
    return exceptions.CosmosHttpResponseError(status_code=503)  # type: ignore[no-untyped-call]


def binding(**updates: Any) -> EntraGroupBinding:
    return EntraGroupBinding.model_validate(
        {
            "directory_tenant_id": DIRECTORY,
            "group_object_id": GROUP,
            "provenance": "attached",
            "verification_state": "verified",
            "verified_at": utc_now(),
            **updates,
        }
    )


def center(center_id: str = "center", **updates: Any) -> CostCenter:
    return CostCenter.model_validate(
        {"id": center_id, "tenant_id": TENANT, "name": center_id, "code": center_id, **updates}
    )


def reservation(center_id: str = "center", **updates: Any) -> CostCenterGroupReservation:
    payload = {
        "tenant_id": TENANT,
        "cost_center_id": center_id,
        "directory_tenant_id": DIRECTORY,
        "group_object_id": GROUP,
        **updates,
    }
    return CostCenterGroupReservation.model_validate(
        {
            **payload,
            "id": cost_center_group_reservation_id(
                payload["tenant_id"], payload["directory_tenant_id"], payload["group_object_id"]
            ),
        }
    )


def provisioning(center_id: str = "center", **updates: Any) -> CostCenterDirectoryOperation:
    return CostCenterDirectoryOperation.model_validate(
        {
            "id": cost_center_provisioning_operation_id(TENANT, center_id),
            "tenant_id": TENANT,
            "cost_center_id": center_id,
            "kind": "createGroup",
            "directory_tenant_id": DIRECTORY,
            "display_name": "Fictional research",
            "requested_by": "fictional-admin",
            **updates,
        }
    )


def member_operation(
    center_id: str = "center", key: str = "request-1", **updates: Any
) -> CostCenterDirectoryOperation:
    return CostCenterDirectoryOperation.model_validate(
        {
            "id": cost_center_member_operation_id(TENANT, center_id, key),
            "tenant_id": TENANT,
            "cost_center_id": center_id,
            "kind": "addMember",
            "directory_tenant_id": DIRECTORY,
            "group_object_id": GROUP,
            "member_object_id": MEMBER,
            "member_kind": "servicePrincipal",
            "requested_by": "fictional-admin",
            **updates,
        }
    )


class DirectoryContainer(Container):
    def __init__(self) -> None:
        super().__init__()
        self.query_count = 0
        self.waiting = 0
        self.barrier: asyncio.Event | None = None

    def synchronize_next_two_writes(self) -> None:
        self.waiting = 0
        self.barrier = asyncio.Event()

    async def execute_item_batch(
        self, *, batch_operations: list[tuple[Any, ...]], partition_key: str
    ) -> list[dict[str, Any]]:
        barrier = self.barrier
        if barrier is not None:
            self.waiting += 1
            if self.waiting == 2:
                self.barrier = None
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=5)
        return await super().execute_item_batch(
            batch_operations=batch_operations, partition_key=partition_key
        )

    async def query_items(
        self, *, query: str, parameters: list[dict[str, Any]], partition_key: str
    ) -> AsyncIterator[dict[str, Any]]:
        self.query_count += 1
        values = {item["name"]: item["value"] for item in parameters}
        items = [
            deepcopy(item)
            for (tenant_id, _), item in self.items.items()
            if tenant_id == partition_key
            and item.get("entityType") == values["@entityType"]
            and (
                "@costCenterId" not in values
                or item.get("costCenterId") == values["@costCenterId"]
            )
        ]
        if "ORDER BY" in query:
            items.sort(key=lambda item: (item["updatedAt"], item["id"]), reverse=True)
        for item in items[: values.get("@limit", len(items))]:
            yield item


@dataclass
class Repositories:
    repository: CostCenterRepository
    other: CostCenterRepository
    cosmos: Cosmos | None = None
    desired: DirectoryContainer | None = None

    def race(self) -> None:
        if self.desired is not None:
            self.desired.synchronize_next_two_writes()

    def snapshot(self) -> object:
        if self.cosmos is not None:
            return deepcopy(
                (
                    self.cosmos.containers["desired"].items,
                    self.cosmos.containers["audit"].items,
                )
            )
        memory = cast(InMemoryCostCenterRepository, self.repository)
        return deepcopy(
            (
                memory.cost_centers,
                memory.group_reservations,
                memory.directory_operations,
                memory.audit_events,
            )
        )


@pytest.fixture(params=["memory", "cosmos"])
def repositories(request: pytest.FixtureRequest) -> Repositories:
    if request.param == "memory":
        memory = InMemoryCostCenterRepository()
        return Repositories(memory, memory)
    cosmos = Cosmos()
    desired = DirectoryContainer()
    cosmos.containers["desired"] = desired
    return Repositories(
        CosmosCostCenterRepository(cast(CosmosClient, cosmos), "db", "desired", "audit"),
        CosmosCostCenterRepository(cast(CosmosClient, cosmos), "db", "desired", "audit"),
        cosmos,
        desired,
    )


async def stored_center(
    repository: CostCenterRepository, value: CostCenter | None = None
) -> CostCenter:
    value = value or center()
    await repository.create_cost_center(value, audit(value.tenant_id))
    stored = await repository.get_cost_center(value.tenant_id, value.id)
    assert stored is not None and stored.etag
    return stored


def test_setup_uses_verified_binding_or_stable_general_identity() -> None:
    general = general_cost_center(TENANT)
    renamed = CostCenter.model_validate(
        {**general.model_dump(by_alias=False), "built_in": False, "name": "Renamed", "code": "new"}
    )
    assert renamed.is_general and renamed.setup_state == "exempt"
    assert center(built_in=True).setup_state == "setupRequired"
    assert center(setup_state="ready").setup_state == "setupRequired"
    assert center(entra_binding=binding()).setup_state == "ready"
    assert center(
        entra_binding=binding(verification_state="failed", verification_error="Consent missing")
    ).setup_state == "setupRequired"
    assert center(entra_binding=binding()).verified_group_object_id == GROUP
    assert center().verified_group_object_id is None


def test_legacy_local_group_does_not_become_an_entra_binding() -> None:
    old = center(group_id="costCenterGroup_legacy", members=[{"principalId": "local"}])
    assert old.entra_binding is None and old.setup_state == "setupRequired"
    with pytest.raises(ValueError, match="legacy local"):
        center(group_id="costCenterGroup_legacy", entra_binding=binding())
    with pytest.raises(ValueError, match="legacy local"):
        center(members=[{"principalId": "local"}], entra_binding=binding())
    with pytest.raises(ValueError, match="exempt"):
        center(general_cost_center(TENANT).id, entra_binding=binding())


def test_directory_binding_canonicalizes_guids_and_requires_verification_evidence() -> None:
    assert binding(group_object_id=GROUP.upper()).group_object_id == GROUP
    with pytest.raises(ValueError, match="GUID"):
        binding(group_object_id="costCenterGroup_local")
    with pytest.raises(ValueError, match="verifiedAt"):
        binding(verified_at=None)
    with pytest.raises(ValueError, match="verification error"):
        binding(verification_error="Not verified")


def test_group_setup_is_optional_and_discriminated_not_a_client_tenant_or_url() -> None:
    assert CostCenterCreate(name="Draft", code="draft").group_setup is None
    attach = CostCenterCreate.model_validate(
        {
            "name": "Research",
            "code": "research",
            "groupSetup": {
                "mode": "attach",
                "groupObjectId": GROUP,
                "acknowledgeMembershipChanges": True,
            },
        }
    )
    assert isinstance(attach.group_setup, AttachCostCenterGroup)
    create = CostCenterUpdate.model_validate({"groupSetup": {"mode": "create"}})
    assert isinstance(create.group_setup, CreateCostCenterGroup)
    with pytest.raises(ValueError):
        CostCenterUpdate.model_validate(
            {"groupSetup": {"mode": "attach", "groupObjectId": GROUP}}
        )
    with pytest.raises(ValueError):
        CostCenterUpdate.model_validate(
            {
                "groupSetup": {
                    "mode": "attach",
                    "groupObjectId": GROUP,
                    "acknowledgeMembershipChanges": False,
                }
            }
        )
    with pytest.raises(ValueError):
        CostCenterUpdate.model_validate(
            {"groupSetup": {"mode": "create", "directoryTenantId": OTHER_DIRECTORY}}
        )


def test_resource_modes_and_limits_use_the_exact_pool_and_model_identity() -> None:
    first = EntitlementResource(kind="poolModel", id="same-model", scope_id="pool-one")
    second = EntitlementResource(kind="poolModel", id="same-model", scope_id="pool-two")
    definitions = [
        CostCenterResourceAccess(resource=first, mode="automatic"),
        CostCenterResourceAccess(resource=second, mode="requestOnly"),
    ]
    configured = center(resource_access=definitions)
    assert configured.access_mode_for(first) == "automatic"
    assert configured.access_mode_for(second) == "requestOnly"
    assert configured.access_mode_for(EntitlementResource(kind="mcpServer", id="other")) == (
        "notAvailable"
    )
    with pytest.raises(ValueError, match="once"):
        center(resource_access=[definitions[0], definitions[0]])
    with pytest.raises(ValueError):
        CostCenterResourceAccess.model_validate(
            {"resource": {"kind": "poolModel", "id": "same-model"}, "mode": "automatic"}
        )
    with pytest.raises(ValueError, match="exact model"):
        CostCenterResourceAccess(
            resource=EntitlementResource(kind="product", id="all", scope_id="gateway"),
            mode="automatic",
        )
    with pytest.raises(ValueError, match="scopeId"):
        CostCenterResourceAccess(
            resource=EntitlementResource(kind="modelApi", id="model", scope_id="pool"),
            mode="automatic",
        )
    limits = [
        CostCenterLimit(resource=first, pool=PooledQuota(monthly_calls=10)),
        CostCenterLimit(resource=second, pool=PooledQuota(monthly_calls=20)),
    ]
    assert CostCenterLimitsUpdate(limits=limits).limits == limits
    configured = center(limits=limits)
    assert configured.limit_for(first) == limits[0]
    assert configured.limit_for(second) == limits[1]


def test_provisioning_identity_and_outcomes_do_not_allow_unsafe_create_replays() -> None:
    with pytest.raises(ValueError, match="stable operation"):
        provisioning(id="arbitrary-create-operation")
    with pytest.raises(ValueError, match="observed group"):
        provisioning(state="succeeded")
    with pytest.raises(ValueError, match="safe to create"):
        provisioning(state="failed", group_object_id=GROUP)
    with pytest.raises(ValueError, match="member object"):
        member_operation(member_object_id=None)
    assert provisioning(state="unknown", group_object_id=GROUP).group_object_id == GROUP
    assert "members" not in CostCenterDirectoryOperation.model_fields


async def test_reservations_are_exclusive_conditional_and_retry_safe(
    repositories: Repositories,
) -> None:
    repositories.race()
    outcomes = await asyncio.gather(
        repositories.repository.reserve_group(reservation("one"), audit()),
        repositories.other.reserve_group(reservation("two"), audit()),
        return_exceptions=True,
    )
    winners = [item for item in outcomes if isinstance(item, CostCenterGroupReservation)]
    assert len(winners) == 1
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    winner = winners[0]
    before = repositories.snapshot()
    repeated = await repositories.other.reserve_group(reservation(winner.cost_center_id), audit())
    assert repeated.etag == winner.etag
    assert repositories.snapshot() == before
    if repositories.desired is not None:
        assert repositories.desired.query_count == 0


async def test_reservation_scope_includes_mosaic_tenant_directory_and_group(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await repo.reserve_group(reservation("one"), audit())
    await repo.reserve_group(
        reservation("two", directory_tenant_id=OTHER_DIRECTORY), audit()
    )
    await repo.reserve_group(reservation("three", group_object_id=OTHER_GROUP), audit())
    await repo.reserve_group(reservation("four", tenant_id="other"), audit("other"))
    assert await repo.get_group_reservation(TENANT, DIRECTORY, GROUP.upper()) is not None
    assert await repo.get_group_reservation("absent", DIRECTORY, GROUP) is None


async def test_concurrent_same_owner_reservation_retries_return_one_durable_record(
    repositories: Repositories,
) -> None:
    repositories.race()
    first, second = await asyncio.gather(
        repositories.repository.reserve_group(reservation(), audit()),
        repositories.other.reserve_group(reservation(), audit()),
    )
    assert first.etag == second.etag and first.created_at == second.created_at
    if repositories.cosmos is not None:
        assert len(repositories.cosmos.containers["audit"].items) == 1
    else:
        memory = cast(InMemoryCostCenterRepository, repositories.repository)
        assert len(memory.group_reservations) == 1 and len(memory.audit_events) == 1


@pytest.mark.parametrize("save_existing", [False, True])
async def test_concurrent_bindings_commit_only_one_center_and_its_audit(
    repositories: Repositories, save_existing: bool
) -> None:
    repo, other = repositories.repository, repositories.other
    first, second = center("one"), center("two")
    if save_existing:
        first, second = await stored_center(repo, first), await stored_center(repo, second)
    repositories.race()
    method = "save_cost_center" if save_existing else "create_cost_center"
    outcomes = await asyncio.gather(
        getattr(repo, method)(first.model_copy(update={"entra_binding": binding()}), audit()),
        getattr(other, method)(second.model_copy(update={"entra_binding": binding()}), audit()),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CostCenter) for item in outcomes) == 1
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    reserved = await repo.get_group_reservation(TENANT, DIRECTORY, GROUP)
    assert reserved is not None
    for center_id in ("one", "two"):
        found = await repo.get_cost_center(TENANT, center_id)
        if center_id == reserved.cost_center_id:
            assert found is not None and found.setup_state == "ready"
        elif save_existing:
            assert found is not None and found.setup_state == "setupRequired"
        else:
            assert found is None


async def test_partial_reservation_is_reused_and_cannot_be_bypassed_by_a_binding(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await repo.reserve_group(reservation(), audit())
    before = repositories.snapshot()
    with pytest.raises(ConflictError):
        await repo.create_cost_center(center("other", entra_binding=binding()), audit())
    assert repositories.snapshot() == before
    saved = await stored_center(repo)
    bound = await repo.save_cost_center(
        saved.model_copy(update={"entra_binding": binding()}), audit()
    )
    assert bound.setup_state == "ready" and bound.etag


async def test_bindings_cannot_be_stolen_released_or_rebound_and_deletion_keeps_reservation(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    saved = await stored_center(repo, center(entra_binding=binding()))
    for next_binding in (None, binding(group_object_id=OTHER_GROUP), binding(provenance="created")):
        before = repositories.snapshot()
        with pytest.raises(ConflictError, match="released or replaced"):
            await repo.save_cost_center(
                saved.model_copy(update={"entra_binding": next_binding}), audit()
            )
        assert repositories.snapshot() == before
    await repo.delete_cost_center(saved, audit())
    assert await repo.get_group_reservation(TENANT, DIRECTORY, GROUP) is not None
    with pytest.raises(ConflictError):
        await repo.create_cost_center(center("other", entra_binding=binding()), audit())


async def test_center_etag_guards_binding_and_does_not_store_a_forged_ready_state(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    original = await stored_center(repo)
    await repo.save_cost_center(original.model_copy(update={"name": "Changed"}), audit())
    before = repositories.snapshot()
    with pytest.raises(ConflictError):
        await repo.save_cost_center(
            original.model_copy(update={"entra_binding": binding()}), audit()
        )
    assert repositories.snapshot() == before
    current = await repo.get_cost_center(TENANT, original.id)
    assert current is not None
    await repo.save_cost_center(current.model_copy(update={"setup_state": "ready"}), audit())
    current = await repo.get_cost_center(TENANT, original.id)
    assert current is not None and current.setup_state == "setupRequired"


async def test_provisioning_requires_saved_custom_center_and_duplicate_intent_never_admits_io(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    with pytest.raises(ConflictError, match="Save the cost center"):
        await repo.create_directory_operation(provisioning(), audit())
    await stored_center(repo)
    repositories.race()
    outcomes = await asyncio.gather(
        repo.create_directory_operation(provisioning(), audit()),
        repositories.other.create_directory_operation(provisioning(), audit()),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CostCenterDirectoryOperation) for item in outcomes) == 1
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1
    general = await stored_center(repo, general_cost_center(TENANT))
    with pytest.raises(ConflictError, match="General"):
        await repo.create_directory_operation(provisioning(general.id), audit())


async def test_legacy_roster_needs_explicit_reconfiguration_before_group_creation(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await stored_center(repo, center(members=[{"principalId": "legacy-local-member"}]))
    before = repositories.snapshot()
    with pytest.raises(ConflictError, match="reconfigure or recreate"):
        await repo.create_directory_operation(provisioning(), audit())
    assert repositories.snapshot() == before


async def test_unknown_create_is_reconciled_not_retried_and_then_binds_its_actual_result(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await stored_center(repo)
    operation = await repo.create_directory_operation(provisioning(), audit())
    uncertain = await repo.save_directory_operation(
        operation.model_copy(update={"state": "unknown", "error_message": "Timed out"}), audit()
    )
    before = repositories.snapshot()
    with pytest.raises(ConflictError):
        await repo.create_directory_operation(provisioning(), audit())
    with pytest.raises(ConflictError, match="unknown outcome"):
        await repo.save_directory_operation(
            uncertain.model_copy(update={"state": "pending", "error_message": None}), audit()
        )
    assert repositories.snapshot() == before
    saved = await repo.get_cost_center(TENANT, "center")
    assert saved is not None
    with pytest.raises(ConflictError, match="Reconcile"):
        await repo.save_cost_center(saved.model_copy(update={"entra_binding": binding()}), audit())
    succeeded = await repo.save_directory_operation(
        uncertain.model_copy(
            update={"state": "succeeded", "group_object_id": GROUP, "error_message": None}
        ),
        audit(),
    )
    before = repositories.snapshot()
    repeated = await repo.save_directory_operation(succeeded, audit())
    assert repeated.etag == succeeded.etag and repositories.snapshot() == before
    with pytest.raises(ConflictError, match="returned"):
        await repo.save_cost_center(saved.model_copy(update={"entra_binding": binding()}), audit())
    bound = await repo.save_cost_center(
        saved.model_copy(update={"entra_binding": binding(provenance="created")}), audit()
    )
    assert bound.setup_state == "ready" and bound.verified_group_object_id == GROUP


async def test_definite_failure_can_retry_once_with_current_etag_but_not_changed_intent(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await stored_center(repo)
    operation = await repo.create_directory_operation(provisioning(), audit())
    failed = await repo.save_directory_operation(
        operation.model_copy(update={"state": "failed", "error_code": "AuthorizationDenied"}),
        audit(),
    )
    retry = failed.model_copy(update={"state": "pending", "error_code": None})
    pending = await repo.save_directory_operation(retry, audit())
    assert pending.state == "pending" and pending.etag != failed.etag
    with pytest.raises(ConflictError):
        await repo.save_directory_operation(retry, audit())
    with pytest.raises(ConflictError, match="intent"):
        await repo.save_directory_operation(
            pending.model_copy(update={"state": "unknown", "display_name": "Different"}), audit()
        )


async def test_observed_create_identity_cannot_be_erased_or_swapped(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await stored_center(repo)
    operation = await repo.create_directory_operation(provisioning(), audit())
    uncertain = await repo.save_directory_operation(
        operation.model_copy(update={"state": "unknown", "group_object_id": GROUP}), audit()
    )
    for next_id in (None, OTHER_GROUP):
        with pytest.raises(ConflictError, match="identity"):
            await repo.save_directory_operation(
                uncertain.model_copy(update={"group_object_id": next_id}), audit()
            )
    with pytest.raises(ValueError, match="safe to create"):
        await repo.save_directory_operation(
            uncertain.model_copy(update={"state": "failed"}), audit()
        )


async def test_membership_intents_are_scoped_to_the_verified_binding_not_a_shadow_roster(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    draft = await stored_center(repo)
    with pytest.raises(ConflictError, match="verified"):
        await repo.create_directory_operation(member_operation(), audit())
    await repo.save_cost_center(draft.model_copy(update={"entra_binding": binding()}), audit())
    for changes in (
        {"group_object_id": OTHER_GROUP},
        {"directory_tenant_id": OTHER_DIRECTORY},
    ):
        with pytest.raises(ConflictError, match="verified"):
            await repo.create_directory_operation(member_operation(**changes), audit())
    operation = await repo.create_directory_operation(member_operation(), audit())
    result = operation.model_copy(update={"state": "succeeded"})
    stored = await repo.save_directory_operation(result, audit())
    repeated = await repo.save_directory_operation(result, audit())
    assert repeated.etag == stored.etag
    assert stored.member_kind == "servicePrincipal"
    current = await repo.get_cost_center(TENANT, "center")
    assert current is not None and current.members == [] and current.group_id is None
    assert await repo.get_directory_operation("other", operation.id) is None


async def test_concurrent_results_use_etags_and_do_not_lose_the_winning_outcome(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await stored_center(repo, center(entra_binding=binding()))
    operation = await repo.create_directory_operation(member_operation(), audit())
    repositories.race()
    outcomes = await asyncio.gather(
        repo.save_directory_operation(operation.model_copy(update={"state": "succeeded"}), audit()),
        repositories.other.save_directory_operation(
            operation.model_copy(update={"state": "unknown", "error_message": "Timeout"}), audit()
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(item, CostCenterDirectoryOperation) for item in outcomes) == 1
    assert sum(isinstance(item, ConflictError) for item in outcomes) == 1


async def test_operation_history_is_bounded_and_does_not_mix_centers_or_tenants(
    repositories: Repositories,
) -> None:
    repo = repositories.repository
    await stored_center(repo, center(entra_binding=binding()))
    now = utc_now()
    for index in range(3):
        await repo.create_directory_operation(
            member_operation(
                key=f"operation-{index}", updated_at=now + timedelta(seconds=index)
            ),
            audit(),
        )
    recent = await repo.list_directory_operations(TENANT, "center", limit=2)
    assert len(recent) == 2 and recent[0].updated_at > recent[1].updated_at
    assert await repo.list_directory_operations(TENANT, "absent") == []
    assert await repo.list_directory_operations("other", "center") == []
    with pytest.raises(ValueError, match="200"):
        await repo.list_directory_operations(TENANT, "center", limit=201)


async def test_cosmos_transaction_failure_preserves_binding_intent_result_and_audit() -> None:
    cosmos = Cosmos()
    repo = CosmosCostCenterRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")
    saved = await stored_center(repo)
    desired = cosmos.containers["desired"]
    before = deepcopy(desired.items)
    desired.failure = unavailable()
    with pytest.raises(exceptions.CosmosHttpResponseError):
        await repo.save_cost_center(saved.model_copy(update={"entra_binding": binding()}), audit())
    with pytest.raises(exceptions.CosmosHttpResponseError):
        await repo.create_directory_operation(provisioning(), audit())
    assert desired.items == before
    desired.failure = None
    operation = await repo.create_directory_operation(provisioning(), audit())
    before = deepcopy(desired.items)
    desired.failure = unavailable()
    with pytest.raises(exceptions.CosmosHttpResponseError):
        await repo.save_directory_operation(
            operation.model_copy(update={"state": "succeeded", "group_object_id": GROUP}), audit()
        )
    assert desired.items == before


async def test_directory_intent_keeps_auditable_outbox_when_projection_fails() -> None:
    cosmos = Cosmos()
    repo = CosmosCostCenterRepository(cast(CosmosClient, cosmos), "db", "desired", "audit")
    await stored_center(repo)
    cosmos.containers["audit"].failure = unavailable()
    event = audit()
    operation = await repo.create_directory_operation(provisioning(), event)
    assert operation.state == "pending"
    assert (TENANT, event.id) in cosmos.containers["desired"].items
    cosmos.containers["audit"].failure = None
    await repo._project_audit_event(event)
    assert (TENANT, event.id) not in cosmos.containers["desired"].items
    assert (TENANT, event.id) in cosmos.containers["audit"].items
