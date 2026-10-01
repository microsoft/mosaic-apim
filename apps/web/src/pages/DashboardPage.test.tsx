import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DashboardPage } from './DashboardPage'
import { overviewFixture, pricedOverviewFixture, spendFixture, statusFixture } from '../test/analytics-fixtures'
import { budgetOverviewFixture, emptyBudgetOverviewFixture } from '../test/budget-fixtures'

const timestamps = { createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' }

const api = {
  listPrincipals: vi.fn(),
  listGroups: vi.fn(),
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  getAnalyticsOverview: vi.fn(),
  getAnalyticsStatus: vi.fn(),
  getBudgets: vi.fn(),
}
vi.mock('../api', () => ({
  useMosaicApi: () => api,
}))

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <Routes>
          <Route path="/dashboard" element={<DashboardPage />} />
          <Route path="/cost-centers/:costCenterId" element={<p>Cost center page</p>} />
          <Route path="/cost-centers" element={<p>Cost centers page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function inventoryCard(label: string) {
  const card = screen.getByText(label, { selector: 'small' }).closest('button')
  if (!card) {
    throw new Error(`No inventory card for ${label}`)
  }
  return card
}

describe('DashboardPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.listPrincipals.mockResolvedValue([
      { id: 'principal-user', tenantId: 'tenant', objectId: 'user-object', kind: 'user', label: 'User', ...timestamps },
      { id: 'principal-agent', tenantId: 'tenant', objectId: 'agent-object', kind: 'agentIdentity', ...timestamps },
      { id: 'principal-agent-user', tenantId: 'tenant', objectId: 'agent-user-object', kind: 'agentUser', ...timestamps },
      { id: 'principal-workload', tenantId: 'tenant', objectId: 'workload-object', kind: 'managedIdentity', ...timestamps },
      { id: 'principal-group', tenantId: 'tenant', objectId: 'group-object', kind: 'securityGroup', ...timestamps },
      { id: 'principal-app', tenantId: 'tenant', objectId: 'app-object', kind: 'servicePrincipal', ...timestamps },
    ])
    api.listGroups.mockResolvedValue([
      {
        id: 'group',
        tenantId: 'tenant',
        name: 'Engineering',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ])
    api.getEnvironmentCatalog.mockResolvedValue({
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
          usage: { gateways: 2, modelEndpoints: 5, mcpEndpoints: 1 },
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
          usage: { gateways: 3, modelEndpoints: 4, mcpEndpoints: 2 },
        },
      ],
      requireClassification: false,
      unclassified: { gateways: 1, modelEndpoints: 0, mcpEndpoints: 3 },
      compatibility: [],
      updatedAt: null,
    })
    api.listEnvironmentFindings.mockResolvedValue({
      items: [{ id: 'finding_1' }],
      limitations: [],
      generatedAt: '2026-09-29T00:00:00Z',
    })
    api.getAnalyticsOverview.mockResolvedValue(overviewFixture)
    api.getAnalyticsStatus.mockResolvedValue(statusFixture)
    api.getBudgets.mockResolvedValue(emptyBudgetOverviewFixture)
  })

  it('shows each budget worst first, with the organization on top, and opens its cost center', async () => {
    api.getBudgets.mockResolvedValue(budgetOverviewFixture)
    renderPage()

    const list = await screen.findByRole('list', { name: 'Budgets' })
    const rows = within(list).getAllByRole('button')
    expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual([
      'Organization',
      'Research',
      'Customer Support',
    ])
    expect(screen.getByText('March 2026 so far, against each monthly budget · 1 cost center blocked')).toBeVisible()
    const research = rows[1]
    expect(within(research).getByText('Blocked')).toBeVisible()
    expect(within(research).getByText('$4,320.00')).toBeVisible()
    expect(within(research).getByText('of $4,000.00')).toBeVisible()
    expect(within(research).getByText('108% used · Projected $7,440.00 by Mar 31')).toBeVisible()
    expect(within(rows[2]).getByText('Near its limit')).toBeVisible()
    expect(within(rows[0]).getByText('Every call, warns only')).toBeVisible()
    expect(screen.getByText('2 cost centers without a budget.')).toBeVisible()

    fireEvent.click(research)
    expect(await screen.findByText('Cost center page')).toBeVisible()
  })

  it('says how to start when there are no budgets, and that email is off', async () => {
    renderPage()

    expect(await screen.findByText(/No budgets yet/)).toBeVisible()
    expect(
      screen.getByText('4 cost centers without a budget. Email is off, so budgets email no one. Set it up in Settings.'),
    ).toBeVisible()
  })

  it('shows live desired state and real usage analytics', async () => {
    renderPage()

    expect(await screen.findByText('person')).toBeVisible()
    expect(within(inventoryCard('person')).getByText('1')).toBeVisible()
    expect(within(inventoryCard('agents')).getByText('2')).toBeVisible()
    expect(within(inventoryCard('apps and security groups')).getByText('3')).toBeVisible()
    expect(within(inventoryCard('MOSAIC group')).getByText('1')).toBeVisible()
    expect(screen.getAllByText('Live data').length).toBeGreaterThan(0)
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
    expect(await screen.findByText('Alice Admin')).toBeVisible()
    const callers = screen.getByRole('list', { name: 'Top callers' })
    expect(within(callers).getByText('15.1K tokens')).toBeVisible()
    expect(within(callers).getByText('106 calls')).toBeVisible()
    expect(within(screen.getByRole('list', { name: 'Top models' })).getByText('45K tokens')).toBeVisible()
    // APIs rank by calls, because MCP servers carry no tokens.
    const apis = screen.getByRole('list', { name: 'Top APIs' })
    expect(within(apis).getByText('154 calls')).toBeVisible()
    expect(within(apis).getByText('Production gateway · 45K tokens')).toBeVisible()
    expect(screen.getByText('Tokens, peak 3K')).toBeVisible()
    expect(screen.getByText('≈1.5 s')).toBeVisible()
    expect(screen.getByText('Current')).toBeVisible()
    expect(screen.getByText('3 governed APIs · 15 min behind')).toBeVisible()
    expect(screen.queryByText(/Estimated cost/)).not.toBeInTheDocument()
    // Without a price list there's no spend, never $0.
    expect(within(screen.getByText('This month').closest('.fui-Card') as HTMLElement).getByText('No price list')).toBeVisible()
    expect(await screen.findByRole('heading', { name: 'Environments' })).toBeVisible()
    expect(screen.getByText('2 gateways')).toBeVisible()
    expect(screen.getByText('3 MCP servers')).toBeVisible()
    expect(screen.getByText('1 gateway')).toBeVisible()
    expect(screen.getByRole('button', { name: '1 environment finding' })).toBeVisible()
  })

  it('shows spend this month with its projected month end', async () => {
    api.getAnalyticsOverview.mockResolvedValue(pricedOverviewFixture)
    renderPage()

    const spend = (await screen.findByText('This month')).closest('.fui-Card') as HTMLElement
    expect(await within(spend).findByText('$4,355.25')).toBeVisible()
    expect(within(spend).getByText('Projected $7,502.10 by Mar 31')).toBeVisible()
  })

  it('waits for a day of figures before projecting, and says when there is no price list', async () => {
    api.getAnalyticsOverview.mockResolvedValue({ ...pricedOverviewFixture, spend: { ...spendFixture, forecast: null } })
    renderPage()

    const spend = (await screen.findByText('This month')).closest('.fui-Card') as HTMLElement
    expect(await within(spend).findByText('Forecast after a day of this month’s figures')).toBeVisible()
  })

  it("doesn't spell out a lag for a gateway that is caught up", async () => {
    const [gateway] = statusFixture.gateways
    api.getAnalyticsStatus.mockResolvedValue({ ...statusFixture, gateways: [{ ...gateway, lagMinutes: 0 }] })
    renderPage()

    expect(await screen.findByText('3 governed APIs')).toBeVisible()
    expect(screen.queryByText(/min behind/)).not.toBeInTheDocument()
  })

  it('shows counts even when environment findings fail', async () => {
    api.listEnvironmentFindings.mockRejectedValue(new Error('Findings failed'))
    renderPage()

    expect(await screen.findByText('2 gateways')).toBeVisible()
    expect(await screen.findByText('Findings unavailable')).toBeVisible()
  })
})
