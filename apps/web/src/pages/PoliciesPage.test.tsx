import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
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
        <MemoryRouter><PoliciesPage /></MemoryRouter>
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
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
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
        <MemoryRouter><PoliciesPage /></MemoryRouter>
      </QueryClientProvider>,
    )

    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))
    await screen.findByText(/Limits model usage to 12,000 tokens per minute/)

    expect(container.textContent).not.toContain('<policies')
    expect(container.textContent).not.toContain('llm-token-limit')
    expect(screen.queryByRole('tab', { name: 'XML' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Copy XML' })).toBeNull()
  })

  it('opens enforcement preview for a removed sample-scopes link without sample records', () => {
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/policies?tab=samples']}>
          <PoliciesPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(screen.getByRole('button', { name: 'Enforcement preview' })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('button', { name: 'Generate Preview' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Sample scopes' })).not.toBeInTheDocument()
    expect(screen.queryByText(/Shared chat completions|Embeddings burst cap|Finance annual quota/)).not.toBeInTheDocument()
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    expect(apiMocks.previewPolicy).not.toHaveBeenCalled()
  })

  it('retains the generated request metadata and warns when the form changes', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><MemoryRouter><PoliciesPage /></MemoryRouter></QueryClientProvider>)
    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))
    await screen.findByText(/Limits model usage to 12,000 tokens per minute/)
    const backend = screen.getByRole('textbox', { name: 'Backend resource' })
    await user.clear(backend)
    await user.type(backend, 'https://example.com')
    expect(screen.getByText(/The form has changed/)).toBeVisible()
    await user.click(screen.getByRole('tab', { name: 'Metadata' }))
    expect(screen.getByText('https://cognitiveservices.azure.com')).toBeVisible()
    expect(apiMocks.previewPolicy).toHaveBeenCalledTimes(1)
  })

  it('shows preview API failures without generating fallback output', async () => {
    apiMocks.previewPolicy.mockRejectedValue(new Error('Preview unavailable'))
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><MemoryRouter><PoliciesPage /></MemoryRouter></QueryClientProvider>)

    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))

    expect(await screen.findByText('Preview unavailable')).toBeVisible()
    expect(screen.queryByText(/Limits model usage to 12,000 tokens per minute/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Copy digest' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Apply to APIM' })).toBeDisabled()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
  })

  it('retains warnings returned by the preview API', async () => {
    apiMocks.previewPolicy.mockResolvedValue({
      contentSha256: 'warning-digest',
      facets: [],
      unrecognizedElements: [],
      warnings: ['Token estimation depends on the backend model.'],
    })
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><MemoryRouter><PoliciesPage /></MemoryRouter></QueryClientProvider>)
    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))

    expect(await screen.findByText('Token estimation depends on the backend model.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Apply to APIM' })).toBeDisabled()
  })

  it.each([
    { field: 'period', message: 'Choose a quota period when token quota is set.' },
    { field: 'quota', message: 'Token quota is required when quota period is selected.' },
  ])('requires a matching quota and period when clearing $field', async ({ field, message }) => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><MemoryRouter><PoliciesPage /></MemoryRouter></QueryClientProvider>)
    if (field === 'period') {
      await user.selectOptions(screen.getByRole('combobox', { name: 'Quota period' }), '')
    } else {
      await user.clear(screen.getByRole('spinbutton', { name: 'Token quota' }))
    }
    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))

    expect(screen.getAllByText(message).length).toBeGreaterThan(0)
    expect(apiMocks.previewPolicy).not.toHaveBeenCalled()
  })

  it('requires backend and counter-key fields before requesting a preview', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><MemoryRouter><PoliciesPage /></MemoryRouter></QueryClientProvider>)
    const backend = screen.getByRole('textbox', { name: 'Backend resource' })
    const counterKey = screen.getByRole('textbox', { name: 'Counter key expression' })
    await user.clear(backend)
    await user.clear(counterKey)
    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))

    expect(backend).toBeRequired()
    expect(backend).toBeInvalid()
    expect(counterKey).toBeRequired()
    expect(counterKey).toBeInvalid()
    expect(apiMocks.previewPolicy).not.toHaveBeenCalled()
  })

  it('requires a token rate or quota before requesting a preview', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({ defaultOptions: { mutations: { retry: false } } })
    render(<QueryClientProvider client={queryClient}><MemoryRouter><PoliciesPage /></MemoryRouter></QueryClientProvider>)
    await user.clear(screen.getByRole('spinbutton', { name: 'Tokens per minute' }))
    await user.clear(screen.getByRole('spinbutton', { name: 'Token quota' }))
    await user.selectOptions(screen.getByRole('combobox', { name: 'Quota period' }), '')
    await user.click(screen.getByRole('button', { name: 'Generate Preview' }))

    expect(screen.getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('spinbutton', { name: 'Token quota' })).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getAllByText('Configure at least one token rate or quota before generating a preview.').length).toBeGreaterThan(0)
    expect(apiMocks.previewPolicy).not.toHaveBeenCalled()
  })
})
