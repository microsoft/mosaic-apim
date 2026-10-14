import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccessRequest, Entitlement, EnvironmentCatalogView } from '../types'
import { GOVERNED_COUNTER_KEY, callRateError, describeLimits } from '../entitlement-limits'
import { EntitlementsPage } from './EntitlementsPage'
import { accessPlan, connectionInfo, directGrant, modelPublication, publishedModelApi } from '../test/model-access'
import { ApiError } from '../api'
import { anthropicPool, governedAnthropicPool } from '../test/pool-fixtures'

const entitlement: Entitlement = {
  id: 'entitlement_1', tenantId: 'tenant',
  subject: { kind: 'group', id: 'group_1' },
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  enabled: true, binding: null, notes: 'Engineering evaluation',
  enforcement: { tokens: { counterKeyExpression: '@(context.Subscription?.Key)', tokensPerMinute: 10000, tokenQuota: 5000000, tokenQuotaPeriod: 'Monthly', estimatePromptTokens: true } },
  createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z',
}
const request: AccessRequest = {
  id: 'request_1', tenantId: 'tenant', requesterObjectId: 'USER-OBJECT-1',
  requesterPrincipalId: null, resource: { kind: 'modelApi', id: 'modelApi_1' },
  justification: 'Support bot evaluation', state: 'pending',
  createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
}
const catalog: EnvironmentCatalogView = {
  environments: [
    {
      key: 'development', displayName: 'Development', color: 'brand', production: false,
      aliases: [], acceptsEndpointsFrom: [], order: 10, builtIn: true,
      usage: { gateways: 1, modelEndpoints: 1, mcpEndpoints: 0 },
    },
    {
      key: 'production', displayName: 'Production', color: 'danger', production: true,
      aliases: [], acceptsEndpointsFrom: [], order: 50, builtIn: true,
      usage: { gateways: 1, modelEndpoints: 1, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false, unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 }, compatibility: [], updatedAt: null,
}
const api = {
  getEnvironmentCatalog: vi.fn(), listEntitlements: vi.fn(), listPrincipals: vi.fn(),
  listGroups: vi.fn(), listModelApis: vi.fn(), listModelPools: vi.fn(), listMcpServers: vi.fn(),
  listCostCenters: vi.fn(), getCostCenterSettings: vi.fn(), listMcpPublications: vi.fn(),
  listGateways: vi.fn(), listModelEndpoints: vi.fn(), listAccessRequests: vi.fn(),
  listPublications: vi.fn(), resolveEntitlements: vi.fn(), createEntitlement: vi.fn(),
  updateEntitlement: vi.fn(), deleteEntitlement: vi.fn(), getPublication: vi.fn(),
  createPublishPlan: vi.fn(), applyPublishPlan: vi.fn(), updatePublication: vi.fn(),
  linkPublicationModelApi: vi.fn(), approveAccessRequest: vi.fn(), denyAccessRequest: vi.fn(),
  getGrantOverlaps: vi.fn(), getMcpConnection: vi.fn(), getEntitlementConnection: vi.fn(),
  createEntitlementKey: vi.fn(), rotateEntitlementKey: vi.fn(), deleteEntitlementKey: vi.fn(),
  revealEntitlementKey: vi.fn(),
}
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

function renderPage(initial = '/entitlements') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const router = createMemoryRouter([{
    path: '*', element: <QueryClientProvider client={queryClient}>
      <FluentProvider theme={webLightTheme}><EntitlementsPage /></FluentProvider>
    </QueryClientProvider>,
  }], { initialEntries: [initial] })
  render(<RouterProvider router={router} />)
  return { queryClient, router, params: () => new URLSearchParams(router.state.location.search) }
}

async function selectGrant(user: ReturnType<typeof userEvent.setup>, subject = 'Engineering') {
  const list = await screen.findByRole('list', { name: 'Grants' })
  await user.click(await within(list).findByRole('button', { name: new RegExp(subject) }))
  return screen.findByRole('region', { name: 'Selected grant' })
}
async function openApproval(user: ReturnType<typeof userEvent.setup>) {
  const detail = await screen.findByRole('region', { name: 'Selected access request' })
  const approve = await within(detail).findByRole('button', { name: 'Approve' })
  await waitFor(() => expect(approve).toBeEnabled())
  await user.click(approve)
  return screen.findByRole('region', { name: 'Approve access request' })
}
async function menu(user: ReturnType<typeof userEvent.setup>, scope: HTMLElement) {
  await user.click(within(scope).getByRole('button', { name: /^More actions/ }))
}

describe('Entitlements workbench', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.listEntitlements.mockResolvedValue([entitlement])
    api.listPrincipals.mockResolvedValue([{ id: 'principal_1', objectId: 'user-object-1', kind: 'user', label: 'Ada Lovelace' }])
    api.listGroups.mockResolvedValue([{ id: 'group_1', name: 'Engineering' }])
    api.getEnvironmentCatalog.mockResolvedValue(catalog)
    api.listGateways.mockResolvedValue([{ id: 'gateway_1', environment: 'production' }])
    api.listModelEndpoints.mockResolvedValue([])
    api.listModelApis.mockResolvedValue([{ ...publishedModelApi, publicationId: null }])
    api.listModelPools.mockResolvedValue([])
    api.listMcpServers.mockResolvedValue([])
    api.listCostCenters.mockResolvedValue([{ id: 'cc_general', name: 'General', code: 'general', limits: [] }])
    api.getCostCenterSettings.mockResolvedValue({ defaultCostCenterId: 'cc_general' })
    api.listMcpPublications.mockResolvedValue([])
    api.listAccessRequests.mockResolvedValue([])
    api.listPublications.mockResolvedValue([])
    api.resolveEntitlements.mockResolvedValue([])
    api.getGrantOverlaps.mockResolvedValue({ overlaps: [], membershipChecked: true, skipped: [] })
    api.createPublishPlan.mockResolvedValue(accessPlan)
    api.getPublication.mockResolvedValue(modelPublication)
    api.createEntitlement.mockResolvedValue({ ...entitlement, id: 'saved_grant' })
    api.updateEntitlement.mockResolvedValue(entitlement)
    api.approveAccessRequest.mockResolvedValue({ ...request, state: 'approved' })
  })

  it('keeps the list slim and moves limits, bindings, notes and actions into the selected detail', async () => {
    const user = userEvent.setup()
    renderPage()
    const list = await screen.findByRole('list', { name: 'Grants' })
    expect(within(list).queryByText('Not bound')).not.toBeInTheDocument()
    expect(screen.queryByRole('table', { name: 'Entitlements' })).not.toBeInTheDocument()
    expect(screen.queryByText('Live data')).not.toBeInTheDocument()
    const detail = await selectGrant(user)
    expect(within(detail).getByText('Not bound')).toBeVisible()
    expect(within(detail).getByText('Engineering evaluation')).toBeVisible()
    expect(within(detail).getByText('Limits usage to 10,000 tokens per minute.')).toBeVisible()
    expect(within(detail).getByRole('button', { name: 'Edit grant' })).toBeVisible()
    expect(within(list).getByRole('button')).toHaveAttribute('aria-pressed', 'true')
  })

  it('selects grants through URLs and restores selection with browser Back and Forward', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([entitlement, { ...directGrant, id: 'second' }])
    const { router, params } = renderPage('/entitlements?selected=entitlement_1&costCenter=cc_general&custom=keep')
    expect(await screen.findByText('Engineering evaluation')).toBeVisible()
    await selectGrant(user, 'Ada')
    expect(params().get('selected')).toBe('second')
    expect(params().get('custom')).toBe('keep')
    await act(() => router.navigate(-1))
    await waitFor(() => expect(params().get('selected')).toBe('entitlement_1'))
    await act(() => router.navigate(1))
    await waitFor(() => expect(params().get('selected')).toBe('second'))
    expect(params().get('costCenter')).toBe('cc_general')
  })

  it('clears cross-section selection atomically while preserving filters', async () => {
    const user = userEvent.setup()
    const { params } = renderPage('/entitlements?selected=entitlement_1&costCenter=cc_general&custom=keep')
    await user.click(screen.getByRole('button', { name: 'Access requests' }))
    expect(params().get('tab')).toBe('requests')
    expect(params().has('selected')).toBe(false)
    expect(params().get('costCenter')).toBe('cc_general')
    expect(params().get('custom')).toBe('keep')
    expect(api.getGrantOverlaps).not.toHaveBeenCalled()
  })

  it('shows an honest missing grant state without selecting a different grant', async () => {
    renderPage('/entitlements?selected=deleted')
    expect(await screen.findByText('Grant unavailable')).toBeVisible()
    expect(screen.queryByText('Engineering evaluation')).not.toBeInTheDocument()
  })

  it('preserves URL environment and cost-center filters', async () => {
    const user = userEvent.setup()
    const { params } = renderPage('/entitlements?environment=production&costCenter=cc_general&custom=keep')
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Filter grants by environment' })).toHaveValue('production'))
    expect(api.listEntitlements).toHaveBeenCalledWith({ costCenter: 'cc_general' })
    await user.selectOptions(screen.getByRole('combobox', { name: 'Filter grants by environment' }), 'development')
    expect(params().get('environment')).toBe('development')
    expect(params().get('custom')).toBe('keep')
    expect(await screen.findByText('No grants match these filters')).toBeVisible()
  })

  it('keeps applied metadata distinct from pending desired intent', async () => {
    api.listEntitlements.mockResolvedValue([{ ...directGrant, runtime: { ...directGrant.runtime!, status: 'pending' } }])
    api.listPublications.mockResolvedValue([modelPublication])
    renderPage(`/entitlements?selected=${directGrant.id}`)
    expect(await screen.findByText('Desired: enabled')).toBeVisible()
    expect(screen.getByText('Saved, not applied')).toBeVisible()
    expect(screen.getByText('An earlier configuration was applied; saved changes are pending.')).toBeVisible()
    expect(screen.getByText('Last applied methods: Subscription key OR Entra token')).toBeVisible()
  })

  it.each(['applying', 'unknown'] as const)('locks grant editing, recovery and revocation while runtime is %s', async (status) => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([{ ...directGrant, runtime: { ...directGrant.runtime, publicationId: modelPublication.id, status } }])
    api.listPublications.mockResolvedValue([modelPublication])
    renderPage(`/entitlements?selected=${directGrant.id}`)
    const detail = await screen.findByRole('region', { name: 'Selected grant' })
    await waitFor(() => expect(within(detail).getByRole('button', { name: 'Edit grant' })).toBeDisabled())
    await menu(user, detail)
    expect(await screen.findByRole('menuitem', { name: 'Revoke' })).toHaveAttribute('aria-disabled', 'true')
    expect(api.updateEntitlement).not.toHaveBeenCalled()
  })

  it('revokes managed grants as disabled intent without deleting applied access', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([directGrant])
    api.listPublications.mockResolvedValue([modelPublication])
    renderPage(`/entitlements?selected=${directGrant.id}`)
    const detail = await screen.findByRole('region', { name: 'Selected grant' })
    await within(detail).findByText('Desired: enabled')
    await menu(user, detail)
    await user.click(await screen.findByRole('menuitem', { name: 'Revoke' }))
    await waitFor(() => expect(api.updateEntitlement).toHaveBeenCalledWith(directGrant.id, { enabled: false }))
    expect(api.deleteEntitlement).not.toHaveBeenCalled()
    expect(await screen.findByText(/Managed revocation is pending/)).toBeVisible()
  })

  it('removes desired-only group grants without claiming runtime revocation', async () => {
    const user = userEvent.setup()
    renderPage('/entitlements?selected=entitlement_1')
    const detail = await screen.findByRole('region', { name: 'Selected grant' })
    await within(detail).findByText('Not bound')
    await menu(user, detail)
    await user.click(await screen.findByRole('menuitem', { name: 'Revoke' }))
    await waitFor(() => expect(api.deleteEntitlement).toHaveBeenCalledWith(entitlement.id))
    expect(await screen.findByText('Removed the desired-state grant. API Management is unchanged.')).toBeVisible()
  })

  it('opens Manage model in the settings section with its model selected', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([directGrant])
    api.listPublications.mockResolvedValue([modelPublication])
    const { params } = renderPage(`/entitlements?selected=${directGrant.id}`)
    const detail = await screen.findByRole('region', { name: 'Selected grant' })
    await within(detail).findByText('Desired: enabled')
    await menu(user, detail)
    await user.click(await screen.findByRole('menuitem', { name: 'Manage model' }))
    expect(params().get('tab')).toBe('settings')
    expect(params().get('selected')).toBe(modelPublication.id)
    expect(await screen.findByRole('combobox', { name: 'Published model' })).toHaveValue(modelPublication.id)
  })

  it('creates in the full workspace and returns with the saved grant selected, preserving filters', async () => {
    const user = userEvent.setup()
    api.createEntitlement.mockImplementation(async () => {
      const saved = { ...entitlement, id: 'saved_grant', subject: { kind: 'user', id: 'principal_1' } }
      api.listEntitlements.mockResolvedValue([saved])
      return saved
    })
    const { params } = renderPage('/entitlements?costCenter=cc_general&custom=keep')
    const newGrant = screen.getByRole('button', { name: 'New grant' })
    await waitFor(() => expect(newGrant).toBeEnabled())
    await user.click(newGrant)
    const editor = await screen.findByRole('region', { name: 'New grant' })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await within(editor).findByRole('option', { name: 'Ada Lovelace (Person)' })
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Resource' }), { target: { value: 'modelApi_1' } })
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Cost center' }), { target: { value: 'cc_general' } })
    await user.click(within(editor).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(params().get('selected')).toBe('saved_grant'))
    expect(params().has('mode')).toBe(false)
    expect(params().get('costCenter')).toBe('cc_general')
    expect(params().get('custom')).toBe('keep')
    expect(api.createEntitlement).toHaveBeenCalledWith(expect.objectContaining({ costCenterId: 'cc_general' }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('supports direct New grant links and protects drafts across section navigation', async () => {
    const user = userEvent.setup()
    const { params } = renderPage('/entitlements?mode=new&custom=keep')
    const editor = await screen.findByRole('region', { name: 'New grant' })
    await user.type(within(editor).getByRole('textbox', { name: 'Notes' }), 'Draft only')
    await user.click(screen.getByRole('button', { name: 'Access requests' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    expect(params().get('mode')).toBe('new')
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(within(editor).getByRole('textbox', { name: 'Notes' })).toHaveValue('Draft only')
    await user.click(within(editor).getByRole('button', { name: 'Cancel' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(params().has('mode')).toBe(false))
    expect(api.createEntitlement).not.toHaveBeenCalled()
    expect(params().get('custom')).toBe('keep')
  })

  it('edits limits and notes without changing immutable subject, resource or cost center', async () => {
    const user = userEvent.setup()
    const { params } = renderPage('/entitlements?selected=entitlement_1&mode=edit')
    const editor = await screen.findByRole('region', { name: 'Edit grant' })
    expect(within(editor).getByRole('combobox', { name: 'Subject' })).toBeDisabled()
    expect(within(editor).getByRole('combobox', { name: 'Resource' })).toBeDisabled()
    expect(within(editor).getByRole('combobox', { name: 'Cost center' })).toBeDisabled()
    await user.clear(within(editor).getByRole('textbox', { name: 'Notes' }))
    await user.type(within(editor).getByRole('textbox', { name: 'Notes' }), 'Updated notes')
    await user.click(within(editor).getByRole('button', { name: 'Save grant' }))
    await waitFor(() => expect(api.updateEntitlement).toHaveBeenCalledWith(entitlement.id, expect.objectContaining({ notes: 'Updated notes' })))
    await waitFor(() => expect(params().has('mode')).toBe(false))
    expect(params().get('selected')).toBe(entitlement.id)
  })

  it('keeps failed grant saves in the workspace without promising applied access', async () => {
    const user = userEvent.setup()
    api.updateEntitlement.mockRejectedValue(new Error('Grant save failed'))
    renderPage('/entitlements?selected=entitlement_1&mode=edit')
    const editor = await screen.findByRole('region', { name: 'Edit grant' })
    await user.click(within(editor).getByRole('button', { name: 'Save grant' }))
    expect(await within(editor).findByText('Grant save failed')).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('retains existing call rates and call quotas on a notes-only edit', async () => {
    const user = userEvent.setup()
    const enforcement = { requests: { calls: 30, renewalPeriodSeconds: 60, callQuota: 500, callQuotaPeriod: 'Monthly' as const, counterKeyExpression: GOVERNED_COUNTER_KEY } }
    api.listEntitlements.mockResolvedValue([{ ...entitlement, enforcement }])
    renderPage('/entitlements?selected=entitlement_1&mode=edit')
    const editor = await screen.findByRole('region', { name: 'Edit grant' })
    expect(within(editor).getByRole('spinbutton', { name: 'Calls' })).toHaveValue(30)
    await user.type(within(editor).getByRole('textbox', { name: 'Notes' }), ' added')
    await user.click(within(editor).getByRole('button', { name: 'Save grant' }))
    await waitFor(() => expect(api.updateEntitlement).toHaveBeenCalledWith(entitlement.id, expect.objectContaining({ enforcement })))
  })

  it('keeps imported-only direct grants desired-only', async () => {
    api.listEntitlements.mockResolvedValue([{ ...directGrant, runtime: null, binding: null }])
    renderPage(`/entitlements?selected=${directGrant.id}`)
    expect(await screen.findByText('No runtime orchestration. APIM is unchanged by this grant.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Manage model' })).not.toBeInTheDocument()
  })

  it('refuses half-filled call limits in the grant workspace', async () => {
    const user = userEvent.setup()
    renderPage('/entitlements?selected=entitlement_1&mode=edit')
    const editor = await screen.findByRole('region', { name: 'Edit grant' })
    await user.type(within(editor).getByRole('spinbutton', { name: 'Calls' }), '30')
    await user.clear(within(editor).getByRole('spinbutton', { name: 'Per how many seconds' }))
    expect(within(editor).getByRole('button', { name: 'Save grant' })).toBeDisabled()
    expect(api.updateEntitlement).not.toHaveBeenCalled()
  })

  it('clears hidden token limits when choosing MCP and sends only call limits', async () => {
    const user = userEvent.setup()
    api.listMcpServers.mockResolvedValue([{ id: 'mcp_1', displayName: 'Docs search' }])
    renderPage('/entitlements?mode=new')
    const editor = await screen.findByRole('region', { name: 'New grant' })
    await within(editor).findByRole('option', { name: 'Docs search (MCP server)' })
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Resource' }), { target: { value: 'modelApi_1' } })
    await user.type(within(editor).getByRole('spinbutton', { name: 'Tokens per minute' }), '1000')
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Resource' }), { target: { value: 'mcp_1' } })
    expect(within(editor).queryByRole('spinbutton', { name: 'Tokens per minute' })).not.toBeInTheDocument()
    await user.type(within(editor).getByRole('spinbutton', { name: 'Calls' }), '30')
    await user.click(within(editor).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith(expect.objectContaining({
      resource: { kind: 'mcpServer', id: 'mcp_1' },
      enforcement: { requests: { calls: 30, renewalPeriodSeconds: 60, counterKeyExpression: '@(context.Subscription?.Key)' } },
    })))
  })

  it('preserves pool-model scope and the shared safeguard', async () => {
    const user = userEvent.setup()
    api.listModelPools.mockResolvedValue([anthropicPool])
    renderPage('/entitlements?mode=new')
    const editor = await screen.findByRole('region', { name: 'New grant' })
    const option = await within(editor).findByRole('option', { name: /Claude Opus 4.5 in Anthropic Claude/ })
    fireEvent.change(within(editor).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    await user.selectOptions(within(editor).getByRole('combobox', { name: 'Resource' }), option)
    expect(within(editor).getByText(/shared token limit still applies/)).toBeVisible()
    await user.click(within(editor).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith(expect.objectContaining({
      resource: { kind: 'poolModel', id: anthropicPool.models[0].id, scopeId: anthropicPool.id },
    })))
  })

  it('selects the EXACT notification request, not the first request in the queue', async () => {
    api.listAccessRequests.mockResolvedValue([{ ...request, id: 'first', justification: 'Wrong request' }, { ...request, id: 'notification-target', justification: 'Exact requested review' }])
    const { params } = renderPage('/entitlements?tab=requests&selected=notification-target&custom=keep')
    const detail = await screen.findByRole('region', { name: 'Selected access request' })
    expect(await within(detail).findByText('Exact requested review')).toBeVisible()
    expect(within(detail).getByText('notification-target')).toBeVisible()
    expect(within(detail).queryByText('Wrong request')).not.toBeInTheDocument()
    expect(params().get('selected')).toBe('notification-target')
    expect(params().get('custom')).toBe('keep')
  })

  it('resolves an exact decided request outside the pending filter and keeps decisions final', async () => {
    api.listAccessRequests.mockImplementation(async (state?: string) => state === 'pending' ? [] : [{ ...request, state: 'approved', decisionNote: 'Already decided' }])
    renderPage('/entitlements?tab=requests&selected=request_1')
    const detail = await screen.findByRole('region', { name: 'Selected access request' })
    expect(await within(detail).findByText('Already decided')).toBeVisible()
    expect(within(detail).getByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(api.listAccessRequests).toHaveBeenCalledWith()
  })

  it('reports an inaccessible exact request rather than silently selecting another request', async () => {
    api.listAccessRequests.mockResolvedValue([request])
    renderPage('/entitlements?tab=requests&selected=missing')
    expect(await screen.findByText('Request unavailable')).toBeVisible()
    expect(screen.queryByText('Support bot evaluation')).not.toBeInTheDocument()
  })

  it('opens substantive approval in the workspace and saves only grant intent', async () => {
    const user = userEvent.setup()
    api.listAccessRequests.mockResolvedValue([request])
    api.listPublications.mockResolvedValue([modelPublication])
    const { params, queryClient } = renderPage('/entitlements?tab=requests&selected=request_1&custom=keep')
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const editor = await openApproval(user)
    expect(params().get('mode')).toBe('review')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(within(editor).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(12000)
    await user.click(within(editor).getByRole('button', { name: 'Approve and create grant' }))
    await waitFor(() => expect(api.approveAccessRequest).toHaveBeenCalledWith(request.id, expect.objectContaining({
      enforcement: { tokens: { counterKeyExpression: GOVERNED_COUNTER_KEY, tokensPerMinute: 12000, estimatePromptTokens: true } },
    })))
    await waitFor(() => expect(params().has('mode')).toBe(false))
    expect(params().get('selected')).toBe(request.id)
    expect(params().get('custom')).toBe('keep')
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['access-requests'] })
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(await screen.findByText(/API Management is unchanged; review and apply/)).toBeVisible()
  })

  it('reconstructs a directly linked approval workspace and preserves approval finality', async () => {
    api.listAccessRequests.mockResolvedValue([{ ...request, state: 'denied' }])
    renderPage('/entitlements?tab=requests&selected=request_1&mode=review')
    const editor = await screen.findByRole('region', { name: 'Approve access request' })
    expect(within(editor).getByRole('button', { name: 'Approve and create grant' })).toBeDisabled()
    expect(api.approveAccessRequest).not.toHaveBeenCalled()
  })

  it('keeps a rejected approval open with its real error', async () => {
    const user = userEvent.setup()
    api.listAccessRequests.mockResolvedValue([request])
    api.approveAccessRequest.mockRejectedValue(new ApiError('This request has already been decided.', 409))
    renderPage('/entitlements?tab=requests&selected=request_1')
    const editor = await openApproval(user)
    await user.click(within(editor).getByRole('button', { name: 'Approve and create grant' }))
    expect(await within(editor).findByText('This request has already been decided.')).toBeVisible()
    expect(editor).toBeVisible()
    expect(screen.queryByText(/Approved the request and created/)).not.toBeInTheDocument()
  })

  it('resolves requester names case-insensitively but retains the exact object ID in details', async () => {
    api.listAccessRequests.mockResolvedValue([request])
    renderPage('/entitlements?tab=requests&selected=request_1')
    const detail = await screen.findByRole('region', { name: 'Selected access request' })
    expect(await within(detail).findByText('Ada Lovelace')).toBeVisible()
    expect(within(detail).getByText('USER-OBJECT-1')).toBeVisible()
  })

  it('does not fabricate requester names when principals fail', async () => {
    api.listPrincipals.mockRejectedValue(new Error('Directory unavailable'))
    api.listAccessRequests.mockResolvedValue([request])
    renderPage('/entitlements?tab=requests&selected=request_1')
    expect(await screen.findByText('Directory unavailable')).toBeVisible()
    const detail = screen.getByRole('region', { name: 'Selected access request' })
    expect(await within(detail).findByText('USER-OBJECT-1')).toBeVisible()
    expect(within(detail).getByRole('button', { name: 'Approve' })).toBeDisabled()
    expect(screen.getByText('Requester name unavailable')).toBeVisible()
  })

  it('denies without creating a grant and invalidates the shell pending key', async () => {
    const user = userEvent.setup()
    api.listAccessRequests.mockResolvedValue([request])
    const { queryClient } = renderPage('/entitlements?tab=requests&selected=request_1')
    const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
    const detail = await screen.findByRole('region', { name: 'Selected access request' })
    await within(detail).findByText('Support bot evaluation')
    await menu(user, detail)
    await user.click(await screen.findByRole('menuitem', { name: 'Deny' }))
    expect(await screen.findByText('Denied the access request. No grant was created, and API Management is unchanged.')).toBeVisible()
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['access-requests'] })
    expect(api.createEntitlement).not.toHaveBeenCalled()
  })

  it('opens model review without applying and keeps the complete access snapshot', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.listModelApis.mockResolvedValue([publishedModelApi])
    renderPage(`/entitlements?tab=settings&selected=${modelPublication.id}`)
    const review = await screen.findByRole('button', { name: 'Review model changes' })
    await waitFor(() => expect(review).toBeEnabled())
    await user.click(review)
    expect(await screen.findByRole('table', { name: 'All target model grants' })).toBeVisible()
    expect(api.createPublishPlan).toHaveBeenCalledWith(modelPublication.id)
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('does not invent an access snapshot when planning fails', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.createPublishPlan.mockRejectedValue(new Error('Planning failed'))
    renderPage(`/entitlements?tab=settings&selected=${modelPublication.id}`)
    const review = await screen.findByRole('button', { name: 'Review model changes' })
    await waitFor(() => expect(review).toBeEnabled())
    await user.click(review)
    expect(await screen.findByText('Planning failed')).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('retains effective-access diagnostics and atomically selects a grant from overlaps', async () => {
    const user = userEvent.setup()
    api.getGrantOverlaps.mockResolvedValue({
      membershipChecked: false, skipped: ['Directory permission missing'],
      overlaps: [{
        kind: 'directAndGroup', resource: entitlement.resource, resourceLabel: 'Chat completions',
        reason: 'Direct grants take precedence', winner: { entitlementId: entitlement.id, subjectLabel: 'Engineering', enforcement: entitlement.enforcement }, shadowed: [],
      }],
    })
    const { params } = renderPage('/entitlements?tab=effective&custom=keep')
    expect(await screen.findByText(/Microsoft Graph membership was not checked/)).toBeVisible()
    await user.click(screen.getByRole('link', { name: /Go to the Engineering grant/ }))
    expect(params().get('tab')).toBe('grants')
    expect(params().get('selected')).toBe(entitlement.id)
    expect(params().get('custom')).toBe('keep')
    expect(await screen.findByText('Engineering evaluation')).toBeVisible()
  })

  it('tears down credential connection state when selection or section changes', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([directGrant, entitlement])
    api.listPublications.mockResolvedValue([modelPublication])
    api.getEntitlementConnection.mockResolvedValue(connectionInfo)
    renderPage(`/entitlements?selected=${directGrant.id}`)
    const detail = await screen.findByRole('region', { name: 'Selected grant' })
    const connection = await within(detail).findByRole('button', { name: 'Connection info' })
    await user.click(connection)
    expect(await screen.findByRole('dialog')).toBeVisible()
    // Programmatic navigation covers notification/sidebar/Back changes while a reveal is mounted.
    fireEvent.click(screen.getByRole('button', { name: 'Access requests', hidden: true }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(api.revealEntitlementKey).not.toHaveBeenCalled()
  })

  it('keeps pool grants linked to their owning pool plan', async () => {
    api.listModelPools.mockResolvedValue([governedAnthropicPool])
    api.listEntitlements.mockResolvedValue([{
      ...entitlement, resource: { kind: 'poolModel', id: governedAnthropicPool.models[0].id, scopeId: governedAnthropicPool.id },
    }])
    renderPage('/entitlements?selected=entitlement_1')
    expect((await screen.findAllByText(/Claude Opus 4.5 in Anthropic Claude/)).length).toBeGreaterThan(0)
    expect(screen.getAllByText(/Pool:/).length).toBeGreaterThan(0)
  })
})

describe('Grant limit wording and validation', () => {
  it('does not equate absent limits with unrestricted gateway access', () => {
    expect(describeLimits({ enforcement: null })).toEqual(['No grant-specific limit is configured.'])
  })
  it('refuses half-filled call rates', () => {
    expect(callRateError({ calls: '30', renewalPeriodSeconds: '' })).toMatch(/how many seconds/)
    expect(callRateError({ calls: '', renewalPeriodSeconds: '' })).toBeNull()
    expect(callRateError({ calls: '30', renewalPeriodSeconds: '60' })).toBeNull()
  })
})
