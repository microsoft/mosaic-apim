import type { ReactNode } from 'react'
import styles from './charts.module.css'

interface Point {
  label: string
  primary: number | null
  secondary?: number | null
}

const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 })

function peak(values: Array<number | null | undefined>): number {
  return Math.max(0, ...values.map((value) => value ?? 0))
}

function points(values: Array<number | null | undefined>, max: number): string {
  return values
    .map((value, index) => {
      if (value == null) return null
      const x = values.length === 1 ? 50 : (index / (values.length - 1)) * 100
      // Costs peak below 1, so scale to the real peak whenever there is one.
      const y = 94 - (value / (max > 0 ? max : 1)) * 82
      return `${x},${y}`
    })
    .filter(Boolean)
    .join(' ')
}

// Each line is scaled to its own peak, so requests stay readable beside millions of tokens.
export function TrendChart({
  title,
  points: chartPoints,
  primaryLabel,
  secondaryLabel,
  caption,
  primaryFormat,
  secondaryFormat,
}: {
  title: string
  points: Point[]
  primaryLabel: string
  secondaryLabel?: string
  caption?: ReactNode
  /** How to show a line's values, such as money. Counts are shown compactly. */
  primaryFormat?: (value: number) => string
  secondaryFormat?: (value: number) => string
}) {
  const primaryValues = chartPoints.map((point) => point.primary)
  const secondaryValues = chartPoints.map((point) => point.secondary)
  const primaryPeak = peak(primaryValues)
  const secondaryPeak = peak(secondaryValues)
  const primary = points(primaryValues, primaryPeak)
  const secondary = secondaryLabel ? points(secondaryValues, secondaryPeak) : ''
  const first = chartPoints[0]?.label
  const last = chartPoints.length > 1 ? chartPoints[chartPoints.length - 1].label : undefined
  return (
    <figure className={styles.figure}>
      <div className={styles.legend}>
        <span><i className={styles.swatch} />{primaryLabel}, peak {(primaryFormat ?? compact.format)(primaryPeak)}</span>
        {secondaryLabel && (
          <span><i className={`${styles.swatch} ${styles.swatchSecondary}`} />{secondaryLabel}, peak {(secondaryFormat ?? compact.format)(secondaryPeak)}</span>
        )}
      </div>
      <svg className={styles.chart} viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={title}>
        {[20, 40, 60, 80].map((y) => (
          <line key={y} className={styles.gridLine} x1="0" x2="100" y1={y} y2={y} />
        ))}
        {primary && <polyline className={styles.area} points={`0,100 ${primary} 100,100`} />}
        {primary && <polyline className={styles.line} points={primary} />}
        {secondary && <polyline className={styles.lineSecondary} points={secondary} />}
      </svg>
      {first && (
        <div className={styles.axis} aria-hidden="true">
          <span>{first}</span>
          {last && <span>{last}</span>}
        </div>
      )}
      <figcaption className={styles.caption}>
        {caption ?? (secondaryLabel ? 'Each line is scaled to its own peak.' : `${primaryLabel} across ${chartPoints.length} buckets.`)}
      </figcaption>
      <table className="sr-only">
        <caption>{title}</caption>
        <thead>
          <tr>
            <th>Bucket</th>
            <th>{primaryLabel}</th>
            {secondaryLabel && <th>{secondaryLabel}</th>}
          </tr>
        </thead>
        <tbody>
          {chartPoints.map((point) => (
            <tr key={point.label}>
              <td>{point.label}</td>
              <td>{point.primary == null ? 'No data' : primaryFormat ? primaryFormat(point.primary) : point.primary}</td>
              {secondaryLabel && <td>{point.secondary == null ? 'No data' : secondaryFormat ? secondaryFormat(point.secondary) : point.secondary}</td>}
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
