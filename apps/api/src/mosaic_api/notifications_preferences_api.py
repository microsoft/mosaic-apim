"""Settings routes are separate from the inbox, delivery diagnostics and health routers."""

from typing import Annotated, cast

from fastapi import APIRouter, Header, Request, Response, status

from mosaic_api.api import Admin, PortalUser
from mosaic_api.domain import utc_now
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.graph.notifications import AdministratorDiscovery
from mosaic_api.notifications import (
    NotificationAudience,
    NotificationChannelSettings,
    NotificationChannelSettingsInput,
    NotificationEvent,
    NotificationEventType,
    NotificationPreferences,
    NotificationPreferencesInput,
    NotificationRoute,
    NotificationRouteInput,
    NotificationSubject,
    notification_category,
)
from mosaic_api.services.notification_preferences import (
    NotificationPreferenceService,
    PreferencePreview,
    RoutePreview,
)

notification_preferences_router = APIRouter(prefix="/api/v1", tags=["admin"])
portal_notification_preferences_router = APIRouter(prefix="/api/v1/me", tags=["portal"])
RevisionHeader = Annotated[str | None, Header(alias="If-Match")]


def _service(request: Request) -> NotificationPreferenceService:
    return cast(NotificationPreferenceService, request.app.state.notification_preference_service)


def _revision(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        revision = int(value.strip('"'))
        if revision < 1:
            raise ValueError()
        return revision
    except ValueError as exc:
        raise ValidationError(
            "If-Match must contain the current notification record revision."
        ) from exc


@notification_preferences_router.get(
    "/notifications/me/preferences", response_model=NotificationPreferences
)
async def admin_preferences(request: Request, auth: Admin) -> NotificationPreferences:
    return await _service(request).get_preferences(auth.tenant_id, auth.object_id, "admin")


@notification_preferences_router.put(
    "/notifications/me/preferences", response_model=NotificationPreferences
)
async def save_admin_preferences(
    request: Request,
    auth: Admin,
    payload: NotificationPreferencesInput,
    if_match: RevisionHeader = None,
) -> NotificationPreferences:
    return await _service(request).save_preferences(
        auth.tenant_id, auth.object_id, "admin", payload, expected_revision=_revision(if_match)
    )


@notification_preferences_router.get(
    "/notifications/me/preferences/preview", response_model=PreferencePreview
)
async def preview_admin_preferences(request: Request, auth: Admin) -> PreferencePreview:
    return await _service(request).preview_preferences(auth.tenant_id, auth.object_id, "admin")


@portal_notification_preferences_router.get(
    "/notifications/preferences", response_model=NotificationPreferences
)
async def portal_preferences(request: Request, auth: PortalUser) -> NotificationPreferences:
    return await _service(request).get_preferences(auth.tenant_id, auth.object_id, "portal")


@portal_notification_preferences_router.put(
    "/notifications/preferences", response_model=NotificationPreferences
)
async def save_portal_preferences(
    request: Request,
    auth: PortalUser,
    payload: NotificationPreferencesInput,
    if_match: RevisionHeader = None,
) -> NotificationPreferences:
    return await _service(request).save_preferences(
        auth.tenant_id, auth.object_id, "portal", payload, expected_revision=_revision(if_match)
    )


@portal_notification_preferences_router.get(
    "/notifications/preferences/preview", response_model=PreferencePreview
)
async def preview_portal_preferences(request: Request, auth: PortalUser) -> PreferencePreview:
    return await _service(request).preview_preferences(auth.tenant_id, auth.object_id, "portal")


@notification_preferences_router.get(
    "/settings/notifications", response_model=NotificationChannelSettings
)
async def channel_settings(request: Request, auth: Admin) -> NotificationChannelSettings:
    return await _service(request).get_settings(auth.tenant_id)


@notification_preferences_router.put(
    "/settings/notifications", response_model=NotificationChannelSettings
)
async def save_channel_settings(
    request: Request,
    auth: Admin,
    payload: NotificationChannelSettingsInput,
    if_match: RevisionHeader = None,
) -> NotificationChannelSettings:
    return await _service(request).save_settings(
        auth.tenant_id, auth.object_id, payload, expected_revision=_revision(if_match)
    )


@notification_preferences_router.get(
    "/notification-routes/administrator-choices", response_model=AdministratorDiscovery
)
async def administrator_choices(request: Request, auth: Admin) -> AdministratorDiscovery:
    return await _service(request).administrator_choices(auth.tenant_id)


@notification_preferences_router.get("/notification-routes", response_model=list[NotificationRoute])
async def list_routes(request: Request, auth: Admin) -> list[NotificationRoute]:
    return await _service(request).list_routes(auth.tenant_id)


@notification_preferences_router.post(
    "/notification-routes", response_model=NotificationRoute, status_code=status.HTTP_201_CREATED
)
async def create_route(
    request: Request, auth: Admin, payload: NotificationRouteInput
) -> NotificationRoute:
    return await _service(request).save_route(auth.tenant_id, auth.object_id, payload)


@notification_preferences_router.get(
    "/notification-routes/{route_id}", response_model=NotificationRoute
)
async def get_route(request: Request, auth: Admin, route_id: str) -> NotificationRoute:
    return await _service(request).get_route(auth.tenant_id, route_id)


@notification_preferences_router.put(
    "/notification-routes/{route_id}", response_model=NotificationRoute
)
async def update_route(
    request: Request,
    auth: Admin,
    route_id: str,
    payload: NotificationRouteInput,
    if_match: RevisionHeader = None,
) -> NotificationRoute:
    return await _service(request).save_route(
        auth.tenant_id,
        auth.object_id,
        payload,
        route_id=route_id,
        expected_revision=_revision(if_match),
    )


@notification_preferences_router.delete(
    "/notification-routes/{route_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_route(
    request: Request, auth: Admin, route_id: str, if_match: RevisionHeader = None
) -> Response:
    await _service(request).delete_route(
        auth.tenant_id, auth.object_id, route_id, expected_revision=_revision(if_match)
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@notification_preferences_router.post(
    "/notification-routes/{route_id}/preview", response_model=RoutePreview
)
async def preview_event_route(
    request: Request, auth: Admin, route_id: str, payload: NotificationEvent
) -> RoutePreview:
    return await _service(request).preview_route(auth.tenant_id, route_id, payload)


@notification_preferences_router.get(
    "/notification-routes/{route_id}/preview", response_model=RoutePreview
)
async def preview_route(request: Request, auth: Admin, route_id: str) -> RoutePreview:
    route = await _service(request).get_route(auth.tenant_id, route_id)
    event_type = next(
        (
            event
            for event in NotificationEventType
            if notification_category(event) in route.categories
            and (not route.event_types or event in route.event_types)
            and (route.audience != "budgetRecipients" or event.startswith("budget."))
        ),
        None,
    )
    if event_type is None:
        return RoutePreview(
            route_id=route_id,
            recipients=[],
            unavailable=["No matching preview event is available."],
        )
    event = NotificationEvent(
        id="route-preview",
        tenant_id=auth.tenant_id,
        event_type=event_type,
        category=notification_category(event_type),
        source_id="route-preview",
        source_revision="preview",
        occurred_at=utc_now(),
        origin="routePreview",
        subject=NotificationSubject(budget_id=route.budget_id),
        audience=NotificationAudience(include_administrators=True, budget_id=route.budget_id),
    )
    return await _service(request).preview_route(auth.tenant_id, route_id, event)
