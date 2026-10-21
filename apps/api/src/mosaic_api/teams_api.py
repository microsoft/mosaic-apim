"""Admin destination settings and user-self-scoped tests; callback never uses MOSAIC auth."""

import json
from typing import cast

from fastapi import APIRouter, HTTPException, Request, Response

from mosaic_api.api import Admin, AnyMosaicRole
from mosaic_api.integrations.teams import TeamsAuthenticationError
from mosaic_api.notifications import (
    CapabilityCheck,
    PersonalTeamsSettings,
    PersonalTeamsSettingsInput,
    TeamsDestination,
    TeamsDestinationInput,
)
from mosaic_api.services.teams import PersonalTeamsStatus, TeamsService, TeamsTestResult

teams_router = APIRouter(prefix="/api/v1", tags=["teams"])


def _service(request: Request) -> TeamsService:
    service = getattr(request.app.state, "teams_service", None)
    if service is None:
        raise HTTPException(503, "Optional Teams integration is not configured.")
    return cast(TeamsService, service)


@teams_router.get("/settings/teams/destinations", response_model=list[TeamsDestination])
async def destinations(request: Request, auth: Admin) -> list[TeamsDestination]:
    return await _service(request).list_destinations(auth.tenant_id)


@teams_router.post("/settings/teams/destinations", response_model=TeamsDestination, status_code=201)
async def create_destination(
    request: Request, auth: Admin, payload: TeamsDestinationInput
) -> TeamsDestination:
    return await _service(request).save_destination(auth.tenant_id, auth.object_id, payload)


@teams_router.get("/settings/teams/destinations/{identifier}", response_model=TeamsDestination)
async def destination(request: Request, auth: Admin, identifier: str) -> TeamsDestination:
    return await _service(request).destination(auth.tenant_id, identifier)


@teams_router.put("/settings/teams/destinations/{identifier}", response_model=TeamsDestination)
async def update_destination(
    request: Request, auth: Admin, identifier: str, payload: TeamsDestinationInput
) -> TeamsDestination:
    return await _service(request).save_destination(
        auth.tenant_id, auth.object_id, payload, identifier=identifier
    )


@teams_router.delete("/settings/teams/destinations/{identifier}", status_code=204)
async def delete_destination(request: Request, auth: Admin, identifier: str) -> Response:
    await _service(request).delete_destination(auth.tenant_id, auth.object_id, identifier)
    return Response(status_code=204)


@teams_router.post("/settings/teams/destinations/{identifier}/test", response_model=TeamsTestResult)
async def test_destination(request: Request, auth: Admin, identifier: str) -> TeamsTestResult:
    return await _service(request).test_shared(auth.tenant_id, identifier)


@teams_router.get(
    "/settings/teams/destinations/{identifier}/checks", response_model=list[CapabilityCheck]
)
async def check_destination(
    request: Request, auth: Admin, identifier: str
) -> list[CapabilityCheck]:
    return await _service(request).destination_checks(auth.tenant_id, identifier)


@teams_router.get("/settings/teams/personal", response_model=PersonalTeamsSettings)
async def personal_settings(request: Request, auth: Admin) -> PersonalTeamsSettings:
    return await _service(request).personal_settings(auth.tenant_id)


@teams_router.put("/settings/teams/personal", response_model=PersonalTeamsSettings)
async def save_personal_settings(
    request: Request, auth: Admin, payload: PersonalTeamsSettingsInput
) -> PersonalTeamsSettings:
    return await _service(request).save_personal_settings(auth.tenant_id, auth.object_id, payload)


@teams_router.get("/me/notifications/teams", response_model=PersonalTeamsStatus)
async def personal_status(request: Request, auth: AnyMosaicRole) -> PersonalTeamsStatus:
    return await _service(request).personal_status(auth.tenant_id, auth.object_id)


@teams_router.post("/me/notifications/teams/test", response_model=TeamsTestResult)
async def test_personal(request: Request, auth: AnyMosaicRole) -> TeamsTestResult:
    # Caller identity, not a body/query recipient, chooses the only conversation tested.
    return await _service(request).test_personal(auth.tenant_id, auth.object_id, "portal")


@teams_router.get("/notifications/me/teams", response_model=PersonalTeamsStatus)
async def admin_personal_status(request: Request, auth: Admin) -> PersonalTeamsStatus:
    return await _service(request).personal_status(auth.tenant_id, auth.object_id, "admin")


@teams_router.get("/me/notifications/teams/checks", response_model=list[CapabilityCheck])
async def check_personal_access(request: Request, auth: AnyMosaicRole) -> list[CapabilityCheck]:
    return await _service(request).personal_checks(
        auth.tenant_id, auth.object_id, "portal", verify_access=True
    )


@teams_router.get("/notifications/me/teams/checks", response_model=list[CapabilityCheck])
async def check_admin_personal_access(request: Request, auth: Admin) -> list[CapabilityCheck]:
    return await _service(request).personal_checks(
        auth.tenant_id, auth.object_id, "admin", verify_access=True
    )


@teams_router.post("/notifications/me/teams/test", response_model=TeamsTestResult)
async def test_admin_personal(request: Request, auth: Admin) -> TeamsTestResult:
    return await _service(request).test_personal(auth.tenant_id, auth.object_id, "admin")


@teams_router.post("/teams/messages", status_code=202)
async def messages(request: Request) -> Response:
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > 65536:
            raise HTTPException(413, "Teams lifecycle activity is too large.")
    try:
        activity = json.loads(data)
        if not isinstance(activity, dict):
            raise ValueError
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(400, "A Teams lifecycle activity is required.") from None
    try:
        await _service(request).lifecycle(request.headers.get("Authorization", ""), activity)
    except TeamsAuthenticationError:
        raise HTTPException(
            401, "Teams Connector authentication or personal scope validation failed."
        ) from None
    return Response(status_code=202)
