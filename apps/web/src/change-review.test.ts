import { afterEach, describe, expect, it, vi } from 'vitest'
import { classifyChanges, creationContext, grantReviewValues, poolReviewValues, rememberEdit, takeEditContext } from './change-review'
import type { ReviewChange } from './types'
import { accessPlan, directGrant } from './test/model-access'
import { anthropicPool } from './test/pool-fixtures'

const change: ReviewChange = {
  key: 'grants/alex/tokensPerMinute', category: 'Limits', label: 'Alex - tokens/minute',
  before: '1000', after: '2000', previousKnown: true,
}

afterEach(() => vi.useRealTimers())

describe('review edit context', () => {
  it('separates the current edit from earlier saved changes and shows supersession once', () => {
    const earlier = { ...change, key: 'grants/app/enabled', before: 'Enabled', after: 'Disabled' }
    rememberEdit('pub', { [change.key]: '1500' }, { [change.key]: '2000' })
    const context = takeEditContext('pub')
    const result = classifyChanges([change, earlier], context)
    expect(result.yours).toEqual([change])
    expect(result.pending).toEqual([earlier])
    expect(result.replaced.get(change.key)).toBe('1500')
    expect(takeEditContext('pub')).toBeUndefined()
  })

  it('never attributes a concurrent or stale final value to this edit', () => {
    rememberEdit('concurrent', { [change.key]: '1000' }, { [change.key]: '1800' })
    const result = classifyChanges([change], takeEditContext('concurrent'))
    expect(result.yours).toEqual([])
    expect(result.pending).toEqual([change])
  })

  it('does not invent an apply change when the current edit canceled pending intent', () => {
    rememberEdit('cancel', { [change.key]: '1500' }, { [change.key]: '1000' })
    expect(classifyChanges([], takeEditContext('cancel')).yours).toEqual([])
  })

  it('uses neutral pending attribution without context or after expiry', () => {
    expect(classifyChanges([change]).pending).toEqual([change])
    vi.useFakeTimers()
    rememberEdit('expired', {}, { [change.key]: '2000' })
    vi.advanceTimersByTime(16 * 60_000)
    expect(takeEditContext('expired')).toBeUndefined()
  })

  it('attributes only known creation fields, not grants saved by someone else or a later replan', () => {
    const context = creationContext({
      ...accessPlan,
      review: {
        version: 1, baseline: 'new', appliedAt: null, changes: [], impact: [],
        target: { version: 1, fields: [
          { key: 'publication/apiPath', label: 'Path', category: 'Routing', value: 'models/chat' },
          { key: change.key, label: 'Tokens', category: 'Limits', value: '2000' },
        ] },
      },
    })
    const config = { ...change, key: 'publication/apiPath', after: 'models/chat' }
    expect(classifyChanges([change, config], context).yours).toEqual([config])
    expect(classifyChanges([{ ...config, after: 'models/other' }], context).yours).toEqual([])
  })

  it('never stores credential-bearing counter expressions in edit context', () => {
    const values = grantReviewValues({
      ...directGrant,
      enforcement: { tokens: { counterKeyExpression: 'secret', tokensPerMinute: 1000, estimatePromptTokens: true } },
    })
    expect(JSON.stringify(values)).not.toContain('secret')
    expect(values[`grants/${directGrant.id}/tokensPerMinute`]).toBe('1000')
    expect(values[`grants/${directGrant.id}/limitsSource`]).toBe('Grant override')
  })

  it('does not label compiled values of a newly created grant as earlier saves', () => {
    const prefix = `grants/${directGrant.id}/`
    const fields = {
      ...grantReviewValues(directGrant),
      [`${prefix}subject`]: 'caller-object',
      [`${prefix}costCenter`]: 'Research',
      [`${prefix}keysAllowed`]: 'Enabled',
      [`${prefix}defaultCostCenter`]: 'Enabled',
      [`${prefix}tokensPerMinute`]: '1000',
      'quotas/research//monthlyTokens': '100000',
    }
    const changes = Object.entries(fields).map(([key, after]) => ({
      ...change, key, before: null, after,
    }))
    rememberEdit('new-grant', {}, grantReviewValues(directGrant), [prefix, 'quotas/'])
    const result = classifyChanges(changes, takeEditContext('new-grant'))
    expect(result.yours.map((row) => row.key)).toEqual([`${prefix}enabled`, `${prefix}limitsSource`])
    expect(result.pending).toEqual([])
    expect(result.unattributed.map((row) => row.key)).toEqual(
      Object.keys(fields).filter((key) => !Object.hasOwn(grantReviewValues(directGrant), key)),
    )
    expect(result.yours.length + result.unattributed.length).toBe(changes.length)
  })

  it('keeps inherited values neutral when an override is cleared instead of claiming an earlier save', () => {
    const prefix = `grants/${directGrant.id}/`
    const before = {
      ...directGrant,
      enforcement: { tokens: { counterKeyExpression: 'secret', tokensPerMinute: 2000, estimatePromptTokens: true } },
    }
    rememberEdit('inherit-grant', grantReviewValues(before), grantReviewValues(directGrant), [prefix, 'quotas/'])
    const inherited = { ...change, key: `${prefix}tokensPerMinute`, before: '2000', after: '1000' }
    const source = { ...change, key: `${prefix}limitsSource`, before: 'Grant override', after: 'Cost-center defaults' }
    const earlier = { ...change, key: 'grants/earlier/tokensPerMinute' }
    const result = classifyChanges([inherited, source, earlier], takeEditContext('inherit-grant'))
    expect(result.yours).toEqual([source])
    expect(result.unattributed).toEqual([inherited])
    expect(result.pending).toEqual([earlier])
  })
})

describe('pool review values', () => {
  it.each(['Updated purpose', null])('records an edited or cleared API description: %s', (description) => {
    const after = { ...anthropicPool, description }
    const beforeValues = poolReviewValues(anthropicPool)
    const afterValues = poolReviewValues(after)
    const key = 'pool/description'
    expect(afterValues[key]).toBe(description || `The models in the ${anthropicPool.displayName} pool, served by MOSAIC.`)
    rememberEdit('description', beforeValues, afterValues)
    expect(classifyChanges([{
      ...change, key, before: beforeValues[key], after: afterValues[key],
    }], takeEditContext('description')).yours).toHaveLength(1)
  })

  it.each(['breaker', 'preferential', 'linear'] as const)('records effective active attempt order for %s pools', (poolType) => {
    const original = anthropicPool.models[0]
    const base = original.members[0]
    const member = (id: string) => ({ ...base, backendName: id, modelEndpointId: id, drained: false })
    const pool = {
      ...anthropicPool, poolType,
      models: [{
        ...original, id: 'chat',
        members: [member('east'), { ...member('drained'), drained: true }, member('identity'), member('west')],
      }],
    }
    const keyed = new Set(['east', 'west', 'drained'])
    const before = poolReviewValues(pool, keyed)
    expect(before['models/chat/members/east/order']).toBe('1')
    expect(before['models/chat/members/west/order']).toBe(poolType === 'linear' ? '3' : '2')
    expect(before['models/chat/members/drained/order']).toBeUndefined()
    if (poolType !== 'linear') expect(before['models/chat/members/identity/order']).toBeUndefined()
    const after = poolReviewValues({
      ...pool, models: [{ ...pool.models[0], members: [...pool.models[0].members].reverse() }],
    }, keyed)
    rememberEdit('routing', before, after)
    const changes = ['east', 'west'].map((id) => {
      const key = `models/chat/members/${id}/order`
      return { ...change, key, before: before[key], after: after[key] }
    })
    expect(classifyChanges(changes, takeEditContext('routing')).yours).toEqual(changes)
  })
})
