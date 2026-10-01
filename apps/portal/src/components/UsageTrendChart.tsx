import { Button } from '@fluentui/react-components'
import { useMemo, useState } from 'react'
import { environmentLabel, findEnvironment } from '../environments'
import { formatNumber, resourceLabel } from '../entitlement-format'
import { environmentSort, formatUtcDate, formatUtcHour } from '../usage-format'
import type {
  PortalEnvironment,
  UsageEnvironmentBreakdown,
  UsageHourPoint,
  UsageResourceRow,
  UsageTimelinePoint,
} from '../types'

type Metric = 'requests' | 'tokens'
type GroupBy = 'environment' | 'resource'

const colorTokens: Record<string, string> = {
  brand: 'var(--mosaic-primary)',
  danger: '#c50f1f',
  important: '#8764b8',
  informative: '#0078d4',
  severe: '#d83b01',
  subtle: '#605e5c',
  success: '#107c10',
  warning: '#ffaa44',
}

function colorFor(environment: string | null, environments?: PortalEnvironment[]) {
  const definition = findEnvironment(environments, environment)
  return colorTokens[definition?.color ?? 'subtle']
}

function keyFor(environment: string | null) {
  return environment ?? '__unclassified__'
}

function metricValue(point: UsageTimelinePoint, metric: Metric) {
  return metric === 'requests' ? point.requests : point.totalTokens
}

// Grants MOSAIC can't measure have no figures at all, so they get no line rather than a false zero.
function measuredPoints(points: UsageTimelinePoint[], metric: Metric) {
  return points.filter((point) => metricValue(point, metric) !== null)
}

function buildEnvironmentSeries(
  points: UsageTimelinePoint[],
  metric: Metric,
  environments?: PortalEnvironment[],
) {
  const dates = Array.from(new Set(points.map((point) => point.date))).sort()
  const measured = measuredPoints(points, metric)
  const environmentRows = new Map<string, UsageEnvironmentBreakdown>()
  for (const point of measured) {
    const key = keyFor(point.environment)
    if (!environmentRows.has(key)) {
      environmentRows.set(key, {
        environment: point.environment,
        resources: 0,
        requests: 0,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        estimatedCost: null,
        costExcludedResources: 0,
        unmeasuredResources: 0,
      })
    }
  }
  const orderedEnvironments = Array.from(environmentRows.values()).sort((a, b) =>
    environmentSort(environments, a, b),
  )
  const byDateAndEnvironment = new Map<string, number>()
  for (const point of measured) {
    const key = `${point.date}:${keyFor(point.environment)}`
    byDateAndEnvironment.set(key, (byDateAndEnvironment.get(key) ?? 0) + (metricValue(point, metric) ?? 0))
  }
  const series = orderedEnvironments.map((row) => ({
    key: keyFor(row.environment),
    label: environmentLabel(environments, row.environment),
    color: colorFor(row.environment, environments),
    points: dates.map((date) => byDateAndEnvironment.get(`${date}:${keyFor(row.environment)}`) ?? 0),
  }))
  return { dates, series }
}

function buildResourceSeries(points: UsageTimelinePoint[], rows: UsageResourceRow[], metric: Metric) {
  const dates = Array.from(new Set(points.map((point) => point.date))).sort()
  const measured = measuredPoints(points, metric)
  const rowMap = new Map(rows.map((row, index) => [row.entitlementId, { row, index }]))
  const ids = Array.from(new Set(measured.map((point) => point.entitlementId))).sort((a, b) => {
    const aRow = rowMap.get(a)
    const bRow = rowMap.get(b)
    if (aRow && bRow) return aRow.index - bRow.index
    return a.localeCompare(b)
  })
  const byDateAndResource = new Map<string, number>()
  for (const point of measured) {
    const key = `${point.date}:${point.entitlementId}`
    byDateAndResource.set(key, (byDateAndResource.get(key) ?? 0) + (metricValue(point, metric) ?? 0))
  }
  const colors = ['brand', 'important', 'informative', 'success', 'warning', 'danger', 'severe']
  return {
    dates,
    series: ids.map((id, index) => {
      const row = rowMap.get(id)?.row
      return {
        key: id,
        label: row ? resourceLabel(row.resource, row.resourceSummary) : id,
        color: colorTokens[colors[index % colors.length]],
        points: dates.map((date) => byDateAndResource.get(`${date}:${id}`) ?? 0),
      }
    }),
  }
}

function pointString(values: number[], max: number) {
  if (values.length === 0) return ''
  if (values.length === 1) {
    const y = max === 0 ? 90 : 90 - (values[0] / max) * 80
    return `50,${y}`
  }
  return values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * 100
      const y = max === 0 ? 90 : 90 - (value / max) * 80
      return `${x},${y}`
    })
    .join(' ')
}

const maxAxisLabels = 7

// Label every nth day, counting back from the latest, so labels never crowd or wrap.
function axisTicks(dates: string[]) {
  if (dates.length === 1) return [{ date: dates[0], position: 50 }]
  const step = Math.max(1, Math.ceil((dates.length - 1) / (maxAxisLabels - 1)))
  const ticks: { date: string; position: number }[] = []
  for (let index = dates.length - 1; index >= 0; index -= step) {
    ticks.unshift({ date: dates[index], position: (index / (dates.length - 1)) * 100 })
  }
  return ticks
}

function axisClass(position: number) {
  if (position === 0) return 'axis-start'
  if (position === 100) return 'axis-end'
  return undefined
}

export function UsageTrendChart({
  points,
  resources = [],
  environments,
}: {
  points: UsageTimelinePoint[]
  resources?: UsageResourceRow[]
  environments?: PortalEnvironment[]
}) {
  const [showTable, setShowTable] = useState(false)
  const [metric, setMetric] = useState<Metric>('requests')
  const [groupBy, setGroupBy] = useState<GroupBy>('environment')
  const { dates, series } = useMemo(
    () =>
      groupBy === 'environment'
        ? buildEnvironmentSeries(points, metric, environments)
        : buildResourceSeries(points, resources, metric),
    [environments, groupBy, metric, points, resources],
  )
  const max = Math.max(0, ...series.flatMap((item) => item.points))
  const metricLabel = metric === 'requests' ? 'requests' : 'tokens'
  const ariaLabel =
    series.length === 0
      ? `Daily ${metricLabel} trend with no usage`
      : `Daily ${metricLabel} trend for ${series.map((item) => item.label).join(', ')}`

  return (
    <section className="usage-section">
      <div className="section-header">
        <div>
          <h2>Daily trend</h2>
          <p>UTC days over the selected period, grouped by environment or resource.</p>
        </div>
        <div className="chart-actions">
          <label>
            <span>Metric</span>
            <select value={metric} onChange={(event) => setMetric(event.currentTarget.value as Metric)}>
              <option value="requests">Requests</option>
              <option value="tokens">Tokens</option>
            </select>
          </label>
          <label>
            <span>Group</span>
            <select value={groupBy} onChange={(event) => setGroupBy(event.currentTarget.value as GroupBy)}>
              <option value="environment">Environment</option>
              <option value="resource">Resource</option>
            </select>
          </label>
          <Button onClick={() => setShowTable((current) => !current)}>
            {showTable ? 'Show chart' : 'Show as table'}
          </Button>
        </div>
      </div>
      {showTable ? (
        <div className="table-scroll">
          <table aria-label={`Daily ${metricLabel} by ${groupBy}`}>
            <thead>
              <tr>
                <th scope="col">Date</th>
                {series.map((item) => (
                  <th key={item.key} scope="col">
                    {item.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {dates.map((date, index) => (
                <tr key={date}>
                  <th scope="row">{formatUtcDate(date)}</th>
                  {series.map((item) => (
                    <td key={item.key}>{formatNumber(item.points[index] ?? 0)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <>
          <div className="usage-chart">
            <div className="chart-grid" aria-hidden="true">
              <span />
              <span />
              <span />
              <span />
            </div>
            <svg viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={ariaLabel}>
              {series.map((item) => (
                <polyline
                  key={item.key}
                  points={pointString(item.points, max)}
                  fill="none"
                  stroke={item.color}
                  strokeWidth="2.5"
                  vectorEffect="non-scaling-stroke"
                />
              ))}
            </svg>
          </div>
          <div className="chart-axis" aria-hidden="true">
            {axisTicks(dates).map((tick) => (
              <span
                key={tick.date}
                className={axisClass(tick.position)}
                style={{ left: `${tick.position}%` }}
              >
                {formatUtcDate(tick.date)}
              </span>
            ))}
          </div>
          <ul className="chart-legend" aria-label="Trend legend">
            {series.map((item) => (
              <li key={item.key}>
                <span style={{ background: item.color }} aria-hidden="true" />
                {item.label}
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

export function RecentHoursChart({ points }: { points: UsageHourPoint[] }) {
  const [showTable, setShowTable] = useState(false)
  const max = Math.max(0, ...points.map((point) => point.requests ?? 0))
  const bars = points.map((point) => ({
    label: formatUtcHour(point.hour),
    requests: point.requests ?? 0,
    tokens: point.totalTokens ?? 0,
    throttled: point.throttled ?? 0,
    quotaRefused: point.quotaRefused ?? 0,
    errors: point.errors ?? 0,
  }))
  return (
    <section className="usage-section">
      <div className="section-header">
        <div>
          <h2>Last 24 hours</h2>
          <p>Measured hourly usage in UTC. Blank hours mean MOSAIC has no gateway data yet.</p>
        </div>
        <Button onClick={() => setShowTable((current) => !current)}>
          {showTable ? 'Show chart' : 'Show as table'}
        </Button>
      </div>
      {showTable ? (
        <div className="table-scroll">
          <table aria-label="Hourly usage for the last 24 hours">
            <thead>
              <tr>
                <th scope="col">Hour</th>
                <th scope="col">Requests</th>
                <th scope="col">Tokens</th>
                <th scope="col">Throttled</th>
                <th scope="col">Quota refused</th>
                <th scope="col">Errors</th>
              </tr>
            </thead>
            <tbody>
              {bars.map((bar) => (
                <tr key={bar.label}>
                  <th scope="row">{bar.label}</th>
                  <td>{formatNumber(bar.requests)}</td>
                  <td>{formatNumber(bar.tokens)}</td>
                  <td>{formatNumber(bar.throttled)}</td>
                  <td>{formatNumber(bar.quotaRefused)}</td>
                  <td>{formatNumber(bar.errors)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div
          className="hour-bars"
          role="img"
          aria-label="Hourly request chart for the last 24 hours in UTC"
        >
          {bars.map((bar) => (
            <span
              key={bar.label}
              title={`${bar.label}: ${formatNumber(bar.requests)} requests, ${formatNumber(
                bar.tokens,
              )} tokens`}
              style={{ height: `${max === 0 ? 2 : Math.max(2, (bar.requests / max) * 100)}%` }}
            />
          ))}
        </div>
      )}
    </section>
  )
}
