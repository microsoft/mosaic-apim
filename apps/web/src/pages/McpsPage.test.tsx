import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { McpsPage } from './McpsPage'
import type { Gateway, McpEndpoint, McpPublication, McpServer, ObservedMcpTool, PublishPlan, PublishRun } from '../types'

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
    environment: null,
    azureEnvironmentTag: null,
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

const mcpPublication: McpPublication = {
  id: 'mcp_pub_1',
  tenantId: 'tenant-test',
  entityType: 'mcpPublication',
  gatewayId: 'gateway_1',
  mcpEndpointId: 'mcpEndpoint_1',
  displayName: 'Contoso tools',
  apiName: 'mosaic-mcp-contoso-tools',
  apiPath: 'mosaic/mcp/contoso-tools',
  backendName: 'mosaic-mcp-contoso-tools',
  fragmentName: 'mosaic-mcp-contoso-tools',
  metadataApiName: 'mosaic-mcp-contoso-tools-prm',
  mcpServerId: 'mcpServer_1',
  status: 'published',
  resources: [],
  lastPlanId: 'mcp_plan_1',
  lastPlanDigest: 'digest',
  lastRunId: 'mcp_run_1',
  lastAppliedAt: '2026-09-01T12:20:00Z',
  lastError: null,
  appliedAccess: null,
  accessState: 'applied',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:20:00Z',
}

const mcpPlan: PublishPlan = {
  id: 'mcp_plan_1',
  tenantId: 'tenant-test',
  entityType: 'publishPlan',
  publicationId: 'mcp_pub_1',
  gatewayId: 'gateway_1',
  digest: 'digest',
  steps: [{ kind: 'api', name: 'mosaic-mcp-contoso-tools', action: 'noChange', reason: 'Already matches.', resourceId: '/apis/mosaic-mcp-contoso-tools', existed: true }],
  facets: [],
  policyContentSha256: 'policy-digest',
  warnings: [],
  target: 'mcp',
  mcpAccessSnapshot: { version: 1, audience: 'runtime-client-id', delegatedScope: 'Mcp.Invoke', applicationRole: 'Mcp.Invoke.Application', grants: [] },
  previousAccessVersion: 1,
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

const mcpUnpublishPlan: PublishPlan = {
  ...mcpPlan,
  id: 'mcp_unpublish_plan',
  operation: 'unpublish',
  steps: [
    {
      kind: 'api',
      name: 'mosaic-mcp-contoso-tools',
      action: 'delete',
      reason: 'Delete the MCP API. The gateway stops serving the server.',
      resourceId: '/apis/mosaic-mcp-contoso-tools',
      existed: true,
    },
  ],
  warnings: ["Before it deletes anything, MOSAIC replaces the MCP API's policy with one that refuses every call, so callers are cut off first."],
  mcpAccessSnapshot: {
    version: 2,
    audience: 'runtime-client-id',
    delegatedScope: 'Mcp.Invoke',
    applicationRole: 'Mcp.Invoke.Application',
    grants: [{
      entitlementId: 'mcp_grant_1',
      subject: { kind: 'user', id: 'principal_1' },
      objectId: 'user-object-1',
      displayName: 'Megan Bowen',
      enabled: true,
      enforcement: null,
      intentDigest: 'digest',
    }],
  },
}

const mcpRun: PublishRun = {
  id: 'mcp_run_2',
  tenantId: 'tenant-test',
  entityType: 'publishRun',
  publicationId: 'mcp_pub_1',
  gatewayId: 'gateway_1',
  planId: 'mcp_plan_1',
  planDigest: 'digest',
  status: 'succeeded',
  startedAt: '2026-09-01T12:20:00Z',
  completedAt: '2026-09-01T12:21:00Z',
  durationMs: 60000,
  steps: [],
  rolledBack: false,
  orphanedResources: [],
  errors: [],
  target: 'mcp',
  createdAt: '2026-09-01T12:20:00Z',
  updatedAt: '2026-09-01T12:21:00Z',
}

function buildMcpEndpoint(overrides: Partial<McpEndpoint> = {}): McpEndpoint {
  return {
    id: 'mcpEndpoint_1',
    tenantId: 'tenant-test',
    name: 'Contoso tools',
    endpoint: 'https://mcp.contoso.com/mcp',
    environment: null,
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
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  listGateways: vi.fn(),
  listMcpPublications: vi.fn(),
  planMcpPublication: vi.fn(),
  planUnpublishMcpPublication: vi.fn(),
  unpublishMcpPublication: vi.fn(),
  listPrincipals: vi.fn(),
  deleteMcpPublication: vi.fn(),
  getMcpPublicationLock: vi.fn(),
  recoverMcpPublication: vi.fn(),
  listMcpServers: vi.fn(),
  deleteMcpServer: vi.fn(),
  listImportableMcpServers: vi.fn(),
  importMcpServers: vi.fn(),
  listMcpEndpoints: vi.fn(),
  getMcpPublishingCapability: vi.fn(),
  createMcpPublication: vi.fn(),
  applyMcpPublication: vi.fn(),
  getMcpPublishRun: vi.fn(),
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
          usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
        },
      ],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.listEnvironmentFindings.mockResolvedValue({
      items: [],
      limitations: [],
      generatedAt: '2026-09-01T12:00:00Z',
    })
    api.listGateways.mockResolvedValue([buildGateway()])
    api.listMcpPublications.mockResolvedValue([])
    api.getMcpPublishingCapability.mockResolvedValue({ gatewayId: 'gateway_1', supported: true, reasons: [], warnings: [] })
    api.planMcpPublication.mockResolvedValue(mcpPlan)
    api.planUnpublishMcpPublication.mockResolvedValue(mcpUnpublishPlan)
    api.unpublishMcpPublication.mockResolvedValue(mcpRun)
    api.listPrincipals.mockResolvedValue([])
    api.deleteMcpPublication.mockResolvedValue(undefined)
    api.getMcpPublicationLock.mockResolvedValue({ publicationId: 'mcp_pub_1', ownerId: null })
    api.recoverMcpPublication.mockResolvedValue(mcpRun)
    api.createMcpPublication.mockResolvedValue(mcpPublication)
    api.applyMcpPublication.mockResolvedValue(mcpRun)
    api.getMcpPublishRun.mockResolvedValue(mcpRun)
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
    expect(screen.getByText(/MCP grant enforcement depends on how the server is governed/)).toBeVisible()
  })

  it('shows the transport and gateway of an imported server', async () => {
    api.listMcpServers.mockResolvedValue([mcpServer])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Imported MCP servers' })
    expect(within(table).getByText('Weather MCP')).toBeVisible()
    expect(within(table).getByText('Passthrough · SSE')).toBeVisible()
    expect(within(table).getByRole('link', { name: 'Development gateway' })).toBeVisible()
  })

  it('shows published MCP servers with server URL and opens plan review', async () => {
    const user = userEvent.setup()
    api.listMcpPublications.mockResolvedValue([mcpPublication])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published MCP servers' })
    expect(within(table).getByText('Contoso tools')).toBeVisible()
    expect(within(table).getByText('https://apim-contoso-dev.azure-api.net/mosaic/mcp/contoso-tools/mcp')).toBeVisible()
    expect(within(table).getByText('Published')).toBeVisible()
    expect(within(table).getByText('Access applied')).toBeVisible()

    await user.click(within(table).getByRole('button', { name: 'Plan and apply' }))
    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(api.planMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(await screen.findByRole('table', { name: 'MCP publish plan steps' })).toBeVisible()
  })

  it('reviews what unpublishing removes and who loses access before it unpublishes', async () => {
    const user = userEvent.setup()
    api.listMcpPublications.mockResolvedValue([mcpPublication])

    renderPage()
    const table = await screen.findByRole('table', { name: 'Published MCP servers' })
    await user.click(within(table).getByRole('button', { name: 'Unpublish' }))

    const dialog = await screen.findByRole('alertdialog', { name: 'Unpublish Contoso tools?' })
    expect(await within(dialog).findByText('1 grant loses access. The gateway refuses its Entra tokens.')).toBeVisible()
    expect(within(dialog).getByText('Megan Bowen')).toBeVisible()
    expect(within(dialog).getByRole('table', { name: 'Unpublish plan steps' })).toHaveTextContent(
      'Delete the MCP API. The gateway stops serving the server.',
    )
    expect(api.planUnpublishMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(api.unpublishMcpPublication).not.toHaveBeenCalled()
    // Fluent returns focus to the button that opened the dialog when it closes.
    expect(within(table).getByRole('button', { name: 'Unpublish', hidden: true })).toHaveAttribute(
      'data-tabster',
      expect.stringContaining('restorer'),
    )

    await user.click(within(dialog).getByRole('button', { name: 'Unpublish MCP server' }))

    await waitFor(() => expect(api.unpublishMcpPublication).toHaveBeenCalledWith('mcp_pub_1', 'mcp_unpublish_plan'))
    expect(api.unpublishMcpPublication).toHaveBeenCalledTimes(1)
  })

  it('shows an unpublished MCP server as unpublished, with when and without an access state', async () => {
    const unpublishedAt = '2026-09-30T11:20:00Z'
    api.listMcpPublications.mockResolvedValue([
      { ...mcpPublication, status: 'draft', resources: [], unpublishedAt },
    ])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published MCP servers' })
    expect(within(table).getByText('Unpublished')).toBeVisible()
    expect(within(table).getByText(`Unpublished ${new Date(unpublishedAt).toLocaleString()}`)).toBeVisible()
    expect(within(table).queryByText('Access applied')).not.toBeInTheDocument()
    expect(within(table).queryByText('Draft')).not.toBeInTheDocument()
    expect(within(table).getByRole('button', { name: 'Plan and apply' })).toBeEnabled()
  })

  it('says a server MOSAIC publishes is hidden from the portal while it is not published', async () => {
    api.listMcpServers.mockResolvedValue([
      { ...mcpServer, publicationId: 'mcp_pub_1' },
      { ...mcpServer, id: 'mcpServer_2', displayName: 'Orders MCP', publicationId: null },
    ])
    api.listMcpPublications.mockResolvedValue([{ ...mcpPublication, status: 'draft', resources: [] }])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Imported MCP servers' })
    const [, published, imported] = within(table).getAllByRole('row')
    expect(
      await within(published).findByText("Hidden from the portal catalog while it isn't published."),
    ).toBeVisible()
    expect(within(imported).queryByText(/Hidden from the portal catalog/)).not.toBeInTheDocument()
  })

  it('marks MOSAIC-published imported rows and points deletion to the publication', async () => {
    api.listMcpServers.mockResolvedValue([{ ...mcpServer, publicationId: 'mcp_pub_1' }])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Imported MCP servers' })
    expect(within(table).getByText('Published by MOSAIC')).toBeVisible()
    expect(within(table).getByText('Delete the publication instead.')).toBeVisible()
    expect(within(table).queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
  })

  it('opens the import dialog from the gateway query', async () => {
    renderPage('/mcps?import=gateway_1')

    expect(await screen.findByRole('dialog')).toBeVisible()
    const orders = await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    // Detection pre-checks the list once it arrives, so the check lands a render later.
    await waitFor(() => expect(orders).toBeChecked())
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

    await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
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
