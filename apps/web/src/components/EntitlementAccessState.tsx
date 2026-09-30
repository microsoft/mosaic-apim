import { Badge, Text } from '@fluentui/react-components'
import { describeAccessMethods } from '../entitlement-limits'
import type { Entitlement, EntitlementRuntime } from '../types'
import styles from '../pages/EntitlementsPage.module.css'

const statusLabels: Record<EntitlementRuntime['status'], string> = {
  pending: 'Saved, not applied',
  applying: 'Applying — not confirmed',
  applied: 'Applied',
  revocationPending: 'Revocation pending',
  revoked: 'Revoked in last apply',
  failed: 'Apply failed',
  unknown: 'Runtime unknown',
}

export function EntitlementAccessState({ entitlement }: { entitlement: Entitlement }) {
  const runtime = entitlement.runtime
  const recordedMcp = entitlement.resource.kind === 'mcpServer' && !runtime
  return (
    <div className={styles.cellStack}>
      <Text size={200}>Desired: {entitlement.enabled ? 'enabled' : 'disabled'}</Text>
      <Badge appearance="tint">
        {runtime ? statusLabels[runtime.status] : recordedMcp ? 'Recorded, not enforced' : 'Desired state only'}
      </Badge>
      {!runtime && (
        <Text size={200}>
          {recordedMcp
            ? "MOSAIC records this grant but doesn't enforce it for an imported MCP server."
            : 'No runtime orchestration. APIM is unchanged by this grant.'}
        </Text>
      )}
      {runtime?.appliedMethods && (
        <Text size={200}>Last applied methods: {describeAccessMethods(runtime.appliedMethods)}</Text>
      )}
      {runtime?.status === 'pending' && runtime.appliedMethods && (
        <Text size={200}>An earlier configuration was applied; saved changes are pending.</Text>
      )}
      {runtime?.status === 'revocationPending' && (
        <Text size={200}>Access may still work until the model plan is explicitly applied and propagates.</Text>
      )}
      {(runtime?.status === 'failed' || runtime?.status === 'unknown') && (
        <Text size={200}>Do not assume access or revocation succeeded.</Text>
      )}
      {runtime?.status === 'unknown' && (
        <Text size={200}>Interrupted applies can retain the model lock. Before confirming recovery, an operator must stop the original worker and verify that all submitted ARM operations are terminal.</Text>
      )}
      {runtime?.error && <Text size={200}>{runtime.error}</Text>}
    </div>
  )
}
