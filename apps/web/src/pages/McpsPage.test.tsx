import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { McpsPage } from './McpsPage'
import type { Gateway, McpServer } from '../types'

const RESOURCE_ID =
  '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-dev' +
  '/providers/Microsoft.ApiManagement/service/apim-contoso-dev'

function buildGateway(overrides: Partial<Gateway> = {}): Gateway {
  return {
    id: 'gateway_1',
    tenantId: 'tenant-test',
    name: 'Development gateway',
    provider: 'apim',
    azureResourceId: RESOURCE_ID,
    subscriptionId: '00000000-0000-0000-0000-000000000000',
    resourceGroup: 'rg-contoso-dev',
    serviceName: 'apim-contoso-dev',
    environmentLabel: 'dev',
    managementMode: 'observe',
    status: 'connected',
    access: {
      canRead: true,
      canWrite: false,
      evaluation: 'effectivePermissions',
      checkedAt: '2026-09-01T12:00:00Z',
      missingActions: [],
      remediation: null,
      message: 'MOSAIC can read this gateway.',
    },
    capabilities: {
      skuName: 'Developer',
      skuCapacity: 1,
      provisioningState: 'Succeeded',
      location: 'eastus2',
      gatewayUrl: 'https://apim-contoso-dev.azure-api.net',
      managementApiVersion: '2024-05-01',
      aiGatewayPolicies: 'available',
      mcpServers: 'available',
      identityObserved: true,
      notes: [],
    },
    inventory: {
      apis: 2,
      aiApis: 1,
      mcpServers: 2,
      operations: 2,
      products: 1,
      subscriptions: 1,
      users: 1,
      groups: 1,
      backends: 1,
      namedValues: 1,
      policyDocuments: 3,
      policyFragments: 1,
      recognizedFacets: 4,
      unrecognizedFacets: 1,
      mosaicManagedFacets: 1,
    },
    lastSyncedAt: '2026-09-01T12:05:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T11:00:00Z',
    updatedAt: '2026-09-01T12:05:00Z',
    ...overrides,
  }
}

const mcpServer: McpServer = {
  id: 'mcpServer_1',
  tenantId: 'tenant-test',
  gatewayId: 'gateway_1',
  apiName: 'weather-mcp',
  displayName: 'Weather MCP',
  path: 'weather-mcp',
  serviceUrl: 'https://mcp.contoso.com',
  protocols: ['https'],
  kind: 'passthrough',
  transportType: 'sse',
  endpoints: [
    { name: 'sse', uriTemplate: '/sse' },
    { name: 'message', uriTemplate: '/messages' },
  ],
  tools: [],
  toolCount: 0,
  subscriptionRequired: false,
  productNames: [],
  selection: 'detected',
  importedFromSnapshotId: 'snapshot_1',
  importedAt: '2026-09-01T12:10:00Z',
  importedBy: 'admin-object-id',
  createdAt: '2026-09-01T12:10:00Z',
  updatedAt: '2026-09-01T12:10:00Z',
}

const api = {
  listGateways: vi.fn(),
  listMcpServers: vi.fn(),
  deleteMcpServer: vi.fn(),
  listImportableMcpServers: vi.fn(),
  importMcpServers: vi.fn(),
}

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: class extends Error {},
}))

function LocationProbe() {
  const location = useLocation()
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>
}

function renderPage(entry = '/mcps') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <McpsPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('McpsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([buildGateway()])
    api.listMcpServers.mockResolvedValue([])
    api.listImportableMcpServers.mockResolvedValue({
      gatewayId: 'gateway_1',
      snapshotId: 'snapshot_1',
      lastSyncedAt: '2026-09-01T12:05:00Z',
      support: 'available',
      candidates: [
        {
          apiName: 'orders-mcp',
          displayName: 'Orders MCP',
          path: 'orders-mcp',
          serviceUrl: null,
          kind: 'restApiBacked',
          transportType: 'unknown',
          toolCount: 1,
          recommended: true,
          alreadyImported: false,
        },
        {
          apiName: 'weather-mcp',
          displayName: 'Weather MCP',
          path: 'weather-mcp',
          serviceUrl: 'https://mcp.contoso.com',
          kind: 'passthrough',
          transportType: 'sse',
          toolCount: 0,
          recommended: true,
          alreadyImported: true,
        },
      ],
    })
    api.importMcpServers.mockResolvedValue([mcpServer])
  })

  it('invites an import when nothing has been adopted yet', async () => {
    renderPage()

    expect(await screen.findByText('No MCP servers imported yet')).toBeVisible()
  })

  it('shows the transport and gateway of an imported server', async () => {
    api.listMcpServers.mockResolvedValue([mcpServer])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Imported MCP servers' })
    expect(within(table).getByText('Weather MCP')).toBeVisible()
    expect(within(table).getByText('Passthrough · SSE')).toBeVisible()
    expect(within(table).getByRole('link', { name: 'Development gateway' })).toBeVisible()
  })

  it('opens the import dialog from the gateway query', async () => {
    renderPage('/mcps?import=gateway_1')

    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(
      await screen.findByRole('checkbox', { name: 'Import Orders MCP' }),
    ).toBeChecked()
  })

  it('does not offer a server that is already governed', async () => {
    renderPage('/mcps?import=gateway_1')

    const alreadyImported = await screen.findByRole('checkbox', {
      name: 'Import Weather MCP',
    })

    expect(alreadyImported).toBeChecked()
    expect(alreadyImported).toBeDisabled()
    // Only the one selectable candidate counts toward the import.
    expect(screen.getByRole('button', { name: 'Import 1' })).toBeEnabled()
  })

  it('imports the selected servers', async () => {
    const user = userEvent.setup()
    renderPage('/mcps?import=gateway_1')

    await user.click(await screen.findByRole('button', { name: 'Import 1' }))

    await waitFor(() => {
      expect(api.importMcpServers).toHaveBeenCalledWith('gateway_1', ['orders-mcp'])
    })
  })

  it('explains when a gateway cannot host MCP servers', async () => {
    api.listImportableMcpServers.mockResolvedValue({
      gatewayId: 'gateway_1',
      snapshotId: 'snapshot_1',
      lastSyncedAt: '2026-09-01T12:05:00Z',
      support: 'unavailable',
      candidates: [],
    })

    renderPage('/mcps?import=gateway_1')

    expect(
      await screen.findByText('MCP servers are not available here'),
    ).toBeVisible()
  })

  it('warns when the chosen gateway has never been synchronised', async () => {
    api.listGateways.mockResolvedValue([buildGateway({ lastSyncedAt: null })])

    renderPage('/mcps?import=gateway_1')

    expect(await screen.findByText('Not synchronised')).toBeVisible()
  })
})

