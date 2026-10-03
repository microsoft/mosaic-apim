import type {
  Entitlement,
  KeyRevealResult,
  McpConnection,
  ModelConnection,
  ResourceSummary,
  ResolvedEntitlement,
} from '../types'

const timestamp = '2026-09-01T12:00:00Z'

export const researchCostCenter = {
  id: 'cost_center_research',
  name: 'Research',
  code: 'RES',
}

export const directGrant: Entitlement = {
  id: 'entitlement_direct',
  tenantId: 'tenant-1',
  entityType: 'entitlement',
  subject: { kind: 'user', id: 'principal_1' },
  resource: { kind: 'modelApi', id: 'modelApi_chat', scopeId: null },
  costCenterId: researchCostCenter.id,
  enabled: true,
  enforcement: {
    tokens: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      tokensPerMinute: 5_000,
      tokenQuota: null,
      tokenQuotaPeriod: null,
      estimatePromptTokens: true,
    },
    requests: null,
  },
  binding: {
    gatewayId: 'gateway_1',
    apimProductName: null,
    apimSubscriptionName: 'grant-subscription',
    source: 'orchestrated',
  },
  runtime: {
    publicationId: 'publication_1',
    status: 'applied',
    appliedMethods: { keysEnabled: true, entraEnabled: true },
    subscriptionName: 'grant-subscription',
    appliedAt: timestamp,
    error: null,
  },
  notes: null,
  createdAt: timestamp,
  updatedAt: timestamp,
}

export const directSummary: ResourceSummary = {
  kind: 'modelApi',
  id: 'modelApi_chat',
  scopeId: null,
  displayName: 'Chat model API',
  gatewayId: 'gateway_1',
  gatewayName: 'Production gateway',
  environment: 'production',
  available: true,
}

export const directResolved: ResolvedEntitlement = {
  entitlement: directGrant,
  resourceSummary: directSummary,
  costCenter: researchCostCenter,
  via: 'direct',
  viaGroupId: null,
  viaGroupName: null,
}

export const groupResolved: ResolvedEntitlement = {
  entitlement: {
    ...directGrant,
    id: 'entitlement_group',
    subject: { kind: 'group', id: 'group_1' },
    binding: null,
    runtime: null,
  },
  resourceSummary: directSummary,
  via: 'group',
  viaGroupId: 'group_1',
  viaGroupName: 'Platform engineering',
}

export const securityGroupResolved: ResolvedEntitlement = {
  entitlement: {
    ...directGrant,
    id: 'entitlement_security_group',
    subject: { kind: 'securityGroup', id: 'principal_group_1' },
    binding: null,
    runtime: null,
  },
  resourceSummary: directSummary,
  via: 'securityGroup',
  viaGroupId: 'principal_group_1',
  viaGroupName: 'AI builders',
  effective: true,
  shadowedBy: null,
}

export const mcpGrant: Entitlement = {
  ...directGrant,
  id: 'entitlement_mcp_person',
  subject: { kind: 'user', id: 'principal_1' },
  resource: { kind: 'mcpServer', id: 'mcp_weather', scopeId: null },
  enforcement: {
    tokens: null,
    requests: {
      counterKeyExpression: 'mosaic:mcp:grant',
      calls: 120,
      renewalPeriodSeconds: 60,
      callQuota: 10_000,
      callQuotaPeriod: 'Monthly',
    },
  },
  binding: null,
  runtime: {
    publicationId: 'mcp_publication_1',
    status: 'applied',
    appliedMethods: { keysEnabled: false, entraEnabled: true },
    subscriptionName: null,
    appliedAt: timestamp,
    error: null,
  },
}

export const mcpSummary: ResourceSummary = {
  kind: 'mcpServer',
  id: 'mcp_weather',
  scopeId: null,
  displayName: 'Weather tools',
  gatewayId: 'gateway_1',
  gatewayName: 'Production gateway',
  environment: 'production',
  available: true,
}

export const mcpResolved: ResolvedEntitlement = {
  entitlement: mcpGrant,
  resourceSummary: mcpSummary,
  via: 'direct',
  viaGroupId: null,
  viaGroupName: null,
}

export const mcpMosaicGroupResolved: ResolvedEntitlement = {
  entitlement: {
    ...mcpGrant,
    id: 'entitlement_mcp_mosaic_group',
    subject: { kind: 'group', id: 'group_1' },
    runtime: null,
  },
  resourceSummary: mcpSummary,
  via: 'group',
  viaGroupId: 'group_1',
  viaGroupName: 'Platform engineering',
}

export const mcpServerUrl = 'https://gateway.example.test/mosaic/mcp/weather/mcp'
export const mcpMetadataUrl = 'https://gateway.example.test/.well-known/oauth-protected-resource/mosaic/mcp/weather/mcp'
export const modelClientId = '99999999-8888-7777-6666-555555555555'
export const mcpConnection: McpConnection = {
  entitlementId: mcpGrant.id,
  mcpServerId: 'mcp_server_weather',
  publicationId: 'mcp_publication_1',
  gatewayId: 'gateway_1',
  displayName: 'Weather tools',
  tenantId: 'tenant-1',
  serverUrl: mcpServerUrl,
  transport: 'streamable',
  enforced: true,
  statusMessage: 'The gateway enforces this grant.',
  runtime: mcpGrant.runtime,
  costCenter: researchCostCenter,
  costCenterHeader: 'x-mosaic-cost-center',
  entraAudience: '11111111-2222-3333-4444-555555555555',
  delegatedScope: 'api://11111111-2222-3333-4444-555555555555/Mcp.Invoke',
  applicationScope: null,
  requiredAppRole: null,
  clientId: modelClientId,
  principalKind: 'user',
  viaGroupId: null,
  viaGroupName: null,
  resourceMetadataUrl: mcpMetadataUrl,
  limits: mcpGrant.enforcement,
}

export const mcpAgentIdentityResolved: ResolvedEntitlement = {
  entitlement: {
    ...mcpGrant,
    id: 'entitlement_mcp_agent_identity',
    subject: { kind: 'application', id: 'principal_agent_identity' },
  },
  resourceSummary: mcpSummary,
  via: 'direct',
  viaGroupId: null,
  viaGroupName: null,
}

export const mcpAgentIdentityConnection: McpConnection = {
  ...mcpConnection,
  entitlementId: mcpAgentIdentityResolved.entitlement.id,
  delegatedScope: null,
  applicationScope: 'api://11111111-2222-3333-4444-555555555555/.default',
  requiredAppRole: 'Mcp.Invoke.Application',
  clientId: '22222222-3333-4444-5555-666666666666',
  principalKind: 'agentIdentity',
}

export const mcpAgentUserResolved: ResolvedEntitlement = {
  entitlement: {
    ...mcpGrant,
    id: 'entitlement_mcp_agent_user',
    subject: { kind: 'user', id: 'principal_agent_user' },
  },
  resourceSummary: mcpSummary,
  via: 'direct',
  viaGroupId: null,
  viaGroupName: null,
}

export const mcpAgentUserConnection: McpConnection = {
  ...mcpConnection,
  entitlementId: mcpAgentUserResolved.entitlement.id,
  clientId: '33333333-4444-5555-6666-777777777777',
  principalKind: 'agentUser',
}

export const mcpSecurityGroupResolved: ResolvedEntitlement = {
  entitlement: {
    ...mcpGrant,
    id: 'entitlement_mcp_security_group',
    subject: { kind: 'securityGroup', id: 'principal_group_1' },
  },
  resourceSummary: mcpSummary,
  via: 'securityGroup',
  viaGroupId: 'principal_group_1',
  viaGroupName: 'AI builders',
}

export const mcpSecurityGroupConnection: McpConnection = {
  ...mcpConnection,
  entitlementId: mcpSecurityGroupResolved.entitlement.id,
  applicationScope: 'api://11111111-2222-3333-4444-555555555555/.default',
  requiredAppRole: 'Mcp.Invoke.Application',
  principalKind: 'securityGroup',
  viaGroupId: 'principal_group_1',
  viaGroupName: 'AI builders',
}

export const endpoint = 'https://gateway.example.test/models/gpt-4o'
export const chatUrl = `${endpoint}/openai/deployments/gpt-4o/chat/completions`
export const responsesUrl = `${endpoint}/openai/responses`

export const connection: ModelConnection = {
  entitlementId: directGrant.id,
  publicationId: 'publication_1',
  gatewayId: 'gateway_1',
  endpoint,
  deploymentName: 'gpt-4o',
  tenantId: 'tenant-1',
  runtime: directGrant.runtime,
  appliedMethods: { keysEnabled: true, entraEnabled: true },
  costCenter: researchCostCenter,
  costCenterHeader: 'x-mosaic-cost-center',
  keyExists: true,
  keysAllowedByCostCenter: true,
  entraAudience: '11111111-2222-3333-4444-555555555555',
  entraScope: 'api://11111111-2222-3333-4444-555555555555/Models.Invoke',
  entraClientId: modelClientId,
  subscriptionHeader: 'Ocp-Apim-Subscription-Key',
  operations: [
    { name: 'chat-completions', method: 'POST', path: '/openai/deployments/gpt-4o/chat/completions' },
    { name: 'responses', method: 'POST', path: '/openai/responses' },
  ],
  publicationLimits: {
    counterKeyExpression: 'publication-counter',
    tokensPerMinute: 20_000,
    tokenQuota: 1_000_000,
    tokenQuotaPeriod: 'Monthly',
    estimatePromptTokens: true,
  },
  grantLimits: directGrant.enforcement,
}

export const securityGroupConnection: ModelConnection = {
  ...connection,
  entitlementId: securityGroupResolved.entitlement.id,
  appliedMethods: { keysEnabled: false, entraEnabled: true },
  runtime: {
    ...directGrant.runtime!,
    subscriptionName: null,
    appliedMethods: { keysEnabled: false, entraEnabled: true },
  },
  principalKind: 'securityGroup',
  requiredAppRole: 'Models.Invoke.Application',
  entraApplicationScope: 'api://11111111-2222-3333-4444-555555555555/.default',
  keysAvailable: false,
  viaGroupId: 'principal_group_1',
  viaGroupName: 'AI builders',
}

export const claudeEndpoint = 'https://gateway.example.test/models/claude'
export const messagesUrl = `${claudeEndpoint}/anthropic/v1/messages`

/** A Claude model on a classic-tier gateway, which can't apply token limits to it. */
export const claudeConnection: ModelConnection = {
  ...connection,
  endpoint: claudeEndpoint,
  deploymentName: 'claude-sonnet-4-5',
  apiShape: 'anthropicMessages',
  operations: [{ name: 'messages', method: 'POST', path: '/anthropic/v1/messages' }],
  publicationLimits: null,
  grantLimits: {
    tokens: null,
    requests: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      calls: 60,
      renewalPeriodSeconds: 60,
      callQuota: null,
      callQuotaPeriod: null,
    },
  },
}

export const poolEndpoint = 'https://gateway.example.test/models/anthropic'
export const poolMessagesUrl = `${poolEndpoint}/anthropic/v1/messages`

/**
 * A grant on a model that several endpoints serve behind one API. Its resource names the model
 * and, as its scope, the group that serves it, but nothing a person reads names either.
 */
export const poolGrant: Entitlement = {
  ...directGrant,
  id: 'entitlement_opus',
  resource: { kind: 'poolModel', id: 'pool_model_opus', scopeId: 'pool_anthropic' },
  binding: null,
  runtime: {
    ...directGrant.runtime!,
    publicationId: 'pool_anthropic',
    subscriptionName: 'mosaic-pool-key-0a1b2c3d',
  },
}

export const poolSummary: ResourceSummary = {
  kind: 'poolModel',
  id: 'pool_model_opus',
  scopeId: 'pool_anthropic',
  displayName: 'Claude Opus',
  gatewayId: 'gateway_1',
  gatewayName: 'Production gateway',
  environment: 'production',
  available: true,
}

export const poolResolved: ResolvedEntitlement = {
  entitlement: poolGrant,
  resourceSummary: poolSummary,
  costCenter: researchCostCenter,
  via: 'direct',
  viaGroupId: null,
  viaGroupName: null,
}

/** The grant's key also works for Sonnet, which the same person holds under the same cost center. */
export const poolConnection: ModelConnection = {
  ...connection,
  entitlementId: poolGrant.id,
  publicationId: 'pool_anthropic',
  endpoint: poolEndpoint,
  deploymentName: 'claude-opus-4-5',
  runtime: poolGrant.runtime,
  apiShape: 'anthropicMessages',
  operations: [{ name: 'messages', method: 'POST', path: '/anthropic/v1/messages' }],
  publicationLimits: {
    counterKeyExpression: 'pool_model_opus',
    tokensPerMinute: 400_000,
    tokenQuota: null,
    tokenQuotaPeriod: null,
    estimatePromptTokens: false,
  },
  poolId: 'pool_anthropic',
  poolModelId: 'pool_model_opus',
  keySharedWith: [
    { poolModelId: 'pool_model_sonnet', displayName: 'Claude Sonnet', publicName: 'claude-sonnet-4-5' },
  ],
}

export const revealedPrimary: KeyRevealResult = {
  entitlementId: directGrant.id,
  subscriptionName: 'grant-subscription',
  slot: 'primary',
  key: 'test-only-primary-secret',
  costCenter: researchCostCenter,
}

export const revealedSecondary: KeyRevealResult = {
  ...revealedPrimary,
  slot: 'secondary',
  key: 'test-only-secondary-secret',
}

/** Shaped like the portal's ApiError without importing the (mocked) API module. */
export function apiFailure(status: number, message: string, code = 'error') {
  return Object.assign(new Error(message), { status, body: { code, message } })
}

function storageText(storage: Storage) {
  return Array.from({ length: storage.length }, (_, index) => {
    const name = storage.key(index) ?? ''
    return `${name}=${storage.getItem(name) ?? ''}`
  }).join('\n')
}

/** Everywhere outside component state that a key must never reach. */
export function persistedText(queryClient: {
  getQueryCache(): { getAll(): { state: unknown }[] }
  getMutationCache(): { getAll(): { state: unknown }[] }
}) {
  return [
    JSON.stringify(queryClient.getQueryCache().getAll().map((query) => query.state)),
    JSON.stringify(queryClient.getMutationCache().getAll().map((mutation) => mutation.state)),
    storageText(window.localStorage),
    storageText(window.sessionStorage),
    window.location.href,
    document.cookie,
  ].join('\n')
}
