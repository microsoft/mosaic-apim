from collections.abc import Awaitable, Callable
from typing import Protocol

import structlog

from mosaic_api.cost_center_directory import (
    CostCenterDirectoryOperation,
    CostCenterSetupState,
    DirectoryOperationState,
    cost_center_member_operation_id,
)
from mosaic_api.cost_center_requests import (
    AdminCostCenterRequest,
    CostCenterRequest,
    CostCenterRequestApproval,
    CostCenterRequestApprovalIntent,
    CostCenterRequestCreate,
    CostCenterRequestDecision,
    PortalCostCenterRequest,
    RequestCounts,
    requested_cost_center_id,
)
from mosaic_api.cost_centers import CostCenter, CostCenterCreate, CostCenterUpdate, default_settings
from mosaic_api.domain import (
    AccessRequestState,
    AuditEvent,
    PrincipalKind,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import (
    ChangeNotRecordedError,
    ConflictError,
    DomainError,
    NotFoundError,
)
from mosaic_api.repositories.base import CostCenterRepository, EntitlementRepository
from mosaic_api.services.directory import Actor
from mosaic_api.services.notification_events import cost_center_request_event

logger = structlog.get_logger()


class RequestedCostCenters(Protocol):
    async def create(
        self, actor: Actor, payload: CostCenterCreate, *, cost_center_id: str | None = None
    ) -> object: ...

    async def update_cost_center(
        self, actor: Actor, cost_center_id: str, payload: CostCenterUpdate
    ) -> object: ...

    async def add_directory_member(
        self,
        actor: Actor,
        cost_center_id: str,
        object_id: str,
        member_kind: PrincipalKind,
        *,
        idempotency_key: str,
    ) -> CostCenterDirectoryOperation: ...


class CostCenterRequestService:
    def __init__(
        self,
        repository: EntitlementRepository,
        cost_centers: CostCenterRepository,
        center_actions: RequestedCostCenters,
        *,
        validate_access: Callable[[Actor, CostCenter], Awaitable[None]],
    ) -> None:
        self._repository = repository
        self._cost_centers = cost_centers
        self._actions = center_actions
        self._validate_access = validate_access

    async def list_admin(
        self, actor: Actor, *, state: AccessRequestState | None = None
    ) -> list[AdminCostCenterRequest]:
        requests = await self._repository.list_cost_center_requests(
            actor.tenant_id, state=str(state) if state is not None else None
        )
        centers = {
            item.id: item for item in await self._cost_centers.list_cost_centers(actor.tenant_id)
        }
        return [self._admin_view(item, centers.get(item.cost_center_id or "")) for item in requests]

    async def get_admin(self, actor: Actor, request_id: str) -> AdminCostCenterRequest:
        request = await self._require_request(actor, request_id)
        return self._admin_view(request, await self._linked_center(request))

    async def list_mine(self, actor: Actor) -> list[PortalCostCenterRequest]:
        requests = await self._repository.list_cost_center_requests(
            actor.tenant_id, requester_object_id=actor.object_id.casefold()
        )
        centers = {
            item.id: item for item in await self._cost_centers.list_cost_centers(actor.tenant_id)
        }
        return [
            self._portal_view(item, centers.get(item.cost_center_id or "")) for item in requests
        ]

    async def get_mine(self, actor: Actor, request_id: str) -> PortalCostCenterRequest:
        request = await self._require_mine(actor, request_id)
        return self._portal_view(request, await self._linked_center(request))

    async def counts(self, actor: Actor, *, administrator: bool) -> RequestCounts:
        resource_requests = await self._repository.list_access_requests(
            actor.tenant_id,
            requester_object_id=None if administrator else actor.object_id,
            state="pending",
        )
        center_requests = await self._repository.list_cost_center_requests(
            actor.tenant_id,
            requester_object_id=None if administrator else actor.object_id.casefold(),
            state="pending",
        )
        membership_requests = await self._repository.list_membership_requests(
            actor.tenant_id,
            requester_object_id=None if administrator else actor.object_id.casefold(),
        )
        membership_count = sum(item.active for item in membership_requests)
        return RequestCounts(
            pending_resource_count=len(resource_requests),
            pending_cost_center_count=len(center_requests),
            pending_membership_count=membership_count,
            pending_count=len(resource_requests) + len(center_requests) + membership_count,
        )

    async def submit(
        self, actor: Actor, payload: CostCenterRequestCreate
    ) -> PortalCostCenterRequest:
        settings = await self._cost_centers.get_settings(actor.tenant_id) or default_settings(
            actor.tenant_id
        )
        if not settings.creation_requests_enabled:
            raise ConflictError(
                "Cost center creation requests are disabled by administrator policy"
            )
        request = CostCenterRequest(
            id=(
                deterministic_id(
                    "costCenterRequest",
                    actor.tenant_id,
                    actor.object_id.casefold(),
                    str(payload.idempotency_key),
                )
                if payload.idempotency_key is not None
                else new_id("costCenterRequest")
            ),
            tenant_id=actor.tenant_id,
            requester_object_id=actor.object_id.casefold(),
            name=payload.name,
            code=payload.code,
            description=payload.description,
            justification=payload.justification,
            suggested_group_object_id=payload.suggested_group_object_id,
        )
        try:
            saved = await self._repository.create_cost_center_request(
                request,
                self._audit(actor, request, "submitted"),
                [cost_center_request_event(request)],
            )
        except ConflictError:
            existing = await self._require_mine(actor, request.id)
            if self._proposal(existing) != self._proposal(request):
                raise ConflictError(
                    "This submission key already identifies a different cost center request"
                ) from None
            saved = existing
        return self._portal_view(saved, await self._linked_center(saved))

    async def approve(
        self, actor: Actor, request_id: str, payload: CostCenterRequestApproval
    ) -> AdminCostCenterRequest:
        request = await self._require_request(actor, request_id)
        self._require_approvable(request, payload)
        if request.approval is None:
            center_id = requested_cost_center_id(actor.tenant_id, request.id)
            proposed = self._proposed_center(actor, center_id, payload.cost_center)
            for existing in await self._cost_centers.list_cost_centers(actor.tenant_id):
                if (
                    existing.id != center_id
                    and existing.code.casefold() == proposed.code.casefold()
                ):
                    raise ConflictError("A cost center already uses the reviewed code")
            await self._validate_access(actor, proposed)
            claimed = request.model_copy(
                update={
                    "approval": CostCenterRequestApprovalIntent(
                        **payload.model_dump(by_alias=False),
                        cost_center_id=center_id,
                        decided_by_object_id=actor.object_id,
                    ),
                    "updated_at": utc_now(),
                }
            )
            try:
                request = await self._repository.save_cost_center_request(
                    claimed, self._audit(actor, claimed, "preparationStarted"), []
                )
            except ConflictError:
                request = await self._require_request(actor, request_id)
                self._require_approvable(request, payload)
                if request.approval is None:
                    raise

        assert request.approval is not None
        center = await self._linked_center(request)
        if request.state == AccessRequestState.PENDING:
            if center is None:
                # Directory work starts only after the request's decision commits.
                local_only = request.approval.cost_center.model_copy(update={"group_setup": None})
                try:
                    await self._actions.create(
                        actor, local_only, cost_center_id=request.approval.cost_center_id
                    )
                except DomainError as error:
                    await self._record_preparation_error(actor, request, error)
                    error.details = {
                        **error.details,
                        "requestId": request.id,
                        "costCenterId": request.approval.cost_center_id,
                    }
                    raise
                center = await self._linked_center(request)
                if center is None:
                    raise ChangeNotRecordedError(
                        "Approval preparation did not produce its reserved cost center. "
                        "Reload and resume this request; do not create another center.",
                        details={"requestId": request.id, "costCenterId": request.cost_center_id},
                    )
            request = await self._commit_approval(actor, request.id, payload)
        request = await self._finish_setup(actor, request)
        return self._admin_view(request, await self._linked_center(request))

    async def deny(
        self, actor: Actor, request_id: str, payload: CostCenterRequestDecision
    ) -> AdminCostCenterRequest:
        request = await self._require_request(actor, request_id)
        if request.state == AccessRequestState.DENIED:
            return self._admin_view(request, await self._linked_center(request))
        if request.state != AccessRequestState.PENDING:
            raise ConflictError("Only a pending cost center request can be declined")
        denied = request.model_copy(
            update={
                "state": AccessRequestState.DENIED,
                "decided_by_object_id": actor.object_id,
                "decided_at": utc_now(),
                "decision_note": payload.note,
                "updated_at": utc_now(),
            }
        )
        saved = await self._repository.save_cost_center_request(
            denied, self._audit(actor, denied, "denied"), [cost_center_request_event(denied)]
        )
        return self._admin_view(saved, await self._linked_center(saved))

    async def withdraw(self, actor: Actor, request_id: str) -> PortalCostCenterRequest:
        request = await self._require_mine(actor, request_id)
        if request.state == AccessRequestState.WITHDRAWN:
            return self._portal_view(request, await self._linked_center(request))
        if request.state != AccessRequestState.PENDING:
            raise ConflictError("Only a pending cost center request can be withdrawn")
        withdrawn = request.model_copy(
            update={
                "state": AccessRequestState.WITHDRAWN,
                "decided_by_object_id": actor.object_id,
                "decided_at": utc_now(),
                "updated_at": utc_now(),
            }
        )
        saved = await self._repository.save_cost_center_request(
            withdrawn,
            self._audit(actor, withdrawn, "withdrawn"),
            [cost_center_request_event(withdrawn)],
        )
        return self._portal_view(saved, await self._linked_center(saved))

    async def _commit_approval(
        self, actor: Actor, request_id: str, payload: CostCenterRequestApproval
    ) -> CostCenterRequest:
        for _attempt in range(3):
            current = await self._require_request(actor, request_id)
            self._require_approvable(current, payload)
            if current.state == AccessRequestState.APPROVED:
                return current
            if current.approval is None:
                raise ConflictError("The request's approval preparation changed; reload it")
            approved = current.model_copy(
                update={
                    "state": AccessRequestState.APPROVED,
                    "decided_by_object_id": current.approval.decided_by_object_id,
                    "decided_at": utc_now(),
                    "decision_note": current.approval.note,
                    "preparation_error": None,
                    "requester_membership_state": (
                        DirectoryOperationState.PENDING if current.approval.add_requester else None
                    ),
                    "updated_at": utc_now(),
                }
            )
            try:
                return await self._repository.save_cost_center_request(
                    approved,
                    self._audit(actor, approved, "approved"),
                    [cost_center_request_event(approved)],
                )
            except ConflictError:
                continue
        raise ConflictError("The approval changed concurrently; reload and resume this request")

    async def _finish_setup(self, actor: Actor, request: CostCenterRequest) -> CostCenterRequest:
        intent = request.approval
        if intent is None or request.state != AccessRequestState.APPROVED:
            raise ConflictError("Directory setup requires a committed cost center approval")
        center = await self._linked_center(request)
        if center is None:
            raise ChangeNotRecordedError(
                "The approved request's cost center is missing. Do not approve it as a new center.",
                details={"requestId": request.id, "costCenterId": intent.cost_center_id},
            )
        if center.verified_group_object_id is None and intent.cost_center.group_setup is not None:
            try:
                await self._actions.update_cost_center(
                    actor,
                    center.id,
                    CostCenterUpdate(group_setup=intent.cost_center.group_setup),
                )
            except DomainError as error:
                return await self._record_preparation_error(actor, request, error)
            center = await self._linked_center(request)
        if (
            request.preparation_error is not None
            and center is not None
            and (center.setup_state == CostCenterSetupState.READY)
        ):
            request = await self._save_progress(
                actor,
                request.model_copy(update={"preparation_error": None, "updated_at": utc_now()}),
            )
        if (
            not intent.add_requester
            or request.requester_membership_state == DirectoryOperationState.SUCCEEDED
            or center is None
            or center.setup_state != CostCenterSetupState.READY
        ):
            return request
        try:
            operation = await self._actions.add_directory_member(
                actor,
                center.id,
                request.requester_object_id,
                PrincipalKind.USER,
                idempotency_key=self._requester_operation_key(request),
            )
        except DomainError as error:
            logger.warning(
                "cost_center_request_membership_incomplete",
                tenant_id=actor.tenant_id,
                request_id=request.id,
                cost_center_id=center.id,
                error_code=error.code,
            )
            stored_operation = await self._cost_centers.get_directory_operation(
                actor.tenant_id,
                cost_center_member_operation_id(
                    actor.tenant_id, center.id, self._requester_operation_key(request)
                ),
            )
            member_state = (
                stored_operation.state
                if stored_operation is not None
                else DirectoryOperationState.FAILED
            )
            if member_state == DirectoryOperationState.PENDING:
                member_state = DirectoryOperationState.UNKNOWN
            return await self._save_progress(
                actor,
                request.model_copy(
                    update={
                        "requester_membership_state": member_state,
                        "requester_membership_error": (
                            None
                            if member_state == DirectoryOperationState.SUCCEEDED
                            else error.summary[:2000]
                        ),
                        "preparation_error": (
                            error.summary[:2000]
                            if member_state == DirectoryOperationState.SUCCEEDED
                            else request.preparation_error
                        ),
                        "updated_at": utc_now(),
                    }
                ),
            )
        return await self._save_progress(
            actor,
            request.model_copy(
                update={
                    "requester_membership_state": operation.state,
                    "requester_membership_error": operation.error_message,
                    "updated_at": utc_now(),
                }
            ),
        )

    async def _record_preparation_error(
        self, actor: Actor, request: CostCenterRequest, error: DomainError
    ) -> CostCenterRequest:
        logger.warning(
            "cost_center_request_setup_incomplete",
            tenant_id=actor.tenant_id,
            request_id=request.id,
            cost_center_id=request.cost_center_id,
            error_code=error.code,
        )
        current = await self._require_request(actor, request.id)
        return await self._save_progress(
            actor,
            current.model_copy(
                update={"preparation_error": error.summary[:2000], "updated_at": utc_now()}
            ),
        )

    async def _save_progress(self, actor: Actor, request: CostCenterRequest) -> CostCenterRequest:
        return await self._repository.save_cost_center_request(
            request, self._audit(actor, request, "setupUpdated"), []
        )

    async def _require_request(self, actor: Actor, request_id: str) -> CostCenterRequest:
        request = await self._repository.get_cost_center_request(actor.tenant_id, request_id)
        if request is None:
            raise NotFoundError("Cost center request was not found")
        return request

    async def _require_mine(self, actor: Actor, request_id: str) -> CostCenterRequest:
        request = await self._require_request(actor, request_id)
        if request.requester_object_id.casefold() != actor.object_id.casefold():
            raise NotFoundError("Cost center request was not found")
        return request

    async def _linked_center(self, request: CostCenterRequest) -> CostCenter | None:
        return (
            await self._cost_centers.get_cost_center(request.tenant_id, request.cost_center_id)
            if request.cost_center_id is not None
            else None
        )

    @staticmethod
    def _require_approvable(request: CostCenterRequest, payload: CostCenterRequestApproval) -> None:
        if request.state not in {AccessRequestState.PENDING, AccessRequestState.APPROVED}:
            raise ConflictError("This cost center request has already been decided")
        if request.approval is not None and (
            request.approval.cost_center != payload.cost_center
            or request.approval.add_requester != payload.add_requester
            or request.approval.note != payload.note
        ):
            raise ConflictError("Resume the existing approval using its reviewed configuration")

    @staticmethod
    def _proposed_center(actor: Actor, center_id: str, payload: CostCenterCreate) -> CostCenter:
        return CostCenter(
            id=center_id,
            tenant_id=actor.tenant_id,
            name=payload.name,
            code=payload.code,
            description=payload.description,
            owners=payload.owners,
            keys_allowed=payload.keys_allowed,
            resource_access=payload.resource_access,
        )

    @staticmethod
    def _requester_operation_key(request: CostCenterRequest) -> str:
        return f"costCenterRequest:{request.id}:requester"

    @staticmethod
    def _proposal(request: CostCenterRequest) -> tuple[object, ...]:
        return (
            request.name,
            request.code,
            request.description,
            request.justification,
            request.suggested_group_object_id,
        )

    @staticmethod
    def _attention(request: CostCenterRequest, center: CostCenter | None) -> bool:
        if request.state not in {AccessRequestState.PENDING, AccessRequestState.APPROVED}:
            return False
        return bool(
            request.preparation_error
            or (
                request.state == AccessRequestState.APPROVED
                and (center is None or center.setup_state != CostCenterSetupState.READY)
            )
            or request.requester_membership_state
            in {
                DirectoryOperationState.PENDING,
                DirectoryOperationState.FAILED,
                DirectoryOperationState.UNKNOWN,
            }
        )

    @classmethod
    def _admin_view(
        cls, request: CostCenterRequest, center: CostCenter | None
    ) -> AdminCostCenterRequest:
        return AdminCostCenterRequest.model_validate(
            {
                **request.model_dump(by_alias=False),
                "etag": request.etag,
                "cost_center": center.ref() if center is not None else None,
                "setup_state": center.setup_state if center is not None else None,
                "needs_administrator_attention": cls._attention(request, center),
            }
        )

    @classmethod
    def _portal_view(
        cls, request: CostCenterRequest, center: CostCenter | None
    ) -> PortalCostCenterRequest:
        approved_center = center if request.state == AccessRequestState.APPROVED else None
        return PortalCostCenterRequest(
            id=request.id,
            name=request.name,
            code=request.code,
            description=request.description,
            justification=request.justification,
            suggested_group_object_id=request.suggested_group_object_id,
            state=request.state,
            created_at=request.created_at,
            updated_at=request.updated_at,
            decided_at=request.decided_at,
            decision_note=request.decision_note,
            preparation_started=request.approval is not None,
            cost_center=approved_center.ref() if approved_center is not None else None,
            setup_state=approved_center.setup_state if approved_center is not None else None,
            requester_membership_requested=bool(
                request.approval and request.approval.add_requester
            ),
            requester_membership_state=request.requester_membership_state,
            needs_administrator_attention=cls._attention(request, center),
        )

    @staticmethod
    def _audit(actor: Actor, request: CostCenterRequest, action: str) -> AuditEvent:
        return AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            actor_object_id=actor.object_id,
            action=f"costCenterRequest.{action}",
            resource_type="costCenterRequest",
            resource_id=request.id,
            details={"state": str(request.state), "costCenterId": request.cost_center_id},
        )
