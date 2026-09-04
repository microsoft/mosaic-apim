from mosaic_api.domain import AccessRequest, AuditEvent, Entitlement
from mosaic_api.errors import ConflictError


class InMemoryEntitlementRepository:
    def __init__(self) -> None:
        self.entitlements: dict[str, Entitlement] = {}
        self.access_requests: dict[str, AccessRequest] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

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
        if entitlement.id in self.entitlements:
            raise ConflictError("This subject is already entitled to this resource")
        return await self.save_entitlement(entitlement, audit_event)

    async def save_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement:
        self.entitlements[entitlement.id] = entitlement
        self.audit_events[audit_event.id] = audit_event
        return entitlement

    async def delete_entitlement(self, entitlement: Entitlement, audit_event: AuditEvent) -> None:
        if await self.get_entitlement(entitlement.tenant_id, entitlement.id):
            del self.entitlements[entitlement.id]
            self.audit_events[audit_event.id] = audit_event

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
        if access_request.id in self.access_requests:
            raise ConflictError("You already have an open request for this resource")
        return await self.save_access_request(access_request, audit_event)

    async def save_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
        self.access_requests[access_request.id] = access_request
        self.audit_events[audit_event.id] = audit_event
        return access_request
