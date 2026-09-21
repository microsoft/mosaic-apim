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
})
