import { Badge, Tooltip } from '@fluentui/react-components'
import { ShieldCheckmarkRegular } from '@fluentui/react-icons'
import type { BadgeProps } from '@fluentui/react-components'
import { environmentLabel, findEnvironment } from '../environments'
import type { EnvironmentCatalogView } from '../types'
import styles from './EnvironmentComponents.module.css'

export function EnvironmentBadge({
  environment,
  catalog,
  size = 'medium',
}: {
  environment: string | null
  catalog?: EnvironmentCatalogView
  size?: BadgeProps['size']
}) {
  const definition = findEnvironment(catalog, environment)
  const unknown = environment != null && !definition
  const label = environmentLabel(catalog, environment)
  const badge = (
    <Badge
      className={styles.badge}
      appearance={environment == null || unknown ? 'outline' : 'tint'}
      color={definition?.color ?? 'informative'}
      size={size}
    >
      <span className={styles.badgeContent}>
        {definition?.production && <ShieldCheckmarkRegular aria-label="Production-class" />}
        <span>{label}</span>
      </span>
    </Badge>
  )
  return unknown ? (
    <Tooltip content="Not defined in Settings → Environments" relationship="label">
      {badge}
    </Tooltip>
  ) : (
    badge
  )
}
