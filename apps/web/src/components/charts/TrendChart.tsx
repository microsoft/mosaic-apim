import { type ReactNode, useId } from 'react'
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

type Coordinate = [x: number, y: number]

function coordinates(values: Array<number | null | undefined>, max: number): Coordinate[] {
  return values.flatMap((value, index): Coordinate[] => {
    if (value == null) return []
    const x = values.length === 1 ? 50 : (index / (values.length - 1)) * 100
    // Costs peak below 1, so scale to the real peak whenever there is one.
    return [[x, 94 - (value / (max > 0 ? max : 1)) * 82]]
  })
}

// Eases between buckets without rising above or dipping below either neighbour, so the curve invents no peak.
function curve(line: Coordinate[]): string {
  return line
    .map(([x, y], index) => {
      if (index === 0) return `M${x} ${y}`
      const [previousX, previousY] = line[index - 1]
      const middle = (previousX + x) / 2
      return `C${middle} ${previousY} ${middle} ${y} ${x} ${y}`
    })
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
  const gradient = useId()
  const primaryLine = coordinates(primaryValues, primaryPeak)
  const primary = curve(primaryLine)
  const secondary = secondaryLabel ? curve(coordinates(secondaryValues, secondaryPeak)) : ''
  const latest = primaryLine.at(-1)
  const first = chartPoints[0]?.label
  const last = chartPoints.length > 1 ? chartPoints[chartPoints.length - 1].label : undefined
  // A line with no values has no peak. Saying "peak 0" would claim a value nobody measured.
  const legend = (label: string, values: Array<number | null | undefined>, top: number, format?: (value: number) => string) =>
    values.some((value) => value != null) ? `${label}, peak ${(format ?? compact.format)(top)}` : label
  return (
    <figure className={styles.figure}>
      <div className={styles.legend}>
        <span><i className={styles.swatch} />{legend(primaryLabel, primaryValues, primaryPeak, primaryFormat)}</span>
        {secondaryLabel && (
          <span><i className={`${styles.swatch} ${styles.swatchSecondary}`} />{legend(secondaryLabel, secondaryValues, secondaryPeak, secondaryFormat)}</span>
        )}
      </div>
      <div className={styles.plot}>
        <svg className={styles.chart} viewBox="0 0 100 100" preserveAspectRatio="none" role="img" aria-label={title}>
          <defs>
            <linearGradient id={gradient} x1="0" y1="0" x2="0" y2="1">
              <stop className={styles.areaStart} offset="0" />
              <stop className={styles.areaEnd} offset="1" />
            </linearGradient>
          </defs>
          {[20, 40, 60, 80].map((y) => (
            <line key={y} className={styles.gridLine} x1="0" x2="100" y1={y} y2={y} />
          ))}
          {primary && <path className={styles.area} fill={`url(#${gradient})`} d={`${primary} L${primaryLine.at(-1)![0]} 100 L${primaryLine[0][0]} 100 Z`} />}
          {secondary && <path className={styles.lineSecondary} d={secondary} />}
          {primary && <path className={styles.line} d={primary} />}
        </svg>
        {/* The latest bucket, marked in the accent. The SVG stretches, so the dot is positioned over it instead of drawn in it. */}
        {latest && <span className={styles.latest} style={{ left: `${latest[0]}%`, top: `${latest[1]}%` }} aria-hidden="true" />}
      </div>
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
