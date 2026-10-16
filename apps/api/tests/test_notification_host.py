from typing import cast
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from mosaic_api.auth import AuthContext
from mosaic_api.config import Settings
from mosaic_api.domain import AccessRequest, AuditEvent, EntitlementResource
from mosaic_api.main import create_app
from mosaic_api.repositories.base import EntitlementRepository
from mosaic_api.services.budget_notification_bridge import BudgetNotificationBridge
from mosaic_api.services.notifications import NotificationPartialAudienceError, NotificationService


@pytest.mark.parametrize(
    "path",
    [
        "/notifications/health",
        "/notifications/me/inbox",
        "/notifications/me/preferences",
        "/me/notifications/health",
        "/me/notifications/inbox",
        "/me/notifications/preferences",
        "/approval-policies",
        "/approval-evaluator",
        "/settings/approval-ai",
        "/approval-ai/readiness",
    ],
)
def test_composed_feature_routes_work_with_optional_providers_unconfigured(
    settings: Settings,
    path: str,
) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1" + path)
        assert response.status_code == 200, response.text


def test_portal_role_cannot_read_admin_notifications_but_admins_can_use_the_portal(
    settings: Settings,
) -> None:
    with TestClient(create_app(settings.model_copy(update={"local_roles": ["Admin"]}))) as client:
        assert client.get("/api/v1/notifications/me/inbox").status_code == 200
        assert client.get("/api/v1/me/notifications/inbox").status_code == 200
    with TestClient(create_app(settings.model_copy(update={"local_roles": ["User"]}))) as client:
        assert client.get("/api/v1/notifications/me/inbox").status_code == 403
        assert client.get("/api/v1/me/notifications/inbox").status_code == 200


def test_committed_request_reaches_own_inbox_without_graph_and_never_another_user(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.portal is not None
        repository = cast(EntitlementRepository, app.state.entitlement_repository)
        notifications = cast(NotificationService, app.state.notification_service)
        request = AccessRequest(
            id="request",
            tenant_id=settings.tenant_id,
            requester_object_id="local-admin",
            resource=EntitlementResource(kind="modelApi", id="resource"),
        )
        client.portal.call(
            repository.create_access_request,
            request,
            AuditEvent(
                id="audit",
                tenant_id=settings.tenant_id,
                action="accessRequest.created",
                resource_type="accessRequest",
                resource_id=request.id,
                actor_object_id="local-admin",
            ),
        )
        client.portal.call(notifications.run_once, settings.tenant_id)
        worker = client.portal.call(
            notifications.repository.get_worker_state, settings.tenant_id, "notifications"
        )
        assert worker and NotificationPartialAudienceError.__name__ in (worker.last_error or "")
        inbox = client.get("/api/v1/me/notifications/inbox").json()
        assert inbox["unreadCount"] == 1
        item = inbox["items"][0]
        assert item["subject"]["requestId"] == request.id
        assert (
            client.patch(
                f"/api/v1/me/notifications/inbox/{item['id']}",
                json={"read": True},
            ).status_code
            == 200
        )
        client.portal.call(notifications.run_once, settings.tenant_id)
        assert client.get("/api/v1/me/notifications/inbox").json()["unreadCount"] == 0
        monkeypatch.setattr(
            app.state.authenticator,
            "authenticate",
            AsyncMock(
                return_value=AuthContext(
                    tenant_id=settings.tenant_id,
                    object_id="another-user",
                    roles=frozenset({"User"}),
                )
            ),
        )
        assert client.get("/api/v1/me/notifications/inbox").json()["items"] == []
        assert client.get(f"/api/v1/me/notifications/inbox/{item['id']}").status_code == 404


@pytest.mark.parametrize("enabled", [False, True])
def test_host_preserves_the_budget_processing_switch_for_migrated_deliveries(
    settings: Settings,
    enabled: bool,
) -> None:
    with patch(
        "mosaic_api.main.BudgetNotificationBridge", wraps=BudgetNotificationBridge
    ) as factory:
        with TestClient(create_app(settings.model_copy(update={"budgets_enabled": enabled}))):
            assert factory.call_args.kwargs["enabled"] is enabled
