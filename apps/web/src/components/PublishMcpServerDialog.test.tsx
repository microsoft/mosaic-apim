import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PublishMcpServerDialog } from './PublishMcpServerDialog'
import type { Gateway, McpEndpoint, McpPublication, PublishPlan, PublishRun } from '../types'

function gateway(overrides: Partial<Gateway> = {}): Gateway {
  return {
    id: 'gateway_1',
    tenantId: 'tenant-test',
    name: 'Managed gateway',
    provider: 'apim',
    azureResourceId: '/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim',
    subscriptionId: 's',
    resourceGroup: 'rg',
    serviceName: 'apim',
    environment: null,
    environmentLabel: null,
    managementMode: 'manage',
    status: 'connected',
    access: { canRead: true, canWrite: true, evaluation: 'effectivePermissions', missingActions: [], remediation: null, message: 'Ready.' },
    capabilities: { managementApiVersion: '2024-05-01', aiGatewayPolicies: 'available', mcpServers: 'available', gatewayUrl: 'https://gateway.example.test', identityObserved: true, notes: [] },
    inventory: { apis: 0, aiApis: 0, mcpServers: 0, operations: 0, products: 0, subscriptions: 0, users: 0, groups: 0, backends: 0, namedValues: 0, policyDocuments: 0, policyFragments: 0, recognizedFacets: 0, unrecognizedFacets: 0, mosaicManagedFacets: 0 },
    lastSyncedAt: '2026-09-01T12:00:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

function endpoint(overrides: Partial<McpEndpoint> = {}): McpEndpoint {
  return {
    id: 'endpoint_1',
    tenantId: 'tenant-test',
    name: 'Weather tools',
    endpoint: 'https://mcp.example.test/mcp',
    environment: null,
    environmentLabel: null,
    authMode: 'none',
    credentialReferenceId: null,
    resourceAudience: null,
    status: 'connected',
    access: { canDiscover: true, evaluation: 'handshake', checkedAt: '2026-09-01T12:00:00Z', challenge: null, message: null },
    capabilities: { offeredProtocolVersion: '2025-11-25', protocolVersion: '2025-11-25', transportType: 'streamable', supportsTools: 'available', sessionManaged: true, notes: [] },
    inventory: { tools: 2, readOnlyTools: 1, unannotatedTools: 0 },
    lastSyncedAt: '2026-09-01T12:00:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

const publication: McpPublication = {
  id: 'mcp_pub_1',
  tenantId: 'tenant-test',
  entityType: 'mcpPublication',
  gatewayId: 'gateway_1',
  mcpEndpointId: 'endpoint_1',
  displayName: 'Weather tools',
  apiName: 'mosaic-mcp-weather-tools',
  apiPath: 'mosaic/mcp/weather-tools',
  backendName: 'mosaic-mcp-weather-tools',
  fragmentName: 'mosaic-mcp-weather-tools',
  metadataApiName: 'mosaic-mcp-weather-tools-prm',
  mcpServerId: 'mcp_1',
  status: 'planned',
  resources: [],
  lastPlanId: 'plan_1',
  lastPlanDigest: 'digest',
  lastRunId: null,
  lastAppliedAt: null,
  lastError: null,
  appliedAccess: null,
  accessState: 'pending',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

const plan: PublishPlan = {
  id: 'plan_1',
  tenantId: 'tenant-test',
  entityType: 'publishPlan',
  publicationId: 'mcp_pub_1',
  gatewayId: 'gateway_1',
  digest: 'digest',
  steps: [{ kind: 'api', name: 'mosaic-mcp-weather-tools', action: 'create', reason: 'Create the MCP API.', resourceId: '/apis/mosaic-mcp-weather-tools', existed: false, stage: 'prepare' }],
  facets: [{ kind: 'authorization', element: 'validate-azure-ad-token', section: 'inbound', summary: 'Validates MCP caller tokens.', details: ['Requires Mcp.Invoke.'], attributes: {}, confidence: 'recognized', managedByMosaic: true }],
  policyContentSha256: 'policy-digest',
  warnings: ['VS Code and other interactive clients need consent for api://runtime-client-id/Mcp.Invoke'],
  target: 'mcp',
  accessSnapshot: null,
  mcpAccessSnapshot: {
    version: 1,
    audience: 'runtime-client-id',
    delegatedScope: 'Mcp.Invoke',
    applicationRole: 'Mcp.Invoke.Application',
    grants: [{
      entitlementId: 'grant_1',
      subject: { kind: 'securityGroup', id: 'sg_1' },
      objectId: '00000000-0000-0000-0000-000000000001',
      displayName: 'Security readers',
      enabled: true,
      enforcement: { requests: { counterKeyExpression: '@(context.Subscription.Id)', calls: 60, renewalPeriodSeconds: 60 } },
      intentDigest: 'digest',
    }],
  },
  previousAccessVersion: null,
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

function run(overrides: Partial<PublishRun> = {}): PublishRun {
  return {
    id: 'run_1',
    tenantId: 'tenant-test',
    entityType: 'publishRun',
    publicationId: 'mcp_pub_1',
    gatewayId: 'gateway_1',
    planId: 'plan_1',
    planDigest: 'digest',
    status: 'succeeded',
    startedAt: '2026-09-01T12:00:00Z',
    completedAt: '2026-09-01T12:00:02Z',
    durationMs: 2000,
    steps: [{ kind: 'api', name: 'mosaic-mcp-weather-tools', action: 'create', status: 'succeeded', resourceId: '/apis/mosaic-mcp-weather-tools', createdByMosaic: true, error: null, stage: 'prepare' }],
    rolledBack: false,
    orphanedResources: [],
    errors: [],
    target: 'mcp',
    mcpAccessSnapshot: plan.mcpAccessSnapshot,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:02Z',
    ...overrides,
  }
}

const api = {
  listGateways: vi.fn(),
  listMcpEndpoints: vi.fn(),
  getMcpPublishingCapability: vi.fn(),
  createMcpPublication: vi.fn(),
  updateMcpPublication: vi.fn(),
  deleteMcpPublication: vi.fn(),
  planMcpPublication: vi.fn(),
  applyMcpPublication: vi.fn(),
  getMcpPublishRun: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderDialog(onPublished = vi.fn()) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PublishMcpServerDialog open onClose={vi.fn()} onPublished={onPublished} />
    </QueryClientProvider>,
  )
}

describe('PublishMcpServerDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([gateway()])
    api.listMcpEndpoints.mockResolvedValue([endpoint()])
    api.getMcpPublishingCapability.mockResolvedValue({ gatewayId: 'gateway_1', supported: true, reasons: [], warnings: [] })
    api.createMcpPublication.mockResolvedValue(publication)
    api.updateMcpPublication.mockResolvedValue(publication)
    api.deleteMcpPublication.mockResolvedValue(undefined)
    api.planMcpPublication.mockResolvedValue(plan)
    api.applyMcpPublication.mockResolvedValue(run())
    api.getMcpPublishRun.mockResolvedValue(run())
  })

  it('blocks publishing when gateway capability returns reasons', async () => {
    api.getMcpPublishingCapability.mockResolvedValue({
      gatewayId: 'gateway_1',
      supported: false,
      reasons: ['Synchronize the gateway before publishing MCP servers.'],
      warnings: ['Diagnostics must log zero-byte response bodies for MCP streaming.'],
    })
    renderDialog()

    expect(await screen.findByText('Synchronize the gateway before publishing MCP servers.')).toBeVisible()
    expect(screen.getByText('Diagnostics must log zero-byte response bodies for MCP streaming.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Configure' })).toBeDisabled()
  })

  it('explains endpoint blockers before create', async () => {
    api.listMcpEndpoints.mockResolvedValue([
      endpoint({ id: 'blocked', authMode: 'apiKey', capabilities: { ...endpoint().capabilities, transportType: 'sse' } }),
    ])
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable MCP servers' })
    expect(within(table).getByText("MOSAIC can't publish an MCP server that needs an API key yet.")).toBeVisible()
    expect(within(table).getByText('SSE servers cannot be published. Register a Streamable HTTP endpoint instead.')).toBeVisible()
    expect(within(table).getByRole('checkbox', { name: 'Publish Weather tools' })).toBeDisabled()
  })

  it('creates, plans, reviews MCP grants, applies, and polls the run', async () => {
    const user = userEvent.setup()
    const onPublished = vi.fn()
    renderDialog(onPublished)

    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    expect(screen.getByRole('textbox', { name: 'API name' })).toHaveValue('mosaic-mcp-weather-tools')
    await user.click(screen.getByRole('button', { name: 'Review plan' }))

    await waitFor(() => expect(api.createMcpPublication).toHaveBeenCalledWith({
      gatewayId: 'gateway_1',
      mcpEndpointId: 'endpoint_1',
      displayName: 'Weather tools',
      apiName: 'mosaic-mcp-weather-tools',
      apiPath: 'mosaic/mcp/weather-tools',
    }))
    expect(api.planMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(await screen.findByText('Security readers')).toBeVisible()
    expect(screen.getByText('Security group')).toBeVisible()
    expect(screen.getByText('Limits traffic to 60 calls per minute.')).toBeVisible()
    expect(screen.getByText('Create the MCP API.')).toBeVisible()
    expect(screen.getByText('Validates MCP caller tokens.')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Apply plan' }))
    await waitFor(() => expect(api.applyMcpPublication).toHaveBeenCalledWith('mcp_pub_1', 'plan_1'))
    expect(await screen.findByText('Local development service reported completion; live APIM apply is not verified.')).toBeVisible()
    expect(await screen.findByRole('table', { name: 'MCP publish run steps' })).toBeVisible()
    expect(onPublished).toHaveBeenCalledWith('Local development service reported completion. Live APIM apply is not verified.')
  })

  it('reuses its draft when a failed plan is retried, rather than creating a duplicate', async () => {
    const user = userEvent.setup()
    api.planMcpPublication.mockRejectedValueOnce(new Error('The gateway did not respond.'))
    renderDialog()

    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    expect(await screen.findByText('The gateway did not respond.')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    expect(await screen.findByText('Security readers')).toBeVisible()
    expect(api.createMcpPublication).toHaveBeenCalledTimes(1)
    expect(api.planMcpPublication).toHaveBeenCalledTimes(2)
    expect(api.planMcpPublication).toHaveBeenLastCalledWith('mcp_pub_1')
    expect(api.updateMcpPublication).not.toHaveBeenCalled()
    expect(api.deleteMcpPublication).not.toHaveBeenCalled()
  })

  it('after going back, renames its draft in place and replaces it when an API name changes', async () => {
    const user = userEvent.setup()
    renderDialog()
    await user.click(await screen.findByRole('checkbox', { name: 'Publish Weather tools' }))
    await user.click(screen.getByRole('button', { name: 'Configure' }))
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')

    api.updateMcpPublication.mockResolvedValue({ ...publication, displayName: 'Forecasts' })
    await user.click(screen.getByRole('button', { name: 'Back' }))
    const displayName = screen.getByRole('textbox', { name: 'Display name' })
    await user.clear(displayName)
    await user.type(displayName, 'Forecasts')
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')
    expect(api.updateMcpPublication).toHaveBeenCalledWith('mcp_pub_1', { displayName: 'Forecasts' })
    expect(api.createMcpPublication).toHaveBeenCalledTimes(1)
    expect(api.deleteMcpPublication).not.toHaveBeenCalled()

    // API names shape API Management resources and can't be edited, so the unapplied draft is
    // replaced instead.
    api.createMcpPublication.mockResolvedValue({
      ...publication, displayName: 'Forecasts', apiName: 'mosaic-mcp-forecasts',
    })
    await user.click(screen.getByRole('button', { name: 'Back' }))
    const apiName = screen.getByRole('textbox', { name: 'API name' })
    await user.clear(apiName)
    await user.type(apiName, 'mosaic-mcp-forecasts')
    await user.click(screen.getByRole('button', { name: 'Review plan' }))
    await screen.findByText('Security readers')
    expect(api.deleteMcpPublication).toHaveBeenCalledWith('mcp_pub_1')
    expect(api.createMcpPublication).toHaveBeenCalledTimes(2)
    expect(api.createMcpPublication).toHaveBeenLastCalledWith({
      gatewayId: 'gateway_1',
      mcpEndpointId: 'endpoint_1',
      displayName: 'Forecasts',
      apiName: 'mosaic-mcp-forecasts',
      apiPath: 'mosaic/mcp/weather-tools',
    })
    expect(api.planMcpPublication).toHaveBeenCalledTimes(3)
  })
})
