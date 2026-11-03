"""Administering cost centers, and which of them a caller may charge. See ADR 0022."""

from collections import Counter
from contextlib import suppress
from typing import Any

import structlog

from mosaic_api.cost_center_directory import DirectoryOperationState
from mosaic_api.cost_center_management import (
    CostCenterDirectoryMembers,
    CostCenterDirectoryOperationView,
    CostCenterGroupCapabilities,
    CostCenterManagementView,
)
from mosaic_api.cost_centers import (
    COST_CENTERS_BUSY,
    COST_CENTERS_SCOPE,
    CostCenter,
    CostCenterBook,
    CostCenterCreate,
    CostCenterLimitsUpdate,
    CostCenterSettings,
    CostCenterSettingsUpdate,
    CostCenterUpdate,
    PendingRecheck,
    PortalCostCenter,
    default_settings,
    general_cost_center,
    tenant_default_id,
    with_recheck,
)
from mosaic_api.domain import (
    AuditEvent,
    EntitlementResourceKind,
    EntitlementSubjectKind,
    PrincipalKind,
    general_cost_center_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    DirectoryError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.integrations.graph import DirectoryLookup, GroupManagement
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
)
from mosaic_api.services.cost_center_directory import CostCenterDirectoryService
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


def cost_center_audit(
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


async def stored_cost_center(
    repository: CostCenterRepository, actor: Actor, cost_center_id: str
) -> CostCenter:
    """A cost center as saved. General is saved the first time anyone changes it."""

    found = await repository.get_cost_center(actor.tenant_id, cost_center_id)
    if found is not None:
        return found
    if cost_center_id == general_cost_center_id(actor.tenant_id):
        general = general_cost_center(actor.tenant_id)
        try:
            await repository.create_cost_center(
                general,
                cost_center_audit(actor, "costCenter.created", general.id, {"builtIn": True}),
            )
        except ConflictError:
            pass
        stored = await repository.get_cost_center(actor.tenant_id, cost_center_id)
        if stored is not None:
            return stored
    raise NotFoundError("Cost center not found", details={"id": cost_center_id})


async def record_recheck(
    repository: CostCenterRepository,
    actor: Actor,
    cost_center_id: str,
    recheck: PendingRecheck,
) -> None:
    """Record that grants under the cost center need checking again, before the change that may
    stop their subjects charging it. A failure after that change then leaves the recheck to
    finish, and applies leave the grants it covers out until it does."""

    current = await stored_cost_center(repository, actor, cost_center_id)
    pending = with_recheck(current.pending_rechecks, recheck)
    if pending == current.pending_rechecks:
        return
    await repository.save_cost_center(
        current.model_copy(
            update={"pending_rechecks": pending, "updated_at": utc_now()}, deep=True
        ),
        cost_center_audit(
            actor,
            "costCenter.recheckRequested",
            current.id,
            {"reason": recheck.reason, "subjectId": recheck.subject_id},
        ),
    )


class CostCenterService:
    def __init__(
        self,
        repository: CostCenterRepository,
        *,
        directory_repository: DirectoryRepository,
        entitlement_repository: EntitlementRepository,
        gateway_repository: GatewayRepository,
        entitlements: Any,
        directory_lookup: DirectoryLookup | None = None,
        group_management: GroupManagement | None = None,
        directory_tenant_id: str | None = None,
        group_management_unavailable_reason: str | None = None,
    ) -> None:
        # ``entitlements`` is the EntitlementService, which revokes a removed member's grants
        # under the publication locks its own mutations take.
        self._repository = repository
        self._directory = directory_repository
        self._entitlement_records = entitlement_repository
        self._gateways = gateway_repository
        self._entitlements = entitlements
        self._directory_groups = CostCenterDirectoryService(
            repository,
            gateway_repository,
            directory_lookup=directory_lookup,
            group_management=group_management,
            directory_tenant_id=directory_tenant_id,
            access_checks=entitlements,
            unavailable_reason=group_management_unavailable_reason,
        )

    @staticmethod
    def _audit(
        actor: Actor,
        action: str,
        resource_id: str,
        details: dict[str, Any] | None = None,
        *,
        resource_type: str = "costCenter",
    ) -> AuditEvent:
        return cost_center_audit(actor, action, resource_id, details, resource_type=resource_type)

    async def book(self, tenant_id: str) -> CostCenterBook:
        return await load_book(self._repository, tenant_id)

    # -- reading ------------------------------------------------------------------------------

    async def _stored(self, actor: Actor, cost_center_id: str) -> CostCenter:
        return await stored_cost_center(self._repository, actor, cost_center_id)

    async def _views(self, actor: Actor, book: CostCenterBook) -> list[CostCenterManagementView]:
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
        views: list[CostCenterManagementView] = []
        for cost_center in sorted(
            book.by_id.values(), key=lambda item: (not item.built_in, item.name.casefold())
        ):
            views.append(
                CostCenterManagementView.model_validate(
                    {
                        **cost_center.model_dump(by_alias=False),
                        "etag": cost_center.etag,
                        "is_tenant_default": cost_center.id == book.tenant_default_id,
                        "member_details": [],
                        "grant_count": counts.get(cost_center.id, 0),
                        "enabled_grant_count": enabled.get(cost_center.id, 0),
                        "default_for": len(defaults.get(cost_center.id, [])),
                    }
                )
            )
        return views

    async def list_cost_centers(self, actor: Actor) -> list[CostCenterManagementView]:
        return await self._views(actor, await self.book(actor.tenant_id))

    async def get_cost_center(self, actor: Actor, cost_center_id: str) -> CostCenterManagementView:
        book = await self.book(actor.tenant_id)
        if book.get(cost_center_id) is None:
            raise NotFoundError("Cost center not found", details={"id": cost_center_id})
        views = await self._views(actor, book)
        view = next(view for view in views if view.id == cost_center_id)
        return view.model_copy(
            update={
                "directory_operations": await self._directory_groups.list_operations(
                    actor, cost_center_id
                )
            }
        )

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

    async def create(
        self,
        actor: Actor,
        request: CostCenterCreate,
        *,
        cost_center_id: str | None = None,
    ) -> CostCenterManagementView:
        """Save before Graph; the internal forced ID lets request approval safely resume."""

        if not request.name.strip():
            raise ValidationError("A cost center name cannot be blank")
        if cost_center_id is not None and (
            not cost_center_id.strip()
            or len(cost_center_id) > 128
            or cost_center_id == general_cost_center_id(actor.tenant_id)
        ):
            raise ValidationError(
                "A reserved custom cost-center ID must be nonempty and not General"
            )
        proposed = CostCenter(
            id=cost_center_id or new_id("costCenter"),
            tenant_id=actor.tenant_id,
            name=request.name.strip(),
            code=request.code,
            description=request.description,
            owners=request.owners,
            keys_allowed=request.keys_allowed,
            resource_access=request.resource_access,
        )
        await self._entitlements.validate_cost_center_access(actor, proposed)
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            existing = await self._repository.get_cost_center(actor.tenant_id, proposed.id)
            if existing is not None:
                fields = (
                    "name",
                    "code",
                    "description",
                    "owners",
                    "keys_allowed",
                    "resource_access",
                )
                if (
                    cost_center_id is None
                    or existing.is_general
                    or any(getattr(existing, field) != getattr(proposed, field) for field in fields)
                ):
                    raise ConflictError(
                        "The reserved ID belongs to a different or subsequently edited center",
                        details={"costCenterId": proposed.id},
                    )
                self._directory_groups.require_custom_center(existing)
            else:
                await self._require_unique_code(actor, request.code)
                await self._repository.create_cost_center(
                    proposed,
                    self._audit(actor, "costCenter.created", proposed.id, {"code": request.code}),
                )
        try:
            if request.group_setup is not None:
                await self._directory_groups.setup_group(actor, proposed.id, request.group_setup)
        except DomainError as exc:
            exc.details.update({"savedCostCenterId": proposed.id})
            raise
        except Exception as exc:
            raise ChangeNotRecordedError(
                "The cost center is saved, but its directory setup could not finish. Reload "
                "this center and its recorded operations; do not create another center or group.",
                details={"savedCostCenterId": proposed.id},
            ) from exc
        logger.info("cost_center_created", cost_center_id=proposed.id, tenant_id=actor.tenant_id)
        return await self.get_cost_center(actor, proposed.id)

    async def create_cost_center(
        self,
        actor: Actor,
        request: CostCenterCreate,
        *,
        cost_center_id: str | None = None,
    ) -> CostCenterManagementView:
        return await self.create(actor, request, cost_center_id=cost_center_id)

    async def update_cost_center(
        self, actor: Actor, cost_center_id: str, request: CostCenterUpdate
    ) -> CostCenterManagementView:
        changes = request.model_dump(by_alias=False, exclude_unset=True, exclude={"group_setup"})
        for field in ("name", "code", "owners", "keys_allowed", "resource_access"):
            if field in changes and changes[field] is None:
                changes.pop(field)
        if "name" in changes:
            changes["name"] = changes["name"].strip()
            if not changes["name"]:
                raise ValidationError("A cost center name cannot be blank")
        before = await self._stored(actor, cost_center_id)
        if request.group_setup is not None:
            self._directory_groups.require_custom_center(before)
        proposed = CostCenter.model_validate(
            {**before.model_dump(by_alias=False), **changes, "etag": before.etag}
        )
        if {"resource_access", "keys_allowed"} & changes.keys():
            await self._entitlements.validate_cost_center_access(actor, proposed)
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            current = await self._stored(actor, cost_center_id)
            if current.etag != before.etag:
                raise ConflictError("The cost center changed; reload it and try again")
            if "code" in changes:
                await self._require_unique_code(actor, changes["code"], exclude=current.id)
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
        try:
            if request.group_setup is not None:
                await self._directory_groups.setup_group(actor, cost_center_id, request.group_setup)
            if "resource_access" in changes:
                await self._entitlements.sync_cost_center_access(actor, cost_center_id)
        except DomainError as exc:
            exc.details.update({"savedCostCenterId": cost_center_id})
            raise
        except Exception as exc:
            raise ChangeNotRecordedError(
                "The cost-center update is saved, but directory/access setup could not finish. "
                "Reload this center and its recorded operations before retrying.",
                details={"savedCostCenterId": cost_center_id},
            ) from exc
        return await self.get_cost_center(actor, cost_center_id)

    async def delete_cost_center(self, actor: Actor, cost_center_id: str) -> None:
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            current = await self._repository.get_cost_center(actor.tenant_id, cost_center_id)
            if current is None:
                raise NotFoundError("Cost center not found", details={"id": cost_center_id})
            if current.is_general:
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
            all_grants = await self._entitlement_records.list_entitlements(actor.tenant_id)
            grants = [grant for grant in all_grants if grant.cost_center_id == current.id]
            if grants:
                raise ConflictError(
                    "Grants are charged to this cost center. Remove them before deleting it.",
                    details={"reason": "hasGrants", "grants": len(grants)},
                )
            applied = [
                (item.id, item.applied_access)
                for item in await self._gateways.list_publications(actor.tenant_id)
            ]
            applied_mcp = [
                (item.id, item.applied_access)
                for item in await self._gateways.list_mcp_publications(actor.tenant_id)
            ]
            applied_pools = [
                (item.id, item.applied_access)
                for item in await self._gateways.list_model_pools(actor.tenant_id)
            ]
            if any(
                access is not None
                and any(grant.cost_center_id == current.id for grant in access.grants)
                for _, access in [*applied, *applied_mcp, *applied_pools]
            ):
                raise ConflictError(
                    "Applied gateway access still names this cost center. Revoke and apply first.",
                    details={"reason": "hasAppliedGrants"},
                )
            unsettled = await self._repository.list_directory_operations(
                actor.tenant_id, current.id, limit=1, unsettled_only=True
            )
            if unsettled:
                raise ConflictError(
                    "Resolve this cost center's pending or unknown directory operation first",
                    details={
                        "reason": "directoryOperationUnsettled",
                        "operationId": unsettled[0].id,
                    },
                )
            if current.group_id and any(
                grant.subject.kind == EntitlementSubjectKind.GROUP
                and grant.subject.id == current.group_id
                for grant in all_grants
            ):
                raise ConflictError(
                    "Grants still reference this cost center's managed group",
                    details={"reason": "groupHasGrants", "groupId": current.group_id},
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
    ) -> CostCenterManagementView:
        for limit in request.limits:
            await self._require_resource(
                actor, limit.resource.kind, limit.resource.id, limit.resource.scope_id
            )
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
        self,
        actor: Actor,
        kind: EntitlementResourceKind,
        resource_id: str,
        scope_id: str | None = None,
    ) -> None:
        if kind == EntitlementResourceKind.MODEL_API:
            found: object = await self._gateways.get_model_api(actor.tenant_id, resource_id)
        elif kind == EntitlementResourceKind.POOL_MODEL:
            model_pool = (
                await self._gateways.get_model_pool(actor.tenant_id, scope_id) if scope_id else None
            )
            found = (
                next((item for item in model_pool.models if item.id == resource_id), None)
                if model_pool is not None
                else None
            )
        else:
            found = await self._gateways.get_mcp_server(actor.tenant_id, resource_id)
        if found is None:
            raise ValidationError(
                "MOSAIC doesn't govern the model or MCP server a limit names",
                details={"resourceKind": str(kind), "resourceId": resource_id},
            )

    async def add_member(
        self, actor: Actor, cost_center_id: str, principal_id: str
    ) -> CostCenterManagementView:
        view, _ = await self.add_member_with_status(actor, cost_center_id, principal_id)
        return view

    async def add_member_with_status(
        self, actor: Actor, cost_center_id: str, principal_id: str
    ) -> tuple[CostCenterManagementView, bool]:
        principal = await self._directory.get_principal(actor.tenant_id, principal_id)
        if principal is None:
            raise NotFoundError("Principal was not found", details={"id": principal_id})
        if await self._directory_groups.direct_membership(
            actor, cost_center_id, principal.object_id
        ):
            return await self.get_cost_center(actor, cost_center_id), False
        operation = await self.add_directory_member(
            actor,
            cost_center_id,
            principal.object_id,
            principal.kind,
            idempotency_key=new_id("member"),
        )
        self._require_confirmed_member_write(operation)
        return await self.get_cost_center(actor, cost_center_id), True

    async def remove_member(
        self, actor: Actor, cost_center_id: str, principal_id: str, *, explicit_only: bool = False
    ) -> CostCenterManagementView:
        """Compatibility wrapper: mutate a real direct relationship, never a local roster."""

        principal = await self._directory.get_principal(actor.tenant_id, principal_id)
        if principal is None:
            raise NotFoundError("Principal was not found", details={"id": principal_id})
        operation = await self.remove_directory_member(
            actor,
            cost_center_id,
            principal.object_id,
            idempotency_key=new_id("member"),
        )
        self._require_confirmed_member_write(operation)
        return await self.get_cost_center(actor, cost_center_id)

    @staticmethod
    def _require_confirmed_member_write(operation: CostCenterDirectoryOperationView) -> None:
        if operation.state != DirectoryOperationState.SUCCEEDED:
            raise DirectoryError(
                operation.error_message or "The directory membership change is not confirmed",
                details={
                    "operationId": operation.id,
                    "state": operation.state,
                    "errorCode": operation.error_code,
                },
            )

    async def group_capabilities(self, actor: Actor) -> CostCenterGroupCapabilities:
        return await self._directory_groups.capabilities(actor)

    async def list_directory_members(
        self,
        actor: Actor,
        cost_center_id: str,
        *,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> CostCenterDirectoryMembers:
        return await self._directory_groups.list_members(
            actor, cost_center_id, page_size=page_size, cursor=cursor
        )

    async def add_directory_member(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        member_kind: PrincipalKind,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperationView:
        return await self._directory_groups.add_member(
            actor, cost_center_id, object_id, member_kind, idempotency_key=idempotency_key
        )

    async def remove_directory_member(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperationView:
        return await self._directory_groups.remove_member(
            actor, cost_center_id, object_id, idempotency_key=idempotency_key
        )

    async def list_directory_operations(
        self, actor: Actor, cost_center_id: str, *, limit: int = 50
    ) -> list[CostCenterDirectoryOperationView]:
        return await self._directory_groups.list_operations(actor, cost_center_id, limit=limit)

    async def get_directory_operation(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperationView:
        return await self._directory_groups.get_operation(actor, cost_center_id, operation_id)

    async def retry_directory_operation(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperationView:
        return await self._directory_groups.retry(actor, cost_center_id, operation_id)

    async def recover_directory_operation(
        self, actor: Actor, cost_center_id: str, operation_id: str
    ) -> CostCenterDirectoryOperationView:
        return await self._directory_groups.recover(actor, cost_center_id, operation_id)

    async def recheck(self, actor: Actor, cost_center_id: str) -> CostCenterManagementView:
        """Check again the grants the cost center's pending rechecks cover, and settle them."""

        await self._stored(actor, cost_center_id)
        revoked = await self._entitlements.resume_rechecks(actor, cost_center_id)
        logger.info(
            "cost_center_rechecked",
            cost_center_id=cost_center_id,
            revoked_grants=len(revoked),
            tenant_id=actor.tenant_id,
        )
        return await self.get_cost_center(actor, cost_center_id)

    async def update_settings(
        self, actor: Actor, request: CostCenterSettingsUpdate
    ) -> CostCenterSettings:
        """Change the default preference without granting membership.

        Preserve the existing durable recheck boundary and finish earlier pending checks after
        releasing the lease. A custom center still requires its verified Entra group.
        """

        recorded: str | None = None
        try:
            async with scope_lease(
                self._gateways,
                actor.tenant_id,
                COST_CENTERS_SCOPE,
                busy_message=COST_CENTERS_BUSY,
            ):
                book = await self.book(actor.tenant_id)
                if book.get(request.default_cost_center_id) is None:
                    raise ValidationError(
                        "No cost center has that ID",
                        details={"defaultCostCenterId": request.default_cost_center_id},
                    )
                chosen = await self._stored(actor, request.default_cost_center_id)
                if not chosen.is_general and chosen.verified_group_object_id is None:
                    raise ValidationError(
                        "A setup-required cost center cannot become the tenant default",
                        details={"reason": "setupRequired", "costCenterId": chosen.id},
                    )
                current = await self._repository.get_settings(actor.tenant_id)
                previous = tenant_default_id(current, actor.tenant_id)
                if previous != request.default_cost_center_id:
                    await self._entitlements.request_recheck(
                        actor, previous, reason="tenantDefaultChanged"
                    )
                    recorded = previous
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
        except Exception:
            if recorded is not None:
                with suppress(Exception):
                    await self._entitlements.resume_rechecks(actor, recorded)
            raise
        revoked = await self._entitlements.resume_rechecks(actor)
        if previous != request.default_cost_center_id:
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
            for item in book.chargeable(
                principal,
                groups,
                group_object_ids=() if actor.groups_overage else actor.group_ids,
            )
        ]
