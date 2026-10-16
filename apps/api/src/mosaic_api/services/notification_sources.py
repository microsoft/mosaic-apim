"""Reconcile durable operation results across containers into the notification outbox."""

from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from functools import partial
from typing import Protocol

from azure.core.async_paging import AsyncPageIterator
from azure.cosmos.aio import ContainerProxy

from mosaic_api.domain import (
    Entitlement,
    GatewaySyncRun,
    McpEndpointSyncRun,
    ModelEndpointSyncRun,
    PublishRun,
    utc_now,
)
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
    NotificationWorkerState,
)
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.notification_events import build_notification_event
from mosaic_api.services.operational_notification_events import (
    gateway_sync_event,
    publish_run_event,
)

OperationRun = GatewaySyncRun | PublishRun | ModelEndpointSyncRun | McpEndpointSyncRun
_RUN_TYPES: dict[str, type[OperationRun]] = {
    str(model.model_fields["entity_type"].default): model
    for model in (GatewaySyncRun, PublishRun, ModelEndpointSyncRun, McpEndpointSyncRun)
}


class NotificationRunSource(Protocol):
    async def page(
        self, tenant_id: str, checkpoint: str | None, *, limit: int
    ) -> tuple[list[OperationRun], str | None]: ...


class CosmosNotificationRunSource:
    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    async def page(
        self, tenant_id: str, checkpoint: str | None, *, limit: int
    ) -> tuple[list[OperationRun], str | None]:
        iterator = self._container.query_items_change_feed(
            partition_key=tenant_id,
            start_time="Beginning",
            continuation=checkpoint,
            max_item_count=limit,
        ).by_page()
        if not isinstance(iterator, AsyncPageIterator):
            raise TypeError("Cosmos notification replay requires a resumable page iterator")
        try:
            page = await anext(iterator)
        except StopAsyncIteration:
            return [], checkpoint
        runs: list[OperationRun] = []
        async for document in page:
            payload = {key: value for key, value in document.items() if not key.startswith("_")}
            model = _RUN_TYPES.get(payload.get("entityType", ""))
            if model is None or payload.get("status") == "running":
                continue
            run = model.model_validate(payload)
            if run.tenant_id == tenant_id and run.status != "running":
                runs.append(run)
        return runs, iterator.continuation_token


class InMemoryNotificationRunSource:
    def __init__(self, *collections: Mapping[str, OperationRun]) -> None:
        self._collections = collections

    async def page(
        self, tenant_id: str, checkpoint: str | None, *, limit: int
    ) -> tuple[list[OperationRun], str | None]:
        runs = sorted(
            (
                run
                for collection in self._collections
                for run in collection.values()
                if run.tenant_id == tenant_id and run.status != "running"
            ),
            key=lambda run: (run.started_at, run.id),
        )
        start = int(checkpoint or "0")
        end = start + limit
        return runs[start:end], str(end) if end < len(runs) else None


class NotificationSourceService:
    """Each page checkpoints only after its events are durable; replay is idempotent."""

    def __init__(
        self,
        repository: NotificationDeliveryRepository,
        source: NotificationRunSource,
        *,
        tenant_id: str,
        additional_sources: tuple[Callable[[], Awaitable[None]], ...] = (),
        resolve_grant: Callable[[str], Awaitable[Entitlement | None]] | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._source = source
        self._tenant = tenant_id
        self._additional = additional_sources
        self._resolve_grant = resolve_grant
        self._clock = clock

    async def reconcile(self) -> None:
        state = await self._repository.get_worker_state(self._tenant, "notificationSources")
        now = self._clock()
        if state and state.paused:
            return
        runs, checkpoint = await self._source.page(
            self._tenant, state.checkpoint if state else None, limit=100
        )
        for run in runs:
            cursor_name = (
                f"operation:publish:{run.target}:{run.publication_id}"
                if isinstance(run, PublishRun)
                else f"operation:gateway:{run.gateway_id}"
                if isinstance(run, GatewaySyncRun)
                else f"operation:{run.entity_type}:{run.endpoint_id}"
            )
            occurred = run.completed_at or run.updated_at
            previous = await self._repository.get_worker_state(self._tenant, cursor_name)
            newer = self._newer(previous, occurred, run.id)
            failed = run.status in {
                "failed",
                "partial",
                "rolledBack",
                "rollbackFailed",
                "interrupted",
            }
            recovered = bool(
                newer and previous and previous.last_error and run.status == "succeeded"
            )
            if isinstance(run, PublishRun):
                event = publish_run_event(run, recovered=recovered)
            elif isinstance(run, GatewaySyncRun):
                event = gateway_sync_event(run, recovered=recovered)
            elif failed or recovered:
                event = build_notification_event(
                    tenant_id=run.tenant_id,
                    event_type=(
                        NotificationEventType.SYNC_FAILED
                        if failed
                        else NotificationEventType.OPERATION_RECOVERED
                    ),
                    source_id=run.id,
                    source_revision=str(run.status),
                    occurred_at=run.completed_at or run.updated_at,
                    origin="endpointSync",
                    subject=NotificationSubject(
                        endpoint_id=run.endpoint_id,
                        run_id=run.id,
                        endpoint_kind="model" if isinstance(run, ModelEndpointSyncRun) else "mcp",
                        integration="endpointInventory",
                    ),
                    audience=NotificationAudience(include_administrators=True),
                    data=NotificationTemplateData(
                        summary=(
                            "Endpoint inventory needs attention. Review its sync run."
                            if failed
                            else "Endpoint inventory refreshed after a recorded issue."
                        ),
                        reason_code=str(run.status),
                    ),
                    severity="warning" if failed else "information",
                )
            else:
                event = None
            if event:
                await self._repository.append_event(event)
            if isinstance(run, PublishRun):
                await self._personal_apply_events(run)
            if newer:
                await self._repository.update_worker_state(
                    self._tenant,
                    cursor_name,
                    partial(
                        self._advance_operation,
                        occurred=occurred,
                        identifier=run.id,
                        failure=str(run.status) if failed else None,
                    ),
                )
        for produce in self._additional:
            await produce()

        def complete(current: NotificationWorkerState) -> NotificationWorkerState:
            return current.model_copy(
                update={
                    "heartbeat_at": now,
                    "last_succeeded_at": self._clock(),
                    "checkpoint": checkpoint,
                    "last_error": None,
                }
            )

        await self._repository.update_worker_state(self._tenant, "notificationSources", complete)

    async def _personal_apply_events(self, run: PublishRun) -> None:
        if self._resolve_grant is None:
            return
        snapshot = run.access_snapshot or run.mcp_access_snapshot or run.pool_access_snapshot
        if snapshot is None:
            return
        for applied in snapshot.grants:
            if applied.subject.kind not in {"user", "securityGroup"} or not applied.enabled:
                continue
            current = await self._resolve_grant(applied.entitlement_id)
            if (
                current is None
                or current.tenant_id != self._tenant
                or current.subject != applied.subject
                or not current.enabled
                or current.runtime is None
                or current.runtime.publication_id != run.publication_id
            ):
                continue
            ready = (
                run.status == "succeeded"
                and current.runtime.status == "applied"
                and current.runtime.applied_at is not None
                and run.completed_at is not None
                and run.started_at <= current.runtime.applied_at <= run.completed_at
            )
            failed = run.status in {
                "failed",
                "partial",
                "rolledBack",
                "rollbackFailed",
                "interrupted",
            } and current.runtime.status in {"failed", "unknown"}
            if not ready and not failed:
                continue
            await self._repository.append_event(
                build_notification_event(
                    tenant_id=self._tenant,
                    event_type=(
                        NotificationEventType.ACCESS_READY
                        if ready
                        else NotificationEventType.ACCESS_APPLY_FAILED
                    ),
                    source_id=run.id,
                    source_revision=f"{current.id}:{applied.intent_digest}",
                    occurred_at=run.completed_at or run.updated_at,
                    origin="publishing",
                    subject=NotificationSubject(
                        resource=current.resource,
                        entitlement_id=current.id,
                        cost_center_id=current.cost_center_id or None,
                        grant_subject=applied.subject,
                        grant_subject_object_id=applied.object_id,
                    ),
                    audience=NotificationAudience(
                        include_administrators=True,
                        user_object_ids=[applied.object_id]
                        if applied.subject.kind == "user"
                        else [],
                    ),
                    data=NotificationTemplateData(
                        summary=(
                            "Approved access is applied. Open its access details to connect."
                            if ready
                            else "A plan applying approved access failed. Existing access may be "
                            "unchanged; review your status or contact an administrator."
                        )
                    ),
                    severity="information" if ready else "warning",
                )
            )

    @staticmethod
    def _newer(
        current: NotificationWorkerState | None,
        occurred: datetime,
        identifier: str,
    ) -> bool:
        return (
            current is None
            or current.heartbeat_at is None
            or (occurred, identifier) > (current.heartbeat_at, current.checkpoint or "")
        )

    @classmethod
    def _advance_operation(
        cls,
        current: NotificationWorkerState,
        *,
        occurred: datetime,
        identifier: str,
        failure: str | None,
    ) -> NotificationWorkerState:
        if not cls._newer(current, occurred, identifier):
            return current
        return current.model_copy(
            update={
                "heartbeat_at": occurred,
                "checkpoint": identifier,
                "last_error": failure,
            }
        )
