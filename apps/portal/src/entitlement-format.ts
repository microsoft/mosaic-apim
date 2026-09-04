import type {
  AccessRequest,
  CatalogEntry,
  Entitlement,
  EntitlementResource,
  QuotaPeriod,
  ResolvedEntitlement,
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
    entitlement.binding.productName ? `Product ${entitlement.binding.productName}` : null,
    entitlement.binding.subscriptionName ? `Subscription ${entitlement.binding.subscriptionName}` : null,
    entitlement.binding.gatewayId ? `Gateway ${entitlement.binding.gatewayId}` : null,
  ].filter(Boolean)
  return pieces.length > 0 ? pieces.join(' · ') : 'Usage attribution is configured.'
}

export function describeLimits(entitlement: Entitlement) {
  const enforcement = entitlement.enforcement
  if (!enforcement) {
    return ['No limits applied']
  }
  const limits: string[] = []
  if (enforcement.tokens?.tokensPerMinute != null) {
    limits.push(`${formatNumber(enforcement.tokens.tokensPerMinute)} tokens per minute`)
  }
  if (enforcement.tokens?.tokenQuota != null && enforcement.tokens.tokenQuotaPeriod) {
    limits.push(
      `${formatNumber(enforcement.tokens.tokenQuota)} tokens per ${periodLabels[enforcement.tokens.tokenQuotaPeriod]}`,
    )
  }
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
  return limits.length > 0 ? limits : ['No limits applied']
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
