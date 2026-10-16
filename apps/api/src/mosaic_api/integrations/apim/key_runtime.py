"""Read back the exact key-only runtime before issuing or revealing Government keys."""

import hashlib
import xml.etree.ElementTree as ET

from mosaic_api.domain import MCP_METADATA_API_NAME
from mosaic_api.errors import ConflictError
from mosaic_api.integrations.apim.client import ApimClient
from mosaic_api.integrations.apim.writer import DEFAULT_SUBSCRIPTION_KEY_NAMES


def policy_hash(xml: str) -> str:
    try:
        canonical = ET.canonicalize(xml_data=xml, strip_text=True)
    except ET.ParseError:
        raise ConflictError("APIM returned an invalid Government runtime policy.") from None
    return hashlib.sha256(canonical.encode()).hexdigest()


async def runtime_policy_hashes(
    client: ApimClient,
    *,
    api_name: str,
    fragment_name: str,
    mcp: bool = False,
    metadata_name: str | None = None,
    legacy_metadata: bool = False,
) -> dict[str, str]:
    api = await client.get_mcp_api(api_name) if mcp else await client.get_api(api_name)
    properties = (api or {}).get("properties") or {}
    if (
        properties.get("subscriptionRequired") is not True
        or properties.get("subscriptionKeyParameterNames") != DEFAULT_SUBSCRIPTION_KEY_NAMES
    ):
        raise ConflictError("APIM did not confirm Government subscription-key enforcement.")
    fragment = await client.get_policy_fragment(fragment_name)
    api_policy = (
        await client.get_mcp_api_policy(api_name) if mcp else await client.get_api_policy(api_name)
    )
    if fragment is None or api_policy is None:
        raise ConflictError("The reviewed Government access policy is missing from APIM.")
    hashes = {"fragment": policy_hash(fragment), "api": policy_hash(api_policy)}
    if metadata_name is not None:
        metadata = (
            await client.get_api_policy(metadata_name)
            if legacy_metadata
            else await client.get_operation_policy(MCP_METADATA_API_NAME, metadata_name)
        )
        if metadata is None:
            raise ConflictError("The Government MCP metadata policy is missing from APIM.")
        hashes["metadata"] = policy_hash(metadata)
    return hashes


async def verify_runtime(
    client: ApimClient,
    *,
    api_name: str,
    fragment_name: str,
    policy_hashes: dict[str, str],
    mcp: bool = False,
    metadata_name: str | None = None,
    legacy_metadata: bool = False,
) -> None:
    if not policy_hashes:
        raise ConflictError("Apply the reviewed Government policy before provisioning a key.")
    live = await runtime_policy_hashes(
        client,
        api_name=api_name,
        fragment_name=fragment_name,
        mcp=mcp,
        metadata_name=metadata_name,
        legacy_metadata=legacy_metadata,
    )
    if live != policy_hashes:
        raise ConflictError("The Government runtime policy changed. Review and apply it again.")
