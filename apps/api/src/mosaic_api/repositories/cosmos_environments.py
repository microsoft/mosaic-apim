from collections.abc import Sequence

from azure.cosmos import exceptions
from azure.cosmos.aio import CosmosClient

from mosaic_api.domain import AuditEvent, Gateway, McpEndpoint, ModelEndpoint
from mosaic_api.environments import (
    ENVIRONMENT_CATALOG_ENTITY_TYPE,
    EnvironmentCatalog,
    EnvironmentUsage,
)
from mosaic_api.errors import ConflictError
from mosaic_api.repositories.cosmos import CosmosRepositoryBase


class CosmosEnvironmentRepository(CosmosRepositoryBase):
    """Environment catalog and usage counts in the desired-state container."""

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

    async def get_environment_catalog(self, tenant_id: str) -> EnvironmentCatalog | None:
        return await self._read(
            EnvironmentCatalog,
            tenant_id,
            EnvironmentCatalog.new(tenant_id).id,
        )

    async def save_environment_catalog(
        self, catalog: EnvironmentCatalog, audit_event: AuditEvent
    ) -> EnvironmentCatalog:
        try:
            await self._mutate(
                catalog,
                None,
                audit_event,
                "replace" if catalog.etag else "create",
                conflict_message="The environment catalog changed; reload it and try again",
            )
        except exceptions.CosmosResourceExistsError as exc:
            raise ConflictError("The environment catalog changed; reload it and try again") from exc
        return catalog

    async def count_resources_by_environment(
        self, tenant_id: str
    ) -> dict[str | None, EnvironmentUsage]:
        counts: dict[str | None, EnvironmentUsage] = {}
        for entity_type, field in (
            ("gateway", "gateways"),
            ("modelEndpoint", "model_endpoints"),
            ("mcpEndpoint", "mcp_endpoints"),
        ):
            items = self._desired.query_items(
                query=(
                    "SELECT c.environment AS environment, COUNT(1) AS n "
                    "FROM c WHERE c.tenantId = @tenantId AND c.entityType = @entityType "
                    "GROUP BY c.environment"
                ),
                parameters=[
                    {"name": "@tenantId", "value": tenant_id},
                    {"name": "@entityType", "value": entity_type},
                ],
                partition_key=tenant_id,
            )
            async for item in items:
                # Documents written before environments existed have no `environment` property.
                # Cosmos groups those apart from explicit nulls, so both groups add to Unclassified.
                environment = item.get("environment")
                usage = counts.setdefault(environment, EnvironmentUsage())
                setattr(usage, field, getattr(usage, field) + int(item.get("n") or 0))
        return counts

    async def save_environment_assignments(
        self,
        resources: Sequence[Gateway | ModelEndpoint | McpEndpoint],
        audit_event: AuditEvent,
    ) -> list[Gateway | ModelEndpoint | McpEndpoint]:
        operations = [
            *(self._replace_operation(resource) for resource in resources),
            ("create", (self._document(audit_event),)),
        ]
        await self._execute_batch(
            operations,
            audit_event,
            "The resource changed; reload it and try again",
        )
        return list(resources)


__all__ = ["ENVIRONMENT_CATALOG_ENTITY_TYPE", "CosmosEnvironmentRepository"]
