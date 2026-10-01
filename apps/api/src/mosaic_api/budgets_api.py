"""Budgets, the email that warns about them, and the portal's budget alerts. See ADR 0023.

The administrator routes need ``Admin``. ``GET /api/v1/me/budgets`` answers for the caller only,
from their own grants: the cost centers they charge whose budgets are near, at, or past their
limit, as totals.
"""

from typing import cast

from fastapi import APIRouter, Request, Response, status

from mosaic_api.api import Admin, PortalUser, _actor
from mosaic_api.budgets import (
    BudgetOverview,
    BudgetUpdate,
    BudgetView,
    EmailSettingsUpdate,
    EmailSettingsView,
    EmailTest,
    EmailTestResult,
    PortalBudgetAlert,
)
from mosaic_api.services.budgets import BudgetService

budgets_router = APIRouter(prefix="/api/v1", tags=["admin"])
portal_budgets_router = APIRouter(prefix="/api/v1/me", tags=["portal"])


def _budgets(request: Request) -> BudgetService:
    return cast(BudgetService, request.app.state.budget_service)


@budgets_router.get("/budgets", response_model=BudgetOverview)
async def budget_overview(request: Request, auth: Admin) -> BudgetOverview:
    return await _budgets(request).overview(_actor(auth))


@budgets_router.post("/budgets/check", response_model=BudgetOverview)
async def check_budgets(request: Request, auth: Admin) -> BudgetOverview:
    """Judge every budget now, at most once a minute."""

    return await _budgets(request).check_now(_actor(auth))


@budgets_router.get("/budgets/organization", response_model=BudgetView | None)
async def organization_budget(request: Request, auth: Admin) -> BudgetView | None:
    return await _budgets(request).get_budget(_actor(auth), None)


@budgets_router.put("/budgets/organization", response_model=BudgetView)
async def set_organization_budget(
    request: Request, auth: Admin, payload: BudgetUpdate
) -> BudgetView:
    return await _budgets(request).set_budget(_actor(auth), None, payload)


@budgets_router.delete("/budgets/organization", status_code=status.HTTP_204_NO_CONTENT)
async def delete_organization_budget(request: Request, auth: Admin) -> Response:
    await _budgets(request).delete_budget(_actor(auth), None)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@budgets_router.get("/cost-centers/{cost_center_id}/budget", response_model=BudgetView | None)
async def cost_center_budget(
    request: Request, auth: Admin, cost_center_id: str
) -> BudgetView | None:
    return await _budgets(request).get_budget(_actor(auth), cost_center_id)


@budgets_router.put("/cost-centers/{cost_center_id}/budget", response_model=BudgetView)
async def set_cost_center_budget(
    request: Request, auth: Admin, cost_center_id: str, payload: BudgetUpdate
) -> BudgetView:
    return await _budgets(request).set_budget(_actor(auth), cost_center_id, payload)


@budgets_router.delete(
    "/cost-centers/{cost_center_id}/budget", status_code=status.HTTP_204_NO_CONTENT
)
async def delete_cost_center_budget(
    request: Request, auth: Admin, cost_center_id: str
) -> Response:
    await _budgets(request).delete_budget(_actor(auth), cost_center_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@budgets_router.get("/settings/email", response_model=EmailSettingsView)
async def email_settings(request: Request, auth: Admin) -> EmailSettingsView:
    return await _budgets(request).email_settings(_actor(auth))


@budgets_router.put("/settings/email", response_model=EmailSettingsView)
async def save_email_settings(
    request: Request, auth: Admin, payload: EmailSettingsUpdate
) -> EmailSettingsView:
    return await _budgets(request).save_email_settings(_actor(auth), payload)


@budgets_router.post("/settings/email/test", response_model=EmailTestResult)
async def send_test_email(request: Request, auth: Admin, payload: EmailTest) -> EmailTestResult:
    """Send one email, now, through the saved settings, whether or not email is turned on."""

    return await _budgets(request).send_test_email(_actor(auth), payload)


@portal_budgets_router.get("/budgets", response_model=list[PortalBudgetAlert])
async def my_budget_alerts(request: Request, auth: PortalUser) -> list[PortalBudgetAlert]:
    return await _budgets(request).alerts_for_caller(_actor(auth))
