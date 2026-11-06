import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useMosaicApi } from './api'
import { centerCapabilities, costCenterCreationRequest, costCenterMembershipRequest, directoryMemberPage, memberOperation, readyCostCenter } from './test/cost-center-fixtures'
import type { CostCenterRequestApproval } from './types'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({ accounts: [], instance: { acquireTokenSilent: vi.fn() } }),
}))

afterEach(() => vi.unstubAllGlobals())

function respond(value: unknown, status = 200) {
  const fetch = vi.fn().mockImplementation(async () => new Response(JSON.stringify(value), {
    status, headers: { 'Content-Type': 'application/json' },
  }))
  vi.stubGlobal('fetch', fetch)
  return fetch
}

describe('cost-center management API contract', () => {
  it('updates self-service policy partially without resetting the default center', async () => {
    const fetch = respond({ defaultCostCenterId: 'general', creationRequestsEnabled: true, membershipRequestsEnabled: true })
    const { result } = renderHook(() => useMosaicApi())
    await result.current.updateCostCenterSettings({ membershipRequestsEnabled: true })
    expect(fetch.mock.calls[0][0]).toContain('/api/v1/cost-center-settings')
    expect(fetch.mock.calls[0][1].method).toBe('PUT')
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({ membershipRequestsEnabled: true })
  })

  it('uses distinct membership decision and durable resume contracts', async () => {
    const fetch = respond(costCenterMembershipRequest)
    const { result } = renderHook(() => useMosaicApi())
    await result.current.listCostCenterMembershipRequests('approved')
    await result.current.getCostCenterMembershipRequest('join/one')
    await result.current.approveCostCenterMembershipRequest('join/one', { note: 'Reviewed', acknowledgeMembershipChanges: true })
    await result.current.denyCostCenterMembershipRequest('join/one', { note: 'Not this team' })
    await result.current.resumeCostCenterMembershipRequest('join/one', { acknowledgeMembershipChanges: true })
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/cost-center-membership-requests?state=approved'),
      expect.stringContaining('/cost-center-membership-requests/join%2Fone'),
      expect.stringContaining('/cost-center-membership-requests/join%2Fone/approve'),
      expect.stringContaining('/cost-center-membership-requests/join%2Fone/deny'),
      expect.stringContaining('/cost-center-membership-requests/join%2Fone/resume'),
    ])
    expect(JSON.parse(fetch.mock.calls[2][1].body)).toEqual({ note: 'Reviewed', acknowledgeMembershipChanges: true })
    expect(JSON.parse(fetch.mock.calls[4][1].body)).toEqual({ acknowledgeMembershipChanges: true })
  })
  it('reads capabilities without a directory mutation or cached capability response', async () => {
    const fetch = respond(centerCapabilities)
    const { result } = renderHook(() => useMosaicApi())
    expect(await result.current.getCostCenterGroupCapabilities()).toEqual(centerCapabilities)
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/cost-centers/group-capabilities')
    expect(options.method ?? 'GET').toBe('GET')
    expect(options.cache).toBe('no-store')
  })

  it('encodes the exact center and opaque member cursor, with explicit page size and no-store reads', async () => {
    const fetch = respond(directoryMemberPage)
    const { result } = renderHook(() => useMosaicApi())
    expect(await result.current.listCostCenterDirectoryMembers('center/one', { pageSize: 100, cursor: 'opaque+/==' })).toEqual(directoryMemberPage)
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/cost-centers/center%2Fone/directory-members?')
    const params = new URL(url, 'http://localhost').searchParams
    expect(params.get('pageSize')).toBe('100')
    expect(params.get('cursor')).toBe('opaque+/==')
    expect(options.cache).toBe('no-store')
  })

  it.each([
    [202, 'unknown', false],
    [202, 'pending', false],
    [200, 'failed', false],
    [200, 'succeeded', true],
  ] as const)('preserves HTTP %i operation state %s rather than inferring success', async (status, state, directoryUpdated) => {
    const operation = { ...memberOperation, state, directoryUpdated }
    const fetch = respond(operation, status)
    const { result } = renderHook(() => useMosaicApi())
    expect(await result.current.addCostCenterDirectoryMember('center/one', 'member/one', 'servicePrincipal', 'same-member-intent')).toEqual(operation)
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/cost-centers/center%2Fone/directory-members/member%2Fone')
    expect(options.method).toBe('PUT')
    expect((options.headers as Headers).get('Idempotency-Key')).toBe('same-member-intent')
    expect(JSON.parse(String(options.body))).toEqual({ memberKind: 'servicePrincipal' })
  })

  it('sends the same supplied removal idempotency key on transport retries with no principal or roster payload', async () => {
    const fetch = respond({ ...memberOperation, kind: 'removeMember' })
    const { result } = renderHook(() => useMosaicApi())
    await result.current.removeCostCenterDirectoryMember('center/one', 'member/one', 'stable-removal')
    await result.current.removeCostCenterDirectoryMember('center/one', 'member/one', 'stable-removal')
    for (const [url, options] of fetch.mock.calls as [string, RequestInit][]) {
      expect(url).toContain('/api/v1/cost-centers/center%2Fone/directory-members/member%2Fone')
      expect(options.method).toBe('DELETE')
      expect(options.body).toBeUndefined()
      expect((options.headers as Headers).get('Idempotency-Key')).toBe('stable-removal')
    }
  })

  it.each([
    ['getCostCenterDirectoryOperation', '', 'GET'],
    ['retryCostCenterDirectoryOperation', '/retry', 'POST'],
    ['recoverCostCenterDirectoryOperation', '/recover', 'POST'],
  ] as const)('uses the durable, encoded operation identity for %s', async (method, suffix, verb) => {
    const fetch = respond(memberOperation)
    const { result } = renderHook(() => useMosaicApi())
    await result.current[method]('center/one', 'operation/one')
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toContain(`/api/v1/cost-centers/center%2Fone/directory-operations/operation%2Fone${suffix}`)
    expect(options.method ?? 'GET').toBe(verb)
    if (verb === 'GET') expect(options.cache).toBe('no-store')
  })

  it('retains exact resource scopes and allowances when saving center access intent', async () => {
    const fetch = respond(readyCostCenter)
    const { result } = renderHook(() => useMosaicApi())
    const resourceAccess = [
      { resource: { kind: 'poolModel' as const, id: 'same-model', scopeId: 'pool-a' }, mode: 'automatic' as const },
      { resource: { kind: 'poolModel' as const, id: 'same-model', scopeId: 'pool-b' }, mode: 'requestOnly' as const },
    ]
    await result.current.updateCostCenter('center/one', { resourceAccess })
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/cost-centers/center%2Fone')
    expect(options.method).toBe('PATCH')
    expect(JSON.parse(String(options.body))).toEqual({ resourceAccess })
    expect(fetch).toHaveBeenCalledOnce()
  })
})

describe('cost-center creation request API contract', () => {
  it('uses the authoritative combined count and the distinct request list/detail routes', async () => {
    const counts = { pendingResourceCount: 2, pendingCostCenterCount: 3, pendingCount: 5 }
    const fetch = respond(counts)
    const { result } = renderHook(() => useMosaicApi())
    expect(await result.current.getRequestCounts()).toEqual(counts)
    await result.current.listCostCenterRequests('pending')
    await result.current.getCostCenterRequest('request/one')
    expect(fetch.mock.calls.map(([url]) => url)).toEqual([
      expect.stringContaining('/api/v1/request-counts'),
      expect.stringContaining('/api/v1/cost-center-requests?state=pending'),
      expect.stringContaining('/api/v1/cost-center-requests/request%2Fone'),
    ])
  })

  it('resends the exact recorded approval without fabricating a resource or a second center identity', async () => {
    const fetch = respond(costCenterCreationRequest)
    const { result } = renderHook(() => useMosaicApi())
    const approval: CostCenterRequestApproval = {
      costCenter: {
        name: 'Reviewed research', code: 'research', owners: ['owner@example.com'], keysAllowed: false,
        groupSetup: { mode: 'attach', groupObjectId: '22222222-2222-4222-8222-222222222222', acknowledgeMembershipChanges: true },
        resourceAccess: [{ resource: { kind: 'poolModel', id: 'same-model', scopeId: 'pool-b' }, mode: 'automatic' }],
      },
      addRequester: true, note: 'Reviewed once',
    }
    await result.current.approveCostCenterRequest('request/one', approval)
    await result.current.approveCostCenterRequest('request/one', approval)
    for (const [url, options] of fetch.mock.calls as [string, RequestInit][]) {
      expect(url).toContain('/api/v1/cost-center-requests/request%2Fone/approve')
      expect(options.method).toBe('POST')
      expect(JSON.parse(String(options.body))).toEqual(approval)
    }
  })

  it('keeps denial on the cost-center request endpoint and sends an optional note', async () => {
    const fetch = respond({ ...costCenterCreationRequest, state: 'denied' })
    const { result } = renderHook(() => useMosaicApi())
    await result.current.denyCostCenterRequest('request/one', 'Not this quarter')
    const [url, options] = fetch.mock.calls[0] as [string, RequestInit]
    expect(url).toContain('/api/v1/cost-center-requests/request%2Fone/deny')
    expect(options.method).toBe('POST')
    expect(JSON.parse(String(options.body))).toEqual({ note: 'Not this quarter' })
  })
})
