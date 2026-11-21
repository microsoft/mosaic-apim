from mosaic_api.appearance import (
    AppearanceSettings,
    AppearanceUpdate,
    AppearanceView,
    default_appearance,
)
from mosaic_api.domain import AuditEvent, new_id, utc_now
from mosaic_api.repositories.base import AppearanceRepository
from mosaic_api.services.directory import Actor


class AppearanceService:
    """Reads and replaces the organization's colour scheme."""

    def __init__(self, repository: AppearanceRepository) -> None:
        self._repository = repository

    async def get(self, actor: Actor) -> AppearanceSettings:
        stored = await self._repository.get_appearance(actor.tenant_id)
        return stored or default_appearance(actor.tenant_id)

    async def view(self, actor: Actor) -> AppearanceView:
        settings = await self.get(actor)
        return AppearanceView(
            scheme=settings.scheme, allow_custom_themes=settings.allow_custom_themes
        )

    async def update(self, actor: Actor, payload: AppearanceUpdate) -> AppearanceSettings:
        current = await self.get(actor)
        settings = current.model_copy(
            update={
                "scheme": payload.scheme,
                "allow_custom_themes": payload.allow_custom_themes,
                "updated_at": utc_now(),
            }
        )
        # The colours are not sensitive, but who changed everyone's interface is worth keeping.
        audit_event = AuditEvent(
            id=new_id("audit"),
            tenant_id=actor.tenant_id,
            action="appearance.settingsUpdated",
            resource_type="appearanceSettings",
            resource_id=settings.id,
            actor_object_id=actor.object_id,
            details={
                "customScheme": settings.scheme is not None,
                "allowCustomThemes": settings.allow_custom_themes,
            },
        )
        return await self._repository.save_appearance(settings, audit_event)
