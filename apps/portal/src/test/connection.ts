import type {
  Entitlement,
  KeyRevealResult,
  ModelConnection,
  ResolvedEntitlement,
} from '../types'

const timestamp = '2026-09-01T12:00:00Z'

export const directGrant: Entitlement = {
  id: 'entitlement_direct',
  tenantId: 'tenant-1',
  entityType: 'entitlement',
  subject: { kind: 'user', id: 'principal_1' },
  resource: { kind: 'modelApi', id: 'modelApi_chat', scopeId: null },
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

export const directResolved: ResolvedEntitlement = {
  entitlement: directGrant,
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
  via: 'securityGroup',
  viaGroupId: 'principal_group_1',
  viaGroupName: 'AI builders',
  effective: true,
  shadowedBy: null,
}

export const endpoint = 'https://gateway.example.test/models/gpt-4o'
export const chatUrl = `${endpoint}/openai/deployments/gpt-4o/chat/completions`
export const responsesUrl = `${endpoint}/openai/responses`
export const modelClientId = '99999999-8888-7777-6666-555555555555'

export const connection: ModelConnection = {
  entitlementId: directGrant.id,
  publicationId: 'publication_1',
  gatewayId: 'gateway_1',
  endpoint,
  deploymentName: 'gpt-4o',
  tenantId: 'tenant-1',
  runtime: directGrant.runtime,
  appliedMethods: { keysEnabled: true, entraEnabled: true },
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

export const revealedPrimary: KeyRevealResult = {
  entitlementId: directGrant.id,
  subscriptionName: 'grant-subscription',
  slot: 'primary',
  key: 'test-only-primary-secret',
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
