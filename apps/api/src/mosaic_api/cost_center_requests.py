"""Cost-center creation requests are not requests for a model or MCP grant."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mosaic_api.cost_center_directory import (
    CostCenterSetupState,
    DirectoryOperationState,
    directory_object_id,
)
from mosaic_api.cost_centers import CostCenterCreate, normalize_code
from mosaic_api.domain import (
    AccessRequestState,
    CostCenterRef,
    Entity,
    MosaicModel,
    deterministic_id,
    utc_now,
)


def requested_cost_center_id(tenant_id: str, request_id: str) -> str:
    return deterministic_id("requestedCostCenter", tenant_id, request_id)


def _optional_group_id(value: str | None) -> str | None:
    return None if value is None else directory_object_id(value)


def _optional_code(value: str | None) -> str | None:
    return None if value is None else normalize_code(value)


class CostCenterRequestCreate(MosaicModel):
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    justification: str = Field(min_length=1, max_length=4000)
    suggested_group_object_id: str | None = None
    idempotency_key: UUID | None = None

    @field_validator("name", "justification")
    @classmethod
    def validate_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A name and justification cannot be blank")
        return value.strip()

    _validate_code = field_validator("code")(_optional_code)
    _validate_group = field_validator("suggested_group_object_id")(_optional_group_id)


class CostCenterRequestDecision(MosaicModel):
    note: str | None = Field(default=None, max_length=2000)


class CostCenterRequestApproval(CostCenterRequestDecision):
    cost_center: CostCenterCreate
    add_requester: bool = False


class CostCenterRequestApprovalIntent(CostCenterRequestApproval):
    cost_center_id: str = Field(min_length=1, max_length=128)
    decided_by_object_id: str = Field(min_length=1, max_length=256)
    started_at: datetime = Field(default_factory=utc_now)


class CostCenterRequest(Entity):
    entity_type: Literal["costCenterRequest"] = "costCenterRequest"
    request_kind: Literal["costCenter"] = "costCenter"
    requester_object_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=120)
    code: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=1000)
    justification: str = Field(min_length=1, max_length=4000)
    suggested_group_object_id: str | None = None
    state: AccessRequestState = AccessRequestState.PENDING
    approval: CostCenterRequestApprovalIntent | None = None
    decided_by_object_id: str | None = None
    decided_at: datetime | None = None
    decision_note: str | None = Field(default=None, max_length=2000)
    preparation_error: str | None = Field(default=None, max_length=2000)
    requester_membership_state: DirectoryOperationState | None = None
    requester_membership_error: str | None = Field(default=None, max_length=2000)

    _validate_group = field_validator("suggested_group_object_id")(_optional_group_id)
    _validate_code = field_validator("code")(_optional_code)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.approval is not None and self.approval.cost_center_id != requested_cost_center_id(
            self.tenant_id, self.id
        ):
            raise ValueError("An approval must reserve this request's stable cost-center ID")
        if self.state == AccessRequestState.PENDING:
            if self.decided_at is not None or self.decided_by_object_id is not None:
                raise ValueError("A pending request has no final decision")
        elif self.decided_at is None or self.decided_by_object_id is None:
            raise ValueError("A decided request needs its decision time and actor")
        if self.state == AccessRequestState.APPROVED:
            if self.approval is None:
                raise ValueError("An approved request must link its center and reviewed setup")
            if self.decided_by_object_id != self.approval.decided_by_object_id:
                raise ValueError("The decision actor must match the recorded approval intent")
        if self.approval is None or not self.approval.add_requester:
            if (
                self.requester_membership_state is not None
                or self.requester_membership_error is not None
            ):
                raise ValueError("Requester membership requires an explicit administrator choice")
        if self.state != AccessRequestState.APPROVED and self.requester_membership_state not in {
            None,
            DirectoryOperationState.PENDING,
        }:
            raise ValueError("A directory membership outcome requires an approved request")
        if (
            self.requester_membership_state == DirectoryOperationState.SUCCEEDED
            and self.requester_membership_error is not None
        ):
            raise ValueError("A successful requester addition cannot carry a membership error")
        return self

    @property
    def cost_center_id(self) -> str | None:
        return self.approval.cost_center_id if self.approval is not None else None


class PortalCostCenterRequest(MosaicModel):
    id: str
    request_kind: Literal["costCenter"] = "costCenter"
    name: str
    code: str | None = None
    description: str | None = None
    justification: str
    suggested_group_object_id: str | None = None
    state: AccessRequestState
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    decision_note: str | None = None
    preparation_started: bool = False
    cost_center: CostCenterRef | None = None
    setup_state: CostCenterSetupState | None = None
    requester_membership_requested: bool = False
    requester_membership_state: DirectoryOperationState | None = None
    needs_administrator_attention: bool = False


class AdminCostCenterRequest(CostCenterRequest):
    cost_center: CostCenterRef | None = None
    setup_state: CostCenterSetupState | None = None
    needs_administrator_attention: bool = False


class RequestCounts(MosaicModel):
    pending_resource_count: int = Field(ge=0)
    pending_cost_center_count: int = Field(ge=0)
    pending_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.pending_count != self.pending_resource_count + self.pending_cost_center_count:
            raise ValueError("The pending total must include both request kinds")
        return self
