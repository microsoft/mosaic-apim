import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FluentProvider, textClassNames, webLightTheme } from '@fluentui/react-components'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccessRequest, Entitlement, EnvironmentCatalogView, McpPublication, McpServer, Principal } from '../types'
import { GOVERNED_COUNTER_KEY, callRateError, describeLimits, describePublicationLimits } from '../entitlement-limits'
import { EntitlementsPage } from './EntitlementsPage'
import { accessPlan, directGrant, modelPublication, publishedModelApi } from '../test/model-access'
import { anthropicPool, draftPool, governedAnthropicPool } from '../test/pool-fixtures'

const entitlement: Entitlement = {
  id: 'entitlement_1',
  tenantId: 'tenant',
  subject: { kind: 'group', id: 'group_1' },
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  enabled: true,
  enforcement: {
    tokens: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      tokensPerMinute: 10000,
      tokenQuota: 5000000,
      tokenQuotaPeriod: 'Monthly',
      estimatePromptTokens: true,
    },
  },
  binding: null,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
}

const api = {
  getEnvironmentCatalog: vi.fn(),
  listEntitlements: vi.fn(),
  listPrincipals: vi.fn(),
  listGroups: vi.fn(),
  listModelApis: vi.fn(),
  listModelPools: vi.fn(),
  listMcpServers: vi.fn(),
  listCostCenters: vi.fn(),
  getCostCenterSettings: vi.fn(),
  listMcpPublications: vi.fn(),
  listGateways: vi.fn(),
  listModelEndpoints: vi.fn(),
  listAccessRequests: vi.fn(),
  listPublications: vi.fn(),
  resolveEntitlements: vi.fn(),
  createEntitlement: vi.fn(),
  updateEntitlement: vi.fn(),
  deleteEntitlement: vi.fn(),
  getPublication: vi.fn(),
  createPublishPlan: vi.fn(),
  applyPublishPlan: vi.fn(),
  updatePublication: vi.fn(),
  linkPublicationModelApi: vi.fn(),
  approveAccessRequest: vi.fn(),
  denyAccessRequest: vi.fn(),
  getGrantOverlaps: vi.fn(),
  getMcpConnection: vi.fn(),
  getEntitlementConnection: vi.fn(),
  createEntitlementKey: vi.fn(),
  rotateEntitlementKey: vi.fn(),
  deleteEntitlementKey: vi.fn(),
  revealEntitlementKey: vi.fn(),
}

const catalog: EnvironmentCatalogView = {
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
      usage: { gateways: 1, modelEndpoints: 1, mcpEndpoints: 0 },
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
      usage: { gateways: 1, modelEndpoints: 1, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

const pendingRequest: AccessRequest = {
  id: 'request_1',
  tenantId: 'tenant',
  // Entra object IDs are matched to principals without regard to letter case.
  requesterObjectId: 'USER-OBJECT-1',
  requesterPrincipalId: null,
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  justification: 'Support bot evaluation',
  state: 'pending',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

const docsServer: McpServer = {
  id: 'mcp_1',
  tenantId: 'tenant',
  gatewayId: 'gateway_1',
  apiName: 'mosaic-mcp-docs',
  displayName: 'Docs search',
  path: 'mosaic/mcp/docs',
  serviceUrl: 'https://mcp.contoso.test',
  protocols: ['https'],
  kind: 'passthrough',
  transportType: 'streamable',
  endpoints: [],
  tools: [],
  toolCount: 0,
  subscriptionRequired: false,
  productNames: [],
  visibility: 'catalog',
  selection: 'detected',
  importedFromSnapshotId: null,
  importedAt: '2026-09-01T12:20:00Z',
  importedBy: null,
  publicationId: 'mcp_pub_1',
  createdAt: '2026-09-01T12:10:00Z',
  updatedAt: '2026-09-01T12:10:00Z',
}

const docsPublication: McpPublication = {
  id: 'mcp_pub_1',
  tenantId: 'tenant',
  entityType: 'mcpPublication',
  gatewayId: 'gateway_1',
  mcpEndpointId: 'mcpEndpoint_1',
  displayName: 'Docs search',
  apiName: 'mosaic-mcp-docs',
  apiPath: 'mosaic/mcp/docs',
  backendName: 'mosaic-mcp-docs',
  fragmentName: 'mosaic-mcp-docs',
  metadataApiName: 'mosaic-mcp-docs-prm',
  mcpServerId: 'mcp_1',
  status: 'published',
  resources: [],
  lastPlanId: 'mcp_plan_1',
  lastPlanDigest: 'digest',
  lastRunId: 'mcp_run_1',
  lastAppliedAt: '2026-09-01T12:20:00Z',
  lastError: null,
  appliedAccess: {
    version: 1,
    audience: 'runtime-client-id',
    delegatedScope: 'Mcp.Invoke',
    applicationRole: 'Mcp.Invoke.Application',
    grants: [],
  },
  accessState: 'applied',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:20:00Z',
}

async function openApproval(user: ReturnType<typeof userEvent.setup>) {
  const requests = await screen.findByRole('table', { name: 'Pending access requests' })
  const approve = within(requests).getByRole('button', { name: 'Approve' })
  await waitFor(() => expect(approve).toBeEnabled())
  await user.click(approve)
  return screen.findByRole('dialog')
}

/** Each body row's cell under the named column header, in row order. */
function columnCells(table: HTMLElement, header: string) {
  const column = within(table).getAllByRole('columnheader').findIndex((cell) => cell.textContent === header)
  return within(table).getAllByRole('row').slice(1).map((row) => within(row).getAllByRole('cell')[column])
}

/** Each line of text in an element, in order. */
function textLines(element: Element) {
  return [...element.querySelectorAll(`.${textClassNames.root}`)].map((line) => line.textContent)
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

/** Shows where the page navigated, since the test renders it without the app's routes. */
function LocationProbe() {
  return <output aria-label="Current location">{useLocation().pathname}</output>
}

function renderPage(initial = '/entitlements') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <FluentProvider theme={webLightTheme}>
          <EntitlementsPage />
          <LocationProbe />
        </FluentProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('EntitlementsPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.listEntitlements.mockResolvedValue([entitlement])
    api.listPrincipals.mockResolvedValue([{
      id: 'principal_1', tenantId: 'tenant', objectId: 'user-object-1', kind: 'user',
      label: 'Ada Lovelace', createdAt: '', updatedAt: '',
    }])
    api.listGroups.mockResolvedValue([{
      id: 'group_1', tenantId: 'tenant', name: 'Engineering', createdAt: '', updatedAt: '',
    }])
    api.getEnvironmentCatalog.mockResolvedValue(catalog)
    api.listGateways.mockResolvedValue([
      { id: 'gateway_1', environment: 'production', name: 'Prod gateway' },
      { id: 'gateway_dev', environment: 'development', name: 'Dev gateway' },
    ])
    api.listModelEndpoints.mockResolvedValue([
      { id: 'endpoint_1', environment: 'development', name: 'Dev endpoint' },
    ])
    api.listModelApis.mockResolvedValue([{ ...publishedModelApi, publicationId: null, importedFromSnapshotId: 'snapshot_1' }])
    api.listModelPools.mockResolvedValue([])
    api.listMcpServers.mockResolvedValue([])
    api.listCostCenters.mockResolvedValue([{ id: 'cc_general', tenantId: 'tenant', entityType: 'costCenter', name: 'General', code: 'general', description: null, owners: [], members: [], keysAllowed: true, limits: [], builtIn: true, createdAt: '', updatedAt: '', isTenantDefault: true, memberDetails: [], grantCount: 0, enabledGrantCount: 0, defaultFor: 0 }])
    api.getCostCenterSettings.mockResolvedValue({ id: 'settings', tenantId: 'tenant', defaultCostCenterId: 'cc_general' })
    api.listMcpPublications.mockResolvedValue([])
    api.listAccessRequests.mockResolvedValue([])
    api.listPublications.mockResolvedValue([])
    api.resolveEntitlements.mockResolvedValue([])
    api.getGrantOverlaps.mockResolvedValue({
      overlaps: [],
      membershipChecked: true,
      skipped: [],
      generatedAt: '2026-09-01T12:00:00Z',
    })
    api.createPublishPlan.mockResolvedValue(accessPlan)
    api.getPublication.mockResolvedValue(modelPublication)
  })

  it('renders live grants with their subject, resource, and binding state', async () => {
    renderPage()

    expect(await screen.findByText('Engineering (MOSAIC group)')).toBeVisible()
    expect(screen.getByText('Chat completions (model API)')).toBeVisible()
    expect(screen.getAllByText('Production').length).toBeGreaterThan(0)
    // A grant with no binding must say so: consumption cannot be attributed without one.
    expect(screen.getByText('Not bound')).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
  })

  it('shows cost-center names and codes in the grants table', async () => {
    api.listCostCenters.mockResolvedValue([
      { id: 'cc-support', tenantId: 'tenant', entityType: 'costCenter', name: 'Support', code: 'support', description: null, owners: [], members: [], keysAllowed: true, limits: [], builtIn: false, createdAt: '', updatedAt: '', isTenantDefault: false, memberDetails: [], grantCount: 1, enabledGrantCount: 1, defaultFor: 0 },
    ])
    api.listEntitlements.mockResolvedValue([{ ...entitlement, costCenterId: 'cc-support', costCenter: { id: 'cc-support', name: 'Support', code: 'support' } }])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Entitlements' })
    const row = within(table).getAllByRole('row').find((item) => item.textContent?.includes('Engineering'))
    await waitFor(() => expect(row).toHaveTextContent('Support'))
    expect(row).toHaveTextContent('support')
    // The cost center sits under its own heading, not under Resource.
    const headings = within(table).getAllByRole('columnheader').map((cell) => cell.textContent)
    const cells = within(row as HTMLElement).getAllByRole('cell')
    expect(cells[headings.indexOf('Cost center')]).toHaveTextContent('Supportsupport')
  })

  it('filters grants by cost center from the query string and from the filter', async () => {
    const user = userEvent.setup()
    api.listCostCenters.mockResolvedValue([
      { id: 'cc-general', tenantId: 'tenant', entityType: 'costCenter', name: 'General', code: 'general', description: null, owners: [], members: [], keysAllowed: true, limits: [], builtIn: true, createdAt: '', updatedAt: '', isTenantDefault: true, memberDetails: [], grantCount: 0, enabledGrantCount: 0, defaultFor: 0 },
      { id: 'cc-support', tenantId: 'tenant', entityType: 'costCenter', name: 'Support', code: 'support', description: null, owners: [], members: [], keysAllowed: true, limits: [], builtIn: false, createdAt: '', updatedAt: '', isTenantDefault: false, memberDetails: [], grantCount: 0, enabledGrantCount: 0, defaultFor: 0 },
    ])
    renderPage('/entitlements?costCenter=cc-support')

    const filter = await screen.findByRole('combobox', { name: 'Filter grants by cost center' })
    await waitFor(() => expect(filter).toHaveValue('cc-support'))
    await waitFor(() => expect(api.listEntitlements).toHaveBeenCalledWith({ costCenter: 'cc-support' }))

    await user.selectOptions(filter, 'cc-general')
    await waitFor(() => expect(api.listEntitlements).toHaveBeenLastCalledWith({ costCenter: 'cc-general' }))
  })

  it('sends the selected cost center when creating a grant', async () => {
    const user = userEvent.setup()
    api.listCostCenters.mockResolvedValue([
      { id: 'cc-support', tenantId: 'tenant', entityType: 'costCenter', name: 'Support', code: 'support', description: null, owners: [], members: [], keysAllowed: true, limits: [], builtIn: false, createdAt: '', updatedAt: '', isTenantDefault: false, memberDetails: [], grantCount: 0, enabledGrantCount: 0, defaultFor: 0 },
    ])
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Add entitlement' }))
    const dialog = await screen.findByRole('dialog')
    const selects = within(dialog).getAllByRole('combobox')
    await user.selectOptions(selects[0], 'group_1')
    await user.selectOptions(selects[1], 'modelApi_1')
    await user.selectOptions(selects[2], 'cc-support')
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))

    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith(expect.objectContaining({ costCenterId: 'cc-support' })))
  })

  it('shows when a grant was revoked because its subject left the cost center', async () => {
    api.listEntitlements.mockResolvedValue([{
      ...entitlement,
      revocation: { reason: 'costCenterMembership', costCenterId: 'cc-general', revokedAt: '2026-09-01T12:00:00Z', revokedBy: 'admin' },
    }])
    renderPage()

    expect(await screen.findByText('Revoked when its subject left the cost center.')).toBeVisible()
  })

  it('keeps the whole APIM subscription name in the binding cell, with its source on a line of its own', async () => {
    // A managed subscription name is long and has no spaces. Administrators look the subscription
    // up by this name, so the cell must hold all of it rather than a shortened form.
    const subscriptionName = 'mosaic-grant-0123456789abcdef0123456789abcdef'
    api.listEntitlements.mockResolvedValue([
      {
        ...directGrant,
        binding: { gatewayId: 'gateway_1', source: 'orchestrated', apimSubscriptionName: subscriptionName },
      },
      { ...entitlement, id: 'recorded_grant', binding: { gatewayId: 'gateway_1', source: 'manual' } },
      entitlement,
    ])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Entitlements' })
    const [named, recorded, unbound] = columnCells(table, 'Binding')
    expect(within(named).getByText(subscriptionName)).toBeVisible()
    expect(within(named).getByText('orchestrated')).toBeVisible()
    expect(named).toHaveAccessibleName(`${subscriptionName} orchestrated`)
    expect(within(recorded).getByText('Recorded')).toBeVisible()
    expect(within(recorded).getByText('manual')).toBeVisible()
    expect(unbound).toHaveTextContent(/^Not bound$/)
  })

  it('filters grants by environment, including deployment grants from their endpoint', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([
      entitlement,
      {
        ...entitlement,
        id: 'deployment_grant',
        resource: { kind: 'modelDeployment', id: 'gpt-4o', scopeId: 'endpoint_1' },
      },
    ])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Entitlements' })
    expect(within(table).getByText('Production')).toBeVisible()
    expect(within(table).getByText('Development')).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Filter grants by environment'), 'development')

    const filtered = await screen.findByRole('table', { name: 'Entitlements' })
    expect(within(filtered).getByText('Development')).toBeVisible()
    expect(within(filtered).queryByText('Chat completions (model API)')).not.toBeInTheDocument()
  })

  it('shows request display names, requested environment, moved note, and removed resource label', async () => {
    api.listAccessRequests.mockResolvedValue([{
      ...pendingRequest,
      requestedEnvironment: 'development',
      resourceSnapshot: { displayName: 'Legacy chat', gatewayId: 'gateway_1', gatewayName: 'Gateway' },
      resourceSummary: {
        kind: 'modelApi',
        id: 'modelApi_1',
        scopeId: null,
        displayName: 'Chat completions',
        gatewayId: 'gateway_1',
        gatewayName: 'Gateway',
        environment: 'production',
        available: false,
      },
    }])
    renderPage()

    const requests = await screen.findByRole('table', { name: 'Pending access requests' })
    expect(within(requests).getByText('Chat completions')).toBeVisible()
    expect(within(requests).getByText('No longer available')).toBeVisible()
    expect(within(requests).getByText('Development')).toBeVisible()
    expect(within(requests).getByText('Now Production')).toBeVisible()
    expect(within(requests).queryByText('modelApi_1')).not.toBeInTheDocument()
  })

  it('states limits as sentences rather than policy markup', async () => {
    renderPage()

    expect(await screen.findByText('Limits usage to 10,000 tokens per minute.')).toBeVisible()
    expect(screen.getByText('Allows 5,000,000 tokens per month.')).toBeVisible()
  })

  it('distinguishes pending desired intent from earlier applied access and inherited limits', async () => {
    api.listEntitlements.mockResolvedValue([{
      ...directGrant, runtime: { ...directGrant.runtime, status: 'pending' },
    }])
    api.listPublications.mockResolvedValue([{ ...modelPublication, accessState: 'pending' }])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Entitlements' })
    expect(within(table).getByText('Desired: enabled')).toBeVisible()
    expect(within(table).getByText('Saved, not applied')).toBeVisible()
    expect(within(table).getByText('An earlier configuration was applied; saved changes are pending.')).toBeVisible()
    expect(within(table).getByText('Last applied methods: Subscription key OR Entra token')).toBeVisible()
    expect(within(table).getAllByText('Publication: Limits usage to 12,000 tokens per minute.')).toHaveLength(2)
    expect(within(table).queryByText(/unrestricted/i)).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('saves revocation intent instead of deleting managed access, and can re-enable it', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([directGrant])
    api.listPublications.mockResolvedValue([modelPublication])
    api.updateEntitlement.mockImplementation(async (_id, { enabled }: { enabled: boolean }) => {
      const updated = {
        ...directGrant, enabled,
        runtime: { ...directGrant.runtime, status: enabled ? 'pending' : 'revocationPending' },
      }
      api.listEntitlements.mockResolvedValue([updated])
      return updated
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Revoke' }))
    expect(api.updateEntitlement).toHaveBeenCalledWith(directGrant.id, { enabled: false })
    expect(api.deleteEntitlement).not.toHaveBeenCalled()
    expect(await screen.findByText('Revocation pending')).toBeVisible()
    expect(screen.getByText(/Access may still work until/)).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Re-enable' }))
    expect(api.updateEntitlement).toHaveBeenLastCalledWith(directGrant.id, { enabled: true })
    expect(await screen.findByText('Saved, not applied')).toBeVisible()
  })

  it('preserves desired-only group grant deletion without claiming APIM revocation', async () => {
    const user = userEvent.setup()
    api.deleteEntitlement.mockResolvedValue(undefined)
    renderPage()
    expect(await screen.findByText('Desired state only')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(api.deleteEntitlement).toHaveBeenCalledWith(entitlement.id)
    expect(api.updateEntitlement).not.toHaveBeenCalled()
    expect(await screen.findByText('Removed the desired-state grant. API Management is unchanged.')).toBeVisible()
  })

  it('keeps imported direct and MCP grants desired-only', async () => {
    api.listEntitlements.mockResolvedValue([
      { ...directGrant, binding: { gatewayId: 'gateway_1', source: 'manual', attributionKey: 'grant:direct_grant' }, runtime: null },
      { ...directGrant, id: 'mcp-grant', resource: { kind: 'mcpServer', id: 'mcp_1' }, binding: null, runtime: null },
    ])
    renderPage()
    expect(await screen.findByText('Desired state only')).toBeVisible()
    expect(screen.getByText('Recorded, not enforced')).toBeVisible()
    const [gatewayLog] = columnCells(screen.getByRole('table', { name: 'Entitlements' }), 'Binding')
    expect(within(gatewayLog).getByText('Gateway log')).toBeVisible()
    expect(within(gatewayLog).getByText('manual')).toBeVisible()
    expect(screen.getByText(/doesn't enforce it for an imported MCP server/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Connection info' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Manage model' })).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('offers only models whose API is in API Management for governed access', async () => {
    api.listPublications.mockResolvedValue([
      modelPublication,
      // A failed re-apply still leaves the model published.
      {
        ...modelPublication,
        id: 'pub_failed',
        displayName: 'Retried chat',
        status: 'failed',
        resources: [
          { kind: 'api', name: modelPublication.apiName, resourceId: '/apis/chat', createdByMosaic: true, appliedAt: modelPublication.createdAt },
        ],
      },
      // Reviewing an unpublished model's access would publish it again.
      { ...modelPublication, id: 'pub_unpublished', displayName: 'Retired chat', status: 'draft', unpublishedAt: modelPublication.createdAt },
    ])
    renderPage()

    const picker = await screen.findByRole('combobox', { name: 'Published model' })
    await within(picker).findByRole('option', { name: /Published chat/ })
    expect(within(picker).getByRole('option', { name: /Retried chat/ })).toBeInTheDocument()
    expect(within(picker).queryByRole('option', { name: /Retired chat/ })).not.toBeInTheDocument()
  })

  it('creates direct grant intent on the canonical model without changing Azure', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.listModelApis.mockResolvedValue([publishedModelApi])
    api.createEntitlement.mockResolvedValue(directGrant)
    renderPage()
    await screen.findByRole('option', { name: /Published chat/ })
    fireEvent.change(screen.getByLabelText('Published model'), { target: { value: modelPublication.id } })
    await user.click(await screen.findByRole('button', { name: 'Add direct grant' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('option', { name: 'Engineering (MOSAIC group)' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('combobox', { name: 'Resource' })).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith({
      subject: { kind: 'user', id: 'principal_1' },
      resource: { kind: 'modelApi', id: publishedModelApi.id },
      enforcement: null,
      notes: null,
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(await screen.findByText(/Saved grant intent. API Management is unchanged/)).toBeVisible()
  })

  it('groups subjects by identity kind and sends the mapped subject kind', async () => {
    const user = userEvent.setup()
    api.listPrincipals.mockResolvedValue([
      { id: 'person_1', tenantId: 'tenant', objectId: 'person-object', kind: 'user', label: 'Ada Person', createdAt: '', updatedAt: '' },
      { id: 'agent_1', tenantId: 'tenant', objectId: 'agent-object', kind: 'agentIdentity', label: 'Agent identity', createdAt: '', updatedAt: '' },
      { id: 'agent_user_1', tenantId: 'tenant', objectId: 'agent-user-object', kind: 'agentUser', label: 'Agent user', createdAt: '', updatedAt: '' },
      { id: 'sg_1', tenantId: 'tenant', objectId: 'sg-object', kind: 'securityGroup', label: 'Security readers', createdAt: '', updatedAt: '' },
      { id: 'app_1', tenantId: 'tenant', objectId: 'app-object', kind: 'servicePrincipal', label: 'Application', createdAt: '', updatedAt: '' },
    ])
    api.createEntitlement.mockResolvedValue(directGrant)
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Add entitlement' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('group', { name: 'People' })).toBeVisible()
    expect(within(dialog).getByRole('group', { name: 'Agents' })).toBeVisible()
    expect(within(dialog).getByRole('group', { name: 'Security groups' })).toBeVisible()
    expect(within(dialog).getByRole('group', { name: 'Applications' })).toBeVisible()
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'agent_user_1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Resource' }), { target: { value: publishedModelApi.id } })
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))

    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith(expect.objectContaining({
      subject: { kind: 'user', id: 'agent_user_1' },
    })))

    api.createEntitlement.mockClear()
    await user.click(await screen.findByRole('button', { name: 'Add entitlement' }))
    const second = await screen.findByRole('dialog')
    fireEvent.change(within(second).getByRole('combobox', { name: 'Subject' }), { target: { value: 'sg_1' } })
    fireEvent.change(within(second).getByRole('combobox', { name: 'Resource' }), { target: { value: publishedModelApi.id } })
    await user.click(within(second).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith(expect.objectContaining({
      subject: { kind: 'securityGroup', id: 'sg_1' },
    })))
  })

  it('offers only call limits for an MCP server, so no token limit is dropped on save', async () => {
    const user = userEvent.setup()
    api.listMcpServers.mockResolvedValue([docsServer])
    api.createEntitlement.mockResolvedValue(directGrant)
    renderPage()
    const add = await screen.findByRole('button', { name: 'Add entitlement' })
    await waitFor(() => expect(add).toBeEnabled())
    await user.click(add)
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Resource' }), { target: { value: 'modelApi_1' } })
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }), '1000')

    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Resource' }), { target: { value: 'mcp_1' } })
    expect(within(dialog).queryByRole('spinbutton', { name: 'Tokens per minute' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('spinbutton', { name: 'Token quota' })).not.toBeInTheDocument()
    expect(within(dialog).getByText(/MCP servers are limited by calls, not tokens/)).toBeVisible()
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Calls' }), '30')
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))

    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith({
      subject: { kind: 'user', id: 'principal_1' },
      resource: { kind: 'mcpServer', id: 'mcp_1' },
      enforcement: {
        requests: { counterKeyExpression: '@(context.Subscription?.Key)', calls: 30, renewalPeriodSeconds: 60 },
      },
      notes: null,
    }))
    expect(await screen.findByText(
      'Saved grant intent. API Management is unchanged; use Plan and apply on the MCPs page to activate it on a MOSAIC-published MCP server.',
    )).toBeVisible()
  })

  describe('pool models', () => {
    const megan: Principal = {
      id: 'principal_megan', tenantId: 'tenant', objectId: '11111111-1111-1111-1111-111111111111', kind: 'user',
      label: 'Megan Bowen', createdAt: '', updatedAt: '',
    }
    const research: Principal = {
      id: 'principal_research', tenantId: 'tenant', objectId: '22222222-2222-2222-2222-222222222222',
      kind: 'securityGroup', label: 'Research engineers', createdAt: '', updatedAt: '',
    }
    const meganGrant: Entitlement = {
      id: 'entitlement_pool_megan',
      tenantId: 'tenant',
      subject: { kind: 'user', id: megan.id },
      resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: governedAnthropicPool.id },
      enabled: true,
      enforcement: {
        tokens: { counterKeyExpression: GOVERNED_COUNTER_KEY, estimatePromptTokens: true, tokensPerMinute: 20000 },
      },
      binding: null,
      runtime: {
        publicationId: governedAnthropicPool.id,
        status: 'applied',
        appliedMethods: { keysEnabled: true, entraEnabled: true },
        subscriptionName: 'mosaic-pool-anthropic-claude-megan',
        keyExists: true,
      },
      createdAt: '2026-09-02T09:00:00Z',
      updatedAt: '2026-09-02T09:00:00Z',
    }
    const researchGrant: Entitlement = {
      ...meganGrant,
      id: 'entitlement_pool_research',
      subject: { kind: 'securityGroup', id: research.id },
      enforcement: null,
      runtime: { ...meganGrant.runtime!, subscriptionName: null, keyExists: false },
    }

    async function openAddDialog(user: ReturnType<typeof userEvent.setup>, poolModelOption: string) {
      const add = await screen.findByRole('button', { name: 'Add entitlement' })
      await waitFor(() => expect(add).toBeEnabled())
      await user.click(add)
      const dialog = await screen.findByRole('dialog')
      // The button enables as soon as any resource loads, so wait for the pools too.
      await within(dialog).findByRole('option', { name: poolModelOption })
      return dialog
    }

    it('offers each pool model under its pool, sends the pool with the grant, and links to the pool to apply it', async () => {
      const user = userEvent.setup()
      // Two pools serve a model with the same ID. Only the pool tells them apart.
      api.listModelPools.mockResolvedValue([governedAnthropicPool, { ...draftPool, models: anthropicPool.models }])
      api.createEntitlement.mockResolvedValue(meganGrant)
      renderPage()

      const dialog = await openAddDialog(user, 'Claude Opus 4.5 in Anthropic Claude (pool model)')
      const resource = within(dialog).getByRole('combobox', { name: 'Resource' })
      expect(within(resource).getByRole('option', { name: 'Claude Opus 4.5 in OpenAI chat (pool model)' })).toBeInTheDocument()
      fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
      fireEvent.change(resource, { target: { value: `poolModel:${governedAnthropicPool.id}:poolmodel_opus` } })
      expect(within(dialog).getByText(/The pool's shared token limit still applies/)).toBeVisible()
      await user.type(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }), '20000')
      await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))

      await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith({
        subject: { kind: 'user', id: 'principal_1' },
        resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: governedAnthropicPool.id },
        enforcement: {
          tokens: { counterKeyExpression: GOVERNED_COUNTER_KEY, estimatePromptTokens: true, tokensPerMinute: 20000 },
        },
        notes: null,
      }))
      expect(api.createPublishPlan).not.toHaveBeenCalled()
      expect(
        await screen.findByText(
          'Saved grant intent. API Management is unchanged until the Anthropic Claude plan is reviewed and applied.',
        ),
      ).toBeVisible()
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      await user.click(await screen.findByRole('link', { name: 'Go to Anthropic Claude' }))
      expect(screen.getByRole('status', { name: 'Current location' })).toHaveTextContent(
        `/pools/${governedAnthropicPool.id}`,
      )
    })

    it('says a grant waits while its pool still shares one subscription', async () => {
      const user = userEvent.setup()
      api.listModelPools.mockResolvedValue([anthropicPool])
      api.createEntitlement.mockResolvedValue(meganGrant)
      renderPage()

      const dialog = await openAddDialog(user, 'Claude Opus 4.5 in Anthropic Claude (pool model)')
      fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
      fireEvent.change(within(dialog).getByRole('combobox', { name: 'Resource' }), {
        target: { value: `poolModel:${anthropicPool.id}:poolmodel_opus` },
      })
      expect(
        within(dialog).getByText(/Anthropic Claude doesn't use governed access yet, so this grant waits until it does\./),
      ).toBeVisible()
      await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))

      expect(
        await screen.findByText(
          "Saved grant intent. Anthropic Claude doesn't use governed access yet, so its callers still share one subscription. Turn on governed access for the pool, then review and apply its plan.",
        ),
      ).toBeVisible()
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(await screen.findByRole('link', { name: 'Go to Anthropic Claude' })).toHaveAttribute(
        'href',
        `/pools/${anthropicPool.id}`,
      )
    })

    it('shows a pool grant’s key, the pool’s shared limit, and what the pool last applied', async () => {
      const user = userEvent.setup()
      api.listModelPools.mockResolvedValue([governedAnthropicPool])
      api.listGateways.mockResolvedValue([{ id: governedAnthropicPool.gatewayId, environment: 'production', name: 'Contoso AI' }])
      api.listPrincipals.mockResolvedValue([megan, research])
      api.listEntitlements.mockResolvedValue([meganGrant, researchGrant])
      renderPage()

      const table = await screen.findByRole('table', { name: 'Entitlements' })
      await waitFor(() =>
        expect(columnCells(table, 'Resource')[0]).toHaveTextContent('Claude Opus 4.5 in Anthropic Claude (pool model)'),
      )
      const [meganEnvironment] = columnCells(table, 'Environment')
      expect(within(meganEnvironment).getByText('Production')).toBeVisible()

      const [meganLimits, researchLimits] = columnCells(table, 'Limits')
      expect(within(meganLimits).getAllByText('Limits usage to 20,000 tokens per minute.')).toHaveLength(2)
      expect(within(meganLimits).getByText('Pool: Every caller shares 200,000 tokens per minute.')).toBeVisible()
      expect(within(meganLimits).getByText(/Last applied limits/)).toBeVisible()
      expect(within(researchLimits).getAllByText('No grant-specific limit is configured.')).toHaveLength(2)

      const [meganKey, researchKey] = columnCells(table, 'Binding')
      expect(within(meganKey).getByText('mosaic-pool-anthropic-claude-megan')).toBeVisible()
      expect(within(meganKey).getByText('Pool key')).toBeVisible()
      expect(within(researchKey).getByText('Entra token')).toBeVisible()
      expect(within(researchKey).getByText('No key')).toBeVisible()
      expect(within(table).queryByText('Not bound')).not.toBeInTheDocument()
      // The pool's policy checks a pool grant on every call, so it never counts as unbound.
      const unbound = screen.getByText('Without a binding').closest('div') as HTMLElement
      expect(within(unbound).getByText('0')).toBeVisible()

      const [meganActions] = columnCells(table, 'Actions')
      await user.click(within(meganActions).getByRole('button', { name: 'Manage pool' }))
      expect(screen.getByRole('status', { name: 'Current location' })).toHaveTextContent(
        `/pools/${governedAnthropicPool.id}`,
      )
    })

    it('says a pool grant waits for the pool to govern access when it has no runtime yet', async () => {
      api.listModelPools.mockResolvedValue([anthropicPool])
      api.listPrincipals.mockResolvedValue([megan])
      api.listEntitlements.mockResolvedValue([{ ...meganGrant, runtime: null }])
      renderPage()

      const table = await screen.findByRole('table', { name: 'Entitlements' })
      const [key] = columnCells(table, 'Binding')
      expect(within(key).getByText('Waits for the pool to govern access')).toBeVisible()
    })
  })

  it('shows overlap winners, shadowed grants, membership-unchecked notice, and the empty state', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([
      directGrant,
      { ...directGrant, id: 'group_grant', subject: { kind: 'securityGroup', id: 'sg_1' }, binding: null },
    ])
    api.getGrantOverlaps.mockResolvedValueOnce({
      membershipChecked: false,
      skipped: ['Graph membership unavailable'],
      generatedAt: '2026-09-01T12:00:00Z',
      overlaps: [{
        kind: 'directAndGroup',
        resource: { kind: 'modelApi', id: 'modelApi_1' },
        resourceLabel: 'Chat completions',
        principalId: 'principal_1',
        principalLabel: 'Ada Lovelace',
        winner: {
          entitlementId: 'direct_grant',
          subject: { kind: 'user', id: 'principal_1' },
          subjectLabel: 'Ada Lovelace',
          enabled: true,
          enforcement: { tokens: { tokensPerMinute: 20000 } },
        },
        shadowed: [{
          entitlementId: 'group_grant',
          subject: { kind: 'securityGroup', id: 'sg_1' },
          subjectLabel: 'Security readers',
          enabled: true,
        }],
        reason: 'A direct grant wins over a security-group grant.',
      }],
    })
    renderPage()

    expect(await screen.findByText('Direct grant overrides group grant')).toBeVisible()
    expect(screen.getByText('Affected principal:')).toBeVisible()
    expect(screen.getAllByText('Ada Lovelace').length).toBeGreaterThan(0)
    expect(screen.getByText('Applies')).toBeVisible()
    expect(screen.getByText("Doesn't apply")).toBeVisible()
    // Each side of an overlap states its own limits, so the admin can see what the winner changes.
    expect(screen.getByText('Limits usage to 20,000 tokens per minute.')).toBeVisible()
    expect(screen.getByText('Security readers').nextElementSibling).toHaveTextContent(
      'No grant-specific limit is configured.',
    )
    expect(screen.getByText(/Microsoft Graph membership was not checked/)).toBeVisible()
    expect(screen.getByText(/Graph membership unavailable/)).toBeVisible()

    // Each grant links to its row in the grants table, where it can be disabled or revoked.
    const link = await screen.findByRole('link', { name: 'Go to the Security readers grant on Chat completions' })
    const row = document.getElementById('grant-group_grant')
    expect(row).not.toBeNull()
    const scrollIntoView = vi.fn()
    row!.scrollIntoView = scrollIntoView
    await user.click(link)
    expect(row).toHaveFocus()
    expect(scrollIntoView).toHaveBeenCalledWith({ block: 'center' })
    expect(screen.getByRole('link', { name: 'Go to the Ada Lovelace grant on Chat completions' })).toBeVisible()
  })

  it('shows resolved access paths and whether each grant applies', async () => {
    api.resolveEntitlements.mockResolvedValue([
      { entitlement: directGrant, via: 'direct', effective: true, shadowedBy: null },
      {
        entitlement: { ...directGrant, id: 'shadowed-grant', enabled: true },
        via: 'securityGroup',
        viaGroupId: 'sg_1',
        viaGroupName: 'Security readers',
        effective: false,
        shadowedBy: directGrant.id,
      },
      {
        entitlement: { ...directGrant, id: 'disabled-grant', enabled: false },
        via: 'group',
        viaGroupId: 'group_1',
        viaGroupName: 'Engineering',
        effective: false,
        shadowedBy: null,
      },
    ])
    renderPage()

    await screen.findByRole('option', { name: 'Ada Lovelace' })
    fireEvent.change(screen.getByRole('combobox', { name: 'Principal' }), { target: { value: 'principal_1' } })

    await waitFor(() => expect(screen.getAllByText((_, element) => element?.textContent?.includes('Direct · Applies') ?? false).length).toBeGreaterThan(0))
    expect(screen.getAllByText((_, element) => element?.textContent?.includes('Security group Security readers · Overridden by the direct grant') ?? false).length).toBeGreaterThan(0)
    expect(screen.getAllByText((_, element) => element?.textContent?.includes('MOSAIC group Engineering · Disabled') ?? false).length).toBeGreaterThan(0)
  })

  it('reviews the full model snapshot, not a row-scoped apply', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.listEntitlements.mockResolvedValue([directGrant])
    api.createPublishPlan.mockResolvedValue({
      ...accessPlan,
      accessSnapshot: {
        ...accessPlan.accessSnapshot,
        grants: [...accessPlan.accessSnapshot!.grants, {
          ...accessPlan.accessSnapshot!.grants[0], entitlementId: 'other-grant', displayName: 'Another pending application',
          subject: { kind: 'application', id: 'app_1' }, enabled: false,
        }],
      },
    })
    renderPage()
    await screen.findByRole('option', { name: /Published chat/ })
    fireEvent.change(screen.getByLabelText('Published model'), { target: { value: modelPublication.id } })
    await user.click(await screen.findByRole('button', { name: 'Review model changes' }))
    const table = await screen.findByRole('table', { name: 'All target model grants' })
    expect(within(table).getByText('Ada Lovelace')).toBeVisible()
    expect(within(table).getByText('Another pending application')).toBeVisible()
    expect(within(table).getByText('Disabled — revoke both methods')).toBeVisible()
    expect(api.createPublishPlan).toHaveBeenCalledWith(modelPublication.id)
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it.each([true, false])('uses the supported shared counter for governed direct limits (governed: %s)', async (governed) => {
    const user = userEvent.setup()
    if (governed) {
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
    }
    api.createEntitlement.mockResolvedValue(directGrant)
    renderPage()
    const add = await screen.findByRole('button', { name: 'Add entitlement' })
    await waitFor(() => expect(add).toBeEnabled())
    if (governed) await screen.findByRole('option', { name: /Published chat/ })
    await user.click(add)
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Resource' }), { target: { value: publishedModelApi.id } })
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }), '1000')
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Calls' }), '20')
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith({
      subject: directGrant.subject,
      resource: directGrant.resource,
      enforcement: {
        tokens: {
          counterKeyExpression: governed ? '@(context.Subscription.Id)' : '@(context.Subscription?.Key)',
          tokensPerMinute: 1000,
          estimatePromptTokens: true,
        },
        requests: {
          counterKeyExpression: governed ? '@(context.Subscription.Id)' : '@(context.Subscription?.Key)',
          calls: 20,
          renewalPeriodSeconds: 60,
        },
      },
      notes: null,
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('shows planning failures without inventing a successful apply', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.createPublishPlan.mockRejectedValue(new Error('This gateway does not support the selected policy.'))
    renderPage()
    await screen.findByRole('option', { name: /Published chat/ })
    fireEvent.change(screen.getByLabelText('Published model'), { target: { value: modelPublication.id } })
    await user.click(await screen.findByRole('button', { name: 'Review model changes' }))
    expect(await screen.findByText('This gateway does not support the selected policy.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Apply plan' })).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  describe('access requests', () => {
    it('names a registered requester above their object ID, found by recorded principal or by object ID', async () => {
      const user = userEvent.setup()
      api.listPrincipals.mockResolvedValue([
        {
          id: 'principal_1', tenantId: 'tenant', objectId: 'user-object-1', kind: 'user',
          label: 'Ada Lovelace', createdAt: '', updatedAt: '',
        },
        {
          id: 'principal_2', tenantId: 'tenant', objectId: 'user-object-2', kind: 'user',
          label: 'Grace Hopper', createdAt: '', updatedAt: '',
        },
      ])
      api.listAccessRequests.mockResolvedValue([
        // Matched by object ID alone, in a different letter case.
        pendingRequest,
        // Matched by the principal the request recorded, whatever its object ID.
        { ...pendingRequest, id: 'request_2', requesterObjectId: 'other-object-2', requesterPrincipalId: 'principal_2' },
      ])
      renderPage()

      const requests = await screen.findByRole('table', { name: 'Pending access requests' })
      await within(requests).findByText('Grace Hopper')
      const [byObjectId, byPrincipal] = columnCells(requests, 'Requester')
      expect(within(byObjectId).getByText('Ada Lovelace')).toBeVisible()
      expect(within(byObjectId).getByText('USER-OBJECT-1')).toBeVisible()
      expect(within(byPrincipal).getByText('Grace Hopper')).toBeVisible()
      expect(within(byPrincipal).getByText('other-object-2')).toBeVisible()

      // Approve names the same person the table does.
      const approve = within(requests).getAllByRole('button', { name: 'Approve' })[1]
      await waitFor(() => expect(approve).toBeEnabled())
      await user.click(approve)
      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText('Grace Hopper')).toBeVisible()
      expect(within(dialog).queryByText(/Not registered in MOSAIC/)).not.toBeInTheDocument()
    })

    it('shows only the object ID for a requester MOSAIC does not know', async () => {
      api.listAccessRequests.mockResolvedValue([
        pendingRequest,
        { ...pendingRequest, id: 'request_2', requesterObjectId: 'new-object-1' },
      ])
      renderPage()

      const requests = await screen.findByRole('table', { name: 'Pending access requests' })
      await within(requests).findByText('Ada Lovelace')
      const [, unregistered] = columnCells(requests, 'Requester')
      expect(unregistered).toHaveTextContent(/^new-object-1$/)
    })

    it('shows only the object ID while principals cannot be loaded', async () => {
      api.listPrincipals.mockRejectedValue(new Error('Principals are unavailable.'))
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      renderPage()

      expect(await screen.findByText('Principals are unavailable.')).toBeVisible()
      const requests = await screen.findByRole('table', { name: 'Pending access requests' })
      const [requester] = columnCells(requests, 'Requester')
      expect(requester).toHaveTextContent(/^USER-OBJECT-1$/)
    })

    // Registered as user-object-1, while the request recorded USER-OBJECT-1.
    const withoutLabel: Principal = {
      id: 'principal_1', tenantId: 'tenant', objectId: 'user-object-1', kind: 'user', createdAt: '', updatedAt: '',
    }

    it.each<[shows: string, principals: Principal[], inTable: string[], inDialog: string[]]>([
      [
        'a registered requester by label, then the object ID the request recorded',
        [{ ...withoutLabel, label: 'Megan Bowen' }],
        ['Megan Bowen', 'USER-OBJECT-1'],
        ['Megan Bowen', 'USER-OBJECT-1'],
      ],
      [
        'a registered requester without a label by the recorded object ID once',
        [withoutLabel],
        ['USER-OBJECT-1'],
        ['USER-OBJECT-1'],
      ],
      [
        'a registered requester with a blank label by the recorded object ID once',
        [{ ...withoutLabel, label: '' }],
        ['USER-OBJECT-1'],
        ['USER-OBJECT-1'],
      ],
      [
        'a requester MOSAIC does not know by the object ID once, and that approving registers them',
        [],
        ['USER-OBJECT-1'],
        ['USER-OBJECT-1', 'Not registered in MOSAIC yet. Approving registers them as a user principal.'],
      ],
    ])('shows %s, in the table and when approving', async (_, principals, inTable, inDialog) => {
      const user = userEvent.setup()
      api.listPrincipals.mockResolvedValue(principals)
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      renderPage()

      const requests = await screen.findByRole('table', { name: 'Pending access requests' })
      // Approve waits for principals, so by then the table shows all it knows about the requester.
      await waitFor(() => expect(within(requests).getByRole('button', { name: 'Approve' })).toBeEnabled())
      expect(textLines(columnCells(requests, 'Requester')[0])).toEqual(inTable)

      const dialog = await openApproval(user)
      expect(textLines(within(dialog).getByText('Requester').nextElementSibling as HTMLElement)).toEqual(inDialog)
    })

    it('approves through the limits dialog, creating linked grant intent that still needs review and apply', async () => {
      const user = userEvent.setup()
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      api.approveAccessRequest.mockImplementation(async () => {
        api.listAccessRequests.mockResolvedValue([])
        return {
          ...pendingRequest, state: 'approved', requesterPrincipalId: 'principal_1',
          grantedEntitlementId: 'granted_1',
        }
      })
      renderPage()

      const dialog = await openApproval(user)
      expect(api.approveAccessRequest).not.toHaveBeenCalled()
      expect(within(dialog).getByRole('heading', { name: 'Approve access request' })).toBeVisible()
      expect(within(dialog).getByText('Ada Lovelace')).toBeVisible()
      expect(within(dialog).getByText('Chat completions (model API)')).toBeVisible()
      expect(within(dialog).getByText('Support bot evaluation')).toBeVisible()
      expect(within(dialog).queryByText(/Not registered in MOSAIC/)).not.toBeInTheDocument()
      expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(12000)
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      await waitFor(() => expect(api.approveAccessRequest).toHaveBeenCalledWith(pendingRequest.id, {
        note: null,
        enforcement: {
          tokens: {
            counterKeyExpression: '@(context.Subscription.Id)',
            estimatePromptTokens: true,
            tokensPerMinute: 12000,
          },
        },
      }))
      expect(await screen.findByText(
        'Approved the request and created grant intent for Ada Lovelace. API Management is unchanged; review and apply the Published chat model plan to activate it.',
      )).toBeVisible()
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(await screen.findByText('No pending requests')).toBeVisible()
      expect(api.createEntitlement).not.toHaveBeenCalled()

      // The rest of the page stays hidden from assistive technology for a moment after the modal
      // dialog is gone, so the first role query outside it has to wait.
      await user.click(await screen.findByRole('link', { name: 'Go to review and apply for Published chat' }))
      const publishedModel = screen.getByRole('combobox', { name: 'Published model' })
      expect(publishedModel).toHaveValue(modelPublication.id)
      expect(publishedModel).toHaveFocus()
      expect(await screen.findByRole('button', { name: 'Review model changes' })).toBeVisible()
      expect(api.createPublishPlan).not.toHaveBeenCalled()
      expect(api.applyPublishPlan).not.toHaveBeenCalled()
    })

    it('approves an MCP server request with call limits only, and points to the MCP plan', async () => {
      const user = userEvent.setup()
      api.listMcpServers.mockResolvedValue([docsServer])
      api.listMcpPublications.mockResolvedValue([docsPublication])
      api.listAccessRequests.mockResolvedValue([
        { ...pendingRequest, resource: { kind: 'mcpServer', id: 'mcp_1' } },
      ])
      api.approveAccessRequest.mockImplementation(async () => {
        api.listAccessRequests.mockResolvedValue([])
        return {
          ...pendingRequest, resource: { kind: 'mcpServer', id: 'mcp_1' }, state: 'approved',
          requesterPrincipalId: 'principal_1', grantedEntitlementId: 'granted_1',
        }
      })
      renderPage()

      const dialog = await openApproval(user)
      expect(within(dialog).getByText('Docs search (MCP server)')).toBeVisible()
      expect(within(dialog).getByText(
        'Approving creates grant intent only. API Management is unchanged until this MCP server is planned and applied from the MCPs page.',
      )).toBeVisible()
      expect(within(dialog).queryByRole('spinbutton', { name: 'Tokens per minute' })).not.toBeInTheDocument()
      expect(within(dialog).getByText(/MCP servers are limited by calls, not tokens/)).toBeVisible()
      await user.type(within(dialog).getByRole('spinbutton', { name: 'Calls' }), '10')
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      await waitFor(() => expect(api.approveAccessRequest).toHaveBeenCalledWith(pendingRequest.id, {
        note: null,
        enforcement: {
          requests: { counterKeyExpression: '@(context.Subscription.Id)', calls: 10, renewalPeriodSeconds: 60 },
        },
      }))
      expect(await screen.findByText(
        'Approved the request and created grant intent for Ada Lovelace. API Management is unchanged; use Plan and apply on the MCPs page to activate it.',
      )).toBeVisible()
    })

    it('says approval registers a requester MOSAIC does not know yet', async () => {
      const user = userEvent.setup()
      api.listAccessRequests.mockResolvedValue([
        { ...pendingRequest, requesterObjectId: 'new-object-1', justification: null },
      ])
      api.approveAccessRequest.mockResolvedValue({
        ...pendingRequest, requesterObjectId: 'new-object-1', state: 'approved',
        requesterPrincipalId: 'principal_new', grantedEntitlementId: 'granted_1',
      })
      renderPage()

      const dialog = await openApproval(user)
      expect(within(dialog).getByText(
        'Not registered in MOSAIC yet. Approving registers them as a user principal.',
      )).toBeVisible()
      expect(within(dialog).getByText('No justification given')).toBeVisible()
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      // The model API is imported-only here, so nothing prefills and the grant stays desired state.
      await waitFor(() => expect(api.approveAccessRequest).toHaveBeenCalledWith(pendingRequest.id, {
        note: null,
        enforcement: null,
      }))
      expect(await screen.findByText(
        'Approved the request, registered new-object-1 as a user principal, and created their grant intent. The grant is desired state only; MOSAIC does not apply grants for this resource to API Management.',
      )).toBeVisible()
      // Include hidden elements: the page stays aria-hidden for a moment after the modal dialog
      // closes, so a plain role query would pass even if the link were there.
      expect(screen.queryByRole('link', { name: /Go to review and apply/, hidden: true })).not.toBeInTheDocument()
    })

    it('keeps a failed approval open in the dialog with the reason', async () => {
      const user = userEvent.setup()
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      api.approveAccessRequest.mockRejectedValue(
        new Error('An apply or mutation is already running for this publication.'),
      )
      renderPage()

      const dialog = await openApproval(user)
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      expect(await within(dialog).findByText(
        'An apply or mutation is already running for this publication.',
      )).toBeVisible()
      expect(screen.getByRole('dialog')).toBeVisible()
      expect(screen.queryByText(/Approved the request/)).not.toBeInTheDocument()
    })

    it('warns instead of approving when the requester already holds a direct grant', async () => {
      const user = userEvent.setup()
      api.listEntitlements.mockResolvedValue([directGrant])
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      renderPage()

      const dialog = await openApproval(user)
      expect(within(dialog).getByText(
        'Ada Lovelace already has a direct grant for Chat completions (model API). Deny this request, or change the existing grant instead.',
      )).toBeVisible()
      expect(within(dialog).getByRole('button', { name: 'Approve and create grant' })).toBeDisabled()
      expect(api.approveAccessRequest).not.toHaveBeenCalled()
    })

    it('names a requester without a label by the recorded object ID when they already hold a direct grant', async () => {
      const user = userEvent.setup()
      api.listPrincipals.mockResolvedValue([withoutLabel])
      api.listEntitlements.mockResolvedValue([directGrant])
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      renderPage()

      const dialog = await openApproval(user)
      expect(within(dialog).getByText(
        'USER-OBJECT-1 already has a direct grant for Chat completions (model API). Deny this request, or change the existing grant instead.',
      )).toBeVisible()
    })

    it('denies without a dialog, and says no grant was created', async () => {
      const user = userEvent.setup()
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      api.denyAccessRequest.mockImplementation(async () => {
        api.listAccessRequests.mockResolvedValue([])
        return { ...pendingRequest, state: 'denied' }
      })
      renderPage()

      const requests = await screen.findByRole('table', { name: 'Pending access requests' })
      await user.click(within(requests).getByRole('button', { name: 'Deny' }))

      await waitFor(() => expect(api.denyAccessRequest).toHaveBeenCalledWith(pendingRequest.id))
      expect(await screen.findByText(
        'Denied the access request. No grant was created, and API Management is unchanged.',
      )).toBeVisible()
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(api.approveAccessRequest).not.toHaveBeenCalled()
    })
  })
})

describe('describeLimits', () => {
  it('does not confuse an absent grant limit with unrestricted gateway access', () => {
    expect(describeLimits({ ...entitlement, enforcement: null })).toEqual([
      'No grant-specific limit is configured.',
    ])
    expect(describeLimits({ ...entitlement, enforcement: {} })).toEqual([
      'No grant-specific limit is configured.',
    ])
    expect(describeLimits({ enforcement: null }, modelPublication.enforcement)).toEqual([
      'No grant-specific limit is configured.',
      'Publication: Limits usage to 12,000 tokens per minute.',
    ])
  })

  it('says a publication without token enforcement has no token limits, and stays silent when the publication is unknown', () => {
    expect(describeLimits({ enforcement: null }, null)).toEqual([
      'No grant-specific limit is configured.',
      "Publication: Token limits are unavailable for this model on this gateway's tier.",
    ])
    expect(describeLimits({ enforcement: null }, undefined)).toEqual([
      'No grant-specific limit is configured.',
    ])
    expect(describePublicationLimits(null)).toEqual([
      "Token limits are unavailable for this model on this gateway's tier.",
    ])
  })

  it('describes request limits separately from token limits', () => {
    expect(
      describeLimits({
        ...entitlement,
        enforcement: {
          requests: {
            counterKeyExpression: '@(context.Subscription?.Key)',
            calls: 60,
            renewalPeriodSeconds: 60,
            callQuota: 100000,
            callQuotaPeriod: 'Monthly',
          },
        },
      }),
    ).toEqual(['Limits traffic to 60 calls per minute.', 'Allows 100,000 calls per month.'])
  })
})

describe('callRateError', () => {
  it('refuses a half-filled call rate rather than dropping it silently', () => {
    // Both halves are needed to build the limit; dropping one would create an unrestricted
    // grant while the UI reported success.
    expect(callRateError({ calls: '100', renewalPeriodSeconds: '' })).toBeTruthy()
    expect(callRateError({ calls: '100', renewalPeriodSeconds: '0' })).toBeTruthy()
    expect(callRateError({ calls: '0', renewalPeriodSeconds: '60' })).toBeTruthy()
  })

  it('accepts an empty pair and a complete pair', () => {
    expect(callRateError({ calls: '', renewalPeriodSeconds: '60' })).toBeNull()
    expect(callRateError({ calls: '', renewalPeriodSeconds: '' })).toBeNull()
    expect(callRateError({ calls: '100', renewalPeriodSeconds: '60' })).toBeNull()
  })
})
