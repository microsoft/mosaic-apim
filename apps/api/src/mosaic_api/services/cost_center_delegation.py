from mosaic_api.approvals import ApprovalContextRevision
from mosaic_api.cost_center_delegation import (
    ReviewCenter,
    ReviewDecision,
    ReviewDetail,
    ReviewItem,
    ReviewKind,
    ReviewOutcome,
    ReviewPage,
)
from mosaic_api.cost_center_membership_requests import (
    MembershipApproval,
    MembershipDecision,
    MembershipRequest,
)
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestApproval,
    AccessRequestState,
    deterministic_id,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.repositories.base import CostCenterRepository, EntitlementRepository
from mosaic_api.request_forms import snapshot_view
from mosaic_api.services.cost_center_membership_requests import CostCenterMembershipService
from mosaic_api.services.directory import Actor
from mosaic_api.services.entitlements import EntitlementService

ReviewRecord = AccessRequest | MembershipRequest


class CostCenterDelegationService:
    def __init__(
        self,
        centers: CostCenterRepository,
        requests: EntitlementRepository,
        entitlements: EntitlementService,
        membership: CostCenterMembershipService,
    ) -> None:
        self._centers = centers
        self._requests = requests
        self._entitlements = entitlements
        self._membership = membership

    @staticmethod
    def _scope(actor: Actor, center: CostCenter) -> ReviewCenter:
        return ReviewCenter(
            center=center.ref(),
            roles=[p.role for p in center.pocs if p.object_id == actor.object_id.casefold()],
        )

    async def _is_active(self, center: CostCenter) -> bool:
        if center.is_general or not center.etag:
            return False
        if center.poc_creation_request_id is None:
            return True
        origin = await self._requests.get_cost_center_request(
            center.tenant_id,
            center.poc_creation_request_id,
        )
        return bool(
            origin is not None
            and origin.tenant_id == center.tenant_id
            and origin.state == AccessRequestState.APPROVED
            and origin.cost_center_id == center.id
        )

    async def centers(self, actor: Actor) -> list[ReviewCenter]:
        scopes: list[ReviewCenter] = []
        for center in await self._centers.list_cost_centers(actor.tenant_id):
            scope = self._scope(actor, center)
            if (
                center.tenant_id == actor.tenant_id
                and scope.roles
                and await self._is_active(center)
            ):
                scopes.append(scope)
        return scopes

    async def _assigned(self, actor: Actor, center_id: str | None) -> CostCenter:
        center = (
            await self._centers.get_cost_center(actor.tenant_id, center_id) if center_id else None
        )
        if (
            center is None
            or center.tenant_id != actor.tenant_id
            or not self._scope(actor, center).roles
            or not await self._is_active(center)
        ):
            raise NotFoundError("Approval not found")
        return center

    @staticmethod
    def _item(record: ReviewRecord) -> ReviewItem:
        if isinstance(record, AccessRequest):
            return ReviewItem(
                id=record.id,
                kind="resourceAccess",
                title=(record.resource_snapshot and record.resource_snapshot.display_name)
                or "Resource access",
                requester=record.requester_name or record.requester_username,
                state=record.state,
                created_at=record.created_at,
            )
        return ReviewItem(
            id=record.id,
            kind="costCenterMembership",
            title="Cost center membership",
            requester=record.requester_name,
            state=record.state,
            created_at=record.created_at,
        )

    async def queue(
        self,
        actor: Actor,
        center_id: str,
        state: AccessRequestState,
        cursor: str | None,
        limit: int,
    ) -> ReviewPage:
        await self._assigned(actor, center_id)
        access = await self._requests.list_access_requests(
            actor.tenant_id,
            cost_center_id=center_id,
            state=str(state),
        )
        memberships = await self._requests.list_membership_requests(
            actor.tenant_id,
            cost_center_id=center_id,
            state=str(state),
        )
        await self._assigned(actor, center_id)
        records: list[ReviewRecord] = [*access, *memberships]
        items = sorted(
            (
                self._item(record)
                for record in records
                if record.requester_object_id.casefold() != actor.object_id.casefold()
            ),
            key=lambda item: (item.created_at, item.kind, item.id),
            reverse=True,
        )

        def cursor_for(item: ReviewItem) -> str:
            return deterministic_id(
                "reviewPage",
                actor.tenant_id,
                actor.object_id,
                center_id,
                str(state),
                item.kind,
                item.id,
            )

        if cursor is not None:
            index = next((i for i, item in enumerate(items) if cursor_for(item) == cursor), None)
            if index is None:
                raise ConflictError("The approval queue changed; reload its first page")
            items = items[index + 1 :]
        page = items[:limit]
        return ReviewPage(
            items=page, next_cursor=cursor_for(page[-1]) if len(items) > limit else None
        )

    async def _load(
        self,
        actor: Actor,
        kind: ReviewKind,
        request_id: str,
    ) -> tuple[ReviewRecord, CostCenter]:
        center_ids = frozenset(scope.center.id for scope in await self.centers(actor))
        if not center_ids:
            raise NotFoundError("Approval not found")
        record: ReviewRecord | None
        if kind == "resourceAccess":
            record = await self._requests.get_access_request(
                actor.tenant_id, request_id, review_center_ids=center_ids,
            )
        else:
            record = await self._requests.get_membership_request(
                actor.tenant_id, request_id, review_center_ids=center_ids,
            )
        if record is None or record.tenant_id != actor.tenant_id:
            raise NotFoundError("Approval not found")
        if record.requester_object_id.casefold() == actor.object_id.casefold():
            raise NotFoundError("Approval not found")
        center_id = (
            record.cost_center_id if isinstance(record, AccessRequest) else record.cost_center.id
        )
        return record, await self._assigned(actor, center_id)

    async def _detail(
        self,
        actor: Actor,
        record: ReviewRecord,
        center: CostCenter,
    ) -> tuple[ReviewDetail, ApprovalContextRevision | None]:
        resource = isinstance(record, AccessRequest)
        preview = (
            await self._entitlements.preview_access_request(
                actor,
                record.id,
                AccessRequestApproval(cost_center_id=center.id),
            )
            if resource and record.state == AccessRequestState.PENDING
            else None
        )
        # A concurrent decision can land while context is being computed. Never give a token
        # pairing the previous request/assignment with a newly read context.
        if preview and (
            preview.revision.request_etag != record.etag
            or preview.revision.cost_center_etag != center.etag
        ):
            raise ConflictError("The approval changed; reload its details")
        fresh_center = await self._assigned(actor, center.id)
        if fresh_center.etag != center.etag:
            raise ConflictError("The cost center changed; reload the approval")
        item = self._item(record)
        limit = center.limit_for(record.resource) if isinstance(record, AccessRequest) else None
        membership_state = (
            record.membership_state if isinstance(record, MembershipRequest) else None
        )
        return ReviewDetail(
            **item.model_dump(by_alias=False),
            scope=self._scope(actor, center),
            environment=record.requested_environment if isinstance(record, AccessRequest) else None,
            justification=record.justification,
            recorded_form=snapshot_view(record.request_form)
            if isinstance(record, AccessRequest)
            else None,
            has_person_limits=bool(limit and limit.person) if resource else None,
            can_decide=bool(record.etag and record.state == AccessRequestState.PENDING),
            approval_blocked=(
                center.verified_group_object_id != record.group_object_id
                if isinstance(record, MembershipRequest)
                else bool(preview and preview.blockers)
            ),
            decision_at=record.decided_at,
            decision_reason=record.decision_note,
            decided_by_you=(
                record.decided_by_object_id is not None
                and record.decided_by_object_id.casefold() == actor.object_id.casefold()
            ),
            membership_state=membership_state,
            revision=deterministic_id(
                "review",
                actor.tenant_id,
                actor.object_id,
                center.id,
                item.kind,
                record.id,
                record.etag or "",
                center.etag or "",
                preview.revision.context_revision if preview else "",
            ),
        ), preview.revision if preview else None

    async def detail(self, actor: Actor, kind: ReviewKind, request_id: str) -> ReviewDetail:
        record, center = await self._load(actor, kind, request_id)
        detail, _ = await self._detail(actor, record, center)
        return detail

    async def decide(
        self,
        actor: Actor,
        kind: ReviewKind,
        request_id: str,
        outcome: ReviewOutcome,
        decision: ReviewDecision,
    ) -> ReviewDetail:
        record, center = await self._load(actor, kind, request_id)
        final = AccessRequestState.APPROVED if outcome == "approve" else AccessRequestState.DENIED
        if record.state == final:
            result, _ = await self._detail(actor, record, center)
            return result
        detail, context = await self._detail(actor, record, center)
        if not detail.can_decide or decision.revision != detail.revision:
            raise ConflictError("The approval changed; reload its details before deciding")
        if outcome == "approve" and detail.approval_blocked:
            raise ConflictError(
                "This request cannot be approved here. Ask an administrator to review it."
            )
        if kind == "resourceAccess":
            if outcome == "approve":
                await self._entitlements.approve_access_request(
                    actor,
                    request_id,
                    AccessRequestApproval(cost_center_id=center.id, note=decision.reason),
                    center_guard=center,
                    expected_context=context,
                )
            else:
                await self._entitlements.decide_access_request(
                    actor,
                    request_id,
                    state=final,
                    note=decision.reason,
                    center_guard=center,
                    reviewed_etag=record.etag,
                )
        elif outcome == "approve":
            await self._membership.approve(
                actor,
                request_id,
                MembershipApproval(
                    note=decision.reason,
                    acknowledge_membership_changes=decision.acknowledge_membership_changes,
                ),
                center_guard=center,
                reviewed_etag=record.etag,
            )
        else:
            await self._membership.close(
                actor,
                request_id,
                MembershipDecision(note=decision.reason),
                withdraw=False,
                center_guard=center,
                reviewed_etag=record.etag,
            )
        return await self.detail(actor, kind, request_id)
