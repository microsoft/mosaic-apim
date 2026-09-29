import { Button } from '@fluentui/react-components'
import { useMemo, useState } from 'react'
import { environmentLabel, findEnvironment } from '../environments'
import { formatNumber } from '../entitlement-format'
import { environmentSort, formatUtcDate } from '../usage-format'
import type { PortalEnvironment, UsageEnvironmentBreakdown, UsageTimelinePoint } from '../types'

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

function buildSeries(points: UsageTimelinePoint[], environments?: PortalEnvironment[]) {
  const dates = Array.from(new Set(points.map((point) => point.date))).sort()
  const environmentRows = new Map<string, UsageEnvironmentBreakdown>()
  for (const point of points) {
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
      })
    }
  }
  const orderedEnvironments = Array.from(environmentRows.values()).sort((a, b) =>
    environmentSort(environments, a, b),
  )
  const byDateAndEnvironment = new Map<string, number>()
  for (const point of points) {
    const key = `${point.date}:${keyFor(point.environment)}`
    byDateAndEnvironment.set(key, (byDateAndEnvironment.get(key) ?? 0) + (point.requests ?? 0))
  }
  const series = orderedEnvironments.map((row) => ({
    environment: row.environment,
    label: environmentLabel(environments, row.environment),
    color: colorFor(row.environment, environments),
    points: dates.map((date) => byDateAndEnvironment.get(`${date}:${keyFor(row.environment)}`) ?? 0),
  }))
  return { dates, series }
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

export function UsageTrendChart({
  points,
  environments,
}: {
  points: UsageTimelinePoint[]
  environments?: PortalEnvironment[]
}) {
  const [showTable, setShowTable] = useState(false)
  const { dates, series } = useMemo(() => buildSeries(points, environments), [points, environments])
  const max = Math.max(0, ...series.flatMap((item) => item.points))
  const ariaLabel =
    series.length === 0
      ? 'Daily request trend with no usage'
      : `Daily request trend for ${series.map((item) => item.label).join(', ')}`

  return (
    <section className="usage-section">
      <div className="section-header">
        <div>
          <h2>Daily trend</h2>
          <p>Requests per day by environment.</p>
        </div>
        <Button onClick={() => setShowTable((current) => !current)}>
          {showTable ? 'Show chart' : 'Show as table'}
        </Button>
      </div>
      {showTable ? (
        <div className="table-scroll">
          <table aria-label="Daily requests by environment">
            <thead>
              <tr>
                <th scope="col">Date</th>
                {series.map((item) => (
                  <th key={keyFor(item.environment)} scope="col">
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
                    <td key={keyFor(item.environment)}>{formatNumber(item.points[index] ?? 0)}</td>
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
                  key={keyFor(item.environment)}
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
            {dates.map((date) => (
              <span key={date}>{formatUtcDate(date)}</span>
            ))}
          </div>
          <ul className="chart-legend" aria-label="Trend legend">
            {series.map((item) => (
              <li key={keyFor(item.environment)}>
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
