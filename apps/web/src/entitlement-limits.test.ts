import { describe, expect, it } from 'vitest'
import {
  buildEnforcement, emptyLimitForm, GOVERNED_COUNTER_KEY, hasLimitValues, limitFormFromEnforcement,
  limitFormsEqual, limitInputError, personLimitsFor,
} from './entitlement-limits'
import type { EntitlementEnforcement, PersonLimits, QuotaPeriod } from './types'

const stored: EntitlementEnforcement = {
  tokens: {
    counterKeyExpression: '@("saved-token-counter")', estimatePromptTokens: false,
    tokensPerMinute: 12000, tokenQuota: 50000, tokenQuotaPeriod: 'Monthly',
  },
  requests: {
    counterKeyExpression: '@("saved-call-counter")', calls: 30, renewalPeriodSeconds: 900,
    callQuota: 1000, callQuotaPeriod: 'Weekly',
  },
}

describe('grant limit authoring', () => {
  it.each<QuotaPeriod>(['Hourly', 'Daily', 'Weekly', 'Monthly', 'Yearly'])(
    'builds a quota-only request restriction for %s without an orphan rate window', period => {
      expect(buildEnforcement({ ...emptyLimitForm, callQuota: '1000', callQuotaPeriod: period }, true)).toEqual({
        requests: { counterKeyExpression: GOVERNED_COUNTER_KEY, callQuota: 1000, callQuotaPeriod: period },
      })
    },
  )

  it('keeps token periods, rate windows and call-quota periods independent', () => {
    expect(buildEnforcement({
      ...emptyLimitForm, tokensPerMinute: '12000', tokenQuota: '50000', tokenQuotaPeriod: 'Monthly',
      calls: '30', renewalPeriodSeconds: '60', callQuota: '1000', callQuotaPeriod: 'Weekly',
    }, true)).toEqual({
      tokens: { counterKeyExpression: GOVERNED_COUNTER_KEY, estimatePromptTokens: true, tokensPerMinute: 12000, tokenQuota: 50000, tokenQuotaPeriod: 'Monthly' },
      requests: { counterKeyExpression: GOVERNED_COUNTER_KEY, calls: 30, renewalPeriodSeconds: 60, callQuota: 1000, callQuotaPeriod: 'Weekly' },
    })
  })

  it('round-trips saved quotas and metadata, including an untouched legacy rate window', () => {
    const form = limitFormFromEnforcement(stored)
    expect(limitInputError(form, form)).toBeNull()
    expect(buildEnforcement(form, true, stored)).toEqual(stored)
    const edited = buildEnforcement({ ...form, tokensPerMinute: '6000' }, true, stored)
    expect(edited?.requests).toEqual(stored.requests)
    expect(edited?.tokens).toEqual({ ...stored.tokens, tokensPerMinute: 6000 })
  })

  it('clears either call restriction without removing the other, and clears the complete override explicitly', () => {
    const form = limitFormFromEnforcement(stored)
    expect(buildEnforcement({ ...form, callQuota: '' }, true, stored)?.requests).toEqual({
      counterKeyExpression: '@("saved-call-counter")', calls: 30, renewalPeriodSeconds: 900,
    })
    expect(buildEnforcement({ ...form, calls: '' }, true, stored)?.requests).toEqual({
      counterKeyExpression: '@("saved-call-counter")', callQuota: 1000, callQuotaPeriod: 'Weekly',
    })
    expect(buildEnforcement({ ...emptyLimitForm }, true, stored)).toBeNull()
  })

  it.each(['0', '-1', '1.5', 'NaN', 'Infinity', '9007199254740992'])(
    'refuses invalid quota %s instead of building an unrestricted grant', amount => {
      const form = { ...emptyLimitForm, callQuota: amount }
      expect(limitInputError(form)).toBeTruthy()
      expect(() => buildEnforcement(form, true)).toThrow()
    },
  )

  it('requires a valid quota period and bounds only introduced or changed call rates', () => {
    const invalidPeriod = { ...emptyLimitForm, callQuota: '5', callQuotaPeriod: 'Rolling' as QuotaPeriod }
    expect(limitInputError(invalidPeriod)).toBe('Choose a call quota period.')
    expect(() => buildEnforcement(invalidPeriod, true)).toThrow()
    expect(limitInputError({ ...emptyLimitForm, calls: '5', renewalPeriodSeconds: '300' })).toBeNull()
    expect(limitInputError({ ...emptyLimitForm, calls: '5', renewalPeriodSeconds: '301' })).toMatch(/1-300/)
    const form = limitFormFromEnforcement(stored)
    expect(limitInputError({ ...form, calls: '31' }, form)).toMatch(/1-300/)
    expect(limitInputError({ ...form, callQuota: '2000' }, form)).toBeNull()
  })

  it.each(['calls', 'callQuota', 'renewalPeriodSeconds'] as const)(
    'retains sanitized-empty native badInput for %s instead of inheriting defaults', field => {
      const invalid = { ...emptyLimitForm, invalidNumbers: { [field]: true } }
      expect(hasLimitValues(invalid)).toBe(true)
      expect(limitFormsEqual(invalid, emptyLimitForm)).toBe(false)
      expect(limitInputError(invalid, emptyLimitForm)).toBeTruthy()
      expect(() => buildEnforcement(invalid, true)).toThrow()
      const cleared = { ...invalid, invalidNumbers: { [field]: false } }
      expect(limitFormsEqual(cleared, emptyLimitForm)).toBe(true)
      expect(buildEnforcement(cleared, true)).toBeNull()
    },
  )

  it('resolves per-person quotas by exact pool scope and distinguishes unknown from absent defaults', () => {
    const person: PersonLimits = {
      tokensPerMinute: null, tokenQuota: null, tokenQuotaPeriod: null, callsPerMinute: 30,
      callQuota: 1000, callQuotaPeriod: 'Weekly',
    }
    const center = { limits: [
      { resource: { kind: 'poolModel' as const, id: 'shared-model', scopeId: 'pool-a' }, person, pool: null },
      { resource: { kind: 'poolModel' as const, id: 'shared-model', scopeId: 'pool-b' }, person: { ...person, callQuota: 2000 }, pool: null },
    ] }
    expect(personLimitsFor(center, { kind: 'poolModel', id: 'shared-model', scopeId: 'pool-b' })?.callQuota).toBe(2000)
    expect(personLimitsFor(center, { kind: 'poolModel', id: 'shared-model', scopeId: 'pool-c' })).toBeNull()
    expect(personLimitsFor(undefined, { kind: 'poolModel', id: 'shared-model', scopeId: 'pool-a' })).toBeUndefined()
  })
})
