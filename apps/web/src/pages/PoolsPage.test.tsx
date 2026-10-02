import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  anthropicPool,
  draftPool,
  governedAnthropicPool,
  observedGateway,
  poolCandidates,
  poolGateway,
  poolSummaries,
} from '../test/pool-fixtures'
import type { ModelPoolSummary } from '../types'
import { PoolsPage } from './PoolsPage'

const api = {
  getEnvironmentCatalog: vi.fn(),
  listModelPoolSummaries: vi.fn(),
  listGateways: vi.fn(),
  getPoolCandidates: vi.fn(),
  createModelPool: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

function PoolProbe() {
  const { poolId } = useParams()
  const location = useLocation()
  return (
    <p>
      Opened {poolId} with {JSON.stringify(location.state)}
    </p>
  )
}

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <FluentProvider theme={webLightTheme}>
        <MemoryRouter initialEntries={['/pools']}>
          <Routes>
            <Route path="/pools" element={<PoolsPage />} />
            <Route path="/pools/:poolId" element={<PoolProbe />} />
            <Route path="/gateways" element={<p>Gateways</p>} />
          </Routes>
        </MemoryRouter>
      </FluentProvider>
    </QueryClientProvider>,
  )
}

describe('PoolsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.listModelPoolSummaries.mockResolvedValue(poolSummaries)
    api.listGateways.mockResolvedValue([poolGateway])
    api.getPoolCandidates.mockResolvedValue(poolCandidates)
  })

  it('lists each pool with what it serves, how it routes, and whether it’s ready', async () => {
    renderPage()

    const table = await screen.findByRole('table', { name: 'Model pools' })
    expect(screen.getByRole('heading', { name: 'Model pools', level: 1 })).toBeVisible()
    expect(screen.getByText('2 of 2 pools')).toBeVisible()
    const [, published, draft] = within(table).getAllByRole('row')

    expect(within(published).getByRole('link', { name: 'Anthropic Claude' })).toHaveAttribute(
      'href',
      `/pools/${anthropicPool.id}`,
    )
    expect(within(published).getByText('/mosaic/pool-anthropic-claude')).toBeVisible()
    expect(within(published).getByText('Contoso AI gateway')).toBeVisible()
    expect(within(published).getByText('Anthropic · Anthropic Messages')).toBeVisible()
    expect(within(published).getByText('1 model')).toBeVisible()
    expect(within(published).getByText('Breaker')).toBeVisible()
    expect(within(published).getByText('1 provisioned · 1 pay-as-you-go')).toBeVisible()
    expect(within(published).getByText('All ready')).toBeVisible()
    expect(within(published).getByText('1 warning')).toBeVisible()
    expect(within(published).getByText('Published')).toBeVisible()
    expect(within(published).getByText('Shared key')).toBeVisible()

    expect(within(draft).getByRole('link', { name: 'OpenAI chat' })).toHaveAttribute('href', `/pools/${draftPool.id}`)
    expect(within(draft).getByText('No models yet')).toBeVisible()
    expect(within(draft).getByText('Linear')).toBeVisible()
    expect(within(draft).getByText('1 problem to fix')).toBeVisible()
    expect(within(draft).getByText('Draft')).toBeVisible()

    expect(screen.queryByRole('combobox', { name: /gateway/i })).not.toBeInTheDocument()
  })

  it('marks a pool whose saved changes the gateway doesn’t run yet', async () => {
    api.listModelPoolSummaries.mockResolvedValue([{ ...poolSummaries[0], unappliedChanges: true }])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Model pools' })
    expect(within(table).getByText('Changes not applied')).toBeVisible()
  })

  it('says which pools give each caller their own grant, and how many are in force', async () => {
    api.listModelPoolSummaries.mockResolvedValue([
      { ...poolSummaries[0], pool: governedAnthropicPool },
      {
        ...poolSummaries[1],
        pool: { ...draftPool, governedAccess: { keysEnabled: true, entraEnabled: false }, accessState: 'pending' },
      },
    ])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Model pools' })
    const [, governed, pending] = within(table).getAllByRole('row')
    expect(within(governed).getByText('Governed')).toBeVisible()
    expect(within(governed).getByText('2 grants in force')).toBeVisible()
    expect(within(pending).getByText('Governed, not applied yet')).toBeVisible()
    expect(within(pending).getByText('0 grants in force')).toBeVisible()
  })

  it('explains pools when there are none yet', async () => {
    api.listModelPoolSummaries.mockResolvedValue([])
    renderPage()

    expect(await screen.findByText('No pools yet')).toBeVisible()
    expect(screen.queryByRole('table', { name: 'Model pools' })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create pool' })).toBeEnabled())
  })

  it('sends people to onboard a gateway before they can create a pool', async () => {
    api.listModelPoolSummaries.mockResolvedValue([])
    api.listGateways.mockResolvedValue([])
    renderPage()

    expect(await screen.findByRole('link', { name: 'Onboard a gateway' })).toHaveAttribute('href', '/gateways')
    expect(screen.getByRole('button', { name: 'Create pool' })).toBeDisabled()
  })

  it('filters pools by gateway when they run on more than one', async () => {
    const user = userEvent.setup()
    const observed: ModelPoolSummary = {
      ...poolSummaries[1],
      pool: { ...draftPool, id: 'modelpool_fabrikam', displayName: 'Fabrikam models', gatewayId: observedGateway.id },
      gatewayName: observedGateway.name,
    }
    api.listModelPoolSummaries.mockResolvedValue([...poolSummaries, observed])
    renderPage()

    const filter = await screen.findByRole('combobox', { name: /gateway/i })
    await user.selectOptions(filter, observedGateway.id)

    const table = screen.getByRole('table', { name: 'Model pools' })
    expect(within(table).getByRole('link', { name: 'Fabrikam models' })).toBeVisible()
    expect(within(table).queryByRole('link', { name: 'Anthropic Claude' })).not.toBeInTheDocument()
    expect(screen.getByText('1 of 3 pools')).toBeVisible()
  })

  it('opens a new draft pool once it’s saved', async () => {
    const user = userEvent.setup()
    api.listModelPoolSummaries.mockResolvedValue([])
    api.createModelPool.mockResolvedValue({ ...draftPool, id: 'modelpool_new', displayName: 'Anthropic' })
    renderPage()

    const create = screen.getByRole('button', { name: 'Create pool' })
    await waitFor(() => expect(create).toBeEnabled())
    await user.click(create)
    const dialog = await screen.findByRole('dialog', { name: 'Create a model pool' })
    await user.type(within(dialog).getByRole('textbox', { name: /^Name/ }), 'Anthropic')
    await user.click(within(dialog).getByRole('tab', { name: '4. Limits and review' }))
    await user.click(within(dialog).getByRole('button', { name: 'Save as draft' }))

    expect(await screen.findByText('Opened modelpool_new with {"openPlan":false}')).toBeVisible()
    expect(api.createModelPool).toHaveBeenCalledWith(
      expect.objectContaining({ displayName: 'Anthropic', gatewayId: poolGateway.id, models: [] }),
    )
  })
})
