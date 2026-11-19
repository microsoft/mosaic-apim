"""The deliberately bounded standalone Bedrock Messages contract; pools remain independent."""

import hashlib
import json
import xml.etree.ElementTree as ET

from mosaic_api.domain import (
    Gateway,
    GatewayTier,
    ModelEndpoint,
    ModelProvider,
    Publication,
    gateway_tier,
)
from mosaic_api.errors import ValidationError
from mosaic_api.integrations.apim.client import ApimClient

STANDALONE_ORIGIN = "https://bedrock-mantle.us-east-1.api.aws"
STANDALONE_MODEL = "anthropic.claude-sonnet-5"
MESSAGES_PATH = "/anthropic/v1/messages"
BEDROCK_NOTES = (
    "AWS processes prompts, images and tool content outside Azure, in us-east-1 for this route.",
    "The API key is operator-managed. AWS recommends short-term keys for production and "
    "long-term keys only for exploration. MOSAIC does not renew keys; API Management may take "
    "four hours to refresh a rotated Key Vault secret.",
    "MOSAIC has not verified AWS accepts the key or model access. Registration and Apply "
    "do not invoke AWS. A usable connection needs an applied grant and valid upstream access.",
    "Streaming and image token limits are estimates, not an exact spending cap. Thinking "
    "consumes output tokens. Cache reads and writes have different prices; aggregate gateway "
    "logs do not provide the breakdown needed to calculate complete Bedrock costs.",
    "This route replaces the inherited default backend forward with one unbuffered forward "
    "and never follows redirects. Custom inherited policies require separate reconciliation.",
)


def is_standalone(publication: Publication) -> bool:
    return publication.provider == ModelProvider.AWS_BEDROCK


def standalone_reason(endpoint: ModelEndpoint, model: str, gateway: Gateway) -> str | None:
    if str(endpoint.endpoint).rstrip("/") != STANDALONE_ORIGIN or model != STANDALONE_MODEL:
        return (
            "Standalone Bedrock publication currently supports only anthropic.claude-sonnet-5 "
            "on https://bedrock-mantle.us-east-1.api.aws. Other declared Bedrock models can "
            "still be used through a model pool."
        )
    if gateway_tier(gateway.capabilities.sku_name) != GatewayTier.V2:
        return "Standalone governed Bedrock Messages requires an API Management v2 gateway."
    return None


def _checked_policy(xml: str | None, scope: str) -> str:
    if xml is None:
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise ValidationError(f"Cannot inspect {scope}'s Bedrock streaming policy.") from None
    # Keep inherited authorization/rate policies only when their semantics are known. In
    # particular, do not bypass custom backend behavior or inherit response-body inspection.
    safe_inbound = {"base", "rate-limit", "rate-limit-by-key", "quota", "quota-by-key"}
    safe = root.tag == "policies" and not root.attrib and not (root.text or "").strip()
    for section in root:
        if section.tag not in {"inbound", "backend", "outbound", "on-error"}:
            safe = False
        allowed = safe_inbound if section.tag == "inbound" else {"base"}
        safe = safe and not section.attrib and not (section.text or "").strip()
        if section.tag == "backend" and len(section) > 1:
            safe = False
        for element in section:
            if section.tag == "backend" and element.tag == "forward-request":
                safe = safe and not element.attrib and len(element) == 0
            elif section.tag == "inbound" and element.tag == "cors":
                safe = safe and all(
                    child.tag in {
                        "cors", "allowed-origins", "origin", "allowed-methods", "method",
                        "allowed-headers", "header", "expose-headers",
                    }
                    and not any("@" in value for value in child.attrib.values())
                    and "@" not in (child.text or "")
                    for child in element.iter()
                )
            else:
                safe = safe and element.tag in allowed and len(element) == 0
            safe = safe and not (element.text or "").strip()
            safe = safe and not any("@" in value for value in element.attrib.values())
    if not safe:
        raise ValidationError(
            f"{scope} has custom or unrecognized inherited policy behavior. Reconcile it "
            "before publishing Bedrock; MOSAIC will not bypass it or risk buffered streaming."
        )
    return ET.canonicalize(xml_data=xml, strip_text=True)


async def inherited_policy_digest(
    client: ApimClient, publication: Publication, *, expected_api_policies: tuple[str, ...]
) -> str:
    """Re-read actual scopes at plan/apply; failures never become 'no policy'."""

    evidence = {
        "service": _checked_policy(await client.get_service_policy(strict=True), "Gateway")
    }
    api_policy = await client.get_api_policy(publication.api_name, strict=True)
    if api_policy is not None:
        try:
            canonical = ET.canonicalize(xml_data=api_policy, strip_text=True)
            expected = {
                ET.canonicalize(xml_data=xml, strip_text=True) for xml in expected_api_policies
            }
        except ET.ParseError:
            raise ValidationError("Cannot inspect the current Bedrock API policy.") from None
        if canonical not in expected:
            raise ValidationError(
                "The Bedrock API has a custom or changed policy. Reconcile it before reviewing "
                "or applying; MOSAIC will not silently replace unrecognized runtime behavior."
            )
        evidence["api"] = canonical
    products = await client.list_api_products(publication.api_name, strict=True)
    names = {publication.product_name}
    for product in products:
        name = product.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("API Management returned an unidentified Bedrock API product.")
        names.add(name)
    for name in sorted(names):
        evidence[f"product:{name}"] = _checked_policy(
            await client.get_product_policy(name, strict=True), "Product"
        )
    for operation in await client.list_operations(
        publication.api_name, strict=True, allow_initial_absence=True,
    ):
        name = operation.get("name")
        if not isinstance(name, str) or not name:
            raise ValidationError("API Management returned an unidentified Bedrock operation.")
        xml = await client.get_operation_policy(publication.api_name, name, strict=True)
        canonical = _checked_policy(xml, "Operation")
        if xml is not None:
            root = ET.fromstring(xml)
            if (
                sorted(section.tag for section in root)
                != ["backend", "inbound", "on-error", "outbound"]
                or any(
                    len(section) != 1 or section[0].tag != "base" or section[0].attrib
                    for section in root
                )
            ):
                raise ValidationError("Bedrock operations must inherit the reviewed API policy.")
        evidence[f"operation:{name}"] = canonical
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
