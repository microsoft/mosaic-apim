import { describe, expect, it } from 'vitest'
import type { MyUsageReport, UsageResourceRow } from './types'
import {
  aggregateUsage,
  formatUtcDate,
  quotaWindowLabel,
  rateLimitLabel,
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
    expect(aggregated.busiestResource?.entitlementId).toBe('grant-2')
  })

  it('uses server totals when no filters are selected', () => {
    const aggregated = aggregateUsage(report, { environment: 'all', resource: 'all' })

    expect(aggregated.totals).toBe(report.totals)
  })

  it('formats UTC dates without local timezone shifts', () => {
    expect(formatUtcDate('2026-09-01')).toBe('Sep 1')
  })

  it('formats quota windows and rate limits', () => {
    expect(quotaWindowLabel('Monthly')).toBe('this month')
    expect(rateLimitLabel('tokens', 10000, 60)).toBe('10,000 tokens per minute')
    expect(rateLimitLabel('requests', 60, 60)).toBe('60 requests per 60 seconds')
  })

  it('labels how usage is tracked', () => {
    expect(usageTrackingLabel('gatewayLog')).toBe('At the gateway')
    expect(usageTrackingLabel('subscription')).toBe('By APIM subscription')
    expect(usageTrackingLabel(null)).toBe('Not linked yet')
  })
})
