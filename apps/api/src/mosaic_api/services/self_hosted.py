"""Identity, inventory and qualification boundaries for the first self-hosted profile."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import urlsplit

from mosaic_api.domain import (
    MCP_METADATA_API_NAME,
    ApiShape,
    AppliedSelfHostedRuntime,
    Gateway,
    GatewayRuntimeChoice,
    McpPublication,
    ModelAccessSettings,
    ModelProvider,
    Publication,
    SelfHostedGateway,
    SelfHostedGatewayCandidate,
    utc_now,
)
from mosaic_api.errors import ConflictError, ValidationError
from mosaic_api.integrations.apim.client import JsonObject, self_hosted_gateway_segment
from mosaic_api.repositories import GatewayRepository

SELF_HOSTED_IMAGE = "2.13.2"
RUNTIME_RULES = "self-hosted-chat/1"
MCP_RUNTIME_RULES = "self-hosted-mcp/1"
CHECKS = {
    "ingress": "The approved local TLS ingress and Azure-ingress denial are unconfirmed.",
    "backend": "Runtime workload identity and backend reachability are unconfirmed.",
    "policy": "The effective Chat and enforcement policy set is unconfirmed on this runtime.",
    "telemetry": "Grant attribution, token usage and budget propagation are unconfirmed.",
    "topology": "Single-replica topology and stale-configuration containment are unconfirmed.",
}
MCP_CHECKS = {
    **CHECKS,
    "policy": "Native MCP streaming and grant enforcement are unconfirmed on this runtime.",
    "telemetry": "MCP grant attribution, call limits and budget propagation are unconfirmed.",
    "discovery": "Local OAuth discovery and the native authentication challenge are unconfirmed.",
}
RUNTIME_ACTIONS = (
    "Microsoft.ApiManagement/service/gateways/read",
    *(
        f"Microsoft.ApiManagement/service/{path}/{action}"
        for path in ("gateways/apis", "namedValues")
        for action in ("read", "write", "delete")
    ),
)


def ingress_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError:
        raise ValidationError("Enter a valid HTTPS ingress origin.") from None
    if (
        re.search(r"[\x00-\x20\\%?#@]", value)
        or parsed.scheme != "https"
        or parsed.path not in {"", "/"}
        or not re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?",
            host,
        )
        or host.endswith(".localhost")
        or (port is not None and not 1 <= port <= 65535)
        or parsed.netloc.lower() != host + (f":{port}" if port is not None else "")
    ):
        raise ValidationError(
            "Enter a DNS-based HTTPS ingress origin with no credentials, path, query or fragment."
        )
    return f"https://{host}" + (f":{port}" if port not in {None, 443} else "")


def child_candidate(
    gateway: Gateway, item: JsonObject, *, expected_name: str | None = None
) -> SelfHostedGatewayCandidate:
    name = item.get("name")
    if not isinstance(name, str):
        raise ConflictError("API Management returned a gateway without an exact identity.")
    try:
        self_hosted_gateway_segment(name)
    except ValueError:
        raise ConflictError("API Management returned an invalid self-hosted gateway.") from None
    resource_id = f"{gateway.arm_resource().canonical}/gateways/{name}"
    observed_id = item.get("id")
    if (
        not isinstance(observed_id, str)
        or observed_id.casefold() != resource_id.casefold()
        or (expected_name is not None and name.casefold() != expected_name.casefold())
    ):
        raise ConflictError("The gateway returned by API Management is outside this parent.")
    return SelfHostedGatewayCandidate(gateway_name=name, resource_id=resource_id)


async def require_runtime(
    repo: GatewayRepository, gateway: Gateway, choice: GatewayRuntimeChoice
) -> SelfHostedGateway:
    item = await repo.get_self_hosted_gateway(gateway.tenant_id, choice.runtime_id or "")
    if (
        item is None
        or item.parent_gateway_id != gateway.id
        or item.parent_resource_id.casefold() != gateway.arm_resource().canonical.casefold()
    ):
        raise ConflictError(
            "The selected self-hosted gateway is unavailable for this APIM service."
        )
    return item


def require_chat_profile(
    gateway: Gateway,
    runtime: SelfHostedGateway,
    provider: ModelProvider,
    api_shape: ApiShape | None,
    chat: bool,
    access: ModelAccessSettings | None,
    *,
    backend_key: bool,
) -> None:
    if (
        gateway.azure_cloud != "commercial"
        or (gateway.capabilities.sku_name or "").casefold() != "developer"
        or runtime.image_version != SELF_HOSTED_IMAGE
    ):
        raise ValidationError(
            f"The first self-hosted profile requires Commercial Developer APIM and image "
            f"{SELF_HOSTED_IMAGE}. Use a qualified runtime."
        )
    if (
        provider != ModelProvider.AZURE_OPENAI
        or api_shape != ApiShape.AZURE_OPENAI
        or not chat
        or access is None
        or access.keys_enabled
        or backend_key
    ):
        raise ValidationError(
            "Self-hosted publishing supports governed Azure OpenAI Chat with Entra only "
            "and workload identity to the backend."
        )


def binding_name(publication: Publication | McpPublication) -> str:
    digest = hashlib.sha256(publication.api_name.casefold().encode()).hexdigest()
    return f"mosaic-binding-{digest[:24]}"


def binding_value(publication: Publication | McpPublication, child_name: str) -> str:
    if isinstance(publication, McpPublication):
        source = {"kind": "mcp", "endpoint": publication.mcp_endpoint_id}
    else:
        source = {
            "kind": "model",
            "endpoint": publication.model_endpoint_id,
            "deployment": publication.deployment_name,
        }
    return json.dumps(
        {
            "version": 1,
            **source,
            "publication": publication.id,
            "api": publication.api_name,
            "target": "selfHosted",
            "gateway": child_name,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def matching_binding(item: JsonObject, value: str) -> bool:
    props = item.get("properties")
    return (
        isinstance(props, dict)
        and props.get("secret") is False
        and props.get("value") == value
        and props.get("keyVault") is None
    )


def simple_service_policy(xml: str | None) -> bool:
    if xml is None:
        return True
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return False
    return root.tag == "policies" and all(
        section.tag in {"inbound", "backend", "outbound", "on-error"}
        and all(
            (
                element.tag == "base"
                or (section.tag == "backend" and element.tag == "forward-request")
            )
            and not element.attrib
            and not len(element)
            for element in section
        )
        for section in root
    )


def fingerprint(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def runtime_fingerprint(runtime: SelfHostedGateway) -> str:
    return fingerprint(runtime.model_dump(mode="json", exclude={"mcp_discovery_link", "etag"}))


@dataclass(frozen=True)
class RuntimeEvidence:
    scope: str
    rules_revision: str
    checked_at: datetime
    expires_at: datetime
    confirmed: frozenset[str]


# Deliberately not connected to an admin request, a cloud client or an environment flag.
# The offline estate and tests can inject evidence. A later fixture must qualify the real source.
EvidenceReader = Callable[[str], Awaitable[RuntimeEvidence | None]]


async def missing_evidence(
    reader: EvidenceReader | None,
    scope: str,
    checks: Iterable[str] = CHECKS,
    *,
    rules: str = RUNTIME_RULES,
) -> list[str]:
    labels = MCP_CHECKS if rules == MCP_RUNTIME_RULES else CHECKS
    evidence = await reader(scope) if reader is not None else None
    if evidence is not None:
        now = utc_now()
        if (
            evidence.scope != scope
            or evidence.rules_revision != rules
            or not evidence.checked_at <= now < evidence.expires_at
        ):
            return ["The selected runtime's evidence is stale or no longer matches this plan."]
    return [
        labels[check] for check in checks if evidence is None or check not in evidence.confirmed
    ]


async def require_applied_ingress(
    repo: GatewayRepository,
    gateway: Gateway,
    publication: Publication | McpPublication,
    reader: EvidenceReader | None,
) -> str:
    applied = publication.applied_runtime
    choice = publication.runtime_target
    unavailable = "Local gateway access is not confirmed. Ask an administrator to review it."
    if choice is None or applied is None or publication.access_state != "applied":
        raise ConflictError(unavailable, details={"reason": "localGatewayNotReady"})
    try:
        runtime = await require_runtime(repo, gateway, choice)
    except ConflictError:
        raise ConflictError(unavailable, details={"reason": "localGatewayNotReady"}) from None
    parent = gateway.arm_resource().canonical
    association = f"{parent}/gateways/{runtime.gateway_name}/apis/{publication.api_name}"
    metadata = f"{parent}/namedValues/{binding_name(publication)}"
    expected = {association, metadata}
    owned = {item.resource_id for item in publication.created_resources()}
    is_mcp = isinstance(publication, McpPublication)
    if is_mcp:
        discovery = f"{parent}/gateways/{runtime.gateway_name}/apis/{MCP_METADATA_API_NAME}"
        if runtime.mcp_discovery_link != discovery:
            raise ConflictError(unavailable, details={"reason": "localGatewayNotReady"})
    if (
        applied.runtime_id != runtime.id
        or applied.ingress_origin != runtime.ingress_origin
        or applied.ingress_origin == str(gateway.capabilities.gateway_url).rstrip("/").lower()
        or applied.runtime_fingerprint != runtime_fingerprint(runtime)
        or not expected <= owned
        or await missing_evidence(
            reader,
            applied.evidence_scope,
            MCP_CHECKS if is_mcp else CHECKS,
            rules=MCP_RUNTIME_RULES if is_mcp else RUNTIME_RULES,
        )
    ):
        raise ConflictError(unavailable, details={"reason": "localGatewayNotReady"})
    return applied.ingress_origin


def applied_runtime_snapshot(runtime: SelfHostedGateway, scope: str) -> AppliedSelfHostedRuntime:
    return AppliedSelfHostedRuntime(
        runtime_id=runtime.id,
        ingress_origin=runtime.ingress_origin,
        runtime_fingerprint=runtime_fingerprint(runtime),
        evidence_scope=scope,
    )
