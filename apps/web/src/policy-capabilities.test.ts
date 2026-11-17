import { describe, expect, it } from 'vitest'
import { ApiError } from './api'
import {
  GATEWAY_POLICY_NAMES, isGatewayPolicyCapabilities, policyCapabilityFailure,
} from './policy-capabilities'
import type { GatewayPolicyCapabilities, PolicyCapabilityIssue } from './types'

export function unknownCapabilities(): GatewayPolicyCapabilities {
  return {
    gatewayId: 'gateway', skuName: null, tier: 'unknown', apiShape: null,
    policies: {
      'rate-limit-by-key': policy('rate-limit-by-key'),
      'quota-by-key': policy('quota-by-key'),
      'llm-token-limit': policy('llm-token-limit'),
      'llm-emit-token-metric': policy('llm-emit-token-metric'),
    },
  }
}

function policy(name: typeof GATEWAY_POLICY_NAMES[number]) {
  return {
    policy: name, support: 'unknown' as const, reason: 'unknownTier' as const,
    message: 'Check gateway access.', nextAction: 'checkGatewayAccess' as const,
    documentationUrl: `https://learn.microsoft.com/en-us/azure/api-management/${name}-policy`,
  }
}

export function capabilityIssue(): PolicyCapabilityIssue {
  return {
    ...policy('rate-limit-by-key'), field: 'enforcement.requests.calls',
    resource: { kind: 'poolModel', id: 'same', scopeId: 'pool' },
    gatewayId: 'gateway', skuName: null, tier: 'unknown', apiShape: null,
  }
}

describe('policy capability results', () => {
  it('retains unknown and requires the complete correctly keyed result map', () => {
    const complete = unknownCapabilities()
    expect(isGatewayPolicyCapabilities(complete)).toBe(true)
    expect(isGatewayPolicyCapabilities({ ...complete, policies: {} })).toBe(false)
    expect(isGatewayPolicyCapabilities({
      ...complete, policies: { ...complete.policies, 'quota-by-key': policy('llm-token-limit') },
    })).toBe(false)
  })

  it('preserves exact scoped fields, action, and recovery from an actual API refusal', () => {
    const issue = capabilityIssue()
    const details = { reason: 'gatewayPolicyLimitsRejected', boundary: 'save', issues: [issue] }
    const error = new ApiError('Nothing was saved.', 422, {
      code: 'validation_error', message: 'Nothing was saved.', details,
    })
    expect(policyCapabilityFailure(error)).toEqual(details)
    expect(policyCapabilityFailure(error)?.issues[0].resource?.scopeId).toBe('pool')
  })

  it('does not treat malformed or other errors as supported capability results', () => {
    expect(policyCapabilityFailure(new Error('Network unavailable.'))).toBeNull()
    expect(policyCapabilityFailure(new ApiError('Denied', 422, {
      code: 'validation_error', message: 'Denied',
      details: { reason: 'gatewayPolicyLimitsRejected', boundary: 'save', issues: [{ support: 'available' }] },
    }))).toBeNull()
    expect(isGatewayPolicyCapabilities({
      ...unknownCapabilities(), resource: { kind: 'poolModel', id: 'same' },
    })).toBe(false)
    expect(isGatewayPolicyCapabilities({
      ...unknownCapabilities(), resource: { kind: 'poolModel', id: 'same', scopeId: '' },
    })).toBe(false)
  })
})
