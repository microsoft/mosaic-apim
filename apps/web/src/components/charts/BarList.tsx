import styles from './charts.module.css'

export interface BarListItem {
  key: string
  label: string
  value: number
  valueLabel: string
  detail?: string
  tone?: 'normal' | 'warning' | 'danger'
}

export function BarList({ label, items }: { label: string; items: BarListItem[] }) {
  const max = Math.max(1, ...items.map((item) => item.value))
  return (
    <div className={styles.list} role="list" aria-label={label}>
      {items.length === 0 && <span className={styles.caption}>No rows in this view.</span>}
      {items.map((item) => (
        <div key={item.key} className={styles.barRow} role="listitem">
          <div className={styles.barHeader}>
            <strong>{item.label}</strong>
            <span>{item.valueLabel}</span>
          </div>
          {item.detail && <span className={styles.caption}>{item.detail}</span>}
          <div className={styles.track} aria-hidden="true">
            <span
              className={`${styles.fill} ${
                item.tone === 'danger'
                  ? styles.fillDanger
                  : item.tone === 'warning'
                    ? styles.fillWarning
                    : ''
              }`}
              style={{ width: `${Math.max(2, (item.value / max) * 100)}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}
