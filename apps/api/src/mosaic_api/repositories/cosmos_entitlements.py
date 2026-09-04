from typing import Any

from mosaic_api.domain import AccessRequest, AuditEvent, Entitlement
from mosaic_api.repositories.cosmos import CosmosRepositoryBase


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
        await self._mutate(
            entitlement,
            None,
            audit_event,
            "create",
            conflict_message="This subject is already entitled to this resource",
        )
        return entitlement

    async def save_entitlement(
        self, entitlement: Entitlement, audit_event: AuditEvent
    ) -> Entitlement:
        await self._mutate(
            entitlement,
            None,
            audit_event,
            "replace",
            conflict_message="The entitlement changed; reload it and try again",
        )
        return entitlement

    async def delete_entitlement(self, entitlement: Entitlement, audit_event: AuditEvent) -> None:
        await self._mutate(
            entitlement,
            entitlement.id,
            audit_event,
            "delete",
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
        return await self._read(AccessRequest, tenant_id, request_id)

    async def create_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
        await self._mutate(
            access_request,
            None,
            audit_event,
            "create",
            conflict_message="You already have an open request for this resource",
        )
        return access_request

    async def save_access_request(
        self, access_request: AccessRequest, audit_event: AuditEvent
    ) -> AccessRequest:
        await self._mutate(
            access_request,
            None,
            audit_event,
            "replace",
            conflict_message="The access request changed; reload it and try again",
        )
        return access_request
