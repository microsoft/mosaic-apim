import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from mosaic_api.domain import (
    PolicyFacet,
    PolicyPreview,
    PolicyPreviewRequest,
    Publication,
    TokenEnforcement,
)
from mosaic_api.integrations.apim.policy_semantics import analyze_policy


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _token_limit_attributes(enforcement: TokenEnforcement) -> dict[str, str]:
    attributes = {
        "counter-key": enforcement.counter_key_expression,
        "estimate-prompt-tokens": _bool(enforcement.estimate_prompt_tokens),
    }
    if enforcement.tokens_per_minute:
        attributes["tokens-per-minute"] = str(enforcement.tokens_per_minute)
    if enforcement.token_quota:
        attributes["token-quota"] = str(enforcement.token_quota)
        attributes["token-quota-period"] = str(enforcement.token_quota_period)
    return attributes


def _serialize(root: ET.Element) -> str:
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode", short_empty_elements=True)


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
    ET.SubElement(inbound, "llm-token-limit", _token_limit_attributes(enforcement))
    backend = ET.SubElement(policies, "backend")
    ET.SubElement(backend, "base")
    outbound = ET.SubElement(policies, "outbound")
    ET.SubElement(outbound, "base")
    on_error = ET.SubElement(policies, "on-error")
    ET.SubElement(on_error, "base")
    policy_xml = _serialize(policies)
    digest = hashlib.sha256(policy_xml.encode()).hexdigest()
    analysis = analyze_policy(policy_xml)
    return PolicyPreview(
        policy_xml=policy_xml,
        content_sha256=digest,
        facets=analysis.facets,
        unrecognized_elements=sorted(set(analysis.unrecognized_elements)),
    )


MANAGED_IDENTITY_RESOURCE = "https://cognitiveservices.azure.com"
METRIC_NAMESPACE = "mosaic"


@dataclass(frozen=True)
class PublicationPolicy:
    """The two documents a publication writes, reduced to facets before they leave this module.

    ``fragment_xml`` and ``api_policy_xml`` stay in process. ADR 0004's rule that MOSAIC-authored
    markup never crosses the API boundary applies to policy MOSAIC writes exactly as it does to
    policy it reads, so callers surface :attr:`facets` and :attr:`content_sha256` instead.
    """

    fragment_xml: str
    api_policy_xml: str
    content_sha256: str
    facets: list[PolicyFacet]
    unrecognized_elements: list[str]


def render_publication_policy(publication: Publication) -> PublicationPolicy:
    """Author the enforcement fragment and the thin API policy that includes it.

    Enforcement lives in the fragment rather than the API policy document so that the ownership
    seam ADR 0004 established survives publishing: there is exactly one place MOSAIC writes rules,
    and changing enforcement never rewrites a document another author might share.
    """

    fragment = ET.Element("fragment")
    ET.SubElement(
        fragment, "authentication-managed-identity", {"resource": MANAGED_IDENTITY_RESOURCE}
    )
    ET.SubElement(fragment, "set-backend-service", {"backend-id": publication.backend_name})
    ET.SubElement(fragment, "llm-token-limit", _token_limit_attributes(publication.enforcement))
    metric = ET.SubElement(fragment, "llm-emit-token-metric", {"namespace": METRIC_NAMESPACE})
    ET.SubElement(metric, "dimension", {"name": "Publication", "value": publication.id})
    ET.SubElement(metric, "dimension", {"name": "Deployment", "value": publication.deployment_name})
    fragment_xml = _serialize(fragment)

    policies = ET.Element("policies")
    inbound = ET.SubElement(policies, "inbound")
    ET.SubElement(inbound, "base")
    ET.SubElement(inbound, "include-fragment", {"fragment-id": publication.fragment_name})
    for section in ("backend", "outbound", "on-error"):
        ET.SubElement(ET.SubElement(policies, section), "base")
    api_policy_xml = _serialize(policies)

    # One digest over both documents. They are applied together and are meaningless apart, so a
    # change to either has to invalidate the plan an administrator approved.
    combined = hashlib.sha256(f"{fragment_xml}\n{api_policy_xml}".encode()).hexdigest()
    analysis = analyze_policy(fragment_xml)
    include_analysis = analyze_policy(api_policy_xml)
    return PublicationPolicy(
        fragment_xml=fragment_xml,
        api_policy_xml=api_policy_xml,
        content_sha256=combined,
        facets=[*analysis.facets, *include_analysis.facets],
        unrecognized_elements=sorted(
            set(analysis.unrecognized_elements) | set(include_analysis.unrecognized_elements)
        ),
    )
