"""Cost-center group ownership, independent of any gateway authorization contract."""

from mosaic_api.cost_centers import MAX_MEMBERS, CostCenter, CostCenterMember
from mosaic_api.domain import Group, GroupMembership, GroupPurpose, deterministic_id
from mosaic_api.errors import ConflictError, ValidationError


def cost_center_group_id(tenant_id: str, cost_center_id: str) -> str:
    return deterministic_id("costCenterGroup", tenant_id, cost_center_id)


def require_unmanaged_group(group: Group | None) -> None:
    if group is not None and group.purpose == GroupPurpose.COST_CENTER:
        raise ConflictError(
            "This group is owned by a cost center; edit or delete its cost center instead",
            details={"costCenterId": group.cost_center_id},
        )


def linked_cost_center(cost_center: CostCenter) -> CostCenter:
    expected = cost_center_group_id(cost_center.tenant_id, cost_center.id)
    if cost_center.group_id not in {None, expected}:
        raise ConflictError("The cost center names a group it does not own")
    if len(cost_center.members) > MAX_MEMBERS:
        raise ValidationError("A managed cost-center group lists at most 500 references")
    if len(cost_center.member_ids()) != len(cost_center.members):
        raise ConflictError("Remove duplicate cost-center membership references before migration")
    return cost_center.model_copy(
        update={
            "group_id": expected,
            "membership_revision": max(1, cost_center.membership_revision),
        },
        deep=True,
    )


def managed_group(cost_center: CostCenter, current: Group | None = None) -> Group:
    linked = linked_cost_center(cost_center)
    if current is not None and (
        current.tenant_id != linked.tenant_id
        or current.id != linked.group_id
        or current.purpose != GroupPurpose.COST_CENTER
        or current.cost_center_id != linked.id
    ):
        raise ConflictError("The linked group is not owned by this cost center")
    return Group(
        id=cost_center_group_id(linked.tenant_id, linked.id),
        tenant_id=linked.tenant_id,
        name=f"Cost center: {linked.name}",
        description=linked.description,
        purpose=GroupPurpose.COST_CENTER,
        cost_center_id=linked.id,
        created_at=current.created_at if current else linked.created_at,
        updated_at=linked.updated_at,
        etag=current.etag if current else None,
    )


def membership_changed(previous: CostCenter, updated: CostCenter) -> CostCenter:
    if previous.group_id is not None and updated.group_id != previous.group_id:
        raise ConflictError("A managed group cannot be unlinked or replaced")
    if updated.group_id is None:
        return updated
    linked = linked_cost_center(updated)
    revision = (
        previous.membership_revision + 1
        if previous.group_id is None or previous.members != updated.members
        else previous.membership_revision
    )
    return linked.model_copy(update={"membership_revision": max(1, revision)}, deep=True)


def group_membership(group: Group, member: CostCenterMember) -> GroupMembership:
    return GroupMembership(
        id=deterministic_id("membership", group.tenant_id, group.id, member.principal_id),
        tenant_id=group.tenant_id,
        group_id=group.id,
        principal_id=member.principal_id,
        created_at=member.added_at,
        updated_at=member.added_at,
    )
