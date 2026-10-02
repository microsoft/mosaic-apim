import { describe, expect, it } from 'vitest'
import { entitlementResourceEnvironment } from './entitlement-environment'
import type { Gateway, ModelApi, ModelEndpoint, ModelPool } from './types'

describe('entitlementResourceEnvironment', () => {
  it('derives model API environments from their gateway', () => {
    expect(entitlementResourceEnvironment(
      { kind: 'modelApi', id: 'api_1' },
      {
        modelApis: [{ id: 'api_1', gatewayId: 'gateway_1' } as ModelApi],
        gateways: [{ id: 'gateway_1', environment: 'production' } as Gateway],
      },
    )).toBe('production')
  })

  it('derives model deployment environments from the endpoint scope', () => {
    expect(entitlementResourceEnvironment(
      { kind: 'modelDeployment', id: 'gpt-4o', scopeId: 'endpoint_1' },
      { modelEndpoints: [{ id: 'endpoint_1', environment: 'development' } as ModelEndpoint] },
    )).toBe('development')
  })

  it("derives pool model environments from their pool's gateway, not the pool ID", () => {
    expect(entitlementResourceEnvironment(
      { kind: 'poolModel', id: 'pm_1', scopeId: 'pool_1' },
      {
        modelPools: [{ id: 'pool_1', gatewayId: 'gateway_1' } as ModelPool],
        gateways: [
          { id: 'gateway_1', environment: 'production' } as Gateway,
          { id: 'pool_1', environment: 'development' } as Gateway,
        ],
      },
    )).toBe('production')
    expect(entitlementResourceEnvironment(
      { kind: 'poolModel', id: 'pm_1', scopeId: 'missing' },
      { gateways: [{ id: 'missing', environment: 'development' } as Gateway] },
    )).toBeNull()
  })

  it('returns unclassified when the resource cannot be resolved', () => {
    expect(entitlementResourceEnvironment({ kind: 'mcpServer', id: 'missing' }, {})).toBeNull()
  })
})
