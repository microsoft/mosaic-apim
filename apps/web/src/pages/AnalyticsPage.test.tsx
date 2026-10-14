import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useEffect } from 'react'
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom'
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

function LocationState({ onChange }: { onChange: (search: string) => void }) {
  const location = useLocation()
  useEffect(() => { onChange(location.search) }, [location.search, onChange])
  return <output aria-label="Current query">{location.search}</output>
}

function HistoryControls() {
  const navigate = useNavigate()
  return <>
    <button onClick={() => navigate(-1)}>Browser back</button>
    <button onClick={() => navigate(1)}>Browser forward</button>
  </>
}

function renderPage(initial = '/analytics') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const queryChanges: string[] = []
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <AnalyticsPage />
        <LocationState onChange={(search) => queryChanges.push(search)} />
        <HistoryControls />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { queryChanges }
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
    URL.createObjectURL = vi.fn(() => 'blob:csv')
    URL.revokeObjectURL = vi.fn()
    HTMLAnchorElement.prototype.click = vi.fn()
  })

  it('renders live overview data and exports CSV through the API', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByText('Alice Admin')).toBeVisible()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
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
    await user.click(screen.getByRole('button', { name: 'Filters' }))
    await user.selectOptions(screen.getByLabelText('Cost center'), 'cc-support')
    expect(api.getAnalyticsOverview).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))

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
    await user.click(screen.getByRole('button', { name: 'Filters' }))
    const resource = screen.getByLabelText('Resource')
    const pools = await within(resource).findByRole('group', { name: 'Model pools' })
    expect(within(pools).getByRole('option', { name: 'Claude' })).toBeInTheDocument()
    expect(within(resource).getByRole('group', { name: 'Model APIs' })).toBeInTheDocument()
    expect(within(resource).getByRole('group', { name: 'MCP servers' })).toBeInTheDocument()
    // A pool that has never been applied has no API to report on.
    expect(within(resource).queryByRole('option', { name: 'Draft pool' })).not.toBeInTheDocument()

    await user.selectOptions(resource, 'pool-claude')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
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

    await user.click(await screen.findByRole('button', { name: label }))

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

    await user.click(screen.getByRole('button', { name: 'Limits' }))

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

    await user.click(screen.getByRole('button', { name: 'Models' }))

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
    renderPage('/analytics?range=custom&start=2026-03-01&end=2026-03-05&extra=kept')

    await user.click(await screen.findByRole('button', { name: /Filters/ }))
    await user.selectOptions(screen.getByLabelText('Range'), '24h')
    await user.selectOptions(screen.getByLabelText('Gateway'), 'gateway-prod')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await user.click(screen.getByRole('button', { name: 'Consumers' }))

    await waitFor(() => {
      expect(api.getAnalyticsConsumers).toHaveBeenCalledWith(expect.objectContaining({ range: '24h', gatewayId: 'gateway-prod' }))
    })
    const query = new URLSearchParams(screen.getByLabelText('Current query').textContent ?? '')
    expect(query.get('tab')).toBe('consumers')
    expect(query.get('gatewayId')).toBe('gateway-prod')
    expect(query.get('range')).toBe('24h')
    expect(query.get('extra')).toBe('kept')
    expect(query.has('start')).toBe(false)
    expect(query.has('end')).toBe(false)
    expect(screen.queryByRole('img', { name: /Requests and tokens trend/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Filters/ }))
    expect(screen.getByLabelText('Gateway')).toHaveValue('gateway-prod')
  })

  it('keeps every scope in a draft and applies it in one URL update', async () => {
    const user = userEvent.setup()
    const initialQuery = '?tab=consumers&extra=kept'
    const { queryChanges } = renderPage(`/analytics${initialQuery}`)

    await screen.findByRole('table', { name: 'Consumers' })
    expect(screen.queryByLabelText('Range')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Export')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Filters' }))
    const dialog = screen.getByRole('dialog', { name: 'Filters' })
    expect(within(dialog).queryByLabelText('Export')).not.toBeInTheDocument()
    await user.selectOptions(within(dialog).getByLabelText('Range'), '7d')
    await user.selectOptions(within(dialog).getByLabelText('Gateway'), 'gateway-prod')
    await user.selectOptions(within(dialog).getByLabelText('Environment'), 'production')
    await user.selectOptions(within(dialog).getByLabelText('Resource'), 'mcp-tickets')
    await user.selectOptions(within(dialog).getByLabelText('Cost center'), 'cc-support')
    await user.selectOptions(within(dialog).getByLabelText('Subject kind'), 'application')
    expect(queryChanges).toEqual([initialQuery])
    expect(api.getAnalyticsConsumers).toHaveBeenCalledTimes(1)
    await user.click(within(dialog).getByRole('button', { name: 'Apply filters' }))

    await waitFor(() => expect(api.getAnalyticsConsumers).toHaveBeenLastCalledWith({
      range: '7d', start: undefined, end: undefined, gatewayId: 'gateway-prod',
      environment: 'production', resourceId: 'mcp-tickets', costCenterId: 'cc-support', subjectKind: 'application',
    }))
    expect(queryChanges).toHaveLength(2)
    const applied = new URLSearchParams(queryChanges[1])
    expect(applied.get('extra')).toBe('kept')
    expect(applied.get('tab')).toBe('consumers')
    expect(applied.get('range')).toBe('7d')
    expect(applied.get('environment')).toBe('production')
    expect(applied.get('gatewayId')).toBe('gateway-prod')
    expect(applied.get('resourceId')).toBe('mcp-tickets')
    expect(applied.get('costCenterId')).toBe('cc-support')
    expect(applied.get('subjectKind')).toBe('application')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Filters, 6 active' })).toHaveFocus()
    await user.click(screen.getByRole('button', { name: 'Filters, 6 active' }))
    expect(screen.getByLabelText('Range')).toHaveValue('7d')
    expect(screen.getByLabelText('Gateway')).toHaveValue('gateway-prod')
    expect(screen.getByLabelText('Environment')).toHaveValue('production')
    expect(screen.getByLabelText('Resource')).toHaveValue('mcp-tickets')
    expect(screen.getByLabelText('Cost center')).toHaveValue('cc-support')
    expect(screen.getByLabelText('Subject kind')).toHaveValue('application')
  })

  it.each(['Escape', 'Close', 'Outside'])('cancels draft changes on %s and restores trigger focus', async (dismiss) => {
    const user = userEvent.setup()
    const initialQuery = '?range=7d&gatewayId=gateway-prod&extra=kept'
    const { queryChanges } = renderPage(`/analytics${initialQuery}`)
    await screen.findByText('Alice Admin')
    const trigger = screen.getByRole('button', { name: 'Filters, 2 active' })
    const outside = screen.getByRole('heading', { name: 'Analytics' })
    await user.click(trigger)
    await user.selectOptions(screen.getByLabelText('Range'), '90d')
    await user.selectOptions(screen.getByLabelText('Gateway'), '')
    if (dismiss === 'Escape') await user.keyboard('{Escape}')
    else if (dismiss === 'Close') await user.click(screen.getByRole('button', { name: 'Close filters' }))
    else await user.click(outside)

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(queryChanges).toEqual([initialQuery])
    expect(api.getAnalyticsOverview).toHaveBeenCalledTimes(1)
    expect(trigger).toHaveFocus()
    await user.click(trigger)
    expect(screen.getByLabelText('Range')).toHaveValue('7d')
    expect(screen.getByLabelText('Gateway')).toHaveValue('gateway-prod')
  })

  it('resets only the draft until Apply and clears all scopes and custom dates', async () => {
    const user = userEvent.setup()
    const initialQuery = '?tab=consumers&extra=kept&range=custom&start=2026-03-01&end=2026-03-05&gatewayId=gateway-prod&environment=production&resourceId=mcp-tickets&costCenterId=cc-support&subjectKind=user'
    const { queryChanges } = renderPage(`/analytics${initialQuery}`)
    await screen.findByRole('table', { name: 'Consumers' })
    await user.click(screen.getByRole('button', { name: 'Filters, 6 active' }))
    expect(screen.getByLabelText('Start')).toHaveValue('2026-03-01')
    expect(screen.getByLabelText('End')).toHaveValue('2026-03-05')
    await user.click(screen.getByRole('button', { name: 'Reset' }))
    expect(screen.getByLabelText('Range')).toHaveValue('30d')
    for (const label of ['Gateway', 'Environment', 'Resource', 'Cost center', 'Subject kind']) {
      expect(screen.getByLabelText(label)).toHaveValue('')
    }
    expect(screen.queryByLabelText('Start')).not.toBeInTheDocument()
    expect(queryChanges).toEqual([initialQuery])
    expect(api.getAnalyticsConsumers).toHaveBeenCalledTimes(1)
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Filters, 6 active' }))
    expect(screen.getByLabelText('Start')).toHaveValue('2026-03-01')
    expect(screen.getByLabelText('Cost center')).toHaveValue('cc-support')
    await user.click(screen.getByRole('button', { name: 'Reset' }))
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(api.getAnalyticsConsumers).toHaveBeenLastCalledWith(expect.objectContaining({
      range: '30d', gatewayId: undefined, environment: undefined, resourceId: undefined,
      costCenterId: undefined, subjectKind: undefined, start: undefined, end: undefined,
    })))
    const applied = new URLSearchParams(queryChanges[1])
    expect([...applied.keys()].sort()).toEqual(['extra', 'range', 'tab'])
    expect(screen.getByRole('button', { name: 'Filters' })).toBeVisible()
  })

  it('validates nonempty ordered custom dates before applying', async () => {
    const user = userEvent.setup()
    const { queryChanges } = renderPage('/analytics?tab=overview&extra=kept')
    await screen.findByText('Alice Admin')
    await user.click(screen.getByRole('button', { name: 'Filters' }))
    await user.selectOptions(screen.getByLabelText('Range'), 'custom')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose both a start date and an end date.')
    await user.type(screen.getByLabelText('Start'), '2026-03-05')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose both a start date and an end date.')
    await user.type(screen.getByLabelText('End'), '2026-03-01')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(screen.getByRole('alert')).toHaveTextContent('The end date must be on or after the start date.')
    expect(screen.getByLabelText('End')).toHaveAttribute('aria-invalid', 'true')
    expect(queryChanges).toHaveLength(1)
    expect(api.getAnalyticsOverview).toHaveBeenCalledTimes(1)
    await user.clear(screen.getByLabelText('End'))
    await user.type(screen.getByLabelText('End'), '2026-03-05')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await waitFor(() => expect(api.getAnalyticsOverview).toHaveBeenLastCalledWith(expect.objectContaining({
      range: 'custom', start: '2026-03-05', end: '2026-03-05',
    })))
    expect(queryChanges).toHaveLength(2)
    expect(new URLSearchParams(queryChanges[1]).get('extra')).toBe('kept')
  })

  it('rejects malformed dates from a custom-range deep link', async () => {
    const user = userEvent.setup()
    const { queryChanges } = renderPage('/analytics?range=custom&start=2026-02-30&end=2026-03-05')
    await screen.findByText('Alice Admin')
    await user.click(screen.getByRole('button', { name: 'Filters, 1 active' }))
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    expect(screen.getByRole('alert')).toHaveTextContent('Choose valid start and end dates.')
    expect(queryChanges).toHaveLength(1)
    expect(api.getAnalyticsOverview).toHaveBeenCalledTimes(1)
  })

  it('restores applied scopes through browser Back and Forward', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?range=7d&extra=kept')
    await screen.findByText('Alice Admin')
    await user.click(screen.getByRole('button', { name: 'Filters, 1 active' }))
    await user.selectOptions(screen.getByLabelText('Range'), '90d')
    await user.selectOptions(screen.getByLabelText('Gateway'), 'gateway-prod')
    await user.click(screen.getByRole('button', { name: 'Apply filters' }))
    await user.click(screen.getByRole('button', { name: 'Browser back' }))
    await user.click(screen.getByRole('button', { name: 'Filters, 1 active' }))
    expect(screen.getByLabelText('Range')).toHaveValue('7d')
    expect(screen.getByLabelText('Gateway')).toHaveValue('')
    await user.keyboard('{Escape}')
    await user.click(screen.getByRole('button', { name: 'Browser forward' }))
    await user.click(screen.getByRole('button', { name: 'Filters, 2 active' }))
    expect(screen.getByLabelText('Range')).toHaveValue('90d')
    expect(screen.getByLabelText('Gateway')).toHaveValue('gateway-prod')
    expect(new URLSearchParams(screen.getByLabelText('Current query').textContent ?? '').get('extra')).toBe('kept')
  })

  it('exports the applied scope, never uncommitted draft values', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?tab=consumers&range=7d&costCenterId=cc-support')
    await screen.findByRole('table', { name: 'Consumers' })
    await user.selectOptions(screen.getByLabelText('Export'), 'groups')
    const exportButton = screen.getByRole('button', { name: 'Export CSV' })
    await user.click(screen.getByRole('button', { name: 'Filters, 2 active' }))
    await user.selectOptions(screen.getByLabelText('Range'), '90d')
    await user.selectOptions(screen.getByLabelText('Cost center'), '')
    await user.click(exportButton)

    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('groups', expect.objectContaining({
      range: '7d', costCenterId: 'cc-support',
    })))
    expect(api.getAnalyticsConsumers).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('Security groups CSV downloaded.')).toBeVisible()
  })

  it('uses only the supported export choices after changing section', async () => {
    const user = userEvent.setup()
    renderPage('/analytics?tab=consumers')
    await screen.findByRole('table', { name: 'Consumers' })
    await user.selectOptions(screen.getByLabelText('Export'), 'groups')
    await user.click(screen.getByRole('button', { name: 'Cost' }))
    await screen.findByText('Spend this month')
    expect(screen.getByLabelText('Export')).toHaveValue('chargeback')
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    await waitFor(() => expect(api.exportAnalytics).toHaveBeenCalledWith('chargeback', expect.anything()))
  })

  it('keeps export progress and failure recoverable', async () => {
    const user = userEvent.setup()
    let rejectExport!: (error: Error) => void
    api.exportAnalytics.mockReturnValueOnce(new Promise((_, reject) => { rejectExport = reject }))
    renderPage()
    await screen.findByText('Alice Admin')
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(screen.getByRole('button', { name: 'Exporting CSV…' })).toBeDisabled()
    rejectExport(new Error('Export is unavailable. Try again.'))
    expect(await screen.findByText('Export is unavailable. Try again.')).toHaveAttribute('role', 'status')
    await user.click(screen.getByRole('button', { name: 'Export CSV' }))
    expect(await screen.findByText('Trend CSV downloaded.')).toBeVisible()
    expect(api.exportAnalytics).toHaveBeenCalledTimes(2)
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