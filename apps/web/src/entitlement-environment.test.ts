import { describe, expect, it } from 'vitest'
import { entitlementResourceEnvironment } from './entitlement-environment'
import type { Gateway, ModelApi, ModelEndpoint } from './types'

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

  it('returns unclassified when the resource cannot be resolved', () => {
    expect(entitlementResourceEnvironment({ kind: 'mcpServer', id: 'missing' }, {})).toBeNull()
  })
})
