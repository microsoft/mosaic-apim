"""Complete, current directory evidence for custom-center individual credentials and rechecks."""

from collections.abc import Sequence
from datetime import timedelta

from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import Principal, utc_now
from mosaic_api.errors import DirectoryError
from mosaic_api.integrations.graph import DirectoryLookup, GroupMembershipEvidence
from mosaic_api.integrations.graph.membership import parse_object_id


async def verified_cost_center_members(
    center: CostCenter,
    principals: Sequence[Principal],
    lookup: DirectoryLookup | None,
) -> frozenset[str]:
    """Verify one cohort, never a bounded preview, old request, local roster, or JWT claim.

    Failures return no eligibility result. Callers must retain a pending recheck instead of
    interpreting incomplete evidence as either successful membership or a definitive removal.
    """

    binding = center.entra_binding
    group_id = center.verified_group_object_id
    if (
        binding is None
        or group_id is None
        or binding.directory_tenant_id.casefold() != center.tenant_id.casefold()
    ):
        raise DirectoryError("This cost center needs a verified tenant-bound Entra group.")
    if lookup is None:
        raise DirectoryError(
            "Current Entra membership cannot be verified; Microsoft Graph lookup is not configured."
        )
    candidates = frozenset(item.object_id.casefold() for item in principals)
    if not candidates or any(parse_object_id(item) is None for item in candidates):
        raise DirectoryError("Current Entra membership needs valid individual object IDs.")
    started = utc_now()
    evidence = await lookup.verify_group_members(group_id, sorted(candidates))
    now = utc_now()
    if (
        not isinstance(evidence, GroupMembershipEvidence)
        or evidence.tenant_id.casefold() != binding.directory_tenant_id.casefold()
        or evidence.group_object_id.casefold() != group_id.casefold()
        or evidence.candidate_object_ids != candidates
        or not evidence.member_object_ids <= candidates
        or evidence.checked_at.tzinfo is None
        or evidence.checked_at < started - timedelta(seconds=30)
        or evidence.checked_at < now - timedelta(minutes=5)
        or evidence.checked_at > now + timedelta(seconds=30)
    ):
        raise DirectoryError("Microsoft Graph returned incomplete, stale, or mismatched evidence.")
    return evidence.member_object_ids
