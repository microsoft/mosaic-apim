import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { AnalyticsPage } from './AnalyticsPage'
import type { EnvironmentCatalogView } from '../types'

const catalog: EnvironmentCatalogView = {
  environments: [
    {
      key: 'development',
      displayName: 'Development',
      description: null,
      color: 'brand',
      production: false,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 10,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
    {
      key: 'staging',
      displayName: 'Staging',
      description: null,
      color: 'warning',
      production: false,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 40,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
    {
      key: 'production',
      displayName: 'Production',
      description: null,
      color: 'danger',
      production: true,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 50,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    getEnvironmentCatalog: async () => catalog,
  }),
}))

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <AnalyticsPage />
    </QueryClientProvider>,
  )
}

describe('AnalyticsPage', () => {
  it('labels telemetry as sample data and supports error drill-in', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(screen.getAllByText('Sample data').length).toBeGreaterThan(0)
    expect(screen.getByRole('note')).toHaveTextContent('Azure Monitor')

    const viewButtons = screen.getAllByRole('button', { name: 'View' })
    await user.click(viewButtons[0])

    expect(screen.getByRole('dialog')).toBeVisible()
    expect(screen.getByText('Suggested follow-up')).toBeVisible()
  })

  it('filters by environment and shows the environment breakdown', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByRole('list', { name: 'Usage by environment' })).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Environment'), 'production')

    const breakdown = screen.getByRole('list', { name: 'Usage by environment' })
    expect(within(breakdown).getByText('Production')).toBeVisible()
    expect(within(breakdown).queryByText('Development')).not.toBeInTheDocument()
  })
})
