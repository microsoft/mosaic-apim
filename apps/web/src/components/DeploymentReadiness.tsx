import { Badge, Text } from '@fluentui/react-components'
import type { ReactNode } from 'react'
import { checkFreshness } from '../runtime-access'
import type { DeploymentAccess, ModelEndpoint } from '../types'
import styles from './DeploymentReadiness.module.css'

export function DeploymentIdentity({ endpoint }: { endpoint: ModelEndpoint }) {
  return <Text>
    Azure checks use <strong>{endpoint.azureConnectionId
      ? 'the identity for the selected Azure connection'
      : 'the MOSAIC host managed identity'}</strong>, not the API Management gateway identity.
  </Text>
}

function ReadinessRow({ label, status, children }: {
  label: string
  status: 'Verified' | 'Action needed' | 'Not verified' | 'Not supported'
  children: ReactNode
}) {
  const color = status === 'Verified' ? 'success' : status === 'Action needed' ? 'warning' : 'informative'
  return <div className={styles.row}>
    <dt>{label}</dt>
    <dd><Badge appearance="tint" color={color}>{status}</Badge></dd>
    <dd className={styles.details}>{children}</dd>
  </div>
}

export function DeploymentReadiness({ access, endpoint, quotaError }: {
  access: DeploymentAccess
  endpoint: ModelEndpoint
  quotaError?: string | null
}) {
  const account = access.scope?.split('/').filter(Boolean).at(-1)
  const quotaStatus = access.quotaStatus
  const fresh = checkFreshness(endpoint.access) === 'current'
  const inventoryReadable = fresh && endpoint.access.canRead
  const inventoryDenied = fresh && endpoint.access.evaluation !== 'notEvaluated' && !endpoint.access.canRead
  const canReadQuota = quotaStatus === 'read' && !quotaError
  return <dl className={styles.readiness}>
    <ReadinessRow label="Read model inventory"
      status={inventoryReadable ? 'Verified' : inventoryDenied ? 'Action needed' : 'Not verified'}>
      {inventoryReadable
        ? 'MOSAIC can list existing deployments. This does not grant permission to create them.'
        : 'MOSAIC does not have a current successful inventory check. Run Check access on the endpoint for details.'}
    </ReadinessRow>
    <ReadinessRow label="Create deployments"
      status={access.canDeploy ? 'Verified' : access.evaluation === 'denied' ? 'Action needed'
        : access.evaluation === 'unsupported' ? 'Not supported' : 'Not verified'}>
      {access.evaluation === 'denied'
        ? <>Ask an Azure administrator to grant the listed permissions on {account
          ? <>the <strong data-deployment-account>{account}</strong> Azure account</>
          : 'the Azure account'}, not on a Foundry project or gateway. MOSAIC does not assign Azure roles.</>
        : access.message}
      {access.missingActions.length > 0 && <ul className={styles.actionList}>
        {access.missingActions.map((action) => <li key={action}><code>{action}</code></li>)}
      </ul>}
    </ReadinessRow>
    {access.evaluation !== 'unsupported' && <ReadinessRow label="Read regional quota"
      status={canReadQuota ? 'Verified' : quotaStatus === 'denied' ? 'Action needed' : 'Not verified'}>
      {canReadQuota
        ? 'MOSAIC can read subscription quota. Available quota depends on the model and deployment type.'
        : quotaStatus === 'denied' || quotaStatus === 'empty'
          ? <>
            Ask an Azure administrator to {quotaStatus === 'denied' ? 'assign' : 'verify'}
            {' '}<strong>Cognitive Services Usages Reader</strong> at <strong>subscription scope</strong>.
            <Text block size={200} className={styles.note}>
              {quotaStatus === 'denied' ? 'Azure denied the quota read.' : 'Azure returned no quota records for this region.'}
              {' '}This does not mean quota is exhausted.
            </Text>
          </>
          : <>{quotaError || 'MOSAIC could not verify regional quota.'} Check again before deploying.</>}
    </ReadinessRow>}
  </dl>
}
