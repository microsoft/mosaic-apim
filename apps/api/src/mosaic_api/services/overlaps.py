"""Reports grants that can apply to the same caller."""

from collections import defaultdict

import structlog

from mosaic_api.domain import (
    Entitlement,
    EntitlementResource,
    EntitlementSubjectKind,
    GrantOverlap,
    GrantOverlapKind,
    GrantOverlapReport,
    OverlapGrant,
    Principal,
    PrincipalKind,
    grant_precedence_key,
)
from mosaic_api.errors import DirectoryError
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.repositories import DirectoryRepository, EntitlementRepository, GatewayRepository
from mosaic_api.services.directory import Actor

logger = structlog.get_logger()

_PRINCIPAL_CAP = 200


def _resource_key(resource: EntitlementResource) -> tuple[str, str, str]:
    return (str(resource.kind), resource.id, resource.scope_id or "")


def _principal_label(principal: Principal) -> str:
    return principal.label or principal.object_id


def _overlap_grant(entitlement: Entitlement, label: str) -> OverlapGrant:
    return OverlapGrant(
        entitlement_id=entitlement.id,
        subject=entitlement.subject,
        subject_label=label,
        enabled=entitlement.enabled,
        enforcement=entitlement.enforcement,
    )


def _winning_group(
    entitlements: list[Entitlement],
) -> tuple[Entitlement, list[Entitlement]]:
    ordered = sorted(
        entitlements,
        key=lambda item: grant_precedence_key(item.enforcement, item.id),
    )
    return ordered[0], ordered[1:]


class GrantOverlapService:
    def __init__(
        self,
        repository: EntitlementRepository,
        *,
        directory_repository: DirectoryRepository,
        gateway_repository: GatewayRepository,
        directory_lookup: DirectoryLookup | None = None,
    ) -> None:
        self._repository = repository
        self._directory = directory_repository
        self._gateways = gateway_repository
        self._directory_lookup = directory_lookup

    async def list_overlaps(
        self, actor: Actor, *, resource_id: str | None = None
    ) -> GrantOverlapReport:
        entitlements = [
            item
            for item in await self._repository.list_entitlements(
                actor.tenant_id, resource_id=resource_id
            )
            if item.enabled
        ]
        groups = {
            item.id: item
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP
        }
        principals = {
            item.id: item
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind != PrincipalKind.SECURITY_GROUP
        }
        labels = {**{key: _principal_label(value) for key, value in groups.items()}}
        labels.update({key: _principal_label(value) for key, value in principals.items()})
        resource_labels = await self._resource_labels(actor, entitlements)

        # Grants under different cost centers never compete: a caller chooses between them.
        by_resource: dict[tuple[str, str, str, str], list[Entitlement]] = defaultdict(list)
        for entitlement in entitlements:
            by_resource[(*_resource_key(entitlement.resource), entitlement.cost_center_id)].append(
                entitlement
            )

        overlaps: list[GrantOverlap] = []
        for key, items in by_resource.items():
            group_grants = [
                item
                for item in items
                if item.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
                and item.subject.id in groups
            ]
            if len(group_grants) < 2:
                continue
            winner, shadowed = _winning_group(group_grants)
            winner_label = labels.get(winner.subject.id, winner.subject.id)
            overlaps.append(
                GrantOverlap(
                    kind=GrantOverlapKind.GROUPS,
                    resource=winner.resource,
                    resource_label=resource_labels.get(key[:3], winner.resource.id),
                    cost_center_id=key[3],
                    principal_id=None,
                    principal_label=None,
                    winner=_overlap_grant(winner, winner_label),
                    shadowed=[
                        _overlap_grant(item, labels.get(item.subject.id, item.subject.id))
                        for item in shadowed
                    ],
                    reason=(
                        "Anyone in more than one of these groups gets only "
                        f"{winner_label}'s limits, because it's the most generous."
                    ),
                )
            )

        skipped: list[str] = []
        if self._directory_lookup is None:
            skipped.append(
                "Directory membership lookup is not configured, so principal membership overlaps "
                "were not checked."
            )
            return GrantOverlapReport(
                overlaps=overlaps, membership_checked=False, skipped=skipped
            )

        checked = list(principals.values())
        if len(checked) > _PRINCIPAL_CAP:
            skipped.append(
                f"Only the first {_PRINCIPAL_CAP} recorded principals were checked for group "
                f"membership; {len(checked) - _PRINCIPAL_CAP} were skipped."
            )
            checked = checked[:_PRINCIPAL_CAP]

        try:
            for principal in checked:
                for key, items in by_resource.items():
                    group_grants = [
                        item
                        for item in items
                        if item.subject.kind == EntitlementSubjectKind.SECURITY_GROUP
                        and item.subject.id in groups
                    ]
                    if not group_grants:
                        continue
                    member_group_object_ids = await self._directory_lookup.member_groups(
                        principal.object_id,
                        [groups[item.subject.id].object_id for item in group_grants],
                    )
                    member_group_object_ids = {
                        object_id.casefold() for object_id in member_group_object_ids
                    }
                    matched_group_grants = [
                        item
                        for item in group_grants
                        if groups[item.subject.id].object_id.casefold()
                        in member_group_object_ids
                    ]
                    if not matched_group_grants:
                        continue
                    direct = next(
                        (
                            item
                            for item in items
                            if item.subject.id == principal.id
                            and item.subject.kind != EntitlementSubjectKind.GROUP
                            and item.subject.kind != EntitlementSubjectKind.SECURITY_GROUP
                        ),
                        None,
                    )
                    if direct is not None:
                        overlaps.append(
                            GrantOverlap(
                                kind=GrantOverlapKind.DIRECT_AND_GROUP,
                                resource=direct.resource,
                                resource_label=resource_labels.get(key[:3], direct.resource.id),
                                cost_center_id=key[3],
                                principal_id=principal.id,
                                principal_label=_principal_label(principal),
                                winner=_overlap_grant(direct, _principal_label(principal)),
                                shadowed=[
                                    _overlap_grant(
                                        item, labels.get(item.subject.id, item.subject.id)
                                    )
                                    for item in matched_group_grants
                                ],
                                reason=(
                                    f"{_principal_label(principal)} has a direct grant, so it "
                                    "wins over security-group grants for the same resource."
                                ),
                            )
                        )
                    if len(matched_group_grants) >= 2:
                        winner, shadowed = _winning_group(matched_group_grants)
                        winner_label = labels.get(winner.subject.id, winner.subject.id)
                        overlaps.append(
                            GrantOverlap(
                                kind=GrantOverlapKind.MULTIPLE_GROUPS,
                                resource=winner.resource,
                                resource_label=resource_labels.get(key[:3], winner.resource.id),
                                cost_center_id=key[3],
                                principal_id=principal.id,
                                principal_label=_principal_label(principal),
                                winner=_overlap_grant(winner, winner_label),
                                shadowed=[
                                    _overlap_grant(
                                        item, labels.get(item.subject.id, item.subject.id)
                                    )
                                    for item in shadowed
                                ],
                                reason=(
                                    f"{_principal_label(principal)} is in more than one granted "
                                    f"group, so {winner_label}'s limits apply."
                                ),
                            )
                        )
        except DirectoryError as error:
            logger.warning(
                "grant_overlap_membership_failed",
                tenant_id=actor.tenant_id,
                error_code=error.code,
            )
            skipped.append(
                "Directory membership lookup failed, so principal membership overlaps were not "
                "checked."
            )
            return GrantOverlapReport(
                overlaps=[
                    item for item in overlaps if item.kind == GrantOverlapKind.GROUPS
                ],
                membership_checked=False,
                skipped=skipped,
            )

        overlaps.sort(
            key=lambda item: (
                str(item.kind),
                str(item.resource.kind),
                item.resource.id,
                item.cost_center_id or "",
                item.principal_id or "",
                item.winner.entitlement_id,
            )
        )
        return GrantOverlapReport(
            overlaps=overlaps, membership_checked=True, skipped=skipped
        )

    async def _resource_labels(
        self, actor: Actor, entitlements: list[Entitlement]
    ) -> dict[tuple[str, str, str], str]:
        keys = {_resource_key(item.resource): item.resource for item in entitlements}
        labels = {key: resource.id for key, resource in keys.items()}
        model_ids = {resource.id for resource in keys.values() if resource.kind == "modelApi"}
        mcp_ids = {resource.id for resource in keys.values() if resource.kind == "mcpServer"}
        pool_ids = {
            resource.scope_id
            for resource in keys.values()
            if resource.kind == "poolModel" and resource.scope_id
        }
        if model_ids:
            for model in await self._gateways.list_model_apis(actor.tenant_id):
                key = ("modelApi", model.id, "")
                if model.id in model_ids:
                    labels[key] = model.display_name
        if mcp_ids:
            for server in await self._gateways.list_mcp_servers(actor.tenant_id):
                key = ("mcpServer", server.id, "")
                if server.id in mcp_ids:
                    labels[key] = server.display_name
        if pool_ids:
            for model_pool in await self._gateways.list_model_pools(actor.tenant_id):
                if model_pool.id not in pool_ids:
                    continue
                for pool_model in model_pool.models:
                    key = ("poolModel", pool_model.id, model_pool.id)
                    if key in labels:
                        labels[key] = f"{pool_model.display_name} in {model_pool.display_name}"
        return labels
