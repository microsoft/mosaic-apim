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
import { UsageTrendChart } from '../components/UsageTrendChart'
import { environmentLabel } from '../environments'
import { formatNumber, gatewayLabel, resourceLabel } from '../entitlement-format'
import { usePortalEnvironments } from '../environments'
import type {
  MyUsageReport,
  PortalEnvironment,
  UsageEnvironmentBreakdown,
  UsagePeriod,
  UsageQuota,
  UsageResourceRow,
} from '../types'
import {
  aggregateUsage,
  environmentSort,
  formatCount,
  formatCurrency,
  formatMetric,
  metricLabel,
  quotaWindowLabel,
  rateLimitLabel,
  rowLabel,
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
    "Figures are simulated from your real grants and limits. Costs are estimates at illustrative rates, and this isn't a bill."
  return (
    <MessageBar intent="info" className="usage-notice">
      <MessageBarBody>
        <MessageBarTitle>Sample data</MessageBarTitle>
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

function EnvironmentBreakdown({
  rows,
  currency,
  environments,
}: {
  rows: UsageEnvironmentBreakdown[]
  currency: string
  environments?: PortalEnvironment[]
}) {
  const maxRequests = Math.max(0, ...rows.map((row) => row.requests))
  const ordered = [...rows].sort((a, b) => environmentSort(environments, a, b))
  return (
    <section className="usage-section">
      <div className="section-header">
        <div>
          <h2>By environment</h2>
          <p>Request volume and estimated cost by environment.</p>
        </div>
      </div>
      <div className="environment-bars">
        {ordered.map((row) => {
          const percent = maxRequests === 0 ? 0 : (row.requests / maxRequests) * 100
          return (
            <div key={row.environment ?? 'unclassified'} className="environment-bar-row">
              <EnvironmentBadge environment={row.environment} environments={environments} />
              <div className="bar-cell">
                <div className="bar-track" aria-hidden="true">
                  <span style={{ width: `${percent}%` }} />
                </div>
                <Text size={200}>
                  {formatCount(row.requests, 'request')} · {formatCount(row.totalTokens, 'token')} ·{' '}
                  {row.estimatedCost === null
                    ? 'cost unknown'
                    : formatCurrency(row.estimatedCost, currency)}{' '}
                  · {formatCount(row.resources, 'resource')}
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
  const utilization = quota.utilization
  const label = `${metricLabel(quota.metric)}: ${
    quota.used === null ? 'unknown' : formatNumber(quota.used)
  } of ${formatNumber(quota.limit)} ${quotaWindowLabel(quota.period)}`
  const value = utilization === null ? 0 : Math.min(utilization, 1)
  const className =
    utilization === null
      ? 'quota-progress'
      : utilization >= 1
        ? 'quota-progress quota-error'
        : utilization >= 0.8
          ? 'quota-progress quota-warning'
          : 'quota-progress'
  return (
    <div className={className}>
      <span>{label}</span>
      <ProgressBar value={value} aria-label={label} />
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

function ResourceTable({
  rows,
  currency,
  environments,
}: {
  rows: UsageResourceRow[]
  currency: string
  environments?: PortalEnvironment[]
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
              <th scope="col">Estimated cost</th>
              <th scope="col">Quotas</th>
              <th scope="col">Rate limits</th>
              <th scope="col">APIM subscription</th>
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
                  {row.via === 'direct'
                    ? 'Direct'
                    : `Through ${row.viaGroupName ?? 'an assigned group'}`}
                </td>
                <td>
                  <ResourceUsageValue
                    value={row.attribution === 'unattributed' ? null : row.requests}
                    unavailable="Usage unavailable"
                  />
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
                <td>
                  <CostCell row={row} currency={currency} />
                </td>
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
                    <ul className="plain-list">
                      {row.rateLimits.map((limit) => (
                        <li key={`${limit.metric}:${limit.limit}:${limit.windowSeconds}`}>
                          {rateLimitLabel(limit.metric, limit.limit, limit.windowSeconds)}
                        </li>
                      ))}
                    </ul>
                  ) : (
                    'No rate limits'
                  )}
                </td>
                <td>
                  {row.bound ? 'Linked' : <span className="binding-hint">Not linked yet</span>}
                  {row.attribution === 'unattributed' && (
                    <small>
                      Usage can't be attributed until this grant is bound to an API Management
                      subscription.
                    </small>
                  )}
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
        description="Review your request volume, token usage, and estimated costs across granted resources."
        actions={
          usage.data?.dataSource === 'simulated' ? (
            <Badge appearance="filled">Sample data</Badge>
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
                <KpiCard
                  title="Estimated cost"
                  value={formatCurrency(filtered.totals.estimatedCost, usage.data.currency)}
                  detail={
                    filtered.totals.costExcludedResources > 0
                      ? `Excludes ${formatCount(filtered.totals.costExcludedResources, 'resource')} with unknown cost`
                      : undefined
                  }
                />
                <KpiCard
                  title="Busiest resource"
                  value={filtered.busiestResource ? rowLabel(filtered.busiestResource) : 'None'}
                />
              </div>
              <UsageTrendChart points={filtered.timeline} environments={environments.data} />
              <EnvironmentBreakdown
                rows={filtered.byEnvironment}
                currency={usage.data.currency}
                environments={environments.data}
              />
              <ResourceTable
                rows={filtered.byResource}
                currency={usage.data.currency}
                environments={environments.data}
              />
              <Text size={200} className="usage-generated">
                Generated {new Date(usage.data.generatedAt).toLocaleString()} for{' '}
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
