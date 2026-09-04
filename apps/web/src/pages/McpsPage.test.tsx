import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { McpsPage } from './McpsPage'
import type { Gateway, McpEndpoint, McpServer, ObservedMcpTool } from '../types'

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
  visibility: 'catalog',
  selection: 'detected',
  importedFromSnapshotId: 'snapshot_1',
  importedAt: '2026-09-01T12:10:00Z',
  importedBy: 'admin-object-id',
  createdAt: '2026-09-01T12:10:00Z',
  updatedAt: '2026-09-01T12:10:00Z',
}

function buildMcpEndpoint(overrides: Partial<McpEndpoint> = {}): McpEndpoint {
  return {
    id: 'mcpEndpoint_1',
    tenantId: 'tenant-test',
    name: 'Contoso tools',
    endpoint: 'https://mcp.contoso.com/mcp',
    environmentLabel: 'prod',
    authMode: 'none',
    credentialReferenceId: null,
    resourceAudience: null,
    status: 'connected',
    access: {
      canDiscover: true,
      evaluation: 'handshake',
      checkedAt: '2026-09-01T12:00:00Z',
      challenge: null,
      message: null,
    },
    capabilities: {
      protocolVersion: '2025-11-25',
      offeredProtocolVersion: '2025-11-25',
      transportType: 'streamable',
      serverName: 'contoso-mcp',
      serverTitle: 'Contoso MCP',
      serverVersion: '3.1.0',
      instructions: null,
      supportsTools: 'available',
      sessionManaged: true,
      notes: [],
    },
    inventory: { tools: 2, readOnlyTools: 1, unannotatedTools: 1 },
    lastSyncedAt: '2026-09-01T12:05:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T11:00:00Z',
    updatedAt: '2026-09-01T12:05:00Z',
    ...overrides,
  }
}

const readOnlyTool: ObservedMcpTool = {
  id: 'mcpTool_1',
  tenantId: 'tenant-test',
  endpointId: 'mcpEndpoint_1',
  snapshotId: 'snapshot_1',
  observedAt: '2026-09-01T12:05:00Z',
  name: 'search_docs',
  displayName: 'Search documents',
  title: 'Search documents',
  description: 'Full text search over the corpus.',
  inputSchema: { type: 'object' },
  outputSchema: { type: 'object' },
  annotations: {
    title: null,
    readOnlyHint: true,
    destructiveHint: null,
    idempotentHint: null,
    openWorldHint: false,
  },
}

const unannotatedTool: ObservedMcpTool = {
  id: 'mcpTool_2',
  tenantId: 'tenant-test',
  endpointId: 'mcpEndpoint_1',
  snapshotId: 'snapshot_1',
  observedAt: '2026-09-01T12:05:00Z',
  name: 'delete_record',
  displayName: 'delete_record',
  title: null,
  description: 'Removes a record.',
  inputSchema: { type: 'object' },
  outputSchema: null,
  annotations: null,
}

const api = {
  listGateways: vi.fn(),
  listMcpServers: vi.fn(),
  deleteMcpServer: vi.fn(),
  listImportableMcpServers: vi.fn(),
  importMcpServers: vi.fn(),
  listMcpEndpoints: vi.fn(),
  registerMcpEndpoint: vi.fn(),
  preflightMcpEndpoint: vi.fn(),
  syncMcpEndpoint: vi.fn(),
  deleteMcpEndpoint: vi.fn(),
  listMcpEndpointTools: vi.fn(),
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
    api.listMcpEndpoints.mockResolvedValue([])
    api.listMcpEndpointTools.mockResolvedValue([])
    api.registerMcpEndpoint.mockResolvedValue(buildMcpEndpoint())
    api.syncMcpEndpoint.mockResolvedValue({ id: 'syncrun_1' })
    api.preflightMcpEndpoint.mockResolvedValue(buildMcpEndpoint())
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

  describe('registered servers', () => {
    it('invites a registration when nothing is registered yet', async () => {
      renderPage()

      expect(await screen.findByText('No MCP servers registered yet')).toBeVisible()
    })

    it('shows the negotiated protocol and tool counts', async () => {
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])

      renderPage()

      const table = await screen.findByRole('table', { name: 'Registered MCP servers' })
      expect(within(table).getByText('Contoso tools')).toBeVisible()
      expect(within(table).getByText('Connected')).toBeVisible()
      expect(within(table).getByText(/Protocol 2025-11-25/)).toBeVisible()
      expect(within(table).getByText('1 state no behaviour')).toBeVisible()
    })

    it('registers a server with no authentication', async () => {
      const user = userEvent.setup()
      renderPage()

      await user.click(await screen.findByRole('button', { name: 'Register server' }))
      await user.type(screen.getByRole('textbox', { name: /Server URL/ }), 'https://a.example/mcp')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      await waitFor(() => {
        expect(api.registerMcpEndpoint).toHaveBeenCalledWith(
          expect.objectContaining({ endpoint: 'https://a.example/mcp', authMode: 'none' }),
        )
      }, { timeout: 5000 })
    })

    it('asks for a Key Vault secret URI rather than a token', async () => {
      const user = userEvent.setup()
      renderPage()

      await user.click(await screen.findByRole('button', { name: 'Register server' }))
      await user.click(screen.getByRole('tab', { name: 'Key Vault secret' }))

      expect(screen.getByRole('textbox', { name: /Key Vault secret URI/ })).toBeVisible()
      expect(screen.getByText(/stores only this URI, never the token/)).toBeVisible()
    })

    it('requires an audience before a managed identity token is issued', async () => {
      const user = userEvent.setup()
      renderPage()

      await user.click(await screen.findByRole('button', { name: 'Register server' }))
      await user.click(screen.getByRole('tab', { name: 'Managed identity' }))
      await user.type(screen.getByRole('textbox', { name: /Server URL/ }), 'https://a.example/mcp')
      await user.type(screen.getByRole('textbox', { name: /Token audience/ }), 'api://contoso')
      await user.click(screen.getByRole('button', { name: 'Register' }))

      await waitFor(() => {
        expect(api.registerMcpEndpoint).toHaveBeenCalledWith(
          expect.objectContaining({
            authMode: 'managedIdentity',
            resourceAudience: 'api://contoso',
          }),
        )
      }, { timeout: 5000 })
    })

    it('will not offer a sync until the server is reachable', async () => {
      api.listMcpEndpoints.mockResolvedValue([
        buildMcpEndpoint({
          status: 'unauthorized',
          access: {
            canDiscover: false,
            evaluation: 'authorizationRequired',
            checkedAt: '2026-09-01T12:00:00Z',
            challenge: {
              scheme: 'Bearer',
              resourceMetadataUrl: 'https://mcp.contoso.com/.well-known/oauth-protected-resource',
              scope: 'mcp.read',
            },
            message: 'This MCP server requires authorization that MOSAIC was not given.',
          },
        }),
      ])

      renderPage()

      const table = await screen.findByRole('table', { name: 'Registered MCP servers' })
      expect(within(table).getByRole('button', { name: 'Sync tools' })).toBeDisabled()
    })

    it('reports what a 401 asked for instead of calling the server unreachable', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([
        buildMcpEndpoint({
          status: 'unauthorized',
          access: {
            canDiscover: false,
            evaluation: 'authorizationRequired',
            checkedAt: '2026-09-01T12:00:00Z',
            challenge: {
              scheme: 'Bearer',
              resourceMetadataUrl: 'https://mcp.contoso.com/.well-known/oauth-protected-resource',
              scope: 'mcp.read',
            },
            message: 'This MCP server requires authorization that MOSAIC was not given.',
          },
        }),
      ])

      renderPage()
      await user.click(await screen.findByRole('button', { name: 'Contoso tools' }))

      // The status also appears as a badge in the table, so scope to the notice itself.
      const notice = await screen.findByText(
        /This MCP server requires authorization that MOSAIC was not given/,
      )
      expect(notice).toBeVisible()
      expect(screen.getByText(/asked for the scope mcp.read/)).toBeVisible()
      expect(
        screen.getByText(/protected resource metadata is at https:\/\/mcp.contoso.com/),
      ).toBeVisible()
    })

    it('labels a stateless server as an unsupported protocol, not a failure', async () => {
      api.listMcpEndpoints.mockResolvedValue([
        buildMcpEndpoint({ status: 'unsupportedProtocol' }),
      ])

      renderPage()

      const table = await screen.findByRole('table', { name: 'Registered MCP servers' })
      expect(within(table).getByText('Protocol not supported')).toBeVisible()
    })

    it('renders an absent annotation as not stated rather than as its default', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
      api.listMcpEndpointTools.mockResolvedValue([readOnlyTool, unannotatedTool])

      renderPage()
      await user.click(await screen.findByRole('button', { name: 'Contoso tools' }))

      const table = await screen.findByRole('table', { name: 'Tools on Contoso tools' })
      // The description is unique to this row; the tool name appears twice because an unannotated
      // tool has no title to fall back from.
      const unannotatedRow = within(table).getByText('Removes a record.').closest('tr')
      expect(unannotatedRow).not.toBeNull()
      const row = unannotatedRow as HTMLElement
      // destructiveHint defaults to true in the specification. Showing "no" would invent a
      // reassurance; showing "yes" would invent a warning.
      expect(within(row).getByText('Destructive: not stated')).toBeVisible()
      expect(within(row).getByText('Read only: not stated')).toBeVisible()
      expect(within(row).getByText('Open world: not stated')).toBeVisible()
    })

    it('shows a stated hint as the server said it', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
      api.listMcpEndpointTools.mockResolvedValue([readOnlyTool])

      renderPage()
      await user.click(await screen.findByRole('button', { name: 'Contoso tools' }))

      const table = await screen.findByRole('table', { name: 'Tools on Contoso tools' })
      expect(within(table).getByText('Read only: yes')).toBeVisible()
      expect(within(table).getByText('Open world: no')).toBeVisible()
      expect(within(table).getByText('Destructive: not stated')).toBeVisible()
      expect(within(table).getByText('Input schema')).toBeVisible()
    })

    it('says annotations are the server\u2019s untrusted claims', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
      api.listMcpEndpointTools.mockResolvedValue([readOnlyTool])

      renderPage()
      await user.click(await screen.findByRole('button', { name: 'Contoso tools' }))

      expect(
        await screen.findByText(/requires clients to treat tool annotations as untrusted/),
      ).toBeVisible()
    })

    it('explains an empty tool list when the server declared no tools capability', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([
        buildMcpEndpoint({
          capabilities: {
            ...buildMcpEndpoint().capabilities,
            supportsTools: 'unavailable',
          },
          inventory: { tools: 0, readOnlyTools: 0, unannotatedTools: 0 },
        }),
      ])

      renderPage()
      await user.click(await screen.findByRole('button', { name: 'Contoso tools' }))

      expect(
        await screen.findByText(/did not advertise a tools capability/),
      ).toBeVisible()
    })

    it('syncs and removes a registered server', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
      api.deleteMcpEndpoint.mockResolvedValue(undefined)

      renderPage()
      const table = await screen.findByRole('table', { name: 'Registered MCP servers' })

      await user.click(within(table).getByRole('button', { name: 'Sync tools' }))
      await waitFor(() => expect(api.syncMcpEndpoint).toHaveBeenCalledWith('mcpEndpoint_1'), {
        timeout: 5000,
      })

      await user.click(within(table).getByRole('button', { name: 'Remove' }))
      await waitFor(() => expect(api.deleteMcpEndpoint).toHaveBeenCalledWith('mcpEndpoint_1'), {
        timeout: 5000,
      })
      expect(
        await screen.findByText(/Stopped governing that MCP server. The server itself is unchanged/),
      ).toBeVisible()
    })
  })
})

