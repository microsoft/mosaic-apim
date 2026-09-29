import { Badge, Tooltip } from '@fluentui/react-components'
import type { BadgeProps } from '@fluentui/react-components'
import { ShieldRegular } from '@fluentui/react-icons'
import { findEnvironment } from '../environments'
import type { PortalEnvironment } from '../types'

export interface EnvironmentBadgeProps {
  environment: string | null
  environments?: PortalEnvironment[]
  size?: BadgeProps['size']
}

export function EnvironmentBadge({ environment, environments, size }: EnvironmentBadgeProps) {
  const definition = findEnvironment(environments, environment)
  const text = environment === null ? 'Unclassified' : definition?.displayName ?? environment
  // Only claim an environment is undefined once the list has loaded; while it loads, show the key.
  const unknownDescription =
    environment && environments && !definition ? 'Not defined by your administrator' : undefined
  const badge = (
    <Badge
      appearance={definition ? 'tint' : 'outline'}
      className="environment-badge"
      color={definition?.color ?? 'informative'}
      icon={definition?.production ? <ShieldRegular aria-hidden="true" /> : undefined}
      size={size}
      title={unknownDescription}
    >
      {text}
    </Badge>
  )

  if (unknownDescription) {
    return (
      <Tooltip content={unknownDescription} relationship="description">
        {badge}
      </Tooltip>
    )
  }
  return badge
}
