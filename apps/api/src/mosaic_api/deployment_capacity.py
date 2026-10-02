"""What capacity a model deployment runs on, and where Azure may process its requests.

Both attributes are read from the deployment's SKU name, as ADR 0024 sets out. Azure fixes them when
the deployment is created, so MOSAIC derives them rather than asking anyone to configure them. An
AWS Bedrock model has no SKU, so they're read from its model ID instead.
"""

import re
from enum import StrEnum


class CapacityType(StrEnum):
    """How a deployment's capacity is bought.

    ``provisioned`` is reserved throughput (PTU). ``payAsYouGo`` is billed per token. ``batch``
    serves asynchronous batch jobs only, so it can't answer a synchronous call.
    """

    PROVISIONED = "provisioned"
    PAY_AS_YOU_GO = "payAsYouGo"
    BATCH = "batch"
    UNKNOWN = "unknown"


class ProcessingScope(StrEnum):
    """Where a deployment's requests may be processed: any region, one data zone, or its own.

    For an AWS Bedrock model, a data zone is the geography its cross-region inference profile
    names, such as the US or the EU.
    """

    GLOBAL = "global"
    DATA_ZONE = "dataZone"
    REGIONAL = "regional"
    UNKNOWN = "unknown"


_BY_SKU: dict[str, tuple[CapacityType, ProcessingScope]] = {
    "standard": (CapacityType.PAY_AS_YOU_GO, ProcessingScope.REGIONAL),
    "globalstandard": (CapacityType.PAY_AS_YOU_GO, ProcessingScope.GLOBAL),
    "datazonestandard": (CapacityType.PAY_AS_YOU_GO, ProcessingScope.DATA_ZONE),
    "provisionedmanaged": (CapacityType.PROVISIONED, ProcessingScope.REGIONAL),
    "globalprovisionedmanaged": (CapacityType.PROVISIONED, ProcessingScope.GLOBAL),
    "datazoneprovisionedmanaged": (CapacityType.PROVISIONED, ProcessingScope.DATA_ZONE),
    "globalbatch": (CapacityType.BATCH, ProcessingScope.GLOBAL),
    "datazonebatch": (CapacityType.BATCH, ProcessingScope.DATA_ZONE),
}
_UNKNOWN = (CapacityType.UNKNOWN, ProcessingScope.UNKNOWN)


def classify_sku(sku_name: str | None) -> tuple[CapacityType, ProcessingScope]:
    """Map a deployment's SKU name to its capacity type and processing scope.

    A missing or unrecognized SKU is unknown on both counts rather than a guess. A pool puts
    provisioned members first, so it must never promote a deployment it can't classify.
    """

    if not sku_name:
        return _UNKNOWN
    return _BY_SKU.get(sku_name.strip().casefold(), _UNKNOWN)


# A cross-region inference profile's ID starts with the geography AWS may route it within, such
# as us. or eu., or with global. for any commercial region.
_BEDROCK_PROFILE = re.compile(r"^(?P<geography>[a-z][a-z-]*)\.anthropic\.")


def classify_bedrock_model(model_id: str) -> tuple[CapacityType, ProcessingScope]:
    """Map an AWS Bedrock model ID to its capacity type and processing scope.

    Every ID MOSAIC accepts is on demand: provisioned throughput is called by its ARN, which
    MOSAIC refuses. A bare ``anthropic.`` model is served in the endpoint's own region. An ID it
    can't place is of unknown scope rather than a guess.
    """

    folded = model_id.strip().casefold()
    if folded.startswith("anthropic."):
        return CapacityType.PAY_AS_YOU_GO, ProcessingScope.REGIONAL
    match = _BEDROCK_PROFILE.match(folded)
    if match is None:
        return CapacityType.PAY_AS_YOU_GO, ProcessingScope.UNKNOWN
    if match["geography"] == "global":
        return CapacityType.PAY_AS_YOU_GO, ProcessingScope.GLOBAL
    return CapacityType.PAY_AS_YOU_GO, ProcessingScope.DATA_ZONE


__all__ = ["CapacityType", "ProcessingScope", "classify_bedrock_model", "classify_sku"]
