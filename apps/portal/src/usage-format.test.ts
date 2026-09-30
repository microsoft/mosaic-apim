import { describe, expect, it } from 'vitest'
import type { MyUsageReport, UsageQuota, UsageResourceRow } from './types'
import {
  aggregateUsage,
  accessLabel,
  attributionExplanation,
  formatUtcDate,
  quotaDetail,
  quotaLabel,
  quotaPercentLabel,
  quotaStatus,
  quotaWindowLabel,
  rateLimitLabel,
  rateLimitPeakLabel,
  relativeTime,
  usageTrackingLabel,
} from './usage-format'

const summary = {
  kind: 'modelApi' as const,
  id: 'chat',
  scopeId: null,
  displayName: 'Chat',
  gatewayId: 'gateway-1',
  gatewayName: 'Gateway',
  environment: 'production',
  available: true,
}

function row(overrides: Partial<UsageResourceRow>): UsageResourceRow {
  return {
    entitlementId: 'grant-1',
    resource: { kind: 'modelApi', id: 'chat', scopeId: null },
    resourceSummary: summary,
    environment: 'production',
    via: 'direct',
    viaGroupName: null,
    enabled: true,
    bound: true,
    linkedBy: 'gatewayLog',
    attribution: 'simulated',
    model: 'gpt-4o',
    requests: 10,
    promptTokens: 100,
    completionTokens: 50,
    totalTokens: 150,
    estimatedCost: 0.01,
    costNote: null,
    quotas: [],
    rateLimits: [],
    ...overrides,
  }
}

const report: MyUsageReport = {
  dataSource: 'simulated',
  period: '30d',
  start: '2026-09-01',
  end: '2026-09-02',
  generatedAt: '2026-09-02T00:00:00Z',
  currency: 'USD',
  totals: {
    requests: 15,
    promptTokens: 100,
    completionTokens: 50,
    totalTokens: 150,
    estimatedCost: 0.01,
    costExcludedResources: 1,
  },
  timeline: [
    {
      date: '2026-09-01',
      entitlementId: 'grant-1',
      environment: 'production',
      requests: 10,
      promptTokens: 100,
      completionTokens: 50,
      totalTokens: 150,
      estimatedCost: 0.01,
      throttled: 1,
      quotaRefused: 0,
      errors: 2,
    },
    {
      date: '2026-09-01',
      entitlementId: 'grant-2',
      environment: null,
      requests: 5,
      promptTokens: null,
      completionTokens: null,
      totalTokens: null,
      estimatedCost: null,
      throttled: 0,
      quotaRefused: 0,
      errors: 0,
    },
  ],
  byEnvironment: [],
  byResource: [
    row({ entitlementId: 'grant-1' }),
    row({
      entitlementId: 'grant-2',
      resource: { kind: 'mcpServer', id: 'docs', scopeId: null },
      resourceSummary: { ...summary, kind: 'mcpServer', id: 'docs', displayName: 'Docs MCP', environment: null },
      environment: null,
      requests: 5,
      promptTokens: null,
      completionTokens: null,
      totalTokens: null,
      estimatedCost: null,
      costNote: 'MCP cost is not metered.',
    }),
  ],
  notes: [],
}

describe('usage-format', () => {
  it('aggregates filtered usage while preserving null cost semantics', () => {
    const aggregated = aggregateUsage(report, { environment: 'unclassified', resource: 'all' })

    expect(aggregated.totals.requests).toBe(5)
    expect(aggregated.totals.totalTokens).toBe(0)
    expect(aggregated.totals.estimatedCost).toBeNull()
    expect(aggregated.totals.costExcludedResources).toBe(1)
    expect(aggregated.totals.errors).toBe(0)
    expect(aggregated.busiestResource?.entitlementId).toBe('grant-2')
  })

  it('uses server totals when no filters are selected', () => {
    const aggregated = aggregateUsage(report, { environment: 'all', resource: 'all' })

    expect(aggregated.totals).toBe(report.totals)
  })

  it('counts resources whose usage is not measured in each filtered environment', () => {
    const unmeasured = row({
      entitlementId: 'grant-3',
      attribution: 'unattributed',
      bound: false,
      linkedBy: null,
      requests: null,
      promptTokens: null,
      completionTokens: null,
      totalTokens: null,
      estimatedCost: null,
    })
    const aggregated = aggregateUsage(
      { ...report, byResource: [...report.byResource, unmeasured] },
      { environment: 'production', resource: 'all' },
    )

    expect(aggregated.byEnvironment).toEqual([
      expect.objectContaining({ environment: 'production', resources: 2, unmeasuredResources: 1, requests: 10 }),
    ])
  })

  it('formats UTC dates without local timezone shifts', () => {
    expect(formatUtcDate('2026-09-01')).toBe('Sep 1')
  })

  it('says how long ago usage was updated', () => {
    const now = new Date('2026-09-30T12:00:00Z')
    expect(relativeTime('2026-09-30T11:59:50Z', now)).toBe('just now')
    expect(relativeTime('2026-09-30T11:48:00Z', now)).toBe('12 min ago')
    expect(relativeTime('2026-09-30T09:00:00Z', now)).toBe('3 hr ago')
    expect(relativeTime('2026-09-27T12:00:00Z', now)).toBe('3 days ago')
  })

  it('formats quota windows and rate limits', () => {
    expect(quotaWindowLabel('Monthly')).toBe('this month')
    expect(rateLimitLabel('tokens', 10000, 60)).toBe('10,000 tokens per minute')
    expect(rateLimitLabel('requests', 60, 60)).toBe('60 requests per 60 seconds')
    expect(rateLimitPeakLabel({ metric: 'tokens', limit: 10000, windowSeconds: 60, peak: 8000 })).toBe(
      'Busiest minute: 8,000 tokens (80%)',
    )
    expect(rateLimitPeakLabel({ metric: 'requests', limit: 60, windowSeconds: 3600, peak: null })).toBeNull()
    const quota: UsageQuota = { metric: 'tokens', limit: 100, period: 'Daily', windowStart: '', windowEnd: '', used: 80, utilization: 0.8 }
    expect(quotaStatus(quota)).toBe('near')
    expect(quotaLabel(quota)).toBe('Tokens: 80 of 100 today')
    expect(quotaDetail({ ...quota, partial: true })).toBe('80% used · Near limit · partial window')
    expect(quotaStatus({ ...quota, used: null, utilization: null })).toBe('unknown')
    expect(quotaLabel({ ...quota, used: null, utilization: null })).toBe('Tokens: up to 100 today')
    expect(quotaDetail({ ...quota, used: null, utilization: null })).toBe('Usage unknown')
    expect(quotaPercentLabel(1)).toBe('100% used')
  })

  it('labels how usage is tracked', () => {
    expect(usageTrackingLabel('gatewayLog')).toBe('Linked from gateway log traces')
    expect(usageTrackingLabel('subscription')).toBe('Linked from the APIM subscription')
    expect(usageTrackingLabel(null)).toBe('Not linked yet')
  })

  it('explains access paths and attribution', () => {
    expect(accessLabel(row({ via: 'securityGroup', viaGroupName: 'Analysts' }))).toBe(
      'Entra security group: Analysts',
    )
    expect(
      attributionExplanation(row({ via: 'securityGroup', viaGroupName: 'Analysts', attribution: 'measured' })),
    ).toBe('Only your own calls through this security-group grant are counted here.')
    expect(attributionExplanation(row({ attribution: 'unattributed' }))).toBe(
      "Usage can't be measured for this grant yet.",
    )
  })
})
