import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AnalyticsPage } from './AnalyticsPage'
import type { EnvironmentCatalogView } from '../types'
import {
  consumersFixture,
  hygieneFixture,
  limitsFixture,
  modelsFixture,
  notConfiguredOverview,
  overviewFixture,
  reliabilityFixture,
  unattributedFixture,
} from '../test/analytics-fixtures'

const catalog: EnvironmentCatalogView = {
  environments: [
    { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, aliases: [], acceptsEndpointsFrom: [], order: 50, builtIn: true, usage: { gateways: 1, modelEndpoints: 1, mcpEndpoints: 0 } },
  ],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

const api = {
  getEnvironmentCatalog: vi.fn(),
  listGateways: vi.fn(),
  listModelApis: vi.fn(),
  listMcpServers: vi.fn(),
  getAnalyticsOverview: vi.fn(),
  getAnalyticsConsumers: vi.fn(),
  getAnalyticsModels: vi.fn(),
  getAnalyticsReliability: vi.fn(),
  getAnalyticsLimits: vi.fn(),
  getAnalyticsHygiene: vi.fn(),
  getAnalyticsUnattributed: vi.fn(),
  refreshGatewayTelemetry: vi.fn(),
  exportAnalytics: vi.fn(),
}

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: class ApiError extends Error {
    readonly status: number
    constructor(message: string, status: number) {
      super(message)
      this.status = status
    }
  },
}))

function renderPage(initial = '/analytics') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <AnalyticsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('AnalyticsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue(catalog)
    api.listGateways.mockResolvedValue([{ id: 'gateway-prod', name: 'Production gateway' }])
    api.listModelApis.mockResolvedValue([{ id: 'model-api-chat', displayName: 'Chat' }])
    api.listMcpServers.mockResolvedValue([{ id: 'mcp-tickets', displayName: 'Ticket tools' }])
    api.getAnalyticsOverview.mockResolvedValue(overviewFixture)
    api.getAnalyticsConsumers.mockResolvedValue(consumersFixture)
    api.getAnalyticsModels.mockResolvedValue(modelsFixture)
    api.getAnalyticsReliability.mockResolvedValue(reliabilityFixture)
    api.getAnalyticsLimits.mockResolvedValue(limitsFixture)
    api.getAnalyticsHygiene.mockResolvedValue(hygieneFixture)
    api.getAnalyticsUnattributed.mockResolvedValue(unattributedFixture)
    api.refreshGatewayTelemetry.mockResolvedValue(overviewFixture.gateways[0])
    api.exportAnalytics.mockResolvedValue({ blob: new Blob(['csv'], { type: 'text/csv' }), filename: 'mosaic-trend-20260219-20260320.csv' })
    vi.stubGlobal('URL', { createObjectURL: vi.fn(() => 'blob:csv'), revokeObjectURL: vi.fn() })
    HTMLAnchorElement.prototype.click = vi.fn()
  })

  it('renders live overview data and exports CSV through the API', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByText('Alice Admin')).toBeVisible()
    expect(screen.getAllByText('Live data').length).toBeGreaterThan(0)
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
    // Models and callers lead with the tokens they rank by; APIs, which include MCP servers, with calls.
    const models = screen.getByRole('list', { name: 'Top models' })
    expect(within(models).getByText('45K tokens')).toBeVisible()
    expect(within(models).getByText('153 calls')).toBeVisible()
    expect(within(screen.getByRole('list', { name: 'Top callers' })).getByText('alice@contoso · 106 calls')).toBeVisible()
    const apis = screen.getByRole('list', { name: 'Top APIs' })
    expect(within(apis).getByText('154 calls')).toBeVisible()
    expect(within(apis).getByText('Production gateway · 45K tokens')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('trend', expect.objectContaining({ range: '30d' })))
    // The file keeps the name the API gave it, which carries the view and its dates.
    await waitFor(() => expect(vi.mocked(HTMLAnchorElement.prototype.click).mock.contexts[0]).toHaveProperty('download', 'mosaic-trend-20260219-20260320.csv'))
  })

  it.each([
    ['Consumers', 'Grants', 'Support bot'],
    ['Models', 'Deployments', 'chat-prod'],
    ['Reliability', 'Denials by reason', 'No grant for this caller'],
    ['Limits', 'Grant limits', '15,000 / 20,000'],
    ['Access hygiene', 'Unused keys', 'Platform team'],
    ['Unattributed', 'Unattributed calls', "Publication's shared key"],
  ])('renders the %s tab from live fixtures', async (label, heading, text) => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('tab', { name: label }))

    expect(await screen.findByRole('heading', { name: heading })).toBeVisible()
    expect(screen.getByText(text)).toBeVisible()
  })

  it('names what each consumer is and how close grants are to their limits', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?tab=consumers')

    const consumers = await screen.findByRole('table', { name: 'Consumers' })
    const kindOf = (name: string) => within(within(consumers).getByText(name).closest('tr') as HTMLElement).getAllByRole('cell')[1]
    expect(kindOf('Alice Admin')).toHaveTextContent('Person')
    // An agent user signs in as a user, so it is counted with people but labelled for what it is.
    expect(kindOf('Scheduling Assistant')).toHaveTextContent('Agent user')
    expect(kindOf('Support bot')).toHaveTextContent('Application')
    expect(kindOf('Analysts')).toHaveTextContent('Security group')
    const grants = screen.getByRole('table', { name: 'Grant usage' })
    expect(within(grants).getByText('Person · alice@contoso')).toBeVisible()
    expect(within(grants).getByText('Active')).toBeVisible()

    await user.click(screen.getByRole('tab', { name: 'Limits' }))

    const limits = await screen.findByRole('table', { name: 'Grant limit use' })
    expect(within(limits).getByText('OK')).toBeVisible()
    expect(within(limits).getByText('Monthly token quota')).toBeVisible()
    // A grant to an agent user is a user grant, but the row says which kind of user it is.
    const scheduler = within(limits).getByText('Scheduling Assistant').closest('tr') as HTMLElement
    expect(within(scheduler).getByText('Agent user')).toBeVisible()
    expect(within(scheduler).getByText('Near limit')).toBeVisible()
    expect(within(scheduler).getByText('Calls per minute')).toBeVisible()
  })

  it('exports the table picked for the open tab', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?tab=consumers')

    await screen.findByRole('table', { name: 'Consumers' })
    const picker = screen.getByLabelText('Export')
    expect(within(picker).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'People',
      'Applications',
      'Security groups',
      'Grants',
      'Client applications',
    ])
    await user.selectOptions(picker, 'Security groups')
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))

    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('groups', expect.anything()))
    expect(await screen.findByText('Security groups CSV downloaded.')).toBeVisible()
  })

  it('names latency buckets and API kinds on the Reliability tab', async () => {
    renderPage('/analytics?tab=reliability')

    const apis = await screen.findByRole('table', { name: 'Per-API reliability' })
    expect(within(apis).getByText('Model API')).toBeVisible()
    const latency = screen.getByRole('img', { name: /Estimated latency histogram/ })
    expect(within(latency).getByText('≤500 ms')).toBeVisible()
    expect(within(latency).getByText('≤2 s')).toBeVisible()
    expect(within(latency).getByText('>2 s')).toBeVisible()
  })

  it("counts a model deployment's own 429s apart from the gateway's", async () => {
    api.getAnalyticsReliability.mockResolvedValue({ ...reliabilityFixture, statusMix: { ...reliabilityFixture.statusMix, throttled: 5, backendThrottled: 2 } })
    api.getAnalyticsModels.mockResolvedValue({ ...modelsFixture, deployments: [{ ...modelsFixture.deployments[0], throttled: 3, backendThrottled: 1 }] })
    const user = userEvent.setup()
    renderPage('/analytics?tab=reliability')

    // The gateway passes a deployment's 429 on, so the call is in both counts until it's taken out.
    const gateway = (await screen.findByText('Gateway throttled', { selector: 'span' })).parentElement as HTMLElement
    expect(within(gateway).getByText('3')).toBeVisible()
    const backend = screen.getByText('Backend 429s', { selector: 'span' }).parentElement as HTMLElement
    expect(within(backend).getByText('2')).toBeVisible()

    await user.click(screen.getByRole('tab', { name: 'Models' }))

    const deployments = await screen.findByRole('table', { name: 'Deployments' })
    const cells = within(within(deployments).getByText('chat-prod').closest('tr') as HTMLElement).getAllByRole('cell')
    expect(cells[3]).toHaveTextContent(/^2$/)
    expect(cells[4]).toHaveTextContent(/^1$/)
  })

  it('labels day buckets without an hour', async () => {
    renderPage()

    expect(await screen.findByRole('img', { name: /Requests and tokens trend/ })).toBeVisible()
    expect(screen.getAllByText('Mar 17').length).toBeGreaterThan(0)
    expect(screen.queryByText(/Mar 17, 12 AM/)).not.toBeInTheDocument()
  })

  it('keeps filters and active tab in the URL', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.selectOptions(await screen.findByLabelText('Range'), '24h')
    await user.selectOptions(screen.getByLabelText('Gateway'), 'gateway-prod')
    await user.click(screen.getByRole('tab', { name: 'Consumers' }))

    await waitFor(() => {
      expect(api.getAnalyticsConsumers).toHaveBeenCalledWith(expect.objectContaining({ range: '24h', gatewayId: 'gateway-prod' }))
    })
  })

  it('shows notConfigured without invented numbers', async () => {
    api.getAnalyticsOverview.mockResolvedValue(notConfiguredOverview)
    renderPage()

    expect(await screen.findByText('Usage telemetry is not configured')).toBeVisible()
    expect(screen.getAllByText(/MOSAIC_USAGE_SOURCE/).length).toBeGreaterThan(0)
    expect(screen.queryByText('$')).not.toBeInTheDocument()
  })

  it('captions 24h whole-day breakdowns', async () => {
    api.getAnalyticsOverview.mockResolvedValue({
      ...overviewFixture,
      window: { ...overviewFixture.window, range: '24h', granularity: 'hour', breakdownStart: '2026-03-17', breakdownEnd: '2026-03-18' },
    })
    renderPage('/analytics?range=24h')

    expect(await screen.findByText(/Breakdowns and active caller/)).toBeVisible()
  })

  it('shows a 429 refresh message', async () => {
    const user = userEvent.setup()
    api.refreshGatewayTelemetry.mockRejectedValue(Object.assign(new Error('Too soon'), { status: 429 }))
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Refresh now' }))

    expect(await screen.findByText(/refreshed less than a minute ago/i)).toBeVisible()
  })
})
