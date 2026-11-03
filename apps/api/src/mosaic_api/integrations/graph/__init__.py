"""Read-only Microsoft Graph directory lookup and a separate opt-in management adapter.

MOSAIC reads the directory with its own managed identity to let administrators find users,
agents and security groups by name, to confirm an object exists and is the kind they said, and to
work out which granted groups a principal belongs to. ``DirectoryLookup`` never writes to the
directory. ``GroupManagement`` is a separate, explicitly consented administrator capability. The
gateway never calls Graph: runtime enforcement uses the ``groups`` claim in the caller's token.
"""

from collections.abc import Sequence
from typing import Protocol

from mosaic_api.domain import DirectoryMemberPage, DirectoryObject, DirectorySearchKind
from mosaic_api.integrations.graph.membership import GroupMembershipEvidence
from mosaic_api.integrations.graph.photos import DirectoryPhoto


class DirectoryLookup(Protocol):
    """What MOSAIC needs from the directory. Every method is a read.

    Implementations raise :class:`mosaic_api.errors.DirectoryForbiddenError` when MOSAIC's
    identity lacks the Graph permission a read needs, and
    :class:`mosaic_api.errors.DirectoryError` when Graph fails or can't be reached.
    """

    async def search(
        self, kind: DirectorySearchKind, query: str, *, limit: int = 20
    ) -> list[DirectoryObject]:
        """Objects of ``kind`` whose name or identifier starts with or contains ``query``."""
        ...

    async def get_object(self, object_id: str) -> DirectoryObject | None:
        """The user, agent or security group with this object ID, or None if there is none."""
        ...

    async def user_photo(self, tenant_id: str, object_id: str) -> DirectoryPhoto | None:
        """A small photo of an ordinary user in the caller's tenant, or None if absent.

        Implementations verify the object kind before reading a photo. Agents, groups and
        applications do not have person avatars. Photos are optional and never persisted.
        """
        ...

    async def application_permissions(self) -> frozenset[str]:
        """Application permission values in the API identity's current Graph token."""
        ...

    async def member_groups(self, object_id: str, group_object_ids: Sequence[str]) -> set[str]:
        """The subset of ``group_object_ids`` this object belongs to, directly or by nesting."""
        ...

    async def group_members(
        self, group_object_id: str, *, limit: int = 200
    ) -> DirectoryMemberPage:
        """The group's direct and nested members, up to ``limit``."""
        ...

    async def verify_group_members(
        self,
        group_object_id: str,
        candidate_object_ids: Sequence[str],
        *,
        timeout_seconds: float = 60,
    ) -> GroupMembershipEvidence:
        """Verify candidates against every transitive page with the app-only tenant identity.

        Return no evidence on a partial/failed enumeration. The immutable result covers only
        the requested cohort; its freshness starts before the first read. It is not a cached
        directory preview or an atomic snapshot of concurrent Entra changes. The whole read
        has a finite deadline of at most 60 seconds, including authentication and retries.
        """
        ...

    async def close(self) -> None: ...


from mosaic_api.integrations.graph.client import GraphDirectoryLookup  # noqa: E402
from mosaic_api.integrations.graph.fake import FakeDirectoryLookup, FakeDirectoryState  # noqa: E402
from mosaic_api.integrations.graph.fake_group_management import FakeGroupManagement  # noqa: E402
from mosaic_api.integrations.graph.group_management import (  # noqa: E402
    DirectGroupMember,
    GraphGroup,
    GraphGroupManagement,
    GroupFailureKind,
    GroupManagement,
    GroupManagementCapabilities,
    GroupManagementError,
    GroupMemberPage,
    GroupWriteOutcome,
    GroupWriteResult,
)

__all__ = [
    "DirectGroupMember",
    "DirectoryLookup",
    "DirectoryPhoto",
    "FakeDirectoryLookup",
    "FakeDirectoryState",
    "FakeGroupManagement",
    "GraphDirectoryLookup",
    "GraphGroup",
    "GraphGroupManagement",
    "GroupFailureKind",
    "GroupManagement",
    "GroupManagementCapabilities",
    "GroupManagementError",
    "GroupMemberPage",
    "GroupMembershipEvidence",
    "GroupWriteOutcome",
    "GroupWriteResult",
]
