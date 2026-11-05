import asyncio
from datetime import timedelta

from mosaic_api.cost_center_directory import (
    CostCenterDirectoryOperation,
    DirectoryOperationState,
    cost_center_member_operation_id,
)
from mosaic_api.cost_center_membership_requests import (
    AdminMembershipRequest,
    MembershipApproval,
    MembershipDecision,
    MembershipRequest,
    MembershipRequestCreate,
    PortalCenter,
    PortalCenterPage,
    PortalMembershipRequest,
    SelfServiceSettings,
)
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import (
    AccessRequestState,
    AuditEvent,
    PrincipalKind,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ConflictError,
    DirectoryError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from mosaic_api.integrations.graph import DirectoryLookup
from mosaic_api.integrations.graph.membership import UserMembershipEvidence, parse_object_id
from mosaic_api.repositories.base import CostCenterRepository, EntitlementRepository
from mosaic_api.services.cost_centers import CostCenterService
from mosaic_api.services.directory import Actor
from mosaic_api.services.notification_events import membership_request_event

SAFE_MEMBERSHIP_ERROR = (
    "Membership verification is unavailable. Try again later; membership is unknown."
)


class CostCenterMembershipService:
    def __init__(
        self,
        repository: EntitlementRepository,
        centers: CostCenterRepository,
        actions: CostCenterService,
        directory: DirectoryLookup | None,
    ) -> None:
        self._repository = repository
        self._centers = centers
        self._actions = actions
        self._directory = directory

    async def settings(self, actor: Actor) -> SelfServiceSettings:
        settings = await self._actions.get_settings(actor)
        return SelfServiceSettings(
            creation_requests_enabled=settings.creation_requests_enabled,
            membership_requests_enabled=settings.membership_requests_enabled,
        )

    async def _membership(self, actor: Actor, centers: list[CostCenter]) -> frozenset[str]:
        groups = frozenset(
            center.verified_group_object_id
            for center in centers
            if center.verified_group_object_id is not None
        )
        if not groups:
            return frozenset()
        directory_id = self._actions._directory_groups._directory_id(actor)
        self._actions._directory_groups._management_for(actor)
        if self._directory is None or parse_object_id(actor.object_id) is None:
            raise DirectoryError(SAFE_MEMBERSHIP_ERROR)
        if any(
            center.entra_binding is None or center.entra_binding.directory_tenant_id != directory_id
            for center in centers
            if not center.is_general
        ):
            raise DirectoryError(SAFE_MEMBERSHIP_ERROR)
        started = utc_now()
        async with asyncio.timeout(15):
            evidence = await self._directory.verify_user_memberships(
                actor.object_id,
                sorted(groups),
                timeout_seconds=15,
            )
        if (
            not isinstance(evidence, UserMembershipEvidence)
            or evidence.tenant_id != directory_id
            or evidence.user_object_id != actor.object_id.casefold()
            or evidence.candidate_group_ids != groups
            or not evidence.member_group_ids <= groups
            or evidence.checked_at.tzinfo is None
            or not started - timedelta(seconds=1) <= evidence.checked_at <= utc_now()
        ):
            raise DirectoryError(SAFE_MEMBERSHIP_ERROR)
        return evidence.member_group_ids

    @staticmethod
    def _center_view(
        center: CostCenter,
        memberships: frozenset[str] | None,
        eligible_ids: frozenset[str],
    ) -> PortalCenter:
        group = center.verified_group_object_id
        state = (
            "exempt"
            if center.is_general
            else "unknown"
            if memberships is None or group is None
            else "member"
            if group in memberships
            else "notMember"
        )
        return PortalCenter(
            id=center.id,
            name=center.name,
            code=center.code,
            public_purpose=center.public_purpose,
            built_in=center.is_general,
            membership_state=state,
            token_eligible=center.id in eligible_ids,
        )

    async def list_centers(
        self,
        actor: Actor,
        *,
        mine: bool,
        q: str = "",
        cursor: str | None = None,
        limit: int = 50,
    ) -> PortalCenterPage:
        if not 1 <= limit <= 100 or len(q) > 200:
            raise ValidationError("Use a limit of 1 to 100 and a search of at most 200 characters")
        book = await self._actions.book(actor.tenant_id)
        query = q.strip().casefold()
        candidates = [
            center
            for center in book.by_id.values()
            if (mine or (center.discoverable and not center.is_general))
            and (center.is_general or center.verified_group_object_id is not None)
            and (
                not query
                or query in center.name.casefold()
                or query in center.code.casefold()
                or query in (center.public_purpose or "").casefold()
            )
        ]
        candidates.sort(key=lambda center: center.id)
        error = None
        memberships: frozenset[str] | None = None
        if mine:
            try:
                memberships = await self._membership(actor, candidates)
            except Exception:
                error = SAFE_MEMBERSHIP_ERROR
            candidates = [
                center
                for center in candidates
                if center.is_general
                or (memberships is not None and center.verified_group_object_id in memberships)
            ]
        cursors = {
            self._cursor(actor, center.id, mine=mine, query=query): center.id
            for center in candidates
        }
        if cursor is not None:
            if cursor not in cursors:
                raise ValidationError("The cost center cursor is no longer valid; reload the list")
            candidates = [center for center in candidates if center.id > cursors[cursor]]
        page = candidates[:limit]
        if not mine:
            try:
                memberships = await self._membership(actor, page)
            except Exception:
                error = SAFE_MEMBERSHIP_ERROR
        eligible_ids = frozenset(
            center.id for center in await self._actions.chargeable_for_caller(actor)
        )
        items = [self._center_view(center, memberships, eligible_ids) for center in page]
        return PortalCenterPage(
            items=items,
            next_cursor=self._cursor(actor, page[-1].id, mine=mine, query=query)
            if len(candidates) > limit
            else None,
            membership_error=error,
        )

    @staticmethod
    def _cursor(actor: Actor, center_id: str, *, mine: bool, query: str) -> str:
        return deterministic_id(
            "costCenterPage",
            actor.tenant_id,
            actor.object_id.casefold(),
            str(mine),
            query,
            center_id,
        )

    async def center(self, actor: Actor, center_id: str) -> PortalCenter:
        book = await self._actions.book(actor.tenant_id)
        center = book.get(center_id)
        if center is None:
            raise NotFoundError("Cost center not found")
        try:
            memberships: frozenset[str] | None = await self._membership(actor, [center])
        except Exception:
            memberships = None
        eligible_ids = frozenset(
            item.id for item in await self._actions.chargeable_for_caller(actor)
        )
        view = self._center_view(center, memberships, eligible_ids)
        if not center.discoverable and view.membership_state not in {"member", "exempt"}:
            raise NotFoundError("Cost center not found")
        return view

    async def _target(self, actor: Actor, center_id: str) -> CostCenter:
        directory = self._actions._directory_groups
        center = await directory._bound_center(actor, center_id)
        management = directory._management_for(actor)
        assert center.entra_binding is not None
        group = await management.validate_group(center.entra_binding.group_object_id)
        directory._assert_group(
            group,
            center.entra_binding.directory_tenant_id,
            center.entra_binding.group_object_id,
        )
        return center

    async def submit(
        self, actor: Actor, payload: MembershipRequestCreate
    ) -> PortalMembershipRequest:
        request_id = deterministic_id(
            "costCenterMembershipRequest",
            actor.tenant_id,
            actor.object_id.casefold(),
            str(payload.idempotency_key),
        )
        existing = await self._repository.get_membership_request(actor.tenant_id, request_id)
        if existing is not None:
            if (
                existing.requester_object_id != actor.object_id.casefold()
                or existing.cost_center.id != payload.cost_center_id
                or existing.justification != payload.justification
            ):
                raise ConflictError("This submission key identifies a different membership request")
            return await self._view(existing, administrator=False)
        if not (await self.settings(actor)).membership_requests_enabled:
            raise ConflictError(
                "Cost center membership requests are disabled by administrator policy"
            )
        center = await self._centers.get_cost_center(actor.tenant_id, payload.cost_center_id)
        if center is None or not center.discoverable or center.is_general:
            raise NotFoundError("A published custom cost center was not found")
        center = await self._target(actor, center.id)
        try:
            memberships = await self._membership(actor, [center])
        except Exception as error:
            raise DirectoryError(SAFE_MEMBERSHIP_ERROR) from error
        if center.verified_group_object_id in memberships:
            raise ConflictError("You are already a member of this cost center")
        assert center.entra_binding is not None
        request = MembershipRequest(
            id=request_id,
            tenant_id=actor.tenant_id,
            requester_object_id=actor.object_id,
            cost_center=center.ref(),
            directory_tenant_id=center.entra_binding.directory_tenant_id,
            group_object_id=center.entra_binding.group_object_id,
            justification=payload.justification,
        )
        try:
            async with self._actions._directory_groups._lease(actor):
                settings = await self._centers.get_settings(actor.tenant_id)
                current = await self._centers.get_cost_center(actor.tenant_id, center.id)
                if settings is None or not settings.membership_requests_enabled:
                    raise ConflictError(
                        "Cost center membership requests are disabled by administrator policy"
                    )
                if (
                    current is None
                    or not current.discoverable
                    or current.entra_binding is None
                    or current.entra_binding.identity != center.entra_binding.identity
                    or not current.entra_binding.is_verified
                ):
                    raise ConflictError(
                        "The published cost center changed; reload it before requesting membership"
                    )
                saved = await self._repository.create_membership_request(
                    request,
                    self._audit(actor, request, "submitted"),
                    [membership_request_event(request)],
                )
        except ConflictError:
            existing = await self._repository.get_membership_request(actor.tenant_id, request_id)
            if existing is None:
                raise
            if (
                existing.cost_center.id != request.cost_center.id
                or existing.justification != request.justification
            ):
                raise ConflictError(
                    "This submission key identifies a different membership request"
                ) from None
            saved = existing
        return await self._view(saved, administrator=False)

    async def _request(
        self, actor: Actor, request_id: str, *, mine: bool = False
    ) -> MembershipRequest:
        item = await self._repository.get_membership_request(actor.tenant_id, request_id)
        if item is None or (mine and item.requester_object_id != actor.object_id.casefold()):
            raise NotFoundError("Membership request not found")
        return item

    async def list_requests(
        self,
        actor: Actor,
        *,
        administrator: bool,
        state: AccessRequestState | None = None,
    ) -> list[PortalMembershipRequest]:
        items = await self._repository.list_membership_requests(
            actor.tenant_id,
            requester_object_id=None if administrator else actor.object_id.casefold(),
            state=str(state) if state is not None else None,
        )
        return [await self._view(item, administrator=administrator) for item in items]

    async def get(
        self,
        actor: Actor,
        request_id: str,
        *,
        administrator: bool,
    ) -> PortalMembershipRequest:
        return await self._view(
            await self._request(actor, request_id, mine=not administrator),
            administrator=administrator,
        )

    async def close(
        self,
        actor: Actor,
        request_id: str,
        payload: MembershipDecision,
        *,
        withdraw: bool,
    ) -> PortalMembershipRequest:
        request = await self._request(actor, request_id, mine=withdraw)
        state = AccessRequestState.WITHDRAWN if withdraw else AccessRequestState.DENIED
        if request.state == state:
            return await self._view(request, administrator=not withdraw)
        if request.state != AccessRequestState.PENDING:
            raise ConflictError("Only a pending membership request may be withdrawn or denied")
        updated = request.model_copy(
            update={
                "state": state,
                "decided_at": utc_now(),
                "decided_by_object_id": actor.object_id,
                "decision_note": None if withdraw else payload.note,
                "updated_at": utc_now(),
            }
        )
        saved = await self._repository.save_membership_request(
            updated,
            self._audit(actor, updated, str(state)),
            [membership_request_event(updated)],
        )
        return await self._view(saved, administrator=not withdraw)

    async def approve(
        self,
        actor: Actor,
        request_id: str,
        payload: MembershipApproval,
        *,
        resume: bool = False,
    ) -> AdminMembershipRequest:
        if not payload.acknowledge_membership_changes:
            raise ValidationError("Acknowledge the real group membership impact")
        request = await self._request(actor, request_id)
        if request.state not in {AccessRequestState.PENDING, AccessRequestState.APPROVED}:
            raise ConflictError("This membership request has already been closed")
        if resume and request.state != AccessRequestState.APPROVED:
            raise ConflictError("Only an approved membership request may be resumed")
        if request.state == AccessRequestState.PENDING:
            await self._verify_request_target(actor, request)
            updated = request.model_copy(
                update={
                    "state": AccessRequestState.APPROVED,
                    "decided_at": utc_now(),
                    "decided_by_object_id": actor.object_id,
                    "decision_note": payload.note,
                    "membership_state": DirectoryOperationState.PENDING,
                    "updated_at": utc_now(),
                }
            )
            try:
                request = await self._repository.save_membership_request(
                    updated,
                    self._audit(actor, updated, "approved"),
                    [membership_request_event(updated)],
                )
            except ConflictError:
                request = await self._request(actor, request_id)
                if request.state != AccessRequestState.APPROVED:
                    raise
        if request.membership_state != DirectoryOperationState.SUCCEEDED:
            request = await self._finish(actor, request, resume=resume)
        view = await self._view(request, administrator=True)
        assert isinstance(view, AdminMembershipRequest)
        return view

    async def _verify_request_target(
        self,
        actor: Actor,
        request: MembershipRequest,
    ) -> tuple[CostCenter, frozenset[str]]:
        center = await self._target(actor, request.cost_center.id)
        assert center.entra_binding is not None
        if center.entra_binding.identity != (request.directory_tenant_id, request.group_object_id):
            raise ConflictError("The requested cost center's directory binding changed")
        if self._directory is None:
            raise DirectoryError("Directory verification is unavailable")
        async with asyncio.timeout(15):
            user = await self._directory.get_object(request.requester_object_id)
        if (
            user is None
            or user.object_id != request.requester_object_id
            or user.kind != PrincipalKind.USER
        ):
            raise ValidationError("The requester is not the recorded ordinary directory user")
        memberships = await self._membership(
            Actor(object_id=request.requester_object_id, tenant_id=request.tenant_id),
            [center],
        )
        return center, memberships

    @staticmethod
    def _key(request: MembershipRequest) -> str:
        return "membershipRequest:" + request.id

    def _operation_id(self, request: MembershipRequest) -> str:
        return cost_center_member_operation_id(
            request.tenant_id, request.cost_center.id, self._key(request)
        )

    async def _operation(self, request: MembershipRequest) -> CostCenterDirectoryOperation | None:
        operation = await self._centers.get_directory_operation(
            request.tenant_id, self._operation_id(request)
        )
        if operation is not None and (
            operation.cost_center_id != request.cost_center.id
            or operation.kind != "addMember"
            or operation.directory_tenant_id != request.directory_tenant_id
            or operation.group_object_id != request.group_object_id
            or operation.member_object_id != request.requester_object_id
            or operation.member_kind != PrincipalKind.USER
        ):
            raise ConflictError(
                "The linked membership operation does not match the approved request"
            )
        return operation

    async def _finish(
        self,
        actor: Actor,
        request: MembershipRequest,
        *,
        resume: bool,
    ) -> MembershipRequest:
        changes: dict[str, object]
        try:
            center, memberships = await self._verify_request_target(actor, request)
            operation = await self._operation(request)
            if operation is None and request.operation_id is not None:
                changes = {
                    "membership_state": DirectoryOperationState.UNKNOWN,
                    "membership_error": (
                        "The linked directory operation is unavailable. Restore or reconcile "
                        "that operation before proceeding; do not start another directory write."
                    ),
                }
            elif operation is None:
                if request.group_object_id in memberships:
                    changes = {
                        "membership_state": DirectoryOperationState.SUCCEEDED,
                        "membership_error": None,
                    }
                else:
                    await self._actions.add_directory_member(
                        actor,
                        center.id,
                        request.requester_object_id,
                        PrincipalKind.USER,
                        idempotency_key=self._key(request),
                    )
                    operation = await self._operation(request)
                    if operation is None:
                        raise DirectoryError(
                            "The approved directory operation was not recorded. "
                            "Resume this request."
                        )
                    changes = self._outcome(operation)
            else:
                if resume and operation.state in {
                    DirectoryOperationState.PENDING,
                    DirectoryOperationState.UNKNOWN,
                }:
                    await self._actions.recover_directory_operation(actor, center.id, operation.id)
                    operation = await self._operation(request)
                    assert operation is not None
                changes = self._outcome(operation)
            if (
                operation is not None
                and resume
                and operation.state == DirectoryOperationState.FAILED
            ):
                if request.group_object_id in memberships:
                    changes.update(
                        {
                            "membership_state": DirectoryOperationState.SUCCEEDED,
                            "membership_error": None,
                        }
                    )
        except Exception as error:
            # The durable operation is authoritative after any execution/persistence failure.
            try:
                operation = await self._operation(request)
            except DomainError:
                operation = None
            changes = (
                self._outcome(operation)
                if operation
                else {
                    "membership_state": DirectoryOperationState.FAILED,
                    "membership_error": (
                        str(error)[:1800]
                        if isinstance(error, DomainError)
                        else "Membership setup could not finish. Resume this approved request."
                    ),
                }
            )
            if operation is not None and operation.state != DirectoryOperationState.SUCCEEDED:
                changes["membership_error"] = operation.error_message or (
                    error.summary[:1800]
                    if isinstance(error, DomainError)
                    else "Membership setup could not finish. Reconcile the linked operation."
                )
        for _ in range(5):
            current = await self._request(actor, request.id)
            if current.membership_state == DirectoryOperationState.SUCCEEDED:
                return current
            if all(getattr(current, key) == value for key, value in changes.items()):
                return current
            updated = current.model_copy(update={**changes, "updated_at": utc_now()})
            try:
                return await self._repository.save_membership_request(
                    updated,
                    self._audit(actor, updated, "membershipOutcome"),
                    [membership_request_event(updated)],
                )
            except ConflictError:
                continue
        raise ConflictError(
            "Membership outcome changed concurrently. Reload this approved request."
        )

    @staticmethod
    def _outcome(operation: CostCenterDirectoryOperation) -> dict[str, object]:
        return {
            "operation_id": operation.id,
            "membership_state": operation.state,
            "membership_error": operation.error_message,
        }

    async def _view(
        self,
        request: MembershipRequest,
        *,
        administrator: bool,
    ) -> PortalMembershipRequest:
        state = request.membership_state
        error = request.membership_error
        operation_id = request.operation_id
        if (
            request.state == AccessRequestState.APPROVED
            and state != DirectoryOperationState.SUCCEEDED
        ):
            try:
                operation = await self._operation(request)
            except DomainError as mismatch:
                operation = None
                error = mismatch.summary
                state = DirectoryOperationState.FAILED
            if operation:
                state = operation.state
                error = operation.error_message or request.membership_error
                operation_id = operation.id
            elif request.operation_id is not None:
                state = DirectoryOperationState.UNKNOWN
                error = (
                    "The linked directory operation is unavailable. Restore or reconcile it; "
                    "do not start another directory write."
                )
            elif error is None:
                error = "Approval is saved, but no directory operation is recorded. "
                "Resume this request."
        values = {
            **request.model_dump(by_alias=False),
            "membership_state": state,
            "needs_administrator_attention": request.state == AccessRequestState.APPROVED
            and (
                state != DirectoryOperationState.SUCCEEDED
                or request.membership_state != DirectoryOperationState.SUCCEEDED
            ),
        }
        if (
            state == DirectoryOperationState.SUCCEEDED
            and request.membership_state != DirectoryOperationState.SUCCEEDED
        ):
            error = (
                "Membership is confirmed, but request completion must be recorded. "
                "Resume this request."
            )
        if administrator:
            return AdminMembershipRequest.model_validate(
                {
                    **{
                        key: value
                        for key, value in values.items()
                        if key in AdminMembershipRequest.model_fields
                    },
                    "membership_error": error,
                    "operation_id": operation_id,
                }
            )
        allowed = PortalMembershipRequest.model_fields
        return PortalMembershipRequest.model_validate(
            {key: value for key, value in values.items() if key in allowed}
        )

    @staticmethod
    def _audit(actor: Actor, request: MembershipRequest, action: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            actor_object_id=actor.object_id,
            action="costCenterMembershipRequest." + action,
            resource_type="costCenterMembershipRequest",
            resource_id=request.id,
            details={"costCenterId": request.cost_center.id, "state": request.state},
        )
