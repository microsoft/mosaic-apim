import { Badge } from '@fluentui/react-components'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { PrincipalKind } from '../types'
import styles from './PrincipalKindBadge.module.css'

export function PrincipalKindBadge({ kind }: { kind: PrincipalKind }) {
  return (
    <Badge appearance="tint" className={styles.badge}>
      {PRINCIPAL_KIND_LABELS[kind]}
    </Badge>
  )
}
