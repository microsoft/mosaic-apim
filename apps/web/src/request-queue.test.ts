import { describe, expect, it } from 'vitest'
import { adminRequestKey, adminRequestLink, combineAdminRequests } from './request-queue'
import { costCenterCreationRequest, costCenterMembershipRequest } from './test/cost-center-fixtures'
import type { AccessRequest } from './types'

const resource: AccessRequest = {
  id: costCenterCreationRequest.id, tenantId: costCenterCreationRequest.tenantId,
  requesterObjectId: costCenterCreationRequest.requesterObjectId,
  resource: { kind: 'poolModel', id: 'same-model', scopeId: 'pool-one' },
  state: 'pending', createdAt: costCenterCreationRequest.createdAt, updatedAt: costCenterCreationRequest.updatedAt,
}

describe('kind-aware administrative request queue', () => {
  it('keeps all three kinds independent even when raw IDs collide', () => {
    const membership = { ...costCenterMembershipRequest, id: resource.id }
    const queue = combineAdminRequests([resource], [costCenterCreationRequest], [membership])
    expect(queue).toHaveLength(3)
    expect(new Set(queue.map((item) => adminRequestKey(item.requestKind, item.request.id))).size).toBe(3)
    expect(queue.find((item) => item.requestKind === 'costCenterMembership')?.request).toBe(membership)
    expect(membership).not.toHaveProperty('resource')
    expect(adminRequestLink('costCenterMembership', 'join/one')).toBe('/entitlements?tab=requests&selected=join%2Fone&requestKind=costCenterMembership')
  })
  it('preserves two kinds with the same raw ID, without inventing a resource for center creation', () => {
    const queue = combineAdminRequests([resource], [costCenterCreationRequest])
    expect(queue).toHaveLength(2)
    expect(new Set(queue.map((item) => adminRequestKey(item.requestKind, item.request.id))).size).toBe(2)
    const center = queue.find((item) => item.requestKind === 'costCenter')
    expect(center?.request).toBe(costCenterCreationRequest)
    expect(center?.request).not.toHaveProperty('resource')
    expect(queue.find((item) => item.requestKind === 'resource')?.request).toBe(resource)
  })

  it('sorts the combined queue deterministically, newest first', () => {
    const newer = { ...costCenterCreationRequest, id: 'newer', createdAt: '2026-11-03T09:00:00Z' }
    const queue = combineAdminRequests([resource], [costCenterCreationRequest, newer])
    expect(queue[0].request.id).toBe('newer')
    expect(queue.map((item) => adminRequestKey(item.requestKind, item.request.id))).toEqual([
      'costCenter:newer', `costCenter:${resource.id}`, `resource:${resource.id}`,
    ])
  })

  it('retains legacy resource deep links and adds the kind only for center creation', () => {
    expect(adminRequestLink('resource', 'request/one')).toBe('/entitlements?tab=requests&selected=request%2Fone')
    expect(adminRequestLink('costCenter', 'request/one')).toBe('/entitlements?tab=requests&selected=request%2Fone&requestKind=costCenter')
    expect(adminRequestLink('resource')).toBe('/entitlements?tab=requests')
  })
})
