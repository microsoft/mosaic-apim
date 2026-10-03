import { describe, expect, it } from 'vitest'
import { poolsByDeployment } from './endpoint-pools'
import { anthropicPool, draftPool } from './test/pool-fixtures'
import type { EndpointPoolDeployment } from './types'

function deployment(deploymentName: string, drained = false): EndpointPoolDeployment {
  return {
    deploymentName,
    poolModelId: `poolmodel_${deploymentName}`,
    publicName: deploymentName,
    modelDisplayName: deploymentName,
    drained,
    warning: null,
  }
}

describe('poolsByDeployment', () => {
  it('lists each pool once per deployment, whatever case the pool typed its name in', () => {
    const pools = poolsByDeployment([
      {
        pool: anthropicPool,
        gatewayName: 'Contoso AI gateway',
        deployments: [deployment('gpt-4o', true), deployment('GPT-4o'), deployment('gpt-4o-mini', true)],
      },
      { pool: draftPool, gatewayName: null, deployments: [deployment('GPT-4O', true)] },
    ])

    expect([...pools.keys()]).toEqual(['gpt-4o', 'gpt-4o-mini'])
    // A pool has drained a deployment only when it has drained every member it has there.
    expect(pools.get('gpt-4o')).toEqual([
      { poolId: anthropicPool.id, displayName: 'Anthropic Claude', drained: false },
      { poolId: draftPool.id, displayName: 'OpenAI chat', drained: true },
    ])
    expect(pools.get('gpt-4o-mini')).toEqual([
      { poolId: anthropicPool.id, displayName: 'Anthropic Claude', drained: true },
    ])
  })

  it('is empty before the pools load', () => {
    expect(poolsByDeployment(undefined).size).toBe(0)
  })
})
