import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { anthropicPool, anthropicPoolDetail, draftPool, observedGateway, poolCandidates, poolGateway, poolSummaries } from '../test/pool-fixtures'
import type { PoolSuggestion } from '../types'
import { PoolsPage } from './PoolsPage'

const api = {
  getEnvironmentCatalog: vi.fn(), listModelPoolSummaries: vi.fn(), listModelPoolSuggestions: vi.fn(),
  listGateways: vi.fn(), getPoolCandidates: vi.fn(), createModelPool: vi.fn(), updateModelPool: vi.fn(),
  getModelPoolDetail: vi.fn(), listModelPoolRuns: vi.fn(), planModelPool: vi.fn(),
  planUnpublishModelPool: vi.fn(),
}
const suggestion: PoolSuggestion = {
  gatewayId: poolGateway.id, gatewayName: poolGateway.name, gatewayEnvironment: 'production',
  vendor: 'Anthropic', apiShape: 'anthropicMessages', endpointCount: 2, regions: ['eastus2', 'westus3'],
  models: [{ modelName: 'claude-sonnet-4-5', modelFormat: 'Anthropic', deploymentCount: 2, endpointCount: 2, regions: ['eastus2', 'westus3'] }],
  familyPools: [{ id: anthropicPool.id, displayName: anthropicPool.displayName }],
}
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})
function LocationProbe() {
  const location = useLocation()
  return <output data-testid="location">{location.pathname}{location.search}</output>
}
function renderPage(entry = '/pools') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([
    { path: '/pools', element: <><PoolsPage /><LocationProbe /></> },
    { path: '/pools/:poolId', element: <p>Expanded pool</p> },
  ], { initialEntries: [entry] })
  render(<QueryClientProvider client={queryClient}><FluentProvider theme={webLightTheme}>
    <RouterProvider router={router} />
  </FluentProvider></QueryClientProvider>)
  return router
}
describe('PoolsPage workbench', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({ environments: [], requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 }, compatibility: [], updatedAt: null })
    api.listModelPoolSummaries.mockResolvedValue(poolSummaries)
    api.listModelPoolSuggestions.mockResolvedValue([])
    api.listGateways.mockResolvedValue([poolGateway])
    api.getPoolCandidates.mockResolvedValue(poolCandidates)
    api.getModelPoolDetail.mockResolvedValue(anthropicPoolDetail)
    api.listModelPoolRuns.mockResolvedValue([])
  })
  it('uses concise selectable resources, not a squeezed comparison table', async () => {
    renderPage()
    const list = await screen.findByRole('list', { name: 'Model pools' })
    expect(within(list).getAllByRole('button')).toHaveLength(2)
    expect(within(list).getByText('Anthropic Claude')).toBeVisible()
    expect(within(list).getByText('Published')).toBeVisible()
    expect(screen.queryByRole('table', { name: 'Model pools' })).not.toBeInTheDocument()
    expect(screen.getByText('Select a resource to see its details.')).toBeVisible()
  })
  it('opens the selected full configuration and preserves selection while changing detail sections', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: /^Anthropic Claude/ }))
    expect(await screen.findByRole('heading', { name: 'Anthropic Claude', level: 1 })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open full detail' })).toHaveAttribute('href', `/pools/${anthropicPool.id}`)
    await user.click(screen.getByRole('button', { name: 'Models and deployments' }))
    expect(await screen.findByRole('table', { name: 'Deployments serving Claude Opus 4.5' })).toBeVisible()
    const url = screen.getByTestId('location').textContent!
    expect(url).toContain(`selected=${anthropicPool.id}`)
    expect(url).toContain('poolTab=models')
    expect(screen.getByRole('button', { name: /^Anthropic Claude/ })).toHaveAttribute('aria-pressed', 'true')
  })
  it('loads deep selection and reports a missing selection honestly', async () => {
    renderPage('/pools?selected=missing')
    expect(await screen.findByText('Pool not in this list')).toBeVisible()
    expect(api.getModelPoolDetail).not.toHaveBeenCalled()
  })
  it.each(['edit', 'publish', 'unpublish'] as const)('locks a direct embedded %s workspace with unknown access', async (mode) => {
    api.getModelPoolDetail.mockResolvedValue({ ...anthropicPoolDetail,
      pool: { ...anthropicPool, status: 'failed', accessState: 'unknown' } })
    renderPage(`/pools?selected=${anthropicPool.id}&mode=${mode === 'edit' ? 'edit' : 'review'}&planAction=${mode}`)

    expect(await screen.findByText(/gateway's access state is unknown/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Check recovery status (diagnostic only)' })).toBeVisible()
    expect(screen.queryByRole('region', { name: 'Edit Anthropic Claude' })).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Plan review' })).not.toBeInTheDocument()
    expect(api.updateModelPool).not.toHaveBeenCalled()
    expect(api.planModelPool).not.toHaveBeenCalled()
    expect(api.planUnpublishModelPool).not.toHaveBeenCalled()
  })
  it('retains unapplied changes in the list', async () => {
    api.listModelPoolSummaries.mockResolvedValue([{ ...poolSummaries[0], unappliedChanges: true }])
    renderPage()
    expect(await screen.findByText('Changes not applied')).toBeVisible()
  })
  it('disables creation without a gateway', async () => {
    api.listModelPoolSummaries.mockResolvedValue([])
    api.listGateways.mockResolvedValue([])
    renderPage()
    expect(await screen.findByRole('link', { name: 'Onboard a gateway' })).toHaveAttribute('href', '/gateways')
    expect(screen.getByRole('button', { name: 'Create pool' })).toBeDisabled()
  })
  it('keeps the gateway filter in URL state', async () => {
    const user = userEvent.setup()
    api.listModelPoolSummaries.mockResolvedValue([...poolSummaries, {
      ...poolSummaries[1], pool: { ...draftPool, id: 'fabrikam-pool', displayName: 'Fabrikam models', gatewayId: observedGateway.id },
      gatewayName: observedGateway.name,
    }])
    renderPage()
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Filter pools by gateway' }), observedGateway.id)
    expect(await screen.findByRole('button', { name: /^Fabrikam models/ })).toBeVisible()
    expect(screen.queryByRole('button', { name: /^Anthropic Claude/ })).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent(`gateway=${observedGateway.id}`)
  })
  it('creates in the workspace and returns to the saved selection without applying', async () => {
    const user = userEvent.setup()
    const saved = { ...draftPool, id: 'modelpool_new', displayName: 'New pool' }
    api.createModelPool.mockImplementation(async () => {
      api.listModelPoolSummaries.mockResolvedValue([{ ...poolSummaries[1], pool: saved }])
      api.getModelPoolDetail.mockResolvedValue({ ...anthropicPoolDetail, pool: saved, models: [] })
      return saved
    })
    renderPage('/pools?gateway=all')
    await waitFor(() => expect(screen.getByRole('button', { name: 'Create pool' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Create pool' }))
    const editor = await screen.findByRole('region', { name: 'Create a model pool' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Model pools' })).not.toBeInTheDocument()
    await user.type(within(editor).getByRole('textbox', { name: /^Name/ }), 'New pool')
    await user.click(within(editor).getByRole('tab', { name: '4. Limits and review' }))
    await user.click(within(editor).getByRole('button', { name: 'Save as draft' }))
    expect(await screen.findByRole('heading', { name: 'New pool', level: 1 })).toBeVisible()
    expect(screen.getByTestId('location')).toHaveTextContent('selected=modelpool_new')
    expect(screen.getByTestId('location')).toHaveTextContent('gateway=all')
    expect(api.planModelPool).not.toHaveBeenCalled()
  })
  it('shows suggested model membership only in the generous selected detail', async () => {
    const user = userEvent.setup()
    api.listModelPoolSuggestions.mockResolvedValue([suggestion])
    renderPage()
    await user.click(screen.getByRole('button', { name: 'Suggested pools' }))
    const list = await screen.findByRole('list', { name: 'Suggested pools' })
    expect(within(list).queryByText('claude-sonnet-4-5')).not.toBeInTheDocument()
    await user.click(within(list).getByRole('button'))
    expect(await screen.findByText('claude-sonnet-4-5')).toBeVisible()
    expect(screen.getByText('2 deployments on 2 endpoints · eastus2, westus3')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Anthropic Claude' })).toHaveAttribute('href', `/pools/${anthropicPool.id}`)
    await user.click(screen.getByRole('button', { name: 'Create pool for Anthropic on Contoso AI gateway' }))
    expect(await screen.findByRole('region', { name: 'Create a model pool' })).toBeVisible()
    expect(api.getPoolCandidates).toHaveBeenCalledWith(poolGateway.id)
    expect(api.createModelPool).not.toHaveBeenCalled()
  })
  it('protects a dirty draft on section navigation and restores it when discard is canceled', async () => {
    const user = userEvent.setup()
    renderPage('/pools?mode=new')
    const editor = await screen.findByRole('region', { name: 'Create a model pool' })
    await user.type(within(editor).getByRole('textbox', { name: /^Name/ }), 'Keep this draft')
    await user.click(screen.getByRole('button', { name: 'Suggested pools' }))
    const confirm = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
    await user.click(within(confirm).getByRole('button', { name: 'Stay' }))
    expect(within(editor).getByRole('textbox', { name: /^Name/ })).toHaveValue('Keep this draft')
    expect(screen.getByTestId('location')).toHaveTextContent('mode=new')
  })
  it('reconstructs suggested defaults on a direct editor URL without persisting a form draft', async () => {
    api.listModelPoolSuggestions.mockResolvedValue([suggestion])
    const id = `${suggestion.gatewayId}|${suggestion.vendor}|${suggestion.apiShape}`
    renderPage(`/pools?tab=suggestions&selected=${encodeURIComponent(id)}&mode=new`)
    const editor = await screen.findByRole('region', { name: 'Create a model pool' })
    expect(within(editor).getByRole('textbox', { name: /^Name/ })).toHaveValue('Anthropic')
    expect(api.getPoolCandidates).toHaveBeenCalledWith(suggestion.gatewayId)
    expect(api.createModelPool).not.toHaveBeenCalled()
  })
})
