import { useQuery } from '@tanstack/react-query'
import { useMosaicApi } from './api'
import type { EndpointPoolUse } from './types'

/** The pools with members on an endpoint. Every view of one endpoint shares the query. */
export function useEndpointPools(endpointId: string | undefined) {
  const api = useMosaicApi()
  return useQuery({
    queryKey: ['model-pools', 'endpoint', endpointId],
    queryFn: () => api.listEndpointPools(endpointId!),
    enabled: Boolean(endpointId),
  })
}

/** A pool that sends calls to one deployment. */
export interface DeploymentPool {
  poolId: string
  displayName: string
  /** Whether the pool has drained every member it has on the deployment. */
  drained: boolean
}

/** The pools that use each deployment, keyed by the deployment's name in lower case. */
export function poolsByDeployment(uses: EndpointPoolUse[] | undefined): Map<string, DeploymentPool[]> {
  const byDeployment = new Map<string, DeploymentPool[]>()
  for (const use of uses ?? []) {
    for (const deployment of use.deployments) {
      const key = deployment.deploymentName.toLowerCase()
      const pools = byDeployment.get(key) ?? []
      const listed = pools.find((pool) => pool.poolId === use.pool.id)
      if (listed) {
        listed.drained = listed.drained && deployment.drained
      } else {
        pools.push({ poolId: use.pool.id, displayName: use.pool.displayName, drained: deployment.drained })
      }
      byDeployment.set(key, pools)
    }
  }
  return byDeployment
}
