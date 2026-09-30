import { environmentLabel } from './environments'
import { formatNumber, resourceLabel } from './entitlement-format'
import type {
  MyUsageReport,
  PortalEnvironment,
  UsageEnvironmentBreakdown,
  UsagePeriod,
  UsageQuota,
  UsageRateLimit,
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

function sumOptionalNumbers(values: Array<number | null | undefined>) {
  const present = values.filter((value): value is number => typeof value === 'number')
  return present.length > 0 ? present.reduce((total, value) => total + value, 0) : null
}

export function aggregateTotals(points: UsageTimelinePoint[], rows: UsageResourceRow[]): UsageTotals {
  const estimatedCost = sumNullable(points.map((point) => point.estimatedCost))
  const lastUsed = rows.map((row) => row.lastUsedAt).filter((value): value is string => Boolean(value))
  return {
    requests: points.reduce((total, point) => total + (point.requests ?? 0), 0),
    promptTokens: points.reduce((total, point) => total + (point.promptTokens ?? 0), 0),
    completionTokens: points.reduce((total, point) => total + (point.completionTokens ?? 0), 0),
    totalTokens: points.reduce((total, point) => total + (point.totalTokens ?? 0), 0),
    estimatedCost,
    costExcludedResources: rows.filter((row) => row.estimatedCost === null).length,
    throttled: sumOptionalNumbers(points.map((point) => point.throttled)),
    quotaRefused: sumOptionalNumbers(points.map((point) => point.quotaRefused)),
    errors: sumOptionalNumbers(points.map((point) => point.errors)),
    lastUsedAt: lastUsed.length > 0 ? lastUsed.sort().at(-1) : null,
  }
}

function aggregateByEnvironment(points: UsageTimelinePoint[], rows: UsageResourceRow[]) {
  const environments = new Map<string, UsageEnvironmentBreakdown>()
  const resourcesByEnvironment = new Map<string, Set<string>>()
  const costExcludedByEnvironment = new Map<string, Set<string>>()
  const unmeasuredByEnvironment = new Map<string, Set<string>>()
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
    if (row.attribution === 'unattributed') {
      if (!unmeasuredByEnvironment.has(key)) unmeasuredByEnvironment.set(key, new Set())
      unmeasuredByEnvironment.get(key)?.add(row.entitlementId)
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
        unmeasuredResources: 0,
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
        unmeasuredResources: 0,
      }
    current.resources = resources.size
    current.costExcludedResources = costExcludedByEnvironment.get(key)?.size ?? 0
    current.unmeasuredResources = unmeasuredByEnvironment.get(key)?.size ?? 0
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
  if (linkedBy === 'gatewayLog') return 'Linked from gateway log traces'
  if (linkedBy === 'subscription') return 'Linked from the APIM subscription'
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

export function formatUtcHour(hour: string) {
  const date = new Date(hour)
  if (Number.isNaN(date.getTime())) return hour
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    hour12: false,
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(date)
}

export function formatGeneratedAt(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(date)
}

export function relativeTime(value: string, now = new Date()) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const minutes = Math.max(0, Math.round((now.getTime() - date.getTime()) / 60_000))
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 48) return `${hours} hr ago`
  const days = Math.round(hours / 24)
  return `${days} days ago`
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

export function quotaStatus(quota: UsageQuota) {
  if (quota.utilization === null) return 'unknown'
  if (quota.utilization >= 1) return 'reached'
  if (quota.utilization >= 0.8) return 'near'
  return 'ok'
}

export function quotaPercentLabel(utilization: number | null) {
  if (utilization === null) return 'Usage unknown'
  return `${Math.round(utilization * 100)}% used`
}

export function quotaLabel(quota: UsageQuota) {
  const metric = metricLabel(quota.metric)
  const window = quotaWindowLabel(quota.period)
  if (quota.used === null) return `${metric}: up to ${formatNumber(quota.limit)} ${window}`
  return `${metric}: ${formatNumber(quota.used)} of ${formatNumber(quota.limit)} ${window}`
}

export function quotaDetail(quota: UsageQuota) {
  const status = quotaStatus(quota)
  return [
    quotaPercentLabel(quota.utilization),
    status === 'reached' ? 'Limit reached' : status === 'near' ? 'Near limit' : null,
    quota.partial ? 'partial window' : null,
  ]
    .filter(Boolean)
    .join(' · ')
}

export function rateLimitLabel(metric: 'tokens' | 'requests', limit: number, windowSeconds: number) {
  const noun = metric === 'tokens' ? 'tokens' : 'requests'
  const window = windowSeconds === 60 && metric === 'tokens' ? 'minute' : `${formatNumber(windowSeconds)} seconds`
  return `${formatNumber(limit)} ${noun} per ${window}`
}

// Peaks are measured per minute, so the API only sends one for a per-minute limit.
export function rateLimitPeakLabel(limit: UsageRateLimit) {
  if (limit.peak === null || limit.peak === undefined) return null
  const utilization = limit.utilization ?? limit.peak / limit.limit
  return `Busiest minute: ${formatNumber(limit.peak)} ${limit.metric} (${Math.round(utilization * 100)}%)`
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

export function accessLabel(row: UsageResourceRow) {
  if (row.via === 'direct') return 'Direct grant'
  const group = row.viaGroupName ?? 'an assigned group'
  if (row.via === 'securityGroup') return `Entra security group: ${group}`
  return `MOSAIC group: ${group}`
}

export function attributionExplanation(row: UsageResourceRow) {
  if (row.attribution === 'simulated') {
    return 'Sample usage generated from this real grant and its limits.'
  }
  if (row.attribution === 'unattributed') {
    return "Usage can't be measured for this grant yet."
  }
  if (row.via === 'securityGroup') {
    return 'Only your own calls through this security-group grant are counted here.'
  }
  return null
}
