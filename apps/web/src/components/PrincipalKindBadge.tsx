import { Badge } from '@fluentui/react-components'
import { PRINCIPAL_KIND_LABELS } from '../labels'
import type { PrincipalKind } from '../types'

export function PrincipalKindBadge({ kind }: { kind: PrincipalKind }) {
  return (
    <Badge appearance="tint">
      {PRINCIPAL_KIND_LABELS[kind]}
    </Badge>
  )
}
