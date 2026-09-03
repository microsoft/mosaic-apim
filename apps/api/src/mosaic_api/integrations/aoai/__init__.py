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
    required_runtime_role,
    verify_gateway_runtime_access,
)

__all__ = [
    "MODEL_READ_ACTIONS",
    "CognitiveServicesClient",
    "EndpointPreflightResult",
    "ModelInventoryCollector",
    "ModelInventorySnapshot",
    "SubscriptionScanner",
    "build_endpoint_remediation",
    "least_privilege_role_definition",
    "required_runtime_role",
    "run_endpoint_preflight",
    "verify_gateway_runtime_access",
]
