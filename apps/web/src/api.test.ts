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
    await result.current.listMyEntitlements()
    await result.current.getMyEntitlementConnection('grant_1')
    await result.current.diagnosePublicationRecovery('pub_1', 'run_1')
    await result.current.getPublicationLock('pub_1')
    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/publications/pub_1/model-api',
      '/api/v1/publications/pub_1',
      '/api/v1/entitlements/grant_1/connection',
      '/api/v1/me/entitlements',
      '/api/v1/me/entitlements/grant_1/connection',
      '/api/v1/publications/pub_1/recover',
      '/api/v1/publications/pub_1/lock',
    ])
    expect(fetchMock.mock.calls[0][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      governedAccess: { keysEnabled: false, entraEnabled: false },
    })
    expect(fetchMock.mock.calls[5][1].method).toBe('POST')
    expect(JSON.parse(fetchMock.mock.calls[5][1].body)).toEqual({
      runId: 'run_1', confirmQuiesced: false,
    })
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

  it('uses the environment catalog contract paths', async () => {
    const fetchMock = vi
      .fn()
      .mockImplementation(
        async () =>
          new Response(
            JSON.stringify({
              environments: [],
              requireClassification: false,
              unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
              compatibility: [],
              updatedAt: null,
              items: [],
              results: [],
              grantsCarried: 0,
              warnings: [],
            }),
            { status: 200, headers: { 'Content-Type': 'application/json' } },
          ),
      )
    vi.stubGlobal('fetch', fetchMock)
    const { result } = renderHook(() => useMosaicApi())
    await result.current.getEnvironmentCatalog()
    await result.current.createEnvironment({ key: 'prod2', displayName: 'Prod 2', color: 'danger' })
    await result.current.updateEnvironment('prod2', { displayName: 'Prod 2' })
    await result.current.deleteEnvironment('prod2')
    await result.current.updateEnvironmentSettings({ requireClassification: true })
    await result.current.listEnvironmentSuggestions()
    await result.current.assignEnvironments({
      assignments: [
        { resourceKind: 'gateway', resourceId: 'gateway_1', environment: 'production' },
      ],
    })
    await result.current.listEnvironmentFindings('gateway_1')

    expect(fetchMock.mock.calls.map(([url]) => String(url).replace(/^https?:\/\/[^/]+/, ''))).toEqual([
      '/api/v1/environment-catalog',
      '/api/v1/environment-catalog/environments',
      '/api/v1/environment-catalog/environments/prod2',
      '/api/v1/environment-catalog/environments/prod2',
      '/api/v1/environment-catalog/settings',
      '/api/v1/environment-suggestions',
      '/api/v1/environment-assignments',
      '/api/v1/environment-findings?gatewayId=gateway_1',
    ])
    expect(fetchMock.mock.calls.map(([, options]) => options.method ?? 'GET')).toEqual([
      'GET',
      'POST',
      'PATCH',
      'DELETE',
      'PATCH',
      'GET',
      'POST',
      'GET',
    ])
    expect(JSON.parse(String(fetchMock.mock.calls[6][1].body))).toEqual({
      assignments: [
        { resourceKind: 'gateway', resourceId: 'gateway_1', environment: 'production' },
      ],
    })
  })

})
