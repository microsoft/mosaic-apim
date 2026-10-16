"""Preferences and routing use the worker's real contracts, with no live directory or email."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.errors import ConflictError, DirectoryError, DomainError, domain_error_handler
from mosaic_api.integrations.graph.notifications import AdministratorDiscovery, NotificationContact
from mosaic_api.notifications import (
    CategoryNotificationPreference,
    ChannelCadence,
    DigestSchedule,
    NotificationAudience,
    NotificationCategory,
    NotificationChannelSettingsInput,
    NotificationDelivery,
    NotificationEvent,
    NotificationEventType,
    NotificationPreferencesInput,
    NotificationRouteInput,
    NotificationSubject,
)
from mosaic_api.notifications_preferences_api import (
    notification_preferences_router,
    portal_notification_preferences_router,
)
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.scheduling import WeeklyTimeWindow
from mosaic_api.services.notification_events import notification_preferences_id
from mosaic_api.services.notification_preferences import (
    NotificationPreferenceService,
    next_digest_delivery,
)
from mosaic_api.services.notifications import (
    NotificationDispatchResult,
    NotificationPartialAudienceError,
    NotificationService,
)
from test_notification_engine import Dispatcher
from test_notification_repository import NOW, Clock, event
from test_notification_repository import store as store


class Directory:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.admins = {"admin-one", "admin-two"}
        self.complete = True
        self.mail_available = True
        self.stale = False

    async def discover_administrators(self, _tenant: str) -> AdministratorDiscovery:
        return AdministratorDiscovery(
            administrators=[
                NotificationContact(
                    object_id=identifier,
                    display_name=identifier,
                    mailbox=f"{identifier}@example.test",
                    available=self.mail_available,
                )
                for identifier in sorted(self.admins)
            ],
            complete=self.complete,
            checked_at=self.clock(),
            fresh_until=self.clock() + timedelta(minutes=-1 if self.stale else 2),
            reason=None
            if self.complete
            else "Group discovery is incomplete; grant GroupMember.Read.All.",
        )

    async def contact(self, _tenant: str, object_id: str) -> NotificationContact:
        return NotificationContact(
            object_id=object_id,
            mailbox=f"{object_id}@example.test" if self.mail_available else None,
            available=self.mail_available,
            reason=None if self.mail_available else "A directory mailbox is unavailable.",
        )


class Visibility:
    def __init__(self) -> None:
        self.allowed = True

    async def __call__(self, _event: NotificationEvent, _object_id: str, _persona: str) -> bool:
        return self.allowed


def admin_event() -> NotificationEvent:
    return event().model_copy(
        update={"audience": NotificationAudience(include_administrators=True)}
    )


def preference(*, mode: str = "immediate", in_app: bool = True) -> NotificationPreferencesInput:
    return NotificationPreferencesInput(
        categories=[
            CategoryNotificationPreference(
                category=NotificationCategory.REQUEST_LIFECYCLE,
                in_app=in_app,
                email=ChannelCadence(
                    mode=mode, digest_periods=["daily"] if mode == "digest" else []
                ),
            )
        ],
        digest_schedules=[DigestSchedule(period="daily")] if mode == "digest" else [],
    )


def route(*, selected: bool = False, cadence: str = "immediate") -> NotificationRouteInput:
    return NotificationRouteInput(
        name="Fictional administrators",
        enabled=True,
        audience="selectedAdmins" if selected else "allAdmins",
        administrator_object_ids=["admin-one"] if selected else [],
        categories=[NotificationCategory.REQUEST_LIFECYCLE],
        channel="email",
        cadence=ChannelCadence(
            mode=cadence, digest_periods=["daily"] if cadence == "digest" else []
        ),
        digest_schedules=[DigestSchedule(period="daily")] if cadence == "digest" else [],
    )


def delivery(source: NotificationEvent, *, routes: list[str] | None = None) -> NotificationDelivery:
    return NotificationDelivery(
        id="delivery",
        tenant_id="tenant",
        event_ids=[source.id],
        channel="email",
        recipient_reference="admin-one",
        persona="admin",
        route_ids=routes or [],
    )


async def configured(
    repository: NotificationPersistence, clock: Clock
) -> tuple[NotificationPreferenceService, Directory, Visibility]:
    directory = Directory(clock)
    visibility = Visibility()
    service = NotificationPreferenceService(
        repository, directory=directory, authorize_recipient=visibility, clock=clock
    )
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    for user in directory.admins:
        await service.save_preferences("tenant", user, "admin", preference())
    return service, directory, visibility


async def test_defaults_and_saved_preferences_are_tenant_subject_persona_scoped(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service = NotificationPreferenceService(repository, clock=clock)
    defaults = await service.get_preferences("tenant", "person", "portal")
    assert defaults.id == notification_preferences_id("tenant", "person", "portal")
    assert all(
        item.in_app and item.email.mode == item.teams.mode == "off" for item in defaults.categories
    )
    await service.save_preferences("tenant", "person", "admin", preference(in_app=False))
    portal = await service.get_preferences("tenant", "person", "portal")
    admin = await service.get_preferences("tenant", "person", "admin")
    other = await service.get_preferences("other-tenant", "person", "admin")
    assert all(item.in_app for item in portal.categories)
    assert not admin.categories[0].in_app
    assert all(item.in_app for item in other.categories)
    with pytest.raises(ConflictError):
        await service.save_preferences(
            "tenant", "person", "admin", preference(), expected_revision=99
        )


async def test_selected_and_all_admin_routes_use_real_roster_and_preview_suppression(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, directory, _ = await configured(repository, clock)
    all_route = await service.save_route("tenant", "actor", route())
    selected_route = await service.save_route("tenant", "actor", route(selected=True))
    preview = await service.preview_route("tenant", all_route.id, admin_event())
    assert {
        item.recipient_reference for item in preview.recipients if item.eligible
    } == directory.admins
    preview = await service.preview_route("tenant", selected_route.id, admin_event())
    assert [item.recipient_reference for item in preview.recipients] == ["admin-one"]
    await service.save_preferences("tenant", "admin-one", "admin", preference(mode="off"))
    preview = await service.preview_route("tenant", selected_route.id, admin_event())
    assert not preview.recipients[0].eligible
    assert "off" in " ".join(preview.recipients[0].reasons)
    directory.admins.remove("admin-one")
    with pytest.raises(DomainError, match="administrator app role"):
        await service.save_route("tenant", "actor", route(selected=True))
    preview = await service.preview_route("tenant", selected_route.id, admin_event())
    assert not preview.recipients[0].eligible


@pytest.mark.parametrize("failure", ["incomplete", "stale", "missing"])
async def test_incomplete_discovery_cannot_fallback_to_saved_or_signed_in_admins(
    store: tuple[NotificationPersistence, Clock], failure: str
) -> None:
    repository, clock = store
    service, directory, _ = await configured(repository, clock)
    saved = await service.save_route("tenant", "actor", route())
    directory.complete = failure != "incomplete"
    directory.stale = failure == "stale"
    if failure == "missing":
        service = NotificationPreferenceService(repository, clock=clock)
    with pytest.raises(DirectoryError):
        await service.resolve(admin_event())
    preview = await service.preview_route("tenant", saved.id, admin_event())
    assert preview.unavailable and not preview.recipients
    [check] = await service.readiness_checks("tenant")
    assert check.state == "needsAction" and check.remediation


@pytest.mark.parametrize("failure", ["missing", "incomplete", "stale", "exception"])
async def test_requester_inapp_survives_admin_outage_and_pending_admins_recover_without_duplicates(
    store: tuple[NotificationPersistence, Clock],
    failure: str,
) -> None:
    repository, clock = store
    directory = Directory(clock)
    visibility = Visibility()

    class UnavailableDirectory(Directory):
        async def discover_administrators(self, _tenant: str) -> AdministratorDiscovery:
            raise DirectoryError("Administrator discovery is temporarily unavailable.")

    broken = UnavailableDirectory(clock) if failure == "exception" else directory
    broken.complete = failure != "incomplete"
    broken.stale = failure == "stale"
    service = NotificationPreferenceService(
        repository,
        directory=None if failure == "missing" else broken,
        authorize_recipient=visibility,
        clock=clock,
    )
    original = event().model_copy(
        update={
            "audience": NotificationAudience(user_object_ids=["user"], include_administrators=True),
        }
    )
    with pytest.raises(NotificationPartialAudienceError) as partial:
        await service.resolve(original)
    assert {
        (target.recipient_reference, target.persona)
        for target in partial.value.targets
        if target.channel == "inApp" and target.suppression_reason is None
    } == {("user", "portal")}
    engine = NotificationService(
        repository,
        audience_resolver=service,
        delivery_authorizer=service,
        clock=clock,
    )
    await engine.publish(original)
    await engine.run_once("tenant")
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error
    [pending] = await repository.list_outbox_states("tenant", failed_only=False)
    assert pending.state == "retrying" and pending.next_attempt_at and pending.last_error
    assert (await repository.get_event("tenant", original.id)).fanout_completed_at is None
    assert not any(
        target.persona == "admin" for target in await repository.list_deliveries("tenant")
    )
    recovered = NotificationPreferenceService(
        repository,
        directory=Directory(clock),
        authorize_recipient=visibility,
        clock=clock,
    )
    engine = NotificationService(
        repository,
        audience_resolver=recovered,
        delivery_authorizer=recovered,
        clock=clock,
    )
    clock.now = pending.next_attempt_at
    await engine.run_once("tenant")
    assert [
        item.state for item in await repository.list_outbox_states("tenant", failed_only=False)
    ] == ["completed"]
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    for object_id in directory.admins:
        assert (await engine.list_inbox("tenant", object_id, "admin")).unread_count == 1
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error is None


@pytest.mark.parametrize("failure", ["exception", "incomplete"])
async def test_mailbox_outage_preserves_inapp_and_email_recovers_without_false_optout(
    store: tuple[NotificationPersistence, Clock],
    failure: str,
) -> None:
    repository, clock = store

    class ContactDirectory(Directory):
        failed = True

        async def contact(self, tenant: str, object_id: str) -> NotificationContact:
            if not self.failed:
                return await super().contact(tenant, object_id)
            if failure == "exception":
                raise DirectoryError("Directory contacts are unavailable.")
            return NotificationContact(
                object_id=object_id,
                available=False,
                lookup_complete=False,
                reason="Directory contacts are incomplete.",
            )

    directory = ContactDirectory(clock)
    service = NotificationPreferenceService(
        repository,
        directory=directory,
        authorize_recipient=Visibility(),
        clock=clock,
    )
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    await service.save_preferences("tenant", "user", "portal", preference())
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted", "operation"))
    engine = NotificationService(
        repository,
        audience_resolver=service,
        delivery_authorizer=service,
        dispatchers={"email": dispatcher},
        clock=clock,
    )
    original = event()
    with pytest.raises(NotificationPartialAudienceError) as partial:
        await service.resolve(original)
    assert any(
        target.channel == "inApp" and target.suppression_reason is None
        for target in partial.value.targets
    )
    await engine.publish(original)
    await engine.run_once("tenant")
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error
    assert not any(
        target.channel == "email" for target in await repository.list_deliveries("tenant")
    )
    [pending] = await repository.list_outbox_states("tenant", failed_only=False)
    assert pending.state == "retrying" and pending.next_attempt_at and pending.last_error
    assert (await repository.get_event("tenant", original.id)).fanout_completed_at is None
    directory.failed = False
    clock.now = pending.next_attempt_at
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1
    assert (await engine.list_inbox("tenant", "user", "portal")).unread_count == 1
    assert [
        item.state for item in await repository.list_outbox_states("tenant", failed_only=False)
    ] == ["completed"]
    worker = await repository.get_worker_state("tenant", "notifications")
    assert worker and worker.last_error is None


@pytest.mark.parametrize(
    "event_type", [NotificationEventType.ACCESS_READY, NotificationEventType.ACCESS_APPLY_FAILED]
)
async def test_access_status_group_provenance_expands_fresh_at_fanout_and_authorization(
    store: tuple[NotificationPersistence, Clock],
    event_type: NotificationEventType,
) -> None:
    repository, clock = store
    members = ["actual-member"]
    seen: list[NotificationEvent] = []

    async def expand(source: NotificationEvent) -> Sequence[str]:
        seen.append(source)
        assert source.subject.grant_subject_object_id == "actual-group"
        return members

    service = NotificationPreferenceService(
        repository,
        directory=Directory(clock),
        authorize_recipient=Visibility(),
        expand_recipients=expand,
        clock=clock,
    )
    original = admin_event().model_copy(
        update={
            "event_type": event_type,
            "category": NotificationCategory.ACCESS_LIFECYCLE,
            "subject": NotificationSubject(
                entitlement_id="grant",
                grant_subject={"kind": "securityGroup", "id": "group"},
                grant_subject_object_id="actual-group",
            ),
        }
    )
    targets = await service.resolve(original)
    assert any(
        target.recipient_reference == "actual-member"
        and target.channel == "inApp"
        and target.persona == "portal"
        and target.suppression_reason is None
        for target in targets
    )
    assert not any(target.recipient_reference in {"group", "actual-group"} for target in targets)
    job = NotificationDelivery(
        id="access-status",
        tenant_id="tenant",
        event_ids=[original.id],
        recipient_reference="actual-member",
        persona="portal",
        channel="inApp",
    )
    assert await service.authorize(job, [original]) is None
    members.clear()
    assert "changed" in (await service.authorize(job, [original]) or "")
    assert len(seen) == 3


@pytest.mark.parametrize("failure", ["unavailable", "malformed", "oversized"])
async def test_optional_group_expansion_failure_preserves_known_personal_inapp(
    store: tuple[NotificationPersistence, Clock],
    failure: str,
) -> None:
    repository, clock = store

    async def expand(_event: NotificationEvent) -> Sequence[str]:
        if failure == "unavailable":
            raise DirectoryError("Group membership discovery is unavailable.")
        return [""] if failure == "malformed" else [f"member-{number}" for number in range(10001)]

    service = NotificationPreferenceService(
        repository,
        authorize_recipient=Visibility(),
        expand_recipients=expand,
        clock=clock,
    )
    with pytest.raises(NotificationPartialAudienceError) as partial:
        await service.resolve(event())
    assert any(
        target.recipient_reference == "user"
        and target.channel == "inApp"
        and target.suppression_reason is None
        for target in partial.value.targets
    )
    assert {target.recipient_reference for target in partial.value.targets} == {"user"}


async def test_selected_route_can_explicitly_use_verified_subset_without_all_admin_fallback(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store

    class SelectedDirectory(Directory):
        async def selected_administrators(
            self, _tenant: str, identifiers: Sequence[str]
        ) -> AdministratorDiscovery:
            return AdministratorDiscovery(
                administrators=[
                    NotificationContact(
                        object_id=identifier, available=True, mailbox=f"{identifier}@example.test"
                    )
                    for identifier in identifiers
                    if identifier in self.admins
                ],
                complete=True,
                checked_at=clock(),
                fresh_until=clock() + timedelta(minutes=2),
            )

    directory = SelectedDirectory(clock)
    directory.complete = False
    service = NotificationPreferenceService(
        repository, directory=directory, authorize_recipient=Visibility(), clock=clock
    )
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    await service.save_preferences("tenant", "admin-one", "admin", preference())
    await service.save_route("tenant", "actor", route(selected=True))
    targets = await service.resolve(admin_event())
    assert {target.recipient_reference for target in targets} == {"admin-one"}
    with pytest.raises(DirectoryError):
        await service.save_route("tenant", "actor", route())


async def test_recipient_contact_unavailable_is_visible_not_an_invented_mailbox(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, directory, _ = await configured(repository, clock)
    saved = await service.save_route("tenant", "actor", route(selected=True))
    directory.mail_available = False
    preview = await service.preview_route("tenant", saved.id, admin_event())
    assert not preview.recipients[0].eligible
    assert "mailbox" in " ".join(preview.recipients[0].reasons)
    assert await service.mailbox_resolver(delivery(admin_event(), routes=[saved.id])) is None
    [check] = await service.readiness_checks("tenant", "admin-one")
    assert check.state == "needsAction"


async def test_route_filters_and_user_only_events_never_leak_into_shared_admin_routes(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, _, _ = await configured(repository, clock)
    saved = await service.save_route("tenant", "actor", route())
    assert (await service.preview_route("tenant", saved.id, event())).recipients == []
    targets = await service.resolve(event())
    assert all(target.persona == "portal" for target in targets)
    with pytest.raises(DomainError, match="authenticated tenant"):
        await service.preview_route("another-tenant", saved.id, admin_event())


async def test_visibility_preferences_admin_removal_and_routes_rechecked_before_sending(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, directory, visibility = await configured(repository, clock)
    saved = await service.save_route("tenant", "actor", route(selected=True))
    source = admin_event()
    job = delivery(source, routes=[saved.id])
    assert await service.authorize(job, [source]) is None
    visibility.allowed = False
    assert "access" in (await service.authorize(job, [source]) or "")
    visibility.allowed = True
    directory.admins.clear()
    assert "administrator app role" in (await service.authorize(job, [source]) or "")
    directory.admins.add("admin-one")
    await service.save_preferences("tenant", "admin-one", "admin", preference(mode="off"))
    assert "off" in (await service.authorize(job, [source]) or "")
    await service.save_preferences("tenant", "admin-one", "admin", preference())
    await service.delete_route("tenant", "actor", saved.id)
    assert "changed" in (await service.authorize(job, [source]) or "")


async def test_portal_authorization_is_not_inferred_from_candidate_audience(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service = NotificationPreferenceService(repository, clock=clock)
    with pytest.raises(DirectoryError, match="authorization"):
        await service.resolve(event())


async def test_group_candidate_expansion_is_fresh_at_fanout_and_send_and_never_authorization(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    directory = Directory(clock)
    directory.admins.clear()
    members = ["member-one"]
    visibility = Visibility()

    async def expand(_event: NotificationEvent) -> Sequence[str]:
        return members

    service = NotificationPreferenceService(
        repository,
        directory=directory,
        expand_recipients=expand,
        authorize_recipient=visibility,
        clock=clock,
    )
    source = admin_event()
    targets = await service.resolve(source)
    assert next(target for target in targets if target.channel == "inApp").recipient_reference == (
        "member-one"
    )
    job = NotificationDelivery(
        id="group-member-job",
        tenant_id="tenant",
        event_ids=[source.id],
        channel="inApp",
        recipient_reference="member-one",
        persona="portal",
    )
    assert await service.authorize(job, [source]) is None
    visibility.allowed = False
    assert "access" in (await service.authorize(job, [source]) or "")
    visibility.allowed = True
    members.clear()
    assert "changed" in (await service.authorize(job, [source]) or "")

    async def incomplete(_event: NotificationEvent) -> Sequence[str]:
        raise DirectoryError("Group membership discovery is truncated.")

    service = NotificationPreferenceService(
        repository,
        directory=directory,
        expand_recipients=incomplete,
        authorize_recipient=visibility,
        clock=clock,
    )
    with pytest.raises(DirectoryError, match="truncated"):
        await service.resolve(source)


async def test_digest_opt_in_never_becomes_immediate_and_preview_explains_next_delivery(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, _, _ = await configured(repository, clock)
    saved = await service.save_route("tenant", "actor", route(selected=True))
    await service.save_preferences("tenant", "admin-one", "admin", preference(mode="digest"))
    assert not [
        target
        for target in await service.resolve(admin_event())
        if target.recipient_reference == "admin-one" and target.channel == "email"
    ]
    [candidate] = await service.digest_candidates(admin_event())
    assert candidate.cadence.digest_periods == ["daily"]
    assert candidate.next_delivery_at == datetime(2026, 10, 17, 9, tzinfo=UTC)
    preview = await service.preview_route("tenant", saved.id, admin_event())
    assert preview.recipients[0].eligible and preview.recipients[0].next_delivery_at
    assert "changed" in (
        await service.authorize(delivery(admin_event(), routes=[saved.id]), [admin_event()]) or ""
    )


async def test_previews_describe_real_route_availability_and_send_rechecks_tenant_channel(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, _, _ = await configured(repository, clock)
    saved = await service.save_route("tenant", "actor", route(selected=True))
    preview = await service.preview_preferences("tenant", "admin-one", "admin")
    email = next(channel for channel in preview.channels if channel.channel == "email")
    assert email.available
    assert not email.reasons
    await service.save_settings("tenant", "actor", NotificationChannelSettingsInput())
    reason = await service.authorize(delivery(admin_event(), routes=[saved.id]), [admin_event()])
    assert "disabled" in (reason or "")


async def test_engine_deduplicates_same_person_from_multiple_routes_and_replay(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    preferences, _, _ = await configured(repository, clock)
    first = await preferences.save_route("tenant", "actor", route(selected=True))
    second = await preferences.save_route("tenant", "actor", route(selected=True))
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted", "provider-operation"))
    engine = NotificationService(
        repository,
        audience_resolver=preferences,
        delivery_authorizer=preferences,
        dispatchers={"email": dispatcher},
        clock=clock,
    )
    await engine.publish(admin_event())
    await engine.run_once("tenant")
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1
    assert set(dispatcher.sent[0].route_ids) == {first.id, second.id}
    assert dispatcher.sent[0].recipient_reference == "admin-one"


async def test_shared_admin_and_private_user_routes_send_a_person_one_email_not_two(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    preferences, _, _ = await configured(repository, clock)
    await preferences.save_route("tenant", "actor", route(selected=True))
    await preferences.save_preferences("tenant", "admin-one", "portal", preference())
    source = admin_event().model_copy(
        update={
            "audience": NotificationAudience(
                user_object_ids=["admin-one"], include_administrators=True
            )
        }
    )
    targets = await preferences.resolve(source)
    assert (
        len(
            [
                target
                for target in targets
                if target.channel == "email"
                and target.recipient_reference == "admin-one"
                and target.suppression_reason is None
            ]
        )
        == 1
    )
    assert {
        target.persona
        for target in targets
        if target.channel == "inApp" and target.recipient_reference == "admin-one"
    } == {"admin", "portal"}


async def test_usage_producer_preserves_exact_persona_and_summary_period_without_shared_fanout(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    preferences, _, _ = await configured(repository, clock)
    payload = NotificationPreferencesInput(
        categories=[
            CategoryNotificationPreference(
                category=NotificationCategory.USAGE_AND_BUDGETS,
                email=ChannelCadence(mode="digest", digest_periods=["daily"]),
            )
        ],
        digest_schedules=[DigestSchedule(period="daily")],
    )
    await preferences.save_preferences("tenant", "admin-one", "admin", payload)
    source = NotificationEvent(
        id="summary",
        tenant_id="tenant",
        source_id="summary-source",
        source_revision="1",
        occurred_at=NOW,
        origin="usageProducer:admin",
        event_type=NotificationEventType.USAGE_SUMMARY,
        category=NotificationCategory.USAGE_AND_BUDGETS,
        audience=NotificationAudience(user_object_ids=["admin-one"]),
        subject=NotificationSubject(),
        data={"reasonCode": "digest:daily"},
    )
    targets = await preferences.resolve(source)
    assert {target.recipient_reference for target in targets} == {"admin-one"}
    assert {target.persona for target in targets} == {"admin"}
    assert (
        next(target for target in targets if target.channel == "email").suppression_reason is None
    )
    assert await preferences.digest_candidates(source) == []
    source = source.model_copy(
        update={"data": source.data.model_copy(update={"reason_code": "digest:weekly"})}
    )
    assert all(target.suppression_reason for target in await preferences.resolve(source))


async def test_quiet_hours_are_durable_due_times_and_do_not_exhaust_delivery_attempts(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    preferences, _, _ = await configured(repository, clock)
    await preferences.save_route("tenant", "actor", route(selected=True))
    quiet = preference().model_copy(
        update={
            "quiet_hours": WeeklyTimeWindow(days_of_week=[4], start_time="15:00", end_time="18:00")
        }
    )
    await preferences.save_preferences("tenant", "admin-one", "admin", quiet)
    dispatcher = Dispatcher(NotificationDispatchResult("providerAccepted", "provider-operation"))
    engine = NotificationService(
        repository,
        audience_resolver=preferences,
        delivery_authorizer=preferences,
        dispatchers={"email": dispatcher},
        clock=clock,
    )
    await engine.publish(admin_event())
    await engine.run_once("tenant")
    assert dispatcher.sent == []
    jobs = [job for job in await repository.list_deliveries("tenant") if job.channel == "email"]
    assert jobs[0].next_attempt_at == datetime(2026, 10, 16, 18, tzinfo=UTC)
    assert jobs[0].attempts == 0
    clock.now = datetime(2026, 10, 16, 18, tzinfo=UTC)
    await engine.run_once("tenant")
    assert len(dispatcher.sent) == 1


async def test_budget_explicit_external_addresses_preserved_but_ownership_not_duplicated(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store

    async def budget_recipients(_tenant: str, identifier: str) -> Sequence[str]:
        return ["owner@example.test", "OWNER@example.test"] if identifier == "budget-id" else []

    service = NotificationPreferenceService(
        repository, budget_recipients=budget_recipients, clock=clock
    )
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    saved = await service.save_route(
        "tenant",
        "actor",
        NotificationRouteInput(
            name="Preserved explicit owners",
            enabled=True,
            audience="budgetRecipients",
            budget_id="budget-id",
            categories=[NotificationCategory.USAGE_AND_BUDGETS],
            channel="email",
            cadence=ChannelCadence(mode="immediate"),
        ),
    )
    source = NotificationEvent(
        id="budget-event",
        tenant_id="tenant",
        source_id="budget-id",
        source_revision="1",
        occurred_at=NOW,
        origin="budget",
        event_type=NotificationEventType.BUDGET_THRESHOLD,
        category=NotificationCategory.USAGE_AND_BUDGETS,
        audience=NotificationAudience(budget_id="budget-id"),
        subject=NotificationSubject(budget_id="budget-id"),
    )
    targets = await service.resolve(source)
    assert targets and all("ownership" in (target.suppression_reason or "") for target in targets)
    target = targets[0]
    assert "@" not in target.recipient_reference
    job = NotificationDelivery(
        id="budget-job",
        tenant_id="tenant",
        event_ids=[source.id],
        channel="email",
        recipient_reference=target.recipient_reference,
        route_ids=[saved.id],
    )
    assert await service.mailbox_resolver(job) == "owner@example.test"


async def test_budget_bridge_suppresses_all_generic_email_even_when_routes_are_opted_in(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    directory = Directory(clock)

    async def budget_recipients(_tenant: str, _identifier: str) -> Sequence[str]:
        return ["budget-owner@example.test"]

    service = NotificationPreferenceService(
        repository,
        directory=directory,
        authorize_recipient=Visibility(),
        budget_recipients=budget_recipients,
        budget_delivery_migrated=True,
        clock=clock,
    )
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    opted_in = NotificationPreferencesInput(
        categories=[
            CategoryNotificationPreference(
                category=NotificationCategory.USAGE_AND_BUDGETS,
                email=ChannelCadence(mode="immediate"),
            )
        ]
    )
    for identifier in directory.admins:
        await service.save_preferences("tenant", identifier, "admin", opted_in)
    await service.save_preferences("tenant", "user", "portal", opted_in)
    admin_route = await service.save_route(
        "tenant",
        "actor",
        NotificationRouteInput(
            name="All budget administrators",
            enabled=True,
            audience="allAdmins",
            categories=[NotificationCategory.USAGE_AND_BUDGETS],
            channel="email",
            cadence=ChannelCadence(mode="immediate"),
        ),
    )
    await service.save_route(
        "tenant",
        "actor",
        NotificationRouteInput(
            name="Explicit budget recipients",
            enabled=True,
            audience="budgetRecipients",
            budget_id="budget-id",
            categories=[NotificationCategory.USAGE_AND_BUDGETS],
            channel="email",
            cadence=ChannelCadence(mode="immediate"),
        ),
    )
    source = NotificationEvent(
        id="budget-bridge-event",
        tenant_id="tenant",
        source_id="budget-id",
        source_revision="1",
        occurred_at=NOW,
        origin="budgetNotificationBridge",
        event_type=NotificationEventType.BUDGET_THRESHOLD,
        category=NotificationCategory.USAGE_AND_BUDGETS,
        audience=NotificationAudience(
            budget_id="budget-id", user_object_ids=["user"], include_administrators=True
        ),
        subject=NotificationSubject(budget_id="budget-id"),
    )
    targets = await service.resolve(source)
    emails = [target for target in targets if target.channel == "email"]
    assert emails
    assert {target.persona for target in emails} == {"admin", "portal", None}
    assert all("bridge owns email" in (target.suppression_reason or "") for target in emails)
    assert any(
        target.channel == "inApp" and target.suppression_reason is None for target in targets
    )
    directory.complete = False
    job = delivery(source, routes=[admin_route.id])
    assert "bridge owns email" in (await service.authorize(job, [source]) or "")


def test_digest_schedule_resolves_dst_gap_and_fall_back_without_duplicate_time() -> None:
    schedule = DigestSchedule(period="daily", delivery_time="02:30")
    assert next_digest_delivery(
        datetime(2026, 3, 8, 5, tzinfo=UTC), "America/New_York", [schedule], ["daily"]
    ) == datetime(2026, 3, 8, 7, tzinfo=UTC)
    schedule = DigestSchedule(period="daily", delivery_time="01:30")
    assert next_digest_delivery(
        datetime(2026, 11, 1, 5, 45, tzinfo=UTC), "America/New_York", [schedule], ["daily"]
    ) == datetime(2026, 11, 2, 6, 30, tzinfo=UTC)


class Authenticator:
    async def authenticate(self, request: Request) -> AuthContext:
        return AuthContext(
            object_id=request.headers.get("x-object", "self"),
            tenant_id="tenant",
            roles=frozenset({request.headers.get("x-role", "Admin")}),
        )


def api(service: NotificationPreferenceService) -> FastAPI:
    app = FastAPI()
    app.state.notification_preference_service = service
    app.state.settings = SimpleNamespace(required_role="Admin", portal_role="User")
    app.state.authenticator = Authenticator()
    app.add_exception_handler(DomainError, domain_error_handler)
    app.include_router(notification_preferences_router)
    app.include_router(portal_notification_preferences_router)
    return app


def test_api_personal_routes_infer_subject_and_separate_personas_and_forbid_spoofing() -> None:
    from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository

    service = NotificationPreferenceService(InMemoryNotificationRepository())
    with TestClient(api(service)) as client:
        admin_path = "/api/v1/notifications/me/preferences"
        portal_path = "/api/v1/me/notifications/preferences"
        initial = client.get(admin_path)
        assert initial.status_code == 200
        assert initial.json()["userObjectId"] == "self"
        assert initial.json()["persona"] == "admin"
        payload = preference(in_app=False).model_dump(mode="json")
        saved = client.put(admin_path, json=payload, headers={"If-Match": '"1"'})
        assert saved.status_code == 200
        assert client.get(portal_path).json()["categories"][0]["inApp"]
        for extra in ("userObjectId", "tenantId", "persona", "id"):
            assert client.put(portal_path, json={**payload, extra: "other"}).status_code == 422
        assert client.get(admin_path, headers={"x-role": "User"}).status_code == 403
        assert client.get(portal_path, headers={"x-role": "User"}).status_code == 200
        assert (
            client.get("/api/v1/settings/notifications", headers={"x-role": "User"}).status_code
            == 403
        )
        assert client.put(admin_path, json=payload, headers={"If-Match": '"99"'}).status_code == 409
        assert (
            client.put(admin_path, json=payload, headers={"If-Match": "invalid"}).status_code == 422
        )
        assert (
            client.get(portal_path, headers={"x-object": "other"}).json()["userObjectId"] == "other"
        )
        assert (
            client.get("/api/v1/notification-routes/administrator-choices").json()["complete"]
            is False
        )


def test_api_route_crud_and_previews_are_admin_only_and_tenant_scoped() -> None:
    from mosaic_api.repositories.memory_notifications import InMemoryNotificationRepository

    service = NotificationPreferenceService(InMemoryNotificationRepository())
    disabled = route(selected=True).model_copy(update={"enabled": False}).model_dump(mode="json")
    with TestClient(api(service)) as client:
        path = "/api/v1/notification-routes"
        created = client.post(path, json=disabled)
        assert created.status_code == 201
        identifier = created.json()["id"]
        assert client.get(f"{path}/{identifier}").status_code == 200
        assert len(client.get(path).json()) == 1
        assert client.get(f"{path}/{identifier}/preview").json()["unavailable"]
        assert client.post(path, json=disabled, headers={"x-role": "User"}).status_code == 403
        assert client.put(f"{path}/{identifier}", json=disabled).json()["revision"] == 2
        assert client.delete(f"{path}/{identifier}", headers={"If-Match": '"1"'}).status_code == 409
        assert client.delete(f"{path}/{identifier}", headers={"If-Match": '"2"'}).status_code == 204
        assert client.get(f"{path}/{identifier}").status_code == 404
