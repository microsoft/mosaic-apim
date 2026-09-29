from collections.abc import Sequence
from uuid import uuid4

from mosaic_api.domain import AuditEvent, Gateway, McpEndpoint, ModelEndpoint
from mosaic_api.environments import EnvironmentCatalog, EnvironmentUsage
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.memory_endpoints import InMemoryModelEndpointRepository
from mosaic_api.repositories.memory_gateway import InMemoryGatewayRepository
from mosaic_api.repositories.memory_mcp_endpoints import InMemoryMcpEndpointRepository


class InMemoryEnvironmentRepository:
    """Environment catalog persistence for local runs and tests."""

    def __init__(
        self,
        gateway_repository: InMemoryGatewayRepository,
        endpoint_repository: InMemoryModelEndpointRepository,
        mcp_repository: InMemoryMcpEndpointRepository,
    ) -> None:
        self.catalogs: dict[str, EnvironmentCatalog] = {}
        self.audit_events: dict[str, AuditEvent] = {}
        self._gateways = gateway_repository
        self._endpoints = endpoint_repository
        self._mcp = mcp_repository

    async def ready(self) -> bool:
        return True

    async def close(self) -> None:
        return None

    async def get_environment_catalog(self, tenant_id: str) -> EnvironmentCatalog | None:
        catalog = self.catalogs.get(tenant_id)
        return catalog if catalog and catalog.tenant_id == tenant_id else None

    async def save_environment_catalog(
        self, catalog: EnvironmentCatalog, audit_event: AuditEvent
    ) -> EnvironmentCatalog:
        existing = self.catalogs.get(catalog.tenant_id)
        if existing and existing.etag != catalog.etag:
            raise ConflictError("The environment catalog changed; reload it and try again")
        saved = catalog.model_copy(update={"etag": uuid4().hex})
        self.catalogs[catalog.tenant_id] = saved
        self.audit_events[audit_event.id] = audit_event
        return saved

    async def count_resources_by_environment(
        self, tenant_id: str
    ) -> dict[str | None, EnvironmentUsage]:
        counts: dict[str | None, EnvironmentUsage] = {}

        def bucket(environment: str | None) -> EnvironmentUsage:
            return counts.setdefault(environment, EnvironmentUsage())

        for gateway in await self._gateways.list_gateways(tenant_id):
            bucket(gateway.environment).gateways += 1
        for endpoint in await self._endpoints.list_endpoints(tenant_id):
            bucket(endpoint.environment).model_endpoints += 1
        for mcp_endpoint in await self._mcp.list_endpoints(tenant_id):
            bucket(mcp_endpoint.environment).mcp_endpoints += 1
        return counts

    async def save_environment_assignments(
        self,
        resources: Sequence[Gateway | ModelEndpoint | McpEndpoint],
        audit_event: AuditEvent,
    ) -> list[Gateway | ModelEndpoint | McpEndpoint]:
        for resource in resources:
            if resource.tenant_id != audit_event.tenant_id:
                raise ConflictError("The resource changed; reload it and try again")
            current: Gateway | ModelEndpoint | McpEndpoint | None
            if isinstance(resource, Gateway):
                current = self._gateways.gateways.get(resource.id)
            elif isinstance(resource, ModelEndpoint):
                current = self._endpoints.endpoints.get(resource.id)
            else:
                current = self._mcp.endpoints.get(resource.id)
            if current is None or current.tenant_id != audit_event.tenant_id:
                raise ConflictError("The resource changed; reload it and try again")
            if resource.etag is not None and current.etag != resource.etag:
                raise ConflictError("The resource changed; reload it and try again")

        saved: list[Gateway | ModelEndpoint | McpEndpoint] = []
        for resource in resources:
            updated = resource.model_copy(update={"etag": uuid4().hex})
            if isinstance(updated, Gateway):
                self._gateways.gateways[updated.id] = updated
                self._gateways._gateway_versions[updated.id] = (
                    self._gateways._gateway_versions.get(updated.id, 0) + 1
                )
            elif isinstance(updated, ModelEndpoint):
                self._endpoints.endpoints[updated.id] = updated
                self._endpoints._endpoint_versions[updated.id] = (
                    self._endpoints._endpoint_versions.get(updated.id, 0) + 1
                )
            else:
                self._mcp.endpoints[updated.id] = updated
                self._mcp._endpoint_versions[updated.id] = (
                    self._mcp._endpoint_versions.get(updated.id, 0) + 1
                )
            saved.append(updated)
        self.audit_events[audit_event.id] = audit_event
        return saved
