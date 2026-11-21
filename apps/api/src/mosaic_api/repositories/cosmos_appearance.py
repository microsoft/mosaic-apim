from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient

from mosaic_api.appearance import AppearanceSettings, appearance_settings_id
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos import CosmosRepositoryBase
from mosaic_api.repositories.memory_appearance import APPEARANCE_CHANGED


class CosmosAppearanceRepository(CosmosRepositoryBase):
    """Appearance settings in the desired-state container."""

    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
        *,
        owns_client: bool = False,
    ) -> None:
        super().__init__(
            client,
            database_name,
            desired_state_container,
            audit_events_container,
            owns_client=owns_client,
        )

    async def get_appearance(self, tenant_id: str) -> AppearanceSettings | None:
        return await self._read(AppearanceSettings, tenant_id, appearance_settings_id(tenant_id))

    async def save_appearance(
        self, settings: AppearanceSettings, audit_event: AuditEvent
    ) -> AppearanceSettings:
        try:
            await self._mutate(
                settings,
                None,
                audit_event,
                "replace" if settings.etag else "create",
                conflict_message=APPEARANCE_CHANGED,
            )
        except exceptions.CosmosResourceExistsError as exc:
            raise ConflictError(APPEARANCE_CHANGED) from exc
        return settings
