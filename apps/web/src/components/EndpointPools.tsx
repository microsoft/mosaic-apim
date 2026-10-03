import {
  Card,
  MessageBar,
  MessageBarBody,
  MessageBarTitle,
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableHeaderCell,
  TableRow,
  Text,
  Title3,
} from '@fluentui/react-components'
import { Link } from 'react-router-dom'
import { type DeploymentPool, useEndpointPools } from '../endpoint-pools'
import type { ModelEndpoint } from '../types'
import { ErrorState } from './AsyncState'
import { PoolStatusBadge } from './PoolBadges'
import styles from './EndpointPools.module.css'

/** A deployment table's Pools cell. Empty until the pools load. */
export function DeploymentPoolsCell({
  deploymentName,
  pools,
}: {
  deploymentName: string
  pools: Map<string, DeploymentPool[]> | undefined
}) {
  if (!pools) return null
  const using = pools.get(deploymentName.toLowerCase()) ?? []
  if (using.length === 0) return <span className={styles.muted}>—</span>
  return (
    <div className={styles.cellStack}>
      {using.map((pool) => (
        <span key={pool.poolId}>
          <Link className={styles.poolLink} to={`/pools/${pool.poolId}`}>
            {pool.displayName}
          </Link>
          {pool.drained && <span className={styles.muted}> · drained</span>}
        </span>
      ))}
    </div>
  )
}

/**
 * The pools that send calls to deployments on an endpoint, and any deployment portal users would
 * see twice. Nothing shows while no pool uses the endpoint.
 */
export function EndpointPoolsCard({ endpoint, className }: { endpoint: ModelEndpoint; className?: string }) {
  const uses = useEndpointPools(endpoint.id)
  if (uses.isError) {
    return (
      <Card className={className}>
        <Title3 as="h2">Used by pools</Title3>
        <ErrorState title="MOSAIC couldn’t list the pools that use this endpoint" error={uses.error} />
      </Card>
    )
  }
  if (!uses.data?.length) return null
  const warnings = uses.data.flatMap((use) =>
    use.deployments.flatMap((deployment) =>
      deployment.warning
        ? [{ key: `${use.pool.id}:${deployment.poolModelId}:${deployment.deploymentName}`, text: deployment.warning }]
        : [],
    ),
  )
  return (
    <Card className={className}>
      <div className={styles.header}>
        <Title3 as="h2">Used by pools</Title3>
        <Text size={200} className={styles.muted}>
          These pools use deployments on {endpoint.name}. Removing the endpoint, or one of its
          deployments, also takes it out of these pools. If a pool’s gateway still routes to it,
          take it out of that pool and publish the pool again first.
        </Text>
      </div>
      {warnings.map((warning) => (
        <MessageBar key={warning.key} intent="warning">
          <MessageBarBody>
            <MessageBarTitle>Portal users would see a model twice</MessageBarTitle>
            {warning.text}
          </MessageBarBody>
        </MessageBar>
      ))}
      <Table aria-label={`Pools that use ${endpoint.name}`}>
        <TableHeader>
          <TableRow>
            <TableHeaderCell>Pool</TableHeaderCell>
            <TableHeaderCell>Gateway</TableHeaderCell>
            <TableHeaderCell>Deployments it uses</TableHeaderCell>
            <TableHeaderCell>Status</TableHeaderCell>
          </TableRow>
        </TableHeader>
        <TableBody>
          {uses.data.map((use) => (
            <TableRow key={use.pool.id}>
              <TableCell>
                <div className={styles.cellStack}>
                  <Link className={styles.poolLink} to={`/pools/${use.pool.id}`}>
                    {use.pool.displayName}
                  </Link>
                  {use.pool.visibility === 'hidden' && (
                    <span className={styles.muted}>Hidden from the portal catalog</span>
                  )}
                </div>
              </TableCell>
              <TableCell>{use.gatewayName ?? use.pool.gatewayId}</TableCell>
              <TableCell>
                <div className={styles.cellStack}>
                  {use.deployments.map((deployment) => (
                    <span key={`${deployment.poolModelId}:${deployment.deploymentName}`}>
                      {deployment.deploymentName} for {deployment.modelDisplayName}
                      {deployment.drained && <span className={styles.muted}> · drained</span>}
                    </span>
                  ))}
                </div>
              </TableCell>
              <TableCell>
                <PoolStatusBadge pool={use.pool} />
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </Card>
  )
}
