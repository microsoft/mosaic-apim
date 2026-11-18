"""Recorded endpoint evidence, distinct from current rules and from a live model invocation."""

from mosaic_api.domain import (
    EndpointAccess,
    EndpointAccessView,
    GatewayRuntimeAccess,
    GatewayRuntimeAccessView,
    ModelEndpoint,
    ModelEndpointView,
    ReadinessFreshness,
)

# Bump when endpoint verdict or remediation semantics change, not for unrelated app releases.
ENDPOINT_READINESS_RULES_REVISION = "model-endpoint-readiness/1"


def readiness_freshness(access: EndpointAccess | GatewayRuntimeAccess) -> ReadinessFreshness:
    recorded = bool(
        access.checked_at
        or access.rules_revision
        or access.evaluation != "notEvaluated"
        or access.message
        or access.remediation
    )
    if isinstance(access, EndpointAccess):
        recorded = recorded or access.can_read or bool(access.missing_actions)
    else:
        recorded = recorded or access.can_invoke or bool(
            access.reason or access.role_findings or access.required_data_actions
        )
    if not recorded:
        return ReadinessFreshness.NEVER_CHECKED
    if access.rules_revision != ENDPOINT_READINESS_RULES_REVISION or not access.checked_at:
        return ReadinessFreshness.STALE
    return ReadinessFreshness.CURRENT


def endpoint_access_view(access: EndpointAccess) -> EndpointAccessView:
    return EndpointAccessView(
        **access.model_dump(by_alias=False),
        freshness=readiness_freshness(access),
        current_rules_revision=ENDPOINT_READINESS_RULES_REVISION,
    )


def runtime_access_view(access: GatewayRuntimeAccess) -> GatewayRuntimeAccessView:
    return GatewayRuntimeAccessView(
        **access.model_dump(by_alias=False),
        freshness=readiness_freshness(access),
        current_rules_revision=ENDPOINT_READINESS_RULES_REVISION,
    )


def model_endpoint_view(endpoint: ModelEndpoint) -> ModelEndpointView:
    return ModelEndpointView.model_validate(
        {
            **endpoint.model_dump(by_alias=False),
            "access": endpoint_access_view(endpoint.access),
            "runtime_access": [runtime_access_view(row) for row in endpoint.runtime_access],
        }
    )
