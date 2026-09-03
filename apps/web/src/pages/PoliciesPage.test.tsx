import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PoliciesPage } from './PoliciesPage'

const apiMocks = vi.hoisted(() => ({
  previewPolicy: vi.fn(),
}))

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    previewPolicy: apiMocks.previewPolicy,
  }),
}))

describe('PoliciesPage', () => {
  beforeEach(() => {
    apiMocks.previewPolicy.mockReset()
    apiMocks.previewPolicy.mockResolvedValue({
      contentSha256: 'policy-digest',
      facets: [
        {
          kind: 'tokenLimit',
          element: 'llm-token-limit',
          section: 'inbound',
          summary:
            'Limits model usage to 12,000 tokens per minute, counted per subscription.',
          details: [],
          attributes: {},
          confidence: 'recognized',
          managedByMosaic: false,
        },
      ],
      unrecognizedElements: [],
      warnings: [],
    })
  })

  it('uses the live deterministic preview API without enabling APIM writes', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <PoliciesPage />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('button', { name: 'Apply to APIM' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))

    expect(apiMocks.previewPolicy.mock.calls[0]?.[0]).toEqual({
      backendResource: 'https://cognitiveservices.azure.com',
      enforcement: {
        counterKeyExpression: '@(context.Subscription.Id)',
        tokensPerMinute: 12000,
        tokenQuota: 500000,
        tokenQuotaPeriod: 'Monthly',
        estimatePromptTokens: true,
      },
    })
    expect(
      await screen.findByText(/Limits model usage to 12,000 tokens per minute/),
    ).toBeVisible()
    await user.click(screen.getByRole('tab', { name: 'Metadata' }))
    expect(screen.getByText('policy-digest')).toBeVisible()
  })

  it('describes the preview in plain language rather than showing markup', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({
      defaultOptions: { mutations: { retry: false } },
    })
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <PoliciesPage />
      </QueryClientProvider>,
    )

    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))
    await screen.findByText(/Limits model usage to 12,000 tokens per minute/)

    expect(container.textContent).not.toContain('<policies')
    expect(container.textContent).not.toContain('llm-token-limit')
    expect(screen.queryByRole('tab', { name: 'XML' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy XML' })).toBeNull()
  })
})
