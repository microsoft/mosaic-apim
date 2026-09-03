from dataclasses import dataclass

from mosaic_api.domain import (
    AuditEvent,
    Group,
    GroupCreate,
    GroupMembership,
    GroupUpdate,
    Principal,
    PrincipalCreate,
    PrincipalUpdate,
    deterministic_id,
    new_id,
    utc_now,
)
from mosaic_api.errors import ConflictError, NotFoundError
from mosaic_api.repositories import DirectoryRepository


@dataclass(frozen=True)
class Actor:
    object_id: str
    tenant_id: str


class DirectoryService:
    def __init__(self, repository: DirectoryRepository) -> None:
        self._repository = repository

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

    async def get_principal(self, actor: Actor, principal_id: str) -> Principal:
        principal = await self._repository.get_principal(actor.tenant_id, principal_id)
        if not principal:
            raise NotFoundError("Principal was not found", details={"id": principal_id})
        return principal

    async def create_principal(self, actor: Actor, request: PrincipalCreate) -> Principal:
        existing = await self._repository.find_principal_by_object_id(
            actor.tenant_id, request.object_id
        )
        if existing:
            raise ConflictError(
                "A principal with this Entra object ID already exists",
                details={"objectId": request.object_id, "id": existing.id},
            )
        principal = Principal(
            id=deterministic_id("principal", actor.tenant_id, request.object_id),
            tenant_id=actor.tenant_id,
            **request.model_dump(),
        )
        saved = await self._repository.create_principal(
            principal,
            self._audit_event(actor, "principal.created", "principal", principal.id),
        )
        return saved

    async def update_principal(
        self, actor: Actor, principal_id: str, request: PrincipalUpdate
    ) -> Principal:
        principal = await self.get_principal(actor, principal_id)
        changes = request.model_dump(exclude_unset=True)
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
        principal = await self.get_principal(actor, principal_id)
        memberships = await self._repository.list_memberships(
            actor.tenant_id, principal_id=principal_id
        )
        if memberships:
            raise ConflictError(
                "Remove this principal from all groups before deleting it",
                details={"membershipCount": len(memberships)},
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
        await self.get_group(actor, group_id)
        return await self._repository.list_memberships(actor.tenant_id, group_id=group_id)

    async def add_membership(
        self, actor: Actor, group_id: str, principal_id: str
    ) -> tuple[GroupMembership, bool]:
        group = await self.get_group(actor, group_id)
        principal = await self.get_principal(actor, principal_id)
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
