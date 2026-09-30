from collections.abc import Mapping, Sequence

from mosaic_api.domain import (
    DirectoryMemberPage,
    DirectoryObject,
    DirectorySearchKind,
    PrincipalKind,
)
from mosaic_api.errors import DirectoryError, DirectoryForbiddenError


class FakeDirectoryLookup:
    def __init__(
        self,
        objects: Sequence[DirectoryObject],
        group_members: Mapping[str, Sequence[str]] | None = None,
        *,
        forbidden: bool = False,
        unavailable: bool = False,
    ) -> None:
        self._objects = {item.object_id.casefold(): item for item in objects}
        self._members = {
            group_id.casefold(): [member_id.casefold() for member_id in members]
            for group_id, members in (group_members or {}).items()
        }
        self._forbidden = forbidden
        self._unavailable = unavailable
        self.closed = False

    async def search(
        self, kind: DirectorySearchKind, query: str, *, limit: int = 20
    ) -> list[DirectoryObject]:
        self._raise_if_configured()
        normalized = query.strip().casefold()
        if not normalized:
            return []
        matches = [
            item
            for item in self._objects.values()
            if _matches_kind(kind, item.kind)
            and (
                normalized in (item.display_name or "").casefold()
                or normalized in (item.detail or "").casefold()
                or normalized == item.object_id.casefold()
            )
        ]
        return sorted(matches, key=lambda item: (item.display_name or "", item.object_id))[
            : min(max(limit, 1), 25)
        ]

    async def get_object(self, object_id: str) -> DirectoryObject | None:
        self._raise_if_configured()
        return self._objects.get(object_id.casefold())

    async def member_groups(self, object_id: str, group_object_ids: Sequence[str]) -> set[str]:
        self._raise_if_configured()
        target = object_id.casefold()
        return {
            group_id.casefold()
            for group_id in group_object_ids
            if self._contains(group_id.casefold(), target, seen=set())
        }

    async def group_members(
        self, group_object_id: str, *, limit: int = 200
    ) -> DirectoryMemberPage:
        self._raise_if_configured()
        found: list[DirectoryObject] = []
        for member_id in self._transitive_members(group_object_id.casefold(), seen=set()):
            item = self._objects.get(member_id)
            if item:
                found.append(item)
        found = sorted(found, key=lambda item: (item.display_name or "", item.object_id))
        capped = found[: max(limit, 0)]
        return DirectoryMemberPage(
            group_object_id=group_object_id.casefold(),
            members=capped,
            truncated=len(found) > len(capped),
        )

    async def close(self) -> None:
        self.closed = True

    def _contains(self, group_id: str, target: str, *, seen: set[str]) -> bool:
        if group_id in seen:
            return False
        seen.add(group_id)
        for member_id in self._members.get(group_id, []):
            if member_id == target or self._contains(member_id, target, seen=seen):
                return True
        return False

    def _transitive_members(self, group_id: str, *, seen: set[str]) -> list[str]:
        results: list[str] = []
        if group_id in seen:
            return results
        seen.add(group_id)
        for member_id in self._members.get(group_id, []):
            results.append(member_id)
            item = self._objects.get(member_id)
            if item and item.kind == PrincipalKind.SECURITY_GROUP:
                results.extend(self._transitive_members(member_id, seen=seen))
        return results

    def _raise_if_configured(self) -> None:
        if self._forbidden:
            raise DirectoryForbiddenError("Microsoft Graph refused the directory read; grant test.")
        if self._unavailable:
            raise DirectoryError("Microsoft Graph directory read failed")


def _matches_kind(kind: DirectorySearchKind, object_kind: PrincipalKind) -> bool:
    if kind == DirectorySearchKind.USER:
        return object_kind in {PrincipalKind.USER, PrincipalKind.AGENT_USER}
    if kind == DirectorySearchKind.GROUP:
        return object_kind == PrincipalKind.SECURITY_GROUP
    if kind == DirectorySearchKind.AGENT:
        return object_kind in {PrincipalKind.AGENT_IDENTITY, PrincipalKind.AGENT_USER}
    return False
