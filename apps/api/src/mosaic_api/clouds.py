"""Closed Azure cloud profiles, independent of persistence and application identity."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

AzureCloud = Literal["commercial", "government"]
ConnectionValidator = Callable[[str, str | None, AzureCloud], Awaitable[None]]


@dataclass(frozen=True)
class AzureResourceContext:
    azure_cloud: AzureCloud = "commercial"
    azure_connection_id: str | None = None


@dataclass(frozen=True)
class CloudProfile:
    authority: str
    arm_endpoint: str
    arm_scope: str
    log_analytics_endpoint: str
    log_analytics_scope: str
    portal_endpoint: str
    cognitive_audience: str
    azure_ai_suffixes: tuple[str, ...]
    apim_suffix: str
    key_vault_suffix: str


_COMMERCIAL = CloudProfile(
    authority="https://login.microsoftonline.com",
    arm_endpoint="https://management.azure.com",
    arm_scope="https://management.azure.com/.default",
    log_analytics_endpoint="https://api.loganalytics.io",
    log_analytics_scope="https://api.loganalytics.io/.default",
    portal_endpoint="https://portal.azure.com",
    cognitive_audience="https://cognitiveservices.azure.com",
    azure_ai_suffixes=(
        ".services.ai.azure.com", ".cognitiveservices.azure.com", ".openai.azure.com",
    ),
    apim_suffix=".azure-api.net",
    key_vault_suffix=".vault.azure.net",
)
_GOVERNMENT = CloudProfile(
    authority="https://login.microsoftonline.us",
    arm_endpoint="https://management.usgovcloudapi.net",
    arm_scope="https://management.usgovcloudapi.net/.default",
    log_analytics_endpoint="https://api.loganalytics.us",
    log_analytics_scope="https://api.loganalytics.us/.default",
    portal_endpoint="https://portal.azure.us",
    cognitive_audience="https://cognitiveservices.azure.us",
    # Foundry inference/Anthropic hosts are deliberately not inferred from commercial names.
    azure_ai_suffixes=(".cognitiveservices.azure.us", ".openai.azure.us"),
    apim_suffix=".azure-api.us",
    key_vault_suffix=".vault.usgovcloudapi.net",
)


def cloud_profile(cloud: AzureCloud) -> CloudProfile:
    if cloud == "commercial":
        return _COMMERCIAL
    if cloud == "government":
        return _GOVERNMENT
    raise ValueError("Unsupported Azure cloud")


def azure_ai_cloud(url: str | None) -> AzureCloud | None:
    """Only recognize verified account-host suffixes, never arbitrary lookalikes."""

    try:
        host = (urlsplit(url or "").hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    for cloud in ("commercial", "government"):
        if any(host.endswith(suffix) for suffix in cloud_profile(cloud).azure_ai_suffixes):
            return cloud
    return None


def known_azure_host_cloud(url: str | None) -> AzureCloud | None:
    """Identify known service hosts only; an operator may separately classify custom domains."""

    try:
        host = (urlsplit(url or "").hostname or "").casefold().rstrip(".")
    except ValueError:
        return None
    for cloud in ("commercial", "government"):
        profile = cloud_profile(cloud)
        if any(host.endswith(suffix) for suffix in (
            *profile.azure_ai_suffixes, profile.apim_suffix, profile.key_vault_suffix,
        )):
            return cloud
    return None


def cloud_qualified_key(value: str, cloud: AzureCloud) -> str:
    """Preserve existing commercial IDs while isolating otherwise identical sovereign targets."""

    return value if cloud == "commercial" else f"{cloud}:{value}"


def management_identity_label(cloud: AzureCloud, connection_id: str | None) -> str:
    return (
        "MOSAIC's management service principal"
        if cloud != "commercial" or connection_id is not None
        else "MOSAIC's managed identity"
    )


async def validate_resource_connection(
    validator: ConnectionValidator | None,
    tenant_id: str,
    connection_id: str | None,
    cloud: AzureCloud,
) -> None:
    from mosaic_api.errors import ValidationError

    cloud_profile(cloud)
    if cloud == "government" and not connection_id:
        raise ValidationError("Azure Government resources require an Azure management connection.")
    if validator is not None:
        await validator(tenant_id, connection_id, cloud)
    elif cloud != "commercial" or connection_id is not None:
        raise ValidationError("This deployment cannot resolve the selected Azure connection.")
