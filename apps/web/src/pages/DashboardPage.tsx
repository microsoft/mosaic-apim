import {
  Badge,
  Button,
  Card,
  Select,
  Spinner,
  Text,
  Title2,
  Title3,
} from '@fluentui/react-components'
import {
  ArrowTrendingRegular,
  BotRegular,
  ChartMultipleRegular,
  CloudDatabaseRegular,
  MoneyRegular,
  PeopleCommunityRegular,
  PersonAccountsRegular,
} from '@fluentui/react-icons'
import { useQuery } from '@tanstack/react-query'
import { type ReactNode, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMosaicApi } from '../api'
import { ErrorState } from '../components/AsyncState'
import { EnvironmentBadge } from '../components/EnvironmentBadge'
import { DataSourceBadge, PageHeader, PreviewNotice } from '../components/PageHeader'
import { environmentFindingsQueryKey, useEnvironmentCatalog } from '../environments'
import { plural, type PrincipalTab, principalTabForKind } from '../labels'
import styles from './DashboardPage.module.css'

type TimeRange = '24h' | '7d' | '30d'

const requestSeries: Record<TimeRange, number[]> = {
  '24h': [2100, 2800, 1500, 5200, 6100, 8700, 7900, 12_100, 10_400, 13_800, 12_900],
  '7d': [48_000, 61_000, 54_000, 72_000, 81_000, 68_000, 76_000],
  '30d': [182_000, 214_000, 205_000, 248_000, 273_000, 291_000, 318_000, 302_000],
}

const metricCopy: Record<
  TimeRange,
  { requests: string; tokens: string; cost: string; trend: string }
> = {
  '24h': { requests: '12.4M', tokens: '4.2B', cost: '$3,420.50', trend: '+5.2%' },
  '7d': { requests: '76.8M', tokens: '25.9B', cost: '$21,480', trend: '+3.8%' },
  '30d': { requests: '318M', tokens: '108B', cost: '$88,920', trend: '+8.4%' },
}

const topModels = [
  { name: 'gpt-4o', share: 52 },
  { name: 'gpt-4o-mini', share: 28 },
  { name: 'text-embedding-3-small', share: 14 },
  { name: 'mistral-large', share: 6 },
]

function SparkMetric({
  label,
  value,
  detail,
  icon,
  intent,
}: {
  label: string
  value: string
  detail: string
  icon: ReactNode
  intent?: 'warning'
}) {
  return (
    <Card className={`${styles.metricCard} ${intent === 'warning' ? styles.warningCard : ''}`}>
      <div className={styles.metricLabel}>
        <Text>{label}</Text>
        <span className={styles.metricIcon}>{icon}</span>
      </div>
      <div className={styles.metricValue}>{value}</div>
      <div className={intent === 'warning' ? styles.warningDetail : styles.trendDetail}>
        {intent === 'warning' ? null : <ArrowTrendingRegular />}
        <Text size={200}>{detail}</Text>
      </div>
      <DataSourceBadge kind="sample" />
    </Card>
  )
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
  const [timeRange, setTimeRange] = useState<TimeRange>('24h')
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
  const metrics = metricCopy[timeRange]
  const chartPoints = useMemo(() => {
    const values = requestSeries[timeRange]
    const maximum = Math.max(...values)
    return values
      .map((value, index) => {
        const x = (index / (values.length - 1)) * 100
        const y = 92 - (value / maximum) * 78
        return `${x},${y}`
      })
      .join(' ')
  }, [timeRange])
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
        description="Monitor MOSAIC desired state and preview the operational experience planned for Azure Monitor telemetry."
        actions={
          <label className={styles.rangeControl}>
            <span>Time range</span>
            <Select
              value={timeRange}
              onChange={(event) => setTimeRange(event.target.value as TimeRange)}
            >
              <option value="24h">Last 24 hours</option>
              <option value="7d">Last 7 days</option>
              <option value="30d">Last 30 days</option>
            </Select>
          </label>
        }
      />

      <div className={styles.liveSection}>
        <div className={styles.sectionHeading}>
          <div>
            <Title2 as="h2">Desired-state inventory</Title2>
            <Text>Live records stored and managed by MOSAIC.</Text>
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

      <PreviewNotice>
        Telemetry, cost, model ranking, and service-health panels below use illustrative sample data.
        MOSAIC is not querying Azure Monitor yet.
      </PreviewNotice>

      <div className={styles.metricGrid}>
        <SparkMetric
          label="Total requests"
          value={metrics.requests}
          detail={`${metrics.trend} vs previous period`}
          icon={<ChartMultipleRegular />}
        />
        <SparkMetric
          label="Token consumption"
          value={metrics.tokens}
          detail="+1.1% vs previous period"
          icon={<CloudDatabaseRegular />}
        />
        <SparkMetric
          label="Estimated cost"
          value={metrics.cost}
          detail="Budget threshold at 82%"
          icon={<MoneyRegular />}
          intent="warning"
        />
      </div>

      <div className={styles.dashboardGrid}>
        <div className={`panel ${styles.volumePanel}`}>
          <div className="panel-header">
            <div>
              <Title3 as="h2">Request volume</Title3>
              <Text size={200}>Illustrative requests routed through APIM</Text>
            </div>
            <Badge appearance="outline">{timeRange}</Badge>
          </div>
          <div className={styles.chartArea}>
            <div className={styles.chartGrid} aria-hidden="true">
              <span />
              <span />
              <span />
              <span />
            </div>
            <svg
              className={styles.lineChart}
              viewBox="0 0 100 100"
              preserveAspectRatio="none"
              role="img"
              aria-label={`Sample request volume for ${timeRange}`}
            >
              <polyline className={styles.areaLine} points={`0,100 ${chartPoints} 100,100`} />
              <polyline className={styles.dataLine} points={chartPoints} />
            </svg>
          </div>
        </div>

        <div className={`panel ${styles.modelsPanel}`}>
          <div className="panel-header">
            <Title3 as="h2">Top models</Title3>
            <Button appearance="subtle" size="small" onClick={() => navigate('/analytics')}>
              View analytics
            </Button>
          </div>
          <div className={styles.modelList}>
            {topModels.map((model) => (
              <div key={model.name} className={styles.modelRow}>
                <div>
                  <code>{model.name}</code>
                  <span>{model.share}%</span>
                </div>
                <div className={styles.progressTrack}>
                  <span style={{ width: `${model.share}%` }} />
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className={`panel ${styles.healthPanel}`}>
          <div className="panel-header">
            <Title3 as="h2">System health</Title3>
            <DataSourceBadge kind="sample" />
          </div>
          <div className={styles.healthList}>
            <div>
              <span>Azure API Management</span>
              <Badge color="success">Healthy</Badge>
            </div>
            <div>
              <span>Cosmos DB desired state</span>
              <Badge color="success">99.99%</Badge>
            </div>
            <div>
              <span>Foundry East US 2</span>
              <Badge color="warning">Degraded</Badge>
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}
