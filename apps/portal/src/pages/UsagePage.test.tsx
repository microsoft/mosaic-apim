import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { MyUsageReport, PortalBudgetAlert, UsageResourceRow } from '../types'
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
    costCenter: { id: 'cc-research', name: 'Research', code: 'RES' },
    environment: 'production',
    via: 'direct',
    viaGroupName: null,
    enabled: true,
    bound: true,
    linkedBy: 'gatewayLog',
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
    rateLimits: [{ metric: 'tokens', limit: 10_000, windowSeconds: 60, peak: 8_000, utilization: 0.8 }],
    throttled: 1,
    quotaRefused: 0,
    errors: 2,
    peakMinuteTokens: 8_000,
    peakMinuteRequests: 20,
    recentHours: [
      {
        hour: '2026-09-02T10:00:00Z',
        requests: 8,
        totalTokens: 1_000,
        throttled: 1,
        quotaRefused: 0,
        errors: 0,
        peakMinuteTokens: 800,
        peakMinuteRequests: 3,
      },
    ],
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
      bound: true,
      linkedBy: 'subscription',
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
      linkedBy: null,
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
      throttled: 1,
      quotaRefused: 0,
      errors: 2,
      lastUsedAt: '2026-09-02T10:30:00Z',
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
        throttled: 0,
        quotaRefused: 0,
        errors: 1,
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
        throttled: 1,
        quotaRefused: 0,
        errors: 1,
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
        throttled: 0,
        quotaRefused: 0,
        errors: 0,
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
        throttled: null,
        quotaRefused: null,
        errors: null,
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
        unmeasuredResources: 0,
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
        unmeasuredResources: 0,
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
        unmeasuredResources: 1,
      },
    ],
    byResource: rows,
    costCenters: [
      {
        costCenter: { id: 'cc-research', name: 'Research', code: 'RES' },
        monthStart: '2026-09-01',
        requests: 300,
        totalTokens: 45_000,
        resources: [
          {
            resource: { kind: 'modelApi', id: 'chat', scopeId: null },
            displayName: 'Chat completions',
            requests: 250,
            totalTokens: 40_000,
            poolTokens: 100_000,
            poolCalls: null,
            utilization: 0.4,
            callerName: 'Mallory',
            objectId: '00000000-1111-2222-3333-444444444444',
          } as never,
          {
            resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: null },
            displayName: 'Docs MCP',
            requests: 50,
            totalTokens: null,
            poolTokens: null,
            poolCalls: 1_000,
            utilization: 0.05,
          },
        ],
      },
    ],
    notes: [
      "Figures are simulated from your real grants and limits.",
      "Costs are estimates at illustrative rates and aren't a bill.",
    ],
    recentHours: rows[0].recentHours ?? [],
    ...overrides,
  }
}

function renderPage(report: MyUsageReport | Error = usageReport(), budgets: PortalBudgetAlert[] = []) {
  const getMyUsage = vi.fn(async () => {
    if (report instanceof Error) throw report
    return report
  })
  mocks.api = {
    getMyUsage,
    getMyBudgets: async () => budgets,
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
  it('shows the sample figures badge and notice for simulated data', async () => {
    renderPage()

    expect((await screen.findAllByText('Sample figures')).length).toBeGreaterThan(0)
    expect(screen.getByText('Figures are simulated from your real grants and limits.')).toBeVisible()
    expect(screen.getByText("Costs are estimates at illustrative rates and aren't a bill.")).toBeVisible()
  })

  it('shows measured freshness for Log Analytics data', async () => {
    renderPage(
      usageReport({
        dataSource: 'logAnalytics',
        notes: [],
        freshness: {
          status: 'current',
          updatedAt: '2026-09-02T11:48:00Z',
          dataFrom: '2026-09-01',
          gateways: 1,
          intervalMinutes: 15,
        },
      }),
    )

    expect(await screen.findByRole('table', { name: 'Usage by resource' })).toBeVisible()
    expect(screen.getByText('Measured')).toBeVisible()
    expect(screen.getByText(/refreshed every 15 min/)).toBeVisible()
    expect(screen.queryByText('Sample figures')).not.toBeInTheDocument()
  })

  it.each([
    ['delayed', /figures may be out of date/],
    ['failing', /having trouble reading gateway logs/],
    ['pending', /hasn't read the gateway's logs yet/],
    ['notLinked', /None of your grants are on a gateway MOSAIC reads/],
  ] as const)('renders the %s freshness state', async (status, text) => {
    renderPage(
      usageReport({
        dataSource: 'logAnalytics',
        notes: [],
        freshness: {
          status,
          updatedAt: status === 'pending' || status === 'notLinked' ? null : '2026-09-02T10:00:00Z',
          dataFrom: '2026-09-02',
          gateways: status === 'notLinked' ? 0 : 1,
          intervalMinutes: 15,
        },
      }),
    )

    expect(await screen.findByText(text)).toBeVisible()
    if (status !== 'notLinked') {
      expect(screen.getByText(/complete data from Sep 2/)).toBeVisible()
    }
  })

  it('renders KPI values, unpriced resources as No price, and excluded-resource captions', async () => {
    const user = userEvent.setup()
    renderPage()

    expect(await screen.findByText('120')).toBeVisible()
    expect(screen.getAllByText('$1.23')[0]).toBeVisible()
    expect(screen.getByText('Excludes 2 resources with unknown cost')).toBeVisible()

    await user.selectOptions(screen.getByLabelText('Resource'), 'grant-mcp')
    expect(screen.getAllByText('20')[0]).toBeVisible()
    // A resource MOSAIC can't price shows No price, never $0.
    expect(screen.getAllByText('No price')[0]).toBeVisible()
    expect(screen.getByText('Excludes 1 resource with unknown cost')).toBeVisible()
  })

  it('shows measured costs per resource and in total, and what they leave out', async () => {
    const report = usageReport({ dataSource: 'logAnalytics', notes: [] })
    renderPage({
      ...report,
      byResource: report.byResource.map((row) =>
        row.entitlementId === 'grant-unattributed'
          ? row
          : row.entitlementId === 'grant-model'
            ? { ...row, attribution: 'measured' }
            : row.entitlementId === 'grant-mcp'
              ? { ...row, attribution: 'measured', costNote: 'MCP servers are billed by their own service, not by tokens.' }
              : { ...row, attribution: 'measured', estimatedCost: null, costNote: 'No price for contoso-llm in Azure Commercial.' },
      ),
      totals: { ...report.totals, costExcludedResources: 3 },
    })

    const table = await screen.findByRole('table', { name: 'Usage by resource' })
    expect(within(table).getByRole('columnheader', { name: 'Estimated cost' })).toBeVisible()
    const chat = within(await findResourceRow('Chat completions'))
    expect(chat.getByText('$1.23')).toBeVisible()
    const retired = within(await findResourceRow('Retired chat'))
    expect(retired.getByText('No price')).toBeVisible()
    expect(retired.getByText('No price for contoso-llm in Azure Commercial.')).toBeVisible()
    expect(screen.getByText('Excludes 3 resources with unknown cost')).toBeVisible()
    expect(screen.getByText(/Estimated costs use list prices from MOSAIC's price list/)).toBeVisible()
  })
  it('shows the priced part of a resource with why the rest has no price', async () => {
    const report = usageReport({ dataSource: 'logAnalytics', notes: [] })
    renderPage({
      ...report,
      byResource: report.byResource.map((row) =>
        row.entitlementId === 'grant-model'
          ? {
              ...row,
              attribution: 'measured',
              estimatedCost: 0.75,
              costNote: 'Part of this usage has no price. The first price for gpt-4o takes effect on 2026-03-10.',
            }
          : row,
      ),
    })

    const chat = within(await findResourceRow('Chat completions'))
    expect(chat.getByText('$0.75')).toBeVisible()
    expect(chat.getByText(/Part of this usage has no price/)).toBeVisible()
  })
  it('renders MCP null token fields as not metered instead of zero', async () => {
    renderPage()

    const row = await findResourceRow('Docs MCP')
    expect(within(row).getByText('Not metered')).toBeVisible()
    expect(within(row).queryByText(/^0$/)).not.toBeInTheDocument()
  })

  it('shows cost center aggregates without per-person details', async () => {
    renderPage()

    expect(await screen.findByText('Cost centers')).toBeVisible()
    expect(screen.getByText("Each cost center's total this month, from everyone who charges it, and its pooled quotas on what you hold there. MOSAIC shows only totals, never who used them.")).toBeVisible()
    expect(screen.getAllByText('Research · RES')[0]).toBeVisible()
    expect(screen.getByText(/300 requests/)).toBeVisible()
    expect(screen.getByText('40,000 tokens of 100,000 pooled tokens')).toBeVisible()
    expect(screen.getByText('40% of pooled quota used')).toBeVisible()
    expect(screen.getByText('50 requests of 1,000 pooled calls')).toBeVisible()
    expect(screen.getByText('5% of pooled quota used')).toBeVisible()
    expect(document.body).not.toHaveTextContent('Mallory')
    expect(document.body).not.toHaveTextContent('00000000-1111-2222-3333-444444444444')
  })

  it('warns above usage when a cost center is past its budget', async () => {
    renderPage(usageReport(), [
      {
        costCenter: { id: 'cc-research', name: 'Research', code: 'RES' },
        level: 'blocked',
        month: '2026-03',
        used: 1.01,
        action: 'block',
      },
    ])

    const banners = await screen.findByRole('region', { name: 'Cost center budgets' })
    expect(within(banners).getByText('Research has used its monthly budget')).toBeVisible()
    expect(within(banners).getByText(/Calls charged to your other cost centers still work/)).toBeVisible()
    expect(await screen.findByText('Busiest resource')).toBeVisible()
  })

  it('shows cost center tags on resource rows', async () => {
    renderPage()

    const row = await findResourceRow('Chat completions')
    const tag = within(row).getByText((_content, element) => element?.tagName === 'SMALL' && element.textContent === 'Research · RES')
    expect(tag).toBeVisible()
  })

  it('explains unattributed and unbound grants', async () => {
    renderPage()

    const row = await findResourceRow('Development chat')
    expect(within(row).getByText("Usage can't be measured for this grant yet.")).toBeVisible()
    expect(within(row).getByText('Not linked yet')).toBeVisible()
  })

  it('labels how usage is tracked for gateway and subscription rows', async () => {
    renderPage()

    expect(within(await findResourceRow('Chat completions')).getByText('Linked from gateway log traces')).toBeVisible()
    expect(within(await findResourceRow('Docs MCP')).getByText('Linked from the APIM subscription')).toBeVisible()
  })

  it('summarizes each environment with its cost and resource count', async () => {
    renderPage()

    expect(await screen.findByText('100 requests · 12,000 tokens · $1.23 · 2 resources')).toBeVisible()
    // The development grant can't be measured, so its environment doesn't claim zero calls.
    expect(screen.getByText('Usage unavailable · 1 resource')).toBeVisible()
  })

  it('says how many resources in an environment are not measured', async () => {
    const [production] = usageReport().byEnvironment
    renderPage(usageReport({ byEnvironment: [{ ...production, resources: 3, unmeasuredResources: 1 }] }))

    expect(
      await screen.findByText('100 requests · 12,000 tokens · $1.23 · 3 resources, 1 not measured'),
    ).toBeVisible()
  })

  it('renders quota period text, rate limits, disabled badge, and removed resource badge', async () => {
    renderPage()

    expect((await screen.findAllByText('Tokens: 12,000 of 50,000 this month'))[0]).toBeVisible()
    expect(screen.getAllByText('10,000 tokens per minute')[0]).toBeVisible()
    expect(screen.getAllByText('Busiest minute: 8,000 tokens (80%)')[0]).toBeVisible()
    expect(screen.getByText('60 requests per 60 seconds')).toBeVisible()
    expect(screen.getByText('Disabled')).toBeVisible()
    const row = await findResourceRow('Retired chat')
    expect(within(row).getByText('No longer available')).toBeVisible()
  })

  it('labels near and reached quota meters', async () => {
    renderPage(
      usageReport({
        byResource: [
          resourceRow({
            entitlementId: 'near',
            quotas: [
              {
                metric: 'tokens',
                limit: 100,
                period: 'Daily',
                windowStart: '2026-09-02T00:00:00Z',
                windowEnd: '2026-09-03T00:00:00Z',
                used: 80,
                utilization: 0.8,
              },
            ],
          }),
          resourceRow({
            entitlementId: 'reached',
            resourceSummary: {
              kind: 'modelApi',
              id: 'chat-2',
              scopeId: null,
              displayName: 'Reached chat',
              gatewayId: 'gateway-1',
              gatewayName: 'Production gateway',
              environment: 'production',
              available: true,
            },
            quotas: [
              {
                metric: 'requests',
                limit: 10,
                period: 'Daily',
                windowStart: '2026-09-02T00:00:00Z',
                windowEnd: '2026-09-03T00:00:00Z',
                used: 10,
                utilization: 1,
              },
            ],
          }),
        ],
      }),
    )

    expect(await screen.findByText('80% used · Near limit')).toBeVisible()
    expect(screen.getByText('100% used · Limit reached')).toBeVisible()
  })

  it("doesn't call a quota fine when its usage is unknown", async () => {
    renderPage(
      usageReport({
        byResource: [
          resourceRow({
            entitlementId: 'unknown',
            attribution: 'unattributed',
            linkedBy: null,
            quotas: [
              {
                metric: 'requests',
                limit: 10_000,
                period: 'Monthly',
                windowStart: '2026-09-01T00:00:00Z',
                windowEnd: '2026-10-01T00:00:00Z',
                used: null,
                utilization: null,
              },
            ],
          }),
        ],
      }),
    )

    const row = await findResourceRow('Chat completions')
    expect(within(row).getByText('Requests: up to 10,000 this month')).toBeVisible()
    expect(within(row).getByText('Usage unknown')).toBeVisible()
    expect(within(row).queryByRole('progressbar')).not.toBeInTheDocument()
    expect(within(row).getAllByText("Usage can't be measured for this grant yet.")).toHaveLength(1)
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

    await screen.findByRole('table', { name: 'Usage by resource' })
    await user.click(screen.getAllByRole('button', { name: 'Show as table' })[0])
    expect(screen.getByRole('table', { name: 'Daily requests by environment' })).toBeVisible()
    expect(screen.getByRole('columnheader', { name: 'Production' })).toBeVisible()
  })

  it('renders recent hours', async () => {
    renderPage(usageReport({ dataSource: 'logAnalytics', freshness: null, notes: [] }))

    expect(await screen.findByRole('img', { name: 'Hourly request chart for the last 24 hours in UTC' })).toBeVisible()
  })

  it("doesn't chart other grants' hours when the filtered grants have none", async () => {
    const user = userEvent.setup()
    const report = usageReport({ dataSource: 'logAnalytics', freshness: null, notes: [] })
    renderPage({
      ...report,
      byResource: report.byResource.map((row) =>
        row.entitlementId === 'grant-model' ? row : { ...row, recentHours: [] },
      ),
    })
    const chartName = 'Hourly request chart for the last 24 hours in UTC'

    expect(await screen.findByRole('img', { name: chartName })).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Environment'), 'development')
    expect(screen.queryByRole('img', { name: chartName })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Environment'), 'all')
    await user.selectOptions(screen.getByLabelText('Resource'), 'grant-mcp')
    expect(screen.queryByRole('img', { name: chartName })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Resource'), 'grant-model')
    expect(screen.getByRole('img', { name: chartName })).toBeVisible()
  })

  it('explains security-group grants show only the caller usage', async () => {
    renderPage(
      usageReport({
        byResource: [
          resourceRow({
            via: 'securityGroup',
            viaGroupName: 'Analysts',
            attribution: 'measured',
          }),
        ],
      }),
    )

    const row = await findResourceRow('Chat completions')
    expect(within(row).getByText('Entra security group: Analysts')).toBeVisible()
    expect(within(row).getByText('Your calls only.')).toBeVisible()
    expect(
      within(row).getByText('Only your own calls through this security-group grant are counted here.'),
    ).toBeVisible()
  })

  it('shows the empty state', async () => {
    renderPage(usageReport({ byResource: [], timeline: [], byEnvironment: [] }))

    expect(await screen.findByText('No grants yet')).toBeVisible()
    expect(screen.getByRole('link', { name: 'catalog' })).toHaveAttribute('href', '/catalog')
  })

  it('shows an empty state when grants have no calls in the period', async () => {
    renderPage(
      usageReport({
        totals: {
          requests: 0,
          promptTokens: 0,
          completionTokens: 0,
          totalTokens: 0,
          estimatedCost: null,
          costExcludedResources: 0,
        },
        timeline: [],
        byEnvironment: [{ ...usageReport().byEnvironment[0], requests: 0, totalTokens: 0 }],
        byResource: [resourceRow({ requests: 0, promptTokens: 0, completionTokens: 0, totalTokens: 0 })],
      }),
    )

    expect(await screen.findByText('No calls in this period')).toBeVisible()
  })

  it('shows the error state with retry', async () => {
    renderPage(new Error('Usage unavailable'))

    expect(await screen.findByText('Unable to load data')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry' })).toBeVisible()
  })
})
