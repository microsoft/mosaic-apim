from uuid import uuid4

from mosaic_api.appearance import AppearanceSettings
from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError

APPEARANCE_CHANGED = "The appearance settings changed; reload them and try again"


class InMemoryAppearanceRepository:
    """Appearance settings for local runs and tests."""

    def __init__(self) -> None:
        self.settings: dict[str, AppearanceSettings] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_appearance(self, tenant_id: str) -> AppearanceSettings | None:
        item = self.settings.get(tenant_id)
        return None if item is None else item.model_copy(deep=True)

    async def save_appearance(
        self, settings: AppearanceSettings, audit_event: AuditEvent
    ) -> AppearanceSettings:
        current = self.settings.get(settings.tenant_id)
        if (current is None) != (settings.etag is None) or (
            current is not None and current.etag != settings.etag
        ):
            raise ConflictError(APPEARANCE_CHANGED)
        stored = settings.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.settings[settings.tenant_id] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)
