from azure.cosmos import exceptions

from mosaic_api.domain import AuditEvent
from mosaic_api.errors import ConflictError
from mosaic_api.pricing import EndpointPricing, PriceVersion, endpoint_pricing_id
from mosaic_api.repositories.cosmos import CosmosRepositoryBase

ENDPOINT_PRICING_CHANGED = "Pricing for this endpoint changed; reload it and try again"


class CosmosPricingRepository(CosmosRepositoryBase):
    """Price versions and endpoint pricing facts in ``desired-state``, each with its audit event.

    A price version is created in one transactional batch with its audit event and never
    replaced. Endpoint pricing is replaced only over the version that was read, so two
    administrators saving at once can't silently undo each other.
    """

    async def list_price_versions(self, tenant_id: str) -> list[PriceVersion]:
        items = await self._query(PriceVersion, tenant_id, "priceVersion")
        return sorted(items, key=lambda item: (item.created_at, item.id))

    async def create_price_version(
        self, version: PriceVersion, audit_event: AuditEvent
    ) -> PriceVersion:
        await self._mutate(
            version,
            None,
            audit_event,
            "create",
            conflict_message="That price version already exists",
        )
        return version

    async def list_endpoint_pricing(self, tenant_id: str) -> list[EndpointPricing]:
        return await self._query(EndpointPricing, tenant_id, "endpointPricing")

    async def get_endpoint_pricing(
        self, tenant_id: str, endpoint_id: str
    ) -> EndpointPricing | None:
        return await self._read(
            EndpointPricing, tenant_id, endpoint_pricing_id(tenant_id, endpoint_id)
        )

    async def save_endpoint_pricing(
        self, settings: EndpointPricing, audit_event: AuditEvent
    ) -> EndpointPricing:
        try:
            await self._mutate(
                settings,
                None,
                audit_event,
                "replace" if settings.etag else "create",
                conflict_message=ENDPOINT_PRICING_CHANGED,
            )
        except exceptions.CosmosResourceExistsError as exc:
            raise ConflictError(ENDPOINT_PRICING_CHANGED) from exc
        return settings
