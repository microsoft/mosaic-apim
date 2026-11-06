"""The price list and each endpoint's pricing facts. Every route needs ``Admin``. See ADR 0020."""

import base64
import binascii
from typing import Annotated, cast

from fastapi import APIRouter, Query, Request, status
from pydantic import ValidationError as PydanticValidationError

from mosaic_api.api import Admin, _actor
from mosaic_api.billing_pricing import (
    BillingConnectionCheckResult,
    BillingConnectionCreate,
    BillingConnectionUpdate,
    BillingConnectionView,
    BillingImportRequest,
    BillingMappingsUpdateRequest,
    BillingProvider,
    BillingPublicationView,
    BillingPublishRequest,
    BillingRunDetailView,
    BillingRunSummaryView,
    BillingRunTriggerRequest,
    BillingSchedulerStatusView,
    BillingSourceTemplateView,
)
from mosaic_api.errors import ValidationError
from mosaic_api.pricing import EndpointPricingUpdate, PriceCreate
from mosaic_api.services.billing_ingestion import (
    DEFAULT_MAX_UPLOAD_BYTES,
    BillingPayloadTooLargeError,
)
from mosaic_api.services.pricing import (
    EndpointPricingView,
    PriceHistoryView,
    PriceListView,
    PriceView,
    PricingOverview,
    PricingService,
    UnpricedReport,
)

pricing_router = APIRouter(prefix="/api/v1/pricing", tags=["admin"])

_ID = 200
_MAX_JSON_WRAPPER_OVERHEAD = 6 * 1024 * 1024


def _pricing(request: Request) -> PricingService:
    return cast(PricingService, request.app.state.pricing_service)


async def _read_bounded_stream(request: Request, *, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > max_bytes:
            raise BillingPayloadTooLargeError(
                f"Uploaded payload ({declared:,} bytes) exceeds the maximum allowed size "
                f"({max_bytes:,} bytes).",
                details={"maxUploadBytes": DEFAULT_MAX_UPLOAD_BYTES},
            )
    buf = bytearray()
    async for chunk in request.stream():
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise BillingPayloadTooLargeError(
                f"Uploaded payload exceeds the maximum allowed size "
                f"({DEFAULT_MAX_UPLOAD_BYTES:,} bytes).",
                details={"maxUploadBytes": DEFAULT_MAX_UPLOAD_BYTES},
            )
    return bytes(buf)


@pricing_router.get("", response_model=PricingOverview)
async def pricing_overview(request: Request, auth: Admin) -> PricingOverview:
    return await _pricing(request).overview(_actor(auth))


@pricing_router.get("/prices", response_model=PriceListView)
async def list_prices(
    request: Request,
    auth: Admin,
    cloud: Annotated[str, Query(pattern=r"^[a-z0-9][a-z0-9-]{0,39}$")] = "commercial",
) -> PriceListView:
    return await _pricing(request).price_list(_actor(auth), cloud)


@pricing_router.post("/prices", response_model=PriceView, status_code=status.HTTP_201_CREATED)
async def add_price(request: Request, auth: Admin, payload: PriceCreate) -> PriceView:
    return await _pricing(request).add_price(_actor(auth), payload)


@pricing_router.get("/prices/{line_id}/history", response_model=PriceHistoryView)
async def price_history(request: Request, auth: Admin, line_id: str) -> PriceHistoryView:
    return await _pricing(request).history(_actor(auth), line_id[:_ID])


@pricing_router.get("/unpriced", response_model=UnpricedReport)
async def unpriced_deployments(request: Request, auth: Admin) -> UnpricedReport:
    return await _pricing(request).unpriced(_actor(auth))


@pricing_router.get("/endpoints", response_model=list[EndpointPricingView])
async def endpoint_pricing(request: Request, auth: Admin) -> list[EndpointPricingView]:
    return await _pricing(request).endpoints(_actor(auth))


@pricing_router.patch("/endpoints/{endpoint_id}", response_model=EndpointPricingView)
async def update_endpoint_pricing(
    request: Request, auth: Admin, endpoint_id: str, payload: EndpointPricingUpdate
) -> EndpointPricingView:
    return await _pricing(request).update_endpoint(_actor(auth), endpoint_id, payload)


@pricing_router.get("/billing/templates", response_model=list[BillingSourceTemplateView])
async def list_billing_templates(
    request: Request, auth: Admin
) -> list[BillingSourceTemplateView]:
    return await _pricing(request).billing_templates(_actor(auth))


@pricing_router.post(
    "/billing/imports",
    response_model=BillingRunDetailView,
    status_code=status.HTTP_201_CREATED,
)
async def import_billing(
    request: Request,
    auth: Admin,
    provider: BillingProvider | None = None,
    pricing_cloud: Annotated[str | None, Query(alias="pricingCloud")] = None,
    source_month: Annotated[str | None, Query(alias="sourceMonth")] = None,
    file_name: Annotated[str | None, Query(alias="fileName")] = None,
    source_scope: Annotated[str | None, Query(alias="sourceScope")] = None,
    force: bool = False,
) -> BillingRunDetailView:
    content_type = (request.headers.get("content-type") or "").casefold()
    is_json_body = provider is None and (
        "application/json" in content_type or not request.query_params.get("provider")
    )

    if is_json_body:
        raw_body = await _read_bounded_stream(
            request, max_bytes=DEFAULT_MAX_UPLOAD_BYTES + _MAX_JSON_WRAPPER_OVERHEAD
        )
        try:
            parsed = BillingImportRequest.model_validate_json(raw_body)
        except PydanticValidationError as exc:
            raise ValidationError(
                "Invalid billing import request payload.",
                details={"errors": exc.errors(include_input=False, include_context=False)},
            ) from exc
        if parsed.content_base64 is not None:
            try:
                content_bytes = base64.b64decode(parsed.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValidationError(
                    "contentBase64 is not valid base64-encoded data.",
                    details={"field": "contentBase64"},
                ) from exc
        elif parsed.content_text is not None:
            content_bytes = parsed.content_text.encode("utf-8")
        else:
            raise ValidationError(
                "Provide either contentBase64 or contentText in the billing import request.",
                details={"field": "content"},
            )
        return await _pricing(request).import_billing_data(
            _actor(auth),
            provider=parsed.provider,
            pricing_cloud=parsed.pricing_cloud,
            source_month=parsed.source_month,
            file_name=parsed.file_name,
            content=content_bytes,
            source_scope=parsed.source_scope,
            force=parsed.force,
        )

    # Streaming / raw bytes upload with query parameters
    raw_bytes = await _read_bounded_stream(request, max_bytes=DEFAULT_MAX_UPLOAD_BYTES)
    resolved_provider = provider
    resolved_cloud = (
        pricing_cloud
        or request.query_params.get("pricing_cloud")
        or "commercial"
    )
    resolved_month = source_month or request.query_params.get("source_month")
    resolved_file_name = (
        file_name or request.query_params.get("file_name") or "upload.csv"
    )
    resolved_scope = source_scope or request.query_params.get("source_scope")
    assert resolved_provider is not None
    return await _pricing(request).import_billing_data(
        _actor(auth),
        provider=resolved_provider,
        pricing_cloud=resolved_cloud,
        source_month=resolved_month,
        file_name=resolved_file_name,
        content=raw_bytes,
        source_scope=resolved_scope,
        force=force,
    )


@pricing_router.get("/billing/runs", response_model=list[BillingRunSummaryView])
async def list_billing_runs(
    request: Request,
    auth: Admin,
    connection_id: Annotated[str | None, Query(alias="connectionId")] = None,
) -> list[BillingRunSummaryView]:
    return await _pricing(request).list_billing_runs(
        _actor(auth), connection_id=connection_id
    )


@pricing_router.get("/billing/runs/{run_id}", response_model=BillingRunDetailView)
async def get_billing_run(
    request: Request, auth: Admin, run_id: str
) -> BillingRunDetailView:
    return await _pricing(request).get_billing_run(_actor(auth), run_id[:_ID])


@pricing_router.patch(
    "/billing/runs/{run_id}/mappings",
    response_model=BillingRunDetailView,
)
async def update_billing_mappings(
    request: Request,
    auth: Admin,
    run_id: str,
    payload: BillingMappingsUpdateRequest,
) -> BillingRunDetailView:
    if payload.version is None:
        if_match = request.headers.get("if-match")
        if if_match and if_match.strip().strip('"'):
            payload = payload.model_copy(
                update={"version": if_match.strip().strip('"')[:_ID]}
            )
    return await _pricing(request).update_billing_mappings(
        _actor(auth), run_id[:_ID], payload
    )


@pricing_router.post(
    "/billing/runs/{run_id}/publish",
    response_model=BillingPublicationView,
)
async def publish_billing_run(
    request: Request,
    auth: Admin,
    run_id: str,
    payload: BillingPublishRequest | None = None,
) -> BillingPublicationView:
    req = payload or BillingPublishRequest()
    if req.version is None:
        if_match = request.headers.get("if-match")
        if if_match and if_match.strip().strip('"'):
            req = req.model_copy(
                update={"version": if_match.strip().strip('"')[:_ID]}
            )
    return await _pricing(request).publish_billing_run(
        _actor(auth), run_id[:_ID], req
    )


@pricing_router.post(
    "/billing/runs/{run_id}/poll",
    response_model=BillingRunDetailView,
)
async def poll_billing_run(
    request: Request,
    auth: Admin,
    run_id: str,
) -> BillingRunDetailView:
    return await _pricing(request).poll_billing_run(_actor(auth), run_id[:_ID])


@pricing_router.get(
    "/billing/publications",
    response_model=list[BillingPublicationView],
)
async def list_billing_publications(
    request: Request,
    auth: Admin,
) -> list[BillingPublicationView]:
    return await _pricing(request).list_billing_publications(_actor(auth))


@pricing_router.get(
    "/billing/connections",
    response_model=list[BillingConnectionView],
)
async def list_billing_connections(
    request: Request,
    auth: Admin,
) -> list[BillingConnectionView]:
    return await _pricing(request).list_billing_connections(_actor(auth))


@pricing_router.get(
    "/billing/connections/{connection_id}",
    response_model=BillingConnectionView,
)
async def get_billing_connection(
    request: Request,
    auth: Admin,
    connection_id: str,
) -> BillingConnectionView:
    return await _pricing(request).get_billing_connection(
        _actor(auth), connection_id[:_ID]
    )


@pricing_router.post(
    "/billing/connections",
    response_model=BillingConnectionView,
    status_code=status.HTTP_201_CREATED,
)
async def create_billing_connection(
    request: Request,
    auth: Admin,
    payload: BillingConnectionCreate,
) -> BillingConnectionView:
    return await _pricing(request).create_billing_connection(_actor(auth), payload)


@pricing_router.patch(
    "/billing/connections/{connection_id}",
    response_model=BillingConnectionView,
)
async def update_billing_connection(
    request: Request,
    auth: Admin,
    connection_id: str,
    payload: BillingConnectionUpdate,
) -> BillingConnectionView:
    if payload.version is None:
        if_match = request.headers.get("if-match")
        if if_match and if_match.strip().strip('"'):
            payload = payload.model_copy(
                update={"version": if_match.strip().strip('"')[:_ID]}
            )
    return await _pricing(request).update_billing_connection(
        _actor(auth), connection_id[:_ID], payload
    )


@pricing_router.delete(
    "/billing/connections/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_billing_connection(
    request: Request,
    auth: Admin,
    connection_id: str,
    version: Annotated[str | None, Query(max_length=_ID)] = None,
) -> None:
    resolved_version = version or request.headers.get("if-match")
    if resolved_version is not None:
        resolved_version = resolved_version.strip().strip('"')[:_ID] or None
    await _pricing(request).delete_billing_connection(
        _actor(auth), connection_id[:_ID], version=resolved_version
    )


@pricing_router.post(
    "/billing/connections/{connection_id}/check",
    response_model=BillingConnectionCheckResult,
)
async def check_billing_connection(
    request: Request,
    auth: Admin,
    connection_id: str,
) -> BillingConnectionCheckResult:
    return await _pricing(request).check_billing_connection(
        _actor(auth), connection_id[:_ID]
    )


@pricing_router.post(
    "/billing/connections/{connection_id}/runs",
    response_model=BillingRunDetailView,
    status_code=status.HTTP_201_CREATED,
)
async def start_billing_connection_run(
    request: Request,
    auth: Admin,
    connection_id: str,
    payload: BillingRunTriggerRequest | None = None,
) -> BillingRunDetailView:
    return await _pricing(request).start_billing_run(
        _actor(auth), connection_id[:_ID], payload
    )


@pricing_router.get(
    "/billing/scheduler",
    response_model=BillingSchedulerStatusView,
)
async def billing_scheduler_status(
    request: Request,
    auth: Admin,
) -> BillingSchedulerStatusView:
    actor = _actor(auth)
    scheduler = getattr(request.app.state, "billing_pricing_scheduler", None)
    if scheduler is not None:
        status_view: BillingSchedulerStatusView = await scheduler.readiness(
            actor.tenant_id
        )
        return status_view
    overview = await _pricing(request).overview(actor)
    warnings = (
        [overview.billing_schedule_warning]
        if overview.billing_schedule_warning
        else []
    )
    return BillingSchedulerStatusView(
        enabled=False,
        scheduled_workers_always_on=overview.scheduled_workers_always_on,
        billing_scheduler_always_on=overview.billing_scheduler_always_on,
        active_scheduled_connections=overview.scheduled_billing_connections,
        ready=False,
        warning=overview.billing_schedule_warning,
        warnings=warnings,
    )
