import { Badge, Tooltip } from '@fluentui/react-components'
import type { BadgeProps } from '@fluentui/react-components'
import { ShieldRegular } from '@fluentui/react-icons'
import { findEnvironment } from '../environments'
import type { AzureCloud, PortalEnvironment } from '../types'
import { CloudBadge } from './CloudBadge'

export interface EnvironmentBadgeProps {
  environment: string | null
  environments?: PortalEnvironment[]
  size?: BadgeProps['size']
  azureCloud?: AzureCloud
}

export function EnvironmentBadge({ environment, environments, size, azureCloud }: EnvironmentBadgeProps) {
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
    return <>{azureCloud && <CloudBadge cloud={azureCloud} />}
      <Tooltip content={unknownDescription} relationship="description">
        {badge}
      </Tooltip>
    </>
  }
  return <>{azureCloud && <CloudBadge cloud={azureCloud} />}{badge}</>
}
