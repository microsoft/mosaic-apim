import type {
  Entitlement,
  EntitlementResource,
  Gateway,
  McpServer,
  ModelApi,
  ModelEndpoint,
  ModelPool,
} from './types'

export interface EntitlementEnvironmentLookups {
  gateways?: Gateway[]
  modelApis?: ModelApi[]
  mcpServers?: McpServer[]
  modelEndpoints?: ModelEndpoint[]
  modelPools?: ModelPool[]
}

function gatewayEnvironment(gateways: Gateway[] | undefined, gatewayId: string | null | undefined) {
  return gateways?.find((gateway) => gateway.id === gatewayId)?.environment ?? null
}

export function entitlementResourceEnvironment(
  resource: EntitlementResource,
  lookups: EntitlementEnvironmentLookups,
  entitlement?: Pick<Entitlement, 'binding'>,
): string | null {
  if (resource.kind === 'modelDeployment') {
    const endpointId = resource.scopeId ?? resource.id
    return lookups.modelEndpoints?.find((endpoint) => endpoint.id === endpointId)?.environment ?? null
  }

  if (resource.kind === 'modelApi') {
    return gatewayEnvironment(
      lookups.gateways,
      lookups.modelApis?.find((modelApi) => modelApi.id === resource.id)?.gatewayId,
    )
  }

  if (resource.kind === 'mcpServer') {
    return gatewayEnvironment(
      lookups.gateways,
      lookups.mcpServers?.find((server) => server.id === resource.id)?.gatewayId,
    )
  }

  // A pool model's scope is its pool, which serves every model from one gateway.
  if (resource.kind === 'poolModel') {
    return gatewayEnvironment(
      lookups.gateways,
      lookups.modelPools?.find((pool) => pool.id === resource.scopeId)?.gatewayId,
    )
  }

  return gatewayEnvironment(lookups.gateways, resource.scopeId ?? entitlement?.binding?.gatewayId)
}
