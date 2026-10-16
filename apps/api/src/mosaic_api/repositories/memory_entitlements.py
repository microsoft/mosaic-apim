from collections.abc import Sequence

from mosaic_api.approvals import ApprovalDecisionProvenance, ApprovalPolicy
from mosaic_api.domain import AccessRequest, AccessRequestState, AuditEvent, Entitlement, new_id
from mosaic_api.errors import ConflictError
from mosaic_api.notifications import NotificationEvent, NotificationEventType
from mosaic_api.repositories.base import DirectoryRepository
from mosaic_api.services.approval_events import grant_event
from mosaic_api.services.notification_events import access_request_event


class InMemoryEntitlementRepository:
    def __init__(
        self,
        *,
        notification_events: dict[tuple[str, str], NotificationEvent] | None = None,
        approval_policies: dict[tuple[str, str], ApprovalPolicy] | None = None,
        directory_repository: DirectoryRepository | None = None,
    ) -> None:
        self.entitlements: dict[str, Entitlement] = {}
        self.access_requests: dict[str, AccessRequest] = {}
        self.audit_events: dict[str, AuditEvent] = {}
        self.notification_events = notification_events if notification_events is not None else {}
        self.approval_policies = approval_policies if approval_policies is not None else {}
        self._notification_directory = directory_repository

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def record_audit(self, event: AuditEvent) -> None:
        self.audit_events[event.id] = event

    async def list_entitlements(
        self,
        tenant_id: str,
        *,
        subject_id: str | None = None,
        resource_id: str | None = None,
    ) -> list[Entitlement]:
        items = [
            item
            for item in self.entitlements.values()
            if item.tenant_id == tenant_id
            and (subject_id is None or item.subject.id == subject_id)
            and (resource_id is None or item.resource.id == resource_id)
        ]
        return sorted(items, key=lambda item: (item.resource.kind, item.resource.id, item.id))

    async def get_entitlement(self, tenant_id: str, entitlement_id: str) -> Entitlement | None:
        entitlement = self.entitlements.get(entitlement_id)
        return entitlement if entitlement and entitlement.tenant_id == tenant_id else None

    async def create_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement:
        event = await self._grant_event(
            entitlement, audit_event, NotificationEventType.GRANT_CREATED
        )
        if entitlement.id in self.entitlements:
            raise ConflictError("This subject is already entitled to this resource")
        self.entitlements[entitlement.id] = entitlement
        self.audit_events[audit_event.id] = audit_event
        self.notification_events[(event.tenant_id, event.id)] = event
        return entitlement

    async def save_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement:
        event = await self._grant_event(
            entitlement,
            audit_event,
            (
                NotificationEventType.GRANT_CHANGED
                if entitlement.enabled
                else NotificationEventType.GRANT_REVOKED
            ),
        )
        self.entitlements[entitlement.id] = entitlement
        self.audit_events[audit_event.id] = audit_event
        self.notification_events[(event.tenant_id, event.id)] = event
        return entitlement

    async def delete_entitlement(self, entitlement: Entitlement, audit_event: AuditEvent) -> None:
        event = await self._grant_event(
            entitlement, audit_event, NotificationEventType.GRANT_REVOKED
        )
        if await self.get_entitlement(entitlement.tenant_id, entitlement.id):
            del self.entitlements[entitlement.id]
            self.audit_events[audit_event.id] = audit_event
            self.notification_events[(event.tenant_id, event.id)] = event

    async def _grant_event(
        self, entitlement: Entitlement, audit_event: AuditEvent, event_type: NotificationEventType
    ) -> NotificationEvent:
        principal = (
            await self._notification_directory.get_principal(
                entitlement.tenant_id, entitlement.subject.id
            )
            if self._notification_directory and entitlement.subject.kind != "group"
            else None
        )
        return grant_event(
            entitlement,
            audit_event,
            event_type,
            user_object_id=(
                principal.object_id if principal and entitlement.subject.kind == "user" else None
            ),
            subject_object_id=principal.object_id if principal else None,
        )

    async def list_access_requests(
        self,
        tenant_id: str,
        *,
        requester_object_id: str | None = None,
        state: str | None = None,
    ) -> list[AccessRequest]:
        items = [
            item
            for item in self.access_requests.values()
            if item.tenant_id == tenant_id
            and (requester_object_id is None or item.requester_object_id == requester_object_id)
            and (state is None or str(item.state) == state)
        ]
        return sorted(items, key=lambda item: item.created_at, reverse=True)

    async def get_access_request(self, tenant_id: str, request_id: str) -> AccessRequest | None:
        access_request = self.access_requests.get(request_id)
        return access_request if access_request and access_request.tenant_id == tenant_id else None

    async def create_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
        return await self.create_access_request_with_events(
            access_request, audit_event, [access_request_event(access_request)]
        )

    async def create_access_request_with_events(
        self,
        request: AccessRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> AccessRequest:
        if request.id in self.access_requests:
            raise ConflictError("You already have an open request for this resource")
        return self._save_request_events(request, audit_event, events)

    async def save_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
        return await self.save_access_request_with_events(
            access_request, audit_event, [access_request_event(access_request)]
        )

    def _save_request_events(
        self, request: AccessRequest, audit_event: AuditEvent, events: Sequence[NotificationEvent]
    ) -> AccessRequest:
        if any(event.tenant_id != request.tenant_id for event in events):
            raise ValueError("Notification events must belong to the source tenant")
        saved = request.model_copy(update={"etag": new_id("etag")}, deep=True)
        self.access_requests[request.id] = saved
        self.audit_events[audit_event.id] = audit_event
        for event in events:
            self.notification_events[(event.tenant_id, event.id)] = event
        return saved.model_copy(deep=True)

    async def save_access_request_with_events(
        self,
        request: AccessRequest,
        audit_event: AuditEvent,
        events: Sequence[NotificationEvent],
    ) -> AccessRequest:
        stored = self.access_requests.get(request.id)
        if request.etag and (stored is None or stored.etag != request.etag):
            raise ConflictError("The access request changed; reload it and try again")
        return self._save_request_events(request, audit_event, events)

    async def approve_access_request(
        self,
        access_request: AccessRequest,
        entitlement: Entitlement,
        audit_events: Sequence[AuditEvent],
    ) -> AccessRequest:
        return await self.approve_access_request_with_events(
            access_request, entitlement, audit_events, [access_request_event(access_request)]
        )

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
        if not audit_events:
            raise ValueError("Approval requires at least one audit event")
        created_event = grant_event(
            entitlement,
            audit_events[0],
            NotificationEventType.GRANT_CREATED,
            user_object_id=request.requester_object_id,
            request_id=request.id,
        )
        if policy_guard:
            current_policy = self.approval_policies.get((policy_guard.tenant_id, policy_guard.id))
            if current_policy is None or (
                current_policy.etag != policy_guard.etag or not current_policy.enabled
            ):
                raise ConflictError("The approval policy changed; evaluate the request again")
        # The checks and writes run with no await between them, which is what makes this
        # all-or-nothing in a single event loop. It stands in for the Cosmos batch's etag and
        # create preconditions.
        stored = self.access_requests.get(access_request.id)
        if (
            stored is None
            or stored.tenant_id != access_request.tenant_id
            or stored.state != AccessRequestState.PENDING
            or stored.etag != access_request.etag
            or entitlement.id in self.entitlements
        ):
            raise ConflictError(
                "The access request was decided or its grant already exists; reload it and try "
                "again"
            )
        self.entitlements[entitlement.id] = entitlement
        saved = access_request.model_copy(update={"etag": new_id("etag")}, deep=True)
        self.access_requests[access_request.id] = saved
        for event in audit_events:
            self.audit_events[event.id] = event
        for notification in events:
            self.notification_events[(notification.tenant_id, notification.id)] = notification
        self.notification_events[(created_event.tenant_id, created_event.id)] = created_event
        return saved.model_copy(deep=True)
