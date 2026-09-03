from enum import StrEnum

from pydantic import Field

from mosaic_api.domain import MosaicModel


class ReconciliationAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NO_CHANGE = "noChange"


class ReconciliationStep(MosaicModel):
    resource_type: str
    resource_id: str
    action: ReconciliationAction
    reason: str


class ReconciliationPlan(MosaicModel):
    tenant_id: str
    desired_revision: str
    observed_revision: str | None
    steps: list[ReconciliationStep] = Field(default_factory=list)


class ApplyResult(MosaicModel):
    operation_id: str
    applied: bool = False
    error: str | None = None
