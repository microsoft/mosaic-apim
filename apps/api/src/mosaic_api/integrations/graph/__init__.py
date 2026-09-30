"""Read-only Microsoft Graph directory lookup.

MOSAIC reads the directory with its own managed identity to let administrators find users,
agents and security groups by name, to confirm an object exists and is the kind they said, and to
work out which granted groups a principal belongs to. It never writes to the directory. The
gateway never calls Graph: runtime enforcement uses the ``groups`` claim Entra puts in the caller's
token.
"""

from collections.abc import Sequence
from typing import Protocol

from mosaic_api.domain import DirectoryMemberPage, DirectoryObject, DirectorySearchKind


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

    async def member_groups(self, object_id: str, group_object_ids: Sequence[str]) -> set[str]:
        """The subset of ``group_object_ids`` this object belongs to, directly or by nesting."""
        ...

    async def group_members(
        self, group_object_id: str, *, limit: int = 200
    ) -> DirectoryMemberPage:
        """The group's direct and nested members, up to ``limit``."""
        ...

    async def close(self) -> None: ...


from mosaic_api.integrations.graph.client import GraphDirectoryLookup  # noqa: E402
from mosaic_api.integrations.graph.fake import FakeDirectoryLookup  # noqa: E402

__all__ = ["DirectoryLookup", "FakeDirectoryLookup", "GraphDirectoryLookup"]
