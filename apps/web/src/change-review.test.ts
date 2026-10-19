import { afterEach, describe, expect, it, vi } from 'vitest'
import { classifyChanges, creationContext, grantReviewValues, rememberEdit, takeEditContext } from './change-review'
import type { ReviewChange } from './types'
import { accessPlan, directGrant } from './test/model-access'

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
})
