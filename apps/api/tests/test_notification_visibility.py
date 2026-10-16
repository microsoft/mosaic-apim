from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from mosaic_api.config import Settings
from mosaic_api.domain import (
    DirectoryMemberPage,
    DirectoryObject,
    Entitlement,
    EntitlementResource,
    EntitlementSubject,
    Group,
    GroupMembership,
    Principal,
    utc_now,
)
from mosaic_api.errors import DirectoryDisabledError, DirectoryError, DirectoryForbiddenError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.integrations.graph.notifications import (
    AdministratorDiscovery,
    GraphNotificationDirectory,
    NotificationContact,
    NotificationDirectory,
)
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
    NotificationSubject,
)
from mosaic_api.repositories.base import DirectoryRepository, EntitlementRepository
from mosaic_api.repositories.notifications import NotificationPersistence
from mosaic_api.services.approval_events import GrantNotificationRecipientResolver
from mosaic_api.services.notification_digests import (
    DigestNotificationDispatcher,
    NotificationDigestCoordinator,
)
from mosaic_api.services.notification_events import build_notification_event
from mosaic_api.services.notification_preferences import NotificationPreferenceService
from mosaic_api.services.notification_visibility import (
    NotificationVisibility,
    usage_role_authorizer,
)
from mosaic_api.services.notifications import (
    NotificationDispatchResult,
    NotificationPartialAudienceError,
    NotificationService,
)
from test_notification_repository import Clock
from test_notification_repository import store as store


def setup() -> tuple[NotificationVisibility, AsyncMock, AsyncMock, AsyncMock]:
    entitlements = AsyncMock(spec=EntitlementRepository)
    directory = AsyncMock(spec=DirectoryRepository)
    expand = AsyncMock(return_value=[])
    return (
        NotificationVisibility(
            entitlements,
            directory,
            grant_recipients=expand,
        ),
        entitlements,
        directory,
        expand,
    )


async def test_personal_request_event_requires_current_requester_not_candidate_audience() -> None:
    service, entitlements, _, _ = setup()
    entitlements.get_access_request.return_value = SimpleNamespace(
        tenant_id="tenant",
        requester_object_id="owner",
    )
    event = build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.REQUEST_APPROVED,
        source_id="request",
        source_revision="approved",
        origin="accessRequests",
        occurred_at=utc_now(),
        subject=NotificationSubject(request_id="request"),
        audience=NotificationAudience(user_object_ids=["owner", "not-owner"]),
    )
    assert await service.authorize(event, "owner", "portal")
    assert not await service.authorize(event, "not-owner", "portal")


async def test_removed_grant_notification_uses_fresh_former_holder_expansion() -> None:
    service, entitlements, _, expand = setup()
    expand.return_value = ["former-holder"]
    event = build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.GRANT_REVOKED,
        source_id="grant",
        source_revision="revoked",
        origin="entitlements",
        occurred_at=utc_now(),
        subject=NotificationSubject.model_validate(
            {
                "entitlementId": "grant",
                "grantSubject": {"kind": "user", "id": "principal"},
                "grantSubjectObjectId": "former-holder",
            }
        ),
        audience=NotificationAudience(include_administrators=True),
    )
    assert await service.authorize(event, "former-holder", "portal")
    assert not await service.authorize(event, "other", "portal")
    entitlements.get_entitlement.assert_not_awaited()


async def test_unknown_personal_event_fails_closed_and_admin_role_is_separate() -> None:
    service, _, _, _ = setup()
    event = build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.SYNC_FAILED,
        source_id="run",
        source_revision="failed",
        origin="gatewaySync",
        occurred_at=utc_now(),
        subject=NotificationSubject(gateway_id="gateway"),
        audience=NotificationAudience(user_object_ids=["person"], include_administrators=True),
    )
    assert not await service.authorize(event, "person", "portal")
    assert await service.authorize(event, "person", "admin")


async def test_group_access_requires_current_matching_enabled_grant_and_membership() -> None:
    service, entitlements, _, expand = setup()
    grant = Entitlement(
        id="grant",
        tenant_id="tenant",
        subject=EntitlementSubject(kind="securityGroup", id="group"),
        resource=EntitlementResource(kind="modelApi", id="resource"),
    )
    entitlements.get_entitlement.return_value = grant
    expand.return_value = ["member"]
    event = build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.ACCESS_READY,
        source_id="run",
        source_revision="intent",
        origin="publishing",
        occurred_at=utc_now(),
        subject=NotificationSubject(
            entitlement_id=grant.id,
            resource=grant.resource,
            grant_subject=grant.subject,
            grant_subject_object_id="security-group-object",
        ),
        audience=NotificationAudience(include_administrators=True),
    )
    assert await service.authorize(event, "member", "portal")
    assert not await service.authorize(event, "other", "portal")
    entitlements.get_entitlement.return_value = grant.model_copy(update={"enabled": False})
    assert not await service.authorize(event, "member", "portal")
    entitlements.get_entitlement.return_value = None
    assert not await service.authorize(event, "member", "portal")


@pytest.mark.parametrize(
    "persona,role",
    [
        ("admin", "Admin"),
        ("portal", "User"),
        ("portal", "Admin"),
    ],
)
async def test_usage_local_roles_only_authorize_the_configured_local_identity(
    persona: str,
    role: str,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="local",
        repository_backend="memory",
        tenant_id="tenant",
        local_roles=[role],
    )
    authorize = usage_role_authorizer(settings, None, None)
    event = build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.QUOTA_APPROACHING,
        source_id="source",
        source_revision="revision",
        origin="usageProducer:test",
        occurred_at=utc_now(),
        subject=NotificationSubject(),
        audience=NotificationAudience(include_administrators=True),
    )
    assert await authorize(event, "local-admin", persona)
    assert not await authorize(event, "seeded-person", persona)
    assert not await authorize(
        event.model_copy(update={"tenant_id": "other"}), "local-admin", persona
    )


@pytest.mark.parametrize(
    "complete,age,allowed",
    [
        (True, -60, True),
        (False, -60, False),
        (True, 60, False),
        (True, -600, False),
    ],
)
async def test_usage_entra_role_proof_requires_complete_fresh_selected_assignment(
    complete: bool,
    age: int,
    allowed: bool,
) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="entra",
        repository_backend="memory",
        tenant_id="tenant",
        api_client_id="api",
    )
    now = utc_now()
    directory = AsyncMock(spec=GraphNotificationDirectory)
    directory.selected_administrators.return_value = AdministratorDiscovery(
        administrators=[NotificationContact(object_id="owner", lookup_complete=True)],
        complete=complete,
        checked_at=now + timedelta(seconds=age),
        fresh_until=now + timedelta(seconds=age + 120),
    )
    authorize = usage_role_authorizer(settings, None, directory)
    event = build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.QUOTA_APPROACHING,
        source_id="source",
        source_revision="revision",
        origin="usageProducer:test",
        occurred_at=now,
        subject=NotificationSubject(),
        audience=NotificationAudience(include_administrators=True),
    )
    assert await authorize(event, "owner", "portal") is allowed
    assert not await authorize(event, "owner", "admin")
    directory.selected_administrators.assert_awaited_once_with("tenant", ["owner"])


def group_event(*, users: list[str] | None = None) -> NotificationEvent:
    return build_notification_event(
        tenant_id="tenant",
        event_type=NotificationEventType.ACCESS_READY,
        source_id="run",
        source_revision="intent",
        origin="publishing",
        occurred_at=utc_now(),
        subject=NotificationSubject(
            entitlement_id="grant",
            resource=EntitlementResource(kind="modelApi", id="resource"),
            grant_subject=EntitlementSubject(kind="securityGroup", id="group"),
            grant_subject_object_id="security-group-object",
        ),
        audience=NotificationAudience(user_object_ids=users or [], include_administrators=True),
    )


def group_preferences(
    repository: NotificationPersistence, clock: Clock, members: set[str]
) -> tuple[NotificationPreferenceService, AsyncMock, AsyncMock, AsyncMock]:
    entitlements = AsyncMock(spec=EntitlementRepository)
    entitlements.get_entitlement.return_value = Entitlement(
        id="grant",
        tenant_id="tenant",
        resource=EntitlementResource(kind="modelApi", id="resource"),
        subject=EntitlementSubject(kind="securityGroup", id="group"),
    )
    directory = AsyncMock(spec=DirectoryRepository)
    lookup = AsyncMock(spec=DirectoryLookup)
    lookup.group_members.side_effect = lambda object_id, **_: DirectoryMemberPage(
        group_object_id=object_id,
        members=[DirectoryObject(object_id=member, kind="user") for member in sorted(members)],
    )
    lookup.get_object.side_effect = lambda object_id: DirectoryObject(
        object_id=object_id, kind="user"
    )
    lookup.member_groups.side_effect = lambda object_id, groups: (
        set(groups) if object_id.casefold() in members else set()
    )
    resolver = GrantNotificationRecipientResolver(directory, directory_lookup=lookup)
    visibility = NotificationVisibility(
        entitlements,
        directory,
        grant_recipients=resolver.expand,
        grant_member=resolver.contains,
    )
    contacts = AsyncMock(spec=NotificationDirectory)
    contacts.contact.side_effect = lambda tenant, object_id: NotificationContact(
        object_id=object_id, mailbox=f"{object_id}@example.test", available=True
    )
    contacts.discover_administrators.return_value = AdministratorDiscovery(
        administrators=[NotificationContact(object_id="admin")],
        complete=True,
        checked_at=clock(),
        fresh_until=clock() + timedelta(minutes=2),
    )
    return (
        NotificationPreferenceService(
            repository, directory=contacts, recipient_visibility=visibility, clock=clock
        ),
        entitlements,
        lookup,
        contacts,
    )


def group_delivery(
    source: NotificationEvent, *, object_id: str = "member", channel: str = "inApp"
) -> NotificationDelivery:
    return NotificationDelivery(
        id="group-delivery",
        tenant_id=source.tenant_id,
        event_ids=[source.id],
        recipient_reference=object_id,
        persona="portal",
        channel=channel,
    )


@pytest.mark.parametrize("external_enabled", [False, True])
async def test_group_fanout_expands_once_and_claims_only_check_the_current_recipient(
    store: tuple[NotificationPersistence, Clock], external_enabled: bool
) -> None:
    repository, clock = store
    members = {f"member-{index}" for index in range(150)}
    service, _, lookup, _ = group_preferences(repository, clock, members)
    await service.save_settings(
        "tenant",
        "actor",
        NotificationChannelSettingsInput(
            email_enabled=external_enabled, teams_personal_enabled=external_enabled
        ),
    )
    source = group_event()
    targets = await service.resolve(source)
    assert len(targets) == 3 * len(members) + 1
    assert sum(
        target.channel == "inApp"
        and target.persona == "portal"
        and target.suppression_reason is None
        for target in targets
    ) == len(members)
    lookup.group_members.assert_awaited_once()
    lookup.member_groups.assert_not_awaited()
    with patch.object(service, "get_preferences", wraps=service.get_preferences) as preferences:
        for member in sorted(members):
            assert (
                await service.authorize(group_delivery(source, object_id=member), [source]) is None
            )
        assert preferences.await_count == len(members)
    lookup.group_members.assert_awaited_once()
    assert lookup.get_object.await_count == lookup.member_groups.await_count == len(members)

    members.remove("member-0")
    assert "access" in (
        await service.authorize(group_delivery(source, object_id="MEMBER-0"), [source]) or ""
    )
    assert await service.authorize(group_delivery(source, object_id="MEMBER-1"), [source]) is None
    fresh = await service.resolve(source)
    assert {target.recipient_reference for target in fresh if target.persona == "portal"} == members
    assert lookup.group_members.await_count == 2
    lookup.member_groups.assert_awaited_with("member-1", ["security-group-object"])


@pytest.mark.parametrize(
    "failure", ["disabled", "forbidden", "unavailable", "truncated", "wrongGroup"]
)
async def test_unknown_group_evidence_never_uses_direct_candidates_or_reexpands_per_channel(
    store: tuple[NotificationPersistence, Clock], failure: str
) -> None:
    repository, clock = store
    service, _, lookup, _ = group_preferences(repository, clock, {"member"})
    errors = {
        "disabled": DirectoryDisabledError("Group lookup is disabled."),
        "forbidden": DirectoryForbiddenError("Group lookup is forbidden."),
        "unavailable": DirectoryError("Group lookup is unavailable."),
    }
    lookup.group_members.side_effect = errors.get(failure)
    lookup.group_members.return_value = DirectoryMemberPage(
        group_object_id="wrong-group" if failure == "wrongGroup" else "security-group-object",
        members=[DirectoryObject(object_id="member", kind="user")],
        truncated=failure == "truncated",
    )
    source = group_event(users=["member"]).model_copy(
        update={
            "audience": NotificationAudience(
                user_object_ids=["member"], include_administrators=True
            )
        }
    )
    with pytest.raises(NotificationPartialAudienceError) as partial:
        await service.resolve(source)
    assert [
        (target.recipient_reference, target.persona, target.channel)
        for target in partial.value.targets
        if target.suppression_reason is None
    ] == [("admin", "admin", "inApp")]
    lookup.group_members.assert_awaited_once()
    lookup.get_object.assert_not_awaited()
    lookup.member_groups.assert_not_awaited()
    admin_job = group_delivery(source, object_id="admin").model_copy(update={"persona": "admin"})
    assert await service.authorize(admin_job, [source]) is None
    lookup.group_members.assert_awaited_once()


async def test_group_members_remain_deliverable_during_an_unrelated_admin_outage(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, _, lookup, contacts = group_preferences(repository, clock, {"member"})
    contacts.discover_administrators.side_effect = DirectoryError(
        "Administrator lookup unavailable."
    )
    source = group_event().model_copy(
        update={"audience": NotificationAudience(include_administrators=True)}
    )
    with pytest.raises(NotificationPartialAudienceError) as partial:
        await service.resolve(source)
    assert any(
        target.recipient_reference == "member" and target.suppression_reason is None
        for target in partial.value.targets
    )
    assert await service.authorize(group_delivery(source), [source]) is None
    contacts.discover_administrators.assert_awaited_once()
    lookup.group_members.assert_awaited_once()
    lookup.member_groups.assert_awaited_once_with("member", ["security-group-object"])


async def test_independent_requester_survives_unknown_group_evidence(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, entitlements, lookup, _ = group_preferences(repository, clock, {"member"})
    entitlements.get_access_request.return_value = SimpleNamespace(
        tenant_id="tenant", requester_object_id="requester"
    )
    lookup.group_members.side_effect = DirectoryError("Group lookup unavailable.")
    source = group_event(users=["requester"])
    source = source.model_copy(
        update={"subject": source.subject.model_copy(update={"request_id": "request"})}
    )
    with pytest.raises(NotificationPartialAudienceError) as partial:
        await service.resolve(source)
    assert any(
        target.recipient_reference == "requester" and target.suppression_reason is None
        for target in partial.value.targets
    )
    assert await service.authorize(group_delivery(source, object_id="requester"), [source]) is None
    lookup.group_members.assert_awaited_once()
    lookup.member_groups.assert_not_awaited()


async def test_targeted_requester_must_still_be_addressed_or_a_current_expanded_member(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    members = {"member"}
    service, entitlements, lookup, _ = group_preferences(repository, clock, members)
    entitlements.get_access_request.return_value = SimpleNamespace(
        tenant_id="tenant", requester_object_id="requester"
    )
    source = group_event(users=["someone-else"])
    source = source.model_copy(
        update={"subject": source.subject.model_copy(update={"request_id": "request"})}
    )
    job = group_delivery(source, object_id="requester")
    assert await service.authorize(job, [source]) is not None
    members.add("requester")
    assert await service.authorize(job, [source]) is None
    lookup.group_members.assert_not_awaited()
    assert lookup.member_groups.await_count == 2


@pytest.mark.parametrize(
    "change", ["grantDisabled", "grantMissing", "resourceChanged", "subjectChanged"]
)
async def test_targeted_group_claim_keeps_current_grant_eligibility_checks(
    store: tuple[NotificationPersistence, Clock], change: str
) -> None:
    repository, clock = store
    service, entitlements, lookup, _ = group_preferences(repository, clock, {"member"})
    source = group_event()
    assert await service.authorize(group_delivery(source), [source]) is None
    grant = entitlements.get_entitlement.return_value
    updates = {
        "grantDisabled": {"enabled": False},
        "resourceChanged": {"resource": EntitlementResource(kind="modelApi", id="other")},
        "subjectChanged": {"subject": EntitlementSubject(kind="securityGroup", id="other")},
    }
    entitlements.get_entitlement.return_value = (
        None if change == "grantMissing" else grant.model_copy(update=updates[change])
    )
    assert "access" in (await service.authorize(group_delivery(source), [source]) or "")
    lookup.group_members.assert_not_awaited()
    lookup.member_groups.assert_awaited_once()


async def test_targeted_group_claim_rechecks_preferences_channel_and_mailbox_evidence(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    service, _, lookup, contacts = group_preferences(repository, clock, {"member"})
    source = group_event()
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    await service.save_preferences(
        "tenant",
        "member",
        "portal",
        NotificationPreferencesInput(
            categories=[
                CategoryNotificationPreference(
                    category=NotificationCategory.ACCESS_LIFECYCLE,
                    in_app=False,
                    email=ChannelCadence(mode="immediate"),
                )
            ]
        ),
    )
    assert "off" in (await service.authorize(group_delivery(source), [source]) or "")
    email = group_delivery(source, channel="email")
    assert await service.authorize(email, [source]) is None
    contacts.contact.return_value = NotificationContact(
        object_id="member", lookup_complete=False, reason="Mailbox evidence incomplete."
    )
    contacts.contact.side_effect = None
    with pytest.raises(DirectoryError, match="incomplete"):
        await service.authorize(email, [source])
    await service.save_settings("tenant", "actor", NotificationChannelSettingsInput())
    assert "disabled" in (await service.authorize(email, [source]) or "")
    lookup.group_members.assert_not_awaited()


async def test_group_digest_claims_revalidate_only_the_recipient_without_another_full_expansion(
    store: tuple[NotificationPersistence, Clock],
) -> None:
    repository, clock = store
    members = {f"member-{index}" for index in range(12)}
    service, _, lookup, _ = group_preferences(repository, clock, members)
    await service.save_settings(
        "tenant", "actor", NotificationChannelSettingsInput(email_enabled=True)
    )
    await service.save_preferences(
        "tenant",
        "member-0",
        "portal",
        NotificationPreferencesInput(
            categories=[
                CategoryNotificationPreference(
                    category=NotificationCategory.ACCESS_LIFECYCLE,
                    email=ChannelCadence(mode="digest", digest_periods=["daily"]),
                )
            ],
            digest_schedules=[DigestSchedule(period="daily")],
        ),
    )
    coordinator = NotificationDigestCoordinator(
        repository, service, tenant_id="tenant", clock=clock, hosting_verified=True
    )
    transport = AsyncMock()
    transport.supports_idempotency = True
    transport.send.return_value = NotificationDispatchResult("providerAccepted", "operation")
    engine = NotificationService(
        repository,
        audience_resolver=coordinator,
        delivery_authorizer=service,
        dispatchers={"email": DigestNotificationDispatcher(coordinator, transport)},
        clock=clock,
    )
    source = group_event().model_copy(update={"occurred_at": clock()})
    await engine.publish(source)
    await engine.run_once("tenant")
    lookup.group_members.assert_awaited_once()
    lookup.member_groups.reset_mock()
    clock.now = datetime(2026, 10, 17, 9, tzinfo=UTC)
    await coordinator.reconcile()
    await engine.run_once("tenant")
    transport.send.assert_awaited_once()
    lookup.group_members.assert_awaited_once()
    assert lookup.member_groups.await_count >= 2
    assert all(call.args[0] == "member-0" for call in lookup.member_groups.await_args_list)
    job, _ = transport.send.await_args.args
    synthetic = await repository.get_event("tenant", job.event_ids[0])
    assert synthetic is not None
    members.remove("member-0")
    assert "remain authorized" in (await service.authorize(job, [synthetic]) or "")
    lookup.group_members.assert_awaited_once()


@pytest.mark.parametrize("failure", ["missing", "application", "wrongObject", "membershipUnknown"])
async def test_targeted_security_group_membership_requires_known_human_and_membership(
    store: tuple[NotificationPersistence, Clock], failure: str
) -> None:
    repository, clock = store
    service, _, lookup, _ = group_preferences(repository, clock, {"member"})
    lookup.get_object.side_effect = None
    lookup.get_object.return_value = (
        None
        if failure == "missing"
        else DirectoryObject(
            object_id="other" if failure == "wrongObject" else "member",
            kind="servicePrincipal" if failure == "application" else "user",
        )
    )
    if failure == "membershipUnknown":
        lookup.member_groups.side_effect = DirectoryError("Membership evidence unavailable.")
        source = group_event()
        with pytest.raises(DirectoryError, match="unavailable"):
            await service.authorize(group_delivery(source), [source])
    else:
        source = group_event()
        assert "access" in (await service.authorize(group_delivery(source), [source]) or "")
        lookup.member_groups.assert_not_awaited()
    lookup.group_members.assert_not_awaited()


async def test_mosaic_group_claim_reads_only_the_recipient_membership() -> None:
    directory = AsyncMock(spec=DirectoryRepository)
    source = group_event()
    source = source.model_copy(
        update={
            "subject": source.subject.model_copy(
                update={"grant_subject": EntitlementSubject(kind="group", id="group")}
            )
        }
    )
    directory.get_group.return_value = Group(id="group", tenant_id="tenant", name="Group")
    directory.find_principal_by_object_id.return_value = Principal(
        id="principal", tenant_id="tenant", object_id="member", kind="agentUser"
    )
    directory.get_membership.return_value = GroupMembership(
        id="membership", tenant_id="tenant", group_id="group", principal_id="principal"
    )
    resolver = GrantNotificationRecipientResolver(directory)
    assert await resolver.contains(source, "MEMBER")
    directory.get_membership.assert_awaited_once_with("tenant", "group", "principal")
    directory.list_memberships.assert_not_awaited()
    directory.get_membership.return_value = None
    assert not await resolver.contains(source, "MEMBER")
    directory.get_group.return_value = None
    with pytest.raises(DirectoryError, match="cannot be verified"):
        await resolver.contains(source, "MEMBER")
