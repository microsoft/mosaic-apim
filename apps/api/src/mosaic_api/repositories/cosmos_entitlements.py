from collections.abc import Sequence
from typing import Any, TypeVar

from azure.cosmos import exceptions

from mosaic_api.approvals import ApprovalDecisionProvenance, ApprovalPolicy
from mosaic_api.cost_center_membership_requests import (
    MembershipRequest,
    MembershipRequestGuard,
    membership_guard_id,
)
from mosaic_api.cost_center_requests import CostCenterRequest
from mosaic_api.domain import AccessRequest, AccessRequestState, AuditEvent, Entitlement, Principal
from mosaic_api.errors import ChangeNotRecordedError, ConflictError
from mosaic_api.notifications import NotificationEvent, NotificationEventType
from mosaic_api.repositories.cosmos import CosmosRepositoryBase
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
    ) -> list[AccessRequest]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if requester_object_id:
            extra += " AND c.requesterObjectId = @requesterObjectId"
            parameters.append({"name": "@requesterObjectId", "value": requester_object_id})
        if state:
            extra += " AND c.state = @state"
            parameters.append({"name": "@state", "value": state})
        items = await self._query(AccessRequest, tenant_id, "accessRequest", extra, parameters)
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def get_access_request(self, tenant_id: str, request_id: str) -> AccessRequest | None:
        return await self._read_request(AccessRequest, tenant_id, request_id)

    async def create_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
        return await self.create_access_request_with_events(
            access_request, audit_event, [access_request_event(access_request)]
        )

    async def save_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
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
    ) -> AccessRequest:
        if any(event.tenant_id != request.tenant_id for event in events):
            raise ValueError("Notification events must belong to the source tenant")
        await self._execute_batch(
            [
                ("create", (self._document(request),))
                if create
                else self._replace_operation(request),
                ("create", (self._document(audit_event),)),
                *(("create", (self._document(event),)) for event in events),
            ],
            audit_event,
            conflict_message="The access request changed; reload it and try again",
        )
        return request

    async def create_access_request_with_events(
        self, request: AccessRequest, audit_event: AuditEvent, events: Sequence[NotificationEvent]
    ) -> AccessRequest:
        return await self._request_events(request, audit_event, events, create=True)

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
    ) -> AccessRequest:
        return await self.approve_access_request_with_events(
            access_request, entitlement, audit_events, [access_request_event(access_request)]
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
    ) -> list[MembershipRequest]:
        extra = ""
        parameters: list[dict[str, Any]] = []
        if requester_object_id is not None:
            extra += " AND c.requesterObjectId = @requesterObjectId"
            parameters.append({"name": "@requesterObjectId", "value": requester_object_id})
        if state is not None:
            extra += " AND c.state = @state"
            parameters.append({"name": "@state", "value": state})
        items = await self._query(
            MembershipRequest, tenant_id, "costCenterMembershipRequest", extra, parameters
        )
        return sorted(items, key=lambda item: (item.created_at, item.id), reverse=True)

    async def get_membership_request(
        self,
        tenant_id: str,
        request_id: str,
    ) -> MembershipRequest | None:
        return await self._read_request(MembershipRequest, tenant_id, request_id)

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
    ) -> MembershipRequest:
        previous = await self.get_membership_request(request.tenant_id, request.id)
        if not request.etag or previous is None or previous.etag != request.etag:
            raise ConflictError(MEMBERSHIP_CHANGED)
        validated = validate_membership_write(request, audit, events, previous)
        operations = [
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
        self, model_type: type[RequestT], tenant_id: str, request_id: str
    ) -> RequestT | None:
        try:
            document = await self._desired.read_item(item=request_id, partition_key=tenant_id)
        except exceptions.CosmosResourceNotFoundError:
            return None
        if document.get("entityType") != model_type.model_fields["entity_type"].default:
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
    ) -> AccessRequest:
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
            ],
            audit_events,
            conflict_message=(
                "The access request was decided or its grant already exists; reload it and try "
                "again"
            ),
        )
        return access_request
