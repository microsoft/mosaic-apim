import {
  Badge,
  Button,
  Card,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  ProgressBar,
  Text,
} from '@fluentui/react-components'
import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { usePortalApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { PageHeader } from '../components/PageHeader'
import { RecentHoursChart, UsageTrendChart } from '../components/UsageTrendChart'
import { environmentLabel } from '../environments'
import { formatNumber, gatewayLabel, resourceLabel } from '../entitlement-format'
import { usePortalEnvironments } from '../environments'
import type {
  MyUsageReport,
  PortalEnvironment,
  UsageEnvironmentBreakdown,
  UsageFreshness,
  UsageHourPoint,
  UsagePeriod,
  UsageQuota,
  UsageRateLimit,
  UsageResourceRow,
} from '../types'
import {
  accessLabel,
  aggregateUsage,
  attributionExplanation,
  environmentSort,
  formatCount,
  formatCurrency,
  formatGeneratedAt,
  formatMetric,
  formatUtcDate,
  quotaDetail,
  quotaLabel,
  quotaStatus,
  rateLimitLabel,
  rateLimitPeakLabel,
  relativeTime,
  rowLabel,
  usageTrackingLabel,
} from '../usage-format'

function environmentOptions(
  report: MyUsageReport | undefined,
  environments: PortalEnvironment[] | undefined,
) {
  const present = new Set((report?.byEnvironment ?? []).map((row) => row.environment))
  const known = (environments ?? []).filter((environment) => present.has(environment.key))
  const knownKeys = new Set(known.map((environment) => environment.key))
  const unknown = Array.from(present)
    .filter((key): key is string => key !== null && !knownKeys.has(key))
    .sort((a, b) => a.localeCompare(b))
  return { known, unknown, hasUnclassified: present.has(null) }
}

function inaccessible(value: string) {
  return (
    <span className="not-metered" aria-label={value}>
      —
    </span>
  )
}

function hasCost(report: MyUsageReport) {
  return report.totals.estimatedCost !== null || report.byResource.some((row) => row.estimatedCost !== null)
}

function combineRecentHours(rows: UsageResourceRow[], fallback: UsageHourPoint[] = []) {
  if (rows.every((row) => !row.recentHours || row.recentHours.length === 0)) return fallback
  const hours = new Map<string, UsageHourPoint>()
  for (const row of rows) {
    for (const point of row.recentHours ?? []) {
      const current =
        hours.get(point.hour) ??
        ({
          hour: point.hour,
          requests: 0,
          totalTokens: 0,
          throttled: 0,
          quotaRefused: 0,
          errors: 0,
          peakMinuteTokens: null,
          peakMinuteRequests: null,
        } satisfies UsageHourPoint)
      current.requests = (current.requests ?? 0) + (point.requests ?? 0)
      current.totalTokens = (current.totalTokens ?? 0) + (point.totalTokens ?? 0)
      current.throttled = (current.throttled ?? 0) + (point.throttled ?? 0)
      current.quotaRefused = (current.quotaRefused ?? 0) + (point.quotaRefused ?? 0)
      current.errors = (current.errors ?? 0) + (point.errors ?? 0)
      current.peakMinuteTokens = Math.max(current.peakMinuteTokens ?? 0, point.peakMinuteTokens ?? 0)
      current.peakMinuteRequests = Math.max(
        current.peakMinuteRequests ?? 0,
        point.peakMinuteRequests ?? 0,
      )
      hours.set(point.hour, current)
    }
  }
  return Array.from(hours.values()).sort((a, b) => a.hour.localeCompare(b.hour))
}

function KpiCard({ title, value, detail }: { title: string; value: string; detail?: string }) {
  return (
    <Card className="usage-kpi-card">
      <Text size={200} weight="semibold">
        {title}
      </Text>
      <strong>{value}</strong>
      {detail && <Text size={200}>{detail}</Text>}
    </Card>
  )
}

function UsageNotice({ report }: { report: MyUsageReport }) {
  if (report.dataSource !== 'simulated') return null
  const fallback =
    'These sample figures are generated from your real grants and limits for local or test runs only.'
  return (
    <MessageBar intent="info" className="usage-notice">
      <MessageBarBody>
        <MessageBarTitle>Sample figures</MessageBarTitle>
        {report.notes.length > 0 ? (
          <ul>
            {report.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        ) : (
          fallback
        )}
      </MessageBarBody>
    </MessageBar>
  )
}

function freshnessText(freshness: UsageFreshness, report: MyUsageReport) {
  const refresh = `refreshed every ${freshness.intervalMinutes} min`
  const late = "the gateway's logs arrive a few minutes late"
  if (freshness.status === 'current' && freshness.updatedAt) {
    return `Updated ${relativeTime(freshness.updatedAt)} · ${refresh} · ${late}`
  }
  if (freshness.status === 'delayed') {
    const updated = freshness.updatedAt ? `last updated ${relativeTime(freshness.updatedAt)}` : 'not updated yet'
    return `Delayed: ${updated} · figures may be out of date · ${refresh}`
  }
  if (freshness.status === 'failing') {
    const updated = freshness.updatedAt ? `last successful update ${relativeTime(freshness.updatedAt)}` : 'no successful update yet'
    return `MOSAIC is having trouble reading gateway logs; figures may be out of date · ${updated}`
  }
  if (freshness.status === 'pending') {
    return "MOSAIC hasn't read the gateway's logs yet — check back soon."
  }
  if (freshness.status === 'notLinked') {
    return "None of your grants are on a gateway MOSAIC reads."
  }
  return `Usage generated ${formatGeneratedAt(report.generatedAt)}`
}

function FreshnessNotice({ report }: { report: MyUsageReport }) {
  if (report.dataSource !== 'logAnalytics') return null
  const freshness = report.freshness
  const body = freshness
    ? freshnessText(freshness, report)
    : "Measured from gateway logs. Recent calls can take a few minutes to appear."
  const showDataFrom =
    freshness?.dataFrom && freshness.dataFrom > report.start
      ? `MOSAIC has complete data from ${formatUtcDate(freshness.dataFrom)}, later than the selected period.`
      : null
  const intent =
    freshness?.status === 'failing'
      ? 'error'
      : freshness?.status === 'delayed' || freshness?.status === 'pending' || freshness?.status === 'notLinked'
        ? 'warning'
        : 'success'
  return (
    <MessageBar intent={intent} className="usage-notice">
      <MessageBarBody>
        <MessageBarTitle>Measured figures</MessageBarTitle>
        {body}
        {showDataFrom && <p>{showDataFrom}</p>}
      </MessageBarBody>
    </MessageBar>
  )
}

function LiveQuotaNote() {
  return (
    <MessageBar intent="info" className="usage-notice">
      <MessageBarBody>
        <MessageBarTitle>Live remaining quota</MessageBarTitle>
        While you call the gateway, read the gateway response headers for the live limit result:
        token and request limits return <code>Retry-After</code> when a call is throttled, and
        APIM's limit policies also expose remaining quota headers such as{' '}
        <code>x-ratelimit-remaining-tokens</code> where configured. This page is a rollup, not a
        real-time counter.
      </MessageBarBody>
    </MessageBar>
  )
}

function EnvironmentBreakdown({
  rows,
  currency,
  environments,
  showCost,
}: {
  rows: UsageEnvironmentBreakdown[]
  currency: string
  environments?: PortalEnvironment[]
  showCost: boolean
}) {
  const maxRequests = Math.max(0, ...rows.map((row) => row.requests))
  const ordered = [...rows].sort((a, b) => environmentSort(environments, a, b))
  return (
    <section className="usage-section">
      <div className="section-header">
        <div>
          <h2>By environment</h2>
          <p>Request and token volume by environment.</p>
        </div>
      </div>
      <div className="environment-bars">
        {ordered.map((row) => {
          const percent = maxRequests === 0 ? 0 : (row.requests / maxRequests) * 100
          const measured = row.resources - row.unmeasuredResources
          return (
            <div key={row.environment ?? 'unclassified'} className="environment-bar-row">
              <EnvironmentBadge environment={row.environment} environments={environments} />
              <div className="bar-cell">
                <div className="bar-track" aria-hidden="true">
                  <span style={{ width: `${percent}%` }} />
                </div>
                <Text size={200}>
                  {measured === 0
                    ? 'Usage unavailable'
                    : `${formatCount(row.requests, 'request')} · ${formatCount(row.totalTokens, 'token')}`}
                  {showCost &&
                    measured > 0 &&
                    ` · ${
                      row.estimatedCost === null
                        ? 'cost unknown'
                        : formatCurrency(row.estimatedCost, currency)
                    }`}{' '}
                  · {formatCount(row.resources, 'resource')}
                  {measured > 0 && row.unmeasuredResources > 0 && `, ${row.unmeasuredResources} not measured`}
                </Text>
              </div>
            </div>
          )
        })}
      </div>
    </section>
  )
}

function QuotaView({ quota }: { quota: UsageQuota }) {
  const label = quotaLabel(quota)
  const status = quotaStatus(quota)
  const className =
    status === 'reached'
      ? 'quota-progress quota-error'
      : status === 'near'
        ? 'quota-progress quota-warning'
        : 'quota-progress'
  return (
    <div className={className}>
      <span>{label}</span>
      {quota.utilization !== null && (
        <ProgressBar
          value={Math.min(quota.utilization, 1)}
          aria-label={label}
          aria-valuenow={quota.used ?? undefined}
          aria-valuemax={quota.limit}
        />
      )}
      <small>{quotaDetail(quota)}</small>
    </div>
  )
}

function ResourceUsageValue({
  value,
  unavailable,
}: {
  value: number | null
  unavailable?: string
}) {
  if (value === null) return inaccessible(unavailable ?? 'Not metered')
  return <>{formatNumber(value)}</>
}

function CostCell({ row, currency }: { row: UsageResourceRow; currency: string }) {
  if (row.estimatedCost !== null) return <>{formatCurrency(row.estimatedCost, currency)}</>
  return (
    <>
      Unknown
      {row.costNote && <small>{row.costNote}</small>}
    </>
  )
}

function ErrorDetails({ row }: { row: UsageResourceRow }) {
  const parts = [
    row.throttled ? `${formatCount(row.throttled, 'throttled call')}` : null,
    row.quotaRefused ? `${formatCount(row.quotaRefused, 'quota refusal')}` : null,
    row.errors ? `${formatCount(row.errors, 'error')}` : null,
  ].filter(Boolean)
  return parts.length > 0 ? <small>{parts.join(' · ')}</small> : null
}

function RateLimitView({ limit }: { limit: UsageRateLimit }) {
  const peak = rateLimitPeakLabel(limit)
  return (
    <li>
      {rateLimitLabel(limit.metric, limit.limit, limit.windowSeconds)}
      {peak && <small>{peak}</small>}
    </li>
  )
}

function ResourceTable({
  rows,
  currency,
  environments,
  showCost,
}: {
  rows: UsageResourceRow[]
  currency: string
  environments?: PortalEnvironment[]
  showCost: boolean
}) {
  return (
    <section className="usage-section">
      <div className="section-header">
        <div>
          <h2>By resource</h2>
          <p>Usage, limits, and attribution for each grant.</p>
        </div>
      </div>
      <div className="table-scroll">
        <table className="usage-table" aria-label="Usage by resource">
          <thead>
            <tr>
              <th scope="col">Resource</th>
              <th scope="col">Environment</th>
              <th scope="col">How granted</th>
              <th scope="col">Requests</th>
              <th scope="col">Tokens</th>
              {showCost && <th scope="col">Estimated cost</th>}
              <th scope="col">Quotas</th>
              <th scope="col">Rate limits</th>
              <th scope="col">Usage tracking</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.entitlementId}>
                <th scope="row">
                  <span className="resource-name">
                    {resourceLabel(row.resource, row.resourceSummary)}
                    {row.resourceSummary.available === false && (
                      <Badge appearance="outline" color="warning">
                        No longer available
                      </Badge>
                    )}
                    {!row.enabled && <Badge color="danger">Disabled</Badge>}
                  </span>
                  <small>{gatewayLabel(row.resourceSummary)}</small>
                </th>
                <td>
                  <EnvironmentBadge environment={row.environment} environments={environments} />
                </td>
                <td>
                  {accessLabel(row)}
                  {row.via === 'securityGroup' && <small>Your calls only.</small>}
                </td>
                <td>
                  <ResourceUsageValue
                    value={row.attribution === 'unattributed' ? null : row.requests}
                    unavailable="Usage unavailable"
                  />
                  <ErrorDetails row={row} />
                </td>
                <td>
                  {row.attribution === 'unattributed' ? (
                    inaccessible('Usage unavailable')
                  ) : row.totalTokens === null ? (
                    <span className="not-metered">Not metered</span>
                  ) : (
                    <>
                      {formatNumber(row.totalTokens)}
                      <small>
                        Prompt {formatMetric(row.promptTokens)} · Completion{' '}
                        {formatMetric(row.completionTokens)}
                      </small>
                    </>
                  )}
                </td>
                {showCost && (
                  <td>
                    <CostCell row={row} currency={currency} />
                  </td>
                )}
                <td>
                  {row.quotas.length > 0 ? (
                    <div className="quota-stack">
                      {row.quotas.map((quota) => (
                        <QuotaView
                          key={`${row.entitlementId}:${quota.metric}:${quota.period}:${quota.windowStart}`}
                          quota={quota}
                        />
                      ))}
                    </div>
                  ) : (
                    'No quotas'
                  )}
                </td>
                <td>
                  {row.rateLimits.length > 0 ? (
                    <ul className="plain-list rate-limit-list">
                      {row.rateLimits.map((limit) => (
                        <RateLimitView
                          key={`${limit.metric}:${limit.limit}:${limit.windowSeconds}`}
                          limit={limit}
                        />
                      ))}
                    </ul>
                  ) : (
                    'No rate limits'
                  )}
                </td>
                <td>
                  {row.linkedBy ? (
                    usageTrackingLabel(row.linkedBy)
                  ) : (
                    <span className="binding-hint">{usageTrackingLabel(row.linkedBy)}</span>
                  )}
                  {attributionExplanation(row) && <small>{attributionExplanation(row)}</small>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

export function UsagePage() {
  const api = usePortalApi()
  const [period, setPeriod] = useState<UsagePeriod>('30d')
  const [environmentFilter, setEnvironmentFilter] = useState('all')
  const [resourceFilter, setResourceFilter] = useState('all')
  const usage = useQuery({ queryKey: ['my-usage', period], queryFn: () => api.getMyUsage(period) })
  const environments = usePortalEnvironments()
  const filtered = useMemo(() => {
    if (!usage.data) return null
    return aggregateUsage(usage.data, { environment: environmentFilter, resource: resourceFilter })
  }, [usage.data, environmentFilter, resourceFilter])
  const showCost = usage.data ? hasCost(usage.data) : false
  const recentHours = useMemo(() => {
    if (!usage.data || !filtered) return []
    // The report's own hours cover every grant, so they can stand in only when nothing is filtered out.
    const unfiltered = environmentFilter === 'all' && resourceFilter === 'all'
    return combineRecentHours(filtered.byResource, unfiltered ? (usage.data.recentHours ?? []) : [])
  }, [filtered, usage.data, environmentFilter, resourceFilter])
  const environmentFilterOptions = useMemo(
    () => environmentOptions(usage.data, environments.data),
    [usage.data, environments.data],
  )
  const clearFilters = () => {
    setEnvironmentFilter('all')
    setResourceFilter('all')
  }

  return (
    <>
      <PageHeader
        title="Usage & cost"
        description="Review your calls, tokens, and limits across granted resources, and their cost where MOSAIC has prices."
        actions={
          usage.data?.dataSource === 'simulated' ? (
            <Badge appearance="filled">Sample figures</Badge>
          ) : usage.data?.dataSource === 'logAnalytics' ? (
            <Badge appearance="filled" color="success">
              Measured
            </Badge>
          ) : undefined
        }
      />
      {usage.isLoading && <Loading label="Loading usage" />}
      {usage.isError && (
        <>
          <ErrorState error={usage.error} />
          <Button className="retry-button" onClick={() => void usage.refetch()}>
            Retry
          </Button>
        </>
      )}
      {usage.isSuccess && usage.data.byResource.length === 0 && (
        <EmptyState title="No grants yet">
          Visit the <Link to="/catalog">catalog</Link> to request access before usage can appear
          here.
        </EmptyState>
      )}
      {usage.isSuccess && usage.data.byResource.length > 0 && filtered && (
        <>
          <UsageNotice report={usage.data} />
          <FreshnessNotice report={usage.data} />
          <div className="filter-row" role="group" aria-label="Usage filters">
            <label>
              <span>Period</span>
              <select
                value={period}
                onChange={(event) => {
                  setPeriod(event.currentTarget.value as UsagePeriod)
                  clearFilters()
                }}
              >
                <option value="7d">Last 7 days</option>
                <option value="30d">Last 30 days</option>
                <option value="90d">Last 90 days</option>
              </select>
            </label>
            <label>
              <span>Environment</span>
              <select
                value={environmentFilter}
                onChange={(event) => setEnvironmentFilter(event.currentTarget.value)}
              >
                <option value="all">All environments</option>
                {environmentFilterOptions.known.map((environment) => (
                  <option key={environment.key} value={environment.key}>
                    {environment.displayName}
                  </option>
                ))}
                {environmentFilterOptions.unknown.map((environment) => (
                  <option key={environment} value={environment}>
                    {environment}
                  </option>
                ))}
                {environmentFilterOptions.hasUnclassified && (
                  <option value="unclassified">Unclassified</option>
                )}
              </select>
            </label>
            <label>
              <span>Resource</span>
              <select
                value={resourceFilter}
                onChange={(event) => setResourceFilter(event.currentTarget.value)}
              >
                <option value="all">All resources</option>
                {usage.data.byResource.map((row) => (
                  <option key={row.entitlementId} value={row.entitlementId}>
                    {rowLabel(row)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          {filtered.byResource.length === 0 ? (
            <EmptyState title="No usage matches these filters">
              <Button onClick={clearFilters}>Clear filters</Button>
            </EmptyState>
          ) : (
            <>
              <div className="usage-kpi-grid">
                <KpiCard title="Requests" value={formatNumber(filtered.totals.requests)} />
                <KpiCard
                  title="Tokens"
                  value={formatNumber(filtered.totals.totalTokens)}
                  detail={`Prompt ${formatNumber(filtered.totals.promptTokens)} · Completion ${formatNumber(
                    filtered.totals.completionTokens,
                  )}`}
                />
                {filtered.totals.errors !== null && filtered.totals.errors !== undefined && (
                  <KpiCard
                    title="Errors and throttling"
                    value={formatNumber(filtered.totals.errors)}
                    detail={`${formatNumber(filtered.totals.throttled ?? 0)} throttled · ${formatNumber(
                      filtered.totals.quotaRefused ?? 0,
                    )} quota refused`}
                  />
                )}
                {showCost && (
                  <KpiCard
                    title="Estimated cost"
                    value={formatCurrency(filtered.totals.estimatedCost, usage.data.currency)}
                    detail={
                      filtered.totals.costExcludedResources > 0
                        ? `Excludes ${formatCount(filtered.totals.costExcludedResources, 'resource')} with unknown cost`
                        : undefined
                    }
                  />
                )}
                <KpiCard
                  title="Busiest resource"
                  value={filtered.busiestResource ? rowLabel(filtered.busiestResource) : 'None'}
                />
              </div>
              {filtered.totals.requests === 0 ? (
                <EmptyState title="No calls in this period">
                  Your grants are ready, but MOSAIC has no calls to show for this period.
                </EmptyState>
              ) : null}
              <UsageTrendChart
                points={filtered.timeline}
                resources={filtered.byResource}
                environments={environments.data}
              />
              {recentHours.length > 0 && <RecentHoursChart points={recentHours} />}
              <LiveQuotaNote />
              <EnvironmentBreakdown
                rows={filtered.byEnvironment}
                currency={usage.data.currency}
                environments={environments.data}
                showCost={showCost}
              />
              <ResourceTable
                rows={filtered.byResource}
                currency={usage.data.currency}
                environments={environments.data}
                showCost={showCost}
              />
              <Text size={200} className="usage-generated">
                Generated {formatGeneratedAt(usage.data.generatedAt)} for{' '}
                {environmentFilter === 'all'
                  ? 'all environments'
                  : environmentLabel(
                      environments.data,
                      environmentFilter === 'unclassified' ? null : environmentFilter,
                    )}
                .
              </Text>
            </>
          )}
        </>
      )}
    </>
  )
}
