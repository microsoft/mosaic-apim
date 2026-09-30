import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { MyUsageReport, UsageResourceRow } from '../types'
import { UsagePage } from './UsagePage'

const mocks = vi.hoisted(() => ({
  api: {} as PortalApi,
}))

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {
    status = 500
  },
  usePortalApi: () => mocks.api,
}))

const environments = [
  { key: 'development', displayName: 'Development', description: null, color: 'brand', production: false, order: 10 },
  { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, order: 50 },
]

function resourceRow(overrides: Partial<UsageResourceRow>): UsageResourceRow {
  return {
    entitlementId: 'grant-model',
    resource: { kind: 'modelApi', id: 'chat', scopeId: null },
    resourceSummary: {
      kind: 'modelApi',
      id: 'chat',
      scopeId: null,
      displayName: 'Chat completions',
      gatewayId: 'gateway-1',
      gatewayName: 'Production gateway',
      environment: 'production',
      available: true,
    },
    environment: 'production',
    via: 'direct',
    viaGroupName: null,
    enabled: true,
    bound: true,
    attribution: 'simulated',
    model: 'gpt-4o',
    requests: 100,
    promptTokens: 8_000,
    completionTokens: 4_000,
    totalTokens: 12_000,
    estimatedCost: 1.23,
    costNote: null,
    quotas: [
      {
        metric: 'tokens',
        limit: 50_000,
        period: 'Monthly',
        windowStart: '2026-09-01T00:00:00Z',
        windowEnd: '2026-10-01T00:00:00Z',
        used: 12_000,
        utilization: 0.24,
      },
    ],
    rateLimits: [{ metric: 'tokens', limit: 10_000, windowSeconds: 60 }],
    ...overrides,
  }
}

function usageReport(overrides: Partial<MyUsageReport> = {}): MyUsageReport {
  const rows = [
    resourceRow({}),
    resourceRow({
      entitlementId: 'grant-mcp',
      resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: null },
      resourceSummary: {
        kind: 'mcpServer',
        id: 'docs-mcp',
        scopeId: null,
        displayName: 'Docs MCP',
        gatewayId: 'gateway-1',
        gatewayName: 'Production gateway',
        environment: null,
        available: true,
      },
      environment: null,
      requests: 20,
      promptTokens: null,
      completionTokens: null,
      totalTokens: null,
      estimatedCost: null,
      costNote: 'MCP server costs are not metered.',
      bound: false,
      quotas: [],
      rateLimits: [{ metric: 'requests', limit: 60, windowSeconds: 60 }],
    }),
    resourceRow({
      entitlementId: 'grant-unattributed',
      resourceSummary: {
        kind: 'modelApi',
        id: 'dev-chat',
        scopeId: null,
        displayName: 'Development chat',
        gatewayId: 'gateway-2',
        gatewayName: 'Development gateway',
        environment: 'development',
        available: true,
      },
      environment: 'development',
      attribution: 'unattributed',
      bound: false,
      requests: null,
      promptTokens: null,
      completionTokens: null,
      totalTokens: null,
      estimatedCost: null,
      costNote: 'Grant is not bound.',
    }),
    resourceRow({
      entitlementId: 'grant-disabled',
      resourceSummary: {
        kind: 'modelApi',
        id: 'retired-chat',
        scopeId: null,
        displayName: 'Retired chat',
        gatewayId: 'gateway-1',
        gatewayName: 'Production gateway',
        environment: 'production',
        available: false,
      },
      enabled: false,
      requests: 0,
      promptTokens: 0,
      completionTokens: 0,
      totalTokens: 0,
      estimatedCost: 0,
    }),
  ]
  return {
    dataSource: 'simulated',
    period: '30d',
    start: '2026-09-01',
    end: '2026-09-02',
    generatedAt: '2026-09-02T12:00:00Z',
    currency: 'USD',
    totals: {
      requests: 120,
      promptTokens: 8_000,
      completionTokens: 4_000,
      totalTokens: 12_000,
      estimatedCost: 1.23,
      costExcludedResources: 2,
    },
    timeline: [
      {
        date: '2026-09-01',
        entitlementId: 'grant-model',
        environment: 'production',
        requests: 40,
        promptTokens: 3_000,
        completionTokens: 1_000,
        totalTokens: 4_000,
        estimatedCost: 0.4,
      },
      {
        date: '2026-09-02',
        entitlementId: 'grant-model',
        environment: 'production',
        requests: 60,
        promptTokens: 5_000,
        completionTokens: 3_000,
        totalTokens: 8_000,
        estimatedCost: 0.83,
      },
      {
        date: '2026-09-02',
        entitlementId: 'grant-mcp',
        environment: null,
        requests: 20,
        promptTokens: null,
        completionTokens: null,
        totalTokens: null,
        estimatedCost: null,
      },
      {
        date: '2026-09-02',
        entitlementId: 'grant-unattributed',
        environment: 'development',
        requests: null,
        promptTokens: null,
        completionTokens: null,
        totalTokens: null,
        estimatedCost: null,
      },
    ],
    byEnvironment: [
      {
        environment: 'production',
        resources: 2,
        requests: 100,
        promptTokens: 8_000,
        completionTokens: 4_000,
        totalTokens: 12_000,
        estimatedCost: 1.23,
        costExcludedResources: 0,
      },
      {
        environment: null,
        resources: 1,
        requests: 20,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        estimatedCost: null,
        costExcludedResources: 1,
      },
      {
        environment: 'development',
        resources: 1,
        requests: 0,
        promptTokens: 0,
        completionTokens: 0,
        totalTokens: 0,
        estimatedCost: null,
        costExcludedResources: 1,
      },
    ],
    byResource: rows,
    notes: [
      "Figures are simulated from your real grants and limits.",
      "Costs are estimates at illustrative rates and aren't a bill.",
    ],
    ...overrides,
  }
}

function renderPage(report: MyUsageReport | Error = usageReport()) {
  const getMyUsage = vi.fn(async () => {
    if (report instanceof Error) throw report
    return report
  })
  mocks.api = {
    getMyUsage,
    listEnvironments: async () => environments,
  } as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <UsagePage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { getMyUsage }
}

async function findResourceRow(name: string) {
  const matches = await screen.findAllByText(name)
  const row = matches.map((match) => match.closest('tr')).find(Boolean)
  expect(row).toBeTruthy()
  return row as HTMLElement
}

describe('UsagePage', () => {
  it('shows the sample data badge and notice for simulated data', async () => {
    renderPage()

    expect((await screen.findAllByText('Sample data')).length).toBeGreaterThan(0)
    expect(screen.getByText('Figures are simulated from your real grants and limits.')).toBeVisible()
    expect(screen.getByText("Costs are estimates at illustrative rates and aren't a bill.")).toBeVisible()
  })

  it('does not show sample data messaging for Log Analytics data', async () => {
    renderPage(usageReport({ dataSource: 'logAnalytics', notes: [] }))

    expect(await screen.findByRole('table', { name: 'Usage by resource' })).toBeVisible()
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
  })

  it('renders KPI values, unknown cost, and excluded-resource captions', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByText('120')).toBeVisible()
    expect(screen.getAllByText('$1.23')[0]).toBeVisible()
    expect(screen.getByText('Excludes 2 resources with unknown cost')).toBeVisible()

    await user.selectOptions(screen.getByLabelText('Resource'), 'grant-mcp')
    expect(screen.getAllByText('20')[0]).toBeVisible()
    expect(screen.getAllByText('Unknown')[0]).toBeVisible()
    expect(screen.getByText('Excludes 1 resource with unknown cost')).toBeVisible()
  })

  it('renders MCP null token fields as not metered instead of zero', async () => {
    renderPage()

    const row = await findResourceRow('Docs MCP')
    expect(within(row).getByText('Not metered')).toBeVisible()
    expect(within(row).queryByText(/^0$/)).not.toBeInTheDocument()
  })

  it('explains unattributed and unbound grants', async () => {
    renderPage()

    const row = await findResourceRow('Development chat')
    expect(within(row).getByText(/Usage can't be attributed until this grant is bound/)).toBeVisible()
    expect(within(row).getByText('Not linked yet')).toBeVisible()
  })

  it('summarizes each environment with its cost and resource count', async () => {
    renderPage()

    expect(await screen.findByText('100 requests · 12,000 tokens · $1.23 · 2 resources')).toBeVisible()
    expect(screen.getByText('0 requests · 0 tokens · cost unknown · 1 resource')).toBeVisible()
  })

  it('renders quota period text, rate limits, disabled badge, and removed resource badge', async () => {
    renderPage()

    expect((await screen.findAllByText('Tokens: 12,000 of 50,000 this month'))[0]).toBeVisible()
    expect(screen.getAllByText('10,000 tokens per minute')[0]).toBeVisible()
    expect(screen.getByText('60 requests per 60 seconds')).toBeVisible()
    expect(screen.getByText('Disabled')).toBeVisible()
    const row = await findResourceRow('Retired chat')
    expect(within(row).getByText('No longer available')).toBeVisible()
  })

  it("refetches when the period changes to 7 days", async () => {
    const user = userEvent.setup()
    const { getMyUsage } = renderPage()

    await screen.findByRole('table', { name: 'Usage by resource' })
    await user.selectOptions(screen.getByLabelText('Period'), '7d')

    await waitFor(() => expect(getMyUsage).toHaveBeenCalledWith('7d'))
  })

  it('recomputes KPIs for environment and resource filters', async () => {
    const user = userEvent.setup()
    renderPage()

    await screen.findByRole('table', { name: 'Usage by resource' })
    await user.selectOptions(screen.getByLabelText('Environment'), 'unclassified')
    let table = screen.getByRole('table', { name: 'Usage by resource' })
    expect(within(table).getByText('Docs MCP')).toBeVisible()
    expect(within(table).queryByText('Chat completions')).not.toBeInTheDocument()
    expect(screen.getAllByText('20')[0]).toBeVisible()

    await user.selectOptions(screen.getByLabelText('Environment'), 'all')
    await user.selectOptions(screen.getByLabelText('Resource'), 'grant-model')
    table = screen.getByRole('table', { name: 'Usage by resource' })
    expect(within(table).getByText('Chat completions')).toBeVisible()
    expect(within(table).queryByText('Docs MCP')).not.toBeInTheDocument()
    expect(screen.getAllByText('100')[0]).toBeVisible()
  })

  it('renders the daily trend as an accessible table on request', async () => {
    const user = userEvent.setup()
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Show as table' }))
    expect(screen.getByRole('table', { name: 'Daily requests by environment' })).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Production' })).toBeVisible()
  })

  it('shows the empty state', async () => {
    renderPage(usageReport({ byResource: [], timeline: [], byEnvironment: [] }))

    expect(await screen.findByText('No grants yet')).toBeVisible()
    expect(screen.getByRole('link', { name: 'catalog' })).toHaveAttribute('href', '/catalog')
  })

  it('shows the error state with retry', async () => {
    renderPage(new Error('Usage unavailable'))

    expect(await screen.findByText('Unable to load data')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
  })
})
