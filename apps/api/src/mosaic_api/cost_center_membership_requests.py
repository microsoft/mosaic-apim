"""Governed ordinary-user joins, independent of resource entitlements."""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from mosaic_api.cost_center_directory import DirectoryOperationState, directory_object_id
from mosaic_api.domain import (
    AccessRequestState,
    CostCenterRef,
    Entity,
    MosaicModel,
    deterministic_id,
)


class MembershipRequestCreate(MosaicModel):
    cost_center_id: str = Field(min_length=1, max_length=128)
    justification: str = Field(min_length=1, max_length=4000)
    idempotency_key: UUID

    @field_validator("justification")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A justification cannot be blank")
        return value.strip()


class MembershipDecision(MosaicModel):
    note: str | None = Field(default=None, max_length=2000)


class MembershipApproval(MembershipDecision):
    acknowledge_membership_changes: Literal[True]


class MembershipRequest(Entity):
    entity_type: Literal["costCenterMembershipRequest"] = "costCenterMembershipRequest"
    request_kind: Literal["costCenterMembership"] = "costCenterMembership"
    requester_object_id: str
    cost_center: CostCenterRef
    directory_tenant_id: str
    group_object_id: str
    justification: str = Field(min_length=1, max_length=4000)
    state: AccessRequestState = AccessRequestState.PENDING
    decided_at: datetime | None = None
    decided_by_object_id: str | None = None
    decision_note: str | None = Field(default=None, max_length=2000)
    membership_state: DirectoryOperationState | None = None
    membership_error: str | None = Field(default=None, max_length=2000)
    operation_id: str | None = None

    _ids = field_validator("requester_object_id", "directory_tenant_id", "group_object_id")(
        directory_object_id
    )

    @model_validator(mode="after")
    def decision(self) -> Self:
        if self.state == AccessRequestState.PENDING:
            if self.decided_at is not None or self.decided_by_object_id is not None:
                raise ValueError("Pending requests cannot have decisions")
        elif self.decided_at is None or not self.decided_by_object_id:
            raise ValueError("A closed request requires its decision time and actor")
        if self.state != AccessRequestState.APPROVED and (
            self.membership_state is not None
            or self.operation_id is not None
            or self.membership_error is not None
        ):
            raise ValueError("Only approval may initiate membership")
        if self.membership_state == DirectoryOperationState.SUCCEEDED and self.membership_error:
            raise ValueError("Successful membership has no error")
        return self

    @property
    def active(self) -> bool:
        return self.state == AccessRequestState.PENDING or (
            self.state == AccessRequestState.APPROVED
            and self.membership_state != DirectoryOperationState.SUCCEEDED
        )


def membership_guard_id(request: MembershipRequest) -> str:
    return deterministic_id(
        "costCenterMembershipGuard",
        request.tenant_id,
        request.requester_object_id,
        request.cost_center.id,
    )


class MembershipRequestGuard(Entity):
    entity_type: Literal["costCenterMembershipGuard"] = "costCenterMembershipGuard"
    request_id: str


class PortalMembershipRequest(MosaicModel):
    id: str
    request_kind: Literal["costCenterMembership"] = "costCenterMembership"
    cost_center: CostCenterRef
    justification: str
    state: AccessRequestState
    created_at: datetime
    updated_at: datetime
    decided_at: datetime | None = None
    decision_note: str | None = None
    membership_state: DirectoryOperationState | None = None
    needs_administrator_attention: bool = False


class AdminMembershipRequest(PortalMembershipRequest):
    requester_object_id: str
    membership_error: str | None = None
    operation_id: str | None = None


class SelfServiceSettings(MosaicModel):
    creation_requests_enabled: bool = True
    membership_requests_enabled: bool = False


class PortalCenter(MosaicModel):
    id: str
    name: str
    code: str
    public_purpose: str | None = None
    built_in: bool = False
    membership_state: Literal["member", "notMember", "unknown", "exempt"]
    token_eligible: bool


class PortalCenterPage(MosaicModel):
    items: list[PortalCenter]
    next_cursor: str | None = None
    membership_error: str | None = None
