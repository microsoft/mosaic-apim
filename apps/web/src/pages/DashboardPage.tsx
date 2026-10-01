import {
  Badge,
  Button,
  Card,
  Spinner,
  Text,
  Title2,
  Title3,
} from '@fluentui/react-components'
import {
  BotRegular,
  ChartMultipleRegular,
  CloudDatabaseRegular,
  PeopleCommunityRegular,
  PersonAccountsRegular,
} from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { ErrorState } from '../components/AsyncState'
import { BarList, type BarListItem } from '../components/charts/BarList'
import { TrendChart } from '../components/charts/TrendChart'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { DataSourceBadge, PageHeader } from '../components/PageHeader'
import { environmentFindingsQueryKey, useEnvironmentCatalog } from '../environments'
import { FRESHNESS_STATUS_LABELS, plural, type PrincipalTab, principalTabForKind } from '../labels'
import type { AnalyticsRankRow } from '../types'
import styles from './DashboardPage.module.css'

function SparkMetric({
  label,
  value,
  detail,
  icon,
}: {
  label: string
  value: string
  detail: string
  icon: ReactNode
}) {
  return (
    <Card className={styles.metricCard}>
      <div className={styles.metricLabel}>
        <Text>{label}</Text>
        <span className={styles.metricIcon}>{icon}</span>
      </div>
      <div className={styles.metricValue}>{value}</div>
      <div className={styles.trendDetail}>
        <Text size={200}>{detail}</Text>
      </div>
      <DataSourceBadge kind="live" />
    </Card>
  )
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

function dayLabel(start: string) {
  return new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric' }).format(new Date(start))
}

// The overview shows the leaders; Analytics has the full rankings.
const TOP = 5

// Models and callers rank by tokens, which drive spend. APIs rank by calls, because MCP servers carry no tokens.
function byTokens(row: AnalyticsRankRow): BarListItem {
  return { key: row.key, label: row.label, value: row.totalTokens, valueLabel: `${formatCompact(row.totalTokens)} tokens`, detail: `${formatCompact(row.requests)} calls` }
}

function byCalls(row: AnalyticsRankRow): BarListItem {
  // MCP servers carry no tokens, so a token count only shows where there is one.
  const tokens = row.totalTokens ? `${formatCompact(row.totalTokens)} tokens` : null
  return { key: row.key, label: row.label, value: row.requests, valueLabel: `${formatCompact(row.requests)} calls`, detail: [row.detail, tokens].filter(Boolean).join(' · ') }
}

function RankingPanel({ title, caption, items, loading }: { title: string; caption: string; items: BarListItem[]; loading: boolean }) {
  return (
    <div className="panel">
      <div className="panel-header">
        <div className={styles.panelHeading}>
          <Title3 as="h2">{title}</Title3>
          <Text size={200}>{caption}</Text>
        </div>
      </div>
      <div className={styles.panelBody}>
        {loading ? <Spinner size="tiny" label={`Loading ${title.toLowerCase()}`} /> : <BarList label={title} items={items} />}
      </div>
    </div>
  )
}

function statusColor(status: string): 'success' | 'warning' | 'danger' | 'subtle' {
  if (status === 'current') return 'success'
  if (status === 'failing') return 'danger'
  if (status === 'delayed') return 'warning'
  return 'subtle'
}

function InventoryCard({
  icon,
  count,
  loadingLabel,
  singular,
  plural,
  onClick,
}: {
  icon: ReactNode
  /** Leave unset while the count is loading. */
  count?: number
  loadingLabel: string
  singular: string
  plural: string
  onClick: () => void
}) {
  return (
    <button className={styles.inventoryCard} onClick={onClick}>
      <span className={styles.inventoryIcon}>{icon}</span>
      <span>
        <strong>{count === undefined ? <Spinner size="tiny" label={loadingLabel} /> : count}</strong>
        <small>{count === 1 ? singular : plural}</small>
      </span>
    </button>
  )
}

export function DashboardPage() {
  const api = useMosaicApi()
  const navigate = useNavigate()
  const principals = useQuery({
    queryKey: ['principals'],
    queryFn: api.listPrincipals,
  })
  const groups = useQuery({ queryKey: ['groups'], queryFn: api.listGroups })
  const environmentCatalog = useEnvironmentCatalog()
  const environmentFindings = useQuery({
    queryKey: environmentFindingsQueryKey(),
    queryFn: () => api.listEnvironmentFindings(),
  })
  const analytics = useQuery({
    queryKey: ['analytics', 'dashboard', '7d'],
    queryFn: () => api.getAnalyticsOverview({ range: '7d' }),
  })
  const analyticsStatus = useQuery({
    queryKey: ['analytics-status'],
    queryFn: api.getAnalyticsStatus,
  })
  const trend = useMemo(
    () =>
      (analytics.data?.trend ?? []).map((point) => ({
        label: dayLabel(point.start),
        primary: point.requests,
        secondary: point.totalTokens,
      })),
    [analytics.data],
  )
  const liveError = principals.error ?? groups.error
  const principalCounts = useMemo(() => {
    const counts: Record<PrincipalTab, number> = { users: 0, agents: 0, workloads: 0 }
    for (const principal of principals.data ?? []) {
      counts[principalTabForKind(principal.kind)] += 1
    }
    return counts
  }, [principals.data])

  return (
    <section className={styles.page}>
      <PageHeader
        title="Overview"
        description="Monitor MOSAIC desired state and real gateway usage from the last 7 days."
      />

      <div className={styles.liveSection}>
        <div className={styles.sectionHeading}>
          <div>
            <Title2 as="h2" block>Desired-state inventory</Title2>
            <Text block>Live records stored and managed by MOSAIC.</Text>
          </div>
          <DataSourceBadge kind="live" />
        </div>
        {liveError ? (
          <ErrorState error={liveError} />
        ) : (
          <div className={styles.liveGrid}>
            <InventoryCard
              icon={<PersonAccountsRegular />}
              count={principals.isLoading ? undefined : principalCounts.users}
              loadingLabel="Loading people"
              singular="person"
              plural="people"
              onClick={() => navigate('/identity?tab=users')}
            />
            <InventoryCard
              icon={<BotRegular />}
              count={principals.isLoading ? undefined : principalCounts.agents}
              loadingLabel="Loading agents"
              singular="agent"
              plural="agents"
              onClick={() => navigate('/identity?tab=agents')}
            />
            <InventoryCard
              icon={<CloudDatabaseRegular />}
              count={principals.isLoading ? undefined : principalCounts.workloads}
              loadingLabel="Loading applications and security groups"
              singular="app or security group"
              plural="apps and security groups"
              onClick={() => navigate('/identity?tab=workloads')}
            />
            <InventoryCard
              icon={<PeopleCommunityRegular />}
              count={groups.isLoading ? undefined : (groups.data?.length ?? 0)}
              loadingLabel="Loading groups"
              singular="MOSAIC group"
              plural="MOSAIC groups"
              onClick={() => navigate('/identity?tab=groups')}
            />
          </div>
        )}
        <Card className={styles.environmentsCard}>
          <div className={styles.environmentsHeader}>
            <div>
              <Title3 as="h3" block>
                Environments
              </Title3>
              <Text size={200} block>
                Live classification counts from Settings.
              </Text>
            </div>
            <Button appearance="subtle" size="small" onClick={() => navigate('/settings')}>
              Classify
            </Button>
          </div>
          {environmentCatalog.isLoading && <Spinner size="tiny" label="Loading environments" />}
          {environmentCatalog.isError && <ErrorState error={environmentCatalog.error} />}
          {environmentCatalog.data && environmentCatalog.data.environments.length === 0 && (
            <Text>No environments are configured yet.</Text>
          )}
          {environmentCatalog.data && environmentCatalog.data.environments.length > 0 && (
            <div className={styles.environmentRows} role="list" aria-label="Environment usage">
              {environmentCatalog.data.environments.map((environment) => (
                <div key={environment.key} className={styles.environmentRow} role="listitem">
                  <EnvironmentBadge environment={environment.key} catalog={environmentCatalog.data} />
                  <span>{plural(environment.usage.gateways, 'gateway')}</span>
                  <span>{plural(environment.usage.modelEndpoints, 'model endpoint')}</span>
                  <span>{plural(environment.usage.mcpEndpoints, 'MCP server')}</span>
                </div>
              ))}
              <div
                className={`${styles.environmentRow} ${
                  environmentCatalog.data.unclassified.gateways +
                    environmentCatalog.data.unclassified.modelEndpoints +
                    environmentCatalog.data.unclassified.mcpEndpoints >
                  0
                    ? styles.unclassifiedRow
                    : ''
                }`}
                role="listitem"
              >
                <EnvironmentBadge environment={null} catalog={environmentCatalog.data} />
                <span>{plural(environmentCatalog.data.unclassified.gateways, 'gateway')}</span>
                <span>
                  {plural(environmentCatalog.data.unclassified.modelEndpoints, 'model endpoint')}
                </span>
                <span>{plural(environmentCatalog.data.unclassified.mcpEndpoints, 'MCP server')}</span>
                <Button appearance="subtle" size="small" onClick={() => navigate('/settings')}>
                  Review suggestions
                </Button>
              </div>
            </div>
          )}
          <div className={styles.findingsLine}>
            {environmentFindings.isLoading ? (
              <Spinner size="tiny" label="Loading findings" />
            ) : environmentFindings.isError ? (
              <Text>Findings unavailable</Text>
            ) : (
              <Button appearance="subtle" size="small" onClick={() => navigate('/settings')}>
                {plural(environmentFindings.data?.items.length ?? 0, 'environment finding')}
              </Button>
            )}
          </div>
        </Card>
      </div>

      {analytics.data?.dataSource === 'notConfigured' && (
        <Card className={styles.environmentsCard}>
          <div className={styles.environmentsHeader}>
            <div>
              <Title3 as="h2">Usage telemetry is not configured</Title3>
              <Text size={200}>Local and test runs do not read gateway telemetry. In Azure, MOSAIC reads gateways' Log Analytics data.</Text>
            </div>
            <Button appearance="primary" onClick={() => navigate('/analytics')}>Open analytics</Button>
          </div>
          {analytics.data.notes.map((note) => <Text key={note} size={200}>{note}</Text>)}
        </Card>
      )}

      <div className={styles.metricGrid}>
        <SparkMetric label="Requests" value={analytics.isLoading ? '—' : formatCompact(analytics.data?.kpis.requests)} detail="Last 7 days" icon={<ChartMultipleRegular />} />
        <SparkMetric label="Tokens" value={analytics.isLoading ? '—' : formatCompact(analytics.data?.kpis.totalTokens)} detail="Prompt and completion tokens" icon={<CloudDatabaseRegular />} />
        <SparkMetric label="Active callers" value={analytics.isLoading ? '—' : formatCompact(analytics.data?.kpis.activeCallers)} detail="Linked people, apps, and groups" icon={<PersonAccountsRegular />} />
        <SparkMetric label="Error rate" value={analytics.isLoading ? '—' : formatPercent(analytics.data?.kpis.errorRate)} detail={`${formatCompact(analytics.data?.kpis.errors)} error calls`} icon={<ChartMultipleRegular />} />
        <SparkMetric label="P95 latency" value={analytics.isLoading ? '—' : formatLatency(analytics.data?.kpis.p95LatencyMs)} detail="Estimated from latency buckets" icon={<CloudDatabaseRegular />} />
      </div>

      <div className={styles.dashboardGrid}>
        <div className={`panel ${styles.volumePanel}`}>
          <div className="panel-header">
            <div className={styles.panelHeading}>
              <Title3 as="h2">Requests and tokens</Title3>
              <Text size={200}>Gateway calls in the last 7 days, rolled up from Log Analytics.</Text>
            </div>
            <Button appearance="subtle" size="small" onClick={() => navigate('/analytics')}>
              Open analytics
            </Button>
          </div>
          <div className={styles.panelBody}>
            {analytics.isLoading ? <Spinner label="Loading request volume" /> : analytics.isError ? <ErrorState error={analytics.error} /> : (
              <TrendChart title="Requests and tokens in the last 7 days" points={trend} primaryLabel="Requests" secondaryLabel="Tokens" />
            )}
          </div>
        </div>

        <div className="panel">
          <div className="panel-header">
            <div className={styles.panelHeading}>
              <Title3 as="h2">Telemetry health</Title3>
              <Text size={200}>How current each gateway&apos;s usage is.</Text>
            </div>
            <DataSourceBadge kind={analyticsStatus.data?.dataSource === 'logAnalytics' ? 'live' : 'local'} />
          </div>
          <div className={styles.healthList}>
            {analyticsStatus.isLoading && <Spinner size="tiny" label="Loading telemetry health" />}
            {analyticsStatus.data?.gateways.map((gateway) => (
              <div key={gateway.gatewayId}>
                <span className={styles.healthName}>
                  <span>{gateway.name}</span>
                  <small>
                    {gateway.status === 'notLinked'
                      ? 'Govern an API here to track its usage'
                      : plural(gateway.governedApis, 'governed API')}
                    {/* The badge already says a caught-up gateway is current, so only a real lag is spelled out. */}
                    {gateway.lagMinutes != null && gateway.lagMinutes > 0 && ` · ${gateway.lagMinutes} min behind`}
                  </small>
                </span>
                <Badge color={statusColor(gateway.status)}>{FRESHNESS_STATUS_LABELS[gateway.status]}</Badge>
              </div>
            ))}
            {!analyticsStatus.isLoading && (analyticsStatus.data?.gateways.length ?? 0) === 0 && <Text>No gateway health yet.</Text>}
          </div>
        </div>

        <RankingPanel title="Top models" caption="By tokens" items={(analytics.data?.topModels ?? []).slice(0, TOP).map(byTokens)} loading={analytics.isLoading} />
        <RankingPanel title="Top callers" caption="By tokens" items={(analytics.data?.topCallers ?? []).slice(0, TOP).map(byTokens)} loading={analytics.isLoading} />
        <RankingPanel title="Top APIs" caption="By calls, since MCP servers use no tokens" items={(analytics.data?.topApis ?? []).slice(0, TOP).map(byCalls)} loading={analytics.isLoading} />
      </div>
    </section>
  )
}
