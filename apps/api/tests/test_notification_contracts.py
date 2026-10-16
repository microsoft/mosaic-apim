"""Durable notification models and deterministic event builders, entirely offline."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from mosaic_api.domain import AccessRequest
from mosaic_api.notifications import (
    CapabilityCheck,
    ChannelCadence,
    DigestWindow,
    NotificationAudience,
    NotificationCategory,
    NotificationChannelSettingsInput,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationOutboxClaim,
    NotificationPreferences,
    NotificationPreferencesInput,
    NotificationRouteInput,
    NotificationSubject,
    NotificationTemplateData,
    PersonalTeamsBinding,
    PersonalTeamsSettingsInput,
    TeamsDestination,
    TeamsDestinationInput,
    notification_category,
)
from mosaic_api.services.notification_events import (
    access_request_event,
    build_notification_event,
    notification_event_id,
    notification_preferences_id,
    notification_record_id,
)
from pydantic import ValidationError

NOW = datetime(2026, 10, 16, 16, 0, tzinfo=UTC)


def _event(**overrides: object) -> NotificationEvent:
    return NotificationEvent.model_validate(
        {
            "id": "event",
            "tenantId": "tenant",
            "eventType": "request.submitted",
            "category": "requestLifecycle",
            "sourceId": "request",
            "sourceRevision": "pending:original",
            "occurredAt": NOW,
            "origin": "entitlements",
            "subject": {"requestId": "request"},
            "audience": {"userObjectIds": ["requester"], "includeAdministrators": True},
            **overrides,
        }
    )


def test_defaults_enable_only_in_app_and_personas_are_separate() -> None:
    preferences = NotificationPreferencesInput()
    assert preferences.time_zone == "UTC"
    assert preferences.digest_schedules == []
    assert preferences.thresholds == [80, 100]
    assert len(preferences.categories) == len(NotificationCategory)
    assert all(item.in_app for item in preferences.categories)
    assert all(item.email.mode == item.teams.mode == "off" for item in preferences.categories)
    settings = NotificationChannelSettingsInput()
    assert settings.in_app_enabled
    assert not (
        settings.email_enabled or settings.teams_channel_enabled or settings.teams_personal_enabled
    )
    assert notification_preferences_id("tenant", "person", "admin") != notification_preferences_id(
        "tenant", "person", "portal"
    )
    assert notification_preferences_id("tenant", "PERSON", "admin") == notification_preferences_id(
        "tenant", "person", "admin"
    )
    one = NotificationPreferences(
        id="one", tenant_id="tenant", user_object_id="person", persona="admin"
    )
    two = NotificationPreferences(
        id="two", tenant_id="tenant", user_object_id="person", persona="portal"
    )
    one.categories[0].in_app = False
    assert two.categories[0].in_app


@pytest.mark.parametrize("identity_field", ["userObjectId", "tenantId", "persona"])
def test_personal_update_cannot_supply_its_own_identity(identity_field: str) -> None:
    with pytest.raises(ValidationError):
        NotificationPreferencesInput.model_validate({identity_field: "someone-else"})


@pytest.mark.parametrize(
    "cadence",
    [
        {"mode": "digest"},
        {"mode": "immediate", "digestPeriods": ["daily"]},
        {"mode": "off", "digestPeriods": ["monthly"]},
        {"mode": "digest", "digestPeriods": ["daily", "daily"]},
    ],
)
def test_digest_periods_are_explicit_and_not_repeated(cadence: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ChannelCadence.model_validate(cadence)


def test_digest_opt_in_needs_schedule_and_does_not_change_enforcement() -> None:
    preferences = {
        "categories": [
            {
                "category": "usageAndBudgets",
                "email": {"mode": "digest", "digestPeriods": ["weekly"]},
            }
        ],
        "timeZone": "America/New_York",
    }
    with pytest.raises(ValidationError, match="delivery schedule"):
        NotificationPreferencesInput.model_validate(preferences)
    saved = NotificationPreferencesInput.model_validate(
        {**preferences, "digestSchedules": [{"period": "weekly", "deliveryTime": "08:30"}]}
    )
    assert saved.categories[0].email.digest_periods == ["weekly"]
    assert "enforcement" not in saved.model_dump()
    with pytest.raises(ValidationError):
        NotificationPreferencesInput.model_validate({**preferences, "dailyBudget": 100})


def test_notification_event_catalog_has_one_category_for_every_event() -> None:
    for event_type in NotificationEventType:
        assert notification_category(event_type) in NotificationCategory
    with pytest.raises(ValidationError, match="determines its category"):
        _event(category="integrationHealth")
    with pytest.raises(ValidationError):
        _event(eventType="request.automaticallyDenied")


def test_events_require_audience_and_only_allow_safe_template_fields() -> None:
    with pytest.raises(ValidationError, match="candidate audience"):
        NotificationAudience()
    for dangerous in ("secret", "webhookUrl", "justification", "recipients", "externalUrl"):
        with pytest.raises(ValidationError):
            NotificationTemplateData.model_validate({dangerous: "untrusted"})


def test_event_builders_are_deterministic_and_never_embed_request_free_text() -> None:
    request = AccessRequest.model_validate(
        {
            "id": "request",
            "tenantId": "tenant",
            "requesterObjectId": "person",
            "resource": {"kind": "modelApi", "id": "model"},
            "createdAt": NOW,
            "updatedAt": NOW,
            "justification": "SECRET do not send",
            "decisionNote": "internal review notes",
        }
    )
    submitted = access_request_event(request)
    assert submitted.id == access_request_event(request).id
    assert submitted.event_type == "request.submitted"
    assert "SECRET" not in submitted.model_dump_json()
    assert "internal review" not in submitted.model_dump_json()
    approved_request = AccessRequest.model_validate(
        {
            **request.model_dump(by_alias=False),
            "state": "approved",
            "decided_at": NOW,
            "granted_entitlement_id": "grant",
        }
    )
    approved = access_request_event(approved_request)
    assert approved.id != submitted.id
    assert approved.data.requires_plan_apply
    assert approved.event_type != "access.ready"
    assert approved.subject.entitlement_id == "grant"
    assert approved.audience.user_object_ids == ["person"]


def test_event_ids_preserve_opaque_revision_case_and_avoid_delimiter_collisions() -> None:
    assert notification_record_id("record", "a|b", "c") != notification_record_id(
        "record", "a", "b|c"
    )
    assert notification_event_id("tenant", "request.approved", "r", "VersionA") != (
        notification_event_id("tenant", "request.approved", "r", "versiona")
    )
    assert notification_event_id("tenant", "request.approved", "r", "1") != (
        notification_event_id("other-tenant", "request.approved", "r", "1")
    )


def test_event_time_is_aware_and_normalized_to_utc_without_changing_deduplication() -> None:
    fields = {
        "tenant_id": "tenant",
        "event_type": NotificationEventType.SYNC_FAILED,
        "source_id": "sync",
        "source_revision": "operation-1",
        "origin": "gateway-sync",
        "subject": NotificationSubject(integration="sync"),
        "audience": NotificationAudience(include_administrators=True),
    }
    event = build_notification_event(**fields, occurred_at=NOW)
    earlier = build_notification_event(**fields, occurred_at=NOW - timedelta(seconds=10))
    assert event.id == earlier.id
    offset = NOW.astimezone(timezone(timedelta(hours=-4)))
    equivalent = build_notification_event(**fields, occurred_at=offset)
    assert event.model_dump_json() == equivalent.model_dump_json()
    assert _event(occurredAt=offset).occurred_at.tzinfo is UTC
    with pytest.raises(ValidationError):
        _event(occurredAt=NOW.replace(tzinfo=None))


def test_routes_are_opt_in_and_never_redirect_personal_teams_to_shared_channels() -> None:
    route = NotificationRouteInput.model_validate(
        {
            "name": "Ops",
            "audience": "selectedAdmins",
            "administratorObjectIds": ["real-admin"],
            "categories": ["operationalIssues"],
            "channel": "email",
        }
    )
    assert not route.enabled
    assert route.cadence.mode == "off"
    assert route.time_zone == "UTC"
    with pytest.raises(ValidationError):
        NotificationRouteInput.model_validate({**route.model_dump(), "channel": "teamsPersonal"})
    with pytest.raises(ValidationError, match="Shared Teams"):
        NotificationRouteInput.model_validate({**route.model_dump(), "channel": "teamsChannel"})
    with pytest.raises(ValidationError, match="selected-admin"):
        NotificationRouteInput.model_validate({**route.model_dump(), "audience": "allAdmins"})
    legacy = NotificationRouteInput(
        name="Existing finance notices",
        audience="budgetRecipients",
        budget_id="legacy-budget",
        migrated_from_budget_id="legacy-budget",
        categories=[NotificationCategory.USAGE_AND_BUDGETS],
        channel="email",
        cadence=ChannelCadence(mode="immediate"),
    )
    assert legacy.budget_id == legacy.migrated_from_budget_id


def test_delivery_default_does_not_claim_provider_acceptance_or_actual_receipt() -> None:
    delivery = NotificationDelivery(
        id="delivery",
        tenant_id="tenant",
        event_ids=["event"],
        channel="email",
        recipient_reference="person",
        persona="portal",
    )
    assert delivery.state == "queued"
    assert delivery.provider_accepted_at is None
    assert delivery.confirmed_delivered_at is None
    assert delivery.ttl == 90 * 86400


@pytest.mark.parametrize(
    "state",
    ["providerAccepted", "confirmedDelivered", "sending", "retrying", "suppressed"],
)
def test_delivery_states_need_durable_evidence(state: str) -> None:
    with pytest.raises(ValidationError):
        NotificationDelivery.model_validate(
            {
                "id": "delivery",
                "tenantId": "tenant",
                "eventIds": ["event"],
                "channel": "email",
                "recipientReference": "person",
                "persona": "portal",
                "state": state,
            }
        )
    accepted = NotificationDelivery.model_validate(
        {
            "id": "delivery",
            "tenantId": "tenant",
            "eventIds": ["event"],
            "channel": "email",
            "recipientReference": "person",
            "persona": "portal",
            "state": "providerAccepted",
            "providerAcceptedAt": NOW,
        }
    )
    assert accepted.confirmed_delivered_at is None


def test_legacy_explicit_budget_recipient_has_no_fabricated_user_persona() -> None:
    legacy = NotificationDelivery(
        id="legacy",
        tenant_id="tenant",
        event_ids=["threshold"],
        channel="email",
        recipient_reference="legacy-budget-recipient-reference",
    )
    assert legacy.persona is None


def test_outbox_claims_cannot_cross_tenant_or_reference_a_different_event() -> None:
    event = _event()
    for tenant, event_id in (("other-tenant", event.id), (event.tenant_id, "another-event")):
        with pytest.raises(ValidationError, match="own tenant"):
            NotificationOutboxClaim.model_validate(
                {"event": event, "state": {"id": "claim", "tenantId": tenant, "eventId": event_id}}
            )


def test_outbox_claims_need_a_processing_lease() -> None:
    event = _event()
    state = {"id": "claim", "tenantId": "tenant", "eventId": event.id}
    with pytest.raises(ValidationError, match="durable processing lease"):
        NotificationOutboxClaim.model_validate({"event": event, "state": state})
    claim = NotificationOutboxClaim.model_validate(
        {
            "event": event,
            "state": {
                **state,
                "state": "processing",
                "leaseOwner": "worker",
                "leaseToken": "token",
                "leaseExpiresAt": NOW + timedelta(minutes=1),
            },
        }
    )
    assert claim.state.lease_token == "token"


def test_readiness_is_not_verified_from_saved_settings_alone() -> None:
    assert CapabilityCheck(feature="email", scope="tenant").state == "unknown"
    with pytest.raises(ValidationError, match="dated evidence"):
        CapabilityCheck(feature="email", scope="tenant", state="verified")
    verified = CapabilityCheck(
        feature="email",
        scope="tenant",
        state="verified",
        checked_at=NOW,
        evidence=["Provider accepted an explicitly requested test"],
    )
    assert verified.state == "verified"
    assert "confirmedDelivered" not in verified.evidence


def test_digests_only_summarize_completed_nonempty_periods() -> None:
    window = DigestWindow(period="daily", start_at=NOW - timedelta(days=1), end_at=NOW, due_at=NOW)
    assert window.time_zone == "UTC"
    with pytest.raises(ValidationError, match="completed nonempty"):
        DigestWindow(period="weekly", start_at=NOW, end_at=NOW, due_at=NOW)
    with pytest.raises(ValidationError, match="completed nonempty"):
        DigestWindow(
            period="daily",
            start_at=NOW - timedelta(days=1),
            end_at=NOW,
            due_at=NOW - timedelta(minutes=1),
        )


def test_teams_urls_are_write_only_and_persisted_destinations_hold_references_only() -> None:
    secret = "https://example.webhook.office.com/workflows/example?sig=TOPSECRET"
    update = TeamsDestinationInput(
        name="Operations",
        allowed_categories=[NotificationCategory.OPERATIONAL_ISSUES],
        webhook_url=secret,
    )
    assert update.webhook_url is not None
    assert update.webhook_url.get_secret_value() == secret
    assert "TOPSECRET" not in update.model_dump_json()
    assert "TOPSECRET" not in repr(update)
    with pytest.raises(ValidationError) as error:
        TeamsDestinationInput(
            name="Operations",
            allowed_categories=[NotificationCategory.OPERATIONAL_ISSUES],
            webhook_url="http://example.test/TOPSECRET",
        )
    assert "TOPSECRET" not in str(error.value)
    destination = TeamsDestination(
        id="teams",
        tenant_id="tenant",
        name="Operations",
        allowed_categories=[NotificationCategory.OPERATIONAL_ISSUES],
        credential_reference_id="credential",
    )
    assert "webhookUrl" not in destination.model_dump()


def test_private_teams_binding_only_represents_personal_authenticated_context() -> None:
    assert not PersonalTeamsSettingsInput().enabled
    with pytest.raises(ValidationError, match="registration"):
        PersonalTeamsSettingsInput(enabled=True)
    binding = {
        "id": "binding",
        "tenantId": "tenant",
        "userObjectId": "person",
        "conversationId": "private-conversation",
        "serviceUrl": "https://smba.trafficmanager.net/",
        "botAppId": "bot",
        "authenticatedAt": NOW,
    }
    assert PersonalTeamsBinding.model_validate(binding).conversation_type == "personal"
    with pytest.raises(ValidationError):
        PersonalTeamsBinding.model_validate({**binding, "conversationType": "channel"})
    with pytest.raises(ValidationError):
        PersonalTeamsBinding.model_validate({**binding, "conversationType": "groupChat"})
