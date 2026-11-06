import asyncio
from dataclasses import replace
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from cost_center_management_double import (
    ADMIN,
    BASELINE,
    GROUP,
    OTHER_GROUP,
    TENANT,
    USER,
    WORKLOAD,
    management,
)
from mosaic_api.cost_center_membership_requests import (
    AdminMembershipRequest,
    MembershipApproval,
    MembershipDecision,
    MembershipRequestCreate,
)
from mosaic_api.cost_centers import CostCenterSettingsUpdate, CostCenterUpdate, default_settings
from mosaic_api.domain import general_cost_center_id, utc_now
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    DirectoryError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.integrations.graph import GroupFailureKind, GroupWriteOutcome
from mosaic_api.services.cost_center_membership_requests import CostCenterMembershipService
from mosaic_api.services.cost_center_requests import CostCenterRequestService
from mosaic_api.services.directory import Actor
from mosaic_api.services.notification_events import membership_request_event
from mosaic_api.services.notification_visibility import NotificationVisibility
from mosaic_api.services.notifications import notification_summary, notification_title
from test_cost_center_request_repositories import cosmos_store
from test_managed_cost_center_groups import attach, failure

REQUESTER = Actor(object_id=USER, tenant_id=TENANT)
APPROVAL = MembershipApproval(acknowledge_membership_changes=True)


@pytest.fixture(params=["memory", "cosmos"])
def estate(request: pytest.FixtureRequest) -> Any:
    h = management(request.param)
    repository = h.entitlements if request.param == "memory" else cosmos_store().repository
    service = CostCenterMembershipService(repository, h.repository, h.service, h.lookup)
    return h, repository, service


async def published(estate: Any, *, center_id: str = "center", group_id: str = GROUP) -> Any:
    h, _, _ = estate
    center = await attach(h, center_id, group_id)
    await h.service.update_settings(
        ADMIN,
        CostCenterSettingsUpdate(membership_requests_enabled=True),
    )
    return await h.service.update_cost_center(
        ADMIN,
        center.id,
        CostCenterUpdate(
            discoverable=True,
            public_purpose="Fictional research",
            acknowledge_membership_changes=True,
        ),
    )


def payload(center_id: str = "center", **kwargs: Any) -> MembershipRequestCreate:
    return MembershipRequestCreate(
        cost_center_id=center_id,
        justification="Fictional project work",
        idempotency_key=uuid4(),
        **kwargs,
    )


async def test_defaults_and_partial_settings_preserve_both_switches(estate: Any) -> None:
    h, _, service = estate
    assert default_settings(TENANT).creation_requests_enabled
    assert not default_settings(TENANT).membership_requests_enabled
    assert (await service.settings(REQUESTER)).model_dump(by_alias=True) == {
        "creationRequestsEnabled": True,
        "membershipRequestsEnabled": False,
    }
    await h.service.update_settings(
        ADMIN,
        CostCenterSettingsUpdate(creation_requests_enabled=False, membership_requests_enabled=True),
    )
    await h.service.update_settings(
        ADMIN,
        CostCenterSettingsUpdate(default_cost_center_id=general_cost_center_id(TENANT)),
    )
    settings = await h.service.get_settings(ADMIN)
    assert not settings.creation_requests_enabled and settings.membership_requests_enabled
    assert settings.default_cost_center_id == general_cost_center_id(TENANT)
    await h.service.update_settings(
        ADMIN, CostCenterSettingsUpdate(membership_requests_enabled=False)
    )
    assert not (await h.service.get_settings(ADMIN)).creation_requests_enabled


async def test_toggle_only_updates_do_not_revalidate_default_or_recheck_grants(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, _, _ = estate
    monkeypatch.setattr(
        h.service, "book", AsyncMock(side_effect=AssertionError("No eligibility read"))
    )
    monkeypatch.setattr(
        h.checks, "resume_rechecks", AsyncMock(side_effect=AssertionError("No recheck"))
    )
    monkeypatch.setattr(
        h.checks, "request_recheck", AsyncMock(side_effect=AssertionError("No recheck"))
    )
    await h.service.update_settings(
        ADMIN, CostCenterSettingsUpdate(creation_requests_enabled=False)
    )
    assert not (await h.service.get_settings(ADMIN)).creation_requests_enabled


async def test_privacy_membership_transitive_evidence_token_separation_and_search(
    estate: Any,
) -> None:
    h, _, service = estate
    await published(estate)
    private = await attach(h, "private", OTHER_GROUP)
    h.lookup.state.group_members[OTHER_GROUP] = [USER]
    h.lookup.state.group_members[GROUP] = [OTHER_GROUP]
    mine = await service.list_centers(REQUESTER, mine=True)
    assert {item.id for item in mine.items} == {"center", "private", general_cost_center_id(TENANT)}
    assert all(not item.token_eligible for item in mine.items if not item.built_in)
    assert (await service.center(REQUESTER, private.id)).membership_state == "member"
    directory = await service.list_centers(REQUESTER, mine=False, q="research")
    assert [item.id for item in directory.items] == ["center"]
    assert directory.items[0].membership_state == "member"
    other = Actor(object_id=WORKLOAD, tenant_id=TENANT)
    with pytest.raises(NotFoundError):
        await service.center(other, private.id)
    view = await service.center(other, "center")
    assert view.membership_state == "unknown"
    assert "groupObjectId" not in view.model_dump_json()
    with pytest.raises(ConflictError, match="already a member"):
        await service.submit(REQUESTER, payload())
    overage = Actor(
        object_id=USER, tenant_id=TENANT, group_ids=frozenset({GROUP}), groups_overage=True
    )
    assert not (await service.center(overage, "center")).token_eligible
    with_token = Actor(object_id=USER, tenant_id=TENANT, group_ids=frozenset({GROUP}))
    assert (await service.center(with_token, "center")).token_eligible
    assert h.graph.writes == []


async def test_bounded_pages_and_private_unknown_fail_closed(
    estate: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, service = estate
    await published(estate)
    await published(estate, center_id="other", group_id=OTHER_GROUP)
    first = await service.list_centers(REQUESTER, mine=False, limit=1)
    assert len(first.items) == 1 and first.next_cursor
    second = await service.list_centers(REQUESTER, mine=False, cursor=first.next_cursor, limit=1)
    assert len(second.items) == 1 and second.next_cursor is None
    assert second.items[0].id != first.items[0].id
    with pytest.raises(ValidationError):
        await service.list_centers(REQUESTER, mine=False, cursor="cross-tenant-id")
    await h.service.update_cost_center(ADMIN, "other", CostCenterUpdate(discoverable=False))
    monkeypatch.setattr(
        h.lookup,
        "verify_user_memberships",
        AsyncMock(side_effect=DirectoryError("secret Graph ID")),
    )
    directory = await service.list_centers(REQUESTER, mine=False)
    assert directory.items[0].membership_state == "unknown"
    assert directory.membership_error and "secret" not in directory.membership_error
    mine = await service.list_centers(REQUESTER, mine=True)
    assert all(item.built_in for item in mine.items) and mine.membership_error
    with pytest.raises(NotFoundError):
        await service.center(REQUESTER, "other")
    with pytest.raises(DirectoryError, match="Membership verification is unavailable"):
        await service.submit(REQUESTER, payload())


@pytest.mark.parametrize(
    "invalid", ["tenant", "user", "candidates", "members", "stale", "future", "missing"]
)
async def test_strict_evidence_rejects_wrong_scope_incomplete_or_stale(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
    invalid: str,
) -> None:
    h, _, service = estate
    await published(estate)
    evidence = await h.lookup.verify_user_memberships(USER, [GROUP])
    replacements: dict[str, dict[str, Any]] = {
        "tenant": {"tenant_id": "another-tenant"},
        "user": {"user_object_id": WORKLOAD},
        "candidates": {"candidate_group_ids": frozenset({OTHER_GROUP})},
        "members": {"member_group_ids": frozenset({OTHER_GROUP})},
        "stale": {"checked_at": utc_now() - timedelta(minutes=5)},
        "future": {"checked_at": utc_now() + timedelta(minutes=5)},
    }
    result = None if invalid == "missing" else replace(evidence, **replacements[invalid])
    monkeypatch.setattr(h.lookup, "verify_user_memberships", AsyncMock(return_value=result))
    with pytest.raises(DirectoryError, match="Membership verification is unavailable"):
        await service.submit(REQUESTER, payload())
    assert h.graph.writes == []


async def test_idempotency_and_concurrent_active_guard_across_keys(estate: Any) -> None:
    h, repository, service = estate
    await published(estate)
    proposal = payload()
    first, replay = await asyncio.gather(
        service.submit(REQUESTER, proposal),
        service.submit(REQUESTER, proposal),
    )
    assert first.id == replay.id
    results = await asyncio.gather(
        service.submit(REQUESTER, payload()),
        service.submit(REQUESTER, payload()),
        return_exceptions=True,
    )
    assert all(isinstance(item, ConflictError) for item in results)
    assert len(await repository.list_membership_requests(TENANT)) == 1
    with pytest.raises(ConflictError):
        await service.submit(
            REQUESTER, proposal.model_copy(update={"justification": "Different work"})
        )
    await service.close(REQUESTER, first.id, MembershipDecision(), withdraw=True)
    assert (await service.submit(REQUESTER, payload())).state == "pending"
    assert h.graph.writes == []


async def test_approval_commits_before_graph_and_preserves_resource_enforcement(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, repository, service = estate
    center = await published(estate)
    settings = await h.repository.get_settings(TENANT)
    before_entitlements = await repository.list_entitlements(TENANT)
    before_runs = dict(h.gateways.publish_runs)
    request = await service.submit(REQUESTER, payload())
    add = h.graph.add_member

    async def committed(*args: Any, **kwargs: Any) -> Any:
        stored = await repository.get_membership_request(TENANT, request.id)
        assert stored and stored.state == "approved" and stored.decided_at
        return await add(*args, **kwargs)

    monkeypatch.setattr(h.graph, "add_member", committed)
    results = await asyncio.gather(
        service.approve(ADMIN, request.id, APPROVAL),
        service.approve(ADMIN, request.id, APPROVAL),
    )
    assert all(result.state == "approved" for result in results)
    assert len(h.graph.writes) == 1 and h.graph.writes[0].operation == "add_member"
    result = await service.get(ADMIN, request.id, administrator=True)
    assert result.membership_state == "succeeded" and not result.needs_administrator_attention
    assert isinstance(result, AdminMembershipRequest) and result.operation_id
    assert await repository.list_entitlements(TENANT) == before_entitlements
    assert h.gateways.publish_runs == before_runs
    assert await h.repository.get_settings(TENANT) == settings
    after = await h.repository.get_cost_center(TENANT, center.id)
    assert (
        after and after.limits == center.limits and after.resource_access == center.resource_access
    )
    assert h.local_directory.principals == {} and h.local_directory.groups == {}
    assert not (await service.center(REQUESTER, center.id)).token_eligible
    portal = await service.get(REQUESTER, request.id, administrator=False)
    assert (
        "operationId" not in portal.model_dump_json()
        and "requesterObjectId" not in portal.model_dump_json()
    )
    assert (
        "groupObjectId" not in portal.model_dump_json()
        and "membershipError" not in portal.model_dump_json()
    )


async def test_out_of_band_inherited_member_needs_no_direct_add(estate: Any) -> None:
    h, _, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    h.lookup.state.group_members[OTHER_GROUP] = [USER]
    h.lookup.state.group_members[GROUP] = [OTHER_GROUP]
    result = await service.approve(ADMIN, request.id, APPROVAL)
    assert result.membership_state == "succeeded" and result.operation_id is None
    assert h.graph.writes == []


async def test_switch_off_and_unpublish_do_not_cancel_existing_reviews(estate: Any) -> None:
    h, repository, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    await h.service.update_settings(
        ADMIN, CostCenterSettingsUpdate(membership_requests_enabled=False)
    )
    await h.service.update_cost_center(ADMIN, "center", CostCenterUpdate(discoverable=False))
    with pytest.raises(ConflictError):
        await service.submit(REQUESTER, payload())
    assert (await service.approve(ADMIN, request.id, APPROVAL)).membership_state == "succeeded"
    assert len(await repository.list_membership_requests(TENANT)) == 1
    assert (await service.center(REQUESTER, "center")).membership_state == "member"


async def test_failed_and_unknown_directory_outcomes_are_durable_and_never_blindly_reposted(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, repository, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    add = h.graph.add_member

    async def uncertain(*args: Any, **kwargs: Any) -> Any:
        await add(*args, **kwargs)
        raise failure(GroupFailureKind.UNKNOWN_OUTCOME, GroupWriteOutcome.UNKNOWN)

    monkeypatch.setattr(h.graph, "add_member", uncertain)
    result = await service.approve(ADMIN, request.id, APPROVAL)
    assert result.state == "approved" and result.membership_state == "unknown"
    assert result.operation_id and result.membership_error and result.needs_administrator_attention
    await service.approve(ADMIN, request.id, APPROVAL)
    assert len(h.graph.writes) == 1
    await service.approve(ADMIN, request.id, APPROVAL, resume=True)
    result = await service.get(ADMIN, request.id, administrator=True)
    assert result.membership_state == "succeeded" and len(h.graph.writes) == 1
    assert not (await repository.get_membership_request(TENANT, request.id)).active


async def test_missing_consent_keeps_approved_failure_visible_and_counts_action_needed() -> None:
    h = management(permissions=BASELINE)
    repository = h.entitlements
    service = CostCenterMembershipService(repository, h.repository, h.service, h.lookup)
    await published((h, repository, service))
    request = await service.submit(REQUESTER, payload())
    result = await service.approve(ADMIN, request.id, APPROVAL)
    assert result.state == "approved" and result.membership_state == "failed"
    assert result.membership_error and result.needs_administrator_attention and result.operation_id
    await service.approve(ADMIN, request.id, APPROVAL, resume=True)
    assert h.graph.writes == []
    creation = CostCenterRequestService(
        repository,
        h.repository,
        h.service,
        validate_access=h.checks.validate_cost_center_access,
    )
    counts = await creation.counts(ADMIN, administrator=True)
    assert counts.pending_membership_count == 1 and counts.pending_count == 1
    with pytest.raises(ConflictError):
        await service.close(REQUESTER, request.id, MembershipDecision(), withdraw=True)
    assert (await service.list_requests(ADMIN, administrator=True))[0].needs_administrator_attention


async def test_failure_before_operation_creation_has_distinct_recoverable_status(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, repository, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    original = h.service.add_directory_member
    monkeypatch.setattr(
        h.service,
        "add_directory_member",
        AsyncMock(side_effect=RuntimeError("crash before operation")),
    )
    failed = await service.approve(ADMIN, request.id, APPROVAL)
    assert failed.state == "approved" and failed.membership_state == "failed"
    assert failed.operation_id is None and failed.needs_administrator_attention
    assert (await repository.get_membership_request(TENANT, request.id)).state == "approved"
    monkeypatch.setattr(h.service, "add_directory_member", original)
    recovered = await service.approve(ADMIN, request.id, APPROVAL, resume=True)
    assert recovered.membership_state == "succeeded" and len(h.graph.writes) == 1


async def test_result_persistence_failure_is_readable_and_resumes_without_second_write(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, repository, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    save = repository.save_membership_request

    async def fail_result(item: Any, *args: Any) -> Any:
        if item.membership_state == "succeeded":
            raise ChangeNotRecordedError("Fictional result persistence failure")
        return await save(item, *args)

    monkeypatch.setattr(repository, "save_membership_request", fail_result)
    with pytest.raises(ChangeNotRecordedError):
        await service.approve(ADMIN, request.id, APPROVAL)
    readable = await service.get(REQUESTER, request.id, administrator=False)
    assert readable.state == "approved" and readable.membership_state == "succeeded"
    assert readable.needs_administrator_attention
    monkeypatch.setattr(repository, "save_membership_request", save)
    assert (
        await service.approve(ADMIN, request.id, APPROVAL, resume=True)
    ).membership_state == "succeeded"
    assert len(h.graph.writes) == 1


async def test_decision_cas_rejects_deny_or_withdraw_after_approval(estate: Any) -> None:
    _, repository, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    previous = await repository.get_membership_request(TENANT, request.id)
    await service.approve(ADMIN, request.id, APPROVAL)
    denied = previous.model_copy(
        update={
            "state": "denied",
            "decided_at": utc_now(),
            "decided_by_object_id": ADMIN.object_id,
        }
    )
    with pytest.raises(ConflictError):
        await repository.save_membership_request(
            denied, service._audit(ADMIN, denied, "denied"), []
        )
    with pytest.raises(ConflictError):
        await service.close(REQUESTER, request.id, MembershipDecision(), withdraw=True)


async def test_target_user_identity_and_notification_authorization(estate: Any) -> None:
    h, repository, service = estate
    await published(estate)
    request = await service.submit(REQUESTER, payload())
    for actor in (
        Actor(object_id=WORKLOAD, tenant_id=TENANT),
        Actor(object_id=USER, tenant_id="other"),
    ):
        with pytest.raises(NotFoundError):
            await service.get(actor, request.id, administrator=False)
        with pytest.raises(NotFoundError):
            await service.close(actor, request.id, MembershipDecision(), withdraw=True)
    saved = await repository.get_membership_request(TENANT, request.id)
    event = membership_request_event(saved)
    visibility = NotificationVisibility(
        repository,
        h.local_directory,
        grant_recipients=AsyncMock(return_value=[]),
    )
    assert await visibility.authorize(event, USER, "portal")
    assert not await visibility.authorize(event, WORKLOAD, "portal")
    assert "membership" in notification_title(event).casefold()
    assert "administrator review" in notification_summary(event)
    del h.lookup.state.objects[USER]
    with pytest.raises(ValidationError):
        await service.approve(ADMIN, request.id, APPROVAL)
    assert (await repository.get_membership_request(TENANT, request.id)).state == "pending"
    assert h.graph.writes == []


async def test_repository_guard_admits_one_concurrent_key_and_old_close_cannot_release_new_guard(
    estate: Any,
) -> None:
    _, repository, service = estate
    await published(estate)
    portal = await service.submit(REQUESTER, payload())
    original = await repository.get_membership_request(TENANT, portal.id)
    await service.close(REQUESTER, portal.id, MembershipDecision(), withdraw=True)
    first = original.model_copy(update={"id": "different-key-one", "etag": None})
    second = original.model_copy(update={"id": "different-key-two", "etag": None})

    async def create(item: Any) -> Any:
        return await repository.create_membership_request(
            item,
            service._audit(REQUESTER, item, "submitted"),
            [membership_request_event(item)],
        )

    results = await asyncio.gather(create(first), create(second), return_exceptions=True)
    assert sum(isinstance(item, ConflictError) for item in results) == 1
    old = await repository.get_membership_request(TENANT, portal.id)
    await repository.save_membership_request(old, service._audit(REQUESTER, old, "observed"), [])
    with pytest.raises(ConflictError):
        await service.submit(REQUESTER, payload())


@pytest.mark.parametrize(
    "field",
    [
        "requester_object_id",
        "group_object_id",
        "directory_tenant_id",
        "justification",
        "cost_center",
        "created_at",
    ],
)
async def test_repository_binding_snapshot_is_immutable(estate: Any, field: str) -> None:
    _, repository, service = estate
    await published(estate)
    portal = await service.submit(REQUESTER, payload())
    saved = await repository.get_membership_request(TENANT, portal.id)
    replacements = {
        "requester_object_id": WORKLOAD,
        "group_object_id": OTHER_GROUP,
        "directory_tenant_id": OTHER_GROUP,
        "justification": "Different request",
        "cost_center": saved.cost_center.model_copy(update={"id": "another-center"}),
        "created_at": saved.created_at + timedelta(seconds=1),
    }
    mutated = saved.model_copy(update={field: replacements[field]})
    with pytest.raises(ConflictError):
        await repository.save_membership_request(
            mutated,
            service._audit(ADMIN, mutated, "approved"),
            [],
        )
    assert await repository.get_membership_request(TENANT, portal.id) == saved


async def test_membership_event_conflict_rolls_back_record_guard_and_audit(estate: Any) -> None:
    _, repository, service = estate
    await published(estate)
    portal = await service.submit(REQUESTER, payload())
    first = await repository.get_membership_request(TENANT, portal.id)
    await service.close(REQUESTER, portal.id, MembershipDecision(), withdraw=True)
    second = first.model_copy(update={"id": "second-event-key", "etag": None})
    duplicate_event = membership_request_event(first).model_copy(
        update={"source_id": second.id, "subject": membership_request_event(second).subject},
    )
    with pytest.raises(ConflictError):
        await repository.create_membership_request(
            second,
            service._audit(REQUESTER, second, "submitted"),
            [duplicate_event],
        )
    assert await repository.get_membership_request(TENANT, second.id) is None
    assert (await service.submit(REQUESTER, payload())).state == "pending"


async def test_creation_switch_gates_only_new_creation_not_existing_decisions(estate: Any) -> None:
    from mosaic_api.cost_center_requests import CostCenterRequestCreate, CostCenterRequestDecision

    h, repository, _ = estate
    creation = CostCenterRequestService(
        repository,
        h.repository,
        h.service,
        validate_access=h.checks.validate_cost_center_access,
    )
    existing = await creation.submit(
        REQUESTER,
        CostCenterRequestCreate(name="Before policy change", justification="work"),
    )
    await h.service.update_settings(
        ADMIN, CostCenterSettingsUpdate(creation_requests_enabled=False)
    )
    with pytest.raises(ConflictError):
        await creation.submit(
            REQUESTER,
            CostCenterRequestCreate(name="After policy change", justification="work"),
        )
    assert (await creation.deny(ADMIN, existing.id, CostCenterRequestDecision())).state == "denied"


async def test_missing_previously_linked_operation_never_admits_another_write(
    estate: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h, repository, service = estate
    await published(estate)
    portal = await service.submit(REQUESTER, payload())
    h.graph.failures["add_member"] = failure(
        GroupFailureKind.UNKNOWN_OUTCOME,
        GroupWriteOutcome.UNKNOWN,
    )
    result = await service.approve(ADMIN, portal.id, APPROVAL)
    assert result.operation_id and result.membership_state == "unknown"
    monkeypatch.setattr(h.repository, "get_directory_operation", AsyncMock(return_value=None))
    h.graph.failures.clear()
    resumed = await service.approve(ADMIN, portal.id, APPROVAL, resume=True)
    assert resumed.membership_state == "unknown" and resumed.needs_administrator_attention
    assert "linked directory operation is unavailable" in resumed.membership_error
    assert h.graph.writes == []
    assert (
        await repository.get_membership_request(TENANT, portal.id)
    ).operation_id == result.operation_id


async def test_existing_failed_operation_and_out_of_band_inheritance_completes_without_retry(
    estate: Any,
) -> None:
    h, _, service = estate
    await published(estate)
    portal = await service.submit(REQUESTER, payload())
    h.graph.failures["add_member"] = failure()
    result = await service.approve(ADMIN, portal.id, APPROVAL)
    assert result.membership_state == "failed" and result.operation_id
    h.graph.failures.clear()
    h.lookup.state.group_members[GROUP] = [OTHER_GROUP]
    h.lookup.state.group_members[OTHER_GROUP] = [USER]
    resumed = await service.approve(ADMIN, portal.id, APPROVAL, resume=True)
    assert resumed.membership_state == "succeeded" and resumed.operation_id == result.operation_id
    assert h.graph.writes == []


async def test_unavailable_cloud_management_cannot_publish_or_join(estate: Any) -> None:
    h, _, service = estate
    await published(estate)
    h.service._directory_groups._management = None
    from mosaic_api.errors import DirectoryDisabledError

    with pytest.raises(DirectoryDisabledError):
        await h.service.update_cost_center(
            ADMIN,
            "center",
            CostCenterUpdate(discoverable=True, acknowledge_membership_changes=True),
        )
    with pytest.raises(DirectoryDisabledError):
        await service.submit(REQUESTER, payload())
    directory = await service.list_centers(REQUESTER, mine=False)
    assert directory.membership_error and directory.items[0].membership_state == "unknown"
    assert h.graph.writes == []


@pytest.mark.parametrize("persona", ["portal", "admin"])
async def test_membership_email_rendering_preserves_exact_kind_and_safe_summary(
    estate: Any,
    persona: str,
) -> None:
    from mosaic_api.integrations.email import EmailSendResult
    from mosaic_api.notifications import NotificationDelivery
    from mosaic_api.repositories.memory_budgets import InMemoryBudgetRepository
    from mosaic_api.services.notification_email import GraphNotificationDispatcher
    from mosaic_api.services.teams import _subject_path
    from test_notification_email import Sender, email_settings, mailbox

    _, repository, service = estate
    await published(estate)
    portal = await service.submit(REQUESTER, payload())
    request = await repository.get_membership_request(TENANT, portal.id)
    event = membership_request_event(request)
    path = _subject_path(event, "admin" if persona == "admin" else "portal")
    assert path and "requestKind=costCenterMembership" in path
    assert portal.id in path
    budgets = InMemoryBudgetRepository()
    await email_settings(budgets)
    sender = Sender(EmailSendResult(True, "operation"))
    dispatcher = GraphNotificationDispatcher(
        sender,
        budgets,
        mailbox_resolver=mailbox,
        portal_url="https://portal.example.test",
        frontend_url="https://admin.example.test",
    )
    delivery = NotificationDelivery(
        id="membership-delivery",
        tenant_id="tenant",
        event_ids=[event.id],
        channel="email",
        persona="portal",
        recipient_reference=USER,
    ).model_copy(update={"persona": persona})
    await dispatcher.send(delivery, [event])
    message = sender.messages[0]
    assert "requestKind=costCenterMembership" in message.plain_text
    assert portal.id in message.plain_text
    assert "cost center membership" in message.subject.casefold()
    assert "administrator review" in message.plain_text
    assert GROUP not in message.plain_text and "grant intent" not in message.plain_text


async def test_general_exemption_does_not_fabricate_charging_eligibility(estate: Any) -> None:
    h, _, service = estate
    await published(estate)
    await h.service.update_settings(
        ADMIN,
        CostCenterSettingsUpdate(default_cost_center_id="center"),
    )
    general = await service.center(REQUESTER, general_cost_center_id(TENANT))
    assert general.membership_state == "exempt" and not general.token_eligible
    stale_token = Actor(object_id=USER, tenant_id=TENANT, group_ids=frozenset({GROUP}))
    center = await service.center(stale_token, "center")
    assert center.membership_state == "notMember" and center.token_eligible
    assert await h.entitlements.list_entitlements(TENANT) == []
