import type {
  AccessRequest,
  ApiShape,
  CatalogEntry,
  Entitlement,
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementRuntime,
  ModelCapacity,
  QuotaPeriod,
  ResourceSummary,
  ResolvedEntitlement,
  TokenEnforcement,
} from './types'

// A pool model is a model to the people who use it: nothing here says it's pooled.
const kindLabels: Record<EntitlementResource['kind'], string> = {
  modelApi: 'Model API',
  mcpServer: 'MCP server',
  poolModel: 'Model',
  modelDeployment: 'Model deployment',
  product: 'Product',
}

const capacityLabels: Record<ModelCapacity, string> = {
  provisioned: 'Provisioned',
  payAsYouGo: 'Pay-as-you-go',
  provisionedWithOverflow: 'Provisioned, with pay-as-you-go overflow',
}

const apiStyleLabels: Record<ApiShape, string> = {
  azureOpenAi: 'Azure OpenAI API',
  foundryModels: 'Foundry Models API',
  anthropicMessages: 'Anthropic Messages API',
}

export function capacityLabel(capacity: ModelCapacity) {
  return capacityLabels[capacity]
}

export function apiStyleLabel(apiStyle: ApiShape) {
  return apiStyleLabels[apiStyle]
}

/** Whether a resource is called as a model, so its grants have connection details and keys. */
export function isModelResource(kind: EntitlementResource['kind']) {
  return kind === 'modelApi' || kind === 'poolModel'
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
  if (entitlement.resource.kind === 'mcpServer') {
    return entitlement.runtime
      ? runtimeStatusLabel(entitlement.runtime.status)
      : 'Recorded, not enforced by MOSAIC'
  }
  if (entitlement.runtime) return runtimeStatusLabel(entitlement.runtime.status)
  return entitlement.enabled ? 'Recorded grant' : 'Disabled grant'
}

export function resourceKindLabel(kind: EntitlementResource['kind'] | CatalogEntry['kind']) {
  return kindLabels[kind]
}

export function requestStateLabel(state: AccessRequest['state']) {
  return stateLabels[state]
}

function usableName(displayName: string | null | undefined) {
  const name = displayName?.trim()
  return name ? name : null
}

/**
 * Names a resource without its ID: the summary's name, then whether it is gone, then its kind.
 * End users never see a raw resource ID, so this is the last resort for every title below.
 */
export function resourceLabel(resource: EntitlementResource, summary?: ResourceSummary | null) {
  const name = usableName(summary?.displayName)
  if (name) return name
  if (summary?.available === false) return 'Resource no longer available'
  return `${resourceKindLabel(resource.kind)} resource`
}

/**
 * Heads a grant or request with the name the catalog shows for its resource. When that name is
 * missing (an older API), null (a resource the API can't resolve), or blank, it falls back to
 * {@link resourceLabel}, so the heading is never a raw ID.
 */
export function resourceTitle(
  resource: EntitlementResource,
  displayName: string | null | undefined,
  summary?: ResourceSummary | null,
) {
  return usableName(displayName) ?? resourceLabel(resource, summary)
}

/** Secondary text for a card, led by the resource's kind unless the title already states it. */
export function withResourceKind(
  resource: EntitlementResource,
  displayName: string | null | undefined,
  detail: string,
  summary?: ResourceSummary | null,
) {
  const titleIsKind =
    !usableName(displayName) && !usableName(summary?.displayName) && summary?.available !== false
  return titleIsKind ? detail : `${resourceKindLabel(resource.kind)} · ${detail}`
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

export function isSecurityGroupGrant(resolved: ResolvedEntitlement) {
  return resolved.via === 'securityGroup' || resolved.entitlement.subject.kind === 'securityGroup'
}

export function describeBinding(entitlement: Entitlement) {
  const binding = entitlement.binding
  if (binding?.attributionKey) {
    return binding.attributionPerMember
      ? 'The gateway records each call you make with this grant, so your own usage can be measured.'
      : 'The gateway records each call made with this grant, so its usage can be measured.'
  }
  if (binding?.apimSubscriptionName) {
    return binding.apimProductName
      ? `Linked through APIM subscription ${binding.apimSubscriptionName} in product ${binding.apimProductName}.`
      : `Linked through APIM subscription ${binding.apimSubscriptionName}.`
  }
  return "Usage can't be measured for this grant yet."
}

export function gatewayLabel(summary: ResourceSummary | null | undefined, fallback?: string | null) {
  return summary?.gatewayName ?? fallback ?? 'Gateway not available'
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

export function describeRequestLimits(enforcement: EntitlementEnforcement | null | undefined) {
  const limits: string[] = []
  if (
    enforcement?.requests?.calls != null &&
    enforcement.requests.renewalPeriodSeconds != null
  ) {
    limits.push(
      `${formatNumber(enforcement.requests.calls)} calls per ${formatNumber(enforcement.requests.renewalPeriodSeconds)} seconds`,
    )
  }
  if (enforcement?.requests?.callQuota != null && enforcement.requests.callQuotaPeriod) {
    limits.push(
      `${formatNumber(enforcement.requests.callQuota)} calls per ${periodLabels[enforcement.requests.callQuotaPeriod]}`,
    )
  }
  return limits.length > 0 ? limits : ['No call limits configured']
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
  // A model API or MCP server is a desired-state record that carries its own gateway, so it takes
  // no scopeId: sending one would change the deterministic ID of any entitlement later created
  // from the request. A pool model is scoped to its pool, which the API requires.
  return {
    kind: entry.kind,
    id: entry.id,
    scopeId: entry.kind === 'poolModel' ? (entry.scopeId ?? null) : null,
  }
}
