"""Real directory intent, recovery, membership and local-group isolation (fictional only)."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from cost_center_management_double import (
    ADMIN,
    ALL_WRITES,
    BASELINE,
    GROUP,
    OTHER_GROUP,
    TENANT,
    USER,
    WORKLOAD,
    Management,
    audit,
    management,
)
from mosaic_api.cost_center_directory import (
    AttachCostCenterGroup,
    CostCenterDirectoryOperation,
    CreateCostCenterGroup,
    cost_center_member_operation_id,
    cost_center_provisioning_operation_id,
)
from mosaic_api.cost_centers import (
    CostCenter,
    CostCenterCreate,
    CostCenterMember,
    CostCenterResourceAccess,
    CostCenterUpdate,
)
from mosaic_api.domain import (
    DirectoryObject,
    EntitlementResource,
    Group,
    Principal,
    PrincipalKind,
    general_cost_center_id,
    utc_now,
)
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    DirectoryError,
    DirectoryForbiddenError,
    ValidationError,
)
from mosaic_api.integrations.graph import (
    GraphGroup,
    GraphGroupManagement,
    GroupFailureKind,
    GroupManagementError,
    GroupWriteOutcome,
)
from mosaic_api.services.directory import Actor, DirectoryService
from test_graph_client import FakeCredential


@pytest.fixture(params=["memory", "cosmos"])
def estate(request: pytest.FixtureRequest) -> Management:
    return management(request.param)


async def attach(
    estate: Management, center_id: str = "center", group_id: str = GROUP
) -> CostCenter:
    return await estate.service.create(
        ADMIN,
        CostCenterCreate(
            name=center_id,
            code=center_id,
            group_setup=AttachCostCenterGroup(
                group_object_id=group_id, acknowledge_membership_changes=True
            ),
        ),
        cost_center_id=center_id,
    )


def failure(
    kind: GroupFailureKind = GroupFailureKind.FORBIDDEN,
    outcome: GroupWriteOutcome = GroupWriteOutcome.NOT_APPLIED,
) -> GroupManagementError:
    return GroupManagementError("Fictional Graph failure", kind=kind, outcome=outcome)


async def test_attach_reserves_and_verifies_without_creating_a_group_or_local_roster(
    estate: Management,
) -> None:
    created = await attach(estate)
    assert created.setup_state == "ready"
    assert created.entra_binding is not None
    assert created.entra_binding.provenance == "attached"
    assert created.verified_group_object_id == GROUP
    reservation = await estate.repository.get_group_reservation(TENANT, TENANT, GROUP)
    assert reservation is not None and reservation.cost_center_id == created.id
    assert created.members == [] and created.group_id is None
    assert estate.graph.writes == []
    assert estate.local_directory.groups == {} and estate.local_directory.memberships == {}
    assert estate.checks.synced == [created.id]


async def test_read_only_consent_does_not_make_a_verified_group_unconfigured() -> None:
    estate = management(permissions=BASELINE)
    created = await attach(estate)
    capabilities = await estate.service.group_capabilities(ADMIN)
    assert capabilities.available and capabilities.advisory and capabilities.can_read_groups
    assert not capabilities.can_create_groups and not capabilities.can_add_users
    assert not capabilities.can_add_service_principals and not capabilities.can_remove_members
    assert "preview" in capabilities.member_listing_notice
    operation = await estate.service.add_directory_member(
        ADMIN, created.id, USER, PrincipalKind.USER, idempotency_key="user-write"
    )
    assert operation.state == "failed" and operation.error_code == "forbidden"
    assert not operation.directory_updated
    assert (await estate.service.get_cost_center(ADMIN, created.id)).setup_state == "ready"
    assert estate.graph.writes == []


@pytest.mark.parametrize(
    "group",
    [
        GraphGroup(TENANT, GROUP, security_enabled=False),
        GraphGroup(TENANT, GROUP, mail_enabled=True),
        GraphGroup(TENANT, GROUP, group_types=("DynamicMembership",)),
        GraphGroup(TENANT, GROUP, is_assignable_to_role=True),
        GraphGroup(TENANT, GROUP, on_premises_sync_enabled=True),
        GraphGroup(TENANT, GROUP, on_premises_security_identifier="fictional-old-sync"),
    ],
)
async def test_unsupported_groups_remain_saved_but_setup_required(group: GraphGroup) -> None:
    estate = management(groups=(group,))
    created = await attach(estate)
    assert created.setup_state == "setupRequired"
    assert created.entra_binding is not None
    assert created.entra_binding.verification_state == "failed"
    assert created.entra_binding.verification_error
    assert not estate.checks.synced and not estate.graph.writes


async def test_attach_validation_failure_can_recover_only_the_same_reserved_group(
    estate: Management,
) -> None:
    estate.graph.failures["get_group"] = failure()
    created = await attach(estate)
    assert created.setup_state == "setupRequired"
    assert created.entra_binding is not None and created.entra_binding.verification_error
    with pytest.raises(ConflictError):
        await estate.service.update_cost_center(
            ADMIN,
            created.id,
            CostCenterUpdate(
                group_setup=AttachCostCenterGroup(
                    group_object_id=OTHER_GROUP, acknowledge_membership_changes=True
                )
            ),
        )
    del estate.graph.failures["get_group"]
    recovered = await estate.service.update_cost_center(
        ADMIN,
        created.id,
        CostCenterUpdate(
            group_setup=AttachCostCenterGroup(
                group_object_id=GROUP, acknowledge_membership_changes=True
            )
        ),
    )
    assert recovered.setup_state == "ready"
    assert recovered.verified_group_object_id == GROUP
    assert estate.graph.writes == []


async def test_group_create_persists_intent_and_actual_id_before_verification(
    estate: Management, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = estate.graph.validate_group

    async def verify(group_id: str) -> GraphGroup:
        operation = await estate.repository.get_directory_operation(
            TENANT, cost_center_provisioning_operation_id(TENANT, "created")
        )
        assert operation is not None and operation.state == "succeeded"
        assert operation.group_object_id == group_id
        current = await estate.repository.get_cost_center(TENANT, "created")
        assert current is not None and current.entra_binding is not None
        assert current.entra_binding.provenance == "created"
        return await original(group_id)

    monkeypatch.setattr(estate.graph, "validate_group", verify)
    created = await estate.service.create(
        ADMIN,
        CostCenterCreate(
            name="Research",
            code="created",
            owners=["contact@example.test"],
            group_setup=CreateCostCenterGroup(),
        ),
        cost_center_id="created",
    )
    assert created.setup_state == "ready"
    assert len(estate.graph.writes) == 1
    assert estate.graph.writes[0].operation == "create_group"
    assert created.members == []
    assert created.verified_group_object_id is not None
    assert estate.lookup.state.group_members[created.verified_group_object_id] == []
    assert estate.graph.owners[created.verified_group_object_id] == (
        "00000000-0000-0000-0000-000000000101",
    )


async def test_missing_create_consent_keeps_center_and_explicit_retry_uses_the_same_intent(
    estate: Management,
) -> None:
    estate.graph.failures["create_group"] = failure()
    request = CostCenterCreate(name="Draft", code="draft", group_setup=CreateCostCenterGroup())
    first = await estate.service.create(ADMIN, request, cost_center_id="draft")
    [operation] = first.directory_operations
    assert first.setup_state == "setupRequired" and first.entra_binding is None
    assert operation.state == "failed" and operation.can_retry
    assert operation.error_code == "forbidden"
    repeated = await estate.service.create(ADMIN, request, cost_center_id="draft")
    assert repeated.id == first.id and repeated.directory_operations[0].id == operation.id
    del estate.graph.failures["create_group"]
    retried = await estate.service.retry_directory_operation(ADMIN, first.id, operation.id)
    assert retried.state == "succeeded"
    assert (await estate.service.get_cost_center(ADMIN, first.id)).setup_state == "ready"
    assert len(estate.graph.writes) == 1


async def test_known_created_object_with_replication_failure_is_verified_not_created_again(
    estate: Management,
) -> None:
    estate.graph.failures["get_group"] = failure(GroupFailureKind.REPLICATION_PENDING)
    created = await estate.service.create(
        ADMIN,
        CostCenterCreate(name="Draft", code="draft", group_setup=CreateCostCenterGroup()),
        cost_center_id="draft",
    )
    [operation] = created.directory_operations
    assert operation.state == "succeeded"
    assert created.setup_state == "setupRequired"
    observed_id = operation.group_object_id
    del estate.graph.failures["get_group"]
    await estate.service.recover_directory_operation(ADMIN, created.id, operation.id)
    recovered = await estate.service.get_cost_center(ADMIN, created.id)
    assert recovered.verified_group_object_id == observed_id and recovered.setup_state == "ready"
    assert len(estate.graph.writes) == 1


async def test_uncertain_create_and_pending_create_never_admit_a_second_post(
    estate: Management,
) -> None:
    estate.graph.failures["create_group"] = failure(
        GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN
    )
    request = CostCenterCreate(name="Draft", code="draft", group_setup=CreateCostCenterGroup())
    first = await estate.service.create(ADMIN, request, cost_center_id="unknown")
    [operation] = first.directory_operations
    assert operation.state == "unknown" and not operation.can_retry
    estate.graph.failures.clear()
    # A same-name existing object is deliberately not adopted.
    estate.graph._groups[OTHER_GROUP] = GraphGroup(TENANT, OTHER_GROUP, "Draft")
    for _ in range(2):
        again = await estate.service.create(ADMIN, request, cost_center_id="unknown")
        assert again.setup_state == "setupRequired" and again.entra_binding is None
        recovered = await estate.service.recover_directory_operation(ADMIN, first.id, operation.id)
        assert recovered.state == "unknown" and recovered.group_object_id is None
        assert "display-name" in (recovered.recovery_notice or "")
    with pytest.raises(ConflictError, match="definitive failed"):
        await estate.service.retry_directory_operation(ADMIN, first.id, operation.id)
    with pytest.raises(ConflictError):
        await estate.service.update_cost_center(
            ADMIN,
            first.id,
            CostCenterUpdate(
                group_setup=AttachCostCenterGroup(
                    group_object_id=OTHER_GROUP, acknowledge_membership_changes=True
                )
            ),
        )
    assert estate.graph.writes == []
    pending = CostCenterDirectoryOperation(
        id=cost_center_provisioning_operation_id(TENANT, "pending"),
        tenant_id=TENANT,
        cost_center_id="pending",
        kind="createGroup",
        directory_tenant_id=TENANT,
        requested_by=ADMIN.object_id,
        display_name="Pending",
    )
    await estate.service.create(
        ADMIN, CostCenterCreate(name="Pending", code="pending"), cost_center_id="pending"
    )
    await estate.repository.create_directory_operation(pending, audit())
    await estate.service.update_cost_center(
        ADMIN, "pending", CostCenterUpdate(group_setup=CreateCostCenterGroup())
    )
    assert estate.graph.writes == []


async def test_forced_center_id_retry_cannot_overwrite_a_different_center(
    estate: Management,
) -> None:
    request = CostCenterCreate(name="Original", code="original")
    original = await estate.service.create(ADMIN, request, cost_center_id="reserved")
    assert (
        await estate.service.create(ADMIN, request, cost_center_id="reserved")
    ).id == original.id
    with pytest.raises(ConflictError, match="different"):
        await estate.service.create(
            ADMIN, CostCenterCreate(name="Other", code="other"), cost_center_id="reserved"
        )
    found = await estate.service.get_cost_center(ADMIN, original.id)
    assert found.name == "Original" and found.code == "original"
    assert not estate.graph.writes


@pytest.mark.parametrize("with_access", [False, True])
async def test_nil_group_setup_reserved_create_is_local_only_even_with_write_consent(
    estate: Management,
    with_access: bool,
) -> None:
    center = await estate.service.create(
        ADMIN,
        CostCenterCreate(
            name="Approval draft",
            code="approval",
            resource_access=[
                {"resource": {"kind": "modelApi", "id": "modelApi_seed"}, "mode": "automatic"}
            ]
            if with_access
            else [],
        ),
        cost_center_id="approval",
    )
    assert center.setup_state == "setupRequired" and center.entra_binding is None
    assert center.directory_operations == []
    assert estate.lookup.reads == 0 and not estate.graph.writes
    assert not estate.local_directory.groups and not estate.checks.synced


async def test_concurrent_provisioning_retries_share_one_pending_create(
    estate: Management, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = asyncio.Event(), asyncio.Event()
    original = estate.graph.create_group

    async def delayed(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.wait_for(release.wait(), timeout=10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(estate.graph, "create_group", delayed)
    request = CostCenterCreate(name="Concurrent", code="race", group_setup=CreateCostCenterGroup())
    first = asyncio.create_task(estate.service.create(ADMIN, request, cost_center_id="race"))
    await asyncio.wait_for(entered.wait(), timeout=10)
    try:
        duplicate = await estate.service.create(ADMIN, request, cost_center_id="race")
        assert duplicate.setup_state == "setupRequired"
        assert duplicate.directory_operations[0].state == "pending"
        assert not estate.gateways.scope_leases
    finally:
        release.set()
    result = await first
    assert result.setup_state == "ready" and len(estate.graph.writes) == 1


async def test_concurrent_group_bindings_have_only_one_owner(estate: Management) -> None:
    results = await asyncio.gather(
        attach(estate, "one"), attach(estate, "two"), return_exceptions=True
    )
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    reservation = await estate.repository.get_group_reservation(TENANT, TENANT, GROUP)
    assert reservation is not None
    centers = await estate.repository.list_cost_centers(TENANT)
    assert [item.id for item in centers if item.verified_group_object_id] == [
        reservation.cost_center_id
    ]
    assert len(centers) == 2  # Losing setup never loses its saved center.


@pytest.mark.parametrize("committed", [False, True])
async def test_lost_create_result_persistence_never_duplicates_and_retains_observed_identity(
    estate: Management, monkeypatch: pytest.MonkeyPatch, committed: bool
) -> None:
    original = estate.repository.save_directory_operation
    lost = False

    async def save(operation: CostCenterDirectoryOperation, event: Any) -> Any:
        nonlocal lost
        if operation.kind == "createGroup" and operation.state == "succeeded" and not lost:
            lost = True
            if committed:
                await original(operation, event)
            raise RuntimeError("Fictional lost persistence response")
        return await original(operation, event)

    monkeypatch.setattr(estate.repository, "save_directory_operation", save)
    request = CostCenterCreate(name="Recover", code="recover", group_setup=CreateCostCenterGroup())
    if committed:
        result = await estate.service.create(ADMIN, request, cost_center_id="recover")
        assert result.setup_state == "ready"
    else:
        with pytest.raises(ChangeNotRecordedError):
            await estate.service.create(ADMIN, request, cost_center_id="recover")
        operation_id = cost_center_provisioning_operation_id(TENANT, "recover")
        operation = await estate.repository.get_directory_operation(TENANT, operation_id)
        assert operation is not None and operation.state == "unknown"
        assert operation.group_object_id == estate.graph.writes[0].group_object_id
        await estate.service.recover_directory_operation(ADMIN, "recover", operation_id)
    result = await estate.service.get_cost_center(ADMIN, "recover")
    assert result.setup_state == "ready"
    assert result.verified_group_object_id == estate.graph.writes[0].group_object_id
    assert len(estate.graph.writes) == 1


async def test_crash_after_success_record_before_binding_recovers_exact_created_object(
    estate: Management, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = estate.repository.save_cost_center
    fail = True

    async def save(center: CostCenter, event: Any) -> CostCenter:
        nonlocal fail
        if center.entra_binding is not None and fail:
            fail = False
            raise RuntimeError("Fictional crash before binding")
        return await original(center, event)

    monkeypatch.setattr(estate.repository, "save_cost_center", save)
    with pytest.raises(ChangeNotRecordedError, match="setup could not finish"):
        await estate.service.create(
            ADMIN,
            CostCenterCreate(name="Crash", code="crash", group_setup=CreateCostCenterGroup()),
            cost_center_id="crash",
        )
    [operation] = await estate.service.list_directory_operations(ADMIN, "crash")
    assert operation.state == "succeeded" and operation.group_object_id
    await estate.service.recover_directory_operation(ADMIN, "crash", operation.id)
    result = await estate.service.get_cost_center(ADMIN, "crash")
    assert result.verified_group_object_id == operation.group_object_id
    assert len(estate.graph.writes) == 1


async def test_lost_intent_acknowledgment_does_not_execute_or_replay_a_create(
    estate: Management,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = estate.repository.create_directory_operation

    async def lost(operation: CostCenterDirectoryOperation, event: Any) -> Any:
        await original(operation, event)
        raise RuntimeError("Fictional intent acknowledgement lost")

    monkeypatch.setattr(estate.repository, "create_directory_operation", lost)
    request = CostCenterCreate(name="Intent", code="intent", group_setup=CreateCostCenterGroup())
    with pytest.raises(ChangeNotRecordedError, match="cost center is saved") as error:
        await estate.service.create(ADMIN, request, cost_center_id="intent")
    assert error.value.details["savedCostCenterId"] == "intent"
    repeated = await estate.service.create(ADMIN, request, cost_center_id="intent")
    assert repeated.directory_operations[0].state == "pending"
    assert estate.graph.writes == [] and estate.lookup.reads == 0


async def test_permanent_persistence_outage_leaves_pending_and_blocks_recreation(
    estate: Management, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = estate.repository.save_directory_operation
    monkeypatch.setattr(
        estate.repository,
        "save_directory_operation",
        AsyncMock(side_effect=RuntimeError("Fictional persistence outage")),
    )
    request = CostCenterCreate(name="Outage", code="outage", group_setup=CreateCostCenterGroup())
    with pytest.raises(ChangeNotRecordedError):
        await estate.service.create(ADMIN, request, cost_center_id="outage")
    monkeypatch.setattr(estate.repository, "save_directory_operation", original)
    retried = await estate.service.create(ADMIN, request, cost_center_id="outage")
    assert retried.setup_state == "setupRequired"
    assert retried.directory_operations[0].state == "pending"
    assert len(estate.graph.writes) == 1


async def test_untyped_domain_error_after_create_cannot_admit_another_post(
    estate: Management,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = estate.graph.create_group

    async def lost(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise DirectoryError("Fictional unclassified post-write failure")

    monkeypatch.setattr(estate.graph, "create_group", lost)
    request = CostCenterCreate(
        name="Unclassified",
        code="unclassified",
        group_setup=CreateCostCenterGroup(),
    )
    center = await estate.service.create(ADMIN, request, cost_center_id="unclassified")
    [operation] = center.directory_operations
    assert operation.state == "unknown" and not operation.can_retry
    assert not operation.directory_updated
    await estate.service.create(ADMIN, request, cost_center_id="unclassified")
    with pytest.raises(ConflictError, match="definitive failed"):
        await estate.service.retry_directory_operation(ADMIN, center.id, operation.id)
    assert len(estate.graph.writes) == 1


async def test_direct_paging_includes_workloads_and_nested_reference_not_transitive_roster(
    estate: Management,
) -> None:
    center = await attach(estate)
    people = [str(UUID(int=1000 + index)) for index in range(1500)]
    for index, object_id in enumerate(people):
        estate.lookup.state.objects[object_id] = DirectoryObject(
            object_id=object_id, kind="user", display_name=f"Fictional person {index}"
        )
    estate.lookup.state.group_members[GROUP] = [*people, WORKLOAD, OTHER_GROUP]
    estate.lookup.state.group_members[OTHER_GROUP] = [USER]
    first = await estate.service.list_directory_members(ADMIN, center.id, page_size=137)
    assert first.api_version == "beta" and "not supported for production" in first.preview_notice
    cursor, members = first.next_cursor, list(first.members)
    while cursor is not None:
        page = await estate.service.list_directory_members(
            ADMIN, center.id, page_size=137, cursor=cursor
        )
        members.extend(page.members)
        cursor = page.next_cursor
    assert len(members) == 1502
    assert {item.object_id for item in members} == {*people, WORKLOAD, OTHER_GROUP}
    assert all(item.membership_type == "direct" for item in members)
    assert next(item for item in members if item.object_id == WORKLOAD).kind == "servicePrincipal"
    calls = estate.lookup.reads
    await estate.service.list_cost_centers(ADMIN)
    detail = await estate.service.get_cost_center(ADMIN, center.id)
    assert estate.lookup.reads == calls and detail.members == [] and detail.member_details == []


async def test_member_writes_are_real_idempotent_and_do_not_sync_automatic_grants(
    estate: Management,
) -> None:
    center = await attach(estate)
    for object_id, kind in [
        (USER, PrincipalKind.USER),
        (WORKLOAD, PrincipalKind.SERVICE_PRINCIPAL),
    ]:
        operation = await estate.service.add_directory_member(
            ADMIN, center.id, object_id, kind, idempotency_key=object_id
        )
        repeated = await estate.service.add_directory_member(
            ADMIN, center.id, object_id, kind, idempotency_key=object_id
        )
        assert operation.state == repeated.state == "succeeded"
        assert operation.id == repeated.id and operation.directory_updated
        assert operation.propagation_notice and operation.key_revocation_notice is None
    assert len(estate.graph.writes) == 2
    assert estate.lookup.state.group_members[GROUP] == [USER, WORKLOAD]
    assert estate.local_directory.principals == {}  # No forced onboarding.
    assert estate.local_directory.groups == {} and estate.entitlements.entitlements == {}
    assert estate.checks.synced == [center.id]
    removed = await estate.service.remove_directory_member(
        ADMIN, center.id, USER, idempotency_key="remove-user"
    )
    assert removed.state == "succeeded" and removed.key_revocation_notice
    assert estate.lookup.state.group_members[GROUP] == [WORKLOAD]
    assert estate.checks.removed == [(center.id, USER, PrincipalKind.USER)]
    assert estate.checks.synced == [center.id]
    stored = await estate.repository.get_cost_center(TENANT, center.id)
    assert stored is not None and stored.members == [] and stored.membership_revision == 0


async def test_member_idempotency_key_cannot_change_target_or_kind(estate: Management) -> None:
    center = await attach(estate)
    await estate.service.add_directory_member(
        ADMIN, center.id, USER, PrincipalKind.USER, idempotency_key="one"
    )
    with pytest.raises(ConflictError, match="different membership"):
        await estate.service.add_directory_member(
            ADMIN, center.id, WORKLOAD, PrincipalKind.SERVICE_PRINCIPAL, idempotency_key="one"
        )
    with pytest.raises(ConflictError, match="different membership"):
        await estate.service.remove_directory_member(ADMIN, center.id, USER, idempotency_key="one")
    assert len(estate.graph.writes) == 1


async def test_member_kind_is_checked_server_side_not_trusted_from_input(
    estate: Management,
) -> None:
    center = await attach(estate)
    operation = await estate.service.add_directory_member(
        ADMIN, center.id, WORKLOAD, PrincipalKind.USER, idempotency_key="spoof-kind"
    )
    assert operation.state == "failed" and operation.error_code == "validation_error"
    assert estate.graph.writes == []


async def test_workload_write_consent_is_separate_from_user_write_consent() -> None:
    estate = management(permissions=(*BASELINE, "GroupMember.ReadWrite.All"))
    center = await attach(estate)
    caps = await estate.service.group_capabilities(ADMIN)
    assert caps.can_add_users and not caps.can_add_service_principals
    operation = await estate.service.add_directory_member(
        ADMIN, center.id, WORKLOAD, PrincipalKind.SERVICE_PRINCIPAL, idempotency_key="workload"
    )
    assert operation.state == "failed" and operation.error_code == "forbidden"
    assert "Application.ReadWrite.All" in (operation.error_message or "")
    assert estate.graph.writes == []


@pytest.mark.parametrize("kind", [GroupFailureKind.FORBIDDEN, GroupFailureKind.NOT_FOUND])
async def test_definitive_no_effect_removal_does_not_claim_success_or_schedule_rechecks(
    estate: Management, kind: GroupFailureKind
) -> None:
    center = await attach(estate)
    estate.graph.failures["remove_member"] = failure(kind)
    operation = await estate.service.remove_directory_member(
        ADMIN, center.id, USER, idempotency_key="denied-remove"
    )
    assert operation.state == "failed" and not operation.directory_updated
    assert operation.key_revocation_notice is None and operation.propagation_notice is None
    assert estate.checks.removed == [] and estate.graph.writes == []


async def test_uncertain_removal_schedules_individual_rechecks_and_recovers_without_replay(
    estate: Management,
) -> None:
    center = await attach(estate)
    estate.lookup.state.group_members[GROUP] = [USER]
    estate.graph.failures["remove_member"] = failure(
        GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN
    )
    operation = await estate.service.remove_directory_member(
        ADMIN, center.id, USER, idempotency_key="uncertain-remove"
    )
    assert operation.state == "unknown" and not operation.directory_updated
    assert operation.key_revocation_notice and operation.propagation_notice
    assert estate.checks.removed == [(center.id, USER, PrincipalKind.USER)]
    with pytest.raises(ConflictError, match="uncertain"):
        await estate.service.remove_directory_member(
            ADMIN, center.id, USER, idempotency_key="another-key"
        )
    estate.graph.failures.clear()
    # An administrator observes/finishes the external relationship change.
    estate.lookup.state.group_members[GROUP] = []
    recovered = await estate.service.recover_directory_operation(ADMIN, center.id, operation.id)
    assert recovered.state == "succeeded" and recovered.directory_updated
    assert estate.graph.writes == []


async def test_untyped_post_remove_error_still_schedules_individual_access_rechecks(
    estate: Management,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    center = await attach(estate)
    estate.lookup.state.group_members[GROUP] = [USER]
    original = estate.graph.remove_member

    async def lost(*args: Any, **kwargs: Any) -> Any:
        await original(*args, **kwargs)
        raise DirectoryError("Fictional unclassified removal result")

    monkeypatch.setattr(estate.graph, "remove_member", lost)
    operation = await estate.service.remove_directory_member(
        ADMIN,
        center.id,
        USER,
        idempotency_key="unclassified-remove",
    )
    assert operation.state == "unknown" and not operation.can_retry
    assert not operation.directory_updated
    assert estate.checks.removed == [(center.id, USER, PrincipalKind.USER)]
    assert len(estate.graph.writes) == 1
    recovered = await estate.service.recover_directory_operation(
        ADMIN,
        center.id,
        operation.id,
    )
    assert recovered.state == "succeeded" and len(estate.graph.writes) == 1


async def test_old_unknown_member_intent_cannot_hide_behind_visible_history(
    estate: Management,
) -> None:
    center = await attach(estate)
    estate.graph.failures["add_member"] = failure(
        GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN
    )
    uncertain = await estate.service.add_directory_member(
        ADMIN, center.id, USER, PrincipalKind.USER, idempotency_key="old"
    )
    for index in range(205):
        operation = CostCenterDirectoryOperation(
            id=cost_center_member_operation_id(TENANT, center.id, f"history-{index}"),
            tenant_id=TENANT,
            cost_center_id=center.id,
            kind="addMember",
            directory_tenant_id=TENANT,
            group_object_id=GROUP,
            member_object_id=WORKLOAD,
            member_kind="servicePrincipal",
            requested_by=ADMIN.object_id,
            updated_at=utc_now() + timedelta(seconds=index),
        )
        operation = await estate.repository.create_directory_operation(operation, audit())
        await estate.repository.save_directory_operation(
            operation.model_copy(update={"state": "failed", "error_code": "forbidden"}), audit()
        )
    with pytest.raises(ConflictError) as error:
        await estate.service.add_directory_member(
            ADMIN, center.id, USER, PrincipalKind.USER, idempotency_key="new-key"
        )
    assert error.value.details["operationId"] == uncertain.id
    assert len(await estate.service.list_directory_operations(ADMIN, center.id, limit=200)) == 200


async def test_post_remove_persistence_failure_still_schedules_individual_recheck(
    estate: Management, monkeypatch: pytest.MonkeyPatch
) -> None:
    center = await attach(estate)
    estate.lookup.state.group_members[GROUP] = [USER]
    original = estate.repository.save_directory_operation
    monkeypatch.setattr(
        estate.repository,
        "save_directory_operation",
        AsyncMock(side_effect=RuntimeError("Fictional persistence outage")),
    )
    with pytest.raises(ChangeNotRecordedError):
        await estate.service.remove_directory_member(
            ADMIN, center.id, USER, idempotency_key="lost-remove-result"
        )
    assert estate.checks.removed == [(center.id, USER, PrincipalKind.USER)]
    assert estate.lookup.state.group_members[GROUP] == []
    monkeypatch.setattr(estate.repository, "save_directory_operation", original)
    repeated = await estate.service.remove_directory_member(
        ADMIN, center.id, USER, idempotency_key="lost-remove-result"
    )
    assert repeated.state == "pending" and not repeated.directory_updated
    assert len(estate.graph.writes) == 1


async def test_removal_recheck_hook_can_resume_after_directory_success(
    estate: Management,
) -> None:
    center = await attach(estate)
    estate.lookup.state.group_members[GROUP] = [USER]
    estate.checks.removal_error = RuntimeError("Fictional recheck outage")
    with pytest.raises(ChangeNotRecordedError, match="rechecks"):
        await estate.service.remove_directory_member(
            ADMIN, center.id, USER, idempotency_key="remove-hook"
        )
    estate.checks.removal_error = None
    repeated = await estate.service.remove_directory_member(
        ADMIN, center.id, USER, idempotency_key="remove-hook"
    )
    assert repeated.state == "succeeded" and estate.checks.removed
    assert len(estate.graph.writes) == 1


async def test_member_editor_rejects_changed_group_type_without_deconfiguring_binding(
    estate: Management,
) -> None:
    center = await attach(estate)
    estate.graph._groups[GROUP] = replace(estate.graph._groups[GROUP], is_assignable_to_role=True)
    operation = await estate.service.add_directory_member(
        ADMIN, center.id, USER, PrincipalKind.USER, idempotency_key="unsupported"
    )
    assert operation.state == "failed" and operation.error_code == "unsupported_group"
    assert (await estate.service.get_cost_center(ADMIN, center.id)).setup_state == "ready"
    assert estate.graph.writes == []


async def test_actor_tenant_and_operation_scope_cannot_select_an_arbitrary_group(
    estate: Management,
) -> None:
    center = await attach(estate)
    other = await attach(estate, "other", OTHER_GROUP)
    before = estate.lookup.reads
    with pytest.raises(DirectoryForbiddenError):
        await estate.service.add_directory_member(
            Actor(object_id="foreign-admin", tenant_id=str(uuid4())),
            center.id,
            USER,
            PrincipalKind.USER,
            idempotency_key="wrong-tenant",
        )
    assert estate.lookup.reads == before and not estate.graph.writes
    operation = await estate.service.add_directory_member(
        ADMIN, center.id, USER, PrincipalKind.USER, idempotency_key="correct"
    )
    with pytest.raises(Exception, match="not found"):
        await estate.service.recover_directory_operation(ADMIN, other.id, operation.id)


async def test_resource_modes_validate_before_save_and_sync_after_save_outside_leases(
    estate: Management,
) -> None:
    center = await attach(estate)
    access = [
        CostCenterResourceAccess(
            resource=EntitlementResource(kind="poolModel", id="same", scope_id="pool-one"),
            mode="requestOnly",
        ),
        CostCenterResourceAccess(
            resource=EntitlementResource(kind="poolModel", id="same", scope_id="pool-two"),
            mode="notAvailable",
        ),
    ]
    estate.checks.validation_error = ValidationError("Fictional token capability failure")
    with pytest.raises(ValidationError):
        await estate.service.update_cost_center(
            ADMIN, center.id, CostCenterUpdate(resource_access=access)
        )
    unchanged = await estate.repository.get_cost_center(TENANT, center.id)
    assert unchanged is not None and unchanged.resource_access == []
    estate.checks.validation_error = None
    updated = await estate.service.update_cost_center(
        ADMIN, center.id, CostCenterUpdate(resource_access=access)
    )
    assert updated.resource_access == access
    assert estate.checks.validated[-1].resource_access == access
    assert estate.checks.synced == [center.id, center.id]
    assert estate.graph.writes == []


async def test_deleting_center_keeps_directory_group_and_exclusive_reservation(
    estate: Management,
) -> None:
    center = await attach(estate)
    await estate.service.delete_cost_center(ADMIN, center.id)
    assert (await estate.graph.get_group(GROUP)).object_id == GROUP
    reservation = await estate.repository.get_group_reservation(TENANT, TENANT, GROUP)
    assert reservation is not None and reservation.cost_center_id == center.id
    with pytest.raises(ConflictError):
        await attach(estate, "cannot-reparent", GROUP)
    assert estate.graph.writes == []


@pytest.mark.parametrize(
    "method",
    [
        "list_publications",
        "list_mcp_publications",
        "list_model_pools",
    ],
)
async def test_applied_grants_prevent_deletion_even_after_desired_grants_disappear(
    estate: Management,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
) -> None:
    center = await attach(estate)
    applied = SimpleNamespace(
        id="publication",
        applied_access=SimpleNamespace(grants=[SimpleNamespace(cost_center_id=center.id)]),
    )
    monkeypatch.setattr(estate.gateways, method, AsyncMock(return_value=[applied]))
    with pytest.raises(ConflictError, match="Revoke and apply") as error:
        await estate.service.delete_cost_center(ADMIN, center.id)
    assert error.value.details["reason"] == "hasAppliedGrants"
    assert await estate.repository.get_cost_center(TENANT, center.id) is not None
    reservation = await estate.repository.get_group_reservation(TENANT, TENANT, GROUP)
    assert reservation is not None and reservation.cost_center_id == center.id


async def test_unsettled_directory_operation_prevents_deletion(estate: Management) -> None:
    estate.graph.failures["create_group"] = failure(
        GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN
    )
    center = await estate.service.create(
        ADMIN, CostCenterCreate(name="Unknown", code="unknown", group_setup=CreateCostCenterGroup())
    )
    with pytest.raises(ConflictError, match="pending or unknown"):
        await estate.service.delete_cost_center(ADMIN, center.id)


async def test_general_exemption_is_stable_and_never_admits_setup_or_member_writes(
    estate: Management,
) -> None:
    general_id = general_cost_center_id(TENANT)
    renamed = await estate.service.update_cost_center(
        ADMIN, general_id, CostCenterUpdate(name="Shared", code="shared")
    )
    assert renamed.setup_state == "exempt"
    for setup in [
        CreateCostCenterGroup(),
        AttachCostCenterGroup(group_object_id=GROUP, acknowledge_membership_changes=True),
    ]:
        with pytest.raises(ValidationError, match="General"):
            await estate.service.update_cost_center(
                ADMIN, general_id, CostCenterUpdate(group_setup=setup)
            )
    with pytest.raises(ValidationError, match="General"):
        await estate.service.add_directory_member(
            ADMIN, general_id, USER, PrincipalKind.USER, idempotency_key="general"
        )
    assert not estate.graph.writes and not estate.lookup.reads


async def test_legacy_local_groups_are_preserved_but_cannot_be_a_hidden_graph_editor(
    estate: Management,
) -> None:
    legacy = CostCenter(
        id="legacy",
        tenant_id=TENANT,
        name="Legacy",
        code="legacy",
        group_id="old-local",
        members=[CostCenterMember(principal_id="old-principal")],
    )
    await estate.repository.create_cost_center(legacy, audit())
    group = Group(
        id="old-local",
        tenant_id=TENANT,
        name="Old projection",
        purpose="costCenter",
        cost_center_id="legacy",
    )
    estate.local_directory.groups[group.id] = group
    directory = DirectoryService(estate.local_directory, directory_lookup=estate.lookup)
    for operation in [
        directory.add_membership(ADMIN, group.id, "old-principal"),
        directory.remove_membership(ADMIN, group.id, "old-principal"),
        directory.list_memberships(ADMIN, group.id),
    ]:
        with pytest.raises(ConflictError):
            await operation
    with pytest.raises(ConflictError, match="legacy local"):
        await estate.service.update_cost_center(
            ADMIN, legacy.id, CostCenterUpdate(group_setup=CreateCostCenterGroup())
        )
    before = deepcopy(estate.local_directory.groups)
    await estate.service.update_cost_center(ADMIN, legacy.id, CostCenterUpdate(name="New label"))
    await estate.service.delete_cost_center(ADMIN, legacy.id)
    assert estate.local_directory.groups == before
    assert estate.lookup.reads == 0 and not estate.graph.writes


async def test_legacy_principal_wrappers_never_report_unknown_or_failed_local_success(
    estate: Management,
) -> None:
    center = await attach(estate)
    person = Principal(id="person", tenant_id=TENANT, object_id=USER, kind="user")
    await estate.local_directory.create_principal(person, audit())
    estate.graph.failures["add_member"] = failure(
        GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN
    )
    with pytest.raises(DirectoryError, match="Fictional"):
        await estate.service.add_member_with_status(ADMIN, center.id, person.id)
    current = await estate.repository.get_cost_center(TENANT, center.id)
    assert current is not None and current.members == []


@pytest.mark.parametrize(
    ("status_code", "expected_state", "attempts"),
    [(403, "failed", 1), (429, "failed", 2), (503, "unknown", 1), (0, "unknown", 1)],
)
async def test_http_create_failures_are_tracked_and_unknown_is_not_replayed(
    status_code: int, expected_state: str, attempts: int
) -> None:
    estate = management()
    requests: list[httpx.Request] = []

    async def no_sleep(_delay: float) -> None:
        return None

    def handle(request: httpx.Request) -> httpx.Response:
        estate.lookup.outside_lease()
        requests.append(request)
        assert request.method == "POST" and request.url.path == "/v1.0/groups"
        if status_code == 0:
            raise httpx.ReadTimeout("Fictional timeout", request=request)
        return httpx.Response(
            status_code,
            json={"error": {"code": "FictionalFailure", "message": "Fictional"}},
            headers={"Retry-After": "0"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graph = GraphGroupManagement(
            FakeCredential(
                {"tid": TENANT, "idtyp": "app", "oid": OTHER_GROUP, "roles": list(ALL_WRITES)}
            ),
            endpoint="https://graph.microsoft.com",
            tenant_id=TENANT,
            client=client,
            sleep=no_sleep,
        )
        estate.service._directory_groups._management = graph
        request = CostCenterCreate(name="HTTP", code="http", group_setup=CreateCostCenterGroup())
        created = await estate.service.create(ADMIN, request, cost_center_id="http")
        assert created.setup_state == "setupRequired" and created.entra_binding is None
        assert created.directory_operations[0].state == expected_state
        error_details = created.directory_operations[0].error_details
        assert error_details is not None
        assert error_details.http_status == (status_code or None)
        if status_code:
            assert error_details.graph_code == "FictionalFailure"
            assert f"HTTP {status_code}" in (created.directory_operations[0].error_message or "")
        await estate.service.create(ADMIN, request, cost_center_id="http")
        assert len(requests) == attempts


async def test_http_remove_deletes_only_the_membership_ref_never_the_directory_object() -> None:
    estate = management()
    center = await attach(estate)
    deletes: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        estate.lookup.outside_lease()
        if request.method == "GET":
            assert request.url.path == f"/v1.0/groups/{GROUP}"
            return httpx.Response(
                200,
                json={
                    "id": GROUP,
                    "displayName": "Fictional group",
                    "securityEnabled": True,
                    "mailEnabled": False,
                    "groupTypes": [],
                    "isAssignableToRole": False,
                    "onPremisesSyncEnabled": None,
                    "onPremisesSecurityIdentifier": None,
                    "membershipRule": None,
                },
            )
        assert request.method == "DELETE"
        deletes.append(request.url.path)
        assert request.url.path == f"/v1.0/groups/{GROUP}/members/{USER}/$ref"
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        graph = GraphGroupManagement(
            FakeCredential(
                {"tid": TENANT, "idtyp": "app", "oid": OTHER_GROUP, "roles": list(ALL_WRITES)}
            ),
            endpoint="https://graph.microsoft.com",
            tenant_id=TENANT,
            client=client,
        )
        estate.service._directory_groups._management = graph
        operation = await estate.service.remove_directory_member(
            ADMIN, center.id, USER, idempotency_key="ref-only"
        )
    assert operation.state == "succeeded" and len(deletes) == 1
