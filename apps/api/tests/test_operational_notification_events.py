from datetime import UTC, datetime

import pytest
from mosaic_api.domain import GatewaySyncRun, PublishRun
from mosaic_api.services.operational_notification_events import (
    gateway_sync_event,
    publish_run_event,
)

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)


def publish(status: str) -> PublishRun:
    return PublishRun.model_validate(
        {
            "id": "run",
            "tenantId": "tenant",
            "publicationId": "publication",
            "gatewayId": "gateway",
            "planId": "plan",
            "planDigest": "revision",
            "status": status,
            "completedAt": NOW,
            "errors": ["secret backend detail must not leave the admin run view"],
        }
    )


@pytest.mark.parametrize("status", ["failed", "rolledBack", "rollbackFailed", "interrupted"])
def test_apply_failure_is_deterministic_and_uses_safe_summary(status: str) -> None:
    event = publish_run_event(publish(status))
    assert event and event.severity == "error"
    assert event.audience.include_administrators
    assert not event.audience.user_object_ids
    assert "secret backend" not in event.model_dump_json()
    repeated = publish_run_event(publish(status))
    assert repeated and event.id == repeated.id


def test_success_without_prior_issue_is_not_reported_as_recovery() -> None:
    assert publish_run_event(publish("succeeded")) is None
    assert publish_run_event(publish("running")) is None
    event = publish_run_event(publish("succeeded"), recovered=True)
    assert event and event.event_type == "operation.recovered"


def test_gateway_sync_failure_is_admin_only_and_redacted() -> None:
    run = GatewaySyncRun(
        id="sync",
        tenant_id="tenant",
        gateway_id="gateway",
        status="failed",
        completed_at=NOW,
        errors=["secret endpoint"],
    )
    event = gateway_sync_event(run)
    assert event and event.event_type == "operation.syncFailed"
    assert "secret endpoint" not in event.model_dump_json()
    assert gateway_sync_event(run.model_copy(update={"status": "running"})) is None
