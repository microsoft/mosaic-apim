import { Badge } from '@fluentui/react-components'
import type { AzureCloud } from '../types'
import { cloudLabel } from '../cloud-label'

export function CloudBadge({ cloud }: { cloud?: AzureCloud | null }) {
  return <Badge appearance="outline" color="informative" aria-label={cloudLabel(cloud)}
    title={cloudLabel(cloud)}>{cloud === 'government' ? 'AzGov' : 'Commercial'}</Badge>
}
