"""Safe event projections of durable operation results, suitable for reconciliation."""

from mosaic_api.domain import EntitlementResource, GatewaySyncRun, PublishRun
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationEvent,
    NotificationEventType,
    NotificationSubject,
    NotificationTemplateData,
)
from mosaic_api.services.notification_events import build_notification_event

_PUBLISH_FAILURES = {"failed", "rolledBack", "rollbackFailed", "interrupted"}


def publish_run_event(
    run: PublishRun,
    *,
    resource: EntitlementResource | None = None,
    recovered: bool = False,
) -> NotificationEvent | None:
    """`recovered` requires a previously persisted issue, not merely a successful operation."""

    failed = run.status in _PUBLISH_FAILURES
    if not failed and not (run.status == "succeeded" and recovered):
        return None
    recovery_required = run.status in {"rollbackFailed", "interrupted"}
    event_type = (
        NotificationEventType.RECOVERY_REQUIRED
        if recovery_required
        else NotificationEventType.APPLY_FAILED
        if failed
        else NotificationEventType.OPERATION_RECOVERED
    )
    return build_notification_event(
        tenant_id=run.tenant_id,
        event_type=event_type,
        source_id=run.id,
        source_revision=f"{run.status}:{run.plan_digest}",
        occurred_at=run.completed_at or run.updated_at,
        origin="publishing",
        subject=NotificationSubject(
            resource=resource,
            gateway_id=run.gateway_id,
            run_id=run.id,
            publication_id=run.publication_id,
            publication_target=run.target,
            integration="gatewayApply",
        ),
        audience=NotificationAudience(include_administrators=True),
        data=NotificationTemplateData(
            summary=(
                "A gateway apply needs explicit recovery. Open the affected resource's run "
                "before making further changes."
                if recovery_required
                else "A gateway apply failed. Review the affected resource's run and plan."
                if failed
                else "A gateway apply succeeded after a previously recorded issue."
            ),
            reason_code=str(run.status),
        ),
        severity="error" if failed else "information",
    )


def gateway_sync_event(run: GatewaySyncRun, *, recovered: bool = False) -> NotificationEvent | None:
    failed = run.status in {"failed", "interrupted", "partial"}
    if not failed and not (run.status == "succeeded" and recovered):
        return None
    return build_notification_event(
        tenant_id=run.tenant_id,
        event_type=(
            NotificationEventType.SYNC_FAILED
            if failed
            else NotificationEventType.OPERATION_RECOVERED
        ),
        source_id=run.id,
        source_revision=str(run.status),
        occurred_at=run.completed_at or run.updated_at,
        origin="gatewaySync",
        subject=NotificationSubject(
            gateway_id=run.gateway_id,
            run_id=run.id,
            integration="gatewayInventory",
        ),
        audience=NotificationAudience(include_administrators=True),
        data=NotificationTemplateData(
            summary=(
                "Gateway inventory could not be refreshed completely. Open the gateway sync "
                "run for permission, connectivity, and recovery details."
                if failed
                else "Gateway inventory refreshed after a previously recorded issue."
            ),
            reason_code=str(run.status),
        ),
        severity="warning" if failed else "information",
    )
