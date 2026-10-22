"""Complete read evidence for an explicitly requested cohort, never a directory preview."""

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from mosaic_api.errors import ValidationError

_OBJECT_ID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class GroupMembershipEvidence:
    tenant_id: str
    group_object_id: str
    candidate_object_ids: frozenset[str]
    member_object_ids: frozenset[str]
    # Freshness starts before the first read, not after a potentially slow enumeration.
    checked_at: datetime


def parse_object_id(value: object) -> str | None:
    if not isinstance(value, str) or not _OBJECT_ID.fullmatch(value):
        return None
    normalized = value.lower()
    return normalized if normalized != "00000000-0000-0000-0000-000000000000" else None


def verification_scope(
    group_object_id: str, candidate_object_ids: Sequence[str]
) -> tuple[str, frozenset[str]]:
    group = parse_object_id(group_object_id)
    if group is None:
        raise ValidationError("Group object ID must be a nonzero GUID")
    candidates: set[str] = set()
    for value in candidate_object_ids:
        normalized = parse_object_id(value)
        if normalized is None:
            raise ValidationError("Every membership-verification candidate must be a nonzero GUID")
        candidates.add(normalized)
    if not candidates:
        raise ValidationError("Membership verification requires at least one candidate")
    return group, frozenset(candidates)


def validate_verification_timeout(timeout_seconds: float) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 60
    ):
        raise ValidationError(
            "Membership verification timeout must be finite, greater than 0, and at most 60 seconds"
        )
