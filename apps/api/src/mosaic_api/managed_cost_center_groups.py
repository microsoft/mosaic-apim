"""Keep retired local cost-center projections read-only; ordinary local groups are unchanged."""

from mosaic_api.domain import Group, GroupPurpose
from mosaic_api.errors import ConflictError


def require_unmanaged_group(group: Group | None) -> None:
    if group is not None and group.purpose == GroupPurpose.COST_CENTER:
        raise ConflictError(
            "This legacy local cost-center group is read-only. Explicitly reconfigure or "
            "recreate its cost center with a dedicated Entra group; local membership is not "
            "directory membership.",
            details={"costCenterId": group.cost_center_id},
        )
