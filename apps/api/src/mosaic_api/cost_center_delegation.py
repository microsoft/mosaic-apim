from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from mosaic_api.cost_centers import PocRole
from mosaic_api.domain import (
    AccessRequestState,
    CostCenterRef,
    MosaicModel,
    RequestFormSnapshotView,
)
from mosaic_api.scheduling import UtcInstant

ReviewKind = Literal["resourceAccess", "costCenterMembership"]
ReviewOutcome = Literal["approve", "deny"]


class PocContact(MosaicModel):
    role: PocRole
    display_name: str


class ReviewCenter(MosaicModel):
    center: CostCenterRef
    roles: list[PocRole]


class ReviewItem(MosaicModel):
    id: str
    kind: ReviewKind
    title: str
    requester: str | None
    state: AccessRequestState
    created_at: datetime


class ReviewPage(MosaicModel):
    items: list[ReviewItem]
    next_cursor: str | None = None


class ReviewPosition(MosaicModel):
    created_at: UtcInstant
    kind: ReviewKind
    id: str = Field(min_length=1, max_length=128)

    @classmethod
    def at(cls, item: ReviewItem) -> "ReviewPosition":
        return cls(created_at=item.created_at, kind=item.kind, id=item.id)

    @property
    def key(self) -> tuple[str, ReviewKind, str]:
        return review_time_key(self.created_at), self.kind, self.id


def review_time_key(created_at: datetime) -> str:
    # Match the stored ISO string that Cosmos indexes, including whole-second values.
    return created_at.isoformat().replace("+00:00", "Z")


def review_sort_key(item: ReviewItem) -> tuple[str, ReviewKind, str]:
    return review_time_key(item.created_at), item.kind, item.id


class ReviewDetail(ReviewItem):
    scope: ReviewCenter
    environment: str | None = None
    justification: str | None = None
    recorded_form: RequestFormSnapshotView | None = None
    has_person_limits: bool | None = None
    can_decide: bool
    approval_blocked: bool = False
    decision_at: datetime | None = None
    decision_reason: str | None = None
    decided_by_you: bool = False
    membership_state: Literal["pending", "succeeded", "failed", "unknown"] | None = None
    revision: str


class ReviewDecision(MosaicModel):
    revision: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=2000)
    acknowledge_membership_changes: bool = False

    @field_validator("reason")
    @classmethod
    def require_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A decision requires a reason")
        return value.strip()
