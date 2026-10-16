"""A source-container outbox transfers budget send ownership without a dual-send window."""

from typing import Literal, Self
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator

from mosaic_api.budgets import Budget, BudgetNotification, BudgetState
from mosaic_api.domain import CostCenterRef, Entity, deterministic_id
from mosaic_api.notifications import NotificationDeliveryState
from mosaic_api.scheduling import UtcInstant


def budget_notice_outbox_id(tenant_id: str, notice_id: str) -> str:
    return deterministic_id("budgetNotificationOutbox", tenant_id, notice_id)


def budget_notice_operation_id(notice_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"mosaic-budget-notice:{notice_id}"))


class BudgetNotificationOutbox(Entity):
    entity_type: Literal["budgetNotificationOutbox"] = "budgetNotificationOutbox"
    budget: Budget
    notice: BudgetNotification
    state_snapshot: BudgetState
    cost_center: CostCenterRef | None = None
    recipients: list[str] = Field(default_factory=list)
    ready: bool = False
    delivery_state: NotificationDeliveryState = "queued"
    provider_accepted_at: UtcInstant | None = None
    enforcement_done: int | None = Field(default=None, ge=0)
    enforcement_total: int | None = Field(default=None, ge=0)
    ttl: int = 90 * 86400

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        if (
            self.budget.tenant_id != self.tenant_id
            or self.state_snapshot.tenant_id != self.tenant_id
            or self.state_snapshot.budget_id != self.budget.id
            or self.id != budget_notice_outbox_id(self.tenant_id, self.notice.id)
        ):
            raise ValueError("A budget notice must identify its own tenant, budget and source")
        return self
