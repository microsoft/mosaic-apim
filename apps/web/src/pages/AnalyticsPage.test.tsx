import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AnalyticsPage } from './AnalyticsPage'
import type { EnvironmentCatalogView } from '../types'
import {
  consumersFixture,
  costFixture,
  costSummaryFixture,
  hygieneFixture,
  limitsFixture,
  modelsFixture,
  notConfiguredOverview,
  overviewFixture,
  pricedOverviewFixture,
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
  listModelPools: vi.fn(),
  listCostCenters: vi.fn(),
  getAnalyticsOverview: vi.fn(),
  getAnalyticsConsumers: vi.fn(),
  getAnalyticsModels: vi.fn(),
  getAnalyticsReliability: vi.fn(),
  getAnalyticsLimits: vi.fn(),
  getAnalyticsHygiene: vi.fn(),
  getAnalyticsUnattributed: vi.fn(),
  getAnalyticsCost: vi.fn(),
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
    api.listModelPools.mockResolvedValue([])
    api.listCostCenters.mockResolvedValue([{ id: 'cc-support', name: 'Support', code: 'support' }])
    api.getAnalyticsOverview.mockResolvedValue(overviewFixture)
    api.getAnalyticsConsumers.mockResolvedValue(consumersFixture)
    api.getAnalyticsModels.mockResolvedValue(modelsFixture)
    api.getAnalyticsReliability.mockResolvedValue(reliabilityFixture)
    api.getAnalyticsLimits.mockResolvedValue(limitsFixture)
    api.getAnalyticsHygiene.mockResolvedValue(hygieneFixture)
    api.getAnalyticsUnattributed.mockResolvedValue(unattributedFixture)
    api.getAnalyticsCost.mockResolvedValue(costFixture)
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
    expect(within(screen.getByRole('list', { name: 'Top cost centers' })).getByText('support · 120 calls')).toBeVisible()
    const apis = screen.getByRole('list', { name: 'Top APIs' })
    expect(within(apis).getByText('154 calls')).toBeVisible()
    expect(within(apis).getByText('Production gateway · 45K tokens')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('trend', expect.objectContaining({ range: '30d' })))
    // The file keeps the name the API gave it, which carries the view and its dates.
    await waitFor(() => expect(vi.mocked(HTMLAnchorElement.prototype.click).mock.contexts[0]).toHaveProperty('download', 'mosaic-trend-20260219-20260320.csv'))
  })

  it('passes the cost-center filter to report requests and exports', async () => {
    const user = userEvent.setup()
    renderPage()

    await screen.findByText('Alice Admin')
    await user.selectOptions(screen.getByLabelText('Cost center'), 'cc-support')

    await waitFor(() => expect(api.getAnalyticsOverview).toHaveBeenLastCalledWith(expect.objectContaining({ costCenterId: 'cc-support' })))
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('trend', expect.objectContaining({ costCenterId: 'cc-support' })))
  })

  it('offers model pools whose API is in API Management as resources', async () => {
    api.listModelPools.mockResolvedValue([
      { id: 'pool-claude', displayName: 'Claude', apiName: 'claude', resources: [{ kind: 'api', name: 'claude', createdByMosaic: true }] },
      { id: 'pool-draft', displayName: 'Draft pool', apiName: 'draft-pool', resources: [] },
    ])
    const user = userEvent.setup()
    renderPage()

    await screen.findByText('Alice Admin')
    const resource = screen.getByLabelText('Resource')
    const pools = await within(resource).findByRole('group', { name: 'Model pools' })
    expect(within(pools).getByRole('option', { name: 'Claude' })).toBeInTheDocument()
    expect(within(resource).getByRole('group', { name: 'Model APIs' })).toBeInTheDocument()
    expect(within(resource).getByRole('group', { name: 'MCP servers' })).toBeInTheDocument()
    // A pool that has never been applied has no API to report on.
    expect(within(resource).queryByRole('option', { name: 'Draft pool' })).not.toBeInTheDocument()

    await user.selectOptions(resource, 'pool-claude')
    await waitFor(() => expect(api.getAnalyticsOverview).toHaveBeenLastCalledWith(expect.objectContaining({ resourceId: 'pool-claude' })))
  })

  it.each([
    ['Consumers', 'Grants', 'Scheduling Assistant'],
    ['Models', 'Deployments', 'chat-prod'],
    ['Reliability', 'Denials by reason', 'No grant for this caller'],
    ['Limits', 'Grant limits', '15,000 / 20,000'],
    ['Access hygiene', 'Unused keys', 'Platform team'],
    ['Unattributed', 'Unattributed calls', 'Shared key'],
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
    const costCenters = screen.getByRole('table', { name: 'Cost center usage' })
    expect(within(costCenters).getByText('Support')).toBeVisible()
    expect(within(costCenters).getByText('support')).toBeVisible()

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

  it('renders cost by cost center', async () => {
    renderPage('/analytics?tab=cost')

    const list = await screen.findByRole('list', { name: 'Cost by cost center' })
    expect(within(list).getByText('Support')).toBeVisible()
    expect(within(list).getByText('support · 11.1M tokens · 60.0% of the cost')).toBeVisible()
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
      'Cost centers',
      'Grants',
      'Client applications',
      'Model use through MCP servers',
    ])
    await user.selectOptions(picker, 'Security groups')
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))

    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('groups', expect.anything()))
    expect(await screen.findByText('Security groups CSV downloaded.')).toBeVisible()
  })

  it('renders model use through MCP servers and unresolved counts', async () => {
    renderPage('/analytics?tab=consumers')

    const table = await screen.findByRole('table', { name: 'Model use through MCP servers' })
    expect(within(table).getByText('Adele Vance')).toBeVisible()
    expect(within(table).getByText('Ticket tools')).toBeVisible()
    expect(within(table).getByText('Support bot')).toBeVisible()
    expect(screen.getByText("MOSAIC couldn't attribute 4 model calls to a person: 1 with a malformed reference, 2 with no matching MCP call, 1 after the MCP call ended.")).toBeVisible()
  })

  it('spans every column of the model use table when it has no rows', async () => {
    api.getAnalyticsConsumers.mockResolvedValue({ ...consumersFixture, onBehalf: [], onBehalfUnresolved: [] })
    renderPage('/analytics?tab=consumers')

    const table = await screen.findByRole('table', { name: 'Model use through MCP servers' })
    const columns = within(table).getAllByRole('columnheader').length
    const empty = within(table).getByText('No model calls were made through MCP servers for these filters.').closest('td')
    expect(empty).toHaveAttribute('colspan', String(columns))
    expect(screen.queryByText(/couldn't attribute/)).not.toBeInTheDocument()
  })

  it('exports model use through MCP servers from the Consumers tab', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?tab=consumers')

    await screen.findByRole('table', { name: 'Model use through MCP servers' })
    const picker = screen.getByLabelText('Export')
    await user.selectOptions(picker, 'Model use through MCP servers')
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))

    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('onBehalf', expect.anything()))
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

  it('shows what the window cost beside its usage', async () => {
    api.getAnalyticsOverview.mockResolvedValue(pricedOverviewFixture)
    renderPage()

    await screen.findByText('Alice Admin')
    const cost = screen.getAllByText('Cost').map((element) => element.closest('.fui-Card')).find(Boolean) as HTMLElement
    expect(within(cost).getByText('$7,235.25')).toBeVisible()
    // What a cost leaves out is said, so an unpriced deployment never reads as free.
    expect(within(cost).getByText('Leaves out 4K tokens with no price')).toBeVisible()
    expect(within(screen.getByRole('list', { name: 'Top models' })).getByText('153 calls · $4,355.25')).toBeVisible()
  })

  it('prices the range and projects the month on the Cost tab', async () => {
    renderPage('/analytics?tab=cost')

    const spend = (await screen.findByText('Spend this month')).closest('.fui-Card') as HTMLElement
    expect(within(spend).getByText('$4,355.25')).toBeVisible()
    const forecast = screen.getByText('Month-end forecast').closest('.fui-Card') as HTMLElement
    expect(within(forecast).getByText('$7,502.10')).toBeVisible()
    expect(within(forecast).getByText('Projected')).toBeVisible()
    expect(within(forecast).getByText('At this month’s pace so far, over 17.7 of 31 days')).toBeVisible()
    const reserved = screen.getByText('Reserved capacity').closest('.fui-Card') as HTMLElement
    expect(within(reserved).getByText('$7,200.00')).toBeVisible()
    // The trend's cost line is labelled in dollars, not as a bare count.
    expect(screen.getByText(/^Cost, peak \$/)).toBeVisible()

    const deployments = screen.getByRole('table', { name: 'Deployment cost' })
    const row = (name: string) => within(within(deployments).getByText(name).closest('tr') as HTMLElement)
    expect(row('chat').getByText('$2.50 in · $10.00 out per 1M')).toBeVisible()
    expect(row('reserved').getByText('10 PTUs at $1.00 an hour')).toBeVisible()
    expect(row('reserved').getByText('$7,440.00 this month, shared by tokens')).toBeVisible()
    expect(row('reserved').getByText('$2,880.00 with no calls')).toBeVisible()
    expect(row('mystery').getAllByText('No price').length).toBeGreaterThan(0)

    const unpriced = screen.getByRole('table', { name: 'Usage with no price' })
    expect(within(unpriced).getByText(/No price for contoso-llm/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Fix them on the Pricing page' })).toHaveAttribute('href', '/pricing?tab=unpriced')
    expect(screen.getByRole('note', { name: 'How MOSAIC priced this usage' })).toHaveTextContent('cached prompt tokens')
    expect(within(screen.getByRole('list', { name: 'Cost by consumer' })).getByText('Bob')).toBeVisible()
  })

  it('waits for a day of figures before forecasting', async () => {
    api.getAnalyticsCost.mockResolvedValue({ ...costFixture, spend: { ...costFixture.spend!, forecast: null, daysElapsed: 0.2 } })
    renderPage('/analytics?tab=cost')

    const forecast = (await screen.findByText('Month-end forecast')).closest('.fui-Card') as HTMLElement
    expect(within(forecast).getByText('—')).toBeVisible()
    expect(within(forecast).getByText('Forecast after a day of this month’s figures')).toBeVisible()
  })

  it('says no reserved capacity was charged rather than showing $0', async () => {
    api.getAnalyticsCost.mockResolvedValue({ ...costFixture, cost: { ...costFixture.cost, reserved: null } })
    renderPage('/analytics?tab=cost')

    const reserved = (await screen.findByText('Reserved capacity')).closest('.fui-Card') as HTMLElement
    expect(within(reserved).getByText('None')).toBeVisible()
    expect(within(reserved).getByText('No provisioned deployment was charged in this range')).toBeVisible()
    expect(within(reserved).queryByText('$0.00')).not.toBeInTheDocument()
  })

  it('gives the cost line no peak when nothing in the range has a price', async () => {
    api.getAnalyticsCost.mockResolvedValue({ ...costFixture, trend: costFixture.trend.map((point) => ({ ...point, cost: null })) })
    renderPage('/analytics?tab=cost')

    const chart = (await screen.findByRole('img', { name: 'Cost and tokens trend' })).closest('figure') as HTMLElement
    expect(within(chart).queryByText(/^Cost, peak/)).not.toBeInTheDocument()
    expect(within(chart).getByText('Tokens, peak 1.2M')).toBeVisible()
  })

  it('exports the chargeback from the Cost tab', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?tab=cost')

    await screen.findByText('Spend this month')
    const picker = screen.getByLabelText('Export')
    expect(within(picker).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Chargeback by month',
      'Cost by deployment',
      'Cost centers',
    ])
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))

    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('chargeback', expect.anything()))
  })

  it('adds a cost column where usage is priced, and says No price rather than $0', async () => {
    api.getAnalyticsConsumers.mockResolvedValue({
      ...consumersFixture,
      cost: costSummaryFixture,
      people: consumersFixture.people.map((row, index) => ({ ...row, cost: index === 0 ? 35 : null })),
    })
    renderPage('/analytics?tab=consumers')

    const consumers = await screen.findByRole('table', { name: 'Consumers' })
    const costOf = (name: string) => within(within(consumers).getByText(name).closest('tr') as HTMLElement).getAllByRole('cell').at(-1)
    expect(costOf('Alice Admin')).toHaveTextContent('$35.00')
    expect(costOf('Scheduling Assistant')).toHaveTextContent('No price')
    const linked = screen.getByText('Linked cost').closest('.fui-Card') as HTMLElement
    expect(within(linked).getByText('$7,235.25')).toBeVisible()
  })

  it('leaves the cost column out where there is no price list', async () => {
    renderPage('/analytics?tab=consumers')

    const consumers = await screen.findByRole('table', { name: 'Consumers' })
    expect(within(consumers).queryByRole('columnheader', { name: 'Cost' })).not.toBeInTheDocument()
  })
})