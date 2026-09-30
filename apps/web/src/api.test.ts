import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useMosaicApi } from './api'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({
    accounts: [],
    instance: {
      acquireTokenSilent: vi.fn(),
    },
  }),
}))

describe('useMosaicApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends the policy preview contract to the live API', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          policyXml: '<policies />',
          contentSha256: 'digest',
          warnings: [],
        }),
        {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        },
      ),
    )
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    const preview = await result.current.previewPolicy({
      backendResource: 'https://cognitiveservices.azure.com',
      enforcement: {
        counterKeyExpression: '@(context.Subscription.Id)',
        tokensPerMinute: 1_000,
        estimatePromptTokens: true,
      },
    })

    expect(preview.contentSha256).toBe('digest')
    expect(fetchMock).toHaveBeenCalledOnce()
    const [, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(options.method).toBe('POST')
    expect(JSON.parse(String(options.body))).toEqual({
      backendResource: 'https://cognitiveservices.azure.com',
      enforcement: {
        counterKeyExpression: '@(context.Subscription.Id)',
        tokensPerMinute: 1000,
        estimatePromptTokens: true,
      },
    })
  })

  it.each(['admin', 'self'] as const)('fetches each %s key only on explicit invocation with no-store', async (caller) => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({
      entitlementId: 'grant_1', subscriptionName: 'dedicated-sub', slot: 'primary', key: 'test-only-key',
    }), { status: 200, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())
    expect(fetchMock).not.toHaveBeenCalled()
    const controller = new AbortController()
    const reveal = caller === 'admin' ? result.current.revealEntitlementKey : result.current.revealMyEntitlementKey
    await reveal('grant_1', 'primary', controller.signal)
    await reveal('grant_1', 'secondary', controller.signal)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    const [url, options] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(url).toContain(`/api/v1/${caller === 'self' ? 'me/' : ''}entitlements/grant_1/keys/reveal`)
    expect(options).toMatchObject({ method: 'POST', cache: 'no-store', signal: controller.signal })
    expect(JSON.parse(String(options.body))).toEqual({ slot: 'secondary' })
  })

  it('sends link, settings, connection and future self-service calls to their existing API prefix', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response('{}', {
      status: 200, headers: { 'Content-Type': 'application/json' },
    }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())
    await result.current.linkPublicationModelApi('pub_1')
    await result.current.updatePublication('pub_1', { governedAccess: { keysEnabled: false, entraEnabled: false } })
    await result.current.getEntitlementConnection('grant_1')
    await result.current.getMcpConnection('grant_1')
    await result.current.listMyEntitlements()
    await result.current.getMyEntitlementConnection('grant_1')
    await result.current.diagnosePublicationRecovery('pub_1', 'run_1')
    await result.current.getPublicationLock('pub_1')
    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/publications/pub_1/model-api',
      '/api/v1/publications/pub_1',
      '/api/v1/entitlements/grant_1/connection',
      '/api/v1/entitlements/grant_1/mcp-connection',
      '/api/v1/me/entitlements',
      '/api/v1/me/entitlements/grant_1/connection',
      '/api/v1/publications/pub_1/recover',
      '/api/v1/publications/pub_1/lock',
    ])
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      governedAccess: { keysEnabled: false, entraEnabled: false },
    })
    expect(fetchMock.mock.calls[6][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[6][1].body)).toEqual({
      runId: 'run_1', confirmQuiesced: false,
    })

  })

  it('calls the MCP publication endpoints with the contracted paths and shapes', async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(
      JSON.stringify({ id: 'ok', supported: true, reasons: [], warnings: [] }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.getMcpPublishingCapability('gateway 1')
    await result.current.listMcpPublications('gateway 1')
    await result.current.createMcpPublication({ gatewayId: 'gateway_1', mcpEndpointId: 'endpoint_1' })
    await result.current.getMcpPublication('mcp pub')
    await result.current.updateMcpPublication('mcp pub', { displayName: 'Tools' })
    await result.current.deleteMcpPublication('mcp pub')
    await result.current.planMcpPublication('mcp pub')
    await result.current.applyMcpPublication('mcp pub', 'plan 1')
    await result.current.unpublishMcpPublication('mcp pub')
    await result.current.listMcpPublishRuns('mcp pub')
    await result.current.getMcpPublishRun('mcp pub', 'run 1')
    await result.current.recoverMcpPublication('mcp pub', { runId: 'run 1', confirmQuiesced: false })
    await result.current.getMcpPublicationLock('mcp pub')
    await result.current.getMcpPublishPlan('plan 1')

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/gateways/gateway%201/mcp-publishing',
      '/api/v1/mcp-publications?gateway=gateway%201',
      '/api/v1/mcp-publications',
      '/api/v1/mcp-publications/mcp%20pub',
      '/api/v1/mcp-publications/mcp%20pub',
      '/api/v1/mcp-publications/mcp%20pub',
      '/api/v1/mcp-publications/mcp%20pub/plan',
      '/api/v1/mcp-publications/mcp%20pub/apply?plan=plan%201',
      '/api/v1/mcp-publications/mcp%20pub/unpublish',
      '/api/v1/mcp-publications/mcp%20pub/runs',
      '/api/v1/mcp-publications/mcp%20pub/runs/run%201',
      '/api/v1/mcp-publications/mcp%20pub/recover',
      '/api/v1/mcp-publications/mcp%20pub/lock',
      '/api/v1/mcp-publish-plans/plan%201',
    ])
    expect(fetchMock.mock.calls[2][1]).toMatchObject({ method: 'POST' })
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({
      gatewayId: 'gateway_1',
      mcpEndpointId: 'endpoint_1',
    })
    expect(fetchMock.mock.calls[4][1]).toMatchObject({ method: 'PATCH' })
    expect(JSON.parse(fetchMock.mock.calls[11][1].body)).toEqual({
      runId: 'run 1',
      confirmQuiesced: false,
    })
  })

  it('calls the directory and overlap endpoints with the contracted query strings', async () => {
    const fetchMock = vi.fn().mockImplementation(async (url: string) => new Response(
      url.includes('/directory/status')
        ? JSON.stringify({ lookupEnabled: true, groupClaimsEnabled: true })
        : url.includes('/directory/search')
          ? JSON.stringify([])
          : url.includes('/directory/objects')
            ? JSON.stringify({ objectId: 'object-1', kind: 'user' })
            : url.includes('/members')
              ? JSON.stringify({ groupObjectId: 'group-object', members: [], truncated: false })
              : JSON.stringify({ overlaps: [], membershipChecked: true, skipped: [], generatedAt: '2026-09-01T12:00:00Z' }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    ))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    await result.current.getDirectoryStatus()
    await result.current.searchDirectory('agent', 'Ada Agent', 10)
    await result.current.getDirectoryObject('object-1')
    await result.current.listPrincipalMembers('principal-group', 50)
    await result.current.getGrantOverlaps({ resource: 'modelApi_1' })

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/directory/status',
      '/api/v1/directory/search?kind=agent&q=Ada%20Agent&limit=10',
      '/api/v1/directory/objects/object-1',
      '/api/v1/principals/principal-group/members?limit=50',
      '/api/v1/entitlements/overlaps?resource=modelApi_1',
    ])
  })

  it('sends only the management mode when switching a gateway, and surfaces a refusal', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ id: 'gateway_1', managementMode: 'manage' }), {
        status: 200, headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({
        code: 'validation_error',
        message: 'MOSAIC cannot write to this gateway yet, so it cannot be managed.',
        details: { managementMode: 'manage', missingActions: [] },
      }), { status: 422, headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())

    const updated = await result.current.updateGateway('gateway_1', { managementMode: 'manage' })

    expect(updated.managementMode).toBe('manage')
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(String(url).replace(/^https?:\/\/[^/]+/, '')).toBe('/api/v1/gateways/gateway_1')
    expect(options.method).toBe('PATCH')
    expect(new Headers(options.headers).get('Content-Type')).toBe('application/json')
    expect(JSON.parse(String(options.body))).toEqual({ managementMode: 'manage' })

    const refusal = result.current.updateGateway('gateway_1', { managementMode: 'manage' })
    await expect(refusal).rejects.toMatchObject({
      status: 422,
      message: 'MOSAIC cannot write to this gateway yet, so it cannot be managed.',
    })
  })
})
