import { renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, usePortalApi } from './api'

vi.mock('@azure/msal-react', () => ({
  useMsal: () => ({
    accounts: [],
    instance: {
      acquireTokenSilent: vi.fn(),
    },
  }),
}))

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function stubFetch(response: () => Response) {
  const fetchMock = vi.fn().mockImplementation(async () => response())
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('usePortalApi', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reads connection details from the owner-scoped API', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ entitlementId: 'grant/1', endpoint: 'https://gateway' }))
    const { result } = renderHook(() => usePortalApi())

    const connection = await result.current.getMyEntitlementConnection('grant/1')

    expect(connection.endpoint).toBe('https://gateway')
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://localhost:8000/api/v1/me/entitlements/grant%2F1/connection')
    expect(options.method).toBe('GET')
    expect(options.body).toBeUndefined()
  })

  it('reads MCP connection details from the owner-scoped API', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ entitlementId: 'grant/1', serverUrl: 'https://gateway/mcp/weather' }))
    const { result } = renderHook(() => usePortalApi())

    const connection = await result.current.getMcpConnection('grant/1')

    expect(connection.serverUrl).toBe('https://gateway/mcp/weather')
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://localhost:8000/api/v1/me/entitlements/grant%2F1/mcp-connection')
    expect(options.method).toBe('GET')
    expect(options.body).toBeUndefined()
  })

  it('reveals one key slot only when called, with no-store and the caller signal', async () => {
    const fetchMock = stubFetch(() => jsonResponse({
      entitlementId: 'grant_1',
      subscriptionName: 'grant-subscription',
      slot: 'secondary',
      key: 'test-only-key',
    }))
    const { result } = renderHook(() => usePortalApi())
    expect(fetchMock).not.toHaveBeenCalled()
    const controller = new AbortController()

    const revealed = await result.current.revealMyEntitlementKey('grant_1', 'secondary', controller.signal)

    expect(revealed.slot).toBe('secondary')
    expect(fetchMock).toHaveBeenCalledOnce()
    const [url, options] = fetchMock.mock.calls[0] as [string, RequestInit]
    expect(url).toBe('http://localhost:8000/api/v1/me/entitlements/grant_1/keys/reveal')
    expect(options).toMatchObject({ method: 'POST', cache: 'no-store', signal: controller.signal })
    expect(JSON.parse(String(options.body))).toEqual({ slot: 'secondary' })
    expect(new Headers(options.headers).get('Content-Type')).toBe('application/json')
  })

  it.each([
    [409, { code: 'conflict', message: 'This grant has not been applied to API Management' }, 'This grant has not been applied to API Management'],
    [401, { detail: 'Not authenticated' }, 'Not authenticated'],
    [422, { detail: [{ loc: ['body', 'slot'], msg: 'Input should be primary or secondary' }] }, 'Request failed with status 422'],
  ])('reports a %s answer with its most useful message', async (status, body, message) => {
    stubFetch(() => jsonResponse(body, status))
    const { result } = renderHook(() => usePortalApi())

    const failure = await result.current.revealMyEntitlementKey('grant_1', 'primary').catch((error: unknown) => error)

    expect(failure).toBeInstanceOf(ApiError)
    expect(failure).toMatchObject({ status, message, body })
  })

  it('reports a failure without a JSON body by its status', async () => {
    stubFetch(() => new Response('Bad gateway', { status: 502 }))
    const { result } = renderHook(() => usePortalApi())

    await expect(result.current.getMyEntitlementConnection('grant_1')).rejects.toMatchObject({
      status: 502,
      message: 'Request failed with status 502',
      body: undefined,
    })
  })
})
