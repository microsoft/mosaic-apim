from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError
from mosaic_api.approvals import AiApprovalReadiness, ApprovalBlocker
from mosaic_api.budgets import EmailSettings
from mosaic_api.config import Settings
from mosaic_api.notifications import (
    CapabilityCheck,
    NotificationChannelSettings,
    NotificationWorkerState,
    PersonalTeamsBinding,
    PersonalTeamsSettings,
)
from mosaic_api.repositories.base import BudgetRepository, GatewayRepository
from mosaic_api.repositories.notifications import (
    NotificationDeliveryRepository,
    NotificationRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.feature_readiness import (
    NotificationReadinessService,
    ai_approval_capability,
    container_retention_probe,
    email_capability,
    gateway_usage_probe,
)
from mosaic_api.services.telemetry import GatewayTelemetry, TelemetryCheck, TelemetryService

NOW = datetime(2026, 10, 16, 12, tzinfo=UTC)
ACTOR = Actor(object_id="user-a", tenant_id="tenant-a")


def channels(**overrides: object) -> NotificationChannelSettings:
    return NotificationChannelSettings.model_validate(
        {"id": "channels", "tenant_id": ACTOR.tenant_id, **overrides}
    )


def service() -> tuple[NotificationReadinessService, AsyncMock, AsyncMock]:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local",
        repository_backend="memory",
        tenant_id=ACTOR.tenant_id,
    )
    repository = AsyncMock(spec=NotificationRepository)
    repository.ready.return_value = True
    repository.get_channel_settings.return_value = channels()
    repository.get_personal_teams_settings.return_value = None
    repository.get_personal_teams_binding.return_value = None
    deliveries = AsyncMock(spec=NotificationDeliveryRepository)
    deliveries.get_worker_state.return_value = None
    budgets = AsyncMock(spec=BudgetRepository)
    budgets.get_email_settings.return_value = None
    return (
        NotificationReadinessService(settings, repository, deliveries, budgets, clock=lambda: NOW),
        repository,
        deliveries,
    )


def test_saved_email_and_prior_acceptance_are_not_delivery_proof() -> None:
    email = EmailSettings(
        id="email",
        tenant_id=ACTOR.tenant_id,
        enabled=True,
        provider="graph",
        sender_mailbox_id="33333333-3333-4333-8333-333333333333",
        sender="mosaic@example.com",
        last_test_at=NOW,
        last_test_revision=1,
        last_test_outcome="providerAccepted",
    )
    check = email_capability(email, channels(email_enabled=True), now=NOW)
    assert check.state == "unknown"
    assert check.last_test_at == NOW
    assert check.remediation and check.remediation.path == (
        "/settings?tab=notifications&notificationView=email"
    )
    assert email_capability(email, channels(), now=NOW).state == "disabled"


def test_email_setup_error_never_echoes_upstream_credentials() -> None:
    email = EmailSettings(
        id="email",
        tenant_id=ACTOR.tenant_id,
        enabled=True,
        provider="graph",
        sender_mailbox_id="33333333-3333-4333-8333-333333333333",
        sender="mosaic@example.com",
        last_test_error="secret-containing-provider-detail",
        last_test_revision=1,
    )
    check = email_capability(email, channels(email_enabled=True), now=NOW)
    assert check.state == "failed"
    assert "secret-containing" not in check.model_dump_json()


async def test_local_memory_and_missing_heartbeat_are_not_healthy() -> None:
    readiness, _, _ = service()
    checks = {item.feature: item for item in await readiness.admin_checks(ACTOR)}
    assert checks["notificationStorage"].state == "degraded"
    assert checks["scheduledHosting"].state == "unknown"
    assert checks["notificationWorker"].state == "degraded"


@pytest.mark.parametrize("age", [timedelta(seconds=1), timedelta(seconds=-60)])
async def test_worker_requires_fresh_nonfuture_evidence_and_no_error(age: timedelta) -> None:
    readiness, _, deliveries = service()
    deliveries.get_worker_state.return_value = NotificationWorkerState(
        id="worker",
        tenant_id=ACTOR.tenant_id,
        worker="notifications",
        heartbeat_at=NOW - age,
    )
    checks = await readiness.admin_checks(ACTOR)
    worker = next(item for item in checks if item.feature == "notificationWorker")
    assert worker.state == ("verified" if age.total_seconds() >= 0 else "degraded")
    deliveries.get_worker_state.return_value.last_error = "send failed"
    checks = await readiness.admin_checks(ACTOR)
    assert next(item for item in checks if item.feature == "notificationWorker").state == "degraded"


async def test_private_health_is_self_scoped_and_hides_conversation_details() -> None:
    readiness, repository, _ = service()
    repository.get_channel_settings.return_value = channels(teams_personal_enabled=True)
    repository.get_personal_teams_settings.return_value = PersonalTeamsSettings(
        id="teams",
        tenant_id=ACTOR.tenant_id,
        enabled=True,
        app_id="app",
        bot_app_id="current-bot",
    )
    repository.get_personal_teams_binding.return_value = PersonalTeamsBinding(
        id="binding",
        tenant_id=ACTOR.tenant_id,
        user_object_id=ACTOR.object_id,
        conversation_id="private-conversation",
        service_url="https://smba.trafficmanager.net/teams/",
        bot_app_id="old-bot",
        authenticated_at=NOW,
    )
    checks = await readiness.personal_checks(ACTOR)
    repository.get_personal_teams_binding.assert_awaited_once_with(ACTOR.tenant_id, ACTOR.object_id)
    assert next(item for item in checks if item.feature == "teamsPersonal").state == "needsAction"
    serialized = str([check.model_dump() for check in checks])
    assert "private-conversation" not in serialized
    assert "smba.trafficmanager" not in serialized
    assert not any(item.feature == "notificationWorker" for item in checks)


async def test_gateway_notifications_reuse_telemetry_checks_without_mutation() -> None:
    gateways = AsyncMock(spec=GatewayRepository)
    gateways.list_gateways.return_value = [SimpleNamespace(id="gateway")]
    telemetry = AsyncMock(spec=TelemetryService)
    telemetry.status.return_value = GatewayTelemetry(
        gateway_id="gateway",
        gateway_name="Gateway",
        management_mode="manage",
        ready=False,
        can_enable=True,
        rollups_enabled=True,
        checked_at=NOW,
        workspace_id=None,
        apis=[],
        probe=None,
        rollup=None,
        checks=[
            TelemetryCheck(
                id="logAccess",
                status="error",
                title="Log Analytics access missing",
                detail="Grant the API identity workspace read access.",
                command="az role assignment create --role 'Log Analytics Reader'",
            ),
            TelemetryCheck(
                id="logger",
                status="ok",
                title="Gateway logger verified",
                detail="Logger exists.",
            ),
        ],
    )
    probe = gateway_usage_probe(gateways, telemetry, configured=True)
    checks = await probe(ACTOR)
    assert [check.state for check in checks] == ["failed", "verified"]
    assert checks[0].scope == "gateway" and checks[0].remediation
    assert checks[0].remediation.command
    telemetry.enable.assert_not_awaited()
    telemetry.status.reset_mock()
    checks = await gateway_usage_probe(gateways, telemetry, configured=False)(ACTOR)
    assert checks[0].state == "notConfigured"
    telemetry.status.assert_not_awaited()


@pytest.mark.parametrize(
    "age,verified",
    [
        (timedelta(minutes=1), True),
        (timedelta(days=2), False),
        (timedelta(seconds=-1), False),
    ],
)
def test_ai_readiness_requires_recent_explicit_verified_response_contract(
    age: timedelta,
    verified: bool,
) -> None:
    result = AiApprovalReadiness(
        ready=True,
        configured=True,
        connection_verified=True,
        response_contract_verified=True,
        checked_at=NOW,
        last_successful_call_at=NOW - age,
    )
    assert (ai_approval_capability(result, now=NOW).state == "verified") is verified
    result.response_contract_verified = False
    assert ai_approval_capability(result, now=NOW).state == "needsAction"


def test_ai_setup_blocker_preserves_specific_remediation_without_calling_the_model() -> None:
    check = ai_approval_capability(
        AiApprovalReadiness(
            configured=True,
            checked_at=NOW,
            blockers=[
                ApprovalBlocker(
                    code="grantMissing",
                    message="The selected caller has no grant.",
                    next_action="Grant the caller the chosen model and review its plan.",
                )
            ],
        ),
        now=NOW,
    )
    assert check.state == "needsAction"
    assert check.remediation and "Grant the caller" in check.remediation.action


async def test_slow_workers_use_their_own_interval_without_extending_old_evidence() -> None:
    readiness, _, deliveries = service()
    readiness._workers = ("notificationUsage",)
    readiness._worker_intervals = {"notificationUsage": 900}
    deliveries.get_worker_state.return_value = NotificationWorkerState(
        id="worker",
        tenant_id=ACTOR.tenant_id,
        worker="notificationUsage",
        heartbeat_at=NOW - timedelta(minutes=10),
    )
    checks = await readiness.admin_checks(ACTOR)
    worker = next(item for item in checks if item.feature == "notificationWorker")
    assert worker.state == "verified"
    assert worker.fresh_until == NOW + timedelta(minutes=35)


async def test_personal_processing_is_not_green_when_only_inbox_storage_is_available() -> None:
    readiness, _, _ = service()
    checks = {check.feature: check for check in await readiness.personal_checks(ACTOR)}
    assert checks["inApp"].state == "verified"
    assert checks["notificationProcessing"].state == "degraded"
    assert checks["notificationProcessing"].remediation
    assert checks["notificationProcessing"].remediation.path is None


async def test_background_tenant_checks_never_collect_an_administrators_private_usage() -> None:
    readiness, _, _ = service()
    personal = AsyncMock(
        return_value=[
            CapabilityCheck(
                feature="privateUsage",
                scope="self",
                state="unknown",
                checked_at=NOW,
            )
        ]
    )
    readiness._admin_self_probes = (personal,)
    checks = await readiness.admin_checks(ACTOR, include_personal=False)
    assert not any(check.feature == "privateUsage" for check in checks)
    personal.assert_not_awaited()
    checks = await readiness.admin_checks(ACTOR)
    assert any(check.feature == "privateUsage" for check in checks)
    personal.assert_awaited_once_with(ACTOR)


@pytest.mark.parametrize(
    "ttl,expected",
    [
        (-1, "verified"),
        (None, "needsAction"),
        (86400, "needsAction"),
    ],
)
async def test_retention_requires_actual_item_only_container_expiry(
    ttl: int | None,
    expected: str,
) -> None:
    container = AsyncMock(spec=ContainerProxy)
    container.read.return_value = {"defaultTtl": ttl}
    checks = await container_retention_probe(container, scope="desired-state")(ACTOR)
    assert checks[0].state == expected
    assert checks[0].scope == "desired-state"
    container.read.assert_awaited_once_with()
    container.upsert_item.assert_not_awaited()


async def test_retention_permission_failures_are_actionable_and_do_not_echo_provider_details() -> (
    None
):
    container = AsyncMock(spec=ContainerProxy)
    container.read.side_effect = CosmosHttpResponseError(status_code=403, message="private-detail")
    check = (await container_retention_probe(container, scope="usage-rollups")(ACTOR))[0]
    assert check.state == "failed"
    assert check.remediation and "Cosmos read access" in check.remediation.action
    assert "private-detail" not in check.model_dump_json()
