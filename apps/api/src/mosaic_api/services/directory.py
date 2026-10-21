import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil
from typing import Protocol
from uuid import UUID

from mosaic_api.cost_centers import (
    COST_CENTERS_BUSY,
    COST_CENTERS_SCOPE,
    CostCenterView,
    RecheckReason,
)
from mosaic_api.domain import (
    AuditEvent,
    DirectoryMemberPage,
    DirectoryObject,
    DirectoryPermissionStatus,
    DirectorySearchKind,
    DirectoryStatus,
    DirectoryVerificationState,
    DirectoryVerificationStatus,
    Group,
    GroupCreate,
    GroupMembership,
    GroupPurpose,
    GroupUpdate,
    Principal,
    PrincipalCreate,
    PrincipalKind,
    PrincipalUpdate,
    deterministic_id,
    general_cost_center_id,
    new_id,
    subject_kind_for,
    utc_now,
)
from mosaic_api.errors import (
    ConflictError,
    DirectoryDisabledError,
    DirectoryError,
    DirectoryForbiddenError,
    DirectoryThrottledError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.integrations.graph import DirectoryLookup, DirectoryPhoto
from mosaic_api.integrations.graph.membership import verification_scope
from mosaic_api.managed_cost_center_groups import group_membership, require_unmanaged_group
from mosaic_api.repositories import (
    CostCenterRepository,
    DirectoryRepository,
    EntitlementRepository,
    GatewayRepository,
)
from mosaic_api.services.model_access import (
    entitlement_publication,
    publication_lock,
    scope_lease,
)


@dataclass(frozen=True)
class Actor:
    object_id: str
    tenant_id: str
    # The caller's Entra security-group object IDs, lowercased, as the validated token listed them.
    # Only meaningful for the caller's own portal requests; empty for local authentication.
    group_ids: frozenset[str] = frozenset()
    # True when the caller is in more groups than a token can list, so ``group_ids`` is incomplete
    # and the gateway can't match any group grant for them.
    groups_overage: bool = False


class CostCenterChecks(Protocol):
    """Revokes grants whose subjects may no longer charge their cost center; see ADR 0022."""

    async def request_recheck(
        self,
        actor: Actor,
        cost_center_id: str,
        *,
        reason: RecheckReason,
        subject_id: str | None = None,
    ) -> None: ...

    async def resume_rechecks(
        self, actor: Actor, cost_center_id: str | None = None
    ) -> list[str]: ...


class CostCenterMemberships(Protocol):
    async def get_cost_center(self, actor: Actor, cost_center_id: str) -> CostCenterView: ...

    async def add_member_with_status(
        self, actor: Actor, cost_center_id: str, principal_id: str
    ) -> tuple[CostCenterView, bool]: ...

    async def remove_member(
        self, actor: Actor, cost_center_id: str, principal_id: str, *, explicit_only: bool = False
    ) -> CostCenterView: ...


def _moves_default(principal: Principal, request: PrincipalUpdate) -> bool:
    """Whether the update may take away the principal's default cost center."""

    fields = request.model_fields_set
    if "default_cost_center_id" in fields and (
        request.default_cost_center_id != principal.default_cost_center_id
    ):
        return True
    return "kind" in fields and request.kind == PrincipalKind.SECURITY_GROUP


class DirectoryService:
    def __init__(
        self,
        repository: DirectoryRepository,
        *,
        gateway_repository: GatewayRepository | None = None,
        entitlement_repository: EntitlementRepository | None = None,
        directory_lookup: DirectoryLookup | None = None,
        group_claims_enabled: bool = True,
        cost_center_repository: CostCenterRepository | None = None,
        cost_center_checks: CostCenterChecks | None = None,
        cost_center_memberships: CostCenterMemberships | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._entitlements = entitlement_repository
        self._directory = directory_lookup
        self._group_claims_enabled = group_claims_enabled
        self._cost_centers = cost_center_repository
        # The EntitlementService, which revokes a principal's grants under a former default
        # cost center it may no longer charge.
        self._cost_center_checks = cost_center_checks
        self._cost_center_memberships = cost_center_memberships
        self._clock = clock
        self._directory_checks: dict[str, DirectoryVerificationStatus] = {}
        self._directory_check_locks: dict[str, asyncio.Lock] = {}

    async def _onboarding_default(
        self, actor: Actor, kind: PrincipalKind, requested: str | None
    ) -> str | None:
        """The default cost center a principal is onboarded with: the one asked for, or the
        tenant's. A security group is never the caller, so it has none."""

        if kind == PrincipalKind.SECURITY_GROUP:
            if requested is not None:
                raise ValidationError(
                    "A security group is never the caller, so it has no default cost center"
                )
            return None
        if self._cost_centers is None:
            return requested
        if requested is not None:
            await self._require_cost_center(actor, requested)
            return requested
        settings = await self._cost_centers.get_settings(actor.tenant_id)
        return settings.default_cost_center_id if settings else general_cost_center_id(
            actor.tenant_id
        )

    async def _require_cost_center(self, actor: Actor, cost_center_id: str) -> None:
        if cost_center_id == general_cost_center_id(actor.tenant_id):
            return
        if self._cost_centers is None or await self._cost_centers.get_cost_center(
            actor.tenant_id, cost_center_id
        ) is None:
            raise ValidationError(
                "No cost center has that ID", details={"defaultCostCenterId": cost_center_id}
            )

    @asynccontextmanager
    async def _principal_mutation(self, actor: Actor, principal_id: str) -> AsyncIterator[None]:
        publications: set[str] = set()
        if self._gateways:
            if self._entitlements:
                for grant in await self._entitlements.list_entitlements(
                    actor.tenant_id, subject_id=principal_id
                ):
                    publication = await entitlement_publication(self._gateways, grant)
                    if publication:
                        publications.add(publication.id)
            for publication in await self._gateways.list_publications(actor.tenant_id):
                if publication.applied_access and any(
                    grant.subject.id == principal_id for grant in publication.applied_access.grants
                ):
                    publications.add(publication.id)
        async with AsyncExitStack() as stack:
            if self._gateways:
                for publication_id in sorted(publications):
                    await stack.enter_async_context(
                        publication_lock(self._gateways, actor.tenant_id, publication_id)
                    )
            yield

    @staticmethod
    def _audit_event(actor: Actor, action: str, resource_type: str, resource_id: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            actor_object_id=actor.object_id,
        )

    async def list_principals(self, actor: Actor) -> list[Principal]:
        return await self._repository.list_principals(actor.tenant_id)

    async def directory_status(
        self, actor: Actor, *, include_permissions: bool = False
    ) -> DirectoryStatus:
        if not self._directory:
            message = "Directory lookup requires Entra authentication. Use manual entry instead."
        elif not self._group_claims_enabled:
            message = (
                "Security-group grants are recorded, but the gateway can't match them until "
                "group claims are configured."
            )
        else:
            message = None
        permission_status_message = None
        granted_permissions: frozenset[str] | None = None
        if self._directory and include_permissions:
            try:
                granted_permissions = await self._directory.application_permissions()
            except (DirectoryError, DirectoryForbiddenError) as exc:
                permission_status_message = str(exc)
        permission_definitions = (
            ("User.ReadBasic.All", "People and agent users", True),
            ("GroupMember.Read.All", "Entra security groups and members", True),
            ("AgentIdentity.Read.All", "Entra agent identities", True),
            ("Application.Read.All", "Applications and managed identities", False),
            ("ProfilePhoto.Read.All", "People and administrator photos", False),
        )
        return DirectoryStatus(
            lookup_enabled=self._directory is not None,
            group_claims_enabled=self._group_claims_enabled,
            permissions=[
                DirectoryPermissionStatus(
                    permission=permission,
                    capability=capability,
                    required=required,
                    granted=(
                        permission in granted_permissions
                        if granted_permissions is not None
                        else None
                    ),
                )
                for permission, capability, required in permission_definitions
            ],
            permission_status_message=permission_status_message,
            message=message,
            membership_check=self._directory_check_status(actor),
        )

    def _directory_check_status(self, actor: Actor) -> DirectoryVerificationStatus:
        if self._directory is None:
            return DirectoryVerificationStatus(state=DirectoryVerificationState.DISABLED)
        status = self._directory_checks.get(
            actor.tenant_id.casefold(), DirectoryVerificationStatus()
        )
        if status.state == DirectoryVerificationState.READY and (
            status.checked_at is None
            or status.fresh_until is None
            or not status.checked_at <= self._clock() < status.fresh_until
        ):
            return status.model_copy(update={"state": DirectoryVerificationState.STALE})
        return status

    async def check_directory_group(
        self, actor: Actor, group_object_id: str
    ) -> DirectoryVerificationStatus:
        if self._directory is None:
            raise DirectoryDisabledError(
                "Directory lookup requires Entra authentication. Use manual entry instead."
            )
        group_id, _ = verification_scope(group_object_id, [group_object_id])
        tenant = actor.tenant_id.casefold()
        previous = self._directory_check_status(actor)
        lock = self._directory_check_locks.setdefault(tenant, asyncio.Lock())
        if lock.locked():
            if previous.group_object_id == group_id:
                return previous
            raise ConflictError("A directory group check is already in progress")
        now = self._clock()
        if previous.retry_at is not None and previous.retry_at > now:
            raise DirectoryThrottledError(
                previous.message or "Microsoft Graph membership verification is throttled",
                retry_after=ceil((previous.retry_at - now).total_seconds()),
            )
        async with lock:
            self._directory_checks[tenant] = DirectoryVerificationStatus(
                state=DirectoryVerificationState.CHECKING,
                group_object_id=group_id,
                checked_at=now,
                last_successful_at=previous.last_successful_at,
                last_successful_group_object_id=previous.last_successful_group_object_id,
            )
            try:
                # Matching the group itself only supplies a bounded diagnostic cohort. The
                # result is discarded, never published as a person's authorization evidence.
                evidence = await self._directory.verify_group_members(group_id, [group_id])
                if (
                    evidence.tenant_id.casefold() != tenant
                    or evidence.group_object_id != group_id
                    or evidence.candidate_object_ids != frozenset({group_id})
                ):
                    raise DirectoryForbiddenError(
                        "Directory verification returned evidence for an unexpected scope"
                    )
            except DirectoryThrottledError as exc:
                self._record_directory_check_failure(
                    tenant, DirectoryVerificationState.THROTTLED, str(exc), exc.retry_after
                )
                raise
            except DirectoryForbiddenError as exc:
                self._record_directory_check_failure(
                    tenant, DirectoryVerificationState.FORBIDDEN, str(exc)
                )
                raise
            except DirectoryError as exc:
                self._record_directory_check_failure(
                    tenant, DirectoryVerificationState.UNAVAILABLE, str(exc)
                )
                raise
            except asyncio.CancelledError:
                self._record_directory_check_failure(
                    tenant, DirectoryVerificationState.CANCELLED, "Directory check was cancelled"
                )
                raise
            else:
                checked_at = min(now, evidence.checked_at)
                self._directory_checks[tenant] = DirectoryVerificationStatus(
                    state=DirectoryVerificationState.READY,
                    group_object_id=group_id,
                    checked_at=checked_at,
                    completed_at=self._clock(),
                    last_successful_at=checked_at,
                    last_successful_group_object_id=group_id,
                    fresh_until=checked_at + timedelta(minutes=5),
                )
            finally:
                if self._directory_checks[tenant].state == DirectoryVerificationState.CHECKING:
                    self._record_directory_check_failure(
                        tenant,
                        DirectoryVerificationState.UNAVAILABLE,
                        "Directory check did not complete",
                    )
        return self._directory_check_status(actor)

    def _record_directory_check_failure(
        self,
        tenant: str,
        state: DirectoryVerificationState,
        message: str,
        retry_after: int | None = None,
    ) -> None:
        current = self._directory_checks[tenant]
        completed_at = self._clock()
        self._directory_checks[tenant] = DirectoryVerificationStatus(
            state=state,
            group_object_id=current.group_object_id,
            checked_at=current.checked_at,
            completed_at=completed_at,
            last_successful_at=current.last_successful_at,
            last_successful_group_object_id=current.last_successful_group_object_id,
            retry_at=(
                completed_at + timedelta(seconds=retry_after)
                if retry_after is not None
                else None
            ),
            message=message,
        )

    async def search_directory(
        self, actor: Actor, kind: DirectorySearchKind, query: str, limit: int
    ) -> list[DirectoryObject]:
        if not self._directory:
            raise DirectoryDisabledError(
                "Directory lookup requires Entra authentication. Use manual entry instead."
            )
        return await self._annotate_directory_objects(
            actor, await self._directory.search(kind, query, limit=limit)
        )

    async def get_directory_object(self, actor: Actor, object_id: str) -> DirectoryObject:
        if not self._directory:
            raise DirectoryDisabledError(
                "Directory lookup requires Entra authentication. Use manual entry instead."
            )
        item = await self._directory.get_object(object_id)
        if not item:
            raise NotFoundError("Directory object was not found", details={"objectId": object_id})
        return (await self._annotate_directory_objects(actor, [item]))[0]

    async def directory_photo(self, actor: Actor, object_id: str) -> DirectoryPhoto:
        if not self._directory:
            raise DirectoryDisabledError("Directory photos require Entra authentication.")
        normalized = _canonical_guid(object_id, "objectId")
        photo = await self._directory.user_photo(actor.tenant_id, normalized)
        if photo is None:
            raise NotFoundError("A person photo was not found")
        return photo

    async def security_group_members(
        self, actor: Actor, principal_id: str, limit: int
    ) -> DirectoryMemberPage:
        principal = await self.get_principal(actor, principal_id)
        if principal.kind != PrincipalKind.SECURITY_GROUP:
            raise ConflictError(
                "Only Entra security-group principals have directory members",
                details={"principalId": principal_id},
            )
        if not self._directory:
            raise DirectoryDisabledError(
                "Directory lookup requires Entra authentication. Use manual entry instead."
            )
        page = await self._directory.group_members(principal.object_id, limit=limit)
        return DirectoryMemberPage(
            group_object_id=page.group_object_id,
            members=await self._annotate_directory_objects(actor, page.members),
            truncated=page.truncated,
        )

    async def get_principal(self, actor: Actor, principal_id: str) -> Principal:
        principal = await self._repository.get_principal(actor.tenant_id, principal_id)
        if not principal:
            raise NotFoundError("Principal was not found", details={"id": principal_id})
        return principal

    async def create_principal(self, actor: Actor, request: PrincipalCreate) -> Principal:
        """Onboard a principal with its default cost center.

        The default is checked as the request arrives, and again as the principal is saved, under
        the lease deleting a cost center holds. A cost center deleted meanwhile can't become its
        default. Microsoft Graph is asked outside the lease.
        """

        kind = request.kind
        requested_default = request.default_cost_center_id
        await self._onboarding_default(actor, kind, requested_default)
        object_id = _normalize_object_id(
            request.object_id,
            request.kind,
            require_guid=(
                self._directory is not None
                or request.kind
                in {
                    PrincipalKind.AGENT_USER,
                    PrincipalKind.AGENT_IDENTITY,
                    PrincipalKind.SECURITY_GROUP,
                }
            ),
        )
        identity_parent_id = request.identity_parent_id
        verified = await self._verify_principal_request(request.kind, object_id)
        if verified:
            request = PrincipalCreate(
                object_id=object_id,
                kind=request.kind,
                label=request.label or verified.display_name,
                identity_parent_id=verified.identity_parent_id,
            )
        elif request.kind == PrincipalKind.AGENT_USER and identity_parent_id:
            identity_parent_id = _canonical_guid(identity_parent_id, "identityParentId")
            request = PrincipalCreate(
                object_id=object_id,
                kind=request.kind,
                label=request.label,
                identity_parent_id=identity_parent_id,
            )
        else:
            request = PrincipalCreate(
                object_id=object_id,
                kind=request.kind,
                label=request.label,
                identity_parent_id=None,
            )
        existing = await self._repository.find_principal_by_object_id(
            actor.tenant_id, request.object_id
        )
        if not existing:
            existing = await self._find_principal_by_object_id_casefold(
                actor.tenant_id, request.object_id
            )
        if existing:
            raise ConflictError(
                "A principal with this Entra object ID already exists",
                details={"objectId": request.object_id, "id": existing.id},
            )
        async with self._cost_centers_lease(actor):
            principal = Principal(
                id=deterministic_id("principal", actor.tenant_id, request.object_id),
                tenant_id=actor.tenant_id,
                **request.model_dump(
                    by_alias=False, exclude={"identity_parent_id", "default_cost_center_id"}
                ),
                detail=verified.detail if verified else None,
                identity_parent_id=(
                    verified.identity_parent_id if verified else request.identity_parent_id
                ),
                blueprint_id=verified.blueprint_id if verified else None,
                directory_verified_at=utc_now() if verified else None,
                default_cost_center_id=await self._onboarding_default(
                    actor, kind, requested_default
                ),
            )
            saved = await self._repository.create_principal(
                principal,
                self._audit_event(actor, "principal.created", "principal", principal.id),
            )
        return saved

    @asynccontextmanager
    async def _cost_centers_lease(self, actor: Actor) -> AsyncIterator[None]:
        """The lease deleting a cost center holds while it checks that it's no one's default."""

        if self._gateways is None or self._cost_centers is None:
            yield
            return
        async with scope_lease(
            self._gateways, actor.tenant_id, COST_CENTERS_SCOPE, busy_message=COST_CENTERS_BUSY
        ):
            yield

    async def update_principal(
        self, actor: Actor, principal_id: str, request: PrincipalUpdate
    ) -> Principal:
        """Update a principal. Moving its default cost center rechecks its grants under the old one.

        They may have charged the old default only because it was theirs. The recheck is recorded
        before the move and runs once the locks are released, with any recheck an earlier attempt
        left pending, so repeating the request finishes one that couldn't. If the update fails, the
        recheck runs anyway and finds nothing changed.
        """

        checks = self._cost_center_checks
        defaults = bool({"default_cost_center_id", "kind"} & request.model_fields_set)
        former: str | None = None
        try:
            async with AsyncExitStack() as stack:
                await stack.enter_async_context(self._principal_mutation(actor, principal_id))
                if defaults:
                    await stack.enter_async_context(self._cost_centers_lease(actor))
                before = await self.get_principal(actor, principal_id)
                if (
                    checks is not None
                    and before.default_cost_center_id is not None
                    and _moves_default(before, request)
                ):
                    former = before.default_cost_center_id
                    await checks.request_recheck(
                        actor, former, reason="defaultChanged", subject_id=before.id
                    )
                saved = await self._update_principal(actor, principal_id, request)
        except Exception:
            if checks is not None and former is not None:
                with suppress(Exception):
                    await checks.resume_rechecks(actor, former)
            raise
        if checks is not None and defaults:
            await checks.resume_rechecks(actor)
        return saved

    async def _update_principal(
        self, actor: Actor, principal_id: str, request: PrincipalUpdate
    ) -> Principal:
        principal = await self.get_principal(actor, principal_id)
        changes = request.model_dump(by_alias=False, exclude_unset=True)
        if "default_cost_center_id" in changes:
            requested = changes["default_cost_center_id"]
            if requested is not None:
                if principal.kind == PrincipalKind.SECURITY_GROUP:
                    raise ValidationError(
                        "A security group is never the caller, so it has no default cost center"
                    )
                await self._require_cost_center(actor, requested)
        if changes.get("kind") not in {None, principal.kind}:
            new_kind = changes["kind"]
            if new_kind == PrincipalKind.SECURITY_GROUP:
                changes["default_cost_center_id"] = None
            if subject_kind_for(new_kind) != subject_kind_for(principal.kind):
                await self._require_no_grants(actor, principal_id, active_only=True)
                await self._require_not_model_caller(actor, principal_id)
            verified = await self._verify_principal_request(new_kind, principal.object_id)
            if verified:
                changes.update(
                    {
                        "detail": verified.detail,
                        "identity_parent_id": verified.identity_parent_id,
                        "blueprint_id": verified.blueprint_id,
                        "directory_verified_at": utc_now(),
                    }
                )
        updated = Principal.model_validate(
            {
                **principal.model_dump(by_alias=False),
                **changes,
                "etag": principal.etag,
                "updated_at": utc_now(),
            }
        )
        saved = await self._repository.save_principal(
            updated,
            self._audit_event(actor, "principal.updated", "principal", updated.id),
        )
        return saved

    async def delete_principal(self, actor: Actor, principal_id: str) -> None:
        async with self._principal_mutation(actor, principal_id):
            await self._delete_principal(actor, principal_id)

    async def _require_no_grants(
        self, actor: Actor, principal_id: str, *, active_only: bool = False
    ) -> None:
        if self._entitlements:
            grants = await self._entitlements.list_entitlements(
                actor.tenant_id, subject_id=principal_id
            )
            if any(grant.enabled or not active_only for grant in grants):
                raise ConflictError(
                    "Remove this principal's grants before deleting it or changing its kind",
                    details={"principalId": principal_id},
                )
        if self._gateways:
            for publication in await self._gateways.list_publications(actor.tenant_id):
                if publication.applied_access and any(
                    grant.subject.id == principal_id
                    and (
                        grant.enabled
                        or publication.created_resources()
                        or publication.access_state in {"applying", "unknown"}
                    )
                    for grant in publication.applied_access.grants
                ):
                    raise ConflictError(
                        "This principal has managed runtime access; unpublish its model first",
                        details={"publicationId": publication.id},
                    )

    async def _require_not_model_caller(self, actor: Actor, principal_id: str) -> None:
        """Refuse while an MCP server names this principal as the application it calls models as.

        Its policy records the principal's object ID for usage attribution (ADR 0025), so the
        setting is cleared on the server first, where the change is planned and applied.
        """

        if not self._gateways:
            return
        naming = sorted(
            publication.id
            for publication in await self._gateways.list_mcp_publications(actor.tenant_id)
            if publication.model_caller_id == principal_id
        )
        if naming:
            raise ConflictError(
                "An MCP server calls models as this principal. Clear Calls models as on the MCP "
                "server first.",
                details={"reason": "mcpModelCaller", "mcpPublicationIds": naming},
            )

    async def _delete_principal(self, actor: Actor, principal_id: str) -> None:
        principal = await self.get_principal(actor, principal_id)
        await self._require_no_grants(actor, principal_id)
        await self._require_not_model_caller(actor, principal_id)
        memberships = await self._repository.list_memberships(
            actor.tenant_id, principal_id=principal_id
        )
        if memberships:
            raise ConflictError(
                "Remove this principal from all groups before deleting it",
                details={"membershipCount": len(memberships)},
            )
        if self._cost_centers is not None:
            listing = sorted(
                item.id
                for item in await self._cost_centers.list_cost_centers(actor.tenant_id)
                if principal_id in item.member_ids()
            )
            if listing:
                # Removing it from each cost center first revokes what relied on it there.
                raise ConflictError(
                    "Remove this principal from its cost centers before deleting it",
                    details={"reason": "costCenterMember", "costCenterIds": listing},
                )
        await self._repository.delete_principal(
            principal,
            self._audit_event(actor, "principal.deleted", "principal", principal_id),
        )

    async def list_groups(self, actor: Actor) -> list[Group]:
        return await self._repository.list_groups(actor.tenant_id)

    async def get_group(self, actor: Actor, group_id: str) -> Group:
        group = await self._repository.get_group(actor.tenant_id, group_id)
        if not group:
            raise NotFoundError("Group was not found", details={"id": group_id})
        return group

    async def create_group(self, actor: Actor, request: GroupCreate) -> Group:
        name = request.name.strip()
        existing = await self._repository.find_group_by_name(actor.tenant_id, name)
        if existing:
            raise ConflictError(
                "A group with this name already exists",
                details={"name": name, "id": existing.id},
            )
        group = Group(
            id=deterministic_id("group", actor.tenant_id, name),
            tenant_id=actor.tenant_id,
            name=name,
            description=request.description,
        )
        saved = await self._repository.create_group(
            group,
            self._audit_event(actor, "group.created", "group", group.id),
        )
        return saved

    async def update_group(self, actor: Actor, group_id: str, request: GroupUpdate) -> Group:
        group = await self.get_group(actor, group_id)
        require_unmanaged_group(group)
        changes = request.model_dump(exclude_unset=True)
        updated = Group.model_validate(
            {
                **group.model_dump(by_alias=False),
                **changes,
                "etag": group.etag,
                "updated_at": utc_now(),
            }
        )
        saved = await self._repository.save_group(
            updated,
            self._audit_event(actor, "group.updated", "group", updated.id),
        )
        return saved

    async def delete_group(self, actor: Actor, group_id: str) -> None:
        group = await self.get_group(actor, group_id)
        require_unmanaged_group(group)
        memberships = await self._repository.list_memberships(actor.tenant_id, group_id=group_id)
        if memberships:
            raise ConflictError(
                "Remove all group members before deleting this group",
                details={"membershipCount": len(memberships)},
            )
        await self._repository.delete_group(
            group,
            self._audit_event(actor, "group.deleted", "group", group_id),
        )

    async def list_memberships(self, actor: Actor, group_id: str) -> list[GroupMembership]:
        group = await self.get_group(actor, group_id)
        if group.purpose == GroupPurpose.COST_CENTER:
            center = await self._managed_center(actor, group)
            return [group_membership(group, member) for member in center.members]
        return await self._repository.list_memberships(actor.tenant_id, group_id=group_id)

    def _membership_service(self) -> CostCenterMemberships:
        if self._cost_center_memberships is None:
            raise ConflictError("The managed cost-center membership service is not configured")
        return self._cost_center_memberships

    async def _managed_center(self, actor: Actor, group: Group) -> CostCenterView:
        if group.purpose != GroupPurpose.COST_CENTER or not group.cost_center_id:
            raise ConflictError("The group does not identify an owning cost center")
        center = await self._membership_service().get_cost_center(actor, group.cost_center_id)
        if center.group_id != group.id:
            raise ConflictError("The cost center does not own this group")
        return center

    async def add_membership(
        self, actor: Actor, group_id: str, principal_id: str
    ) -> tuple[GroupMembership, bool]:
        group = await self.get_group(actor, group_id)
        if group.purpose == GroupPurpose.COST_CENTER:
            center = await self._managed_center(actor, group)
            updated, created = await self._membership_service().add_member_with_status(
                actor, center.id, principal_id
            )
            member = next(
                (item for item in updated.members if item.principal_id == principal_id), None
            )
            if member is None:
                raise ConflictError(
                    "The cost-center membership changed; reload it and try again",
                    details={"costCenterId": center.id, "principalId": principal_id},
                )
            return group_membership(group, member), created
        principal = await self.get_principal(actor, principal_id)
        if principal.kind == PrincipalKind.SECURITY_GROUP:
            raise ValidationError(
                "A MOSAIC group can't contain an Entra security group; grant the security group "
                "directly."
            )
        existing = await self._repository.get_membership(actor.tenant_id, group_id, principal_id)
        if existing:
            return existing, False
        membership = GroupMembership(
            id=deterministic_id("membership", actor.tenant_id, group_id, principal_id),
            tenant_id=actor.tenant_id,
            group_id=group_id,
            principal_id=principal_id,
        )
        try:
            saved = await self._repository.create_membership(
                membership,
                group,
                principal,
                self._audit_event(
                    actor,
                    "group.member_added",
                    "groupMembership",
                    membership.id,
                ),
            )
        except ConflictError:
            existing = await self._repository.get_membership(
                actor.tenant_id, group_id, principal_id
            )
            if existing:
                return existing, False
            raise
        return saved, True

    async def remove_membership(self, actor: Actor, group_id: str, principal_id: str) -> None:
        group = await self._repository.get_group(actor.tenant_id, group_id)
        if group is not None and group.purpose == GroupPurpose.COST_CENTER:
            center = await self._managed_center(actor, group)
            await self._membership_service().remove_member(
                actor, center.id, principal_id, explicit_only=True
            )
            return
        membership = await self._repository.get_membership(actor.tenant_id, group_id, principal_id)
        if not membership:
            raise NotFoundError(
                "Group membership was not found",
                details={"groupId": group_id, "principalId": principal_id},
            )
        await self._repository.delete_membership(
            membership,
            self._audit_event(
                actor,
                "group.member_removed",
                "groupMembership",
                membership.id,
            ),
        )

    async def _annotate_directory_objects(
        self, actor: Actor, objects: list[DirectoryObject]
    ) -> list[DirectoryObject]:
        principals = await self._repository.list_principals(actor.tenant_id)
        by_object_id = {principal.object_id.casefold(): principal.id for principal in principals}
        return [
            item.model_copy(
                update={"principal_id": by_object_id.get(item.object_id.casefold())}
            )
            for item in objects
        ]

    async def _verify_principal_request(
        self, expected_kind: PrincipalKind, object_id: str
    ) -> DirectoryObject | None:
        if not self._directory or expected_kind in {
            PrincipalKind.SERVICE_PRINCIPAL,
            PrincipalKind.MANAGED_IDENTITY,
        }:
            return None
        found = await self._directory.get_object(object_id)
        if not found:
            raise ValidationError(
                "That object was not found in Entra.",
                details={"objectId": object_id},
            )
        if found.kind is not expected_kind:
            raise ValidationError(
                f"That object is a {_kind_label(found.kind)} in Entra; add it as "
                f"{_kind_article(found.kind)} {_kind_label(found.kind)}.",
                details={"objectId": object_id, "actualKind": found.kind},
            )
        return found

    async def _find_principal_by_object_id_casefold(
        self, tenant_id: str, object_id: str
    ) -> Principal | None:
        wanted = object_id.casefold()
        for principal in await self._repository.list_principals(tenant_id):
            if principal.object_id.casefold() == wanted:
                return principal
        return None


def _canonical_guid(value: str, field_name: str) -> str:
    try:
        return str(UUID(value.strip()))
    except ValueError as exc:
        raise ValidationError(f"{field_name} must be a GUID") from exc


def _normalize_object_id(value: str, kind: PrincipalKind, *, require_guid: bool) -> str:
    if require_guid and kind in {
        PrincipalKind.USER,
        PrincipalKind.AGENT_USER,
        PrincipalKind.AGENT_IDENTITY,
        PrincipalKind.SECURITY_GROUP,
    }:
        return _canonical_guid(value, "objectId")
    try:
        return str(UUID(value.strip()))
    except ValueError:
        return value.strip()


def _kind_label(kind: PrincipalKind) -> str:
    return {
        PrincipalKind.USER: "user",
        PrincipalKind.AGENT_USER: "agent user",
        PrincipalKind.AGENT_IDENTITY: "agent identity",
        PrincipalKind.SECURITY_GROUP: "security group",
        PrincipalKind.SERVICE_PRINCIPAL: "service principal",
        PrincipalKind.MANAGED_IDENTITY: "managed identity",
    }[kind]


def _kind_article(kind: PrincipalKind) -> str:
    return "an" if kind in {PrincipalKind.AGENT_USER, PrincipalKind.AGENT_IDENTITY} else "a"
