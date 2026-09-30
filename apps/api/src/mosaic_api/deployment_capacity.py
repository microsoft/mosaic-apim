"""What capacity a model deployment runs on, and where Azure may process its requests.

Both attributes are read from the deployment's SKU name, as ADR 0018 sets out. Azure fixes them when
the deployment is created, so MOSAIC derives them rather than asking anyone to configure them.
"""

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
    """Where Azure may process a deployment's requests: any region, one data zone, or its own."""

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


__all__ = ["CapacityType", "ProcessingScope", "classify_sku"]
