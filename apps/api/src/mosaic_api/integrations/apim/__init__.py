"""Azure API Management integration.

Reads and writes are separate classes on purpose: :class:`ApimClient` is read-only by
construction, and :class:`ApimWriter` is the only way MOSAIC changes a gateway. See ADR 0010.
"""

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
from mosaic_api.integrations.apim.writer import ApimWriter

__all__ = [
    "MOSAIC_FRAGMENT_PREFIX",
    "ApimClient",
    "ApimWriter",
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
