import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SupportPage } from './SupportPage'

describe('SupportPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('shows live health and creates only a local support draft', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const endpoint = String(input).endsWith('/readyz') ? 'ready' : 'ok'
      return new Response(JSON.stringify({ status: endpoint }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <SupportPage />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Both runtime checks completed successfully.')).toBeVisible()
    expect(fetchMock).toHaveBeenCalledTimes(2)

    await user.selectOptions(screen.getByLabelText('Category'), 'question')
    await user.selectOptions(screen.getByLabelText('Severity'), 'low')
    await user.type(screen.getByLabelText('Subject'), 'How do I preview a policy?')
    await user.type(screen.getByLabelText('Description'), 'Document the preview-only workflow.')
    await user.click(screen.getByRole('button', { name: 'Create local draft' }))

    expect(screen.getAllByText('LOCAL-SUPPORT-0001').length).toBeGreaterThan(0)
    expect(screen.getByText(/Nothing was sent to MOSAIC/i)).toBeVisible()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
