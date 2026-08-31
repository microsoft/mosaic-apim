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
      policyXml: '<policies><inbound /></policies>',
      contentSha256: 'policy-digest',
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
    expect(await screen.findByText('<policies><inbound /></policies>')).toBeVisible()
    await user.click(screen.getByRole('tab', { name: 'Metadata' }))
    expect(screen.getByText('policy-digest')).toBeVisible()
  })
})
