from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient

from mosaic_api.cost_centers import CostCenter, CostCenterSettings, cost_center_settings_id
from mosaic_api.domain import AuditEvent, Group
from mosaic_api.errors import ConflictError
from mosaic_api.managed_cost_center_groups import (
    linked_cost_center,
    managed_group,
    membership_changed,
)
from mosaic_api.repositories.cosmos import CosmosRepositoryBase

COST_CENTER_CHANGED = "The cost center changed; reload it and try again"
SETTINGS_CHANGED = "The cost center settings changed; reload them and try again"


class CosmosCostCenterRepository(CosmosRepositoryBase):
    """Cost centers and their settings in ``desired-state``, each write with its audit event.

    Every replace and delete carries the ETag that was read, so an administrator saving over a
    newer version gets a conflict rather than silently undoing it.
    """

    def __init__(
        self,
        client: CosmosClient,
        database_name: str,
        desired_state_container: str,
        audit_events_container: str,
        *,
        owns_client: bool = True,
        managed_groups_enabled: bool = False,
    ) -> None:
        super().__init__(
            client, database_name, desired_state_container, audit_events_container,
            owns_client=owns_client,
        )
        self._managed_groups_enabled = managed_groups_enabled

    async def list_cost_centers(self, tenant_id: str) -> list[CostCenter]:
        items = await self._query(CostCenter, tenant_id, "costCenter")
        return sorted(items, key=lambda item: (item.name.casefold(), item.code.casefold(), item.id))

    async def get_cost_center(self, tenant_id: str, cost_center_id: str) -> CostCenter | None:
        return await self._read(CostCenter, tenant_id, cost_center_id)

    async def create_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        if self._managed_groups_enabled:
            cost_center = linked_cost_center(cost_center)
        if cost_center.group_id is not None:
            group = managed_group(cost_center)
            await self._execute_batch(
                [
                    ("create", (self._document(cost_center),)),
                    ("create", (self._document(group),)),
                    ("create", (self._document(audit_event),)),
                ],
                audit_event,
                conflict_message="The cost center or its managed group already exists",
            )
            return cost_center
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
        current = await self.get_cost_center(cost_center.tenant_id, cost_center.id)
        if current is None or current.etag != cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        cost_center = membership_changed(current, cost_center)
        if cost_center.group_id is not None:
            previous = await self._read(Group, cost_center.tenant_id, cost_center.group_id)
            if current.group_id is not None and previous is None:
                raise ConflictError("The cost center's managed group is missing")
            if current.group_id is None and previous is not None:
                raise ConflictError("The managed-group identifier is already in use")
            group = managed_group(cost_center, previous)
            await self._execute_batch(
                [
                    self._replace_operation(cost_center),
                    self._replace_operation(group)
                    if previous is not None
                    else ("create", (self._document(group),)),
                    ("create", (self._document(audit_event),)),
                ],
                audit_event,
                conflict_message=COST_CENTER_CHANGED,
            )
            return cost_center
        await self._mutate(
            cost_center, None, audit_event, "replace", conflict_message=COST_CENTER_CHANGED
        )
        return cost_center

    async def delete_cost_center(self, cost_center: CostCenter, audit_event: AuditEvent) -> None:
        current = await self.get_cost_center(cost_center.tenant_id, cost_center.id)
        if (
            not cost_center.etag
            or current is None
            or current.etag != cost_center.etag
            or current.group_id != cost_center.group_id
        ):
            raise ConflictError(COST_CENTER_CHANGED)
        if cost_center.group_id is not None:
            group = await self._read(Group, cost_center.tenant_id, cost_center.group_id)
            if group is None:
                raise ConflictError("The cost center's managed group is missing")
            managed_group(cost_center, group)
            await self._execute_batch(
                [
                    self._delete_operation(cost_center.id, cost_center),
                    self._delete_operation(group.id, group),
                    ("create", (self._document(audit_event),)),
                ],
                audit_event,
                conflict_message=COST_CENTER_CHANGED,
            )
            return
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
