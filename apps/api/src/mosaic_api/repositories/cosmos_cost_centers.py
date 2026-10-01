from azure.cosmos import exceptions

from mosaic_api.cost_centers import CostCenter, CostCenterSettings, cost_center_settings_id
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos import CosmosRepositoryBase

COST_CENTER_CHANGED = "The cost center changed; reload it and try again"
SETTINGS_CHANGED = "The cost center settings changed; reload them and try again"


class CosmosCostCenterRepository(CosmosRepositoryBase):
    """Cost centers and their settings in ``desired-state``, each write with its audit event.

    Every replace and delete carries the ETag that was read, so an administrator saving over a
    newer version gets a conflict rather than silently undoing it.
    """

    async def list_cost_centers(self, tenant_id: str) -> list[CostCenter]:
        items = await self._query(CostCenter, tenant_id, "costCenter")
        return sorted(items, key=lambda item: (item.name.casefold(), item.code.casefold(), item.id))

    async def get_cost_center(self, tenant_id: str, cost_center_id: str) -> CostCenter | None:
        return await self._read(CostCenter, tenant_id, cost_center_id)

    async def create_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        await self._mutate(
            cost_center,
            None,
            audit_event,
            "create",
            conflict_message="A cost center with this ID already exists",
        )
        return cost_center

    async def save_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        if not cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        await self._mutate(
            cost_center, None, audit_event, "replace", conflict_message=COST_CENTER_CHANGED
        )
        return cost_center

    async def delete_cost_center(self, cost_center: CostCenter, audit_event: AuditEvent) -> None:
        await self._mutate(
            cost_center,
            cost_center.id,
            audit_event,
            "delete",
            conflict_message=COST_CENTER_CHANGED,
        )

    async def get_settings(self, tenant_id: str) -> CostCenterSettings | None:
        return await self._read(CostCenterSettings, tenant_id, cost_center_settings_id(tenant_id))

    async def save_settings(
        self, settings: CostCenterSettings, audit_event: AuditEvent
    ) -> CostCenterSettings:
        try:
            await self._mutate(
                settings,
                None,
                audit_event,
                "replace" if settings.etag else "create",
                conflict_message=SETTINGS_CHANGED,
            )
        except exceptions.CosmosResourceExistsError as exc:
            raise ConflictError(SETTINGS_CHANGED) from exc
        return settings
