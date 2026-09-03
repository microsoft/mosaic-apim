"""Read-only Azure API Management integration."""

from mosaic_api.integrations.apim.ai_detection import classify_api, classify_url
from mosaic_api.integrations.apim.client import ApimClient, ArmClient, JsonObject
from mosaic_api.integrations.apim.inventory import InventoryCollector, InventorySnapshot
from mosaic_api.integrations.apim.policy_semantics import (
    MOSAIC_FRAGMENT_PREFIX,
    PolicyAnalysis,
    analyze_policy,
    describe_counter_key,
    summarize_facets,
)
from mosaic_api.integrations.apim.preflight import PreflightResult, build_remediation, run_preflight

__all__ = [
    "MOSAIC_FRAGMENT_PREFIX",
    "ApimClient",
    "ArmClient",
    "InventoryCollector",
    "InventorySnapshot",
    "JsonObject",
    "PolicyAnalysis",
    "PreflightResult",
    "analyze_policy",
    "build_remediation",
    "classify_api",
    "classify_url",
    "describe_counter_key",
    "run_preflight",
    "summarize_facets",
]
