from collections.abc import Collection
from typing import Any, cast

from pydantic import BaseModel

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
    }
)


def merge_observation[EntityT: BaseModel](
    stored: EntityT, incoming: EntityT, authored_fields: Collection[str]
) -> EntityT:
    """Merge observed state into the latest stored entity while preserving authored fields."""

    preserved = {field: getattr(stored, field) for field in authored_fields}
    preserved["etag"] = cast(Any, stored).etag
    return incoming.model_copy(update=preserved)
