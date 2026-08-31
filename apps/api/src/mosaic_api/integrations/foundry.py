from dataclasses import dataclass
from typing import Protocol

from mosaic_api.domain import FoundryConnection, ModelDeployment


@dataclass(frozen=True)
class ConnectionValidation:
    reachable: bool
    resource_id_matches: bool
    message: str


class FoundryImporter(Protocol):
    async def validate_connection(self, connection: FoundryConnection) -> ConnectionValidation: ...

    async def discover_deployments(
        self, connection: FoundryConnection
    ) -> list[ModelDeployment]: ...
