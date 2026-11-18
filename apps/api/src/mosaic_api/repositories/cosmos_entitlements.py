from collections.abc import Sequence
from typing import Any, TypeVar

from azure.cosmos import exceptions

from mosaic_api.approvals import ApprovalDecisionProvenance, ApprovalPolicy
from mosaic_api.cost_center_delegation import (
    ReviewItem,
    ReviewKind,
    ReviewPosition,
    review_time_key,
)
from mosaic_api.cost_center_membership_requests import (
    MembershipRequest,
    MembershipRequestGuard,
    membership_guard_id,
)
from mosaic_api.cost_center_requests import CostCenterRequest
from mosaic_api.cost_centers import CostCenter
from mosaic_api.domain import (
    AccessRequest,
    AccessRequestState,
    AuditEvent,
    Entitlement,
    Principal,
    RequestFormHead,
    RequestFormRevision,
)
from mosaic_api.errors import ChangeNotRecordedError, ConflictError
from mosaic_api.notifications import NotificationEvent, NotificationEventType
from mosaic_api.repositories.access_request_writes import (
    form_head_id,
    form_revision_id,
    require_human_form_review,
    require_unchanged_form,
)
from mosaic_api.repositories.cosmos import BatchOperation, CosmosRepositoryBase
from mosaic_api.repositories.cost_center_request_writes import (
    REQUEST_CHANGED,
    require_request_change,
    validate_request_write,
)
from mosaic_api.repositories.membership_request_writes import (
    MEMBERSHIP_CHANGED,
    validate_membership_write,
)
from mosaic_api.services.approval_events import grant_event
from mosaic_api.services.notification_events import access_request_event

RequestT = TypeVar("RequestT", AccessRequest, CostCenterRequest, MembershipRequest)


class CosmosEntitlementRepository(CosmosRepositoryBase):
    """Entitlements and access requests in the ``desired-state`` container.

    Entitlement IDs are deterministic on subject and resource, so ``create`` conflicts are how
    a duplicate grant is detected rather than a separate uniqueness query.
    """

    def _center_guard(
        self, center: CostCenter | None, tenant_id: str, center_id: str | None,
    ) -> list[BatchOperation]:
        if center is None:
            return []
        if center.tenant_id != tenant_id or center.id != center_id or not center.etag:
            raise ValueError("The delegated decision requires its exact center version")
        return [self._replace_operation(center)]

    async def list_review_items(
        self, tenant_id: str, center_id: str, reviewer_id: str, state: str, kind: ReviewKind,
        *, before: ReviewPosition | None, limit: int,
    ) -> list[ReviewItem]:
        resource = kind == "resourceAccess"
        center = "c.costCenterId" if resource else "c.costCenter.id"
        fields = "c.id, c.state, c.createdAt, c.requesterName"
        if resource:
            fields += ", c.requesterUsername, c.resourceSnapshot.displayName AS resourceName"
        common = (
            f"{fields} FROM c WHERE c.tenantId = @tenant AND c.entityType = @type "
            f"AND {center} = @center AND c.state = @state "
            "AND NOT STRINGEQUALS(c.requesterObjectId, @reviewer, true)"
        )
        scope: dict[str, Any] = {
            "@tenant": tenant_id, "@type": "accessRequest" if resource
            else "costCenterMembershipRequest",
            "@center": center_id, "@state": state, "@reviewer": reviewer_id,
        }

        async def read(
            count: int, condition: str, order: str, **bounds: str,
        ) -> list[dict[str, Any]]:
            parameters = {**scope, "@limit": count, **{f"@{k}": v for k, v in bounds.items()}}
            return [row async for row in self._desired.query_items(
                query=f"SELECT TOP @limit {common}{condition} ORDER BY c.{order} DESC",
                parameters=[{"name": name, "value": value} for name, value in parameters.items()],
                partition_key=tenant_id,
            )]

        # Use the existing single-field indexes. Limit timestamp ties separately by id so
        # boundary rows are deterministic without requiring a new composite index.
        rows: list[dict[str, Any]] = []
        prior: dict[str, str] = {}
        if before is not None:
            moment = review_time_key(before.created_at)
            if kind < before.kind:
                rows = await read(limit, " AND c.createdAt = @at", "id", at=moment)
            elif kind == before.kind:
                rows = await read(
                    limit, " AND c.createdAt = @at AND c.id < @id", "id", at=moment, id=before.id,
                )
            prior["at"] = moment
        remaining = limit - len(rows)
        if remaining:
            older = await read(
                remaining, " AND c.createdAt < @at" if prior else "", "createdAt", **prior,
            )
            if len(older) == remaining:
                boundary = older[-1]["createdAt"]
                prefix = [row for row in older if row["createdAt"] != boundary]
                older = prefix + await read(
                    remaining - len(prefix), " AND c.createdAt = @at", "id", at=boundary,
                )
            rows.extend(older)
        return [
            ReviewItem(
                id=row["id"], kind=kind, state=row["state"], created_at=row["createdAt"],
                title=(row.get("resourceName") or "Resource access")
                if resource else "Cost center membership",
                requester=row.get("requesterName") or row.get("requesterUsername"),
            )
            for row in rows
        ]

    async def list_entitlements(
        self,
        tenant_id: str,
        *,
        subject_id: str | None = None,
        resource_id: str | None = None,
    ) -> list[Entitlement]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if subject_id:
            extra += " AND c.subject.id = @subjectId"
            parameters.append({"name": "@subjectId", "value": subject_id})
        if resource_id:
            extra += " AND c.resource.id = @resourceId"
            parameters.append({"name": "@resourceId", "value": resource_id})
        items = await self._query(Entitlement, tenant_id, "entitlement", extra, parameters)
        return sorted(items, key=lambda item: (item.resource.kind, item.resource.id, item.id))

    async def get_entitlement(self, tenant_id: str, entitlement_id: str) -> Entitlement | None:
        return await self._read(Entitlement, tenant_id, entitlement_id)

    async def create_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement:
        await self._grant_mutation(entitlement, audit_event, "create")
        return entitlement

    async def save_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement:
        await self._grant_mutation(entitlement, audit_event, "replace")
        return entitlement

    async def delete_entitlement(self, entitlement: Entitlement, audit_event: AuditEvent) -> None:
        await self._grant_mutation(entitlement, audit_event, "delete")

    async def _grant_mutation(
        self, entitlement: Entitlement, audit_event: AuditEvent, operation: str
    ) -> None:
        principal = (
            await self._read(Principal, entitlement.tenant_id, entitlement.subject.id)
            if entitlement.subject.kind != "group"
            else None
        )
        event_type = (
            NotificationEventType.GRANT_CREATED
            if operation == "create"
            else NotificationEventType.GRANT_REVOKED
            if operation == "delete" or not entitlement.enabled
            else NotificationEventType.GRANT_CHANGED
        )
        event = grant_event(
            entitlement,
            audit_event,
            event_type,
            user_object_id=(
                principal.object_id if principal and entitlement.subject.kind == "user" else None
            ),
            subject_object_id=principal.object_id if principal else None,
        )
        mutation = (
            ("create", (self._document(entitlement),))
            if operation == "create"
            else self._delete_operation(entitlement.id, entitlement)
            if operation == "delete"
            else self._replace_operation(entitlement)
        )
        await self._execute_batch(
            [
                mutation,
                ("create", (self._document(audit_event),)),
                ("create", (self._document(event),)),
            ],
            audit_event,
            conflict_message="The entitlement changed; reload it and try again",
        )

    async def list_access_requests(
        self,
        tenant_id: str,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
        cost_center_id: str | None = None,
    ) -> list[AccessRequest]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if requester_object_id:
            extra += " AND c.requesterObjectId = @requesterObjectId"
            parameters.append({"name": "@requesterObjectId", "value": requester_object_id})
        if state:
            extra += " AND c.state = @state"
            parameters.append({"name": "@state", "value": state})
        if cost_center_id is not None:
            extra += " AND c.costCenterId = @center"
            parameters.append({"name": "@center", "value": cost_center_id})
        items = await self._query(AccessRequest, tenant_id, "accessRequest", extra, parameters)
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def get_access_request(
        self, tenant_id: str, request_id: str, *, requester_object_id: str | None = None,
        review_center_ids: frozenset[str] | None = None,
    ) -> AccessRequest | None:
        return await self._read_request(
            AccessRequest, tenant_id, request_id, requester_object_id=requester_object_id,
            review_center_ids=review_center_ids,
        )

    async def create_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent,
        *, form_guard: RequestFormHead | None = None,
    ) -> AccessRequest:
        return await self.create_access_request_with_events(
            access_request, audit_event, [access_request_event(access_request)],
            form_guard=form_guard,
        )

    async def get_request_form_head(self, tenant_id: str) -> RequestFormHead | None:
        return await self._read(RequestFormHead, tenant_id, form_head_id(tenant_id))

    async def get_request_form_revision(
        self, tenant_id: str, revision: int,
    ) -> RequestFormRevision | None:
        return await self._read(
            RequestFormRevision, tenant_id, form_revision_id(tenant_id, revision)
        )

    async def publish_request_form(
        self, head: RequestFormHead, revision: RequestFormRevision, audit: AuditEvent,
    ) -> RequestFormRevision:
        if (
            head.tenant_id != revision.tenant_id
            or head.tenant_id != audit.tenant_id
            or head.revision != revision.revision
        ):
            raise ValueError("A form publication must share its tenant and revision")
        await self._execute_batch(
            [
                self._replace_operation(head) if head.etag else ("create", (self._document(head),)),
                ("create", (self._document(revision),)),
                ("create", (self._document(audit),)),
            ],
            audit,
            conflict_message="The request form changed; reload before publishing.",
        )
        return revision

    async def save_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent, *,
        center_guard: CostCenter | None = None,
    ) -> AccessRequest:
        if center_guard is not None:
            if not access_request.etag:
                raise ValueError("A request decision requires the etag it was read with")
            return await self._request_events(
                access_request, audit_event, [access_request_event(access_request)],
                create=False, center_guard=center_guard,
            )
        return await self.save_access_request_with_events(
            access_request, audit_event, [access_request_event(access_request)]
        )

    async def _request_events(
        self,
        request: AccessRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
        *,
        create: bool,
        form_guard: RequestFormHead | None = None,
        center_guard: CostCenter | None = None,
    ) -> AccessRequest:
        if any(event.tenant_id != request.tenant_id for event in events):
            raise ValueError("Notification events must belong to the source tenant")
        if not create:
            require_unchanged_form(
                await self.get_access_request(request.tenant_id, request.id), request
            )
        guard = []
        if form_guard is not None:
            if form_guard.tenant_id != request.tenant_id:
                raise ValueError("The request form guard must belong to the source tenant")
            guard = [
                self._replace_operation(form_guard) if form_guard.etag
                else ("create", (self._document(form_guard),))
            ]
        try:
            await self._execute_batch(
                [
                    *guard,
                    *self._center_guard(center_guard, request.tenant_id, request.cost_center_id),
                ("create", (self._document(request),))
                if create
                else self._replace_operation(request),
                ("create", (self._document(audit_event),)),
                *(("create", (self._document(event),)) for event in events),
                ],
                audit_event,
                conflict_message="The access request changed; reload it and try again",
            )
        except ConflictError as error:
            cause = error.__cause__
            if guard and isinstance(cause, exceptions.CosmosBatchOperationError) and (
                cause.error_index == 0
            ):
                raise ConflictError(
                    "The request form changed during submission. Reload and review it.",
                    details={"reason": "requestFormWriteConflict"},
                ) from error
            raise
        return request

    async def create_access_request_with_events(
        self, request: AccessRequest, audit_event: AuditEvent, events: Sequence[NotificationEvent],
        *, form_guard: RequestFormHead | None = None,
    ) -> AccessRequest:
        return await self._request_events(
            request, audit_event, events, create=True, form_guard=form_guard
        )

    async def save_access_request_with_events(
        self, request: AccessRequest, audit_event: AuditEvent, events: Sequence[NotificationEvent]
    ) -> AccessRequest:
        if not request.etag:
            raise ValueError("A request decision requires the etag it was read with")
        return await self._request_events(request, audit_event, events, create=False)

    async def approve_access_request(
        self,
        access_request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
        *,
        center_guard: CostCenter | None = None,
    ) -> AccessRequest:
        return await self.approve_access_request_with_events(
            access_request, entitlement, audit_events, [access_request_event(access_request)],
            center_guard=center_guard,
        )

    async def list_cost_center_requests(
        self,
        tenant_id: str,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
    ) -> list[CostCenterRequest]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if requester_object_id is not None:
            extra += " AND c.requesterObjectId = @requesterObjectId"
            parameters.append({"name": "@requesterObjectId", "value": requester_object_id})
        if state is not None:
            extra += " AND c.state = @state"
            parameters.append({"name": "@state", "value": state})
        items = await self._query(
            CostCenterRequest, tenant_id, "costCenterRequest", extra, parameters
        )
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    async def list_membership_requests(
        self,
        tenant_id: str,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
        cost_center_id: str | None = None,
    ) -> list[MembershipRequest]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if requester_object_id is not None:
            extra += " AND c.requesterObjectId = @requesterObjectId"
            parameters.append({"name": "@requesterObjectId", "value": requester_object_id})
        if state is not None:
            extra += " AND c.state = @state"
            parameters.append({"name": "@state", "value": state})
        if cost_center_id is not None:
            extra += " AND c.costCenter.id = @center"
            parameters.append({"name": "@center", "value": cost_center_id})
        items = await self._query(
            MembershipRequest, tenant_id, "costCenterMembershipRequest", extra, parameters
        )
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    async def get_membership_request(
        self,
        tenant_id: str,
        request_id: str,
        *,
        review_center_ids: frozenset[str] | None = None,
    ) -> MembershipRequest | None:
        return await self._read_request(
            MembershipRequest, tenant_id, request_id, review_center_ids=review_center_ids,
        )

    async def create_membership_request(
        self,
        request: MembershipRequest,
        audit: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> MembershipRequest:
        validated = validate_membership_write(request, audit, events)
        guard = MembershipRequestGuard(
            id=membership_guard_id(request),
            tenant_id=request.tenant_id,
            request_id=request.id,
        )
        await self._execute_batch(
            [
                ("create", (self._document(validated),)),
                ("create", (self._document(guard),)),
                ("create", (self._document(audit),)),
                *(("create", (self._document(event),)) for event in events),
            ],
            audit,
            conflict_message="An active membership request already exists for this center",
        )
        return await self._membership_result(request)

    async def save_membership_request(
        self,
        request: MembershipRequest,
        audit: AuditEvent,
        events: Sequence[NotificationEvent],
        *,
        center_guard: CostCenter | None = None,
    ) -> MembershipRequest:
        previous = await self.get_membership_request(request.tenant_id, request.id)
        if not request.etag or previous is None or previous.etag != request.etag:
            raise ConflictError(MEMBERSHIP_CHANGED)
        validated = validate_membership_write(request, audit, events, previous)
        operations = [
            *self._center_guard(center_guard, request.tenant_id, request.cost_center.id),
            self._replace_operation(validated),
            ("create", (self._document(audit),)),
            *(("create", (self._document(event),)) for event in events),
        ]
        if previous.active and not request.active:
            guard = await self._read(
                MembershipRequestGuard,
                request.tenant_id,
                membership_guard_id(request),
            )
            if guard is None or guard.request_id != request.id:
                raise ConflictError("The active membership guard changed; reload the request")
            operations.append(self._delete_operation(guard.id, guard))
        await self._execute_batch(operations, audit, conflict_message=MEMBERSHIP_CHANGED)
        return await self._membership_result(request)

    async def _membership_result(self, request: MembershipRequest) -> MembershipRequest:
        saved = await self.get_membership_request(request.tenant_id, request.id)
        if saved is None:
            raise ChangeNotRecordedError(
                "Membership request write completed but its result is unavailable. Reload "
                "the existing request; do not submit another."
            )
        return saved

    async def get_cost_center_request(
        self, tenant_id: str, request_id: str
    ) -> CostCenterRequest | None:
        return await self._read_request(CostCenterRequest, tenant_id, request_id)

    async def _read_request(
        self, model_type: type[RequestT], tenant_id: str, request_id: str,
        *, requester_object_id: str | None = None,
        review_center_ids: frozenset[str] | None = None,
    ) -> RequestT | None:
        try:
            document = await self._desired.read_item(item=request_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        if document.get("entityType") != model_type.model_fields["entity_type"].default:
            return None
        if (
            requester_object_id is not None
            and document.get("requesterObjectId") != requester_object_id
        ):
            return None
        if review_center_ids is not None:
            if model_type is MembershipRequest:
                scope = document.get("costCenter")
                center_id = scope.get("id") if isinstance(scope, dict) else None
            else:
                center_id = document.get("costCenterId")
            # A malformed foreign request must never reach typed evidence decoding.
            if (
                document.get("tenantId") != tenant_id
                or not isinstance(center_id, str)
                or center_id not in review_center_ids
            ):
                return None
        model = self._model(model_type, document)
        return model if model.tenant_id == tenant_id else None

    async def create_cost_center_request(
        self,
        request: CostCenterRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> CostCenterRequest:
        validated = validate_request_write(request, audit_event, events)
        if request.state != AccessRequestState.PENDING or request.approval is not None:
            raise ValueError("A new cost center request must be pending and unclaimed")
        return await self._cost_center_request_events(validated, audit_event, events, create=True)

    async def save_cost_center_request(
        self,
        request: CostCenterRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> CostCenterRequest:
        validated = validate_request_write(request, audit_event, events)
        if not request.etag:
            raise ValueError("A cost center request decision requires its read ETag")
        stored = await self.get_cost_center_request(request.tenant_id, request.id)
        if stored is None or stored.etag != request.etag:
            raise ConflictError(REQUEST_CHANGED)
        require_request_change(stored, validated)
        return await self._cost_center_request_events(validated, audit_event, events, create=False)

    async def _cost_center_request_events(
        self,
        request: CostCenterRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
        *,
        create: bool,
    ) -> CostCenterRequest:
        await self._execute_batch(
            [
                ("create", (self._document(request),))
                if create
                else self._replace_operation(request),
                ("create", (self._document(audit_event),)),
                *(("create", (self._document(event),)) for event in events),
            ],
            audit_event,
            conflict_message=REQUEST_CHANGED,
        )
        stored = await self.get_cost_center_request(request.tenant_id, request.id)
        if stored is None:
            raise ChangeNotRecordedError(
                "The cost center request write completed, but its result could not be read. "
                "Reload the existing request instead of creating another."
            )
        return stored

    async def approve_access_request_with_events(
        self,
        request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
        events: Sequence[NotificationEvent],
        *,
        provenance: ApprovalDecisionProvenance | None = None,
        policy_guard: ApprovalPolicy | None = None,
        center_guard: CostCenter | None = None,
    ) -> AccessRequest:
        require_human_form_review(
            request, policy=bool(policy_guard or (provenance and provenance.origin == "policy"))
        )
        access_request = request
        if provenance:
            access_request = request.model_copy(
                update={"approval_provenance": provenance.model_dump(mode="json", by_alias=True)}
            )
        if any(event.tenant_id != request.tenant_id for event in events):
            raise ValueError("Notification events must belong to the source tenant")
        if policy_guard and (not policy_guard.etag or policy_guard.tenant_id != request.tenant_id):
            raise ValueError("The policy guard must have its tenant partition and read ETag")
        # One transactional batch on the tenant partition: the grant is created and the request
        # is replaced (conditionally on the etag it was read with) together or not at all.
        if not access_request.etag:
            raise ValueError("Approving a request requires the etag it was read with")
        require_unchanged_form(
            await self.get_access_request(request.tenant_id, request.id), access_request
        )
        await self._execute_batch(
            [
                ("create", (self._document(entitlement),)),
                self._replace_operation(access_request),
                *(("create", (self._document(event),)) for event in audit_events),
                *(("create", (self._document(event),)) for event in events),
                (
                    "create",
                    (
                        self._document(
                            grant_event(
                                entitlement,
                                audit_events[0],
                                NotificationEventType.GRANT_CREATED,
                                user_object_id=request.requester_object_id,
                                request_id=request.id,
                            )
                        ),
                    ),
                ),
                *([self._replace_operation(policy_guard)] if policy_guard else []),
                *self._center_guard(center_guard, request.tenant_id, request.cost_center_id),
            ],
            audit_events,
            conflict_message=(
                "The access request was decided or its grant already exists; reload it and try "
                "again"
            ),
        )
        return access_request
