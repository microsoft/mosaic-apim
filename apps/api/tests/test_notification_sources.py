from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from azure.core.async_paging import AsyncItemPaged
from azure.cosmos.aio import ContainerProxy
from mosaic_api.domain import (
    Entitlement,
    EntitlementResource,
    EntitlementResourceKind,
    EntitlementRuntime,
    EntitlementSubject,
    EntitlementSubjectKind,
    GatewaySyncRun,
    ModelAccessGrant,
    ModelAccessSettings,
    ModelAccessSnapshot,
    PublishRun,
)
from mosaic_api.notifications import NotificationEventType, NotificationWorkerState
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.notification_sources import (
    CosmosNotificationRunSource,
    InMemoryNotificationRunSource,
    NotificationSourceService,
)

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def run(identifier: str, tenant: str = "tenant") -> GatewaySyncRun:
    return GatewaySyncRun(
        id=identifier,
        tenant_id=tenant,
        gateway_id="gateway",
        status="failed",
        started_at=NOW,
        completed_at=NOW,
    )


async def test_terminal_source_pages_are_bounded_and_tenant_scoped() -> None:
    records = {str(index): run(str(index)) for index in range(125)}
    records["other"] = run("other", "other-tenant")
    records["running"] = run("running").model_copy(update={"status": "running"})
    source = InMemoryNotificationRunSource(records)
    first, continuation = await source.page("tenant", None, limit=100)
    assert len(first) == 100 and continuation
    second, final = await source.page("tenant", continuation, limit=100)
    assert len(second) == 25 and final is None
    assert len({item.id for item in first + second}) == 125
    assert all(item.tenant_id == "tenant" for item in first + second)


async def test_cosmos_source_resumes_provider_page_without_recent_run_truncation() -> None:
    document = run("older-terminal-run").model_dump(mode="json", by_alias=True)
    document["_etag"] = "storage-only"

    async def get_next(token: str | None) -> tuple[str | None, list[dict[str, object]]]:
        assert token is None
        return None, [document]

    async def extract(response: object) -> tuple[None, list[dict[str, object]]]:
        return None, [document]

    container = MagicMock(spec=ContainerProxy)
    container.query_items_change_feed.return_value = AsyncItemPaged(
        get_next=get_next,
        extract_data=extract,
    )
    source = CosmosNotificationRunSource(container)
    records, final = await source.page("tenant", "provider-checkpoint", limit=100)
    assert [record.id for record in records] == ["older-terminal-run"]
    assert final is None
    options = container.query_items_change_feed.call_args.kwargs
    assert options["partition_key"] == "tenant"
    assert options["max_item_count"] == 100
    assert options["continuation"] == "provider-checkpoint"


async def test_checkpoint_is_not_advanced_when_event_persistence_fails() -> None:
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    repository.get_worker_state.return_value = None
    repository.append_event.side_effect = RuntimeError("storage unavailable")
    service = NotificationSourceService(
        repository,
        InMemoryNotificationRunSource({"run": run("run")}),
        tenant_id="tenant",
        clock=lambda: NOW,
    )
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await service.reconcile()
    repository.update_worker_state.assert_not_awaited()


async def test_source_replay_reuses_event_identity_and_can_pause() -> None:
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    repository.get_worker_state.return_value = None
    service = NotificationSourceService(
        repository,
        InMemoryNotificationRunSource({"run": run("run")}),
        tenant_id="tenant",
        clock=lambda: NOW,
    )
    await service.reconcile()
    await service.reconcile()
    calls = repository.append_event.await_args_list
    assert calls[0].args[0].id == calls[1].args[0].id
    repository.get_worker_state.return_value = NotificationWorkerState(
        id="source",
        tenant_id="tenant",
        worker="notificationSources",
        paused=True,
    )
    await service.reconcile()
    assert repository.append_event.await_count == 2


async def test_recovery_requires_a_prior_issue_and_never_uses_an_older_success() -> None:
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    states: dict[str, NotificationWorkerState] = {}

    async def get_state(tenant: str, name: str) -> NotificationWorkerState | None:
        return states.get(name)

    async def update_state(tenant: str, name: str, change: object) -> NotificationWorkerState:
        assert callable(change)
        current = states.get(name) or NotificationWorkerState(
            id=name,
            tenant_id=tenant,
            worker=name,
        )
        changed: NotificationWorkerState = change(current)
        states[name] = changed
        return changed

    repository.get_worker_state.side_effect = get_state
    repository.update_worker_state.side_effect = update_state
    records = {
        "old-success": run("old-success").model_copy(
            update={
                "status": "succeeded",
                "completed_at": NOW - timedelta(hours=1),
            }
        ),
        "failure": run("failure"),
    }
    service = NotificationSourceService(
        repository,
        InMemoryNotificationRunSource(records),
        tenant_id="tenant",
        clock=lambda: NOW,
    )
    await service.reconcile()
    await service.reconcile()
    assert all(
        call.args[0].event_type != NotificationEventType.OPERATION_RECOVERED
        for call in repository.append_event.await_args_list
    )
    records["new-success"] = run("new-success").model_copy(
        update={
            "status": "succeeded",
            "completed_at": NOW + timedelta(minutes=1),
        }
    )
    await service.reconcile()
    recovered = [
        call.args[0]
        for call in repository.append_event.await_args_list
        if call.args[0].event_type == NotificationEventType.OPERATION_RECOVERED
    ]
    assert len(recovered) == 1


@pytest.mark.parametrize("runtime", ["pending", "applied", "failed"])
@pytest.mark.parametrize("kind", ["user", "securityGroup"])
async def test_access_ready_requires_current_derived_applied_state(runtime: str, kind: str) -> None:
    subject = EntitlementSubject(kind=EntitlementSubjectKind(kind), id="person")
    applied = ModelAccessGrant(
        entitlement_id="grant",
        subject=subject,
        object_id="person-object",
        display_name="Person",
        subscription_name="subscription" if kind == "user" else None,
        enabled=True,
        intent_digest="intent",
    )
    operation = PublishRun(
        id="publish",
        tenant_id="tenant",
        publication_id="publication",
        gateway_id="gateway",
        plan_id="plan",
        plan_digest="digest",
        status="succeeded",
        started_at=NOW - timedelta(minutes=1),
        completed_at=NOW,
        access_snapshot=ModelAccessSnapshot(
            version=1,
            settings=ModelAccessSettings(),
            grants=[applied],
        ),
    )
    current = Entitlement(
        id="grant",
        tenant_id="tenant",
        subject=subject,
        resource=EntitlementResource(kind=EntitlementResourceKind.MODEL_API, id="model"),
        runtime=EntitlementRuntime.model_validate(
            {
                "publicationId": "publication",
                "status": runtime,
                "appliedAt": NOW,
            }
        ),
    )
    repository = AsyncMock(spec=NotificationDeliveryRepository)
    repository.get_worker_state.return_value = None
    resolve = AsyncMock(return_value=current)
    service = NotificationSourceService(
        repository,
        InMemoryNotificationRunSource({"publish": operation}),
        tenant_id="tenant",
        resolve_grant=resolve,
        clock=lambda: NOW,
    )
    await service.reconcile()
    personal = [
        call.args[0]
        for call in repository.append_event.await_args_list
        if call.args[0].event_type == NotificationEventType.ACCESS_READY
    ]
    assert len(personal) == (1 if runtime == "applied" else 0)
    if personal:
        assert personal[0].audience.user_object_ids == (["person-object"] if kind == "user" else [])
        assert personal[0].subject.grant_subject == subject
        assert personal[0].subject.grant_subject_object_id == "person-object"
        assert personal[0].subject.resource == current.resource
        assert personal[0].data.requires_plan_apply is False
