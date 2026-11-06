import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { PricingPage } from './PricingPage'
import type {
  BillingConnectionCheckResult,
  BillingConnectionView,
  BillingPublicationView,
  BillingRunDetailView,
  BillingRunSummaryView,
  BillingSourceTemplateView,
  EndpointPricingView,
  PriceListView,
  PriceView,
  PricingOverview,
  UnpricedReport,
} from '../types'

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
    version: 'etag-partner-1',
  },
]

const billingTemplates: BillingSourceTemplateView[] = [
  {
    provider: 'azure',
    title: 'Azure Cost Management CSV',
    schemaVersion: '1',
    supportedFormats: ['.csv'],
    requiredColumns: ['MeterCategory', 'MeterName', 'Quantity', 'CostInBillingCurrency', 'BillingCurrency'],
    optionalColumns: ['ResourceId', 'ResourceLocation'],
    sampleCsv: 'MeterCategory,MeterName,Quantity,CostInBillingCurrency,BillingCurrency\nAzure OpenAI,gpt-4o input,1000,2.10,USD',
    extractionQuery: null,
    notes: ['Export amortized cost details in USD for a completed calendar month.'],
  },
  {
    provider: 'gcp',
    title: 'GCP BigQuery Billing Export',
    schemaVersion: '1',
    supportedFormats: ['.csv', '.json', '.ndjson'],
    requiredColumns: ['service.description', 'sku.description', 'usage.amount_in_pricing_units', 'cost', 'currency'],
    optionalColumns: ['credits'],
    sampleCsv: null,
    extractionQuery: 'SELECT service.description, sku.description, cost, currency FROM `project.dataset.gcp_billing_export`',
    notes: ['Filter by usage_start_time in UTC and preserve credit signs.'],
  },
]

const billingConnections: BillingConnectionView[] = [
  {
    id: 'billconn_azure_1',
    name: 'Contoso EA Amortized',
    provider: 'azure',
    pricingCloud: 'commercial',
    pricingCloudLabel: 'Azure Commercial',
    azureCloud: 'commercial',
    azureConnectionId: null,
    azureScope: '/subscriptions/00000000-0000-0000-0000-000000000001',
    azureRetrievalMode: 'costDetailsApi',
    azureExportManifestUrl: null,
    hasStoredSecret: false,
    schedule: {
      enabled: true,
      dayOfMonth: 4,
      timeUtc: '05:00',
      timeZone: 'UTC',
    },
    status: 'ready',
    statusMessage: 'Verified read access to amortized cost details.',
    checkedAt: '2026-10-02T05:00:00Z',
    costNoticeAcknowledged: true,
    revision: 1,
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-02T05:00:00Z',
    updatedBy: 'adele',
    version: 'etag-conn-1',
  },
]

const billingRunSummary: BillingRunSummaryView = {
  id: 'billrun_sep_2026',
  connectionId: 'billconn_azure_1',
  connectionName: 'Contoso EA Amortized',
  provider: 'azure',
  pricingCloud: 'commercial',
  pricingCloudLabel: 'Azure Commercial',
  acquisitionKind: 'connected',
  sourceMonth: '2026-09',
  invoiceMonths: ['2026-09'],
  costBasis: 'contractAmortized',
  currency: 'USD',
  derivationVersion: '1',
  status: 'readyForReview',
  statusMessage: 'Normalized 18 eligible row(s) and derived 2 candidate rate(s).',
  sourceScope: '/subscriptions/00000000-0000-0000-0000-000000000001',
  sourceFileName: 'azure-amortized-2026-09.csv',
  sourceManifestId: null,
  contentFingerprint: 'a1b2c3d4e5f60718293a4b5c6d7e8f90',
  duplicateOfRunId: null,
  startedAt: '2026-10-03T04:00:00Z',
  completedAt: '2026-10-03T04:01:00Z',
  publishedAt: null,
  publicationId: null,
  rawRowCount: 20,
  eligibleRowCount: 18,
  excludedRowCount: 2,
  totalReadBytes: 16384,
  totalEligibleCostUsd: 1420.5,
  totalExcludedCostUsd: 85.0,
  evidenceChunkCount: 1,
  evidenceItemCount: 2,
  candidateCount: 2,
  publishableCandidateCount: 1,
  blockerCount: 0,
  warningCount: 1,
  previewRevision: 1,
  createdBy: 'adele',
  publishedBy: null,
  version: 'etag-run-1',
}

const billingRunDetail: BillingRunDetailView = {
  ...billingRunSummary,
  effectiveFromDefault: '2026-10-29',
  candidates: [
    {
      candidateId: 'cand_gpt4o',
      provider: 'azure',
      pricingCloud: 'commercial',
      sourceScope: '/subscriptions/00000000-0000-0000-0000-000000000001',
      sourceAccountId: null,
      sourceResourceId: '/subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/rg-ai/providers/Microsoft.CognitiveServices/accounts/contoso-aoai',
      sourceResourceName: 'contoso-aoai',
      publisher: 'OpenAI',
      model: 'gpt-4o',
      version: '2024-11-20',
      deploymentType: 'GlobalStandard',
      region: 'eastus2',
      serviceTier: null,
      routingMode: null,
      inputTokens: 400_000_000,
      cachedInputTokens: 100_000_000,
      cacheWriteTokens: null,
      outputTokens: 60_000_000,
      ptuHours: null,
      commitmentMonths: null,
      inputCostUsd: 800,
      cachedInputCostUsd: 100,
      cacheWriteCostUsd: null,
      outputCostUsd: 520.5,
      ptuCostUsd: null,
      monthlyCommitmentCostUsd: null,
      totalEligibleCostUsd: 1420.5,
      excludedCostUsd: 0,
      inputPerMillion: 2.0,
      cachedInputPerMillion: 1.0,
      outputPerMillion: 8.675,
      ptuHourly: null,
      monthlyAmount: null,
      publishable: true,
      unpublishableReason: null,
      suggestedScope: 'deployment',
      suggestedDeploymentKey: 'endpoint-aoai/chat',
      ambiguousDeploymentKeys: ['endpoint-aoai/chat', 'endpoint-partner/chat-secondary'],
      mappingConfidence: 'ambiguous',
      existingPriceId: 'price_manual_gpt4o',
      existingLineId: 'priceline_gpt4o',
      existingOrigin: 'admin',
      existingInputPerMillion: 2.5,
      existingCachedInputPerMillion: 1.25,
      existingOutputPerMillion: 10,
      existingPtuHourly: null,
      existingMonthlyAmount: null,
      conflictsWithManualOverride: true,
      warnings: ['Candidate rate for gpt-4o overlaps an administrator manual price.'],
      evidenceLineKeys: ['line_gpt4o_in', 'line_gpt4o_out'],
    },
    {
      candidateId: 'cand_partial_embed',
      provider: 'azure',
      pricingCloud: 'commercial',
      sourceScope: '/subscriptions/00000000-0000-0000-0000-000000000001',
      sourceAccountId: null,
      sourceResourceId: null,
      sourceResourceName: 'contoso-aoai',
      publisher: 'OpenAI',
      model: 'gpt-4.1',
      version: '2025-04-14',
      deploymentType: 'GlobalStandard',
      region: 'eastus2',
      serviceTier: null,
      routingMode: null,
      inputTokens: 10_000_000,
      cachedInputTokens: null,
      cacheWriteTokens: null,
      outputTokens: null,
      ptuHours: null,
      commitmentMonths: null,
      inputCostUsd: 15,
      cachedInputCostUsd: null,
      cacheWriteCostUsd: null,
      outputCostUsd: null,
      ptuCostUsd: null,
      monthlyCommitmentCostUsd: null,
      totalEligibleCostUsd: 15,
      excludedCostUsd: 0,
      inputPerMillion: 1.5,
      cachedInputPerMillion: null,
      outputPerMillion: null,
      ptuHourly: null,
      monthlyAmount: null,
      publishable: false,
      unpublishableReason: 'Missing output token usage; partial candidate cannot replace a complete model price.',
      suggestedScope: 'excluded',
      suggestedDeploymentKey: null,
      ambiguousDeploymentKeys: [],
      mappingConfidence: 'unmatched',
      existingPriceId: null,
      existingLineId: null,
      existingOrigin: null,
      existingInputPerMillion: null,
      existingCachedInputPerMillion: null,
      existingOutputPerMillion: null,
      existingPtuHourly: null,
      existingMonthlyAmount: null,
      conflictsWithManualOverride: false,
      warnings: [],
      evidenceLineKeys: [],
    },
  ],
  mappings: [
    {
      candidateId: 'cand_gpt4o',
      scope: 'deployment',
      deploymentKey: 'endpoint-aoai/chat',
      cloud: 'commercial',
      confirmed: false,
      replaceManualOverride: false,
    },
    {
      candidateId: 'cand_partial_embed',
      scope: 'excluded',
      deploymentKey: null,
      cloud: 'commercial',
      confirmed: false,
      replaceManualOverride: false,
    },
  ],
  issues: [
    {
      code: 'cachedTokensObserved',
      severity: 'warning',
      message: 'Cached input tokens were observed in billing data; verify gateway telemetry captures cached prompt tokens.',
      model: 'gpt-4o',
      region: 'eastus2',
    },
  ],
  evidenceItems: [
    {
      lineKey: 'line_gpt4o_in',
      provider: 'azure',
      sourceResourceName: 'contoso-aoai',
      skuDescription: 'GPT-4o Global Standard Input',
      model: 'gpt-4o',
      modelVersion: '2024-11-20',
      region: 'eastus2',
      deploymentType: 'GlobalStandard',
      usageCategory: 'inputTokens',
      rawUsageUnit: '1K Tokens',
      rawQuantity: 400_000,
      normalizedQuantity: 400_000_000,
      normalizedUnit: 'tokens',
      eligibleCostUsd: 800,
      amortizedBenefitCostUsd: 0,
      excludedCostUsd: 85,
      excludedReason: 'Tax line item excluded',
      rowCount: 10,
      currency: 'USD',
      notes: [],
    },
  ],
}

const billingPublication: BillingPublicationView = {
  id: 'billpub_sep_2026',
  runId: 'billrun_sep_2026',
  connectionId: 'billconn_azure_1',
  provider: 'azure',
  pricingCloud: 'commercial',
  pricingCloudLabel: 'Azure Commercial',
  sourceMonth: '2026-09',
  effectiveFrom: '2026-10-01',
  backdated: true,
  priceVersionIds: ['price_bill_gpt4o'],
  affectedDeploymentKeys: ['endpoint-aoai/chat'],
  replacedManualOverrideIds: ['price_manual_gpt4o'],
  note: 'Approved September billing rates',
  publishedBy: 'adele',
  publishedAt: '2026-10-29T05:00:00Z',
}

const api = {
  getPricingOverview: vi.fn(),
  listPrices: vi.fn(),
  addPrice: vi.fn(),
  getPriceHistory: vi.fn(),
  getUnpricedDeployments: vi.fn(),
  listEndpointPricing: vi.fn(),
  updateEndpointPricing: vi.fn(),
  listBillingTemplates: vi.fn(),
  importBillingData: vi.fn(),
  listBillingRuns: vi.fn(),
  getBillingRun: vi.fn(),
  updateBillingMappings: vi.fn(),
  publishBillingRun: vi.fn(),
  pollBillingRun: vi.fn(),
  listBillingPublications: vi.fn(),
  listBillingConnections: vi.fn(),
  getBillingConnection: vi.fn(),
  createBillingConnection: vi.fn(),
  updateBillingConnection: vi.fn(),
  deleteBillingConnection: vi.fn(),
  checkBillingConnection: vi.fn(),
  startBillingConnectionRun: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

function LocationState() {
  const location = useLocation()
  return <output aria-label="Current query">{location.search}</output>
}

function renderPage(initial = '/pricing') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <PricingPage />
        <LocationState />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

// Pasting a long value is one input event rather than one per character, which keeps the form
// tests quick when the whole suite runs at once.
async function fill(user: ReturnType<typeof userEvent.setup>, field: HTMLElement, text: string) {
  await user.click(field)
  await user.paste(text)
}

describe('PricingPage', { timeout: 15000 }, () => {
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
    api.listBillingTemplates.mockResolvedValue(billingTemplates)
    api.listBillingConnections.mockResolvedValue(billingConnections)
    api.getBillingConnection.mockResolvedValue(billingConnections[0])
    api.createBillingConnection.mockResolvedValue(billingConnections[0])
    api.updateBillingConnection.mockResolvedValue(billingConnections[0])
    api.deleteBillingConnection.mockResolvedValue(undefined)
    api.checkBillingConnection.mockResolvedValue({
      connectionId: 'billconn_azure_1',
      provider: 'azure',
      status: 'ready',
      message: 'Read-only access to amortized cost details verified.',
      checkedAt: '2026-10-29T05:00:00Z',
      scopeSummary: '/subscriptions/00000000-0000-0000-0000-000000000001',
      supportedSourceMonths: ['2026-09', '2026-08'],
      warnings: [],
    } satisfies BillingConnectionCheckResult)
    api.listBillingRuns.mockResolvedValue([billingRunSummary])
    api.getBillingRun.mockResolvedValue(billingRunDetail)
    api.importBillingData.mockResolvedValue(billingRunDetail)
    api.updateBillingMappings.mockImplementation(async (_runId, payload) => ({
      ...billingRunDetail,
      mappings: payload.mappings,
      previewRevision: 2,
      version: 'etag-run-2',
    }))
    api.publishBillingRun.mockResolvedValue(billingPublication)
    api.pollBillingRun.mockResolvedValue(billingRunDetail)
    api.listBillingPublications.mockResolvedValue([billingPublication])
    api.startBillingConnectionRun.mockResolvedValue(billingRunDetail)
  })

  it('navigates real sections while retaining cloud filters and clearing endpoint focus', async () => {
    const user = userEvent.setup()
    renderPage('/pricing?tab=endpoints&endpoint=endpoint-partner&cloud=government&extra=kept')
    await screen.findByRole('table', { name: 'Endpoint pricing' })
    await user.click(screen.getByRole('button', { name: 'Sources' }))
    expect(screen.queryByRole('table', { name: 'Endpoint pricing' })).not.toBeInTheDocument()
    expect(await screen.findByRole('list', { name: 'Price sources' })).toBeVisible()
    const query = new URLSearchParams(screen.getByLabelText('Current query').textContent ?? '')
    expect(query.get('tab')).toBe('sources')
    expect(query.get('endpoint')).toBeNull()
    expect(query.get('cloud')).toBe('government')
    expect(query.get('extra')).toBe('kept')
    await user.click(screen.getByRole('button', { name: 'Prices' }))
    await waitFor(() => expect(api.listPrices).toHaveBeenCalledWith('government'))
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
    await user.click(screen.getByRole('button', { name: 'More actions for gpt-4o' }))
    await user.click(await screen.findByRole('menuitem', { name: 'History (2)' }))

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
    await fill(user, within(dialog).getByRole('textbox', { name: /Source URL/ }), 'https://contoso.example/agreement')
    await fill(user, within(dialog).getByRole('textbox', { name: /Note/ }), 'Negotiated rate')
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
    await fill(user, within(dialog).getByRole('textbox', { name: /Source URL/ }), 'https://contoso.example/agreement')
    await fill(user, within(dialog).getByRole('textbox', { name: /Note/ }), 'Every model')
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

    // The page behind a modal stays hidden until the dialog has finished closing.
    const unpricedTable = await screen.findByRole('table', { name: 'Unpriced deployments' }, { timeout: 5000 })
    await user.click(await within(unpricedTable).findByRole('button', { name: 'Set facts' }, { timeout: 5000 }))
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
      // The version the form opened on, so a save over another administrator's change fails.
      version: 'etag-partner-1',
    }))
  })

  it('does not suggest adding prices to fix unknown inventory or deployment dates', async () => {
    api.getUnpricedDeployments.mockResolvedValue({
      ...unpriced,
      rows: [
        { ...unpriced.rows[0], key: 'unknown', label: 'Unknown model', model: null, reason: 'noModel' },
        { ...unpriced.rows[0], key: 'before', label: 'Earlier usage', reason: 'beforeDeployment' },
        { ...unpriced.rows[0], key: 'imported', label: 'Imported API', kind: 'api', reason: 'unknownDeployment' },
      ],
    })
    renderPage('/pricing?tab=unpriced')
    const table = await screen.findByRole('table', { name: 'Unpriced deployments' })
    expect(within(table).queryByRole('button', { name: 'Add price' })).not.toBeInTheDocument()
    expect(within(table).getByRole('link', { name: 'Review endpoint' })).toHaveAttribute('href', '/models?tab=endpoints')
    expect(within(table).getByRole('link', { name: 'Review imported API' })).toHaveAttribute('href', '/models?tab=imported')
    expect(within(table).getByText(/Adding a price will not fix this date mismatch/)).toBeVisible()
  })

  it('never saves a form opened before someone else’s change, and can load their change', async () => {
    const user = userEvent.setup()
    api.updateEndpointPricing.mockRejectedValue(
      new ApiError('Someone changed this endpoint’s pricing after you opened it. Reload it and make your change again.', 409),
    )
    renderPage('/pricing?tab=endpoints')

    const table = await screen.findByRole('table', { name: 'Endpoint pricing' })
    const partner = within(within(table).getByText('Fabrikam partner Foundry').closest('tr') as HTMLElement)
    await user.click(partner.getByRole('button', { name: 'Edit' }))
    // Another administrator sets claude's type while this form is open.
    const theirs = endpoints.map((item) => item.endpointId === 'endpoint-partner'
      ? {
          ...item,
          version: 'etag-partner-2',
          deployments: [{ ...item.deployments[0], deploymentType: 'GlobalStandard', deploymentTypeSource: 'admin' as const, priced: true, reason: undefined }],
        }
      : item)
    api.listEndpointPricing.mockResolvedValue(theirs)
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(await screen.findByText(/Someone changed this endpoint’s pricing after you opened it/)).toBeVisible()
    // Saving again still sends the version the form came from, so it can't undo their change.
    const reload = await screen.findByRole('button', { name: 'Load the latest' })
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateEndpointPricing).toHaveBeenCalledTimes(2))
    expect(api.updateEndpointPricing.mock.calls.map((call) => call[1].version)).toEqual(['etag-partner-1', 'etag-partner-1'])

    await user.click(reload)
    expect(screen.getByLabelText('claude deployment type')).toHaveValue('GlobalStandard')
    api.updateEndpointPricing.mockResolvedValue(theirs[1])
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.updateEndpointPricing).toHaveBeenLastCalledWith('endpoint-partner', expect.objectContaining({ version: 'etag-partner-2' })))
  })

  it('lists the seed’s sources', async () => {
    renderPage('/pricing?tab=sources')

    const sources = await screen.findByRole('list', { name: 'Price sources' })
    expect(within(sources).getByRole('link', { name: seedSource.title })).toHaveAttribute('href', seedSource.url)
    expect(within(sources).getAllByText('Retrieved Sep 30, 2026')).toHaveLength(2)
  })

  it('navigates to Billing data, checks a connection, and warns when scheduled host is not always-on', async () => {
    const user = userEvent.setup()
    api.getPricingOverview.mockResolvedValue({
      ...overview,
      pricingBasis: 'billingDerived',
      lastBillingSourceMonth: '2026-09',
      pendingBillingReviews: 1,
      billingSchedulerAlwaysOn: false,
    })
    renderPage('/pricing?tab=billing')

    expect(await screen.findByText(/US dollars from published billing-derived rates \(2026-09\)/)).toBeVisible()
    expect(await screen.findByText(/Scheduled monthly draft refresh requires an always-on API host/)).toBeVisible()

    const connTable = await screen.findByRole('table', { name: 'Connected billing sources' })
    const connRow = within(within(connTable).getByText('Contoso EA Amortized').closest('tr') as HTMLElement)
    expect(connRow.getByText('Ready')).toBeVisible()
    expect(connRow.getByText(/Day 4 at 05:00 UTC/)).toBeVisible()

    await user.click(connRow.getByRole('button', { name: 'Check access' }))
    await waitFor(() => expect(api.checkBillingConnection).toHaveBeenCalledWith('billconn_azure_1'))
    expect(await screen.findByText(/Read-only access to amortized cost details verified/)).toBeVisible()

    const runsTable = await screen.findByRole('table', { name: 'Billing runs' })
    const runRow = within(within(runsTable).getByText('2026-09').closest('tr') as HTMLElement)
    expect(runRow.getByText('Ready for review')).toBeVisible()
    expect(runRow.getByText('$1,420.50')).toBeVisible()
  })

  it('opens the full-workspace Import billing file flow, imports data, and transitions to Review & publish', async () => {
    const user = userEvent.setup()
    renderPage('/pricing?tab=billing')

    await user.click(await screen.findByRole('button', { name: 'Import billing file' }))
    expect(await screen.findByRole('heading', { name: 'Import billing file' })).toBeVisible()

    // Template guidance is loaded for the selected provider, and sample CSV can populate the form.
    expect(await screen.findByText(/Azure Cost Management CSV schema & guidance/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Use sample CSV' }))

    const monthInput = screen.getByRole('textbox', { name: /Source month/ })
    await user.clear(monthInput)
    await user.type(monthInput, '2026-09')

    await user.click(screen.getByRole('button', { name: 'Import & review' }))
    await waitFor(() => expect(api.importBillingData).toHaveBeenCalledWith(expect.objectContaining({
      provider: 'azure',
      pricingCloud: 'commercial',
      sourceMonth: '2026-09',
      fileName: 'upload.csv',
    })))

    expect(await screen.findByRole('heading', { name: 'Review billing run (2026-09)' })).toBeVisible()
    const query = new URLSearchParams(screen.getByLabelText('Current query').textContent ?? '')
    expect(query.get('run')).toBe('billrun_sep_2026')
  })

  it('reviews candidate rates, requires ambiguous & manual-override & backdate confirmations, saves mappings, and publishes rates', async () => {
    const user = userEvent.setup()
    renderPage('/pricing?tab=billing&run=billrun_sep_2026')

    expect(await screen.findByRole('heading', { name: 'Review billing run (2026-09)' })).toBeVisible()
    const candidateTable = await screen.findByRole('table', { name: 'Candidate rates' })
    expect(within(candidateTable).getByText('gpt-4o')).toBeVisible()
    expect(within(candidateTable).getByText(/\$2\.50 in/)).toBeVisible()
    expect(within(candidateTable).getByText(/\$2\.00 in/)).toBeVisible()
    expect(within(candidateTable).getByText('Partial / unpublishable')).toBeVisible()
    expect(within(candidateTable).getByText(/Missing output token usage/)).toBeVisible()

    // Normalized evidence is shown as well.
    const evidenceTable = await screen.findByRole('table', { name: 'Normalized billing evidence' })
    expect(within(evidenceTable).getByText('line_gpt4o_in')).toBeVisible()
    expect(within(evidenceTable).getByText(/Tax line item excluded/)).toBeVisible()

    const publishBtn = screen.getByRole('button', { name: 'Publish rates' })
    // Disabled until ambiguous deployment mapping and manual override conflict are confirmed.
    expect(publishBtn).toBeDisabled()

    await user.click(screen.getByRole('checkbox', { name: 'Confirm mapping for gpt-4o' }))
    await user.click(screen.getByRole('checkbox', { name: 'Replace manual override for gpt-4o' }))
    await user.click(screen.getByRole('button', { name: 'Save mappings' }))

    await waitFor(() => expect(api.updateBillingMappings).toHaveBeenCalledWith('billrun_sep_2026', expect.objectContaining({
      previewRevision: 1,
      version: 'etag-run-1',
      mappings: expect.arrayContaining([
        expect.objectContaining({
          candidateId: 'cand_gpt4o',
          scope: 'deployment',
          deploymentKey: 'endpoint-aoai/chat',
          confirmed: true,
          replaceManualOverride: true,
        }),
      ]),
    })))

    // Confirm replacing overlapping administrator manual price overrides in Publication controls.
    await user.click(screen.getByRole('checkbox', { name: /I confirm replacing overlapping administrator manual price overrides/ }))
    expect(publishBtn).toBeEnabled()

    // Backdate effectiveFrom earlier than effectiveFromDefault (2026-10-29) -> requires backdate confirmation.
    const effectiveInput = screen.getByLabelText('Effective from')
    await user.clear(effectiveInput)
    await user.type(effectiveInput, '2026-10-01')
    expect(await screen.findByText(/Backdating changes historical reports and budget status/)).toBeVisible()
    expect(publishBtn).toBeDisabled()

    await user.click(screen.getByRole('checkbox', { name: /I understand backdating changes historical cost reports and budget calculations/ }))
    await fill(user, screen.getByLabelText('Publication note'), 'Approved September billing rates')
    expect(publishBtn).toBeEnabled()

    await user.click(publishBtn)
    await waitFor(() => expect(api.publishBillingRun).toHaveBeenCalledWith('billrun_sep_2026', {
      effectiveFrom: '2026-10-01',
      acknowledgeBackdateImpact: true,
      acknowledgeManualOverrideReplacement: true,
      note: 'Approved September billing rates',
      previewRevision: 2,
      version: 'etag-run-2',
    }))
    expect(await screen.findByText(/Billing-derived rates published/)).toBeVisible()
  })

  it('creates a billing connection in WorkspaceEditorFrame and recovers from 409 stale preview during review', async () => {
    const user = userEvent.setup()
    api.createBillingConnection.mockRejectedValueOnce(new Error('Missing Cost Management Reader permission on scope'))
    renderPage('/pricing?tab=billing')

    await user.click(await screen.findByRole('button', { name: 'New billing connection' }))
    expect(await screen.findByRole('heading', { name: 'New billing connection' })).toBeVisible()

    const createBtn = screen.getByRole('button', { name: 'Create connection' })
    expect(createBtn).toBeDisabled()

    await fill(user, screen.getByRole('textbox', { name: /Connection name/ }), 'AWS Bedrock CUR')
    await user.selectOptions(screen.getByRole('combobox', { name: /Billing provider/ }), 'aws')
    await fill(user, screen.getByRole('textbox', { name: /IAM Role ARN/ }), 'arn:aws:iam::123456789012:role/MosaicBillingReadRole')
    await fill(user, screen.getByRole('textbox', { name: /S3 CUR Bucket/ }), 'contoso-cur-bucket')
    await user.click(screen.getByRole('checkbox', { name: /I acknowledge that connected billing queries/ }))
    expect(createBtn).toBeEnabled()

    await user.click(createBtn)
    expect(await screen.findByText('Missing Cost Management Reader permission on scope')).toBeVisible()

    api.createBillingConnection.mockResolvedValueOnce({
      ...billingConnections[0],
      id: 'billconn_aws_1',
      name: 'AWS Bedrock CUR',
      provider: 'aws',
      pricingCloud: 'aws',
      pricingCloudLabel: 'aws',
    })
    await user.click(createBtn)
    await waitFor(() => expect(api.createBillingConnection).toHaveBeenCalledTimes(2))
    expect(await screen.findByRole('table', { name: 'Connected billing sources' })).toBeVisible()

    // Open Review & publish and verify 409 Conflict stale preview recovery.
    api.updateBillingMappings.mockRejectedValueOnce(
      new ApiError('The billing preview revision is stale; reload the run and review the latest candidates before saving mappings.', 409),
    )
    await user.click(screen.getByRole('button', { name: 'Review & publish' }))
    expect(await screen.findByRole('heading', { name: 'Review billing run (2026-09)' })).toBeVisible()

    api.getBillingRun.mockResolvedValue({
      ...billingRunDetail,
      previewRevision: 4,
      version: 'etag-run-4',
    })
    await user.click(screen.getByRole('button', { name: 'Save mappings' }))
    expect(await screen.findByText(/The billing preview revision is stale/)).toBeVisible()

    const reloadBtn = await screen.findByRole('button', { name: 'Reload latest preview' })
    await user.click(reloadBtn)
    await waitFor(() => expect(api.getBillingRun).toHaveBeenCalled())

    api.updateBillingMappings.mockResolvedValueOnce({
      ...billingRunDetail,
      previewRevision: 5,
      version: 'etag-run-5',
    })
    await user.click(screen.getByRole('button', { name: 'Save mappings' }))
    await waitFor(() => expect(api.updateBillingMappings).toHaveBeenLastCalledWith('billrun_sep_2026', expect.objectContaining({
      previewRevision: 4,
      version: 'etag-run-4',
    })))
  })

  it('renders billing-derived price origin and internal evidence links in Prices and Price history', async () => {
    const user = userEvent.setup()
    const billingPrice = price({
      id: 'price_billing_gpt4o',
      origin: 'billing',
      sourceMonth: '2026-09',
      billingRunId: 'billrun_sep_2026',
      inputPerMillion: 2.0,
      cachedInputPerMillion: 1.0,
      outputPerMillion: 8.675,
      sources: [
        {
          title: 'Azure billing (2026-09)',
          url: null,
          reference: 'mosaic://pricing/billing-runs/billrun_sep_2026#candidate=cand_gpt4o',
          kind: 'billingRun',
          runId: 'billrun_sep_2026',
          sourceMonth: '2026-09',
        },
      ],
    })
    api.listPrices.mockResolvedValue({
      ...commercial,
      lines: [
        {
          lineId: 'priceline_gpt4o',
          current: billingPrice,
          upcoming: null,
          versions: 2,
        },
      ],
    })
    api.getPriceHistory.mockResolvedValue({
      lineId: 'priceline_gpt4o',
      versions: [billingPrice, price()],
    })

    renderPage()
    const table = await screen.findByRole('table', { name: 'Prices' })
    const row = within(within(table).getByText('gpt-4o').closest('tr') as HTMLElement)
    expect(row.getByText('From 2026-09 billing')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'More actions for gpt-4o' }))
    await user.click(await screen.findByRole('menuitem', { name: 'History (2)' }))
    const history = await screen.findByRole('table', { name: 'Price history' })
    expect(within(history).getByText('From 2026-09 billing')).toBeVisible()

    // Clicking the internal evidence reference navigates to the Billing data review workspace.
    await user.click(row.getByRole('button', { name: 'Azure billing (2026-09)' }))
    expect(await screen.findByRole('heading', { name: 'Review billing run (2026-09)' })).toBeVisible()
    const query = new URLSearchParams(screen.getByLabelText('Current query').textContent ?? '')
    expect(query.get('tab')).toBe('billing')
    expect(query.get('run')).toBe('billrun_sep_2026')
  })
})
