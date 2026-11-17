from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from mosaic_api.domain import (
    EndpointAccess,
    GatewayRuntimeAccess,
    ModelEndpoint,
    ReadinessFreshness,
)
from mosaic_api.endpoint_readiness import readiness_freshness

# Fields authored by administrators must survive stale preflight/sync writes. Observation writes
# still refresh every other field, which closes the existing race for names, labels, and now
# environment classification.
GATEWAY_AUTHORED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "created_at",
        "name",
        "environment_label",
        "environment",
        "management_mode",
        "azure_cloud",
        "azure_connection_id",
    }
)

MODEL_ENDPOINT_AUTHORED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "created_at",
        "name",
        "environment_label",
        "environment",
        "credential_reference_id",
        "declared_deployments",
        "management_mode",
        "azure_cloud",
        "azure_connection_id",
    }
)

MCP_ENDPOINT_AUTHORED_FIELDS: frozenset[str] = frozenset(
    {
        "id",
        "tenant_id",
        "created_at",
        "name",
        "environment_label",
        "environment",
        "credential_reference_id",
        "resource_audience",
        "azure_cloud",
        "azure_connection_id",
    }
)


def merge_observation[EntityT: BaseModel](
    stored: EntityT, incoming: EntityT, authored_fields: Collection[str]
) -> EntityT:
    """Merge observed state into the latest stored entity while preserving authored fields."""

    preserved = {field: getattr(stored, field) for field in authored_fields}
    preserved["etag"] = cast(Any, stored).etag
    if isinstance(stored, ModelEndpoint) and isinstance(incoming, ModelEndpoint):
        if _check_order(stored.access) > _check_order(incoming.access):
            preserved["access"] = stored.access
        if _runtime_snapshot_order(stored) > _runtime_snapshot_order(incoming):
            preserved["runtime_access"] = stored.runtime_access
        stored_read = stored.detail_evidence.account_read_at if stored.detail_evidence else None
        incoming_read = (
            incoming.detail_evidence.account_read_at if incoming.detail_evidence else None
        )
        if _observed_at(stored_read) > _observed_at(incoming_read):
            for field in (
                "endpoint", "provider", "detail_evidence", "capabilities", "azure_environment_tag"
            ):
                preserved[field] = getattr(stored, field)
    return incoming.model_copy(update=preserved)


def _observed_at(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _check_order(access: EndpointAccess | GatewayRuntimeAccess) -> tuple[bool, datetime]:
    # A legacy/skewed timestamp must not prevent an explicit current-rules check.
    return (
        readiness_freshness(access) == ReadinessFreshness.CURRENT,
        _observed_at(access.checked_at),
    )


def _runtime_snapshot_order(endpoint: ModelEndpoint) -> tuple[bool, datetime]:
    # Full preflight stamps MOSAIC's check even when its gateway snapshot is empty.
    return max([_check_order(endpoint.access), *map(_check_order, endpoint.runtime_access)])
