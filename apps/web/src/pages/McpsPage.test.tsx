import { QueryClient, QueryClientProvider, useMutation } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation, useNavigate } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { McpsPage } from './McpsPage'
import type { Gateway, McpEndpoint, McpPublication, McpServer, ObservedMcpTool, Principal, PublishPlan, PublishRun } from '../types'

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

const supportBot: Principal = {
  id: 'principal-support-bot',
  tenantId: 'tenant-test',
  objectId: 'aaaabbbb-cccc-dddd-eeee-ffff00001111',
  kind: 'servicePrincipal',
  label: 'Support bot',
  detail: 'Contoso support application',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

const adele: Principal = {
  id: 'principal-adele',
  tenantId: 'tenant-test',
  objectId: 'bbbbcccc-dddd-eeee-ffff-000011112222',
  kind: 'user',
  label: 'Adele Vance',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
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
  setMcpModelCaller: vi.fn(),
  clearMcpModelCaller: vi.fn(),
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
  assignEnvironments: vi.fn(),
  updateMcpServerCatalog: vi.fn(),
}

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: class extends Error {},
}))

function LocationProbe() {
  const location = useLocation()
  const navigate = useNavigate()
  return <><span data-testid="location">{`${location.pathname}${location.search}${location.hash}`}</span>
    <button onClick={() => navigate(-1)}>History back</button>
    <button onClick={() => navigate(1)}>History forward</button>
  </>
}

function UnrelatedPendingMutation() {
  const mutation = useMutation({
    mutationKey: ['unrelated'],
    mutationFn: () => new Promise<void>(() => {}),
  })
  return <button onClick={() => mutation.mutate()}>Start unrelated operation</button>
}

function renderMcpsPage(entry = '/mcps', includeUnrelatedMutation = false, initialEntries = [entry]) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  const router = createMemoryRouter([{ path: '*', element: <>
        <McpsPage />
        {includeUnrelatedMutation && <UnrelatedPendingMutation />}
        <LocationProbe />
      </> }], { initialEntries })
  return render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

function renderPage(entry = '/mcps', includeUnrelatedMutation = false) {
  const url = new URL(entry, 'https://mosaic.example')
  if (!url.searchParams.has('selected') && !url.searchParams.has('import')) {
    const section = url.searchParams.get('tab')
    url.searchParams.set('selected', section === 'registered' ? 'mcpEndpoint_1' : section === 'imported' ? 'mcpServer_1' : 'mcp_pub_1')
  }
  return renderMcpsPage(`${url.pathname}${url.search}`, includeUnrelatedMutation)
}

async function openRowMenu(user: ReturnType<typeof userEvent.setup>, scope: HTMLElement) {
  await user.click(await within(scope).findByRole('button', { name: /^More actions for/ }))
}

async function findDetails(name: string) {
  const region = await screen.findByRole('region', { name })
  await within(region).findAllByRole('heading', { level: 3 })
  return region
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
    api.setMcpModelCaller.mockResolvedValue(mcpPublication)
    api.clearMcpModelCaller.mockResolvedValue(mcpPublication)
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
    api.assignEnvironments.mockResolvedValue({ results: [], grantsCarried: 0, warnings: [] })
    api.updateMcpServerCatalog.mockResolvedValue(mcpServer)
  })

  it('allows section changes while an unrelated mutation is pending', async () => {
    const user = userEvent.setup()
    renderPage('/mcps', true)

    await user.click(screen.getByRole('button', { name: 'Start unrelated operation' }))
    await user.click(screen.getByRole('button', { name: 'Registered MCP servers' }))

    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
  })

  it('loads only the selected section and keeps lifecycle tasks separate', async () => {
    const user = userEvent.setup()
    renderPage('/mcps?tab=invalid')
    expect(await screen.findByText('No MCP servers published yet')).toBeVisible()
    expect(api.listMcpEndpoints).not.toHaveBeenCalled()
    expect(api.listImportableMcpServers).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Registered MCP servers' }))
    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Published MCP servers' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Imported MCP servers' }))
    expect(await screen.findByText('No MCP servers imported yet')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Register server' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Import from gateway' })).toBeVisible()
  })

  it('invites an import when nothing has been adopted yet', async () => {
    renderPage('/mcps?tab=imported')

    expect(await screen.findByText('No MCP servers imported yet')).toBeVisible()
    expect(screen.getByText(/MCP grant enforcement depends on how the server is governed/)).toBeVisible()
  })

  it('shows the transport and gateway of an imported server', async () => {
    api.listMcpServers.mockResolvedValue([mcpServer])

    renderPage('/mcps?tab=imported')

    const table = await findDetails('Imported server details')
    expect(within(table).getByText('Weather MCP')).toBeVisible()
    expect(within(table).getByText('Passthrough · SSE')).toBeVisible()
    expect(within(table).getByRole('link', { name: 'Development gateway' })).toBeVisible()
  })

  it('shows published MCP servers with server URL and opens plan review', async () => {
    const user = userEvent.setup()
    api.listMcpPublications.mockResolvedValue([mcpPublication])

    renderPage()

    const table = await findDetails('Published server details')
    expect(within(table).getByText('Contoso tools')).toBeVisible()
    expect(within(table).getByText('https://apim-contoso-dev.azure-api.net/mosaic/mcp/contoso-tools/mcp')).toBeVisible()
    expect(within(table).getByText('Published')).toBeVisible()
    expect(within(table).getByText('Access applied')).toBeVisible()

    await user.click(within(table).getByRole('button', { name: /Review changes|Review publish plan/ }))
    expect(await screen.findByRole('heading', { name: 'Review MCP access' })).toBeVisible()
    expect(api.planMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(await screen.findByRole('table', { name: 'MCP publish plan steps' })).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.queryByRole('list', { name: 'Published MCP servers' })).not.toBeInTheDocument()
    expect(screen.getByText('Preview the changes MOSAIC would apply. Nothing changes until you apply all pending changes.')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Back' }))
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'Review MCP access' })).not.toBeInTheDocument())
    const opener = screen.getByRole('button', { name: /Review changes|Review publish plan/ })
    await waitFor(() => expect(screen.getByRole('region', { name: 'Published server details' })).toHaveFocus())
    await user.click(opener)
    await user.click(await screen.findByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Published server details' })).toHaveFocus())
    expect(api.applyMcpPublication).not.toHaveBeenCalled()
  })

  it('takes over the workspace while planning and blocks leaving an active plan request', async () => {
    const user = userEvent.setup()
    let finish!: (value: PublishPlan) => void
    api.listMcpPublications.mockResolvedValue([mcpPublication])
    api.planMcpPublication.mockReturnValueOnce(new Promise<PublishPlan>((resolve) => { finish = resolve }))
    renderPage()
    await user.click(await screen.findByRole('button', { name: /Review changes|Review publish plan/ }))
    expect(await screen.findByText('Preparing MCP review')).toBeVisible()
    expect(screen.queryByRole('list', { name: 'Published MCP servers' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByRole('dialog', { name: 'Operation in progress' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Discard changes' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(api.planMcpPublication).toHaveBeenCalledTimes(1)
    await act(async () => { finish(mcpPlan) })
    await user.click(await screen.findByRole('button', { name: 'Close' }))
    await waitFor(() => expect(screen.getByRole('region', { name: 'Published server details' })).toHaveFocus())
    expect(api.applyMcpPublication).not.toHaveBeenCalled()
  })

  it.each(['Publish an MCP server', 'Publish through a gateway'])('opens %s in the workspace and returns to its selected detail', async (name) => {
    const user = userEvent.setup()
    api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
    renderPage(name === 'Publish through a gateway' ? '/mcps?tab=registered' : '/mcps')
    const fromRow = name === 'Publish through a gateway'
    const opener = await screen.findByRole('button', { name: fromRow ? /^More actions for/ : name })
    await user.click(opener)
    if (fromRow) await user.click(await screen.findByRole('menuitem', { name }))
    expect(await screen.findByRole('heading', { name: 'Publish an MCP server' })).toBeVisible()
    expect(screen.queryByRole('list', { name: fromRow ? 'Registered MCP servers' : 'Published MCP servers' })).not.toBeInTheDocument()
    if (fromRow) expect(await screen.findByRole('checkbox', { name: 'Publish Contoso tools' })).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Back' }))
    if (fromRow) {
      await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    }
    await waitFor(() => expect(screen.getByRole('region', { name: fromRow ? 'Registered server details' : 'Published server details' })).toHaveFocus())
  })

  it('shows the applied model caller for a published MCP server', async () => {
    api.listPrincipals.mockResolvedValue([supportBot])
    api.listMcpPublications.mockResolvedValue([{
      ...mcpPublication,
      modelCallerId: supportBot.id,
      appliedAccess: {
        version: 1,
        audience: 'runtime-client-id',
        delegatedScope: 'Mcp.Invoke',
        applicationRole: 'Mcp.Invoke.Application',
        modelCaller: { principalId: supportBot.id, objectId: supportBot.objectId, displayName: 'Support bot' },
        grants: [],
      },
    }])

    renderPage()

    const table = await findDetails('Published server details')
    expect(within(table).getByText('Model-calling identity')).toBeVisible()
    expect(within(table).getByText('Support bot')).toBeVisible()
    expect(within(table).getByText('Applied')).toBeVisible()
  })

  it('shows a changed model caller as not applied until plan and apply', async () => {
    api.listPrincipals.mockResolvedValue([supportBot])
    api.listMcpPublications.mockResolvedValue([{
      ...mcpPublication,
      modelCallerId: supportBot.id,
      appliedAccess: {
        version: 1,
        audience: 'runtime-client-id',
        delegatedScope: 'Mcp.Invoke',
        applicationRole: 'Mcp.Invoke.Application',
        modelCaller: { principalId: 'principal-old-bot', objectId: 'ccccdddd-eeee-ffff-0000-111122223333', displayName: 'Old bot' },
        grants: [],
      },
    }])

    renderPage()

    const table = await findDetails('Published server details')
    expect(within(table).getByText(/Not applied yet/)).toBeVisible()
    expect(within(table).getByText('Still live as Old bot. Review changes and apply the plan to switch.')).toBeVisible()
    expect(within(table).getByRole('button', { name: /Review changes|Review publish plan/ })).toBeVisible()
  })

  it('says what to do when a model caller is named but never applied, or cleared while live', async () => {
    api.listPrincipals.mockResolvedValue([supportBot])
    const live = {
      version: 1,
      audience: 'runtime-client-id',
      delegatedScope: 'Mcp.Invoke',
      applicationRole: 'Mcp.Invoke.Application',
      modelCaller: { principalId: supportBot.id, objectId: supportBot.objectId, displayName: 'Support bot' },
      grants: [],
    }
    api.listMcpPublications.mockResolvedValue([
      { ...mcpPublication, modelCallerId: supportBot.id },
      { ...mcpPublication, id: 'mcp_pub_2', displayName: 'Contoso Search', apiName: 'search', modelCallerId: null, appliedAccess: live },
      { ...mcpPublication, id: 'mcp_pub_3', displayName: 'Contoso Files', apiName: 'files' },
    ])

    renderPage()

    const named = await findDetails('Published server details')
    expect(within(named).getByText('Not applied yet')).toBeVisible()
    expect(within(named).getByText('Review changes and apply the plan to start attributing its model calls.')).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: /^Contoso Search/ }))
    const cleared = screen.getByRole('region', { name: 'Published server details' })
    expect(within(cleared).getByText('None')).toBeVisible()
    expect(
      within(cleared).getByText('Still live as Support bot. Review changes and apply the plan to stop attributing its model calls.'),
    ).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: /^Contoso Files/ }))
    const none = screen.getByRole('region', { name: 'Published server details' })
    expect(within(none).getAllByText('None')).toHaveLength(1)
    expect(within(none).queryByText('Not applied yet')).not.toBeInTheDocument()
    expect(within(none).queryByRole('button', { name: /^More model caller actions for/ })).not.toBeInTheDocument()
  })

  it('sets a model caller from application principals only', async () => {
    const user = userEvent.setup()
    api.listPrincipals.mockResolvedValue([supportBot, adele])
    api.listMcpPublications.mockResolvedValue([mcpPublication])

    renderPage()

    const table = await findDetails('Published server details')
    await user.click(within(table).getByRole('button', { name: 'Choose' }))
    const dialog = await screen.findByRole('region', { name: 'Model-calling identity' })
    expect(screen.queryByRole('list', { name: 'Published MCP servers' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('option', { name: /Support bot/ })).toBeVisible()
    expect(within(dialog).queryByRole('option', { name: /Adele Vance/ })).not.toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.setMcpModelCaller).toHaveBeenCalledWith('mcp_pub_1', supportBot.id))
  })

  it('clears a model caller', async () => {
    const user = userEvent.setup()
    api.listPrincipals.mockResolvedValue([supportBot])
    api.listMcpPublications.mockResolvedValue([{ ...mcpPublication, modelCallerId: supportBot.id }])

    renderPage()

    const table = await findDetails('Published server details')
    await user.click(within(table).getByRole('button', { name: 'Change' }))
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Application principal' }), '')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(api.clearMcpModelCaller).toHaveBeenCalledWith('mcp_pub_1'))
  })

  it('shows model caller API errors', async () => {
    const user = userEvent.setup()
    api.listPrincipals.mockResolvedValue([supportBot])
    api.listMcpPublications.mockResolvedValue([mcpPublication])
    api.setMcpModelCaller.mockRejectedValue(new Error('Only applications can call models for an MCP server.'))

    renderPage()

    const table = await findDetails('Published server details')
    await user.click(within(table).getByRole('button', { name: 'Choose' }))
    await user.click(within(await screen.findByRole('region', { name: 'Model-calling identity' })).getByRole('button', { name: 'Save' }))

    expect((await screen.findAllByText('Only applications can call models for an MCP server.')).length).toBeGreaterThan(0)
  })

  it('reviews what unpublishing removes and who loses access before it unpublishes', async () => {
    const user = userEvent.setup()
    api.listMcpPublications.mockResolvedValue([mcpPublication])

    renderPage()
    const table = await findDetails('Published server details')
    await openRowMenu(user, table)
    await user.click(await screen.findByRole('menuitem', { name: 'Unpublish' }))

    const dialog = await screen.findByRole('alertdialog', { name: 'Unpublish Contoso tools?' })
    expect(await within(dialog).findByText('1 grant loses access. The gateway refuses its Entra tokens.')).toBeVisible()
    expect(within(dialog).getByText('Megan Bowen')).toBeVisible()
    expect(within(dialog).getByRole('table', { name: 'Unpublish plan steps' })).toHaveTextContent(
      'Delete the MCP API. The gateway stops serving the server.',
    )
    expect(api.planUnpublishMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(api.unpublishMcpPublication).not.toHaveBeenCalled()
    // Restore dialog focus to the persistent overflow trigger.
    expect(within(table).getByRole('button', { name: /^More actions for/, hidden: true })).toHaveAttribute(
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

    const table = await findDetails('Published server details')
    expect(within(table).getByText('Unpublished')).toBeVisible()
    expect(within(table).getByText(`Unpublished ${new Date(unpublishedAt).toLocaleString()}`)).toBeVisible()
    expect(within(table).queryByText('Access applied')).not.toBeInTheDocument()
    expect(within(table).queryByText('Draft')).not.toBeInTheDocument()
    expect(within(table).getByRole('button', { name: /Review changes|Review publish plan/ })).toBeEnabled()
  })

  it('says a server MOSAIC publishes is hidden from the portal while it is not published', async () => {
    api.listMcpServers.mockResolvedValue([
      { ...mcpServer, publicationId: 'mcp_pub_1' },
      { ...mcpServer, id: 'mcpServer_2', displayName: 'Orders MCP', publicationId: null },
    ])
    api.listMcpPublications.mockResolvedValue([{ ...mcpPublication, status: 'draft', resources: [] }])

    renderPage('/mcps?tab=imported')

    const published = await findDetails('Imported server details')
    expect(
      await within(published).findByText("Hidden from the portal catalog while it isn't published."),
    ).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: /^Orders MCP/ }))
    expect(within(screen.getByRole('region', { name: 'Imported server details' })).queryByText(/Hidden from the portal catalog/)).not.toBeInTheDocument()
  })

  it('marks MOSAIC-published imported rows and points deletion to the publication', async () => {
    api.listMcpServers.mockResolvedValue([{ ...mcpServer, publicationId: 'mcp_pub_1' }])

    renderPage('/mcps?tab=imported')

    const table = await findDetails('Imported server details')
    expect(within(table).getByText('Published by MOSAIC')).toBeVisible()
    expect(within(table).getByText('Remove the publication record instead.')).toBeVisible()
    expect(within(table).queryByRole('button', { name: 'Remove' })).not.toBeInTheDocument()
  })

  it('opens the import dialog from the gateway query', async () => {
    renderPage('/mcps?import=gateway_1')

    expect(await screen.findByRole('heading', { name: 'Import MCP servers from a gateway' })).toBeVisible()
    const orders = await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    // Detection pre-checks the list once it arrives, so the check lands a render later.
    await waitFor(() => expect(orders).toBeChecked())
  })

  it('canonicalizes a legacy MCP import link without losing gateway preselection or unrelated URL state', async () => {
    api.listGateways.mockResolvedValue([
      buildGateway({ id: 'other-gateway', name: 'Other gateway' }),
      buildGateway(),
    ])
    renderMcpsPage('/mcps?import=gateway_1&environment=development&scope=kept#details')
    const gateway = await screen.findByRole('combobox', { name: 'Gateway to import from' })
    await waitFor(() => expect(gateway).toHaveValue('gateway_1'))
    const params = new URL(screen.getByTestId('location').textContent!, 'https://mosaic.example').searchParams
    expect(params.has('import')).toBe(false)
    expect(params.get('tab')).toBe('imported')
    expect(params.get('mode')).toBe('import')
    expect(params.get('importGateway')).toBe('gateway_1')
    expect(params.get('environment')).toBe('development')
    expect(params.get('scope')).toBe('kept')
    expect(screen.getByTestId('location')).toHaveTextContent('#details')
    expect(api.listImportableMcpServers).toHaveBeenCalledWith('gateway_1')
  })

  it.each([
    ['Registered MCP servers', 'registered', 'Register server'],
    ['Published MCP servers', 'published', 'Publish an MCP server'],
  ])('allows leaving a consumed MCP import link for %s without reopening Import', async (sectionLabel, tab, action) => {
    const user = userEvent.setup()
    renderMcpsPage('/mcps?import=gateway_1&scope=kept')
    await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    await user.click(screen.getByRole('button', { name: sectionLabel }))
    expect(await screen.findByRole('button', { name: action })).toBeVisible()
    await waitFor(() => expect(new URL(screen.getByTestId('location').textContent!, 'https://mosaic.example').searchParams.get('tab')).toBe(tab))
    expect(screen.queryByRole('heading', { name: 'Import MCP servers from a gateway' })).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('scope=kept')
    expect(api.importMcpServers).not.toHaveBeenCalled()
  })

  it('honors Stay and confirmed discard when switching sections from a consumed MCP import draft', async () => {
    const user = userEvent.setup()
    renderMcpsPage('/mcps?import=gateway_1&scope=kept')
    const orders = await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    await waitFor(() => expect(orders).toBeChecked())
    await user.click(orders)
    await user.click(screen.getByRole('button', { name: 'Registered MCP servers' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByRole('heading', { name: 'Import MCP servers from a gateway' })).toBeVisible()
    expect(orders).not.toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Registered MCP servers' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Import MCP servers from a gateway' })).not.toBeInTheDocument()
    expect(api.importMcpServers).not.toHaveBeenCalled()
  })

  it.each(['Cancel', 'Back'])('keeps a consumed MCP import link closed after workspace %s and later section changes', async (action) => {
    const user = userEvent.setup()
    renderMcpsPage('/mcps?import=gateway_1&scope=kept')
    await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    await user.click(screen.getByRole('button', { name: action }))
    expect(await screen.findByRole('button', { name: 'Import from gateway' })).toBeVisible()
    const params = new URL(screen.getByTestId('location').textContent!, 'https://mosaic.example').searchParams
    expect(params.has('import')).toBe(false)
    expect(params.has('importGateway')).toBe(false)
    expect(params.has('mode')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Published MCP servers' }))
    expect(await screen.findByRole('button', { name: 'Publish an MCP server' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Imported MCP servers' }))
    expect(await screen.findByRole('button', { name: 'Import from gateway' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Import MCP servers from a gateway' })).not.toBeInTheDocument()
    expect(api.importMcpServers).not.toHaveBeenCalled()
  })

  it('restores gateway preselection from a canonical MCP import workspace URL', async () => {
    api.listGateways.mockResolvedValue([
      buildGateway({ id: 'other-gateway', name: 'Other gateway' }),
      buildGateway(),
    ])
    renderMcpsPage('/mcps?tab=imported&mode=import&importGateway=gateway_1&scope=kept')
    const gateway = await screen.findByRole('combobox', { name: 'Gateway to import from' })
    await waitFor(() => expect(gateway).toHaveValue('gateway_1'))
    const orders = await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    await waitFor(() => expect(orders).toBeChecked())
    expect(screen.getByText('Orders MCP')).toBeVisible()
    expect(screen.getByTestId('location')).toHaveTextContent('scope=kept')
    expect(api.importMcpServers).not.toHaveBeenCalled()
  })

  it('replaces a legacy MCP import history entry so Back and Forward after cancel do not reopen Import', async () => {
    const user = userEvent.setup()
    const legacy = '/mcps?import=gateway_1&scope=kept'
    renderMcpsPage(legacy, false, ['/mcps?tab=registered', legacy])
    await screen.findByRole('checkbox', { name: 'Import Orders MCP' })
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('button', { name: 'Import from gateway' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'History back' }))
    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'History forward' }))
    expect(await screen.findByRole('button', { name: 'Import from gateway' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Import MCP servers from a gateway' })).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('scope=kept')
    expect(api.importMcpServers).not.toHaveBeenCalled()
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
    await waitFor(() => expect(screen.getByTestId('location')).toHaveTextContent('selected=mcpServer_1'))
    expect(screen.getByTestId('location')).not.toHaveTextContent('import=')
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

  it('keeps registration metadata out of compact rows until a server is selected', async () => {
    const user = userEvent.setup()
    api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
    renderMcpsPage('/mcps?tab=registered')
    const list = await screen.findByRole('list', { name: 'Registered MCP servers' })
    expect(await within(list).findByText('Contoso tools')).toBeVisible()
    expect(within(list).getByText(/2 tools/)).toBeVisible()
    expect(within(list).queryByText('https://mcp.contoso.com/mcp')).not.toBeInTheDocument()
    expect(within(list).queryByText(/Protocol/)).not.toBeInTheDocument()
    expect(api.listMcpEndpointTools).not.toHaveBeenCalled()
    await user.click(within(list).getByRole('button', { name: /^Contoso tools/ }))
    expect(await screen.findByText('https://mcp.contoso.com/mcp')).toBeVisible()
    await waitFor(() => expect(api.listMcpEndpointTools).toHaveBeenCalledWith('mcpEndpoint_1'))
  })

  it('preserves section and environment filters through selection and browser history', async () => {
    const user = userEvent.setup()
    api.listMcpEndpoints.mockResolvedValue([
      buildMcpEndpoint({ environment: 'development' }),
      buildMcpEndpoint({ id: 'second', name: 'Orders tools', environment: 'development' }),
    ])
    renderMcpsPage('/mcps?tab=registered&environment=development&scope=kept')
    await user.click(await screen.findByRole('button', { name: /^Contoso tools/ }))
    await user.click(screen.getByRole('button', { name: /^Orders tools/ }))
    const params = () => new URL(screen.getByTestId('location').textContent!, 'https://mosaic.example').searchParams
    expect(params().get('tab')).toBe('registered')
    expect(params().get('environment')).toBe('development')
    expect(params().get('scope')).toBe('kept')
    expect(params().get('selected')).toBe('second')
    await user.click(screen.getByRole('button', { name: 'History back' }))
    await waitFor(() => expect(params().get('selected')).toBe('mcpEndpoint_1'))
    await user.click(screen.getByRole('button', { name: 'History forward' }))
    await waitFor(() => expect(params().get('selected')).toBe('second'))
  })

  it('protects a registration draft across section changes and restores its draft on Stay', async () => {
    const user = userEvent.setup()
    renderMcpsPage('/mcps?tab=registered&environment=development')
    await user.click(await screen.findByRole('button', { name: 'Register server' }))
    expect(screen.queryByRole('list', { name: 'Registered MCP servers' })).not.toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: /Server URL/ }), 'https://draft.example/mcp')
    await user.click(screen.getByRole('button', { name: 'Imported MCP servers' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByRole('textbox', { name: /Server URL/ })).toHaveValue('https://draft.example/mcp')
    expect(api.registerMcpEndpoint).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
    expect(screen.getByTestId('location')).toHaveTextContent('environment=development')
  })

  it('guards pending registration and returns with the saved resource selected without persisting its credentials', async () => {
    const user = userEvent.setup()
    let finish!: (endpoint: McpEndpoint) => void
    api.registerMcpEndpoint.mockReturnValueOnce(new Promise<McpEndpoint>((resolve) => { finish = resolve }))
    renderMcpsPage('/mcps?tab=registered&environment=development')
    await user.click(await screen.findByRole('button', { name: 'Register server' }))
    await user.type(screen.getByRole('textbox', { name: /Server URL/ }), 'https://a.example/mcp')
    await user.click(screen.getByRole('tab', { name: 'Key Vault secret' }))
    await user.type(screen.getByRole('textbox', { name: /Key Vault secret URI/ }), 'https://my-vault.vault.azure.net/secrets/mcp-token')
    await user.click(screen.getByRole('button', { name: 'Register' }))
    await waitFor(() => expect(api.registerMcpEndpoint).toHaveBeenCalledWith(expect.objectContaining({
      authMode: 'apiKey', credentialSecretUri: 'https://my-vault.vault.azure.net/secrets/mcp-token',
    })))
    expect(screen.getByTestId('location')).not.toHaveTextContent('my-vault')
    expect(screen.getByTestId('location')).not.toHaveTextContent('a.example')
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByRole('dialog', { name: 'Operation in progress' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    const saved = buildMcpEndpoint({ environment: 'development' })
    api.listMcpEndpoints.mockResolvedValue([saved])
    await act(async () => { finish(saved) })
    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
    expect(screen.queryByRole('dialog', { name: 'Discard unsaved changes?' })).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('selected=mcpEndpoint_1')
    expect(screen.getByTestId('location')).toHaveTextContent('environment=development')
  })

  it('edits a registered environment in the workspace without changing credentials or its server', async () => {
    const user = userEvent.setup()
    api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
    renderPage('/mcps?tab=registered')
    await openRowMenu(user, await findDetails('Registered server details'))
    await user.click(await screen.findByRole('menuitem', { name: 'Change environment' }))
    expect(await screen.findByRole('heading', { name: 'Edit Contoso tools' })).toBeVisible()
    expect(screen.queryByRole('list', { name: 'Registered MCP servers' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('combobox', { name: 'Environment' }))
    await user.click(await screen.findByRole('option', { name: 'Development' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(api.assignEnvironments).toHaveBeenCalledWith({
      assignments: [{ resourceKind: 'mcpEndpoint', resourceId: 'mcpEndpoint_1', environment: 'development' }],
      acknowledgeGrants: false,
    }))
    expect(api.registerMcpEndpoint).not.toHaveBeenCalled()
    expect(api.syncMcpEndpoint).not.toHaveBeenCalled()
    await waitFor(() => expect(new URL(screen.getByTestId('location').textContent!, 'https://mosaic.example').searchParams.get('mode')).toBeNull())
    expect(await screen.findByRole('button', { name: 'Register server' })).toBeVisible()
  })

  it.each([
    { status: 'applying', accessState: 'applied' },
    { status: 'published', accessState: 'applying' },
    { status: 'published', accessState: 'unknown' },
  ] as const)('keeps publication and identity operations locked for $status / $accessState', async (state) => {
    api.listMcpPublications.mockResolvedValue([{ ...mcpPublication, ...state }])
    api.listPrincipals.mockResolvedValue([supportBot])
    renderPage()
    const detail = await findDetails('Published server details')
    expect(within(detail).getByRole('button', { name: /Review changes|Review publish plan/ })).toBeDisabled()
    expect(within(detail).getByRole('button', { name: 'Choose' })).toBeDisabled()
    await openRowMenu(userEvent.setup(), detail)
    expect(await screen.findByRole('menuitem', { name: 'Unpublish' })).toHaveAttribute('aria-disabled', 'true')
    expect(api.planMcpPublication).not.toHaveBeenCalled()
    expect(api.applyMcpPublication).not.toHaveBeenCalled()
  })

  it('requires explicit acknowledgment before carrying active grants during an MCP environment edit', async () => {
    const user = userEvent.setup()
    api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
    api.assignEnvironments.mockRejectedValueOnce(Object.assign(new Error('Acknowledge the grants.'), {
      body: { details: {
        reason: 'grantsAcknowledgmentRequired', grants: [], grantCount: 1, principalCount: 1, truncated: false,
      } },
    }))
    renderPage('/mcps?tab=registered')
    await openRowMenu(user, await findDetails('Registered server details'))
    await user.click(await screen.findByRole('menuitem', { name: 'Change environment' }))
    await user.click(await screen.findByRole('combobox', { name: 'Environment' }))
    await user.click(await screen.findByRole('option', { name: 'Development' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByText('Active grants will carry across environments')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Confirm and save' })).toBeDisabled()
    expect(api.assignEnvironments).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('checkbox', { name: 'I understand these active grants will carry to the new environment.' }))
    await user.click(screen.getByRole('button', { name: 'Confirm and save' }))
    await waitFor(() => expect(api.assignEnvironments).toHaveBeenLastCalledWith({
      assignments: [{ resourceKind: 'mcpEndpoint', resourceId: 'mcpEndpoint_1', environment: 'development' }],
      acknowledgeGrants: true,
    }))
  })

  it('preserves the reviewed environment-pair refusal and explicit companion assignment', async () => {
    const user = userEvent.setup()
    api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
    const suggestion = { resourceKind: 'gateway', resourceId: 'gateway_1', environment: 'development' }
    api.assignEnvironments.mockRejectedValueOnce(Object.assign(new Error('Pair is blocked.'), {
      body: { details: {
        reason: 'publicationsBlocked', suggestedAssignments: [suggestion],
        publications: [{
          kind: 'mcp', publicationId: mcpPublication.id, displayName: mcpPublication.displayName,
          gatewayId: 'gateway_1', gatewayName: 'Development gateway', gatewayEnvironment: null,
          mcpEndpointId: 'mcpEndpoint_1', mcpEndpointName: 'Contoso tools', endpointEnvironment: null,
          verdict: { level: 'blocked', reason: 'The endpoint and gateway must be classified together.' },
        }],
      } },
    }))
    renderPage('/mcps?tab=registered')
    await openRowMenu(user, await findDetails('Registered server details'))
    await user.click(await screen.findByRole('menuitem', { name: 'Change environment' }))
    await user.click(await screen.findByRole('combobox', { name: 'Environment' }))
    await user.click(await screen.findByRole('option', { name: 'Development' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('table', { name: 'Blocked publications' })).toHaveTextContent('The endpoint and gateway must be classified together.')
    expect(api.assignEnvironments).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Also classify this resource as Development' }))
    await waitFor(() => expect(api.assignEnvironments).toHaveBeenLastCalledWith({
      assignments: [
        { resourceKind: 'mcpEndpoint', resourceId: 'mcpEndpoint_1', environment: 'development' }, suggestion,
      ],
      acknowledgeGrants: undefined,
    }))
  })

  it('loads a direct review link without applying anything', async () => {
    api.listMcpPublications.mockResolvedValue([mcpPublication])
    renderMcpsPage('/mcps?tab=published&selected=mcp_pub_1&mode=review&scope=kept')
    expect(await screen.findByRole('table', { name: 'MCP publish plan steps' })).toBeVisible()
    expect(api.planMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(api.applyMcpPublication).not.toHaveBeenCalled()
    expect(screen.queryByRole('list', { name: 'Published MCP servers' })).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('scope=kept')
  })

  it('updates catalog discovery separately from imported server authorization and removal', async () => {
    const user = userEvent.setup()
    api.listMcpServers.mockResolvedValue([mcpServer])
    renderPage('/mcps?tab=imported')
    const detail = await findDetails('Imported server details')
    await user.selectOptions(within(detail).getByRole('combobox', { name: 'Catalog visibility for Weather MCP' }), 'private')
    await waitFor(() => expect(api.updateMcpServerCatalog).toHaveBeenCalledWith('mcpServer_1', { visibility: 'private' }))
    expect(api.applyMcpPublication).not.toHaveBeenCalled()
    await user.click(within(detail).getByRole('button', { name: 'Remove import' }))
    await waitFor(() => expect(api.deleteMcpServer).toHaveBeenCalledWith('mcpServer_1'))
  })

  describe('registered servers', () => {
    function renderPage(entry = '/mcps?tab=registered') {
      return renderMcpsPage(`${entry}&selected=mcpEndpoint_1`)
    }
    it('invites a registration when nothing is registered yet', async () => {
      renderPage()

      expect(await screen.findByText('No MCP servers registered yet')).toBeVisible()
    })

    it('shows the negotiated protocol and tool counts', async () => {
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])

      renderPage()

      const table = await findDetails('Registered server details')
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

    it('does not register missing authentication inputs or a blank URL', async () => {
      const user = userEvent.setup()
      renderPage()
      await user.click(await screen.findByRole('button', { name: 'Register server' }))
      await user.click(screen.getByRole('button', { name: 'Register' }))
      expect(api.registerMcpEndpoint).not.toHaveBeenCalled()
      await user.type(screen.getByRole('textbox', { name: /Server URL/ }), 'https://a.example/mcp')
      await user.click(screen.getByRole('tab', { name: 'Managed identity' }))
      await user.click(screen.getByRole('button', { name: 'Register' }))
      expect(api.registerMcpEndpoint).not.toHaveBeenCalled()
      await user.click(screen.getByRole('tab', { name: 'Key Vault secret' }))
      await user.click(screen.getByRole('button', { name: 'Register' }))
      expect(api.registerMcpEndpoint).not.toHaveBeenCalled()
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

      const table = await findDetails('Registered server details')
      await openRowMenu(userEvent.setup(), table)
      expect(await screen.findByRole('menuitem', { name: 'Sync tools' })).toHaveAttribute('aria-disabled', 'true')
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
      await user.click(await screen.findByRole('button', { name: /^Contoso tools/ }))

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

      const table = await findDetails('Registered server details')
      expect(within(table).getByText('Protocol not supported')).toBeVisible()
    })

    it('renders an absent annotation as not stated rather than as its default', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
      api.listMcpEndpointTools.mockResolvedValue([readOnlyTool, unannotatedTool])

      renderPage()
      await user.click(await screen.findByRole('button', { name: /^Contoso tools/ }))

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
      await user.click(await screen.findByRole('button', { name: /^Contoso tools/ }))

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
      await user.click(await screen.findByRole('button', { name: /^Contoso tools/ }))

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
      await user.click(await screen.findByRole('button', { name: /^Contoso tools/ }))

      expect(
        await screen.findByText(/did not advertise a tools capability/),
      ).toBeVisible()
    })

    it('syncs and removes a registered server', async () => {
      const user = userEvent.setup()
      api.listMcpEndpoints.mockResolvedValue([buildMcpEndpoint()])
      api.deleteMcpEndpoint.mockResolvedValue(undefined)

      renderPage()
      const table = await findDetails('Registered server details')

      await openRowMenu(user, table)
      await user.click(await screen.findByRole('menuitem', { name: 'Sync tools' }))
      await waitFor(() => expect(api.syncMcpEndpoint).toHaveBeenCalledWith('mcpEndpoint_1'), {
        timeout: 5000,
      })

      await openRowMenu(user, table)
      await user.click(await screen.findByRole('menuitem', { name: 'Remove MOSAIC record' }))
      await waitFor(() => expect(api.deleteMcpEndpoint).toHaveBeenCalledWith('mcpEndpoint_1'), {
        timeout: 5000,
      })
      expect(
        await screen.findByText(/Stopped governing that MCP server. The server itself is unchanged/),
      ).toBeVisible()
    })
  })
})
