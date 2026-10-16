from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mosaic_api.config import Settings
from mosaic_api.domain import Entitlement, EntitlementResource, EntitlementSubject, utc_now
from mosaic_api.integrations.graph.notifications import (
    AdministratorDiscovery,
    GraphNotificationDirectory,
    NotificationContact,
)
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationEventType,
    NotificationSubject,
)
from mosaic_api.repositories.base import DirectoryRepository, EntitlementRepository
from mosaic_api.services.notification_events import build_notification_event
from mosaic_api.services.notification_visibility import (
    NotificationVisibility,
    usage_role_authorizer,
)


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
