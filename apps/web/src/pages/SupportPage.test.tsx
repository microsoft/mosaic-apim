import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SupportPage } from './SupportPage'

vi.mock('../runtime-config', () => ({
  runtimeConfig: {
    apiBaseUrl: 'https://mosaic.example/api',
    authMode: 'entra',
    applicationInsightsConnectionString: 'InstrumentationKey=sensitive-test-value',
  },
}))

function renderSupport() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <SupportPage />
    </QueryClientProvider>,
  )
}

describe('SupportPage', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('retains documentation and anonymous runtime checks without ticket drafts', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const endpoint = String(input).endsWith('/readyz') ? 'ready' : 'ok'
      return new Response(JSON.stringify({ status: endpoint }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const user = userEvent.setup()
    renderSupport()

    expect(await screen.findByText('Both runtime checks completed successfully.')).toBeVisible()
    expect(fetchMock).toHaveBeenCalledTimes(2)
    for (const endpoint of ['healthz', 'readyz']) {
      expect(fetchMock).toHaveBeenCalledWith(`https://mosaic.example/api/${endpoint}`, {
        method: 'GET',
        headers: { Accept: 'text/plain' },
      })
    }
    const links = screen.getAllByRole('link', { name: 'Open Microsoft Learn' })
    expect(links).toHaveLength(3)
    for (const link of links) {
      expect(link).toHaveAttribute('href', expect.stringContaining('https://learn.microsoft.com/'))
      expect(link).toHaveAttribute('target', '_blank')
      expect(link).toHaveAttribute('rel', 'noreferrer')
    }
    expect(screen.getByText('Configured')).toBeVisible()
    expect(screen.queryByText(/sensitive-test-value/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Support ticket draft' })).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create local draft' })).not.toBeInTheDocument()
    expect(screen.queryByText(/LOCAL-SUPPORT/)).not.toBeInTheDocument()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'What do the system checks validate?' }))
    expect(screen.getByText(/anonymous GET requests/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Does the support form send a ticket anywhere?' })).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('shows pending diagnostics rather than claiming the runtime is healthy', () => {
    vi.stubGlobal('fetch', vi.fn(() => new Promise<Response>(() => {})))
    renderSupport()

    expect(screen.getByText('Checking /healthz and /readyz at runtime.')).toBeVisible()
    expect(screen.getAllByText('Loading')).toHaveLength(2)
    expect(screen.queryByText('Healthy')).not.toBeInTheDocument()
  })

  it('reports an actual readiness failure alongside successful liveness', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => (
      String(input).endsWith('/readyz')
        ? new Response('Database unavailable', { status: 503 })
        : new Response('ok', { status: 200 })
    ))
    vi.stubGlobal('fetch', fetchMock)
    renderSupport()

    expect(await screen.findByText(/one of the runtime checks is degraded or unavailable/)).toBeVisible()
    const readiness = screen.getByText('Readiness check (/readyz)').parentElement!.parentElement!
    expect(within(readiness).getByText('/readyz returned 503: Database unavailable')).toBeVisible()
    expect(within(readiness).getByText('Error')).toBeVisible()
    const health = screen.getByText('Liveness check (/healthz)').parentElement!.parentElement!
    expect(within(health).getByText('200 · ok')).toBeVisible()
    expect(within(health).getByText('Healthy')).toBeVisible()
    expect(screen.queryByText('Both runtime checks completed successfully.')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('reports unreachable endpoints without fabricated healthy fallback results', async () => {
    const fetchMock = vi.fn().mockRejectedValue(new Error('Network unavailable'))
    vi.stubGlobal('fetch', fetchMock)
    renderSupport()

    expect(await screen.findByText(/MOSAIC could not confirm runtime health/)).toBeVisible()
    expect(screen.getAllByText('Network unavailable')).toHaveLength(2)
    expect(screen.getAllByText('Error')).toHaveLength(2)
    expect(screen.queryByText('Healthy')).not.toBeInTheDocument()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})
