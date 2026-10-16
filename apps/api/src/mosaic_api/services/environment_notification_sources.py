"""Commit each environment transition with its cursor, without snapshot-sized journals."""

import json
from collections.abc import Awaitable, Callable

from pydantic import BaseModel, Field

from mosaic_api.notifications import (
    NotificationAudience,
    NotificationEvent,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
    NotificationWorkerState,
)
from mosaic_api.repositories.notifications import NotificationDeliveryRepository
from mosaic_api.services.environment_findings import EnvironmentFindingList
from mosaic_api.services.notification_events import build_notification_event, notification_record_id


class _FindingCursor(BaseModel):
    generation: int = Field(default=0, ge=0)
    findings: dict[str, str] = Field(default_factory=dict, max_length=2000)


class EnvironmentNotificationSource:
    def __init__(
        self,
        repository: NotificationDeliveryRepository,
        scan: Callable[[], Awaitable[EnvironmentFindingList]],
        *,
        tenant_id: str,
    ) -> None:
        self._repository = repository
        self._scan = scan
        self._tenant = tenant_id

    async def reconcile(self) -> None:
        state = await self._repository.get_worker_state(self._tenant, "environmentFindings")
        cursor = (
            _FindingCursor.model_validate_json(state.checkpoint)
            if state and state.checkpoint
            else _FindingCursor()
        )
        result = await self._scan()
        if len(result.items) > 2000:
            raise ValueError("Environment findings exceed the notification scan bound of 2000")
        current = {
            finding.id: notification_record_id(
                "environmentRevision",
                finding.kind,
                finding.gateway_id,
                json.dumps(finding.gateway_environment),
                finding.subject.id,
                finding.target.resource_id,
                json.dumps(finding.target.environment),
                finding.verdict.model_dump_json(),
                finding.confidence,
            )
            for finding in result.items
        }
        changed = {
            identifier
            for identifier, revision in current.items()
            if cursor.findings.get(identifier) != revision
        }
        removed = set(cursor.findings) - set(current) if not result.limitations else set()
        if not changed and not removed:
            return
        generation = cursor.generation + 1
        events: list[NotificationEvent] = []
        for finding in result.items:
            if finding.id not in changed:
                continue
            events.append(
                build_notification_event(
                    tenant_id=self._tenant,
                    event_type=NotificationEventType.READINESS_FINDING,
                    source_id=finding.id,
                    source_revision=f"{generation}:{current[finding.id]}",
                    occurred_at=result.generated_at,
                    origin="environmentFindings",
                    subject=NotificationSubject(
                        gateway_id=finding.gateway_id,
                        endpoint_id=finding.target.resource_id,
                        endpoint_kind="model"
                        if finding.target.resource_kind == "modelEndpoint"
                        else "mcp",
                        integration="environmentBoundary",
                    ),
                    audience=NotificationAudience(include_administrators=True),
                    data=NotificationTemplateData(
                        summary=(
                            "An environment boundary finding needs review. Open gateway findings "
                            f"for its evidence and remediation (confidence: {finding.confidence})."
                        ),
                        reason_code=finding.kind,
                    ),
                    severity="warning",
                )
            )
        for identifier in sorted(removed):
            events.append(
                build_notification_event(
                    tenant_id=self._tenant,
                    event_type=NotificationEventType.OPERATION_RECOVERED,
                    source_id=identifier,
                    source_revision=f"{generation}:{cursor.findings[identifier]}",
                    occurred_at=result.generated_at,
                    origin="environmentFindings",
                    subject=NotificationSubject(integration="environmentBoundary"),
                    audience=NotificationAudience(include_administrators=True),
                    data=NotificationTemplateData(
                        summary=(
                            "A previously recorded environment finding is absent "
                            "from a complete scan."
                        ),
                    ),
                )
            )
        # Free removed entries first so replacing a full scan never exceeds 2000 cursor entries.
        events.sort(key=lambda event: event.event_type != NotificationEventType.OPERATION_RECOVERED)
        for index, event in enumerate(events):
            findings = dict(cursor.findings)
            if event.event_type == NotificationEventType.OPERATION_RECOVERED:
                findings.pop(event.source_id)
            else:
                findings[event.source_id] = current[event.source_id]
            cursor = _FindingCursor(generation=generation, findings=findings)
            checkpoint = cursor.model_dump_json()
            record = state or NotificationWorkerState(
                id=notification_record_id(
                    "notificationWorker", self._tenant, "environmentFindings"
                ),
                tenant_id=self._tenant,
                worker="environmentFindings",
            )
            await self._repository.commit_source_event(
                event,
                checkpoint=record.model_copy(
                    update={
                        "checkpoint": checkpoint,
                        "heartbeat_at": result.generated_at,
                        "last_succeeded_at": result.generated_at
                        if index == len(events) - 1
                        else record.last_succeeded_at,
                        "last_error": None,
                        "updated_at": result.generated_at,
                    }
                ),
            )
            state = await self._repository.get_worker_state(self._tenant, "environmentFindings")
            if state is None or state.checkpoint != checkpoint:
                raise ValueError(
                    "Environment notification checkpoint changed during reconciliation"
                )
