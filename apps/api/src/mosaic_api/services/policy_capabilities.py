"""Resolve policy evidence from MOSAIC records, never from a caller's shape hint."""

from dataclasses import replace

from mosaic_api.cost_centers import PersonLimits
from mosaic_api.domain import (
    ApiShape,
    CapabilitySupport,
    DeclaredDeployment,
    DeploymentCapability,
    EntitlementResource,
    Gateway,
    GatewayPolicyCapabilities,
    ModelEndpoint,
    Publication,
    default_api_shape,
    shape_fits_provider,
)
from mosaic_api.errors import NotFoundError, UpstreamNotFoundError, ValidationError
from mosaic_api.integrations.apim import ApimClient
from mosaic_api.integrations.apim.model_apis import classify_deployment, is_anthropic_model
from mosaic_api.integrations.apim.policy_capabilities import (
    LimitPolicyRequirement,
    assess_gateway_policy_capabilities,
    required_limit_policies,
)
from mosaic_api.integrations.apim.preflight import capabilities_from_service
from mosaic_api.model_pools import ModelPool, PoolSafeguard
from mosaic_api.observed import ObservedModelDeployment, ObservedProduct
from mosaic_api.repositories import GatewayRepository, ModelEndpointRepository


async def refresh_policy_gateway(client: ApimClient, gateway: Gateway) -> Gateway:
    service = await client.get_service()
    if service is None:
        raise UpstreamNotFoundError(
            "The gateway service could not be read before validating its policy limits.",
            details={"gatewayId": gateway.id},
        )
    observed = capabilities_from_service(service)
    return gateway.model_copy(
        update={
            "capabilities": gateway.capabilities.model_copy(
                update={
                    "sku_name": observed.sku_name,
                    "sku_capacity": observed.sku_capacity,
                }
            ),
        }
    )


def _observed_schema(
    endpoint: ModelEndpoint, deployment: ObservedModelDeployment
) -> tuple[ApiShape | None, CapabilitySupport]:
    shape: ApiShape | None
    if is_anthropic_model(deployment.model_format, deployment.model_name):
        shape = ApiShape.ANTHROPIC_MESSAGES
        support = (
            CapabilitySupport.AVAILABLE
            if shape_fits_provider(shape, endpoint.provider)
            else CapabilitySupport.UNAVAILABLE
        )
        return shape, support
    shape = default_api_shape(endpoint.provider)
    if shape is None:
        return None, CapabilitySupport.UNKNOWN
    affirmative = {
        name: value
        for name, value in deployment.capabilities.items()
        if value.strip().casefold() in {"true", "1"}
    }
    capability = classify_deployment(deployment.model_name, affirmative)
    if capability == DeploymentCapability.UNKNOWN:
        return shape, CapabilitySupport.UNKNOWN
    documented = capability == DeploymentCapability.CHAT or (
        shape == ApiShape.AZURE_OPENAI and capability == DeploymentCapability.RESPONSES
    )
    return shape, (CapabilitySupport.AVAILABLE if documented else CapabilitySupport.UNAVAILABLE)


def deployment_policy_capabilities(
    gateway: Gateway,
    endpoint: ModelEndpoint,
    deployment: ObservedModelDeployment | DeclaredDeployment,
) -> GatewayPolicyCapabilities:
    shape: ApiShape | None
    if isinstance(deployment, DeclaredDeployment):
        shape = deployment.api_shape
        support = (
            CapabilitySupport.AVAILABLE
            if shape_fits_provider(shape, endpoint.provider)
            else CapabilitySupport.UNAVAILABLE
        )
        if endpoint.is_governed_external():
            support = CapabilitySupport.UNKNOWN
    else:
        shape, support = _observed_schema(endpoint, deployment)
    return assess_gateway_policy_capabilities(
        gateway.id,
        gateway.capabilities.sku_name,
        api_shape=shape,
        token_schema_support=support,
        observed_at=gateway.access.checked_at,
    )


async def _deployment_schema(
    endpoints: ModelEndpointRepository | None,
    tenant_id: str,
    endpoint_id: str,
    deployment_name: str,
    expected_shape: ApiShape | None,
) -> tuple[ApiShape | None, CapabilitySupport]:
    shape: ApiShape | None
    if endpoints is None:
        return expected_shape, CapabilitySupport.UNKNOWN
    endpoint = await endpoints.get_endpoint(tenant_id, endpoint_id)
    if endpoint is None:
        return expected_shape, CapabilitySupport.UNKNOWN
    if endpoint.uses_backend_key() or endpoint.is_governed_external():
        declared = endpoint.declared(deployment_name)
        if declared is None:
            return expected_shape, CapabilitySupport.UNKNOWN
        shape = declared.api_shape
        support = (
            CapabilitySupport.AVAILABLE
            if shape_fits_provider(shape, endpoint.provider)
            else CapabilitySupport.UNAVAILABLE
        )
        if endpoint.is_governed_external():
            support = CapabilitySupport.UNKNOWN
    else:
        deployments = await endpoints.list_observed_for_endpoint(
            ObservedModelDeployment, tenant_id, endpoint_id, "observedModelDeployment"
        )
        deployment = next(
            (
                item
                for item in deployments
                if item.deployment_name.casefold() == deployment_name.casefold()
            ),
            None,
        )
        if deployment is None:
            return expected_shape, CapabilitySupport.UNKNOWN
        shape, support = _observed_schema(endpoint, deployment)
    if expected_shape is not None and shape is not None and shape != expected_shape:
        return expected_shape, CapabilitySupport.UNAVAILABLE
    return shape or expected_shape, support


async def publication_policy_capabilities(
    endpoints: ModelEndpointRepository | None,
    publication: Publication,
    gateway: Gateway,
    *,
    resource: EntitlementResource | None = None,
) -> GatewayPolicyCapabilities:
    shape, support = await _deployment_schema(
        endpoints,
        publication.tenant_id,
        publication.model_endpoint_id,
        publication.deployment_name,
        publication.api_shape,
    )
    return assess_gateway_policy_capabilities(
        gateway.id,
        gateway.capabilities.sku_name,
        api_shape=shape,
        token_schema_support=support,
        resource=resource,
        observed_at=gateway.access.checked_at,
    )


async def pool_policy_capabilities(
    endpoints: ModelEndpointRepository | None,
    pool: ModelPool,
    gateway: Gateway,
    *,
    resource: EntitlementResource | None = None,
) -> GatewayPolicyCapabilities:
    models = (
        [model for model in pool.models if model.id == resource.id]
        if resource is not None
        else pool.models
    )
    schemas = [
        await _deployment_schema(
            endpoints,
            pool.tenant_id,
            member.model_endpoint_id,
            member.deployment_name,
            pool.api_shape,
        )
        for model in models
        for member in model.members
    ]
    shapes = {shape for shape, _ in schemas if shape is not None}
    shape = next(iter(shapes)) if len(shapes) == 1 else pool.api_shape
    support = CapabilitySupport.AVAILABLE
    if len(shapes) > 1 or any(state == CapabilitySupport.UNAVAILABLE for _, state in schemas):
        support = CapabilitySupport.UNAVAILABLE
    elif not schemas or any(state == CapabilitySupport.UNKNOWN for _, state in schemas):
        support = CapabilitySupport.UNKNOWN
    return assess_gateway_policy_capabilities(
        gateway.id,
        gateway.capabilities.sku_name,
        api_shape=shape,
        token_schema_support=support,
        resource=resource,
        observed_at=gateway.access.checked_at,
    )


async def resolve_resource_policy_capabilities(
    gateways: GatewayRepository,
    endpoints: ModelEndpointRepository | None,
    tenant_id: str,
    resource: EntitlementResource,
    *,
    gateway: Gateway | None = None,
) -> GatewayPolicyCapabilities:
    publication: Publication | None = None
    pool: ModelPool | None = None
    gateway_id: str | None = None
    if resource.kind == "modelApi":
        model = await gateways.get_model_api(tenant_id, resource.id)
        if model is None:
            raise NotFoundError("Model API was not found.", details={"id": resource.id})
        gateway_id = model.gateway_id
        if model.publication_id is not None:
            publication = await gateways.get_publication(tenant_id, model.publication_id)
            if publication is not None and (
                publication.gateway_id != gateway_id or publication.api_name != model.api_name
            ):
                raise ValidationError(
                    "The model API and its publication do not reference the same API and gateway."
                )
    elif resource.kind == "poolModel":
        pool = await gateways.get_model_pool(tenant_id, resource.scope_id or "")
        if pool is None or pool.pool_model(resource.id) is None:
            raise NotFoundError(
                "Pool model was not found.",
                details={"id": resource.id, "scopeId": resource.scope_id},
            )
        gateway_id = pool.gateway_id
    elif resource.kind == "mcpServer":
        server = await gateways.get_mcp_server(tenant_id, resource.id)
        if server is None:
            raise NotFoundError("MCP server was not found.", details={"id": resource.id})
        gateway_id = server.gateway_id
    elif resource.kind == "product":
        products = await gateways.list_observed(
            ObservedProduct, tenant_id, resource.scope_id or "", "observedProduct"
        )
        if not any(product.id == resource.id for product in products):
            raise NotFoundError("Product was not found.", details={"id": resource.id})
        gateway_id = resource.scope_id
    if gateway is not None and gateway.id != gateway_id:
        raise ValidationError(
            "This resource does not belong to the requested gateway.",
            details={
                "reason": "resourceGatewayMismatch",
                "resource": resource.model_dump(mode="json"),
            },
        )
    if gateway is None and gateway_id is not None:
        gateway = await gateways.get_gateway(tenant_id, gateway_id)
    if gateway is not None:
        if publication is not None:
            return await publication_policy_capabilities(
                endpoints, publication, gateway, resource=resource
            )
        if pool is not None:
            return await pool_policy_capabilities(endpoints, pool, gateway, resource=resource)
    return assess_gateway_policy_capabilities(
        gateway_id,
        gateway.capabilities.sku_name if gateway else None,
        resource=resource,
        observed_at=gateway.access.checked_at if gateway else None,
    )


def person_limit_policies(
    person: PersonLimits | None, *, field_prefix: str
) -> tuple[LimitPolicyRequirement, ...]:
    if person is None:
        return ()
    fields = {
        f"{field_prefix}.tokens.tokensPerMinute": f"{field_prefix}.tokensPerMinute",
        f"{field_prefix}.tokens.tokenQuota": f"{field_prefix}.tokenQuota",
        f"{field_prefix}.requests.calls": f"{field_prefix}.callsPerMinute",
        f"{field_prefix}.requests.callQuota": f"{field_prefix}.callQuota",
    }
    return tuple(
        replace(requirement, field=fields[requirement.field])
        for requirement in required_limit_policies(person.enforcement(), field_prefix=field_prefix)
    )


def pooled_limit_policies(
    monthly_tokens: int | None,
    monthly_calls: int | None,
    *,
    field_prefix: str,
) -> tuple[LimitPolicyRequirement, ...]:
    requirements: list[LimitPolicyRequirement] = []
    if monthly_tokens is not None:
        requirements.append(
            LimitPolicyRequirement(
                "llm-token-limit", f"{field_prefix}.monthlyTokens", (monthly_tokens, "Monthly")
            )
        )
    if monthly_calls is not None:
        requirements.append(
            LimitPolicyRequirement(
                "quota-by-key", f"{field_prefix}.monthlyCalls", (monthly_calls, "Monthly")
            )
        )
    return tuple(requirements)


def safeguard_limit_policies(
    safeguard: PoolSafeguard | None, *, field_prefix: str = "safeguard"
) -> tuple[LimitPolicyRequirement, ...]:
    if safeguard is None:
        return ()
    requirements: list[LimitPolicyRequirement] = []
    if safeguard.tokens_per_minute is not None:
        requirements.append(
            LimitPolicyRequirement(
                "llm-token-limit",
                f"{field_prefix}.tokensPerMinute",
                (safeguard.tokens_per_minute, False),
            )
        )
    if safeguard.token_quota is not None:
        requirements.append(
            LimitPolicyRequirement(
                "llm-token-limit",
                f"{field_prefix}.tokenQuota",
                (safeguard.token_quota, safeguard.token_quota_period, False),
            )
        )
    return tuple(requirements)
