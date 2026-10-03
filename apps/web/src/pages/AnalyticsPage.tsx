import {
  Badge,
  Button,
  Card,
  Input,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Select,
  Spinner,
  Tab,
  TabList,
  Text,
  Title3,
} from '@fluentui/react-components'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useMemo, useState } from 'react'
import { Link as RouterLink, useSearchParams } from 'react-router-dom'
import { ApiError, useMosaicApi } from '../api'
import { EmptyState, ErrorState, Loading } from '../components/AsyncState'
import { BarList, type BarListItem } from '../components/charts/BarList'
import { Histogram } from '../components/charts/Histogram'
import { TrendChart } from '../components/charts/TrendChart'
import { DataSourceBadge, PageHeader } from '../components/PageHeader'
import { formatCost, formatCostCompact, formatRate, formatShare } from '../cost-format'
import { environmentLabel, useEnvironmentCatalog } from '../environments'
import { BACKFILL_STATUS_LABELS, ENTITLEMENT_SUBJECT_KIND_LABELS, FRESHNESS_STATUS_LABELS, PRINCIPAL_KIND_LABELS, plural } from '../labels'
import { holdsApi } from '../publication-state'
import type {
  AnalyticsApiRow,
  AnalyticsConsumerRow,
  AnalyticsConsumers,
  AnalyticsCost,
  AnalyticsCostDeploymentRow,
  AnalyticsCostSummary,
  AnalyticsDataSource,
  AnalyticsDeploymentRow,
  AnalyticsFilters,
  AnalyticsGatewayHealth,
  AnalyticsGranularity,
  AnalyticsGrantRow,
  AnalyticsHygiene,
  AnalyticsKpis,
  AnalyticsLimitRow,
  AnalyticsLimits,
  AnalyticsLimitUse,
  AnalyticsModels,
  AnalyticsOnBehalfUnresolved,
  AnalyticsOverview,
  AnalyticsRange,
  AnalyticsRankRow,
  AnalyticsReliability,
  AnalyticsReport,
  AnalyticsUnattributed,
  EntitlementSubjectKind,
  ExportView,
  Gateway,
  McpServer,
  ModelApi,
  ModelPool,
  PrincipalKind,
  UsageFreshness,
} from '../types'
import styles from './AnalyticsPage.module.css'

type TabKey = 'overview' | 'cost' | 'consumers' | 'models' | 'reliability' | 'limits' | 'hygiene' | 'unattributed'

const tabs: Array<{ key: TabKey; label: string; exports: ExportView[] }> = [
  { key: 'overview', label: 'Overview', exports: ['trend'] },
  { key: 'cost', label: 'Cost', exports: ['chargeback', 'costDeployments', 'costCenters'] },
  { key: 'consumers', label: 'Consumers', exports: ['people', 'applications', 'groups', 'costCenters', 'grants', 'clientApps', 'onBehalf'] },
  { key: 'models', label: 'Models', exports: ['apis', 'models', 'deployments'] },
  { key: 'reliability', label: 'Reliability', exports: ['denials', 'apis'] },
  { key: 'limits', label: 'Limits', exports: ['limits'] },
  { key: 'hygiene', label: 'Access hygiene', exports: ['unusedGrants', 'unusedKeys', 'untrackedGrants'] },
  { key: 'unattributed', label: 'Unattributed', exports: ['unattributed'] },
]

const exportLabels: Record<ExportView, string> = {
  trend: 'Trend',
  people: 'People',
  applications: 'Applications',
  groups: 'Security groups',
  grants: 'Grants',
  clientApps: 'Client applications',
  apis: 'APIs and MCP servers',
  models: 'Models',
  deployments: 'Deployments',
  denials: 'Denials by reason',
  limits: 'Grant limits',
  unusedGrants: 'Unused grants',
  unusedKeys: 'Unused keys',
  untrackedGrants: 'Untracked grants',
  unattributed: 'Unattributed calls',
  onBehalf: 'Model use through MCP servers',
  chargeback: 'Chargeback by month',
  costDeployments: 'Cost by deployment',
  costCenters: 'Cost centers',
}

const apiKindLabels: Record<NonNullable<AnalyticsApiRow['kind']>, string> = {
  model: 'Model API',
  mcp: 'MCP server',
  pool: 'Model pool',
}

const subjectLabels: Record<EntitlementSubjectKind, string> = {
  user: 'People',
  application: 'Applications',
  securityGroup: 'Entra security groups',
  group: 'MOSAIC groups',
}

const consumerKindLabels: Record<AnalyticsConsumerRow['kind'], string> = {
  person: 'Person',
  application: 'Application',
  group: 'Security group',
}

const grantStateLabels: Record<AnalyticsGrantRow['state'], string> = {
  active: 'Active',
  disabled: 'Disabled',
  removed: 'Removed',
}

const limitStatusLabels: Record<AnalyticsLimitRow['status'], string> = {
  ok: 'OK',
  near: 'Near limit',
  reached: 'Reached',
  unknown: 'Unknown',
}

function consumerKindLabel(row: AnalyticsConsumerRow) {
  return row.principalKind ? PRINCIPAL_KIND_LABELS[row.principalKind] : consumerKindLabels[row.kind]
}

// A grant's subject kind can't tell a person from an agent user, or an app from a managed
// identity, so the directory's principal kind names it when MOSAIC knows it.
function grantSubjectKindLabel(row: { subjectKind: EntitlementSubjectKind | null, subjectPrincipalKind?: PrincipalKind | null }) {
  if (row.subjectPrincipalKind) return PRINCIPAL_KIND_LABELS[row.subjectPrincipalKind]
  return row.subjectKind ? ENTITLEMENT_SUBJECT_KIND_LABELS[row.subjectKind] : 'Unknown'
}

function limitLabel(limit: AnalyticsLimitUse) {
  const noun = limit.metric === 'tokens' ? 'Tokens' : 'Calls'
  if (limit.kind === 'quota') return `${limit.period ?? 'Period'} ${limit.metric === 'tokens' ? 'token' : 'call'} quota`
  if (limit.windowSeconds === 60) return `${noun} per minute`
  if (limit.windowSeconds === 3600) return `${noun} per hour`
  return limit.windowSeconds ? `${noun} per ${formatNumber(limit.windowSeconds)} s` : `${noun} rate limit`
}

function formatNumber(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US').format(value)
}

function formatCompact(value: number | null | undefined) {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 }).format(value)
}

function formatPercent(value: number | null | undefined) {
  if (value == null) return '—'
  return `${(value * 100).toFixed(1)}%`
}

function formatLatency(value: number | null | undefined) {
  if (value == null) return '—'
  return value >= 1000 ? `≈${(value / 1000).toFixed(1)} s` : `≈${Math.round(value)} ms`
}

function formatDateTime(value: string | null | undefined) {
  if (!value) return 'Never'
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'UTC',
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    timeZoneName: 'short',
  }).format(new Date(value))
}

function formatDay(value: string) {
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(new Date(`${value}T00:00:00Z`))
}

function trendLabel(point: { start: string }, granularity: AnalyticsGranularity) {
  const start = new Date(point.start)
  if (granularity === 'month') {
    return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', year: 'numeric' }).format(start)
  }
  return new Intl.DateTimeFormat('en-US', {
    timeZone: 'UTC',
    month: 'short',
    day: 'numeric',
    hour: granularity === 'hour' ? '2-digit' : undefined,
  }).format(start)
}

function filtersFromSearch(params: URLSearchParams): AnalyticsFilters {
  const range = (params.get('range') ?? '30d') as AnalyticsRange
  return {
    range,
    start: range === 'custom' ? params.get('start') ?? undefined : undefined,
    end: range === 'custom' ? params.get('end') ?? undefined : undefined,
    gatewayId: params.get('gatewayId') ?? undefined,
    environment: params.get('environment') ?? undefined,
    resourceId: params.get('resourceId') ?? undefined,
    subjectKind: (params.get('subjectKind') as EntitlementSubjectKind | null) ?? undefined,
    costCenterId: params.get('costCenterId') ?? undefined,
  }
}

function tabFromSearch(params: URLSearchParams): TabKey {
  const value = params.get('tab') as TabKey | null
  return value && tabs.some((tab) => tab.key === value) ? value : 'overview'
}

function sourceKind(source: AnalyticsDataSource) {
  return source === 'logAnalytics' ? 'live' : 'local'
}

function freshnessText(freshness: UsageFreshness) {
  if (freshness.status === 'notLinked') return 'No gateway is linked.'
  if (freshness.status === 'pending') return "MOSAIC hasn't read the gateways' telemetry yet."
  const updated = freshness.updatedAt ? `Updated ${formatDateTime(freshness.updatedAt)}` : 'Not updated yet'
  const cadence = freshness.intervalMinutes ? ` · every ${freshness.intervalMinutes} min` : ''
  const message = freshness.message ? ` · ${freshness.message}` : ''
  if (freshness.status === 'delayed') return `${updated}${cadence} · delayed${message}`
  if (freshness.status === 'failing') return `${updated}${cadence} · failing${message}`
  return `${updated}${cadence}`
}

function breakdownCaption(report: AnalyticsReport) {
  if (report.window.range !== '24h') return null
  return `Breakdowns and active caller/grant/API counts cover whole UTC days ${formatDay(report.window.breakdownStart)} through ${formatDay(report.window.breakdownEnd)}.`
}

function DataNotes({ report }: { report: AnalyticsReport }) {
  if (report.dataSource === 'notConfigured') {
    return (
      <MessageBar intent="warning">
        <MessageBarBody>
          <MessageBarTitle>Usage telemetry is not configured</MessageBarTitle>
          Local and test runs do not read gateway telemetry. In Azure, MOSAIC reads gateways' Log Analytics data; MOSAIC_USAGE_SOURCE controls this.
          {report.notes.length > 0 && <ul>{report.notes.map((note) => <li key={note}>{note}</li>)}</ul>}
        </MessageBarBody>
      </MessageBar>
    )
  }
  return (
    <div className={styles.notes}>
      <DataSourceBadge kind="live" />
      <Text>{freshnessText(report.freshness)}</Text>
      {breakdownCaption(report) && <Text>{breakdownCaption(report)}</Text>}
      {report.notes.map((note) => <Text key={note}>{note}</Text>)}
    </div>
  )
}

function KpiCard({ label, value, detail, badge }: { label: string; value: string; detail?: string; badge?: string }) {
  return (
    <Card className={styles.kpiCard}>
      <Text className={styles.kpiLabel}>{label}</Text>
      <div className={styles.kpiValue}>
        {value}
        {badge && <Badge appearance="tint" color="informative" className={styles.kpiBadge}>{badge}</Badge>}
      </div>
      {detail && <Text size={200} className={styles.kpiDetail}>{detail}</Text>}
    </Card>
  )
}

function changeText(current: number | null | undefined, previous: number | null | undefined) {
  if (current == null || previous == null || previous === 0) return 'No previous period comparison'
  const change = (current - previous) / previous
  return `${change >= 0 ? '+' : ''}${(change * 100).toFixed(1)}% vs previous period`
}

// What a cost leaves out matters more than how it moved: an unpriced deployment isn't free.
function costDetail(summary: AnalyticsCostSummary, current: number | null | undefined, previous: number | null | undefined) {
  if (summary.unpricedTokens > 0) {
    return `Leaves out ${formatCompact(summary.unpricedTokens)} tokens with no price`
  }
  return changeText(current, previous)
}

function reportEmpty(report: AnalyticsReport & { kpis?: AnalyticsKpis }) {
  return report.dataSource === 'notConfigured' || report.kpis?.requests === 0
}

function TableEmpty({ children, colSpan = 8 }: { children: string; colSpan?: number }) {
  return (
    <tr>
      <td colSpan={colSpan}>
        <EmptyState title="No rows">{children}</EmptyState>
      </td>
    </tr>
  )
}

function GatewayHealthRows({
  gateways,
  onRefresh,
  busyGateway,
}: {
  gateways: AnalyticsGatewayHealth[]
  onRefresh: (gatewayId: string) => void
  busyGateway: string | null
}) {
  return (
    <div className="table-scroll">
      <table aria-label="Gateway telemetry health">
        <thead>
          <tr>
            <th>Gateway</th>
            <th>Status</th>
            <th>Lag</th>
            <th>Backfill</th>
            <th>Last error</th>
            <th><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {gateways.length === 0 ? (
            <TableEmpty>No gateway telemetry health is available.</TableEmpty>
          ) : gateways.map((gateway) => (
            <tr key={gateway.gatewayId}>
              <td>{gateway.name}<Text block size={200}>{gateway.environmentName}</Text></td>
              <td><Badge color={gateway.status === 'current' ? 'success' : gateway.status === 'failing' ? 'danger' : gateway.status === 'notLinked' ? 'subtle' : 'warning'}>{FRESHNESS_STATUS_LABELS[gateway.status]}</Badge></td>
              <td>{gateway.lagMinutes == null ? '—' : `${gateway.lagMinutes} min`}</td>
              <td>{BACKFILL_STATUS_LABELS[gateway.backfillStatus]}{gateway.backfillNext ? ` · next ${gateway.backfillNext}` : ''}</td>
              <td>{gateway.lastError ?? gateway.diagnosticsError ?? '—'}</td>
              <td>
                <Button size="small" onClick={() => onRefresh(gateway.gatewayId)} disabled={busyGateway === gateway.gatewayId}>
                  {busyGateway === gateway.gatewayId ? 'Refreshing…' : 'Refresh now'}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function OverviewTab({ report }: { report: AnalyticsOverview }) {
  const trend = report.trend.map((point) => ({ label: trendLabel(point, report.window.granularity), primary: point.requests, secondary: point.totalTokens }))
  // Models and callers rank by tokens, which drive spend. APIs rank by calls, because MCP servers carry no tokens.
  const byTokens = (row: AnalyticsRankRow): BarListItem => ({ key: row.key, label: row.label, value: row.totalTokens, valueLabel: `${formatCompact(row.totalTokens)} tokens`, detail: [row.detail, `${formatCompact(row.requests)} calls`, row.cost != null ? formatCost(row.cost) : null].filter(Boolean).join(' · ') })
  const topModels = report.topModels.map(byTokens)
  const topCallers = report.topCallers.map(byTokens)
  const topCostCenters = (report.topCostCenters ?? []).map(byTokens)
  const topApis = report.topApis.map<BarListItem>((row) => ({ key: row.key, label: row.label, value: row.requests, valueLabel: `${formatCompact(row.requests)} calls`, detail: [row.detail, row.totalTokens ? `${formatCompact(row.totalTokens)} tokens` : null].filter(Boolean).join(' · ') }))

  return (
    <>
      <div className={styles.kpiGrid}>
        <KpiCard label="Requests" value={formatCompact(report.kpis.requests)} detail={changeText(report.kpis.requests, report.previous?.requests)} />
        <KpiCard label="Tokens" value={formatCompact(report.kpis.totalTokens)} detail={changeText(report.kpis.totalTokens, report.previous?.totalTokens)} />
        {report.cost && (
          <KpiCard
            label="Cost"
            value={report.window.granularity === 'hour' ? '—' : formatCostCompact(report.kpis.cost)}
            detail={report.window.granularity === 'hour' ? 'Priced by whole days; choose 7 days or more' : costDetail(report.cost, report.kpis.cost, report.previous?.cost)}
          />
        )}
        <KpiCard label="Active callers" value={formatNumber(report.kpis.activeCallers)} detail={report.window.range === '24h' ? 'Whole UTC-day breakdown' : undefined} />
        <KpiCard label="Errors" value={formatPercent(report.kpis.errorRate)} detail={`${formatNumber(report.kpis.errors)} error calls`} />
        <KpiCard label="P95 latency" value={formatLatency(report.kpis.p95LatencyMs)} detail="Estimated from latency buckets" />
      </div>
      {reportEmpty(report) ? <EmptyState title="No usage yet">MOSAIC has no rolled-up calls for these filters.</EmptyState> : (
        <div className={styles.analyticsGrid}>
          <Card className={styles.wideCard}>
            <Title3 as="h2">Requests and tokens</Title3>
            <TrendChart title="Requests and tokens trend" points={trend} primaryLabel="Requests" secondaryLabel="Tokens" />
          </Card>
          <Card className={styles.panelCard}><Title3 as="h2">Top models</Title3><BarList label="Top models" items={topModels} /></Card>
          <Card className={styles.panelCard}><Title3 as="h2">Top callers</Title3><BarList label="Top callers" items={topCallers} /></Card>
          <Card className={styles.panelCard}><Title3 as="h2">Top cost centers</Title3><BarList label="Top cost centers" items={topCostCenters} /></Card>
          <Card className={styles.panelCard}><Title3 as="h2">Top APIs</Title3><BarList label="Top APIs" items={topApis} /></Card>
        </div>
      )}
    </>
  )
}

function monthLabel(value: string) {
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'long', year: 'numeric' }).format(new Date(`${value}T00:00:00Z`))
}

function forecastDetail(report: AnalyticsCost) {
  const spend = report.spend
  if (!spend) return undefined
  if (spend.forecast == null) return 'Forecast after a day of this month’s figures'
  return `At this month’s pace so far, over ${spend.daysElapsed.toFixed(1)} of ${spend.daysInMonth} days`
}

function chargedBy(row: AnalyticsCostDeploymentRow) {
  if (row.pricing === 'unpriced') {
    return (
      <>
        <span className={styles.noPrice}>No price</span>
        <Text block size={200} className={styles.kpiDetail}>{row.unpricedMessage}</Text>
      </>
    )
  }
  if (row.pricing === 'provisioned') {
    const rate = row.monthlyAmount != null ? `${formatRate(row.monthlyAmount)} a month` : `${row.capacity ?? '?'} PTUs at ${formatRate(row.ptuHourly)} an hour`
    return (
      <>
        {rate}
        <Text block size={200} className={styles.kpiDetail}>{row.monthCost != null ? `${formatCost(row.monthCost)} this month, shared by tokens` : 'Shared by tokens'}</Text>
      </>
    )
  }
  return (
    <>
      {`${formatRate(row.inputPerMillion)} in · ${formatRate(row.outputPerMillion)} out per 1M`}
      <Text block size={200} className={styles.kpiDetail}>{[row.deploymentType, row.cloudLabel, row.region].filter(Boolean).join(' · ')}</Text>
    </>
  )
}

function costBars(rows: AnalyticsCost['models']): BarListItem[] {
  return rows.map((row) => ({
    key: row.key,
    label: row.label,
    value: row.cost ?? 0,
    valueLabel: formatCost(row.cost),
    detail: [row.detail, `${formatCompact(row.totalTokens)} tokens`, row.costShare != null ? `${formatShare(row.costShare)} of the cost` : null].filter(Boolean).join(' · '),
  }))
}

const CONSUMER_KINDS: Record<string, string> = { person: 'Person', application: 'Application', group: 'Security group' }

const onBehalfUnresolvedLabels: Record<AnalyticsOnBehalfUnresolved['reason'], string> = {
  malformed: 'with a malformed reference',
  missing: 'with no matching MCP call',
  late: 'after the MCP call ended',
  caller: 'made by another application',
  unknown: 'before MOSAIC knew the MCP grant',
}

function unresolvedOnBehalfText(rows: AnalyticsOnBehalfUnresolved[] | undefined) {
  const present = (rows ?? []).filter((row) => row.requests > 0)
  if (present.length === 0) return null
  const requests = present.reduce((total, row) => total + row.requests, 0)
  const details = present
    .map((row) => `${formatNumber(row.requests)} ${onBehalfUnresolvedLabels[row.reason]}`)
    .join(', ')
  return `MOSAIC couldn't attribute ${plural(requests, 'model call')} to a person: ${details}.`
}

function CostTab({ report }: { report: AnalyticsCost }) {
  if (!report.priced) {
    return <EmptyState title="No price list">This deployment has no price list, so MOSAIC can&apos;t put a cost on usage.</EmptyState>
  }
  const spend = report.spend
  const cost = report.cost
  const hourly = report.window.granularity === 'hour'
  const trend = report.trend.map((point) => ({ label: trendLabel(point, report.window.granularity), primary: point.cost, secondary: point.totalTokens ?? null }))
  const consumers = costBars(report.consumers.map((row) => ({ ...row, detail: row.kind ? CONSUMER_KINDS[row.kind] ?? row.kind : row.detail })))
  return (
    <div className={styles.stack}>
      <div className={styles.kpiGrid}>
        <KpiCard
          label="Spend this month"
          value={formatCostCompact(spend?.monthToDate, '—')}
          detail={spend ? `${monthLabel(spend.monthStart)} to ${formatDateTime(spend.through ?? null)}` : undefined}
        />
        <KpiCard label="Month-end forecast" value={formatCostCompact(spend?.forecast, '—')} badge="Projected" detail={forecastDetail(report)} />
        <KpiCard label="Cost in this range" value={hourly ? '—' : formatCostCompact(cost.total)} detail={hourly ? 'Priced by whole days; choose 7 days or more' : costDetail(cost, null, null)} />
        <KpiCard
          label="Reserved capacity"
          value={cost.reserved == null ? 'None' : formatCostCompact(cost.reserved)}
          detail={cost.reserved == null ? 'No provisioned deployment was charged in this range' : 'Provisioned deployments’ PTUs in this range'}
        />
      </div>
      {!hourly && (
        <Card className={styles.wideCard}>
          <Title3 as="h2">Cost and tokens</Title3>
          <TrendChart title="Cost and tokens trend" points={trend} primaryLabel="Cost" secondaryLabel="Tokens" primaryFormat={(value) => formatCost(value)} caption="Each line is scaled to its own peak. A day MOSAIC has no figures for is left blank." />
        </Card>
      )}
      <div className={styles.analyticsGrid}>
        <Card className={styles.panelCard}><Title3 as="h2">Cost by model</Title3><BarList label="Cost by model" items={costBars(report.models)} /></Card>
        <Card className={styles.panelCard}><Title3 as="h2">Cost by consumer</Title3><BarList label="Cost by consumer" items={consumers} /></Card>
        <Card className={styles.panelCard}><Title3 as="h2">Cost by cost center</Title3><BarList label="Cost by cost center" items={costBars(report.costCenters ?? [])} /></Card>
        <Card className={styles.panelCard}><Title3 as="h2">Cost by API</Title3><BarList label="Cost by API" items={costBars(report.apis)} /></Card>
      </div>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Deployments</Title3>
        <Text size={200}>What each deployment is charged by today, and what its usage in this range cost. A provisioned deployment costs its reserved capacity whether or not it&apos;s called; utilization is its tokens against what its PTUs could serve.</Text>
        <div className="table-scroll">
          <table aria-label="Deployment cost">
            <thead><tr><th>Deployment</th><th>Model</th><th>Charged by</th><th>Tokens</th><th>Utilization</th><th>Cost</th><th>Share</th></tr></thead>
            <tbody>
              {report.deployments.length === 0 ? <TableEmpty>No deployments match these filters.</TableEmpty> : report.deployments.map((row) => (
                <tr key={row.key}>
                  <td>{row.deploymentName}<Text block size={200}>{row.endpointName ?? 'Unknown endpoint'}</Text></td>
                  <td>{row.modelName ?? 'Unknown'}{row.modelVersion && <Text block size={200}>{row.modelVersion}</Text>}</td>
                  <td>{chargedBy(row)}</td>
                  <td>{formatNumber(row.totalTokens)}</td>
                  <td>{row.pricing === 'provisioned' ? formatPercent(row.utilization) : '—'}</td>
                  <td>
                    {costCell(row.cost, row.totalTokens)}
                    {row.idleCost != null && <Text block size={200} className={styles.kpiDetail}>{`${formatCost(row.idleCost)} with no calls`}</Text>}
                  </td>
                  <td>{formatShare(row.costShare)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      {cost.unpriced.length > 0 && (
        <Card className={styles.panelCard}>
          <Title3 as="h2">Usage with no price</Title3>
          <Text size={200}>
            {`${formatCompact(cost.unpricedTokens)} tokens in ${plural(cost.unpricedRequests, 'call')} have no cost, because MOSAIC has no price for them. They're left out of every total, never counted as $0. `}
            <RouterLink to="/pricing?tab=unpriced">Fix them on the Pricing page</RouterLink>.
          </Text>
          <div className="table-scroll">
            <table aria-label="Usage with no price">
              <thead><tr><th>Deployment or API</th><th>Why it has no price</th><th>Calls</th><th>Tokens</th></tr></thead>
              <tbody>
                {cost.unpriced.map((row) => (
                  <tr key={row.key}><td>{row.label}<Text block size={200}>{row.detail ?? ''}</Text></td><td>{row.message}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(row.totalTokens)}</td></tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
      {cost.notes.length > 0 && (
        <div className={styles.notes} role="note" aria-label="How MOSAIC priced this usage">
          {cost.notes.map((note) => <Text key={note}>{note}</Text>)}
        </div>
      )}
    </div>
  )
}

function OnBehalfCard({ report, priced }: { report: AnalyticsConsumers; priced: boolean }) {
  const rows = report.onBehalf ?? []
  const unresolvedText = unresolvedOnBehalfText(report.onBehalfUnresolved)
  return (
    <Card className={styles.panelCard}>
      <Title3 as="h2">Model use through MCP servers</Title3>
      <Text size={200}>
        Model calls an MCP server&apos;s application made for the people who called it. They&apos;re
        already counted above as the application&apos;s own, under its grant and cost center, so this
        table adds to no total.
      </Text>
      {unresolvedText && <Text size={200}>{unresolvedText}</Text>}
      <div className="table-scroll">
        <table aria-label="Model use through MCP servers">
          <thead><tr><th>Person</th><th>MCP server</th><th>Application</th><th>Requests</th><th>Tokens</th><th>Share</th>{priced && <th>Cost</th>}</tr></thead>
          <tbody>
            {rows.length === 0 ? <TableEmpty colSpan={priced ? 7 : 6}>No model calls were made through MCP servers for these filters.</TableEmpty> : rows.map((row) => (
              <tr key={row.key}>
                <td>{row.personLabel}<Text block size={200}>{row.personDetail ?? ''}</Text></td>
                <td>{row.mcpLabel}<Text block size={200}>{row.gatewayName}</Text></td>
                <td>{row.applicationLabel}<Text block size={200}>{row.applicationDetail ?? ''}</Text></td>
                <td>{formatNumber(row.requests)}</td>
                <td>{formatNumber(row.totalTokens)}</td>
                <td>{formatShare(row.requestShare)}</td>
                {priced && <td>{costCell(row.cost, row.totalTokens)}</td>}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function ConsumersTab({ report }: { report: AnalyticsConsumers }) {
  const priced = report.cost != null
  return (
    <div className={styles.stack}>
      <div className={styles.kpiGrid}>
        <KpiCard label="Linked requests" value={formatCompact(report.linkedRequests)} />
        <KpiCard label="Linked tokens" value={formatCompact(report.linkedTokens)} />
        {report.cost && <KpiCard label="Linked cost" value={formatCostCompact(report.cost.total)} detail={costDetail(report.cost, null, null)} />}
        <KpiCard label="Unidentified linked calls" value={formatCompact(report.unidentifiedRequests)} />
      </div>
      <Card className={styles.panelCard}>
        <Title3 as="h2">People, applications, and Entra security groups</Title3>
        <div className="table-scroll">
          <table aria-label="Consumers">
            <thead><tr><th>Name</th><th>Kind</th><th>Requests</th><th>Tokens</th><th>Grants</th><th>Resources</th><th>Last seen</th>{priced && <th>Cost</th>}</tr></thead>
            <tbody>
              {[...report.people, ...report.applications, ...report.groups].length === 0 ? <TableEmpty>No linked consumers match these filters.</TableEmpty> :
                [...report.people, ...report.applications, ...report.groups].map((row) => (
                  <tr key={row.key}><td>{row.label}<Text block size={200}>{row.detail ?? (row.members != null ? `${row.members} active members` : '')}</Text></td><td>{consumerKindLabel(row)}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(row.totalTokens)}</td><td>{row.grants}</td><td>{row.resources}</td><td>{formatDateTime(row.lastSeen)}</td>{priced && <td>{costCell(row.cost, row.totalTokens)}</td>}</tr>
                ))}
            </tbody>
          </table>
        </div>
      </Card>
      <OnBehalfCard report={report} priced={priced} />
      <Card className={styles.panelCard}>
        <Title3 as="h2">Grants</Title3>
        <div className="table-scroll">
          <table aria-label="Grant usage">
            <thead><tr><th>Subject</th><th>Resource</th><th>Cost center</th><th>State</th><th>Requests</th><th>Key requests</th><th>Callers</th><th>Peak minute</th>{priced && <th>Cost</th>}</tr></thead>
            <tbody>
              {report.grants.length === 0 ? <TableEmpty>No grants match these filters.</TableEmpty> : report.grants.map((row) => (
                <tr key={row.key}><td>{row.subjectLabel}<Text block size={200}>{grantSubjectKindLabel(row)} · {row.subjectDetail ?? 'No detail'}</Text></td><td>{row.resourceLabel}<Text block size={200}>{row.gatewayName ?? 'No gateway'}</Text></td><td>{row.costCenterName ?? '—'}<Text block size={200}>{row.costCenterCode ?? ''}</Text></td><td>{grantStateLabels[row.state]}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(row.keyRequests)}</td><td>{row.callers}</td><td>{row.peakMinuteTokens == null ? '—' : `${formatNumber(row.peakMinuteTokens)} tokens`}</td>{priced && <td>{costCell(row.cost, row.totalTokens)}</td>}</tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Cost centers</Title3>
        <div className="table-scroll">
          <table aria-label="Cost center usage">
            <thead><tr><th>Cost center</th><th>Grants</th><th>Callers</th><th>Requests</th><th>Tokens</th>{priced && <th>Cost</th>}<th>Share</th></tr></thead>
            <tbody>
              {!(report.costCenters ?? []).length ? <TableEmpty>No cost centers match these filters.</TableEmpty> : (report.costCenters ?? []).map((row) => (
                <tr key={row.key}><td>{row.label}<Text block size={200}>{row.code}</Text></td><td>{row.grants}</td><td>{row.callers}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(row.totalTokens)}</td>{priced && <td>{costCell(row.cost, row.totalTokens)}</td>}<td>{formatShare(row.requestShare)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Client applications</Title3>
        <BarList label="Client applications" items={report.clientApps.map((row) => ({ key: row.clientAppId, label: row.label, value: row.requests, valueLabel: `${formatCompact(row.requests)} calls`, detail: [`${row.apis} APIs`, row.cost != null ? formatCost(row.cost) : null].filter(Boolean).join(' · ') }))} />
      </Card>
    </div>
  )
}

// A row with tokens but no cost has no price; a row with no tokens, such as an MCP server's, has no cost to show.
function costCell(cost: number | null | undefined, tokens: number) {
  if (cost != null) return formatCost(cost)
  return tokens > 0 ? <span className={styles.noPrice}>No price</span> : '—'
}

// A model deployment's own 429 reaches the caller as the gateway's 429 too, so it counts as throttled
// as well. Taking those out leaves the calls the gateway's limits refused.
function gatewayThrottled(throttled: number, backendThrottled: number | null) {
  return Math.max(throttled - (backendThrottled ?? 0), 0)
}

function ApiRows({ rows, label, showCost = false }: { rows: AnalyticsApiRow[]; label: string; showCost?: boolean }) {
  return (
    <div className="table-scroll">
      <table aria-label={label}>
        <thead><tr><th>API</th><th>Kind</th><th>Requests</th><th>Denied</th><th>Errors</th><th>Backend 429s</th><th>P95</th><th>Models</th>{showCost && <th>Cost</th>}</tr></thead>
        <tbody>
          {rows.length === 0 ? <TableEmpty>No APIs match these filters.</TableEmpty> : rows.map((row) => (
            <tr key={row.key}><td>{row.label}<Text block size={200}>{row.gatewayName} · {row.apiName}</Text></td><td>{row.kind ? apiKindLabels[row.kind] : 'Unknown'}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(row.denied)}</td><td>{formatNumber(row.errors)}</td><td>{formatNumber(row.backendThrottled)}</td><td>{formatLatency(row.p95LatencyMs)}</td><td>{row.models?.join(', ') ?? 'Not read in this view'}</td>{showCost && <td>{costCell(row.cost, row.totalTokens)}</td>}</tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function ModelsTab({ report }: { report: AnalyticsModels }) {
  const priced = report.cost != null
  return (
    <div className={styles.stack}>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Models</Title3>
        <BarList label="Models by requests" items={report.models.map((row) => ({ key: row.model, label: row.model, value: row.requests, valueLabel: `${formatCompact(row.requests)} calls`, detail: [`${formatCompact(row.totalTokens)} tokens across ${row.apis} APIs`, row.cost != null ? formatCost(row.cost) : null].filter(Boolean).join(' · ') }))} />
      </Card>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Deployments</Title3>
        <Text size={200}>Deployment request counts include calls the gateway throttled. Backend 429s are shown separately from MOSAIC gateway throttling. Capacity and utilization appear for Azure OpenAI standard deployments, whose capacity is set in tokens a minute. Other models share a regional rate limit instead.</Text>
        <div className="table-scroll">
          <table aria-label="Deployments">
            <thead><tr><th>Deployment</th><th>Model</th><th>Requests</th><th>Gateway throttled</th><th>Backend 429s</th><th>Peak TPM</th><th>Capacity</th><th>Utilization</th>{priced && <th>Cost</th>}</tr></thead>
            <tbody>
              {report.deployments.length === 0 ? <TableEmpty>No deployments match these filters.</TableEmpty> : report.deployments.map((row: AnalyticsDeploymentRow) => (
                <tr key={row.key}><td>{row.deploymentName}<Text block size={200}>{row.endpointName ?? row.endpointId}</Text></td><td>{row.modelName ?? 'Unknown'}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(gatewayThrottled(row.throttled, row.backendThrottled))}</td><td>{formatNumber(row.backendThrottled)}</td><td>{formatNumber(row.peakMinuteTokens)}</td><td>{formatNumber(row.capacityTokensPerMinute)}</td><td>{formatPercent(row.utilization)}</td>{priced && <td>{costCell(row.cost, row.totalTokens)}</td>}</tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
      <Card className={styles.panelCard}><Title3 as="h2">APIs and MCP servers</Title3><ApiRows rows={report.apis} label="APIs and MCP servers" showCost={priced} /></Card>
    </div>
  )
}

function ReliabilityTab({ report }: { report: AnalyticsReliability }) {
  const mix = report.statusMix
  return (
    <div className={styles.stack}>
      <div className={styles.kpiGrid}>
        <KpiCard label="OK" value={formatCompact(mix.ok)} />
        <KpiCard label="Gateway throttled" value={formatCompact(gatewayThrottled(mix.throttled, mix.backendThrottled))} detail="A rate or token limit at the gateway answered these with 429." />
        <KpiCard label="Backend 429s" value={formatCompact(mix.backendThrottled)} detail="The model deployment returned 429, and the gateway passed it on." />
        <KpiCard label="Denied" value={formatCompact(mix.denied)} />
      </div>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Latency</Title3>
        <Text size={200}>Percentiles are estimated from latency buckets. Latency covers admitted calls only; denied calls are not timed, so the histogram total is requests minus denied calls.</Text>
        <Histogram buckets={report.latency.buckets} label={`Estimated latency histogram; P50 ${formatLatency(report.latency.p50Ms)}, P95 ${formatLatency(report.latency.p95Ms)}, P99 ${formatLatency(report.latency.p99Ms)}.`} />
      </Card>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Denials by reason</Title3>
        <BarList label="Denial reasons" items={report.denialReasons.map((row) => ({ key: row.reason, label: row.label, value: row.requests, valueLabel: `${formatCompact(row.requests)} calls`, detail: formatPercent(row.share) }))} />
      </Card>
      <Card className={styles.panelCard}><Title3 as="h2">Per-API reliability</Title3><ApiRows rows={report.apis} label="Per-API reliability" /></Card>
    </div>
  )
}

function LimitsTab({ report }: { report: AnalyticsLimits }) {
  const rows = [...report.rows].sort((left, right) => (right.utilization ?? -1) - (left.utilization ?? -1))
  return (
    <Card className={styles.panelCard}>
      <Title3 as="h2">Grant limits</Title3>
      <Text size={200}>Reached means the gateway refused calls under the grant&apos;s limits in this range, or use is at a limit. Near means at least {Math.round(report.threshold * 100)}% used. Unknown means MOSAIC cannot tell how close the grant is. Throttled counts only the gateway&apos;s refusals, not a model deployment&apos;s own 429s.</Text>
      <div className="table-scroll">
        <table aria-label="Grant limit use">
          <thead><tr><th>Grant</th><th>Resource</th><th>Cost center</th><th>Status</th><th>Use vs limit</th><th>Throttled</th><th>Quota refused</th></tr></thead>
          <tbody>
            {rows.length === 0 ? <TableEmpty>No grant limits match these filters.</TableEmpty> : rows.map((row) => (
              <tr key={row.key}>
                <td>{row.subjectLabel}<Text block size={200}>{row.memberLabel ? `Member ${row.memberLabel}` : grantSubjectKindLabel(row)}</Text></td>
                <td>{row.resourceLabel}<Text block size={200}>{row.gatewayName ?? 'No gateway'}</Text></td>
                <td>{row.costCenterName ?? '—'}<Text block size={200}>{row.costCenterCode ?? ''}</Text></td>
                <td><Badge className={styles.statusBadge} color={row.status === 'reached' ? 'danger' : row.status === 'near' ? 'warning' : row.status === 'ok' ? 'success' : 'subtle'}>{limitStatusLabels[row.status]}</Badge></td>
                <td>
                  <BarList label={`${row.subjectLabel} limits`} items={row.limits.map((limit) => ({ key: `${row.key}-${limit.kind}-${limit.metric}`, label: limitLabel(limit), value: limit.utilization ?? 0, valueLabel: `${formatNumber(limit.used)} / ${formatNumber(limit.limit)}`, detail: limit.partial ? 'Partial window' : undefined, tone: limit.utilization != null && limit.utilization >= 1 ? 'danger' : limit.utilization != null && limit.utilization >= report.threshold ? 'warning' : 'normal' }))} />
                </td>
                <td>{formatNumber(row.throttled)}</td>
                <td>{formatNumber(row.quotaRefused)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

const untrackedReasons = {
  mosaicGroup: "MOSAIC groups are desired state; the gateway enforces Entra security groups, so calls can't be tracked to this grant.",
  notApplied: 'This grant is not applied to the gateway yet.',
  noLink: 'This grant has no attribution link.',
}

const unattributedReasons: Record<AnalyticsUnattributed['rows'][number]['reason'], string> = {
  noSubscription: 'No subscription key',
  unknownSubscription: 'Unknown key',
  sharedKey: 'Shared key',
}

function HygieneTab({ report }: { report: AnalyticsHygiene }) {
  return (
    <div className={styles.stack}>
      <MessageBar>
        <MessageBarBody>
          <MessageBarTitle>{formatNumber(report.judgedGrants)} grants judged</MessageBarTitle>
          A grant is judged only when data covers the whole window, is at most a day stale, and the grant predates the window.
        </MessageBarBody>
      </MessageBar>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Unused grants</Title3>
        <div className="table-scroll"><table aria-label="Unused grants"><thead><tr><th>Grant</th><th>Resource</th><th>Cost center</th><th>Granted</th><th>Last used</th></tr></thead><tbody>{report.unusedGrants.length === 0 ? <TableEmpty>No unused grants in this window.</TableEmpty> : report.unusedGrants.map((row) => <tr key={row.entitlementId}><td>{row.subjectLabel}</td><td>{row.resourceLabel}</td><td>{row.costCenterName ?? '—'}<Text block size={200}>{row.costCenterCode ?? ''}</Text></td><td>{formatDateTime(row.grantedAt)}</td><td>{formatDateTime(row.lastUsedAt)}</td></tr>)}</tbody></table></div>
      </Card>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Unused keys</Title3>
        <Text size={200}>These grants have APIM subscription keys, but every call came without the key. Consider turning keys off.</Text>
        <div className="table-scroll"><table aria-label="Unused keys"><thead><tr><th>Grant</th><th>Cost center</th><th>Subscription</th><th>Token requests</th><th>Resource</th></tr></thead><tbody>{report.unusedKeys.length === 0 ? <TableEmpty>No unused keys in this window.</TableEmpty> : report.unusedKeys.map((row) => <tr key={row.entitlementId}><td>{row.subjectLabel}</td><td>{row.costCenterName ?? '—'}<Text block size={200}>{row.costCenterCode ?? ''}</Text></td><td>{row.subscriptionName ?? '—'}</td><td>{formatNumber(row.tokenRequests)}</td><td>{row.resourceLabel}</td></tr>)}</tbody></table></div>
      </Card>
      <Card className={styles.panelCard}>
        <Title3 as="h2">Untracked grants</Title3>
        <div className="table-scroll"><table aria-label="Untracked grants"><thead><tr><th>Grant</th><th>Resource</th><th>Cost center</th><th>Reason</th></tr></thead><tbody>{report.untrackedGrants.length === 0 ? <TableEmpty>No untracked grants in this window.</TableEmpty> : report.untrackedGrants.map((row) => <tr key={row.entitlementId}><td>{row.subjectLabel}</td><td>{row.resourceLabel}</td><td>{row.costCenterName ?? '—'}<Text block size={200}>{row.costCenterCode ?? ''}</Text></td><td>{untrackedReasons[row.reason]}</td></tr>)}</tbody></table></div>
      </Card>
    </div>
  )
}

function UnattributedTab({ report }: { report: AnalyticsUnattributed }) {
  const priced = report.cost != null
  return (
    <Card className={styles.panelCard}>
      <Title3 as="h2">Unattributed calls</Title3>
      <Text size={200}>{formatCompact(report.requests)} calls ({formatPercent(report.share)}) could not be linked to a grant. A publication&apos;s or model pool&apos;s shared key belongs to no one caller, so grant access per caller to see who uses it.{report.cost?.total != null ? ` They cost ${formatCost(report.cost.total)} at list prices.` : ''}</Text>
      <div className="table-scroll">
        <table aria-label="Unattributed calls">
          <thead><tr><th>API</th><th>Gateway</th><th>Subscription</th><th>Reason</th><th>Requests</th><th>Tokens</th><th>Last seen</th>{priced && <th>Cost</th>}</tr></thead>
          <tbody>
            {report.rows.length === 0 ? <TableEmpty>No unattributed calls match these filters.</TableEmpty> : report.rows.map((row) => (
              <tr key={`${row.gatewayId}-${row.apiName}-${row.subscription ?? 'none'}`}><td>{row.apiLabel}<Text block size={200}>{row.apiName}</Text></td><td>{row.gatewayName}</td><td>{row.subscription ?? 'No subscription'}</td><td>{unattributedReasons[row.reason]}</td><td>{formatNumber(row.requests)}</td><td>{formatNumber(row.totalTokens)}</td><td>{formatDateTime(row.lastSeen)}</td>{priced && <td>{costCell(row.cost, row.totalTokens)}</td>}</tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}

function ResourceOptions({ modelApis, modelPools, mcpServers }: { modelApis?: ModelApi[]; modelPools?: ModelPool[]; mcpServers?: McpServer[] }) {
  // A pool is reported once its API is in API Management, as the API resolves the filter.
  const groups = [
    { label: 'Model APIs', items: modelApis ?? [] },
    { label: 'Model pools', items: (modelPools ?? []).filter(holdsApi) },
    { label: 'MCP servers', items: mcpServers ?? [] },
  ].filter((group) => group.items.length > 0)
  return (
    <>
      {groups.map((group) => (
        <optgroup key={group.label} label={group.label}>
          {group.items.map((item) => <option key={item.id} value={item.id}>{item.displayName}</option>)}
        </optgroup>
      ))}
    </>
  )
}

function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = filename
  document.body.append(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

export function AnalyticsPage() {
  const api = useMosaicApi()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()
  const filters = useMemo(() => filtersFromSearch(params), [params])
  const tab = tabFromSearch(params)
  const [exportStatus, setExportStatus] = useState<string | null>(null)
  const [exportChoice, setExportChoice] = useState<ExportView | null>(null)
  const [refreshMessage, setRefreshMessage] = useState<string | null>(null)
  const catalog = useEnvironmentCatalog()
  const gateways = useQuery({ queryKey: ['gateways'], queryFn: api.listGateways })
  const modelApis = useQuery({ queryKey: ['model-apis'], queryFn: () => api.listModelApis() })
  const mcpServers = useQuery({ queryKey: ['mcp-servers'], queryFn: () => api.listMcpServers() })
  const modelPools = useQuery({ queryKey: ['model-pools', 'list'], queryFn: () => api.listModelPools() })
  const costCenters = useQuery({ queryKey: ['cost-centers'], queryFn: api.listCostCenters })

  const reportQuery = useQuery<AnalyticsReport>({
    queryKey: ['analytics', tab, filters],
    queryFn: () => {
      if (tab === 'cost') return api.getAnalyticsCost(filters)
      if (tab === 'consumers') return api.getAnalyticsConsumers(filters)
      if (tab === 'models') return api.getAnalyticsModels(filters)
      if (tab === 'reliability') return api.getAnalyticsReliability(filters)
      if (tab === 'limits') return api.getAnalyticsLimits(filters)
      if (tab === 'hygiene') return api.getAnalyticsHygiene(filters)
      if (tab === 'unattributed') return api.getAnalyticsUnattributed(filters)
      return api.getAnalyticsOverview(filters)
    },
  })

  const refreshGateway = useMutation({
    mutationFn: api.refreshGatewayTelemetry,
    onMutate: (gatewayId) => {
      setRefreshMessage(null)
      return gatewayId
    },
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['analytics'] }),
    onError: (error) => {
      const status = error instanceof ApiError ? error.status : (error as { status?: number }).status
      if (status === 429) {
        setRefreshMessage('That gateway was refreshed less than a minute ago. Try again shortly.')
      } else if (status === 409) {
        setRefreshMessage("This deployment doesn't roll up gateway telemetry.")
      } else {
        setRefreshMessage(error instanceof Error ? error.message : 'Refresh failed.')
      }
    },
  })

  const exportCsv = useMutation({
    mutationFn: async (view: ExportView) => ({ view, file: await api.exportAnalytics(view, filters) }),
    onSuccess: ({ view, file }) => {
      downloadBlob(file.blob, file.filename ?? `mosaic-${view}.csv`)
      setExportStatus(`${exportLabels[view]} CSV downloaded.`)
    },
    onError: (error) => setExportStatus(error instanceof Error ? error.message : 'Export failed.'),
  })

  function updateFilter(key: keyof AnalyticsFilters | 'tab', value: string) {
    const next = new URLSearchParams(params)
    if (value) next.set(key, value)
    else next.delete(key)
    if (key === 'range' && value !== 'custom') {
      next.delete('start')
      next.delete('end')
    }
    setParams(next)
  }

  const report = reportQuery.data as AnalyticsReport | undefined
  const activeExports = tabs.find((item) => item.key === tab)?.exports ?? ['trend']
  const exportView = exportChoice && activeExports.includes(exportChoice) ? exportChoice : activeExports[0]

  return (
    <section className={styles.page}>
      <PageHeader
        title="Analytics"
        description="Inspect real gateway usage, reliability, limits, and access hygiene from MOSAIC usage rollups."
        source={report ? sourceKind(report.dataSource) : undefined}
        actions={
          <div className={styles.headerControls}>
            <label className={styles.filterControl}><span>Range</span><Select value={filters.range ?? '30d'} onChange={(event) => updateFilter('range', event.target.value)}><option value="24h">Last 24 hours</option><option value="7d">Last 7 days</option><option value="30d">Last 30 days</option><option value="90d">Last 90 days</option><option value="12m">Last 12 months</option><option value="custom">Custom</option></Select></label>
            {filters.range === 'custom' && <><label className={styles.filterControl}><span>Start</span><Input type="date" value={filters.start ?? ''} onChange={(event) => updateFilter('start', event.target.value)} /></label><label className={styles.filterControl}><span>End</span><Input type="date" value={filters.end ?? ''} onChange={(event) => updateFilter('end', event.target.value)} /></label></>}
            <label className={styles.filterControl}><span>Gateway</span><Select value={filters.gatewayId ?? ''} onChange={(event) => updateFilter('gatewayId', event.target.value)}><option value="">All gateways</option>{(gateways.data ?? []).map((gateway: Gateway) => <option key={gateway.id} value={gateway.id}>{gateway.name}</option>)}</Select></label>
            <label className={styles.filterControl}><span>Environment</span><Select value={filters.environment ?? ''} onChange={(event) => updateFilter('environment', event.target.value)}><option value="">All environments</option>{(catalog.data?.environments ?? []).map((environment) => <option key={environment.key} value={environment.key}>{environmentLabel(catalog.data, environment.key)}</option>)}</Select></label>
            <label className={styles.filterControl}><span>Resource</span><Select value={filters.resourceId ?? ''} onChange={(event) => updateFilter('resourceId', event.target.value)}><option value="">All APIs and MCP servers</option><ResourceOptions modelApis={modelApis.data} modelPools={modelPools.data} mcpServers={mcpServers.data} /></Select></label>
            <label className={styles.filterControl}><span>Cost center</span><Select value={filters.costCenterId ?? ''} onChange={(event) => updateFilter('costCenterId', event.target.value)}><option value="">All cost centers</option>{(costCenters.data ?? []).map((costCenter) => <option key={costCenter.id} value={costCenter.id}>{costCenter.name} ({costCenter.code})</option>)}</Select></label>
            <label className={styles.filterControl}><span>Subject kind</span><Select value={filters.subjectKind ?? ''} onChange={(event) => updateFilter('subjectKind', event.target.value)}><option value="">All subjects</option>{Object.entries(subjectLabels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</Select></label>
            <label className={styles.filterControl}><span>Export</span><Select value={exportView} onChange={(event) => setExportChoice(event.target.value as ExportView)}>{activeExports.map((view) => <option key={view} value={view}>{exportLabels[view]}</option>)}</Select></label>
            <Button appearance="primary" onClick={() => exportCsv.mutate(exportView)} disabled={exportCsv.isPending}>Export CSV</Button>
          </div>
        }
      />

      <TabList className={styles.tabs} selectedValue={tab} onTabSelect={(_, data) => updateFilter('tab', data.value as string)}>
        {tabs.map((item) => <Tab key={item.key} value={item.key}>{item.label}</Tab>)}
      </TabList>

      {exportStatus && <div className={styles.exportStatus} role="status">{exportStatus}</div>}
      {refreshMessage && <MessageBar intent="warning"><MessageBarBody>{refreshMessage}</MessageBarBody></MessageBar>}

      {reportQuery.isPending && <Loading label="Loading analytics" />}
      {reportQuery.isError && <ErrorState error={reportQuery.error} />}
      {report && (
        <div className={styles.stack}>
          <DataNotes report={report} />
          {tab === 'overview' && (
            <>
              <OverviewTab report={report as AnalyticsOverview} />
              <Card className={styles.panelCard}>
                <Title3 as="h2">Gateway health</Title3>
                <GatewayHealthRows gateways={(report as AnalyticsOverview).gateways} onRefresh={(gatewayId) => refreshGateway.mutate(gatewayId)} busyGateway={refreshGateway.isPending ? refreshGateway.variables ?? null : null} />
              </Card>
            </>
          )}
          {tab === 'cost' && <CostTab report={report as AnalyticsCost} />}
          {tab === 'consumers' && <ConsumersTab report={report as AnalyticsConsumers} />}
          {tab === 'models' && <ModelsTab report={report as AnalyticsModels} />}
          {tab === 'reliability' && <ReliabilityTab report={report as AnalyticsReliability} />}
          {tab === 'limits' && <LimitsTab report={report as AnalyticsLimits} />}
          {tab === 'hygiene' && <HygieneTab report={report as AnalyticsHygiene} />}
          {tab === 'unattributed' && <UnattributedTab report={report as AnalyticsUnattributed} />}
        </div>
      )}
      {reportQuery.isFetching && !reportQuery.isPending && <Spinner size="tiny" label="Refreshing analytics" />}
    </section>
  )
}
