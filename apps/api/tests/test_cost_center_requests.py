import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import pytest
from mosaic_api.cost_center_directory import (
    CostCenterDirectoryOperation,
    EntraGroupBinding,
    cost_center_member_operation_id,
)
from mosaic_api.cost_center_requests import (
    CostCenterRequest,
    CostCenterRequestApproval,
    CostCenterRequestCreate,
    CostCenterRequestDecision,
    requested_cost_center_id,
)
from mosaic_api.cost_centers import CostCenter, CostCenterCreate, CostCenterUpdate
from mosaic_api.domain import (
    AccessRequest,
    AuditEvent,
    EntitlementResource,
    PrincipalKind,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    DirectoryForbiddenError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.repositories import EntitlementRepository, InMemoryCostCenterRepository
from mosaic_api.services.cost_center_requests import CostCenterRequestService
from mosaic_api.services.directory import Actor
from test_cost_center_request_repositories import (
    GROUP,
    OTHER_USER,
    TENANT,
    USER,
    Store,
    cosmos_store,
)

DIRECTORY = "33333333-3333-4333-8333-333333333333"
ADMIN = Actor(object_id="fictional-admin", tenant_id=TENANT)
REQUESTER = Actor(object_id=USER, tenant_id=TENANT)


def event(actor: Actor, center_id: str) -> AuditEvent:
    return AuditEvent(
        id=new_id("audit"),
        tenant_id=actor.tenant_id,
        actor_object_id=actor.object_id,
        action="costCenter.requestTest",
        resource_type="costCenter",
        resource_id=center_id,
    )


class CenterActions:
    def __init__(
        self, requests: EntitlementRepository, centers: InMemoryCostCenterRepository
    ) -> None:
        self.requests = requests
        self.centers = centers
        self.created_ids: list[str] = []
        self.group_updates: list[str] = []
        self.member_writes: list[tuple[str, str, str]] = []
        self.create_error: DomainError | None = None
        self.group_error: DomainError | None = None
        self.member_error: DomainError | None = None
        self.validation_error: DomainError | None = None
        self.omit_record = False
        self.leave_member_pending = False
        self.before_create: Callable[[], Awaitable[None]] | None = None
        self.request_id: str | None = None

    async def validate(self, _actor: Actor, _center: CostCenter) -> None:
        if self.validation_error is not None:
            raise self.validation_error

    async def create(
        self, actor: Actor, payload: CostCenterCreate, *, cost_center_id: str | None = None
    ) -> object:
        assert cost_center_id is not None
        assert payload.group_setup is None
        self.created_ids.append(cost_center_id)
        if self.before_create is not None:
            callback, self.before_create = self.before_create, None
            await callback()
        if self.create_error is not None:
            raise self.create_error
        if self.omit_record:
            return None
        existing = await self.centers.get_cost_center(actor.tenant_id, cost_center_id)
        if existing is not None:
            return existing
        return await self.centers.create_cost_center(
            CostCenter(
                id=cost_center_id,
                tenant_id=actor.tenant_id,
                name=payload.name,
                code=payload.code,
                resource_access=payload.resource_access,
            ),
            event(actor, cost_center_id),
        )

    async def update_cost_center(
        self, actor: Actor, cost_center_id: str, payload: CostCenterUpdate
    ) -> object:
        await self._assert_committed(actor)
        self.group_updates.append(cost_center_id)
        if self.group_error is not None:
            raise self.group_error
        center = await self.centers.get_cost_center(actor.tenant_id, cost_center_id)
        assert center is not None and payload.group_setup is not None
        if center.entra_binding is not None:
            return center
        group_id = (
            payload.group_setup.group_object_id
            if payload.group_setup.mode == "attach"
            else str(uuid5(NAMESPACE_URL, f"fictional-group:{cost_center_id}"))
        )
        return await self.centers.save_cost_center(
            center.model_copy(
                update={
                    "entra_binding": EntraGroupBinding(
                        directory_tenant_id=DIRECTORY,
                        group_object_id=group_id,
                        provenance="attached",
                        verification_state="verified",
                        verified_at=utc_now(),
                    )
                }
            ),
            event(actor, cost_center_id),
        )

    async def add_directory_member(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        member_kind: PrincipalKind,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperation:
        await self._assert_committed(actor)
        if self.member_error is not None:
            raise self.member_error
        operation_id = cost_center_member_operation_id(
            actor.tenant_id, cost_center_id, idempotency_key
        )
        existing = await self.centers.get_directory_operation(actor.tenant_id, operation_id)
        if existing is not None:
            return existing
        center = await self.centers.get_cost_center(actor.tenant_id, cost_center_id)
        assert center is not None and center.verified_group_object_id is not None
        operation = await self.centers.create_directory_operation(
            CostCenterDirectoryOperation(
                id=operation_id,
                tenant_id=actor.tenant_id,
                cost_center_id=cost_center_id,
                directory_tenant_id=DIRECTORY,
                group_object_id=center.verified_group_object_id,
                member_object_id=object_id,
                member_kind=member_kind,
                kind="addMember",
                requested_by=actor.object_id,
            ),
            event(actor, cost_center_id),
        )
        if self.leave_member_pending:
            return operation
        self.member_writes.append((cost_center_id, object_id, idempotency_key))
        return await self.centers.save_directory_operation(
            operation.model_copy(update={"state": "succeeded", "updated_at": utc_now()}),
            event(actor, cost_center_id),
        )

    async def _assert_committed(self, actor: Actor) -> None:
        assert self.request_id is not None
        request = await self.requests.get_cost_center_request(actor.tenant_id, self.request_id)
        assert request is not None and request.state == "approved"


@dataclass
class Harness:
    store: Store
    centers: InMemoryCostCenterRepository
    actions: CenterActions
    service: CostCenterRequestService


def make_harness(*, cosmos: bool = False) -> Harness:
    if cosmos:
        store = cosmos_store()
    else:
        from mosaic_api.repositories import InMemoryEntitlementRepository

        repository = InMemoryEntitlementRepository()
        store = Store(repository, memory=repository)
    centers = InMemoryCostCenterRepository()
    actions = CenterActions(store.repository, centers)
    service = CostCenterRequestService(
        store.repository, centers, actions, validate_access=actions.validate
    )
    return Harness(store, centers, actions, service)


@pytest.fixture(params=["memory", "cosmos"])
def harness(request: pytest.FixtureRequest) -> Harness:
    return make_harness(cosmos=request.param == "cosmos")


async def submit(harness: Harness, **updates: Any) -> str:
    view = await harness.service.submit(
        REQUESTER,
        CostCenterRequestCreate(
            name="Fictional research",
            justification="Evaluate fictional model workloads",
            **updates,
        ),
    )
    harness.actions.request_id = view.id
    return view.id


def approve_payload(**updates: Any) -> CostCenterRequestApproval:
    return CostCenterRequestApproval.model_validate(
        {
            "cost_center": {
                "name": "Fictional research",
                "code": "RESEARCH",
                "group_setup": {"mode": "create"},
            },
            **updates,
        }
    )


async def stored(harness: Harness, request_id: str) -> CostCenterRequest:
    request = await harness.store.repository.get_cost_center_request(TENANT, request_id)
    assert request is not None
    return request


async def test_user_group_is_only_a_suggestion_and_submission_does_no_directory_work(
    harness: Harness,
) -> None:
    request_id = await submit(harness, suggested_group_object_id=GROUP)
    request = await stored(harness, request_id)
    assert request.suggested_group_object_id == GROUP
    assert request.approval is None
    assert harness.actions.created_ids == []
    assert harness.actions.group_updates == []
    assert harness.actions.member_writes == []
    assert await harness.store.repository.list_access_requests(TENANT) == []
    assert await harness.store.repository.list_entitlements(TENANT) == []


async def test_submission_idempotency_is_owned_and_payload_checked(harness: Harness) -> None:
    key = uuid4()
    request_id = await submit(harness, idempotency_key=key)
    same = await harness.service.submit(
        REQUESTER,
        CostCenterRequestCreate(
            name="Fictional research",
            justification="Evaluate fictional model workloads",
            idempotency_key=key,
        ),
    )
    assert same.id == request_id
    assert len(await harness.service.list_mine(REQUESTER)) == 1
    with pytest.raises(ConflictError, match="different cost center request"):
        await harness.service.submit(
            REQUESTER,
            CostCenterRequestCreate(name="Changed", justification="Changed", idempotency_key=key),
        )
    another_user = Actor(object_id=OTHER_USER, tenant_id=TENANT)
    another = await harness.service.submit(
        another_user,
        CostCenterRequestCreate(name="Another", justification="Purpose", idempotency_key=key),
    )
    assert another.id != request_id


async def test_request_reads_and_withdrawals_are_requester_and_tenant_scoped(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    for actor in (
        Actor(object_id=OTHER_USER, tenant_id=TENANT),
        Actor(object_id=USER, tenant_id="other-tenant"),
    ):
        with pytest.raises(NotFoundError):
            await harness.service.get_mine(actor, request_id)
        with pytest.raises(NotFoundError):
            await harness.service.withdraw(actor, request_id)
        assert await harness.service.list_mine(actor) == []
    withdrawn = await harness.service.withdraw(REQUESTER, request_id)
    assert withdrawn.state == "withdrawn"
    assert (
        await harness.service.withdraw(REQUESTER, request_id)
    ).decided_at == withdrawn.decided_at
    with pytest.raises(ConflictError):
        await harness.service.approve(ADMIN, request_id, approve_payload())
    assert harness.actions.created_ids == []


async def test_approval_is_local_then_committed_before_group_and_member_work(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    result = await harness.service.approve(ADMIN, request_id, approve_payload(add_requester=True))
    assert result.state == "approved" and result.setup_state == "ready"
    assert result.requester_membership_state == "succeeded"
    assert not result.needs_administrator_attention
    assert result.cost_center is not None
    assert result.cost_center.id == requested_cost_center_id(TENANT, request_id)
    assert harness.actions.created_ids == [result.cost_center.id]
    assert len(harness.actions.group_updates) == len(harness.actions.member_writes) == 1


async def test_requester_is_not_added_without_explicit_administrator_choice(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    result = await harness.service.approve(ADMIN, request_id, approve_payload())
    assert result.state == "approved"
    assert result.requester_membership_state is None
    assert harness.actions.member_writes == []


async def test_admin_can_ignore_the_group_suggestion_or_attach_a_different_group(
    harness: Harness,
) -> None:
    request_id = await submit(harness, suggested_group_object_id=GROUP)
    selected = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    result = await harness.service.approve(
        ADMIN,
        request_id,
        approve_payload(
            cost_center={
                "name": "Fictional research",
                "code": "RESEARCH",
                "group_setup": {
                    "mode": "attach",
                    "group_object_id": selected,
                    "acknowledge_membership_changes": True,
                },
            }
        ),
    )
    assert result.cost_center is not None
    center = await harness.centers.get_cost_center(TENANT, result.cost_center.id)
    assert center is not None and center.verified_group_object_id == selected


async def test_retry_approval_reuses_center_group_and_completed_member_operation(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    payload = approve_payload(add_requester=True)
    first = await harness.service.approve(ADMIN, request_id, payload)
    second = await harness.service.approve(ADMIN, request_id, payload)
    assert first.cost_center == second.cost_center
    assert len(await harness.centers.list_cost_centers(TENANT)) == 1
    assert len(harness.actions.created_ids) == 1
    assert len(harness.actions.group_updates) == 1
    assert len(harness.actions.member_writes) == 1
    assert len(await harness.centers.list_directory_operations(TENANT, first.cost_center.id)) == 1


async def test_different_configuration_cannot_repurpose_an_approved_request(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    await harness.service.approve(ADMIN, request_id, approve_payload())
    with pytest.raises(ConflictError, match="reviewed configuration"):
        await harness.service.approve(ADMIN, request_id, approve_payload(add_requester=True))
    assert len(harness.actions.created_ids) == 1


async def test_group_failure_preserves_approved_center_and_exposes_partial_result(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    harness.actions.group_error = DirectoryForbiddenError("Group.Create consent is missing")
    result = await harness.service.approve(ADMIN, request_id, approve_payload(add_requester=True))
    assert result.state == "approved" and result.cost_center is not None
    assert result.setup_state == "setupRequired"
    assert result.needs_administrator_attention
    assert result.preparation_error == "Group.Create consent is missing"
    assert result.requester_membership_state == "pending"
    assert harness.actions.member_writes == []
    portal = await harness.service.get_mine(REQUESTER, request_id)
    payload = portal.model_dump(mode="json", by_alias=True)
    assert payload["needsAdministratorAttention"]
    assert "approval" not in payload and "preparationError" not in payload
    assert "Group.Create" not in portal.model_dump_json()
    harness.actions.group_error = None
    recovered = await harness.service.approve(
        ADMIN, request_id, approve_payload(add_requester=True)
    )
    assert recovered.cost_center == result.cost_center
    assert recovered.setup_state == "ready"
    assert recovered.preparation_error is None
    assert recovered.requester_membership_state == "succeeded"
    assert len(harness.actions.created_ids) == 1


async def test_group_can_be_finished_later_without_reapproving_as_another_center(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    payload = approve_payload(
        cost_center={"name": "Fictional research", "code": "RESEARCH"},
        add_requester=True,
    )
    result = await harness.service.approve(ADMIN, request_id, payload)
    assert result.cost_center is not None and result.setup_state == "setupRequired"
    assert result.requester_membership_state == "pending"
    await harness.actions.update_cost_center(
        ADMIN,
        result.cost_center.id,
        CostCenterUpdate(
            group_setup={
                "mode": "attach",
                "group_object_id": GROUP,
                "acknowledge_membership_changes": True,
            }
        ),
    )
    resumed = await harness.service.approve(ADMIN, request_id, payload)
    assert resumed.requester_membership_state == "succeeded"
    assert len(harness.actions.created_ids) == len(harness.actions.member_writes) == 1


async def test_membership_denial_does_not_unapprove_or_duplicate_the_center(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    harness.actions.member_error = DirectoryForbiddenError("GroupMember.ReadWrite.All is missing")
    payload = approve_payload(add_requester=True)
    result = await harness.service.approve(ADMIN, request_id, payload)
    assert result.state == "approved" and result.setup_state == "ready"
    assert result.requester_membership_state == "failed"
    assert result.needs_administrator_attention
    harness.actions.member_error = None
    resumed = await harness.service.approve(ADMIN, request_id, payload)
    assert resumed.requester_membership_state == "succeeded"
    assert len(harness.actions.created_ids) == 1
    assert len(harness.actions.member_writes) == 1


async def test_pending_member_operation_requires_attention_and_is_not_replayed(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    harness.actions.leave_member_pending = True
    payload = approve_payload(add_requester=True)
    first = await harness.service.approve(ADMIN, request_id, payload)
    assert first.state == "approved" and first.setup_state == "ready"
    assert first.requester_membership_state == "pending"
    assert first.needs_administrator_attention
    harness.actions.leave_member_pending = False
    repeated = await harness.service.approve(ADMIN, request_id, payload)
    assert repeated.requester_membership_state == "pending"
    assert repeated.needs_administrator_attention
    assert harness.actions.member_writes == []


async def test_recorded_membership_success_survives_a_later_service_error(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id = await submit(harness)
    original = harness.actions.add_directory_member

    async def succeed_then_error(
        actor: Actor,
        center_id: str,
        object_id: str,
        member_kind: PrincipalKind,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperation:
        await original(actor, center_id, object_id, member_kind, idempotency_key=idempotency_key)
        raise ChangeNotRecordedError("The directory change completed; a later update failed")

    monkeypatch.setattr(harness.actions, "add_directory_member", succeed_then_error)
    payload = approve_payload(add_requester=True)
    result = await harness.service.approve(ADMIN, request_id, payload)
    assert result.requester_membership_state == "succeeded"
    assert result.requester_membership_error is None
    assert result.preparation_error and result.needs_administrator_attention
    resumed = await harness.service.approve(ADMIN, request_id, payload)
    assert resumed.requester_membership_state == "succeeded"
    assert resumed.preparation_error is None
    assert len(harness.actions.member_writes) == 1


async def test_preflight_failure_does_not_lock_the_request_configuration(harness: Harness) -> None:
    request_id = await submit(harness)
    harness.actions.validation_error = ValidationError("Automatic access needs Entra token support")
    with pytest.raises(ValidationError):
        await harness.service.approve(ADMIN, request_id, approve_payload())
    assert (await stored(harness, request_id)).approval is None
    assert harness.actions.created_ids == []
    harness.actions.validation_error = None
    assert (await harness.service.approve(ADMIN, request_id, approve_payload())).state == "approved"


async def test_existing_code_is_rejected_before_claiming_the_review(harness: Harness) -> None:
    request_id = await submit(harness)
    await harness.centers.create_cost_center(
        CostCenter(id="existing", tenant_id=TENANT, name="Existing", code="research"),
        event(ADMIN, "existing"),
    )
    with pytest.raises(ConflictError, match="reviewed code"):
        await harness.service.approve(ADMIN, request_id, approve_payload())
    assert (await stored(harness, request_id)).approval is None
    assert harness.actions.created_ids == []


async def test_local_preparation_failure_is_visible_and_resumes_the_same_id(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    harness.actions.create_error = ValidationError("The reviewed setup is not currently available")
    with pytest.raises(ValidationError) as failure:
        await harness.service.approve(ADMIN, request_id, approve_payload())
    assert failure.value.details["costCenterId"] == requested_cost_center_id(TENANT, request_id)
    pending = await stored(harness, request_id)
    assert pending.state == "pending" and pending.approval is not None
    assert pending.preparation_error
    assert harness.actions.group_updates == []
    harness.actions.create_error = None
    resumed = await harness.service.approve(ADMIN, request_id, approve_payload())
    assert resumed.state == "approved"
    assert len(set(harness.actions.created_ids)) == 1


async def test_success_shaped_create_without_persistence_is_not_approval(harness: Harness) -> None:
    request_id = await submit(harness)
    harness.actions.omit_record = True
    with pytest.raises(ChangeNotRecordedError):
        await harness.service.approve(ADMIN, request_id, approve_payload())
    assert (await stored(harness, request_id)).state == "pending"
    assert harness.actions.group_updates == []


async def test_withdrawal_during_preparation_prevents_all_request_driven_directory_writes(
    harness: Harness,
) -> None:
    request_id = await submit(harness)

    async def withdraw() -> None:
        await harness.service.withdraw(REQUESTER, request_id)

    harness.actions.before_create = withdraw
    with pytest.raises(ConflictError):
        await harness.service.approve(ADMIN, request_id, approve_payload(add_requester=True))
    assert (await stored(harness, request_id)).state == "withdrawn"
    centers = await harness.centers.list_cost_centers(TENANT)
    assert len(centers) == 1 and centers[0].setup_state == "setupRequired"
    assert harness.actions.group_updates == harness.actions.member_writes == []
    assert not (await harness.service.get_mine(REQUESTER, request_id)).needs_administrator_attention


async def test_concurrent_approval_still_links_one_center(harness: Harness) -> None:
    request_id = await submit(harness)
    payload = approve_payload()
    results = await asyncio.gather(
        harness.service.approve(ADMIN, request_id, payload),
        harness.service.approve(ADMIN, request_id, payload),
        return_exceptions=True,
    )
    assert any(not isinstance(item, Exception) for item in results)
    assert all(
        not isinstance(item, Exception) or isinstance(item, ConflictError) for item in results
    )
    request = await stored(harness, request_id)
    assert request.state == "approved"
    assert len(await harness.centers.list_cost_centers(TENANT)) == 1
    assert request.cost_center_id == requested_cost_center_id(TENANT, request_id)
    assert len(harness.actions.group_updates) == 1


async def test_denial_is_idempotent_and_approved_requests_cannot_be_withdrawn(
    harness: Harness,
) -> None:
    request_id = await submit(harness)
    denied = await harness.service.deny(
        ADMIN, request_id, CostCenterRequestDecision(note="Use the existing project center")
    )
    repeated = await harness.service.deny(ADMIN, request_id, CostCenterRequestDecision())
    assert (
        denied.decided_at == repeated.decided_at and denied.decision_note == repeated.decision_note
    )
    another_id = await submit(harness)
    await harness.service.approve(ADMIN, another_id, approve_payload())
    with pytest.raises(ConflictError):
        await harness.service.withdraw(REQUESTER, another_id)
    with pytest.raises(ConflictError):
        await harness.service.deny(ADMIN, another_id, CostCenterRequestDecision())


async def test_counts_include_both_kinds_and_portal_counts_are_owned(harness: Harness) -> None:
    await submit(harness)
    another_user = Actor(object_id=OTHER_USER, tenant_id=TENANT)
    await harness.service.submit(
        another_user, CostCenterRequestCreate(name="Other", justification="Another project")
    )
    await harness.store.repository.create_access_request(
        AccessRequest(
            id="resource-request",
            tenant_id=TENANT,
            requester_object_id=USER,
            resource=EntitlementResource(kind="modelApi", id="model"),
        ),
        event(ADMIN, "resource-request"),
    )
    mine = await harness.service.counts(REQUESTER, administrator=False)
    admin = await harness.service.counts(ADMIN, administrator=True)
    assert (mine.pending_resource_count, mine.pending_cost_center_count, mine.pending_count) == (
        1,
        1,
        2,
    )
    assert (admin.pending_resource_count, admin.pending_cost_center_count, admin.pending_count) == (
        1,
        2,
        3,
    )
