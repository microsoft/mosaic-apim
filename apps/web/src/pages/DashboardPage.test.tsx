import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useParams } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DashboardPage } from './DashboardPage'
import { overviewFixture, pricedOverviewFixture, spendFixture, statusFixture } from '../test/analytics-fixtures'
import { budgetOverviewFixture, emptyBudgetOverviewFixture } from '../test/budget-fixtures'
import { anthropicPool, draftPool, poolSummaries } from '../test/pool-fixtures'
import type { ModelPool, ModelPoolSummary, PoolMemberProblem } from '../types'

const timestamps = { createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' }

const api = {
  listPrincipals: vi.fn(),
  listGroups: vi.fn(),
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  getAnalyticsOverview: vi.fn(),
  getAnalyticsStatus: vi.fn(),
  getBudgets: vi.fn(),
  listModelPoolSummaries: vi.fn(),
}
vi.mock('../api', () => ({
  useMosaicApi: () => api,
}))

const noRole =
  'The gateway’s managed identity has no role that lets it call foundry-northcentralus. Grant it Cognitive ' +
  'Services User on the endpoint.'

function memberProblem(overrides: Partial<PoolMemberProblem> = {}): PoolMemberProblem {
  return {
    poolModelId: 'poolmodel_opus',
    modelDisplayName: 'Claude Opus 4.5',
    modelEndpointId: 'endpoint_northcentralus',
    endpointName: 'foundry-northcentralus',
    deploymentName: 'claude-opus-4-5',
    region: 'northcentralus',
    readiness: 'cannotInvoke',
    problems: [noRole],
    ...overrides,
  }
}

/** A pool the gateway runs, because MOSAIC created its API. */
function publishedSummary(
  slug: string,
  displayName: string,
  overrides: Partial<ModelPoolSummary> = {},
  pool: Partial<ModelPool> = {},
): ModelPoolSummary {
  const apiName = `mosaic-pool-${slug}`
  return {
    ...poolSummaries[0],
    problemCount: 0,
    warningCount: 0,
    ...overrides,
    pool: {
      ...anthropicPool,
      id: `modelpool_${slug}`,
      displayName,
      apiName,
      resources: [{ ...anthropicPool.resources[0], name: apiName, resourceId: `/apis/${apiName}` }],
      ...pool,
    },
  }
}

function PoolPageProbe() {
  const { poolId } = useParams()
  return <p>{`Pool page for ${poolId}`}</p>
}

async function poolRows() {
  const list = await screen.findByRole('list', { name: 'Pools that need attention' })
  return within(list)
    .getAllByRole('button')
    .map((button) => button.closest('li') as HTMLElement)
}

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
          <Route path="/pools/:poolId" element={<PoolPageProbe />} />
          <Route path="/pools" element={<p>Pools page</p>} />
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
    api.listModelPoolSummaries.mockResolvedValue([])
  })

  it('lists the published pools with members the gateway can’t use, worst first, and opens one', async () => {
    api.listModelPoolSummaries.mockResolvedValue([
      publishedSummary('anthropic', 'Anthropic Claude', { problemCount: 1, memberProblems: [memberProblem()] }),
      publishedSummary('openai', 'OpenAI GPT', {
        problemCount: 4,
        memberProblems: [
          memberProblem({
            poolModelId: 'poolmodel_gpt',
            modelDisplayName: 'GPT-4o',
            modelEndpointId: 'endpoint_sweden',
            endpointName: 'aoai-sweden',
            deploymentName: 'gpt-4o',
            region: 'swedencentral',
            problems: ['MOSAIC no longer sees gpt-4o on aoai-sweden.', 'It has no key.'],
          }),
          memberProblem({ poolModelId: 'poolmodel_gpt', deploymentName: 'gpt-4o-east', endpointName: 'aoai-east' }),
          memberProblem({ poolModelId: 'poolmodel_gpt', deploymentName: 'gpt-4o-west', endpointName: 'aoai-west' }),
          memberProblem({ poolModelId: 'poolmodel_gpt', deploymentName: 'gpt-4o-north', endpointName: 'aoai-north' }),
        ],
      }),
      // A published pool with nothing wrong, and a draft that serves no calls yet.
      publishedSummary('mistral', 'Mistral'),
      { ...poolSummaries[1], problemCount: 1, memberProblems: [memberProblem()] },
    ])
    renderPage()

    const rows = await poolRows()
    expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual(['OpenAI GPT', 'Anthropic Claude'])
    expect(screen.queryByText('Mistral')).not.toBeInTheDocument()
    expect(screen.queryByText('OpenAI chat')).not.toBeInTheDocument()

    const [openai, anthropic] = rows
    expect(within(openai).getByText('4 members have problems')).toBeVisible()
    const openaiMembers = within(openai).getAllByRole('listitem')
    expect(openaiMembers).toHaveLength(3)
    expect(openaiMembers[0]).toHaveTextContent('gpt-4o on aoai-sweden · swedencentral')
    expect(
      within(openaiMembers[0]).getByText('MOSAIC no longer sees gpt-4o on aoai-sweden. (+1 more)'),
    ).toBeVisible()
    expect(within(openai).getByText('1 more member on the pool’s page.')).toBeVisible()

    expect(within(anthropic).getByText('1 member has a problem')).toBeVisible()
    expect(within(anthropic).getByText('Contoso AI gateway')).toBeVisible()
    const anthropicMembers = within(anthropic).getByRole('list', { name: 'Anthropic Claude members with problems' })
    expect(anthropicMembers).toHaveTextContent('claude-opus-4-5 on foundry-northcentralus · northcentralus')
    expect(within(anthropicMembers).getByText(noRole)).toBeVisible()

    fireEvent.click(within(anthropic).getByRole('button'))
    expect(await screen.findByText('Pool page for modelpool_anthropic')).toBeVisible()
  })

  it('lists a pool whose last apply failed, published or not', async () => {
    api.listModelPoolSummaries.mockResolvedValue([
      { ...poolSummaries[1], pool: { ...draftPool, status: 'failed' }, problemCount: 0 },
      publishedSummary('anthropic', 'Anthropic Claude', {}, { status: 'rolledBack' }),
      publishedSummary('mistral', 'Mistral', { problemCount: 2 }),
    ])
    renderPage()

    const rows = await poolRows()
    expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual([
      'Anthropic Claude',
      'OpenAI chat',
      'Mistral',
    ])
    const [rolledBack, failed, mistral] = rows
    expect(within(rolledBack).getByText('Apply failed')).toBeVisible()
    expect(within(rolledBack).getByText('The last apply failed and was rolled back.')).toBeVisible()
    expect(within(failed).getByText('The last apply failed.')).toBeVisible()
    // A pool-wide problem, with no member to name, sends the administrator to the pool.
    expect(within(mistral).getByText('2 problems')).toBeVisible()
    expect(within(mistral).getByText('Open the pool to see what to fix.')).toBeVisible()
  })

  it('says when every published pool is fine', async () => {
    api.listModelPoolSummaries.mockResolvedValue(poolSummaries)
    renderPage()

    expect(await screen.findByText('1 published pool, none with a problem.')).toBeVisible()
    expect(screen.queryByRole('list', { name: 'Pools that need attention' })).not.toBeInTheDocument()
  })

  it('says no pool serves calls while every pool is a draft', async () => {
    api.listModelPoolSummaries.mockResolvedValue([poolSummaries[1]])
    renderPage()

    expect(await screen.findByText('No pool is published yet, so none serves calls.')).toBeVisible()
  })

  it('says what a pool is when there are none, and opens the Pools page', async () => {
    renderPage()

    expect(await screen.findByText(/No pools yet/)).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Open pools' }))
    expect(await screen.findByText('Pools page')).toBeVisible()
  })

  it('shows the four worst pools and counts the rest', async () => {
    api.listModelPoolSummaries.mockResolvedValue(
      ['a', 'b', 'c', 'd', 'e', 'f'].map((slug, index) =>
        publishedSummary(slug, `Pool ${slug.toUpperCase()}`, {
          problemCount: index + 1,
          memberProblems: Array.from({ length: index + 1 }, (_, member) =>
            memberProblem({ deploymentName: `deployment-${member}` }),
          ),
        }),
      ),
    )
    renderPage()

    const rows = await poolRows()
    expect(rows.map((row) => row.querySelector('strong')?.textContent)).toEqual(['Pool F', 'Pool E', 'Pool D', 'Pool C'])
    expect(screen.getByText('2 more pools to look at on the Pools page.')).toBeVisible()
  })

  it('shows the rest of the dashboard when pools fail to load', async () => {
    api.listModelPoolSummaries.mockRejectedValue(new Error('Pools failed'))
    renderPage()

    expect(await screen.findByText('Pools failed')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Pools' })).toBeVisible()
    expect(await screen.findByText('2 gateways')).toBeVisible()
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
