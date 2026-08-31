import hashlib
import xml.etree.ElementTree as ET

from mosaic_api.domain import PolicyPreview, PolicyPreviewRequest


def _bool(value: bool) -> str:
    return "true" if value else "false"


def render_policy_preview(request: PolicyPreviewRequest) -> PolicyPreview:
    enforcement = request.enforcement

    policies = ET.Element("policies")
    inbound = ET.SubElement(policies, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(
        inbound,
        "authentication-managed-identity",
        {"resource": request.backend_resource},
    )
    attributes = {
        "counter-key": enforcement.counter_key_expression,
        "estimate-prompt-tokens": _bool(enforcement.estimate_prompt_tokens),
    }
    if enforcement.tokens_per_minute:
        attributes["tokens-per-minute"] = str(enforcement.tokens_per_minute)
    if enforcement.token_quota:
        attributes["token-quota"] = str(enforcement.token_quota)
        attributes["token-quota-period"] = str(enforcement.token_quota_period)
    ET.SubElement(inbound, "llm-token-limit", attributes)
    backend = ET.SubElement(policies, "backend")
    ET.SubElement(backend, "base")
    outbound = ET.SubElement(policies, "outbound")
    ET.SubElement(outbound, "base")
    on_error = ET.SubElement(policies, "on-error")
    ET.SubElement(on_error, "base")
    ET.indent(policies, space="  ")
    policy_xml = ET.tostring(policies, encoding="unicode", short_empty_elements=True)
    digest = hashlib.sha256(policy_xml.encode()).hexdigest()
    return PolicyPreview(policy_xml=policy_xml, content_sha256=digest)
