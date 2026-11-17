import type {
  EntitlementResource,
  GatewayPolicyCapabilities,
  GatewayPolicyName,
  PolicyCapability,
  PolicyCapabilityIssue,
} from './types'

export const GATEWAY_POLICY_NAMES: readonly GatewayPolicyName[] = [
  'rate-limit-by-key', 'quota-by-key', 'llm-token-limit', 'llm-emit-token-metric',
]

export const POLICY_LABELS: Record<GatewayPolicyName, string> = {
  'rate-limit-by-key': 'Call rate',
  'quota-by-key': 'Call quota',
  'llm-token-limit': 'Token limit',
  'llm-emit-token-metric': 'Token metrics',
}

const SUPPORT = ['available', 'unavailable', 'unknown']
const TIERS = ['classic', 'v2', 'consumption', 'unknown']
const SHAPES = ['azureOpenAi', 'foundryModels', 'anthropicMessages']
const REASONS = [
  'documentedSupport', 'unsupportedTier', 'unknownTier', 'unverifiedSku',
  'unsupportedApiShape', 'unknownApiShape',
]
const ACTIONS = ['none', 'checkGatewayAccess', 'reviewResource', 'chooseSupportedGateway', 'reviewLimit']

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function oneOf(value: unknown, allowed: readonly string[]): value is string {
  return typeof value === 'string' && allowed.includes(value)
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

function resource(value: unknown): value is EntitlementResource | null {
  if (value === null) return true
  return record(value)
    && oneOf(value.kind, ['modelApi', 'poolModel', 'mcpServer', 'product', 'modelDeployment'])
    && typeof value.id === 'string' && value.id.length > 0
    && (value.scopeId === undefined || nullableString(value.scopeId))
    && (!['poolModel', 'product', 'modelDeployment'].includes(value.kind)
      || (typeof value.scopeId === 'string' && value.scopeId.length > 0))
}

function capabilityFields(value: Record<string, unknown>, allowInvalid = false): boolean {
  return oneOf(value.policy, GATEWAY_POLICY_NAMES)
    && oneOf(value.support, SUPPORT)
    && (oneOf(value.reason, REASONS) || (allowInvalid && value.reason === 'invalidPolicyValue'))
    && typeof value.message === 'string'
    && oneOf(value.nextAction, ACTIONS)
    && value.documentationUrl === `https://learn.microsoft.com/en-us/azure/api-management/${value.policy}-policy`
}

export function isPolicyCapability(value: unknown): value is PolicyCapability {
  return record(value) && capabilityFields(value)
    && (value.maximumRenewalPeriodSeconds === undefined
      || value.maximumRenewalPeriodSeconds === null
      || value.maximumRenewalPeriodSeconds === 300)
}

export function isGatewayPolicyCapabilities(value: unknown): value is GatewayPolicyCapabilities {
  if (!record(value) || !record(value.policies)) return false
  const policies = value.policies
  return nullableString(value.gatewayId)
    && nullableString(value.skuName)
    && oneOf(value.tier, TIERS)
    && (value.apiShape === null || oneOf(value.apiShape, SHAPES))
    && (value.resource === undefined || resource(value.resource))
    && (value.observedAt === undefined || nullableString(value.observedAt))
    && GATEWAY_POLICY_NAMES.every(name => {
      const capability = policies[name]
      return isPolicyCapability(capability) && capability.policy === name
    })
}

export function isPolicyCapabilityIssue(value: unknown): value is PolicyCapabilityIssue {
  return record(value) && capabilityFields(value, true)
    && typeof value.field === 'string'
    && resource(value.resource)
    && nullableString(value.gatewayId)
    && nullableString(value.skuName)
    && oneOf(value.tier, TIERS)
    && (value.apiShape === null || oneOf(value.apiShape, SHAPES))
    && (value.minimum === undefined || value.minimum === null || Number.isSafeInteger(value.minimum))
    && (value.maximum === undefined || value.maximum === null || Number.isSafeInteger(value.maximum))
}

export interface PolicyCapabilityFailure {
  reason: 'gatewayPolicyLimitsRejected'
  boundary: 'save' | 'plan' | 'apply'
  issues: PolicyCapabilityIssue[]
}

function failureDetails(value: unknown): value is PolicyCapabilityFailure {
  return record(value) && value.reason === 'gatewayPolicyLimitsRejected'
    && oneOf(value.boundary, ['save', 'plan', 'apply'])
    && Array.isArray(value.issues) && value.issues.length > 0
    && value.issues.every(isPolicyCapabilityIssue)
}

export function policyCapabilityFailure(error: unknown): PolicyCapabilityFailure | null {
  if (!record(error) || !record(error.body) || error.body.code !== 'validation_error') return null
  return failureDetails(error.body.details) ? error.body.details : null
}

export function policyDocumentationUrl(policy: GatewayPolicyName): string {
  return `https://learn.microsoft.com/en-us/azure/api-management/${policy}-policy`
}

const FIELD_LABELS: Record<string, string> = {
  tokensPerMinute: 'Tokens per minute',
  tokenQuota: 'Token quota',
  calls: 'Calls',
  renewalPeriodSeconds: 'Per how many seconds',
  callQuota: 'Call quota',
  monthlyTokens: 'Monthly pooled tokens',
  monthlyCalls: 'Monthly pooled calls',
  callsPerMinute: 'Calls per minute',
}

export function policyFieldLabel(issue: PolicyCapabilityIssue): string {
  const field = issue.field.split('.').at(-1) ?? ''
  return FIELD_LABELS[field] ?? POLICY_LABELS[issue.policy]
}

export function policyReviewFields(
  root: HTMLElement | null, error: unknown,
): Record<string, HTMLElement> {
  const result: Record<string, HTMLElement> = {}
  const failure = policyCapabilityFailure(error)
  if (!root || !failure) return result
  for (const issue of failure.issues) {
    if (!issue.field.startsWith('enforcement.')) continue
    const label = [...root.querySelectorAll<HTMLLabelElement>('label')].find(
      candidate => candidate.textContent?.trim().replace(/\s*\*$/, '') === policyFieldLabel(issue),
    )
    const control = label?.htmlFor ? root.ownerDocument.getElementById(label.htmlFor) : null
    if (control instanceof HTMLElement && root.contains(control)) result[issue.field] = control
  }
  return result
}
