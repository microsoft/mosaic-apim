"""The price list and each endpoint's pricing facts. Every route needs ``Admin``. See ADR 0020."""

from typing import Annotated, cast

from fastapi import APIRouter, Query, Request, status

from mosaic_api.api import Admin, _actor
from mosaic_api.pricing import EndpointPricingUpdate, PriceCreate
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


def _pricing(request: Request) -> PricingService:
    return cast(PricingService, request.app.state.pricing_service)


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
