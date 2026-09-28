import type {
  AccessRequest,
  CatalogEntry,
  Entitlement,
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementRuntime,
  QuotaPeriod,
  ResolvedEntitlement,
  TokenEnforcement,
} from './types'

const kindLabels: Record<EntitlementResource['kind'], string> = {
  modelApi: 'Model API',
  mcpServer: 'MCP server',
  modelDeployment: 'Model deployment',
  product: 'Product',
}

const periodLabels: Record<QuotaPeriod, string> = {
  Hourly: 'hour',
  Daily: 'day',
  Weekly: 'week',
  Monthly: 'month',
  Yearly: 'year',
}

const stateLabels: Record<AccessRequest['state'], string> = {
  pending: 'Pending',
  approved: 'Approved',
  denied: 'Denied',
  withdrawn: 'Withdrawn',
}

const runtimeLabels: Record<EntitlementRuntime['status'], string> = {
  pending: 'APIM changes pending',
  applying: 'Applying to APIM',
  applied: 'Applied to APIM',
  revocationPending: 'APIM revocation pending',
  revoked: 'Runtime access revoked',
  failed: 'APIM apply failed',
  unknown: 'Runtime status unknown',
}

export function runtimeStatusLabel(status: EntitlementRuntime['status']) {
  return runtimeLabels[status]
}

export function describeRuntime(entitlement: Entitlement) {
  if (entitlement.runtime) return runtimeStatusLabel(entitlement.runtime.status)
  return entitlement.enabled ? 'Recorded grant' : 'Disabled grant'
}

export function resourceKindLabel(kind: EntitlementResource['kind'] | CatalogEntry['kind']) {
  return kindLabels[kind]
}

export function requestStateLabel(state: AccessRequest['state']) {
  return stateLabels[state]
}

export function resourceLabel(resource: EntitlementResource) {
  return `${resourceKindLabel(resource.kind)} ${resource.id}`
}

export function formatNumber(value: number) {
  return new Intl.NumberFormat('en-US').format(value)
}

export function describeAttribution(resolved: ResolvedEntitlement) {
  if (resolved.via === 'direct') {
    return 'Granted directly to you'
  }
  const groupName = resolved.viaGroupName ?? resolved.viaGroupId ?? 'an assigned group'
  return `Granted through ${groupName}`
}

export function describeBinding(entitlement: Entitlement) {
  if (!entitlement.binding) {
    return 'No usage attribution is configured yet.'
  }
  const pieces = [
    entitlement.binding.apimProductName ? `Product ${entitlement.binding.apimProductName}` : null,
    entitlement.binding.apimSubscriptionName ? `Subscription ${entitlement.binding.apimSubscriptionName}` : null,
    entitlement.binding.gatewayId ? `Gateway ${entitlement.binding.gatewayId}` : null,
  ].filter(Boolean)
  return pieces.length > 0 ? pieces.join(' · ') : 'Usage attribution is configured.'
}

export function describeTokenLimits(tokens: TokenEnforcement | null | undefined) {
  const limits: string[] = []
  if (tokens?.tokensPerMinute != null) {
    limits.push(`${formatNumber(tokens.tokensPerMinute)} tokens per minute`)
  }
  if (tokens?.tokenQuota != null && tokens.tokenQuotaPeriod) {
    limits.push(`${formatNumber(tokens.tokenQuota)} tokens per ${periodLabels[tokens.tokenQuotaPeriod]}`)
  }
  return limits
}

export function describeEnforcementLimits(enforcement: EntitlementEnforcement | null | undefined) {
  if (!enforcement) {
    return ['No additional grant limits configured']
  }
  const limits = describeTokenLimits(enforcement.tokens)
  if (
    enforcement.requests?.calls != null &&
    enforcement.requests.renewalPeriodSeconds != null
  ) {
    limits.push(
      `${formatNumber(enforcement.requests.calls)} calls per ${formatNumber(enforcement.requests.renewalPeriodSeconds)} seconds`,
    )
  }
  if (enforcement.requests?.callQuota != null && enforcement.requests.callQuotaPeriod) {
    limits.push(
      `${formatNumber(enforcement.requests.callQuota)} calls per ${periodLabels[enforcement.requests.callQuotaPeriod]}`,
    )
  }
  return limits.length > 0 ? limits : ['No additional grant limits configured']
}

export function describeLimits(entitlement: Entitlement) {
  return describeEnforcementLimits(entitlement.enforcement)
}

export function sameResource(a: EntitlementResource, b: EntitlementResource) {
  return a.kind === b.kind && a.id === b.id
}

export function resourceFromCatalog(entry: CatalogEntry): EntitlementResource {
  // scopeId is only meaningful for observed resources (product, modelDeployment). A catalog entry
  // is always a desired-state record that carries its own gateway, and sending a scopeId here
  // would change the deterministic ID of any entitlement later created from the request.
  return { kind: entry.kind, id: entry.id, scopeId: null }
}
