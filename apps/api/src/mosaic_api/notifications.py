"""Notification contracts. Settings express intent, never proof of delivery or authorization."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import ConfigDict, Field, SecretStr, field_validator, model_validator

from mosaic_api.domain import EntitlementResource, EntitlementSubject, Entity, MosaicModel, utc_now
from mosaic_api.scheduling import IanaTimeZone, LocalTime, UtcInstant, Weekday, WeeklyTimeWindow

NOTIFICATION_RETENTION_DAYS = 90
MAX_NOTIFICATION_PAGE_SIZE = 100
NotificationPersona = Literal["admin", "portal"]
NotificationChannel = Literal["inApp", "email", "teamsPersonal", "teamsChannel"]
DigestPeriod = Literal["daily", "weekly", "monthly"]
NotificationSeverity = Literal["information", "warning", "error"]
NotificationDeliveryState = Literal[
    "queued",
    "sending",
    "providerAccepted",
    "confirmedDelivered",
    "suppressed",
    "retrying",
    "permanentlyFailed",
    "unknownOutcome",
]
Identifier = Annotated[str, Field(min_length=1, max_length=256)]
SafeText = Annotated[str, Field(min_length=1, max_length=1000)]


class NotificationCategory(StrEnum):
    REQUEST_LIFECYCLE = "requestLifecycle"
    ACCESS_LIFECYCLE = "accessLifecycle"
    OPERATIONAL_ISSUES = "operationalIssues"
    USAGE_AND_BUDGETS = "usageAndBudgets"
    INTEGRATION_HEALTH = "integrationHealth"


class NotificationEventType(StrEnum):
    REQUEST_SUBMITTED = "request.submitted"
    REQUEST_PENDING_REVIEW = "request.pendingReview"
    REQUEST_APPROVED = "request.approved"
    REQUEST_DENIED = "request.denied"
    REQUEST_WITHDRAWN = "request.withdrawn"
    GRANT_CREATED = "grant.created"
    GRANT_CHANGED = "grant.changed"
    GRANT_REVOKED = "grant.revoked"
    ACCESS_READY = "access.ready"
    ACCESS_APPLY_FAILED = "access.applyFailed"
    PUBLISH_FAILED = "operation.publishFailed"
    APPLY_FAILED = "operation.applyFailed"
    SYNC_FAILED = "operation.syncFailed"
    RECOVERY_REQUIRED = "operation.recoveryRequired"
    READINESS_FINDING = "operation.readinessFinding"
    TELEMETRY_UNAVAILABLE = "operation.telemetryUnavailable"
    TELEMETRY_STALE = "operation.telemetryStale"
    BUDGET_GATE_FAILED = "operation.budgetGateFailed"
    OPERATION_RECOVERED = "operation.recovered"
    QUOTA_APPROACHING = "usage.quotaApproaching"
    QUOTA_REACHED = "usage.quotaReached"
    BUDGET_THRESHOLD = "budget.threshold"
    BUDGET_BLOCKED = "budget.blocked"
    BUDGET_UNBLOCKED = "budget.unblocked"
    USAGE_SUMMARY = "usage.summary"
    INTEGRATION_FAILED = "integration.failed"
    INTEGRATION_RECOVERED = "integration.recovered"
    DELIVERY_FAILED = "notification.deliveryFailed"
    DELIVERY_RECOVERED = "notification.deliveryRecovered"
    WORKER_UNHEALTHY = "notification.workerUnhealthy"


def notification_category(event_type: NotificationEventType | str) -> NotificationCategory:
    event = NotificationEventType(event_type)
    prefix = event.value.split(".", 1)[0]
    return {
        "request": NotificationCategory.REQUEST_LIFECYCLE,
        "grant": NotificationCategory.ACCESS_LIFECYCLE,
        "access": NotificationCategory.ACCESS_LIFECYCLE,
        "operation": NotificationCategory.OPERATIONAL_ISSUES,
        "usage": NotificationCategory.USAGE_AND_BUDGETS,
        "budget": NotificationCategory.USAGE_AND_BUDGETS,
        "integration": NotificationCategory.INTEGRATION_HEALTH,
        "notification": NotificationCategory.INTEGRATION_HEALTH,
    }[prefix]


class NotificationSubject(MosaicModel):
    """References, not arbitrary external links; renderers resolve authorized deep links."""

    resource: EntitlementResource | None = None
    request_id: Identifier | None = None
    entitlement_id: Identifier | None = None
    budget_id: Identifier | None = None
    cost_center_id: Identifier | None = None
    incident_id: Identifier | None = None
    gateway_id: Identifier | None = None
    run_id: Identifier | None = None
    endpoint_id: Identifier | None = None
    publication_id: Identifier | None = None
    publication_target: Literal["model", "mcp", "pool"] | None = None
    endpoint_kind: Literal["model", "mcp"] | None = None
    grant_subject: EntitlementSubject | None = None
    grant_subject_object_id: Identifier | None = None
    integration: str | None = Field(default=None, max_length=100)


class NotificationAudience(MosaicModel):
    """Candidate audience only. Fan-out and send claim must recheck current authorization."""

    user_object_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    include_administrators: bool = False
    budget_id: Identifier | None = None

    @model_validator(mode="after")
    def _nonempty(self) -> Self:
        if not (self.user_object_ids or self.include_administrators or self.budget_id):
            raise ValueError("An event needs a candidate audience")
        return self


class NotificationTemplateData(MosaicModel):
    """Allowlisted safe facts. Never put secrets, justification, or unrelated usage here."""

    resource_name: str | None = Field(default=None, max_length=200)
    summary: str | None = Field(default=None, max_length=1000)
    reason_code: str | None = Field(default=None, max_length=100)
    threshold_percent: float | None = Field(default=None, ge=0, le=1000, allow_inf_nan=False)
    period_label: str | None = Field(default=None, max_length=100)
    calls: int | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    coverage: Literal["complete", "delayed", "incomplete", "unavailable"] | None = None
    unpriced_tokens: int | None = Field(default=None, ge=0)
    requires_plan_apply: bool = False


class NotificationEvent(Entity):
    entity_type: Literal["notificationEvent"] = "notificationEvent"
    created_at: UtcInstant = Field(default_factory=utc_now)
    updated_at: UtcInstant = Field(default_factory=utc_now)
    event_type: NotificationEventType
    event_version: Literal[1] = 1
    category: NotificationCategory
    source_id: Identifier
    source_revision: Identifier
    occurred_at: UtcInstant
    origin: str = Field(min_length=1, max_length=100)
    correlation_id: str | None = Field(default=None, max_length=256)
    severity: NotificationSeverity = "information"
    subject: NotificationSubject
    audience: NotificationAudience
    data: NotificationTemplateData = Field(default_factory=NotificationTemplateData)
    fanout_completed_at: UtcInstant | None = None
    fanout_failed_at: UtcInstant | None = None
    fanout_next_attempt_at: UtcInstant | None = None
    ttl: int = Field(default=NOTIFICATION_RETENTION_DAYS * 86400, ge=86400)

    @model_validator(mode="after")
    def _category_matches_event(self) -> Self:
        if self.category != notification_category(self.event_type):
            raise ValueError("The event type determines its category")
        return self


class ChannelCadence(MosaicModel):
    mode: Literal["off", "immediate", "digest"] = "off"
    digest_periods: list[DigestPeriod] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _explicit_digests(self) -> Self:
        if (self.mode == "digest") != bool(self.digest_periods):
            raise ValueError("Digest mode requires explicit periods; other modes have no periods")
        if len(set(self.digest_periods)) != len(self.digest_periods):
            raise ValueError("Digest periods must be unique")
        return self


class CategoryNotificationPreference(MosaicModel):
    category: NotificationCategory
    in_app: bool = True
    email: ChannelCadence = Field(default_factory=ChannelCadence)
    teams: ChannelCadence = Field(default_factory=ChannelCadence)


class DigestSchedule(MosaicModel):
    period: DigestPeriod
    delivery_time: LocalTime = "09:00"
    day_of_week: Weekday = 0
    day_of_month: int = Field(default=1, ge=1, le=28, strict=True)


def default_category_preferences() -> list[CategoryNotificationPreference]:
    return [CategoryNotificationPreference(category=category) for category in NotificationCategory]


class NotificationPreferencesInput(MosaicModel):
    """No user/tenant/persona input: APIs derive them from the authenticated route."""

    time_zone: IanaTimeZone = "UTC"
    categories: list[CategoryNotificationPreference] = Field(
        default_factory=default_category_preferences, max_length=len(NotificationCategory)
    )
    digest_schedules: list[DigestSchedule] = Field(default_factory=list, max_length=3)
    quiet_hours: WeeklyTimeWindow | None = None
    thresholds: list[Annotated[int, Field(strict=True, ge=1, le=1000)]] = Field(
        default_factory=lambda: [80, 100], min_length=1, max_length=5
    )
    repeat_cooldown_seconds: int = Field(default=86400, ge=300, le=2_678_400)

    @model_validator(mode="after")
    def _unique_and_scheduled(self) -> Self:
        categories = [item.category for item in self.categories]
        periods = [schedule.period for schedule in self.digest_schedules]
        if len(set(categories)) != len(categories) or len(set(periods)) != len(periods):
            raise ValueError("Categories and digest schedules must be unique")
        requested = {
            period
            for item in self.categories
            for channel in (item.email, item.teams)
            for period in channel.digest_periods
        }
        if not requested.issubset(set(periods)):
            raise ValueError("Every opted-in digest period needs a delivery schedule")
        return self


class NotificationPreferences(NotificationPreferencesInput, Entity):
    entity_type: Literal["notificationPreferences"] = "notificationPreferences"
    user_object_id: Identifier
    persona: NotificationPersona
    revision: int = Field(default=1, ge=1)


class NotificationRouteInput(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    audience: Literal["selectedAdmins", "allAdmins", "budgetRecipients", "teamsDestination"]
    administrator_object_ids: list[Identifier] = Field(default_factory=list, max_length=100)
    budget_id: Identifier | None = None
    teams_destination_id: Identifier | None = None
    categories: list[NotificationCategory] = Field(min_length=1, max_length=5)
    event_types: list[NotificationEventType] = Field(default_factory=list, max_length=50)
    channel: Literal["inApp", "email", "teamsChannel"]
    cadence: ChannelCadence = Field(default_factory=ChannelCadence)
    time_zone: IanaTimeZone = "UTC"
    digest_schedules: list[DigestSchedule] = Field(default_factory=list, max_length=3)
    # Migration uses the existing budget record, never new arbitrary forwarding addresses.
    migrated_from_budget_id: Identifier | None = None

    @model_validator(mode="after")
    def _audience_channel(self) -> Self:
        if (self.audience == "selectedAdmins") != bool(self.administrator_object_ids):
            raise ValueError("Only selected-admin routes name administrator object IDs")
        if (self.audience == "budgetRecipients") != bool(self.budget_id):
            raise ValueError("Only budget-recipient routes name a budget")
        if self.audience == "budgetRecipients" and self.channel != "email":
            raise ValueError("Legacy budget-recipient routes use email")
        shared = self.audience == "teamsDestination"
        if shared != bool(self.teams_destination_id) or shared != (self.channel == "teamsChannel"):
            raise ValueError("Shared Teams routes require exactly one Teams destination")
        if any(notification_category(event) not in self.categories for event in self.event_types):
            raise ValueError("Route event filters must belong to its selected categories")
        periods = [schedule.period for schedule in self.digest_schedules]
        if len(set(periods)) != len(periods):
            raise ValueError("Route digest schedules must be unique")
        if not set(self.cadence.digest_periods).issubset(periods):
            raise ValueError("Route digest periods need delivery schedules")
        if self.channel == "inApp" and self.cadence.mode == "digest":
            raise ValueError("In-app notifications are immediate, not digested")
        return self


class NotificationRoute(NotificationRouteInput, Entity):
    entity_type: Literal["notificationRoute"] = "notificationRoute"
    revision: int = Field(default=1, ge=1)
    updated_by: str | None = None


class NotificationInboxItem(Entity):
    entity_type: Literal["notificationInboxItem"] = "notificationInboxItem"
    user_object_id: Identifier
    persona: NotificationPersona
    event_id: Identifier
    category: NotificationCategory
    severity: NotificationSeverity = "information"
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(max_length=1000)
    subject: NotificationSubject
    occurred_at: UtcInstant
    sort_key: str = ""
    read_at: UtcInstant | None = None
    ttl: int = Field(default=NOTIFICATION_RETENTION_DAYS * 86400, ge=86400)

    @model_validator(mode="after")
    def _stable_sort_key(self) -> Self:
        expected = f"{self.occurred_at.isoformat()}|{self.id}"
        if self.sort_key and self.sort_key != expected:
            raise ValueError("The inbox sort key must identify its occurrence and item")
        self.sort_key = expected
        return self


class NotificationInboxPage(MosaicModel):
    items: list[NotificationInboxItem] = Field(default_factory=list)
    continuation_token: str | None = None
    unread_count: int = Field(default=0, ge=0)


class NotificationReadUpdate(MosaicModel):
    read: bool


class NotificationDelivery(Entity):
    entity_type: Literal["notificationDelivery"] = "notificationDelivery"
    event_ids: list[Identifier] = Field(min_length=1, max_length=100)
    channel: NotificationChannel
    recipient_reference: Identifier
    persona: NotificationPersona | None = None
    route_ids: list[Identifier] = Field(default_factory=list)
    preference_revision: int | None = Field(default=None, ge=1)
    state: NotificationDeliveryState = "queued"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: UtcInstant | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: UtcInstant | None = None
    provider_operation_id: str | None = None
    provider_message_id: str | None = None
    provider_accepted_at: UtcInstant | None = None
    confirmed_delivered_at: UtcInstant | None = None
    last_error_code: str | None = Field(default=None, max_length=100)
    last_error: str | None = Field(default=None, max_length=1000)
    suppression_reason: str | None = Field(default=None, max_length=1000)
    digest_window_id: str | None = None
    legacy_budget_notice_id: Identifier | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=10)
    ttl: int = Field(default=NOTIFICATION_RETENTION_DAYS * 86400, ge=86400)

    @model_validator(mode="after")
    def _state_evidence(self) -> Self:
        if self.state == "providerAccepted" and self.provider_accepted_at is None:
            raise ValueError("Provider acceptance requires its observed timestamp")
        if self.state == "confirmedDelivered" and self.confirmed_delivered_at is None:
            raise ValueError("Confirmed delivery requires delivery evidence and a timestamp")
        if self.state == "sending" and not (
            self.lease_owner and self.lease_token and self.lease_expires_at
        ):
            raise ValueError("Sending requires a durable lease owner, token, and expiry")
        if self.state == "retrying" and self.next_attempt_at is None:
            raise ValueError("Retrying requires its durable next-attempt time")
        if self.state == "suppressed" and not self.suppression_reason:
            raise ValueError("Suppressed delivery requires a reason")
        if self.channel in {"inApp", "teamsPersonal"} and self.persona is None:
            raise ValueError("Personal deliveries require a persona")
        if self.channel == "teamsChannel" and self.persona is not None:
            raise ValueError("Shared Teams delivery is not a personal delivery")
        return self


class NotificationDeliveryAttempt(Entity):
    entity_type: Literal["notificationDeliveryAttempt"] = "notificationDeliveryAttempt"
    delivery_id: Identifier
    attempt: int = Field(ge=1)
    started_at: UtcInstant
    finished_at: UtcInstant | None = None
    outcome: Literal[
        "sending",
        "deferred",
        "providerAccepted",
        "confirmedDelivered",
        "retryableFailure",
        "permanentFailure",
        "unknownOutcome",
        "suppressed",
    ]
    provider_operation_id: str | None = None
    retry_after_seconds: int | None = Field(default=None, ge=0)
    safe_error: str | None = Field(default=None, max_length=1000)
    ttl: int = Field(default=NOTIFICATION_RETENTION_DAYS * 86400, ge=86400)


class NotificationOutboxState(Entity):
    entity_type: Literal["notificationOutboxState"] = "notificationOutboxState"
    event_id: Identifier
    state: Literal["queued", "processing", "retrying", "completed", "permanentlyFailed"] = "queued"
    attempts: int = Field(default=0, ge=0)
    next_attempt_at: UtcInstant | None = None
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: UtcInstant | None = None
    last_error: str | None = Field(default=None, max_length=1000)
    ttl: int = Field(default=NOTIFICATION_RETENTION_DAYS * 86400, ge=86400)


class NotificationOutboxClaim(MosaicModel):
    event: NotificationEvent
    state: NotificationOutboxState

    @model_validator(mode="after")
    def _same_event_and_tenant(self) -> Self:
        if self.event.id != self.state.event_id or self.event.tenant_id != self.state.tenant_id:
            raise ValueError("An outbox claim must reference its own tenant's event")
        if self.state.state != "processing" or not (
            self.state.lease_owner and self.state.lease_token and self.state.lease_expires_at
        ):
            raise ValueError("An outbox claim requires a durable processing lease")
        return self


class DigestWindow(MosaicModel):
    period: DigestPeriod
    time_zone: IanaTimeZone = "UTC"
    start_at: UtcInstant
    end_at: UtcInstant
    due_at: UtcInstant
    catch_up: bool = False

    @model_validator(mode="after")
    def _completed_period(self) -> Self:
        if self.end_at <= self.start_at or self.due_at < self.end_at:
            raise ValueError("Digests cover completed nonempty periods")
        return self


class DigestWatermark(Entity):
    entity_type: Literal["digestWatermark"] = "digestWatermark"
    recipient_reference: Identifier
    persona: NotificationPersona | None = None
    channel: Literal["inApp", "email", "teamsPersonal", "teamsChannel"]
    schedule_revision: int = Field(ge=1)
    period: DigestPeriod
    time_zone: IanaTimeZone = "UTC"
    last_completed_end_at: UtcInstant | None = None
    pending_window: DigestWindow | None = None
    next_due_at: UtcInstant | None = None


class AlertEvidence(MosaicModel):
    observed_at: UtcInstant
    through: UtcInstant | None = None
    coverage: Literal["complete", "delayed", "incomplete", "unavailable"]
    priced: bool = False
    scope_kind: Literal["personal", "sharedGrant", "costCenter", "pool", "organization"]
    limit: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    used: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    unpriced_tokens: int = Field(default=0, ge=0)


class NotificationAlertIncident(Entity):
    entity_type: Literal["notificationAlertIncident"] = "notificationAlertIncident"
    scope_id: Identifier
    scope_revision: Identifier
    period_start_at: UtcInstant
    period_end_at: UtcInstant
    threshold_percent: int = Field(ge=1, le=1000)
    state: Literal["open", "recovered", "unknown"] = "unknown"
    first_observed_at: UtcInstant
    last_observed_at: UtcInstant
    last_notified_at: UtcInstant | None = None
    recovered_at: UtcInstant | None = None
    evidence: AlertEvidence
    ttl: int = Field(default=NOTIFICATION_RETENTION_DAYS * 86400, ge=86400)

    @model_validator(mode="after")
    def _ordered_period_and_observations(self) -> Self:
        if self.period_end_at <= self.period_start_at:
            raise ValueError("An alert incident needs a nonempty accounting period")
        if self.last_observed_at < self.first_observed_at:
            raise ValueError("The last observation cannot precede the first")
        return self


CapabilityState = Literal[
    "disabled",
    "notConfigured",
    "needsAction",
    "checking",
    "verified",
    "degraded",
    "failed",
    "unknown",
]


class CapabilityRemediation(MosaicModel):
    action: SafeText
    path: str | None = Field(default=None, max_length=500)
    command: str | None = Field(default=None, max_length=1000)
    owner: Literal["mosaicAdmin", "azureAdmin", "entraAdmin", "teamsAdmin", "user"] = "mosaicAdmin"


class CapabilityCheck(MosaicModel):
    feature: Identifier
    scope: Identifier
    required: bool = True
    state: CapabilityState = "unknown"
    reason: str | None = Field(default=None, max_length=1000)
    evidence: list[SafeText] = Field(default_factory=list, max_length=20)
    checked_at: UtcInstant | None = None
    fresh_until: UtcInstant | None = None
    configuration_revision: str | None = None
    last_test_at: UtcInstant | None = None
    dependencies: list[Identifier] = Field(default_factory=list)
    remediation: CapabilityRemediation | None = None

    @model_validator(mode="after")
    def _verified_evidence(self) -> Self:
        if self.state == "verified" and (self.checked_at is None or not self.evidence):
            raise ValueError("Verified readiness requires dated evidence, not saved settings")
        if (
            self.fresh_until is not None
            and self.checked_at is not None
            and self.fresh_until <= self.checked_at
        ):
            raise ValueError("Readiness evidence must expire after its check")
        return self


class NotificationTestEvidence(MosaicModel):
    outcome: Literal[
        "providerAccepted",
        "confirmedDelivered",
        "retryableFailure",
        "permanentFailure",
        "unknownOutcome",
        "suppressed",
    ]
    message: SafeText
    tested_at: UtcInstant
    configuration_revision: str | None = None
    error_code: SafeText | None = None
    confirmed_delivered: bool = False


class NotificationChannelHealth(Entity):
    entity_type: Literal["notificationChannelHealth"] = "notificationChannelHealth"
    channel: NotificationChannel
    destination_id: str | None = None
    checks: list[CapabilityCheck] = Field(default_factory=list)
    last_provider_accepted_at: UtcInstant | None = None
    last_confirmed_delivered_at: UtcInstant | None = None
    last_failure_at: UtcInstant | None = None
    last_test: NotificationTestEvidence | None = None


class NotificationWorkerState(Entity):
    entity_type: Literal["notificationWorkerState"] = "notificationWorkerState"
    worker: Identifier
    paused: bool = False
    heartbeat_at: UtcInstant | None = None
    last_succeeded_at: UtcInstant | None = None
    backlog: int = Field(default=0, ge=0)
    oldest_queued_at: UtcInstant | None = None
    last_error: str | None = Field(default=None, max_length=1000)
    checkpoint: str | None = None
    hosting_verified: bool = False
    ttl: int | None = Field(default=None, ge=86400, exclude_if=lambda value: value is None)


class NotificationChannelSettingsInput(MosaicModel):
    in_app_enabled: bool = True
    email_enabled: bool = False
    teams_channel_enabled: bool = False
    teams_personal_enabled: bool = False
    retention_days: int = Field(default=NOTIFICATION_RETENTION_DAYS, ge=7, le=365)


class NotificationChannelSettings(NotificationChannelSettingsInput, Entity):
    entity_type: Literal["notificationChannelSettings"] = "notificationChannelSettings"
    revision: int = Field(default=1, ge=1)


class TeamsDestinationInput(MosaicModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    allowed_categories: list[NotificationCategory] = Field(min_length=1, max_length=5)
    # The service must also enforce its deployment-controlled endpoint allowlist and no redirects.
    webhook_url: SecretStr | None = Field(default=None, exclude=True, repr=False)

    @field_validator("webhook_url")
    @classmethod
    def _https_secret(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                parts = urlsplit(value.get_secret_value())
                valid = (
                    parts.scheme == "https"
                    and bool(parts.hostname)
                    and parts.username is None
                    and parts.password is None
                    and not parts.fragment
                    and parts.port in {None, 443}
                )
            except ValueError:
                valid = False
            if not valid:
                raise ValueError("Use a supported HTTPS Teams Workflows destination")
        return value


class TeamsDestination(Entity):
    """Only a Key Vault reference persists; no webhook URL is returned or stored here."""

    entity_type: Literal["teamsDestination"] = "teamsDestination"
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = False
    allowed_categories: list[NotificationCategory] = Field(min_length=1, max_length=5)
    credential_reference_id: Identifier
    revision: int = Field(default=1, ge=1)
    updated_by: str | None = None


class PersonalTeamsSettingsInput(MosaicModel):
    enabled: bool = False
    app_id: Identifier | None = None
    bot_app_id: Identifier | None = None
    cloud: Literal["public", "usGovernment"] = "public"

    @model_validator(mode="after")
    def _enabled_registration(self) -> Self:
        if self.enabled and not (self.app_id and self.bot_app_id):
            raise ValueError("Personal Teams needs an app and bot registration")
        return self


class PersonalTeamsSettings(PersonalTeamsSettingsInput, Entity):
    entity_type: Literal["personalTeamsSettings"] = "personalTeamsSettings"
    revision: int = Field(default=1, ge=1)


class PersonalTeamsBinding(Entity):
    """Internal only: populated exclusively from authenticated installation/lifecycle events."""

    entity_type: Literal["personalTeamsBinding"] = "personalTeamsBinding"
    user_object_id: Identifier
    conversation_type: Literal["personal"] = "personal"
    conversation_id: Identifier = Field(repr=False)
    service_url: str = Field(min_length=1, max_length=1000, repr=False)
    bot_app_id: Identifier
    authenticated_at: UtcInstant
    state: Literal["installed", "uninstalled", "blocked"] = "installed"
    cloud: Literal["public", "usGovernment"] = "public"


class EffectiveNotificationRecipient(MosaicModel):
    recipient_reference: Identifier
    channel: NotificationChannel
    eligible: bool
    route_ids: list[Identifier] = Field(default_factory=list)
    reasons: list[SafeText] = Field(default_factory=list)
    next_delivery_at: UtcInstant | None = None
