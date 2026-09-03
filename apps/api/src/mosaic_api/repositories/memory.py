from collections.abc import Iterable
from typing import Protocol

from mosaic_api.domain import AuditEvent, Group, GroupMembership, Principal
from mosaic_api.errors import ConflictError


class TenantEntity(Protocol):
    tenant_id: str


class InMemoryDirectoryRepository:
    def __init__(self) -> None:
        self.principals: dict[str, Principal] = {}
        self.groups: dict[str, Group] = {}
        self.memberships: dict[str, GroupMembership] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    @staticmethod
    def _for_tenant[T: TenantEntity](values: Iterable[T], tenant_id: str) -> list[T]:
        return [item for item in values if item.tenant_id == tenant_id]

    async def list_principals(self, tenant_id: str) -> list[Principal]:
        return sorted(
            self._for_tenant(self.principals.values(), tenant_id),
            key=lambda item: (item.label or "", item.object_id),
        )

    async def get_principal(self, tenant_id: str, principal_id: str) -> Principal | None:
        principal = self.principals.get(principal_id)
        return principal if principal and principal.tenant_id == tenant_id else None

    async def find_principal_by_object_id(self, tenant_id: str, object_id: str) -> Principal | None:
        return next(
            (
                principal
                for principal in self.principals.values()
                if principal.tenant_id == tenant_id and principal.object_id == object_id
            ),
            None,
        )

    async def save_principal(self, principal: Principal, audit_event: AuditEvent) -> Principal:
        self.principals[principal.id] = principal
        self.audit_events[audit_event.id] = audit_event
        return principal

    async def create_principal(self, principal: Principal, audit_event: AuditEvent) -> Principal:
        if principal.id in self.principals:
            raise ConflictError("A principal with this Entra object ID already exists")
        return await self.save_principal(principal, audit_event)

    async def delete_principal(self, principal: Principal, audit_event: AuditEvent) -> None:
        if await self.get_principal(principal.tenant_id, principal.id):
            del self.principals[principal.id]
            self.audit_events[audit_event.id] = audit_event

    async def list_groups(self, tenant_id: str) -> list[Group]:
        return sorted(
            self._for_tenant(self.groups.values(), tenant_id),
            key=lambda item: item.name.casefold(),
        )

    async def get_group(self, tenant_id: str, group_id: str) -> Group | None:
        group = self.groups.get(group_id)
        return group if group and group.tenant_id == tenant_id else None

    async def find_group_by_name(self, tenant_id: str, name: str) -> Group | None:
        normalized = name.casefold()
        return next(
            (
                group
                for group in self.groups.values()
                if group.tenant_id == tenant_id and group.name.casefold() == normalized
            ),
            None,
        )

    async def save_group(self, group: Group, audit_event: AuditEvent) -> Group:
        self.groups[group.id] = group
        self.audit_events[audit_event.id] = audit_event
        return group

    async def create_group(self, group: Group, audit_event: AuditEvent) -> Group:
        if group.id in self.groups:
            raise ConflictError("A group with this name already exists")
        return await self.save_group(group, audit_event)

    async def delete_group(self, group: Group, audit_event: AuditEvent) -> None:
        if await self.get_group(group.tenant_id, group.id):
            del self.groups[group.id]
            self.audit_events[audit_event.id] = audit_event

    async def list_memberships(
        self, tenant_id: str, *, group_id: str | None = None, principal_id: str | None = None
    ) -> list[GroupMembership]:
        memberships = self._for_tenant(self.memberships.values(), tenant_id)
        return [
            item
            for item in memberships
            if (group_id is None or item.group_id == group_id)
            and (principal_id is None or item.principal_id == principal_id)
        ]

    async def get_membership(
        self, tenant_id: str, group_id: str, principal_id: str
    ) -> GroupMembership | None:
        return next(
            (
                item
                for item in self.memberships.values()
                if item.tenant_id == tenant_id
                and item.group_id == group_id
                and item.principal_id == principal_id
            ),
            None,
        )

    async def create_membership(
        self,
        membership: GroupMembership,
        group: Group,
        principal: Principal,
        audit_event: AuditEvent,
    ) -> GroupMembership:
        if group.id not in self.groups or principal.id not in self.principals:
            raise ConflictError("A referenced group or principal no longer exists")
        if membership.id in self.memberships:
            raise ConflictError("This group membership already exists")
        self.memberships[membership.id] = membership
        self.audit_events[audit_event.id] = audit_event
        return membership

    async def delete_membership(self, membership: GroupMembership, audit_event: AuditEvent) -> None:
        item = self.memberships.get(membership.id)
        if item and item.tenant_id == membership.tenant_id:
            del self.memberships[membership.id]
            self.audit_events[audit_event.id] = audit_event
