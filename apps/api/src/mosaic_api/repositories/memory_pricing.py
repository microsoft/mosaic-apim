from uuid import uuid4

from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.pricing import EndpointPricing, PriceVersion

ENDPOINT_PRICING_CHANGED = "Pricing for this endpoint changed; reload it and try again"


class InMemoryPricingRepository:
    """Administrator-authored prices for local runs and tests. Every read returns a copy."""

    def __init__(self) -> None:
        self.versions: dict[tuple[str, str], PriceVersion] = {}
        self.endpoints: dict[tuple[str, str], EndpointPricing] = {}
        self.audit_events: dict[str, AuditEvent] = {}

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def list_price_versions(self, tenant_id: str) -> list[PriceVersion]:
        return [
            version.model_copy(deep=True)
            for (tenant, _), version in sorted(self.versions.items())
            if tenant == tenant_id
        ]

    async def create_price_version(
        self, version: PriceVersion, audit_event: AuditEvent
    ) -> PriceVersion:
        key = (version.tenant_id, version.id)
        if key in self.versions:
            raise ConflictError("That price version already exists")
        stored = version.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.versions[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)

    async def list_endpoint_pricing(self, tenant_id: str) -> list[EndpointPricing]:
        return [
            settings.model_copy(deep=True)
            for (tenant, _), settings in sorted(self.endpoints.items())
            if tenant == tenant_id
        ]

    async def get_endpoint_pricing(
        self, tenant_id: str, endpoint_id: str
    ) -> EndpointPricing | None:
        settings = self.endpoints.get((tenant_id, endpoint_id))
        return None if settings is None else settings.model_copy(deep=True)

    async def save_endpoint_pricing(
        self, settings: EndpointPricing, audit_event: AuditEvent
    ) -> EndpointPricing:
        key = (settings.tenant_id, settings.endpoint_id)
        current = self.endpoints.get(key)
        if (current is None) != (settings.etag is None) or (
            current is not None and current.etag != settings.etag
        ):
            raise ConflictError(ENDPOINT_PRICING_CHANGED)
        stored = settings.model_copy(update={"etag": uuid4().hex}, deep=True)
        self.endpoints[key] = stored
        self.audit_events[audit_event.id] = audit_event
        return stored.model_copy(deep=True)
