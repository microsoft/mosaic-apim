"""Administering cost centers, and which of them a caller may charge. See ADR 0021."""

from collections import Counter
from typing import Any

import structlog

from mosaic_api.cost_centers import (
    COST_CENTERS_BUSY,
    COST_CENTERS_SCOPE,
    CostCenter,
    CostCenterBook,
    CostCenterCreate,
    CostCenterLimitsUpdate,
    CostCenterMember,
    CostCenterMemberView,
    CostCenterSettings,
    CostCenterSettingsUpdate,
    CostCenterUpdate,
    CostCenterView,
    PortalCostCenter,
    default_settings,
    general_cost_center,
    tenant_default_id,
)
from mosaic_api.domain import (
    AuditEvent,
    EntitlementResourceKind,
    PrincipalKind,
    general_cost_center_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError, ValidationError
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
)
from mosaic_api.services.directory import Actor
from mosaic_api.services.model_access import scope_lease

logger = structlog.get_logger()


async def load_book(repository: CostCenterRepository | None, tenant_id: str) -> CostCenterBook:
    """Every cost center and the settings, with General present even before it's first saved."""

    if repository is None:
        return CostCenterBook(tenant_id, [], None)
    cost_centers = await repository.list_cost_centers(tenant_id)
    settings = await repository.get_settings(tenant_id)
    return CostCenterBook(tenant_id, cost_centers, settings)


class CostCenterService:
    def __init__(
        self,
        repository: CostCenterRepository,
        *,
        directory_repository: DirectoryRepository,
        entitlement_repository: EntitlementRepository,
        gateway_repository: GatewayRepository,
        entitlements: Any,
    ) -> None:
        # ``entitlements`` is the EntitlementService, which revokes a removed member's grants
        # under the publication locks its own mutations take.
        self._repository = repository
        self._directory = directory_repository
        self._entitlement_records = entitlement_repository
        self._gateways = gateway_repository
        self._entitlements = entitlements

    @staticmethod
    def _audit(
        actor: Actor,
        action: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        *,
        resource_type: str = "costCenter",
    ) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_object_id=actor.object_id,
            details=details or {},
        )

    async def book(self, tenant_id: str) -> CostCenterBook:
        return await load_book(self._repository, tenant_id)

    # -- reading ------------------------------------------------------------------------------

    async def _stored(self, actor: Actor, cost_center_id: str) -> CostCenter:
        """A cost center as saved. General is saved the first time anyone changes it."""

        found = await self._repository.get_cost_center(actor.tenant_id, cost_center_id)
        if found is not None:
            return found
        if cost_center_id == general_cost_center_id(actor.tenant_id):
            general = general_cost_center(actor.tenant_id)
            try:
                await self._repository.create_cost_center(
                    general,
                    self._audit(actor, "costCenter.created", general.id, {"builtIn": True}),
                )
            except ConflictError:
                pass
            stored = await self._repository.get_cost_center(actor.tenant_id, cost_center_id)
            if stored is not None:
                return stored
        raise NotFoundError("Cost center not found", details={"id": cost_center_id})

    async def _views(self, actor: Actor, book: CostCenterBook) -> list[CostCenterView]:
        principals = {
            principal.id: principal
            for principal in await self._directory.list_principals(actor.tenant_id)
        }
        grants = await self._entitlement_records.list_entitlements(actor.tenant_id)
        counts = Counter(grant.cost_center_id for grant in grants)
        enabled = Counter(grant.cost_center_id for grant in grants if grant.enabled)
        defaults: dict[str, list[str]] = {}
        for principal in principals.values():
            if principal.kind == PrincipalKind.SECURITY_GROUP:
                continue
            defaults.setdefault(book.default_for(principal), []).append(principal.id)
        views: list[CostCenterView] = []
        for cost_center in sorted(
            book.by_id.values(), key=lambda item: (not item.built_in, item.name.casefold())
        ):
            explicit = cost_center.member_ids()
            details: list[CostCenterMemberView] = []
            for member in cost_center.members:
                listed = principals.get(member.principal_id)
                details.append(
                    CostCenterMemberView(
                        principal_id=member.principal_id,
                        object_id=listed.object_id if listed else None,
                        label=(listed.label or listed.object_id) if listed else None,
                        kind=listed.kind if listed else None,
                        explicit=True,
                        is_default=bool(
                            listed and listed.default_cost_center_id == cost_center.id
                        ),
                        added_at=member.added_at,
                    )
                )
            # People whose own default this is may charge it without being listed. The tenant's
            # default is everyone's, so listing them there would list everybody.
            listed_defaults = (
                [] if cost_center.id == book.tenant_default_id else defaults.get(cost_center.id, [])
            )
            for principal_id in listed_defaults:
                principal = principals[principal_id]
                if principal_id in explicit or principal.default_cost_center_id != cost_center.id:
                    continue
                details.append(
                    CostCenterMemberView(
                        principal_id=principal_id,
                        object_id=principal.object_id,
                        label=principal.label or principal.object_id,
                        kind=principal.kind,
                        explicit=False,
                        is_default=True,
                    )
                )
            views.append(
                CostCenterView.model_validate(
                    {
                        **cost_center.model_dump(by_alias=False),
                        "etag": cost_center.etag,
                        "is_tenant_default": cost_center.id == book.tenant_default_id,
                        "member_details": details,
                        "grant_count": counts.get(cost_center.id, 0),
                        "enabled_grant_count": enabled.get(cost_center.id, 0),
                        "default_for": len(defaults.get(cost_center.id, [])),
                    }
                )
            )
        return views

    async def list_cost_centers(self, actor: Actor) -> list[CostCenterView]:
        return await self._views(actor, await self.book(actor.tenant_id))

    async def get_cost_center(self, actor: Actor, cost_center_id: str) -> CostCenterView:
        book = await self.book(actor.tenant_id)
        if book.get(cost_center_id) is None:
            raise NotFoundError("Cost center not found", details={"id": cost_center_id})
        views = await self._views(actor, book)
        return next(view for view in views if view.id == cost_center_id)

    async def get_settings(self, actor: Actor) -> CostCenterSettings:
        return await self._repository.get_settings(actor.tenant_id) or default_settings(
            actor.tenant_id
        )

    # -- writing ------------------------------------------------------------------------------

    async def _require_unique_code(
        self, actor: Actor, code: str, *, exclude: str | None = None
    ) -> None:
        book = await self.book(actor.tenant_id)
        clash = next(
            (
                item
                for item in book.by_id.values()
                if item.code.casefold() == code.casefold() and item.id != exclude
            ),
            None,
        )
        if clash is not None:
            raise ConflictError(
                f"Cost center {clash.name} already uses the code {clash.code}. Codes are unique, "
                "whatever their letter case.",
                details={"reason": "codeInUse", "costCenterId": clash.id},
            )

    async def create_cost_center(
        self, actor: Actor, request: CostCenterCreate
    ) -> CostCenterView:
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            await self._require_unique_code(actor, request.code)
            cost_center = CostCenter(
                id=new_id("costCenter"),
                tenant_id=actor.tenant_id,
                name=request.name.strip(),
                code=request.code,
                description=request.description,
                owners=request.owners,
                keys_allowed=request.keys_allowed,
            )
            await self._repository.create_cost_center(
                cost_center,
                self._audit(actor, "costCenter.created", cost_center.id, {"code": request.code}),
            )
        logger.info("cost_center_created", cost_center_id=cost_center.id, tenant_id=actor.tenant_id)
        return await self.get_cost_center(actor, cost_center.id)

    async def update_cost_center(
        self, actor: Actor, cost_center_id: str, request: CostCenterUpdate
    ) -> CostCenterView:
        changes = request.model_dump(by_alias=False, exclude_unset=True)
        for field in ("name", "code", "owners", "keys_allowed"):
            if field in changes and changes[field] is None:
                changes.pop(field)
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            current = await self._stored(actor, cost_center_id)
            if "code" in changes:
                await self._require_unique_code(actor, changes["code"], exclude=current.id)
            if "name" in changes:
                changes["name"] = changes["name"].strip()
            updated = CostCenter.model_validate(
                {
                    **current.model_dump(by_alias=False),
                    **changes,
                    "etag": current.etag,
                    "updated_at": utc_now(),
                }
            )
            details: dict[str, Any] = {"fields": sorted(changes)}
            if "code" in changes and changes["code"] != current.code:
                details.update({"previousCode": current.code, "code": changes["code"]})
            await self._repository.save_cost_center(
                updated, self._audit(actor, "costCenter.updated", cost_center_id, details)
            )
        return await self.get_cost_center(actor, cost_center_id)

    async def delete_cost_center(self, actor: Actor, cost_center_id: str) -> None:
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            current = await self._repository.get_cost_center(actor.tenant_id, cost_center_id)
            if current is None:
                raise NotFoundError("Cost center not found", details={"id": cost_center_id})
            if current.built_in:
                raise ConflictError(
                    "General is built in. Rename it instead of deleting it.",
                    details={"reason": "builtIn"},
                )
            settings = await self._repository.get_settings(actor.tenant_id)
            if current.id == tenant_default_id(settings, actor.tenant_id):
                raise ConflictError(
                    "This is the tenant's default cost center. Choose another default first.",
                    details={"reason": "tenantDefault"},
                )
            grants = [
                grant
                for grant in await self._entitlement_records.list_entitlements(actor.tenant_id)
                if grant.cost_center_id == current.id
            ]
            if grants:
                raise ConflictError(
                    "Grants are charged to this cost center. Remove them before deleting it.",
                    details={"reason": "hasGrants", "grants": len(grants)},
                )
            defaults = [
                principal.id
                for principal in await self._directory.list_principals(actor.tenant_id)
                if principal.default_cost_center_id == current.id
            ]
            if defaults:
                raise ConflictError(
                    "This is the default cost center of principals. Change their default first.",
                    details={"reason": "isDefault", "principals": len(defaults)},
                )
            await self._repository.delete_cost_center(
                current, self._audit(actor, "costCenter.deleted", current.id)
            )

    async def set_limits(
        self, actor: Actor, cost_center_id: str, request: CostCenterLimitsUpdate
    ) -> CostCenterView:
        for limit in request.limits:
            await self._require_resource(actor, limit.resource.kind, limit.resource.id)
        current = await self._stored(actor, cost_center_id)
        updated = current.model_copy(
            update={"limits": request.limits, "updated_at": utc_now()}, deep=True
        )
        await self._repository.save_cost_center(
            updated,
            self._audit(
                actor, "costCenter.limitsUpdated", cost_center_id, {"limits": len(request.limits)}
            ),
        )
        return await self.get_cost_center(actor, cost_center_id)

    async def _require_resource(
        self, actor: Actor, kind: EntitlementResourceKind, resource_id: str
    ) -> None:
        if kind == EntitlementResourceKind.MODEL_API:
            found: object = await self._gateways.get_model_api(actor.tenant_id, resource_id)
        else:
            found = await self._gateways.get_mcp_server(actor.tenant_id, resource_id)
        if found is None:
            raise ValidationError(
                "MOSAIC doesn't govern the model or MCP server a limit names",
                details={"resourceKind": str(kind), "resourceId": resource_id},
            )

    async def add_member(
        self, actor: Actor, cost_center_id: str, principal_id: str
    ) -> CostCenterView:
        principal = await self._directory.get_principal(actor.tenant_id, principal_id)
        if principal is None:
            raise NotFoundError("Principal was not found", details={"id": principal_id})
        current = await self._stored(actor, cost_center_id)
        if principal_id in current.member_ids():
            return await self.get_cost_center(actor, cost_center_id)
        if len(current.members) >= 500:
            raise ValidationError(
                "A cost center lists at most 500 members. Add a security group instead."
            )
        updated = current.model_copy(
            update={
                "members": [
                    *current.members,
                    CostCenterMember(principal_id=principal_id, added_by=actor.object_id),
                ],
                "updated_at": utc_now(),
            },
            deep=True,
        )
        await self._repository.save_cost_center(
            updated,
            self._audit(
                actor, "costCenter.memberAdded", cost_center_id, {"principalId": principal_id}
            ),
        )
        return await self.get_cost_center(actor, cost_center_id)

    async def remove_member(
        self, actor: Actor, cost_center_id: str, principal_id: str
    ) -> CostCenterView:
        """Stop a principal charging the cost center, and revoke its grants under it.

        The member is removed, and the principal's default reset if it was this cost center,
        before its grants are revoked. A grant written meanwhile checks the membership again
        after it's saved (see ``EntitlementService._revoke_if_ineligible``), so none outlives the
        membership. A failure part way leaves grants to revoke: repeating the request finishes
        the job. The next apply of each grant's model deletes its key.
        """

        current = await self._stored(actor, cost_center_id)
        principal = await self._directory.get_principal(actor.tenant_id, principal_id)
        explicit = principal_id in current.member_ids()
        is_default = principal is not None and principal.default_cost_center_id == current.id
        settings = await self._repository.get_settings(actor.tenant_id)
        charged = current.id == tenant_default_id(settings, actor.tenant_id)
        remaining = (
            []
            if charged
            else [
                grant
                for grant in await self._entitlement_records.list_entitlements(
                    actor.tenant_id, subject_id=principal_id
                )
                if grant.cost_center_id == current.id and grant.revocation is None
            ]
        )
        if not explicit and not is_default and not remaining:
            raise NotFoundError(
                "That principal isn't a member of this cost center",
                details={"costCenterId": cost_center_id, "principalId": principal_id},
            )
        if explicit:
            await self._repository.save_cost_center(
                current.model_copy(
                    update={
                        "members": [
                            member
                            for member in current.members
                            if member.principal_id != principal_id
                        ],
                        "updated_at": utc_now(),
                    },
                    deep=True,
                ),
                self._audit(
                    actor,
                    "costCenter.memberRemoved",
                    cost_center_id,
                    {"principalId": principal_id},
                ),
            )
        if is_default and principal is not None:
            await self._directory.save_principal(
                principal.model_copy(
                    update={"default_cost_center_id": None, "updated_at": utc_now()}
                ),
                self._audit(
                    actor,
                    "principal.updated",
                    principal.id,
                    {"defaultCostCenterReset": current.id},
                    resource_type="principal",
                ),
            )
        # Everyone may charge the tenant's default, so leaving it revokes nothing.
        revoked: list[str] = []
        if not charged:
            for grant in await self._entitlement_records.list_entitlements(
                actor.tenant_id, subject_id=principal_id
            ):
                if grant.cost_center_id != current.id or grant.revocation is not None:
                    continue
                await self._entitlements.revoke_for_cost_center(actor, grant.id, current.id)
                revoked.append(grant.id)
            # People may have charged it only through the group that left. A member whose
            # principal is gone may have been one, so it's checked too.
            if explicit and (principal is None or principal.kind == PrincipalKind.SECURITY_GROUP):
                revoked.extend(await self._entitlements.recheck_cost_center(actor, current.id))
        logger.info(
            "cost_center_member_removed",
            cost_center_id=cost_center_id,
            principal_id=principal_id,
            revoked_grants=len(revoked),
            tenant_id=actor.tenant_id,
        )
        return await self.get_cost_center(actor, cost_center_id)

    async def update_settings(
        self, actor: Actor, request: CostCenterSettingsUpdate
    ) -> CostCenterSettings:
        """Name the tenant's default cost center.

        Everyone may charge the tenant default, so moving it off a cost center revokes the grants
        under that cost center whose subjects may charge it no other way: as a listed member,
        through a listed security group, or as their own default.
        """

        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            book = await self.book(actor.tenant_id)
            if book.get(request.default_cost_center_id) is None:
                raise ValidationError(
                    "No cost center has that ID",
                    details={"defaultCostCenterId": request.default_cost_center_id},
                )
            await self._stored(actor, request.default_cost_center_id)
            current = await self._repository.get_settings(actor.tenant_id)
            previous = tenant_default_id(current, actor.tenant_id)
            settings = (current or default_settings(actor.tenant_id)).model_copy(
                update={
                    "default_cost_center_id": request.default_cost_center_id,
                    "updated_at": utc_now(),
                    "etag": current.etag if current else None,
                }
            )
            await self._repository.save_settings(
                settings,
                self._audit(
                    actor,
                    "costCenter.settingsUpdated",
                    settings.id,
                    {
                        "defaultCostCenterId": request.default_cost_center_id,
                        "previousDefaultCostCenterId": previous,
                    },
                    resource_type="costCenterSettings",
                ),
            )
        if previous != request.default_cost_center_id:
            revoked = await self._entitlements.recheck_cost_center(actor, previous)
            logger.info(
                "cost_center_tenant_default_changed",
                previous_cost_center_id=previous,
                cost_center_id=request.default_cost_center_id,
                revoked_grants=len(revoked),
                tenant_id=actor.tenant_id,
            )
        return await self.get_settings(actor)

    # -- the caller's own ---------------------------------------------------------------------

    async def chargeable_for_caller(self, actor: Actor) -> list[PortalCostCenter]:
        """The cost centers the caller may charge, from their own token's groups only."""

        book = await self.book(actor.tenant_id)
        principal = await self._directory.find_principal_by_object_id(
            actor.tenant_id, actor.object_id
        )
        groups = [
            item.id
            for item in await self._directory.list_principals(actor.tenant_id)
            if item.kind == PrincipalKind.SECURITY_GROUP
            and item.object_id.casefold() in actor.group_ids
        ]
        default_id = book.default_for(principal)
        return [
            PortalCostCenter(
                id=item.id,
                name=item.name,
                code=item.code,
                is_default=item.id == default_id,
                keys_allowed=item.keys_allowed,
            )
            for item in book.chargeable(principal, groups)
        ]
