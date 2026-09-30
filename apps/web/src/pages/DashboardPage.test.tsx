import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { DashboardPage } from './DashboardPage'

const timestamps = { createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' }

const api = {
  listPrincipals: vi.fn(),
  listGroups: vi.fn(),
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
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
      <MemoryRouter>
        <DashboardPage />
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
  })

  it('separates live desired state from sample telemetry', async () => {
    renderPage()

    expect(await screen.findByText('person')).toBeVisible()
    expect(within(inventoryCard('person')).getByText('1')).toBeVisible()
    expect(within(inventoryCard('agents')).getByText('2')).toBeVisible()
    expect(within(inventoryCard('apps and security groups')).getByText('3')).toBeVisible()
    expect(within(inventoryCard('MOSAIC group')).getByText('1')).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(screen.getAllByText('Sample data').length).toBeGreaterThan(1)
    expect(screen.getByRole('note')).toHaveTextContent('MOSAIC is not querying Azure Monitor yet')
    expect(await screen.findByRole('heading', { name: 'Environments' })).toBeVisible()
    expect(screen.getByText('2 gateways')).toBeVisible()
    expect(screen.getByText('3 MCP servers')).toBeVisible()
    expect(screen.getByText('1 gateway')).toBeVisible()
    expect(screen.getByRole('button', { name: '1 environment finding' })).toBeVisible()
  })

  it('shows counts even when environment findings fail', async () => {
    api.listEnvironmentFindings.mockRejectedValue(new Error('Findings failed'))
    renderPage()

    expect(await screen.findByText('2 gateways')).toBeVisible()
    expect(await screen.findByText('Findings unavailable')).toBeVisible()
  })
})
