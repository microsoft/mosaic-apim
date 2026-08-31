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
})
