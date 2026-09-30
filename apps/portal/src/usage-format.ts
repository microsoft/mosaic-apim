import { environmentLabel } from './environments'
import { formatNumber, resourceLabel } from './entitlement-format'
import type {
  MyUsageReport,
  PortalEnvironment,
  UsageEnvironmentBreakdown,
  UsagePeriod,
  UsageResourceRow,
  UsageTimelinePoint,
  UsageTotals,
} from './types'

export type UsageEnvironmentFilter = 'all' | 'unclassified' | string

export interface UsageFilters {
  environment: UsageEnvironmentFilter
  resource: 'all' | string
}

export interface AggregatedUsage {
  totals: UsageTotals
  byEnvironment: UsageEnvironmentBreakdown[]
  byResource: UsageResourceRow[]
  timeline: UsageTimelinePoint[]
  busiestResource: UsageResourceRow | null
}

const periodLabels: Record<UsagePeriod, string> = {
  '7d': 'Last 7 days',
  '30d': 'Last 30 days',
  '90d': 'Last 90 days',
}

export function usagePeriodLabel(period: UsagePeriod) {
  return periodLabels[period]
}

export function matchesEnvironmentFilter(environment: string | null, filter: UsageEnvironmentFilter) {
  if (filter === 'all') return true
  if (filter === 'unclassified') return environment === null
  return environment === filter
}

function rowMatches(row: UsageResourceRow, filters: UsageFilters) {
  return (
    matchesEnvironmentFilter(row.environment, filters.environment) &&
    (filters.resource === 'all' || row.entitlementId === filters.resource)
  )
}

function pointMatches(point: UsageTimelinePoint, entitlementIds: Set<string>, filters: UsageFilters) {
  return (
    matchesEnvironmentFilter(point.environment, filters.environment) &&
    (filters.resource === 'all' || entitlementIds.has(point.entitlementId))
  )
}

function sumNullable(values: Array<number | null>) {
  let hasValue = false
  const sum = values.reduce<number>((total, value) => {
    if (value === null) return total
    hasValue = true
    return total + value
  }, 0)
  return hasValue ? sum : null
}

export function aggregateTotals(points: UsageTimelinePoint[], rows: UsageResourceRow[]): UsageTotals {
  const estimatedCost = sumNullable(points.map((point) => point.estimatedCost))
  return {
    requests: points.reduce((total, point) => total + (point.requests ?? 0), 0),
    promptTokens: points.reduce((total, point) => total + (point.promptTokens ?? 0), 0),
    completionTokens: points.reduce((total, point) => total + (point.completionTokens ?? 0), 0),
    totalTokens: points.reduce((total, point) => total + (point.totalTokens ?? 0), 0),
    estimatedCost,
    costExcludedResources: rows.filter((row) => row.estimatedCost === null).length,
  }
}

function aggregateByEnvironment(points: UsageTimelinePoint[], rows: UsageResourceRow[]) {
  const environments = new Map<string, UsageEnvironmentBreakdown>()
  const resourcesByEnvironment = new Map<string, Set<string>>()
  const costExcludedByEnvironment = new Map<string, Set<string>>()
  const keyFor = (environment: string | null) => environment ?? '__unclassified__'
  const environmentFor = (key: string) => (key === '__unclassified__' ? null : key)

  for (const row of rows) {
    const key = keyFor(row.environment)
    if (!resourcesByEnvironment.has(key)) resourcesByEnvironment.set(key, new Set())
    resourcesByEnvironment.get(key)?.add(row.entitlementId)
    if (row.estimatedCost === null) {
      if (!costExcludedByEnvironment.has(key)) costExcludedByEnvironment.set(key, new Set())
      costExcludedByEnvironment.get(key)?.add(row.entitlementId)
    }
  }

  for (const point of points) {
    const key = keyFor(point.environment)
    const current =
      environments.get(key) ??
      {
        environment: environmentFor(key),
        resources: 0,
        requests: 0,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        estimatedCost: null,
        costExcludedResources: 0,
      }
    current.requests += point.requests ?? 0
    current.promptTokens += point.promptTokens ?? 0
    current.completionTokens += point.completionTokens ?? 0
    current.totalTokens += point.totalTokens ?? 0
    if (point.estimatedCost !== null) {
      current.estimatedCost = (current.estimatedCost ?? 0) + point.estimatedCost
    }
    environments.set(key, current)
  }

  for (const [key, resources] of resourcesByEnvironment) {
    const current =
      environments.get(key) ??
      {
        environment: environmentFor(key),
        resources: 0,
        requests: 0,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        estimatedCost: null,
        costExcludedResources: 0,
      }
    current.resources = resources.size
    current.costExcludedResources = costExcludedByEnvironment.get(key)?.size ?? 0
    environments.set(key, current)
  }

  return Array.from(environments.values())
}

export function busiestResource(rows: UsageResourceRow[]) {
  const busiest = rows.reduce<UsageResourceRow | null>((current, row) => {
    if (row.requests === null) return current
    if (!current || (current.requests ?? 0) < row.requests) return row
    return current
  }, null)
  return busiest && (busiest.requests ?? 0) > 0 ? busiest : null
}

export function aggregateUsage(report: MyUsageReport, filters: UsageFilters): AggregatedUsage {
  const filteredRows = report.byResource.filter((row) => rowMatches(row, filters))
  const entitlementIds = new Set(filteredRows.map((row) => row.entitlementId))
  const filteredTimeline = report.timeline.filter((point) => pointMatches(point, entitlementIds, filters))
  const hasFilters = filters.environment !== 'all' || filters.resource !== 'all'
  return {
    totals: hasFilters ? aggregateTotals(filteredTimeline, filteredRows) : report.totals,
    byEnvironment: hasFilters ? aggregateByEnvironment(filteredTimeline, filteredRows) : report.byEnvironment,
    byResource: filteredRows,
    timeline: filteredTimeline,
    busiestResource: busiestResource(filteredRows),
  }
}

export function formatCurrency(value: number | null, currency: string) {
  if (value === null) return 'Unknown'
  return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(value)
}

export function formatMetric(value: number | null, unavailableLabel = '—') {
  return value === null ? unavailableLabel : formatNumber(value)
}

export function formatCount(count: number, noun: string) {
  return `${formatNumber(count)} ${count === 1 ? noun : `${noun}s`}`
}

export function usageTrackingLabel(linkedBy: UsageResourceRow['linkedBy']) {
  if (linkedBy === 'gatewayLog') return 'At the gateway'
  if (linkedBy === 'subscription') return 'By APIM subscription'
  return 'Not linked yet'
}

export function formatUtcDate(date: string) {
  const [year, month, day] = date.split('-').map(Number)
  if (!year || !month || !day) return date
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    timeZone: 'UTC',
  }).format(new Date(Date.UTC(year, month - 1, day)))
}

export function quotaWindowLabel(period: string) {
  switch (period) {
    case 'Hourly':
      return 'this hour'
    case 'Daily':
      return 'today'
    case 'Weekly':
      return 'this week'
    case 'Monthly':
      return 'this month'
    case 'Yearly':
      return 'this year'
    default:
      return 'this window'
  }
}

export function metricLabel(metric: 'tokens' | 'requests') {
  return metric === 'tokens' ? 'Tokens' : 'Requests'
}

export function rateLimitLabel(metric: 'tokens' | 'requests', limit: number, windowSeconds: number) {
  const noun = metric === 'tokens' ? 'tokens' : 'requests'
  const window = windowSeconds === 60 && metric === 'tokens' ? 'minute' : `${formatNumber(windowSeconds)} seconds`
  return `${formatNumber(limit)} ${noun} per ${window}`
}

export function environmentSort(
  environments: PortalEnvironment[] | undefined,
  a: UsageEnvironmentBreakdown,
  b: UsageEnvironmentBreakdown,
) {
  const known = new Map((environments ?? []).map((environment, index) => [environment.key, index]))
  const aKnown = a.environment === null ? Number.MAX_SAFE_INTEGER - 1 : known.get(a.environment)
  const bKnown = b.environment === null ? Number.MAX_SAFE_INTEGER - 1 : known.get(b.environment)
  if (aKnown !== undefined || bKnown !== undefined) {
    return (aKnown ?? Number.MAX_SAFE_INTEGER) - (bKnown ?? Number.MAX_SAFE_INTEGER)
  }
  return environmentLabel(environments, a.environment).localeCompare(environmentLabel(environments, b.environment))
}

export function rowLabel(row: UsageResourceRow) {
  return resourceLabel(row.resource, row.resourceSummary)
}
