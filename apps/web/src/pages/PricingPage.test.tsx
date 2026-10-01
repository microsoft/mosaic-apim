import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PricingPage } from './PricingPage'
import type { EndpointPricingView, PriceListView, PriceView, PricingOverview, UnpricedReport } from '../types'

const seedSource = {
  title: 'Azure Retail Prices API: Foundry Models, Azure OpenAI, every region',
  url: "https://prices.azure.com/api/retail/prices?$filter=serviceName%20eq%20'Foundry%20Models'",
  retrievedOn: '2026-09-30',
}

function price(overrides: Partial<PriceView> = {}): PriceView {
  return {
    id: 'commercial.openai.gpt-4o.2024-11-20.globalstandard',
    lineId: 'priceline_gpt4o',
    origin: 'seed',
    status: 'current',
    cloud: 'commercial',
    cloudLabel: 'Azure Commercial',
    publisher: 'OpenAI',
    model: 'gpt-4o',
    aliases: [],
    version: '2024-11-20',
    deploymentType: 'GlobalStandard',
    regions: null,
    deployment: null,
    inputPerMillion: 2.5,
    cachedInputPerMillion: 1.25,
    outputPerMillion: 10,
    ptuHourly: null,
    monthlyAmount: null,
    effectiveFrom: '2024-12-01',
    recordedAt: '2026-09-30T00:00:00Z',
    recordedBy: null,
    sources: [seedSource],
    note: null,
    overrides: null,
    ...overrides,
  }
}

const overview: PricingOverview = {
  currency: 'USD',
  asOf: '2026-09-30',
  seedLastUpdated: '2026-09-30',
  sources: [seedSource, { title: 'Determine PTU sizing for a workload', url: 'https://learn.microsoft.com/azure/foundry/openai/how-to/provisioned-throughput-sizing', retrievedOn: '2026-09-30' }],
  clouds: [
    { key: 'commercial', label: 'Azure Commercial', builtIn: true, prices: 2, endpoints: 1 },
    { key: 'government', label: 'Azure Government', builtIn: true, prices: 1, endpoints: 0 },
  ],
  deploymentTypes: ['GlobalStandard', 'Standard', 'GlobalProvisionedManaged'],
  deployments: 3,
  pricedDeployments: 1,
}

const commercial: PriceListView = {
  cloud: 'commercial',
  cloudLabel: 'Azure Commercial',
  currency: 'USD',
  asOf: '2026-09-30',
  lines: [
    { lineId: 'priceline_gpt4o', current: price(), upcoming: price({ id: 'price_new', origin: 'admin', status: 'upcoming', inputPerMillion: 2, effectiveFrom: '2026-11-01' }), versions: 2 },
    {
      lineId: 'priceline_ptu',
      current: price({ id: 'commercial.openai.all-models.any.globalprovisionedmanaged', lineId: 'priceline_ptu', model: '*', version: null, deploymentType: 'GlobalProvisionedManaged', inputPerMillion: null, cachedInputPerMillion: null, outputPerMillion: null, ptuHourly: 1 }),
      upcoming: null,
      versions: 1,
    },
  ],
}

const unpriced: UnpricedReport = {
  asOf: '2026-09-30',
  days: 30,
  deployments: 3,
  pricedDeployments: 1,
  rows: [
    { key: 'endpoint-aoai/mistral', kind: 'deployment', label: 'mistral', endpointId: 'endpoint-aoai', endpointName: 'Contoso Azure OpenAI', deploymentName: 'mistral', model: 'Mistral-Large-2411', version: '2', deploymentType: 'GlobalStandard', cloud: 'commercial', region: 'eastus2', declared: false, reason: 'noPrice', message: 'No price for Mistral-Large-2411 2 (GlobalStandard) in eastus2 in Azure Commercial.', requests: 120, totalTokens: 800_000 },
    { key: 'endpoint-partner/claude', kind: 'deployment', label: 'claude', endpointId: 'endpoint-partner', endpointName: 'Fabrikam partner Foundry', deploymentName: 'claude', model: 'claude-sonnet-4-5', version: null, deploymentType: null, cloud: 'commercial', region: null, declared: true, reason: 'noDeploymentType', message: "MOSAIC doesn't know claude's deployment type.", requests: 0, totalTokens: 0 },
  ],
}

const endpoints: EndpointPricingView[] = [
  {
    endpointId: 'endpoint-aoai',
    name: 'Contoso Azure OpenAI',
    provider: 'azureOpenAi',
    host: 'contoso.openai.azure.com',
    detectedCloud: 'commercial',
    cloud: 'commercial',
    cloudLabel: 'Azure Commercial',
    cloudSource: 'detected',
    region: 'eastus2',
    regionSource: 'detected',
    deployments: [
      { deploymentName: 'chat', model: 'gpt-4o', version: '2024-11-20', deploymentType: 'GlobalStandard', deploymentTypeSource: 'observed', capacity: 100, declared: false, priced: true },
      { deploymentName: 'mistral', model: 'Mistral-Large-2411', version: '2', deploymentType: 'GlobalStandard', deploymentTypeSource: 'observed', capacity: 1, declared: false, priced: false, reason: 'noPrice' },
    ],
  },
  {
    endpointId: 'endpoint-partner',
    name: 'Fabrikam partner Foundry',
    provider: 'azureAiFoundry',
    host: 'fabrikam.services.ai.azure.com',
    detectedCloud: 'commercial',
    cloud: 'commercial',
    cloudLabel: 'Azure Commercial',
    cloudSource: 'detected',
    region: null,
    regionSource: null,
    deployments: [
      { deploymentName: 'claude', model: 'claude-sonnet-4-5', version: null, deploymentType: null, deploymentTypeSource: null, capacity: null, declared: true, priced: false, reason: 'noDeploymentType' },
    ],
  },
]

const api = {
  getPricingOverview: vi.fn(),
  listPrices: vi.fn(),
  addPrice: vi.fn(),
  getPriceHistory: vi.fn(),
  getUnpricedDeployments: vi.fn(),
  listEndpointPricing: vi.fn(),
  updateEndpointPricing: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderPage(initial = '/pricing') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <PricingPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('PricingPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getPricingOverview.mockResolvedValue(overview)
    api.listPrices.mockImplementation(async (cloud: string) =>
      cloud === 'government'
        ? { ...commercial, cloud, cloudLabel: 'Azure Government', lines: [] }
        : commercial,
    )
    api.getUnpricedDeployments.mockResolvedValue(unpriced)
    api.listEndpointPricing.mockResolvedValue(endpoints)
    api.getPriceHistory.mockResolvedValue({
      lineId: 'priceline_gpt4o',
      versions: [
        price({ id: 'price_new', origin: 'admin', status: 'upcoming', inputPerMillion: 2, effectiveFrom: '2026-11-01', recordedBy: 'adele', note: 'Negotiated rate', sources: [{ title: 'Entered by an administrator', url: 'https://contoso.example/agreement' }] }),
        price(),
        price({ id: 'price_old', status: 'past', inputPerMillion: 5, effectiveFrom: '2024-12-01', effectiveUntil: '2025-03-31' }),
      ],
    })
    api.addPrice.mockResolvedValue(price({ id: 'price_added', origin: 'admin' }))
    api.updateEndpointPricing.mockResolvedValue(endpoints[1])
  })

  it('lists each cloud’s prices with their sources', async () => {
    const user = userEvent.setup()
    renderPage()

    const table = await screen.findByRole('table', { name: 'Prices' })
    const row = within(within(table).getByText('gpt-4o').closest('tr') as HTMLElement)
    expect(row.getByText('$2.50')).toBeVisible()
    expect(row.getByText('$1.25')).toBeVisible()
    expect(row.getByText('$10.00')).toBeVisible()
    expect(row.getByText('Global Standard')).toBeVisible()
    expect(row.getByText('Changes Nov 1, 2026')).toBeVisible()
    expect(row.getByRole('link', { name: 'Azure Retail Prices API' })).toHaveAttribute('href', seedSource.url)
    expect(row.getByText('List price')).toBeVisible()
    const ptu = within(within(table).getByText('Every model').closest('tr') as HTMLElement)
    expect(ptu.getByText('$1.00 per PTU an hour')).toBeVisible()
    expect(screen.getByText('1 of 3 deployments priced today.')).toBeVisible()

    await user.selectOptions(screen.getByLabelText('Cloud'), 'government')
    await waitFor(() => expect(api.listPrices).toHaveBeenCalledWith('government'))
    expect(await screen.findByText('No prices for Azure Government yet')).toBeVisible()
  })

  it('shows a price’s history, newest first', async () => {
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('table', { name: 'Prices' })
    await user.click(screen.getByRole('button', { name: 'History (2)' }))

    const history = await screen.findByRole('table', { name: 'Price history' })
    const rows = within(history).getAllByRole('row').slice(1)
    expect(rows[0]).toHaveTextContent('Scheduled')
    expect(rows[0]).toHaveTextContent('Negotiated rate')
    expect(rows[1]).toHaveTextContent('In effect')
    expect(rows[1]).toHaveTextContent('Shipped with MOSAIC')
    // A seeded price a later seed replaced says when it ended.
    expect(rows[2]).toHaveTextContent('Superseded')
    expect(rows[2]).toHaveTextContent('Until Mar 31, 2025')
  })

  it('overrides a price from a date with a source and a note', async () => {
    const user = userEvent.setup()
    renderPage()

    const table = await screen.findByRole('table', { name: 'Prices' })
    await user.click(within(within(table).getByText('gpt-4o').closest('tr') as HTMLElement).getByRole('button', { name: 'Override' }))

    const dialog = await screen.findByRole('dialog', { name: 'Override gpt-4o' })
    // An override changes the amounts and the date, never which price it is.
    expect(within(dialog).getByRole('textbox', { name: /Model/ })).toBeDisabled()
    const input = within(dialog).getByRole('spinbutton', { name: /Input per 1M tokens/ })
    await user.clear(input)
    await user.type(input, '2')
    const effective = within(dialog).getByLabelText(/In effect from/)
    await user.clear(effective)
    await user.type(effective, '2026-11-01')
    await user.type(within(dialog).getByRole('textbox', { name: /Source URL/ }), 'https://contoso.example/agreement')
    await user.type(within(dialog).getByRole('textbox', { name: /Note/ }), 'Negotiated rate')
    await user.click(within(dialog).getByRole('button', { name: 'Save price' }))

    await waitFor(() => expect(api.addPrice).toHaveBeenCalled())
    expect(api.addPrice).toHaveBeenCalledWith(expect.objectContaining({
      cloud: 'commercial',
      model: 'gpt-4o',
      version: '2024-11-20',
      deploymentType: 'GlobalStandard',
      inputPerMillion: 2,
      outputPerMillion: 10,
      effectiveFrom: '2026-11-01',
      sourceUrl: 'https://contoso.example/agreement',
      note: 'Negotiated rate',
      overrides: 'commercial.openai.gpt-4o.2024-11-20.globalstandard',
    }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('reports a price the API refuses without closing the form', async () => {
    const user = userEvent.setup()
    api.addPrice.mockRejectedValue(new Error('A price for every model needs a publisher'))
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Add price' }))
    const dialog = await screen.findByRole('dialog', { name: 'Add a price' })
    await user.type(within(dialog).getByRole('textbox', { name: /Model/ }), '*')
    await user.type(within(dialog).getByRole('spinbutton', { name: /Input per 1M tokens/ }), '1')
    await user.type(within(dialog).getByRole('textbox', { name: /Source URL/ }), 'https://contoso.example/agreement')
    await user.type(within(dialog).getByRole('textbox', { name: /Note/ }), 'Every model')
    await user.click(within(dialog).getByRole('button', { name: 'Save price' }))

    expect(await within(dialog).findByText('A price for every model needs a publisher')).toBeVisible()
    expect(screen.getByRole('dialog')).toBeVisible()
  })

  it('lists unpriced deployments and how to fix each', async () => {
    const user = userEvent.setup()
    renderPage('/pricing?tab=unpriced')

    const table = await screen.findByRole('table', { name: 'Unpriced deployments' })
    expect(screen.getByText(/1 of 3 deployments MOSAIC knows has a price today/)).toBeVisible()
    const mistral = within(within(table).getByText('mistral').closest('tr') as HTMLElement)
    expect(mistral.getByText('No price listed')).toBeVisible()
    expect(mistral.getByText('800K')).toBeVisible()
    const claude = within(within(table).getByText('claude').closest('tr') as HTMLElement)
    expect(claude.getByText('Deployment type unknown')).toBeVisible()

    await user.click(mistral.getByRole('button', { name: 'Add price' }))
    const dialog = await screen.findByRole('dialog', { name: 'Price mistral' })
    expect(within(dialog).getByRole('textbox', { name: /Model/ })).toHaveValue('Mistral-Large-2411')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    await user.click(claude.getByRole('button', { name: 'Set facts' }))
    expect(await screen.findByRole('table', { name: 'Endpoint pricing' })).toBeVisible()
    expect(screen.getByLabelText('claude deployment type')).toBeVisible()
  })

  it('sets a declared deployment’s type and an endpoint’s cloud', async () => {
    const user = userEvent.setup()
    renderPage('/pricing?tab=endpoints')

    const table = await screen.findByRole('table', { name: 'Endpoint pricing' })
    const partner = within(within(table).getByText('Fabrikam partner Foundry').closest('tr') as HTMLElement)
    expect(partner.getByText('0 of 1')).toBeVisible()
    await user.click(partner.getByRole('button', { name: 'Edit' }))

    await user.selectOptions(screen.getByLabelText('Cloud'), 'government')
    await user.selectOptions(screen.getByLabelText('claude deployment type'), 'GlobalStandard')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.updateEndpointPricing).toHaveBeenCalledWith('endpoint-partner', {
      cloud: 'government',
      region: null,
      deployments: [{ deploymentName: 'claude', deploymentType: 'GlobalStandard', capacity: null }],
    }))
  })

  it('lists the seed’s sources', async () => {
    renderPage('/pricing?tab=sources')

    const sources = await screen.findByRole('list', { name: 'Price sources' })
    expect(within(sources).getByRole('link', { name: seedSource.title })).toHaveAttribute('href', seedSource.url)
    expect(within(sources).getAllByText('Retrieved Sep 30, 2026')).toHaveLength(2)
  })
})
