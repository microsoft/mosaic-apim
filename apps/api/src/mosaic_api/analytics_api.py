"""Administrator analytics, and each gateway's telemetry readiness. See ADR 0019."""

from datetime import date
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query, Request, Response, status

from mosaic_api.api import Admin, _actor
from mosaic_api.domain import EntitlementSubjectKind
from mosaic_api.errors import ConflictError
from mosaic_api.services.analytics import (
    AnalyticsConsumers,
    AnalyticsCost,
    AnalyticsFilters,
    AnalyticsGatewayHealth,
    AnalyticsHygiene,
    AnalyticsLimits,
    AnalyticsModels,
    AnalyticsOverview,
    AnalyticsRange,
    AnalyticsReliability,
    AnalyticsService,
    AnalyticsStatus,
    AnalyticsUnattributed,
    ExportView,
    TelemetryBackfillRequest,
)
from mosaic_api.services.telemetry import GatewayTelemetry, TelemetryService
from mosaic_api.services.usage_rollup import UsageRollupService

analytics_router = APIRouter(prefix="/api/v1", tags=["admin"])

_ID = 200


def _analytics(request: Request) -> AnalyticsService:
    return cast(AnalyticsService, request.app.state.analytics_service)


def _telemetry(request: Request) -> TelemetryService:
    service = getattr(request.app.state, "telemetry_service", None)
    if service is None:
        raise ConflictError("This deployment doesn't check gateway telemetry")
    return cast(TelemetryService, service)


def _rollups(request: Request) -> UsageRollupService:
    service = getattr(request.app.state, "usage_rollup_service", None)
    if service is None:
        raise ConflictError("This deployment doesn't roll up gateway telemetry")
    return cast(UsageRollupService, service)


def analytics_filters(
    range_: Annotated[AnalyticsRange, Query(alias="range")] = "30d",
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    gateway_id: Annotated[str | None, Query(alias="gatewayId", max_length=_ID)] = None,
    environment: Annotated[str | None, Query(max_length=_ID)] = None,
    resource_id: Annotated[str | None, Query(alias="resourceId", max_length=_ID)] = None,
    subject_kind: Annotated[EntitlementSubjectKind | None, Query(alias="subjectKind")] = None,
    cost_center_id: Annotated[str | None, Query(alias="costCenterId", max_length=_ID)] = None,
) -> AnalyticsFilters:
    return AnalyticsFilters(
        range=range_,
        start=start if range_ == "custom" else None,
        end=end if range_ == "custom" else None,
        gateway_id=gateway_id or None,
        environment=environment or None,
        resource_id=resource_id or None,
        subject_kind=subject_kind,
        cost_center_id=cost_center_id or None,
    )


Filters = Annotated[AnalyticsFilters, Depends(analytics_filters)]


@analytics_router.get("/analytics/status", response_model=AnalyticsStatus)
async def analytics_status(request: Request, auth: Admin) -> AnalyticsStatus:
    return await _analytics(request).status(_actor(auth))


@analytics_router.post(
    "/analytics/refresh", response_model=AnalyticsStatus, status_code=status.HTTP_202_ACCEPTED
)
async def refresh_analytics(request: Request, auth: Admin) -> AnalyticsStatus:
    return await _analytics(request).refresh(_actor(auth))


@analytics_router.get("/analytics/overview", response_model=AnalyticsOverview)
async def analytics_overview(
    request: Request, auth: Admin, filters: Filters
) -> AnalyticsOverview:
    return await _analytics(request).overview(_actor(auth), filters)


@analytics_router.get("/analytics/consumers", response_model=AnalyticsConsumers)
async def analytics_consumers(
    request: Request, auth: Admin, filters: Filters
) -> AnalyticsConsumers:
    return await _analytics(request).consumers(_actor(auth), filters)


@analytics_router.get("/analytics/models", response_model=AnalyticsModels)
async def analytics_models(request: Request, auth: Admin, filters: Filters) -> AnalyticsModels:
    return await _analytics(request).models(_actor(auth), filters)


@analytics_router.get("/analytics/reliability", response_model=AnalyticsReliability)
async def analytics_reliability(
    request: Request, auth: Admin, filters: Filters
) -> AnalyticsReliability:
    return await _analytics(request).reliability(_actor(auth), filters)


@analytics_router.get("/analytics/limits", response_model=AnalyticsLimits)
async def analytics_limits(request: Request, auth: Admin, filters: Filters) -> AnalyticsLimits:
    return await _analytics(request).limits(_actor(auth), filters)


@analytics_router.get("/analytics/hygiene", response_model=AnalyticsHygiene)
async def analytics_hygiene(request: Request, auth: Admin, filters: Filters) -> AnalyticsHygiene:
    return await _analytics(request).hygiene(_actor(auth), filters)


@analytics_router.get("/analytics/unattributed", response_model=AnalyticsUnattributed)
async def analytics_unattributed(
    request: Request, auth: Admin, filters: Filters
) -> AnalyticsUnattributed:
    return await _analytics(request).unattributed(_actor(auth), filters)


@analytics_router.get("/analytics/cost", response_model=AnalyticsCost)
async def analytics_cost(request: Request, auth: Admin, filters: Filters) -> AnalyticsCost:
    return await _analytics(request).cost(_actor(auth), filters)


@analytics_router.get(
    "/analytics/export",
    response_class=Response,
    responses={200: {"content": {"text/csv": {}}, "description": "The view's rows as CSV."}},
)
async def export_analytics(
    request: Request, auth: Admin, filters: Filters, view: Annotated[ExportView, Query()]
) -> Response:
    name, body = await _analytics(request).export(_actor(auth), filters, view)
    # The byte order mark tells spreadsheet programs the file is UTF-8.
    return Response(
        content="\ufeff" + body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@analytics_router.get("/gateways/{gateway_id}/telemetry", response_model=GatewayTelemetry)
async def gateway_telemetry(request: Request, auth: Admin, gateway_id: str) -> GatewayTelemetry:
    return await _telemetry(request).status(_actor(auth), gateway_id)


@analytics_router.post("/gateways/{gateway_id}/telemetry/enable", response_model=GatewayTelemetry)
async def enable_gateway_telemetry(
    request: Request, auth: Admin, gateway_id: str
) -> GatewayTelemetry:
    return await _telemetry(request).enable(_actor(auth), gateway_id)


@analytics_router.post(
    "/gateways/{gateway_id}/telemetry/refresh",
    response_model=AnalyticsGatewayHealth,
    status_code=status.HTTP_202_ACCEPTED,
)
async def refresh_gateway_telemetry(
    request: Request, auth: Admin, gateway_id: str
) -> AnalyticsGatewayHealth:
    actor = _actor(auth)
    await _rollups(request).request_refresh(actor, gateway_id)
    return await _analytics(request).gateway(actor, gateway_id)


@analytics_router.post(
    "/gateways/{gateway_id}/telemetry/backfill",
    response_model=AnalyticsGatewayHealth,
    status_code=status.HTTP_202_ACCEPTED,
)
async def backfill_gateway_telemetry(
    request: Request, auth: Admin, gateway_id: str, payload: TelemetryBackfillRequest
) -> AnalyticsGatewayHealth:
    actor = _actor(auth)
    await _rollups(request).request_backfill(actor, gateway_id, payload.days)
    return await _analytics(request).gateway(actor, gateway_id)
