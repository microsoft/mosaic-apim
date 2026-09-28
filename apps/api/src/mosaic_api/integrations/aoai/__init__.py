"""Read-only Azure AI (Cognitive Services) integration."""

from mosaic_api.integrations.aoai.client import CognitiveServicesClient, SubscriptionScanner
from mosaic_api.integrations.aoai.inventory import ModelInventoryCollector, ModelInventorySnapshot
from mosaic_api.integrations.aoai.preflight import (
    MODEL_READ_ACTIONS,
    EndpointPreflightResult,
    build_endpoint_remediation,
    least_privilege_role_definition,
    run_endpoint_preflight,
)
from mosaic_api.integrations.aoai.runtime_access import (
    KNOWN_SUFFICIENT_ROLES,
    RuntimeAccessCheck,
    evaluate_network_path,
    known_sufficient_roles,
    published_shapes,
    recommended_runtime_role,
    required_runtime_data_actions,
    verify_gateway_runtime_access,
)

__all__ = [
    "KNOWN_SUFFICIENT_ROLES",
    "MODEL_READ_ACTIONS",
    "CognitiveServicesClient",
    "EndpointPreflightResult",
    "ModelInventoryCollector",
    "ModelInventorySnapshot",
    "RuntimeAccessCheck",
    "SubscriptionScanner",
    "build_endpoint_remediation",
    "evaluate_network_path",
    "known_sufficient_roles",
    "least_privilege_role_definition",
    "published_shapes",
    "recommended_runtime_role",
    "required_runtime_data_actions",
    "run_endpoint_preflight",
    "verify_gateway_runtime_access",
]
