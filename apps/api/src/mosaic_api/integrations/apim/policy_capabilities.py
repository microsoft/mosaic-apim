"""Documented support for the four policies MOSAIC's existing managed target emits."""

import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from mosaic_api.domain import (
    ApiShape,
    CapabilitySupport,
    EntitlementEnforcement,
    EntitlementResource,
    GatewayPolicyCapabilities,
    GatewayPolicyName,
    GatewayTier,
    PolicyCapability,
    PolicyCapabilityIssue,
    PolicyCapabilityNextAction,
    PolicyCapabilityReason,
    TokenEnforcement,
    gateway_tier,
)
from mosaic_api.errors import ValidationError

MAXIMUM_RATE_WINDOW_SECONDS = 300
PolicyBoundary = Literal["save", "plan", "apply"]
_POLICIES: tuple[GatewayPolicyName, ...] = (
    "rate-limit-by-key",
    "quota-by-key",
    "llm-token-limit",
    "llm-emit-token-metric",
)
_DOCUMENTATION: dict[GatewayPolicyName, str] = {
    policy: f"https://learn.microsoft.com/en-us/azure/api-management/{policy}-policy"
    for policy in _POLICIES
}
_MESSAGES: dict[PolicyBoundary, str] = {
    "save": "These limits cannot be saved. Review the affected fields; nothing was saved.",
    "plan": "These limits cannot be enforced. Review them before planning; no plan was produced.",
    "apply": "These limits cannot be enforced here. Nothing was applied; review and plan again.",
}


def policy_support_on_tier(
    policy: GatewayPolicyName, shape: ApiShape | None, tier: GatewayTier
) -> CapabilitySupport:
    if tier == GatewayTier.CONSUMPTION and policy != "llm-emit-token-metric":
        return CapabilitySupport.UNAVAILABLE
    if (
        policy in {"llm-token-limit", "llm-emit-token-metric"}
        and shape == ApiShape.ANTHROPIC_MESSAGES
        and tier in {GatewayTier.CLASSIC, GatewayTier.CONSUMPTION}
    ):
        return CapabilitySupport.UNAVAILABLE
    if tier == GatewayTier.UNKNOWN or (
        policy in {"llm-token-limit", "llm-emit-token-metric"} and shape is None
    ):
        return CapabilitySupport.UNKNOWN
    return CapabilitySupport.AVAILABLE


def _capability(
    policy: GatewayPolicyName,
    sku_name: str | None,
    shape: ApiShape | None,
    schema_support: CapabilitySupport,
    resource: EntitlementResource | None,
) -> PolicyCapability:
    tier = gateway_tier(sku_name)
    support = CapabilitySupport.AVAILABLE
    reason: PolicyCapabilityReason = "documentedSupport"
    action: PolicyCapabilityNextAction = "none"
    message = (
        "The recorded gateway tier supports this policy. "
        "This does not verify live policy compilation or applied access."
    )
    token_policy = policy in {"llm-token-limit", "llm-emit-token-metric"}
    if token_policy and (
        (resource is not None and resource.kind == "mcpServer")
        or schema_support == CapabilitySupport.UNAVAILABLE
    ):
        support, reason, action = (
            CapabilitySupport.UNAVAILABLE,
            "unsupportedApiShape",
            "reviewResource",
        )
        message = (
            "MCP servers support call limits, not token policies."
            if resource is not None and resource.kind == "mcpServer"
            else "This resource does not use a documented LLM token API schema."
        )
    elif (
        policy_support_on_tier(policy, shape, tier) == CapabilitySupport.UNAVAILABLE
        and tier == GatewayTier.CONSUMPTION
        and policy != "llm-emit-token-metric"
    ):
        support, reason, action = (
            CapabilitySupport.UNAVAILABLE,
            "unsupportedTier",
            "chooseSupportedGateway",
        )
        message = (
            f"Consumption does not support {policy}. "
            "Choose a resource on a supported gateway, or remove this limit where permitted."
        )
    elif policy_support_on_tier(policy, shape, tier) == CapabilitySupport.UNAVAILABLE:
        support, reason, action = (
            CapabilitySupport.UNAVAILABLE,
            "unsupportedTier",
            "chooseSupportedGateway",
        )
        message = (
            "API Management applies token limits and token metrics to Anthropic Messages "
            "only on v2 tiers (Basic v2, Standard v2 and Premium v2). "
            + (
                f"This gateway uses the {sku_name} tier, a classic tier."
                if tier == GatewayTier.CLASSIC
                else "This gateway uses the Consumption tier."
            )
        )
    elif tier == GatewayTier.UNKNOWN:
        support, reason, action = (CapabilitySupport.UNKNOWN, "unknownTier", "checkGatewayAccess")
        message = (
            "MOSAIC has not verified this gateway's tier. "
            "Run Check access on the gateway before configuring this policy."
        )
    elif (sku_name or "").strip().casefold() == "isolated":
        support, reason, action = (CapabilitySupport.UNKNOWN, "unverifiedSku", "checkGatewayAccess")
        message = (
            "Current policy support for this gateway's SKU has not been verified. "
            "Check its tier and the policy reference; MOSAIC will not assume support."
        )
    elif token_policy and (shape is None or schema_support == CapabilitySupport.UNKNOWN):
        support, reason, action = (CapabilitySupport.UNKNOWN, "unknownApiShape", "reviewResource")
        message = (
            "MOSAIC has not verified this resource's token API schema. "
            "Review its publication or model configuration; a provider name is not enough."
        )
    return PolicyCapability(
        policy=policy,
        support=support,
        reason=reason,
        message=message,
        next_action=action,
        documentation_url=_DOCUMENTATION[policy],
        maximum_renewal_period_seconds=(
            MAXIMUM_RATE_WINDOW_SECONDS if policy == "rate-limit-by-key" else None
        ),
    )


def assess_gateway_policy_capabilities(
    gateway_id: str | None,
    sku_name: str | None,
    *,
    api_shape: ApiShape | None = None,
    token_schema_support: CapabilitySupport | None = None,
    resource: EntitlementResource | None = None,
    observed_at: datetime | None = None,
) -> GatewayPolicyCapabilities:
    schema = token_schema_support or (
        CapabilitySupport.AVAILABLE if api_shape is not None else CapabilitySupport.UNKNOWN
    )
    return GatewayPolicyCapabilities(
        gateway_id=gateway_id,
        sku_name=sku_name,
        tier=gateway_tier(sku_name),
        api_shape=api_shape,
        observed_at=observed_at,
        resource=resource,
        policies={
            policy: _capability(policy, sku_name, api_shape, schema, resource)
            for policy in _DOCUMENTATION
        },
    )


def shared_policy_capabilities(
    capabilities: Sequence[GatewayPolicyCapabilities],
) -> GatewayPolicyCapabilities:
    if not capabilities:
        return assess_gateway_policy_capabilities(None, None)
    priority = {
        CapabilitySupport.UNAVAILABLE: 0,
        CapabilitySupport.UNKNOWN: 1,
        CapabilitySupport.AVAILABLE: 2,
    }
    return capabilities[0].model_copy(
        update={
            "policies": {
                policy: min(
                    (item.policies[policy] for item in capabilities),
                    key=lambda item: priority[item.support],
                )
                for policy in _POLICIES
            },
        }
    )


@dataclass(frozen=True)
class LimitPolicyRequirement:
    policy: GatewayPolicyName
    field: str
    values: tuple[int | str | bool | None, ...]
    renewal_period_seconds: int | None = None
    renewal_field: str | None = None


def required_emitted_policies(*documents: str) -> tuple[LimitPolicyRequirement, ...]:
    roots = [ET.fromstring(document) for document in documents]
    return tuple(
        LimitPolicyRequirement(
            policy,
            "policy.tokenMetrics" if policy == "llm-emit-token-metric" else f"policy.{policy}",
            (),
        )
        for policy in _POLICIES
        if any(element.tag == policy for root in roots for element in root.iter())
    )


def required_limit_policies(
    enforcement: EntitlementEnforcement | TokenEnforcement | None,
    *,
    field_prefix: str = "enforcement",
    counter_key_effective: bool = False,
) -> tuple[LimitPolicyRequirement, ...]:
    if enforcement is None:
        return ()
    requirements: list[LimitPolicyRequirement] = []
    tokens = enforcement if isinstance(enforcement, TokenEnforcement) else enforcement.tokens
    token_prefix = (
        field_prefix if isinstance(enforcement, TokenEnforcement) else f"{field_prefix}.tokens"
    )
    if tokens is not None:
        semantics = (
            tokens.estimate_prompt_tokens,
            tokens.counter_key_expression if counter_key_effective else None,
        )
        if tokens.tokens_per_minute is not None:
            requirements.append(
                LimitPolicyRequirement(
                    "llm-token-limit",
                    f"{token_prefix}.tokensPerMinute",
                    (tokens.tokens_per_minute, *semantics),
                )
            )
        if tokens.token_quota is not None:
            requirements.append(
                LimitPolicyRequirement(
                    "llm-token-limit",
                    f"{token_prefix}.tokenQuota",
                    (tokens.token_quota, tokens.token_quota_period, *semantics),
                )
            )
    requests = None if isinstance(enforcement, TokenEnforcement) else enforcement.requests
    if requests is not None:
        counter = requests.counter_key_expression if counter_key_effective else None
        if requests.calls is not None:
            requirements.append(
                LimitPolicyRequirement(
                    "rate-limit-by-key",
                    f"{field_prefix}.requests.calls",
                    (requests.calls, requests.renewal_period_seconds, counter),
                    renewal_period_seconds=requests.renewal_period_seconds,
                    renewal_field=f"{field_prefix}.requests.renewalPeriodSeconds",
                )
            )
        if requests.call_quota is not None:
            requirements.append(
                LimitPolicyRequirement(
                    "quota-by-key",
                    f"{field_prefix}.requests.callQuota",
                    (requests.call_quota, requests.call_quota_period, counter),
                )
            )
    return tuple(requirements)


def changed_limit_policies(
    before: Sequence[LimitPolicyRequirement], after: Sequence[LimitPolicyRequirement]
) -> tuple[LimitPolicyRequirement, ...]:
    previous = {requirement.field: requirement for requirement in before}
    return tuple(
        requirement for requirement in after if previous.get(requirement.field) != requirement
    )


def limit_policy_issues(
    capabilities: GatewayPolicyCapabilities,
    requirements: Sequence[LimitPolicyRequirement],
    *,
    intrinsic_only: bool = False,
) -> list[PolicyCapabilityIssue]:
    issues: list[PolicyCapabilityIssue] = []
    for requirement in requirements:
        capability = capabilities.policies[requirement.policy]
        invalid_window = (
            requirement.renewal_period_seconds is not None
            and not 1 <= requirement.renewal_period_seconds <= MAXIMUM_RATE_WINDOW_SECONDS
        )
        if not invalid_window and (
            intrinsic_only or capability.support == CapabilitySupport.AVAILABLE
        ):
            continue
        issues.append(
            PolicyCapabilityIssue(
                field=(requirement.renewal_field or requirement.field)
                if invalid_window
                else requirement.field,
                resource=capabilities.resource,
                gateway_id=capabilities.gateway_id,
                sku_name=capabilities.sku_name,
                tier=capabilities.tier,
                api_shape=capabilities.api_shape,
                policy=requirement.policy,
                support=capability.support,
                reason="invalidPolicyValue" if invalid_window else capability.reason,
                message=(
                    "Call rate windows must be between 1 and 300 seconds. "
                    "Change the window or remove the call rate."
                    if invalid_window
                    else capability.message
                ),
                next_action="reviewLimit" if invalid_window else capability.next_action,
                documentation_url=capability.documentation_url,
                minimum=1 if invalid_window else None,
                maximum=MAXIMUM_RATE_WINDOW_SECONDS if invalid_window else None,
            )
        )
    return sorted(issues, key=lambda issue: (issue.field, issue.policy))


def require_supported_limits(
    capabilities: GatewayPolicyCapabilities,
    requirements: Sequence[LimitPolicyRequirement],
    *,
    boundary: PolicyBoundary = "save",
    intrinsic_only: bool = False,
) -> None:
    issues = limit_policy_issues(capabilities, requirements, intrinsic_only=intrinsic_only)
    if issues:
        raise ValidationError(
            _MESSAGES[boundary] + (" " + issues[0].message if len(issues) == 1 else ""),
            details={
                "reason": "gatewayPolicyLimitsRejected",
                "boundary": boundary,
                "issues": [issue.model_dump(mode="json") for issue in issues],
            },
        )
