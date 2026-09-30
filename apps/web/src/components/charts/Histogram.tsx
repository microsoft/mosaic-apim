import type { AnalyticsLatencyBucket } from '../../types'
import styles from './charts.module.css'

function duration(ms: number) {
  return ms >= 1000 ? `${ms / 1000} s` : `${ms} ms`
}

// Buckets carry only their upper bound, so the open-ended last one is named from the bound before it.
function bucketLabel(buckets: AnalyticsLatencyBucket[], index: number) {
  const upper = buckets[index].upperMs
  if (upper != null) return `≤${duration(upper)}`
  const previous = index > 0 ? buckets[index - 1].upperMs : null
  return previous == null ? 'Any' : `>${duration(previous)}`
}

export function Histogram({
  buckets,
  label,
}: {
  buckets: AnalyticsLatencyBucket[]
  label: string
}) {
  const max = Math.max(1, ...buckets.map((bucket) => bucket.count))
  return (
    <figure className={styles.figure}>
      <div className={styles.histogram} role="img" aria-label={label}>
        {buckets.map((bucket, index) => (
          <div key={`${bucket.upperMs ?? 'inf'}-${index}`} className={styles.bucket}>
            <span
              className={styles.bucketBar}
              style={{ height: `${Math.max(2, (bucket.count / max) * 170)}px` }}
              title={`${bucket.count} calls`}
            />
            <span className={styles.bucketLabel}>{bucketLabel(buckets, index)}</span>
          </div>
        ))}
      </div>
      <figcaption className={styles.caption}>{label}</figcaption>
      <table className="sr-only">
        <caption>{label}</caption>
        <thead>
          <tr>
            <th>Latency</th>
            <th>Calls</th>
          </tr>
        </thead>
        <tbody>
          {buckets.map((bucket, index) => (
            <tr key={`${bucket.upperMs ?? 'inf'}-${index}`}>
              <td>{bucketLabel(buckets, index)}</td>
              <td>{bucket.count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
