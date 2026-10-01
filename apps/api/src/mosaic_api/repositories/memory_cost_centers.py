from uuid import uuid4

from mosaic_api.cost_centers import CostCenter, CostCenterSettings
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError

COST_CENTER_CHANGED = "The cost center changed; reload it and try again"
SETTINGS_CHANGED = "The cost center settings changed; reload them and try again"


class InMemoryCostCenterRepository:
    """Cost centers for local runs and tests. Every read returns a copy."""

    def __init__(self) -> None:
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
        stored = cost_center.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.cost_centers[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def save_cost_center(
        self, cost_center: CostCenter, audit_event: AuditEvent
    ) -> CostCenter:
        key = (cost_center.tenant_id, cost_center.id)
        current = self.cost_centers.get(key)
        if current is None or current.etag != cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
        stored = cost_center.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.cost_centers[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def delete_cost_center(self, cost_center: CostCenter, audit_event: AuditEvent) -> None:
        key = (cost_center.tenant_id, cost_center.id)
        current = self.cost_centers.get(key)
        if current is None or current.etag != cost_center.etag:
            raise ConflictError(COST_CENTER_CHANGED)
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
