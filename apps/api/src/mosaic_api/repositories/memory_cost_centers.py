from datetime import datetime
from uuid import uuid4

from mosaic_api.cost_centers import CostCenter, CostCenterSettings
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.managed_cost_center_groups import (
    linked_cost_center,
    managed_group,
    membership_changed,
)
from mosaic_api.repositories.memory import InMemoryDirectoryRepository

COST_CENTER_CHANGED = "The cost center changed; reload it and try again"
SETTINGS_CHANGED = "The cost center settings changed; reload them and try again"


class InMemoryCostCenterRepository:
    """Cost centers for local runs and tests. Every read returns a copy."""

    def __init__(
        self,
        directory_repository: InMemoryDirectoryRepository | None = None,
        *,
        managed_groups_enabled: bool = False,
    ) -> None:
        if managed_groups_enabled and directory_repository is None:
            raise ValueError("Managed cost-center groups require the shared directory repository")
        self._directory = directory_repository
        self._managed_groups_enabled = managed_groups_enabled
        self.cost_centers: dict[tuple[str, str], CostCenter] = {}
        self.settings: dict[str, CostCenterSettings] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def list_cost_centers(self, tenant_id: str) -> list[CostCenter]:
        return sorted(
            (
                item.model_copy(deep=True)
                for (tenant, _), item in self.cost_centers.items()
                if tenant == tenant_id
            ),
            key=lambda item: (item.name.casefold(), item.code.casefold(), item.id),
        )

    async def get_cost_center(self, tenant_id: str, cost_center_id: str) -> CostCenter | None:
        item = self.cost_centers.get((tenant_id, cost_center_id))
        return None if item is None else item.model_copy(deep=True)

    async def create_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        key = (cost_center.tenant_id, cost_center.id)
        if key in self.cost_centers:
            raise ConflictError("A cost center with this ID already exists")
        if self._managed_groups_enabled:
            cost_center = linked_cost_center(cost_center)
        group = managed_group(cost_center) if cost_center.group_id is not None else None
        if group is not None:
            if self._directory is None:
                raise ConflictError("The managed-group directory repository is not configured")
            if group.id in self._directory.groups:
                raise ConflictError("The managed-group identifier is already in use")
        stored = cost_center.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.cost_centers[key] = stored
        if group is not None and self._directory is not None:
            self._directory.groups[group.id] = group.model_copy(update={"etag": uuid4().hex})
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def save_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        key = (cost_center.tenant_id, cost_center.id)
        current = self.cost_centers.get(key)
        if current is None or current.etag != cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        cost_center = membership_changed(current, cost_center)
        group = None
        if cost_center.group_id is not None:
            if self._directory is None:
                raise ConflictError("The managed-group directory repository is not configured")
            previous = self._directory.groups.get(cost_center.group_id)
            if current.group_id is not None and previous is None:
                raise ConflictError("The cost center's managed group is missing")
            if current.group_id is None and previous is not None:
                raise ConflictError("The managed-group identifier is already in use")
            group = managed_group(cost_center, previous)
        stored = cost_center.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.cost_centers[key] = stored
        if group is not None and self._directory is not None:
            self._directory.groups[group.id] = group.model_copy(update={"etag": uuid4().hex})
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def member_removal_recorded(
        self,
        tenant_id: str,
        cost_center_id: str,
        principal_id: str,
        *,
        not_before: datetime | None = None,
    ) -> bool:
        return any(
            event.tenant_id == tenant_id
            and event.action == "costCenter.memberRemoved"
            and event.resource_type == "costCenter"
            and event.resource_id == cost_center_id
            and event.details.get("principalId") == principal_id
            and event.details.get("listed") is True
            and (not_before is None or event.created_at >= not_before)
            for event in self.audit_events.values()
        )

    async def delete_cost_center(self, cost_center: CostCenter, audit_event: AuditEvent) -> None:
        key = (cost_center.tenant_id, cost_center.id)
        current = self.cost_centers.get(key)
        if (
            current is None
            or current.etag != cost_center.etag
            or current.group_id != cost_center.group_id
        ):
            raise ConflictError(COST_CENTER_CHANGED)
        if current.group_id is not None:
            if self._directory is None:
                raise ConflictError("The managed-group directory repository is not configured")
            group = self._directory.groups.get(current.group_id)
            if group is None:
                raise ConflictError("The cost center's managed group is missing")
            managed_group(current, group)
            del self._directory.groups[group.id]
        del self.cost_centers[key]
        self.audit_events[audit_event.id] = audit_event

    async def get_settings(self, tenant_id: str) -> CostCenterSettings | None:
        item = self.settings.get(tenant_id)
        return None if item is None else item.model_copy(deep=True)

    async def save_settings(
        self, settings: CostCenterSettings, audit_event: AuditEvent
    ) -> CostCenterSettings:
        current = self.settings.get(settings.tenant_id)
        if (current is None) != (settings.etag is None) or (
            current is not None and current.etag != settings.etag
        ):
            raise ConflictError(SETTINGS_CHANGED)
        stored = settings.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.settings[settings.tenant_id] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)
