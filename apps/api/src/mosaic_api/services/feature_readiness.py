"""Read-only capability evidence. Saving configuration is never a connectivity test."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta

from azure.cosmos.aio import ContainerProxy
from azure.cosmos.exceptions import CosmosHttpResponseError

from mosaic_api.approvals import AiApprovalReadiness
from mosaic_api.budgets import EmailSettings
from mosaic_api.config import RepositoryBackend, Settings
from mosaic_api.domain import utc_now
from mosaic_api.notifications import (
    CapabilityCheck,
    CapabilityRemediation,
    CapabilityState,
    NotificationChannelSettings,
)
from mosaic_api.repositories.base import BudgetRepository, GatewayRepository
from mosaic_api.repositories.notifications import (
    NotificationDeliveryRepository,
    NotificationRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.telemetry import TelemetryService

CapabilityProbe = Callable[[Actor], Awaitable[list[CapabilityCheck]]]


def container_retention_probe(container: ContainerProxy, *, scope: str) -> CapabilityProbe:
    async def check(actor: Actor) -> list[CapabilityCheck]:
        now = utc_now()
        try:
            metadata = await container.read()
        except CosmosHttpResponseError:
            state: CapabilityState = "failed"
            reason = "Container expiry metadata could not be read."
        else:
            enabled = metadata.get("defaultTtl") == -1
            state = "verified" if enabled else "needsAction"
            reason = (
                "Item expiry is enabled without a default lifetime for ordinary state."
                if enabled
                else "Item expiry must be enabled with defaultTtl=-1 for notification retention."
            )
        return [
            CapabilityCheck(
                feature="notificationRetention",
                scope=scope,
                state=state,
                checked_at=now,
                fresh_until=now + timedelta(minutes=5) if state == "verified" else None,
                reason=reason,
                evidence=["The container's current defaultTtl is -1."]
                if state == "verified"
                else [],
                remediation=None
                if state == "verified"
                else CapabilityRemediation(
                    action=(
                        "Apply the updated Cosmos infrastructure with defaultTtl=-1. "
                        "Keep ordinary settings, grants and monthly records without an item ttl. "
                        "If metadata cannot be read, repair the API identity's Cosmos read access."
                    ),
                    path="/settings?tab=runtime",
                    owner="azureAdmin",
                ),
            )
        ]

    return check


def ai_approval_capability(result: AiApprovalReadiness, *, now: datetime) -> CapabilityCheck:
    tested = result.last_successful_call_at
    fresh = bool(tested and timedelta(0) <= now - tested <= timedelta(hours=24))
    verified = (
        result.ready and result.connection_verified and result.response_contract_verified and fresh
    )
    return CapabilityCheck(
        feature="approvalAi",
        scope="tenant",
        required=False,
        state="verified" if verified else "needsAction" if result.configured else "notConfigured",
        checked_at=now,
        reason=(
            "Current setup and a recent explicit governed-model test have supporting evidence."
            if verified
            else result.blockers[0].message
            if result.blockers
            else "Complete setup and explicitly test the governed model's response contract."
        ),
        evidence=[blocker.message for blocker in result.blockers[:20]]
        if not verified
        else [
            "Current caller, grant and applied route checks passed.",
            f"An explicit model test succeeded at {tested.isoformat()}." if tested else "",
        ],
        last_test_at=tested,
        fresh_until=now + timedelta(minutes=5) if verified else None,
        remediation=CapabilityRemediation(
            action=(
                result.blockers[0].next_action
                if result.blockers and result.blockers[0].next_action
                else "Review AI setup, caller identity, Entitlements and applied model plan; "
                "then explicitly run its low-cost test."
            ),
            path="/settings?tab=approval-ai",
        ),
    )


def gateway_usage_probe(
    gateways: GatewayRepository,
    telemetry: TelemetryService,
    *,
    configured: bool,
) -> CapabilityProbe:
    async def check(actor: Actor) -> list[CapabilityCheck]:
        now = utc_now()
        if not configured:
            return [
                CapabilityCheck(
                    feature="usageNotifications",
                    scope="tenant",
                    state="notConfigured",
                    checked_at=now,
                    reason="Measured gateway rollups are not configured.",
                    remediation=CapabilityRemediation(
                        action=(
                            "Configure Log Analytics and measured usage rollups. Simulated local "
                            "figures do not establish live quota or budget notification readiness."
                        ),
                        path="/settings?tab=runtime",
                        owner="azureAdmin",
                    ),
                )
            ]
        checks: list[CapabilityCheck] = []
        for gateway in await gateways.list_gateways(actor.tenant_id):
            status = await telemetry.status(actor, gateway.id)
            for prerequisite in status.checks:
                state: CapabilityState = (
                    "verified"
                    if prerequisite.status == "ok"
                    else "needsAction"
                    if prerequisite.status == "warning"
                    else "failed"
                    if prerequisite.status == "error"
                    else "unknown"
                )
                checks.append(
                    CapabilityCheck(
                        feature=f"gatewayTelemetry:{prerequisite.id}",
                        scope=gateway.id,
                        state=state,
                        checked_at=status.checked_at,
                        reason=prerequisite.title,
                        evidence=[f"{prerequisite.title}: {prerequisite.status}."],
                        fresh_until=status.checked_at + timedelta(minutes=5)
                        if state == "verified"
                        else None,
                        remediation=CapabilityRemediation(
                            action=(
                                "Open this gateway's Telemetry checks for the current missing "
                                "prerequisite and explicit enable or apply workflow."
                            ),
                            path=f"/gateways/{gateway.id}",
                            command=prerequisite.command,
                            owner="azureAdmin",
                        ),
                    )
                )
        if not checks:
            checks.append(
                CapabilityCheck(
                    feature="usageNotifications",
                    scope="tenant",
                    state="needsAction",
                    checked_at=now,
                    reason="No gateway telemetry is available to check.",
                    remediation=CapabilityRemediation(
                        action="Register a gateway and complete its Telemetry checks.",
                        path="/gateways",
                        owner="mosaicAdmin",
                    ),
                )
            )
        return checks

    return check


def email_capability(
    email: EmailSettings | None,
    channels: NotificationChannelSettings | None,
    *,
    now: datetime,
) -> CapabilityCheck:
    enabled = bool(channels and channels.email_enabled)
    remediation = CapabilityRemediation(
        action=(
            "Select a Microsoft Graph sender mailbox. Ask an Exchange administrator to scope "
            "Application Mail.Send to that mailbox for the API managed identity, run Check "
            "access, then explicitly send a test."
        ),
        path="/settings?tab=notifications&notificationView=email",
        owner="azureAdmin",
    )
    if not enabled:
        state = "disabled"
        reason = "General notification email is off. Existing budget-email settings are separate."
    elif email and email.migration_required:
        state = "needsAction"
        reason = "Legacy ACS settings must be replaced with a Graph sender mailbox."
    elif email is None or not email.configured:
        state = "notConfigured"
        reason = "Configure the sending mailbox's Entra object ID for Microsoft Graph."
    elif email.last_test_revision != email.revision:
        state = "needsAction"
        reason = "The saved Graph configuration has not been tested, or previous evidence is stale."
    elif email.last_test_error:
        state = "failed"
        reason = "The last email test failed. Review the email test result and permission guidance."
    else:
        state = "unknown"
        reason = (
            "Email configuration is saved. A previous test or provider acceptance does not "
            "prove current permissions or receipt by the recipient."
        )
    return CapabilityCheck(
        feature="email",
        scope="tenant",
        required=False,
        state=state,
        reason=reason,
        checked_at=now,
        last_test_at=email.last_test_at if email else None,
        remediation=remediation,
    )


class NotificationReadinessService:
    def __init__(
        self,
        settings: Settings,
        repository: NotificationRepository,
        deliveries: NotificationDeliveryRepository,
        budgets: BudgetRepository,
        *,
        worker_names: Sequence[str] = ("notifications",),
        worker_intervals: Mapping[str, float] | None = None,
        admin_probes: Sequence[CapabilityProbe] = (),
        admin_self_probes: Sequence[CapabilityProbe] = (),
        personal_probes: Sequence[CapabilityProbe] = (),
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._deliveries = deliveries
        self._budgets = budgets
        self._workers = tuple(worker_names)
        self._worker_intervals = dict(worker_intervals or {})
        self._admin_probes = tuple(admin_probes)
        self._admin_self_probes = tuple(admin_self_probes)
        self._personal_probes = tuple(personal_probes)
        self._clock = clock

    async def admin_checks(
        self,
        actor: Actor,
        *,
        include_personal: bool = True,
    ) -> list[CapabilityCheck]:
        now = self._clock()
        settings = self._settings
        ready = await self._repository.ready()
        durable = settings.repository_backend is RepositoryBackend.COSMOS
        checks = [
            CapabilityCheck(
                feature="notificationStorage",
                scope="tenant",
                state="verified" if ready and durable else "degraded" if ready else "failed",
                reason=(
                    "Notification storage is not reachable."
                    if not ready
                    else "Notification storage is reachable."
                    if durable
                    else "Local in-memory notifications are lost when the API restarts."
                ),
                checked_at=now,
                evidence=["The configured repository answered its readiness check."]
                if ready
                else [],
                remediation=None
                if ready and durable
                else CapabilityRemediation(
                    action="Use reachable Cosmos persistence for durable delivery and schedules.",
                    path="/settings?tab=runtime",
                    owner="azureAdmin",
                ),
            ),
            CapabilityCheck(
                feature="scheduledHosting",
                scope="tenant",
                state="unknown",
                checked_at=now,
                reason=(
                    "The deployment declares Always On. Verify it in the host configuration; "
                    "an application setting alone is not proof that an instance stays awake."
                    if settings.scheduled_workers_always_on
                    else "Always-on hosting has not been declared for scheduled processing."
                ),
                remediation=CapabilityRemediation(
                    action=(
                        "Keep at least one API instance awake (App Service Always On or a minimum "
                        "running replica). Then verify worker heartbeats; scale-to-zero delays "
                        "digests and policy evaluation."
                    ),
                    path="/settings?tab=runtime",
                    owner="azureAdmin",
                ),
            ),
        ]
        channels = await self._repository.get_channel_settings(actor.tenant_id)
        checks.append(
            email_capability(
                await self._budgets.get_email_settings(actor.tenant_id), channels, now=now
            )
        )
        origins_ready = bool(
            settings.frontend_url
            and settings.frontend_url.scheme == "https"
            and settings.portal_url
            and settings.portal_url.scheme == "https"
        )
        checks.append(
            CapabilityCheck(
                feature="notificationLinkConfiguration",
                scope="tenant",
                required=False,
                state="verified" if origins_ready else "needsAction",
                checked_at=now,
                reason=(
                    "HTTPS application links are configured; frontend availability is not tested."
                    if origins_ready
                    else "Public HTTPS console and portal origins are needed for message links."
                ),
                evidence=["Both application origins use HTTPS."] if origins_ready else [],
                remediation=None
                if origins_ready
                else CapabilityRemediation(
                    action=(
                        "Set MOSAIC_FRONTEND_URL and MOSAIC_PORTAL_URL to public HTTPS origins."
                    ),
                    path="/settings?tab=runtime",
                    owner="azureAdmin",
                ),
            )
        )
        for worker in self._workers:
            record = await self._deliveries.get_worker_state(actor.tenant_id, worker)
            lifetime = timedelta(
                seconds=max(
                    self._worker_intervals.get(worker, settings.notification_interval_seconds) * 3,
                    120,
                )
            )
            fresh = bool(
                record
                and record.heartbeat_at
                and timedelta(0) <= now - record.heartbeat_at < lifetime
            )
            paused = bool(record and record.paused)
            enabled = settings.notifications_enabled and not paused
            failed = bool(record and record.last_error)
            checks.append(
                CapabilityCheck(
                    feature="notificationWorker",
                    scope=worker,
                    state=(
                        "disabled"
                        if not enabled
                        else "verified"
                        if fresh and not failed
                        else "degraded"
                    ),
                    reason=(
                        "The worker is disabled or paused."
                        if not enabled
                        else "The worker reported a processing failure. Review delivery health."
                        if failed
                        else "The worker heartbeat is current."
                        if fresh
                        else "No current worker heartbeat is available."
                    ),
                    checked_at=now,
                    fresh_until=record.heartbeat_at + lifetime
                    if fresh and record and record.heartbeat_at
                    else None,
                    evidence=[f"Heartbeat at {record.heartbeat_at.isoformat()}."]
                    if fresh and record and record.heartbeat_at
                    else [],
                    remediation=CapabilityRemediation(
                        action=(
                            "Check the worker configuration, API host and storage connectivity. "
                            "Review delivery failures and backlog before resuming."
                        ),
                        path="/settings?tab=delivery-health",
                    ),
                )
            )
        for probe in self._admin_probes:
            checks.extend(await probe(actor))
        if include_personal:
            for probe in self._admin_self_probes:
                checks.extend(await probe(actor))
        return checks

    async def personal_checks(self, actor: Actor) -> list[CapabilityCheck]:
        now = self._clock()
        channels = await self._repository.get_channel_settings(actor.tenant_id)
        in_app = channels is None or channels.in_app_enabled
        storage_ready = await self._repository.ready()
        checks = [
            CapabilityCheck(
                feature="inApp",
                scope="self",
                required=False,
                state="disabled" if not in_app else "verified" if storage_ready else "failed",
                reason="In-app notifications are available."
                if in_app and storage_ready
                else "Disabled by an administrator."
                if not in_app
                else "Notification storage is not currently available.",
                checked_at=now,
                evidence=["The inbox storage answered its readiness check."]
                if in_app and storage_ready
                else [],
            ),
        ]
        worker = await self._deliveries.get_worker_state(actor.tenant_id, "notifications")
        fresh = bool(
            worker
            and worker.heartbeat_at
            and timedelta(0)
            <= now - worker.heartbeat_at
            < timedelta(seconds=max(self._settings.notification_interval_seconds * 3, 120))
        )
        enabled = self._settings.notifications_enabled and not (worker and worker.paused)
        healthy = fresh and not (worker and worker.last_error)
        checks.append(
            CapabilityCheck(
                feature="notificationProcessing",
                scope="self",
                state="disabled" if not enabled else "verified" if healthy else "degraded",
                checked_at=now,
                reason=(
                    "New notification processing has a current heartbeat."
                    if enabled and healthy
                    else "New notification processing is disabled."
                    if not enabled
                    else "New notification processing has no current successful heartbeat."
                ),
                remediation=None
                if enabled and healthy
                else CapabilityRemediation(
                    action="Ask a MOSAIC administrator to check notification delivery health.",
                    owner="mosaicAdmin",
                ),
            )
        )
        email = email_capability(
            await self._budgets.get_email_settings(actor.tenant_id), channels, now=now
        )
        checks.append(
            email.model_copy(
                update={
                    "scope": "self",
                    "remediation": CapabilityRemediation(
                        action="Ask a MOSAIC administrator to check notification email setup.",
                        owner="mosaicAdmin",
                    ),
                }
            )
        )
        teams = await self._repository.get_personal_teams_settings(actor.tenant_id)
        binding = await self._repository.get_personal_teams_binding(
            actor.tenant_id, actor.object_id
        )
        enabled = bool(channels and channels.teams_personal_enabled and teams and teams.enabled)
        installed = bool(
            binding
            and binding.state == "installed"
            and teams
            and binding.bot_app_id == teams.bot_app_id
            and binding.cloud == teams.cloud
        )
        checks.append(
            CapabilityCheck(
                feature="teamsPersonal",
                scope="self",
                required=False,
                state="disabled" if not enabled else "unknown" if installed else "needsAction",
                reason=(
                    "Private Teams notifications are not enabled by an administrator."
                    if not enabled
                    else "The personal app is installed. Use an explicit test to check sending; "
                    "installation alone does not prove delivery."
                    if installed
                    else "Install or unblock the MOSAIC Teams app in your personal Teams context."
                ),
                checked_at=now,
                remediation=CapabilityRemediation(
                    action=(
                        "Install the approved MOSAIC Teams app for yourself, then opt in to "
                        "private notifications. Personal events never use shared channels."
                    ),
                    owner="user",
                ),
            )
        )
        for probe in self._personal_probes:
            checks.extend(await probe(actor))
        return checks
