from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
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
from mosaic_api.repositories import DirectoryRepository, EntitlementRepository, GatewayRepository
from mosaic_api.services.model_access import entitlement_publication, publication_lock


@dataclass(frozen=True)
class Actor:
    object_id: str
    tenant_id: str


class DirectoryService:
    def __init__(
        self,
        repository: DirectoryRepository,
        *,
        gateway_repository: GatewayRepository | None = None,
        entitlement_repository: EntitlementRepository | None = None,
    ) -> None:
        self._repository = repository
        self._gateways = gateway_repository
        self._entitlements = entitlement_repository

    @asynccontextmanager
    async def _principal_mutation(self, actor: Actor, principal_id: str) -> AsyncIterator[None]:
        publications: set[str] = set()
        if self._gateways:
            if self._entitlements:
                for grant in await self._entitlements.list_entitlements(
                    actor.tenant_id, subject_id=principal_id
                ):
                    publication = await entitlement_publication(self._gateways, grant)
                    if publication:
                        publications.add(publication.id)
            for publication in await self._gateways.list_publications(actor.tenant_id):
                if publication.applied_access and any(
                    grant.subject.id == principal_id for grant in publication.applied_access.grants
                ):
                    publications.add(publication.id)
        async with AsyncExitStack() as stack:
            if self._gateways:
                for publication_id in sorted(publications):
                    await stack.enter_async_context(
                        publication_lock(self._gateways, actor.tenant_id, publication_id)
                    )
            yield

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
        async with self._principal_mutation(actor, principal_id):
            return await self._update_principal(actor, principal_id, request)

    async def _update_principal(
        self, actor: Actor, principal_id: str, request: PrincipalUpdate
    ) -> Principal:
        principal = await self.get_principal(actor, principal_id)
        changes = request.model_dump(exclude_unset=True)
        if changes.get("kind") not in {None, principal.kind}:
            await self._require_no_grants(actor, principal_id, active_only=True)
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
        async with self._principal_mutation(actor, principal_id):
            await self._delete_principal(actor, principal_id)

    async def _require_no_grants(
        self, actor: Actor, principal_id: str, *, active_only: bool = False
    ) -> None:
        if self._entitlements:
            grants = await self._entitlements.list_entitlements(
                actor.tenant_id, subject_id=principal_id
            )
            if any(grant.enabled or not active_only for grant in grants):
                raise ConflictError(
                    "Remove this principal's grants before deleting it or changing its kind",
                    details={"principalId": principal_id},
                )
        if self._gateways:
            for publication in await self._gateways.list_publications(actor.tenant_id):
                if publication.applied_access and any(
                    grant.subject.id == principal_id
                    and (
                        grant.enabled
                        or publication.created_resources()
                        or publication.access_state in {"applying", "unknown"}
                    )
                    for grant in publication.applied_access.grants
                ):
                    raise ConflictError(
                        "This principal has managed runtime access; unpublish its model first",
                        details={"publicationId": publication.id},
                    )

    async def _delete_principal(self, actor: Actor, principal_id: str) -> None:
        principal = await self.get_principal(actor, principal_id)
        await self._require_no_grants(actor, principal_id)
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
