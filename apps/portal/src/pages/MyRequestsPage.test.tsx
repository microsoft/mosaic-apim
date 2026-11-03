import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type PortalApi } from '../api'
import type { AccessRequest, PortalCostCenterRequest } from '../types'
import { costCenterRequest } from '../test/cost-center-request-fixtures'
import { MyRequestsPage } from './MyRequestsPage'

const mocks = vi.hoisted(() => ({
  api: {} as PortalApi,
}))

vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  usePortalApi: () => mocks.api,
}))

function accessRequest(overrides: Partial<AccessRequest>): AccessRequest {
  return {
    id: 'request-1',
    tenantId: 'tenant-1',
    entityType: 'accessRequest',
    requesterObjectId: 'user-object-1',
    requesterPrincipalId: null,
    resource: { kind: 'modelApi', id: 'chat-completions', scopeId: null },
    justification: 'Support bot evaluation',
    state: 'pending',
    decidedByObjectId: null,
    decidedAt: null,
    decisionNote: null,
    grantedEntitlementId: null,
    requestedEnvironment: 'production',
    resourceSnapshot: { displayName: 'Chat completions', gatewayId: 'gateway-1', gatewayName: 'Production gateway' },
    resourceSummary: {
      kind: 'modelApi',
      id: 'chat-completions',
      scopeId: null,
      displayName: 'Chat completions',
      gatewayId: 'gateway-1',
      gatewayName: 'Production gateway',
      environment: 'production',
      available: true,
    },
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

const opened = `Opened ${new Date('2026-01-01T00:00:00Z').toLocaleDateString()}`

function renderPage(requests: AccessRequest[], withdrawAccessRequest = vi.fn(), path = '/',
  centers: PortalCostCenterRequest[] = [], getCostCenterRequest = vi.fn(async (id: string) => {
    const request = centers.find((item) => item.id === id)
    if (!request) throw new ApiError('Request not found', 404)
    return request
  }), listFailure?: Error) {
  let costCenterRequests = centers
  const withdrawCostCenterRequest = vi.fn(async (id: string) => {
    const request = costCenterRequests.find((item) => item.id === id)!
    const withdrawn = { ...request, state: 'withdrawn' as const }
    costCenterRequests = costCenterRequests.map((item) => item.id === id ? withdrawn : item)
    return withdrawn
  })
  mocks.api = {
    listAccessRequests: async () => requests,
    listCostCenterRequests: async () => {
      if (listFailure) throw listFailure
      return costCenterRequests
    },
    getCostCenterRequest,
    withdrawCostCenterRequest,
    getRequestCounts: async () => ({
      pendingResourceCount: requests.filter((request) => request.state === 'pending').length,
      pendingCostCenterCount: costCenterRequests.filter((request) => request.state === 'pending').length,
      pendingCount: [...requests, ...costCenterRequests].filter((request) => request.state === 'pending').length,
    }),
    listEnvironments: async () => [
      { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, order: 50 },
    ],
    withdrawAccessRequest,
  } as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[path]}>
        <MyRequestsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { withdrawAccessRequest, withdrawCostCenterRequest, getCostCenterRequest }
}

describe('MyRequestsPage', () => {
  it('combines distinct request kinds and counts without treating a center as a resource', async () => {
    renderPage([accessRequest({ id: 'same-id' })], vi.fn(), '/requests', [costCenterRequest({ id: 'same-id' })])
    expect(await screen.findByRole('heading', { name: 'Research team' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Chat completions' })).toBeVisible()
    expect(screen.getByText('1 resource request pending · 1 cost-center request pending')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Request a cost center' })).toHaveAttribute('href', '/requests/new-cost-center')
    await userEvent.selectOptions(screen.getByLabelText('Request type'), 'costCenter')
    expect(screen.queryByRole('heading', { name: 'Chat completions' })).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Research team' })).toBeVisible()
  })

  it('opens the exact cost-center deep link outside pending filters and never opens a colliding resource ID', async () => {
    const { getCostCenterRequest } = renderPage([accessRequest({ id: 'center/request' })], vi.fn(),
      '/requests?request=center%2Frequest&requestKind=costCenter&status=pending',
      [costCenterRequest({ id: 'center/request', state: 'approved', costCenter: { id: 'cc-1', name: 'Research', code: 'RES' }, setupState: 'setupRequired' })])
    expect(await screen.findByRole('heading', { name: 'Research team' })).toBeVisible()
    expect(getCostCenterRequest).toHaveBeenCalledWith('center/request')
    expect(screen.queryByRole('heading', { name: 'Chat completions' })).not.toBeInTheDocument()
    expect(screen.getByText('Group setup required')).toBeVisible()
    expect(screen.getByText(/It does not confirm that its group/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })

  it('withdraws only a pending cost-center request before preparation and keeps the resource endpoint separate', async () => {
    const { withdrawAccessRequest, withdrawCostCenterRequest } = renderPage([], vi.fn(), '/requests', [
      costCenterRequest(),
      costCenterRequest({ id: 'preparing', name: 'Preparing center', preparationStarted: true }),
      costCenterRequest({ id: 'approved', name: 'Approved center', state: 'approved' }),
      costCenterRequest({ id: 'denied', name: 'Denied center', state: 'denied' }),
      costCenterRequest({ id: 'withdrawn', name: 'Withdrawn center', state: 'withdrawn' }),
    ])
    const withdraw = await screen.findByRole('button', { name: 'Withdraw' })
    expect(screen.getAllByRole('button', { name: 'Withdraw' })).toHaveLength(1)
    expect(screen.getByText(/can no longer be withdrawn/)).toBeVisible()
    await userEvent.click(withdraw)
    await userEvent.click(screen.getByRole('button', { name: 'Confirm withdrawal' }))
    await waitFor(() => expect(withdrawCostCenterRequest).toHaveBeenCalledWith('center-request-1'))
    expect(withdrawAccessRequest).not.toHaveBeenCalled()
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument())
  })

  it.each([403, 404])('shows an unavailable owner-scoped %s response without leaking a list record or another request', async (status) => {
    const failed = vi.fn().mockRejectedValue(new ApiError('Request not found', status))
    renderPage([], vi.fn(), '/requests?request=private&requestKind=costCenter', [costCenterRequest()], failed)
    expect(await screen.findByText('Request unavailable')).toBeVisible()
    expect(screen.queryByText('You do not have access to the portal yet')).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Research team' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })

  it('does not replace a failed list with a success-shaped empty state', async () => {
    renderPage([accessRequest({})], vi.fn(), '/requests', [], undefined, new Error('Cost-center requests unavailable'))
    expect(await screen.findByText('Cost-center requests unavailable')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Chat completions' })).toBeVisible()
    expect(screen.queryByText('No requests opened')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Retry cost-center requests' })).toBeVisible()
  })

  it('describes requester addition as a historical result, not a current membership claim', async () => {
    renderPage([], vi.fn(), '/requests', [costCenterRequest({
      state: 'approved', setupState: 'ready', requesterMembershipRequested: true, requesterMembershipState: 'succeeded',
    })])
    expect(await screen.findByText(/past operation result, not confirmation of current membership/)).toBeVisible()
    expect(screen.getByText(/managed-identity caches can last around 24 hours/)).toBeVisible()
    expect(screen.getByText(/Previously copied keys need separate revocation/)).toBeVisible()
  })

  it('describes approved membership awaiting group setup without implying a directory operation has begun', async () => {
    renderPage([], vi.fn(), '/requests', [costCenterRequest({
      name: 'Knowledge Engineering',
      state: 'approved',
      costCenter: { id: 'cc-knowledge', name: 'Knowledge Engineering', code: 'KNOW' },
      setupState: 'setupRequired',
      requesterMembershipRequested: true,
      requesterMembershipState: 'pending',
      needsAdministratorAttention: true,
    })])
    expect(await screen.findByRole('heading', { name: 'Knowledge Engineering' })).toBeVisible()
    const readiness = within(screen.getByRole('region', { name: 'Cost-center readiness' }))
    expect(readiness.getByText('Group setup required')).toBeVisible()
    expect(readiness.getByText('Your group membership is pending. An administrator must finish or review group setup and the membership step.')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent('Ask your administrator to review setup or the pending membership step.')
    expect(screen.queryByText(/directory operation|membership operation/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })
  it('opens only the exact linked request and returns to the full list', async () => {
    const user = userEvent.setup()
    renderPage([
      accessRequest({ id: 'request/1', resourceDisplayName: 'Linked resource' }),
      accessRequest({ id: 'request-2', resourceDisplayName: 'Other resource' }),
    ], vi.fn(), '/requests?request=request%2F1&status=completed')
    expect(await screen.findByRole('heading', { name: 'Linked resource' })).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Other resource' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Request status')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Show all requests' }))
    expect(screen.getByLabelText('Request status')).toHaveValue('completed')
    await user.selectOptions(screen.getByLabelText('Request status'), 'all')
    expect(screen.getByRole('heading', { name: 'Other resource' })).toBeVisible()
  })

  it('does not replace an unavailable linked request with unrelated records', async () => {
    renderPage([accessRequest({ resourceDisplayName: 'Unrelated resource' })], vi.fn(), '/requests?request=missing')
    expect(await screen.findByText('Request unavailable')).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Unrelated resource' })).not.toBeInTheDocument()
  })
  it('keeps same-model requests distinguishable by their offering and version', async () => {
    renderPage([
      accessRequest({
        id: 'request-first', resourceDisplayName: 'Research offering',
        model: { name: 'gpt-4o', format: 'OpenAI', version: '2024-08-06' },
      }),
      accessRequest({
        id: 'request-second', resourceDisplayName: 'Operations offering',
        model: { name: 'gpt-4o', format: 'OpenAI', version: '2024-11-20' },
      }),
    ])

    const headings = await screen.findAllByRole('heading', { level: 2, name: 'gpt-4o' })
    const first = within(headings[0].closest('.request-card') as HTMLElement)
    const second = within(headings[1].closest('.request-card') as HTMLElement)
    expect(first.getByText('Research offering')).toBeVisible()
    expect(first.getByText(/Version 2024-08-06/)).toBeVisible()
    expect(first.queryByText('Operations offering')).not.toBeInTheDocument()
    expect(second.getByText('Operations offering')).toBeVisible()
    expect(second.getByText(/Version 2024-11-20/)).toBeVisible()
  })

  it('uses a recorded offering when the API omits its display name', async () => {
    renderPage([accessRequest({ model: { name: 'gpt-4o', format: 'OpenAI', version: '2024-11-20' } })])
    expect(await screen.findByRole('heading', { level: 2, name: 'gpt-4o' })).toBeVisible()
    expect(screen.getByText('Chat completions')).toBeVisible()
  })

  it('links an approved request to the grant it created', async () => {
    renderPage([
      accessRequest({
        state: 'approved',
        requesterPrincipalId: 'principal-1',
        decisionNote: 'Approved for the pilot',
        grantedEntitlementId: 'entitlement-1',
      }),
    ])

    const card = (await screen.findByText('Approved for the pilot')).closest('.request-card')
    expect(card).not.toBeNull()
    const request = within(card as HTMLElement)
    expect(request.getByText('Grant')).toBeVisible()
    expect(request.getByText('Production')).toBeVisible()
    expect(request.getByText('Production gateway')).toBeVisible()
    expect(
      request.getByText(/Approval created your grant. It may not work until an administrator applies it./),
    ).toBeVisible()
    expect(request.getByRole('link', { name: 'See its status in My access' })).toHaveAttribute(
      'href',
      '/access?grant=entitlement-1',
    )
    expect(request.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })

  it('names the cost center each request charges', async () => {
    renderPage([
      accessRequest({
        id: 'request-research',
        justification: 'Research notebook',
        costCenterId: 'cc-research',
        costCenter: { id: 'cc-research', name: 'Research', code: 'RES' },
      }),
      accessRequest({
        id: 'request-general',
        justification: 'Personal experiments',
        costCenterId: 'cc-general',
        costCenter: { id: 'cc-general', name: 'General', code: 'general' },
      }),
    ])

    const research = (await screen.findByText('Research notebook')).closest('.request-card')
    const general = screen.getByText('Personal experiments').closest('.request-card')
    expect(within(research as HTMLElement).getByText('Research (RES)')).toBeVisible()
    expect(within(general as HTMLElement).getByText('General (general)')).toBeVisible()
  })

  it('does not imply a grant for an approval that predates linked grants', async () => {
    renderPage([accessRequest({ state: 'approved' })])

    expect(
      await screen.findByText(
        'Approved before approvals created grants, so no grant is linked to this request.',
      ),
    ).toBeVisible()
    expect(screen.queryByRole('link', { name: /My access/ })).not.toBeInTheDocument()
  })

  it('shows no grant for pending or denied requests', async () => {
    renderPage([
      accessRequest({ id: 'request-1' }),
      accessRequest({ id: 'request-2', state: 'denied', decisionNote: 'Not for this project' }),
    ])

    expect(await screen.findByRole('button', { name: 'Withdraw' })).toBeVisible()
    expect(screen.getByText('Not for this project')).toBeVisible()
    expect(screen.queryByText('Grant')).not.toBeInTheDocument()
  })

  it('shows a withdrawal error only on the request that failed', async () => {
    const user = userEvent.setup()
    const withdrawAccessRequest = vi.fn(async () => {
      throw new Error('Withdrawal failed')
    })
    renderPage([
      accessRequest({ id: 'request-1' }),
      accessRequest({
        id: 'request-2',
        resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: null },
        resourceDisplayName: 'Docs search',
      }),
    ], withdrawAccessRequest)

    const firstCard = (await screen.findByRole('heading', { level: 2, name: 'Chat completions' })).closest('.request-card')
    const secondCard = screen.getByRole('heading', { level: 2, name: 'Docs search' }).closest('.request-card')
    const firstRequest = within(firstCard as HTMLElement)
    const secondRequest = within(secondCard as HTMLElement)
    await user.click(firstRequest.getByRole('button', { name: 'Withdraw' }))
    await user.click(firstRequest.getByRole('button', { name: 'Confirm withdrawal' }))

    await waitFor(() => expect(firstRequest.getByText('Withdrawal failed')).toBeVisible())
    expect(secondRequest.queryByText('Withdrawal failed')).not.toBeInTheDocument()
  })

  it('uses summaries and snapshots without rendering raw resource IDs', async () => {
    renderPage([
      accessRequest({
        id: 'request-removed-known',
        resource: { kind: 'modelApi', id: 'modelApi_removed_123', scopeId: null },
        resourceDisplayName: null,
        resourceSummary: {
          kind: 'modelApi',
          id: 'modelApi_removed_123',
          scopeId: null,
          displayName: 'Retired chat',
          gatewayId: 'gateway-1',
          gatewayName: 'Production gateway',
          environment: 'production',
          available: false,
        },
      }),
      accessRequest({
        id: 'request-removed-unknown',
        resource: { kind: 'modelApi', id: 'modelApi_unknown_456', scopeId: null },
        resourceDisplayName: null,
        resourceSnapshot: null,
        resourceSummary: {
          kind: 'modelApi',
          id: 'modelApi_unknown_456',
          scopeId: null,
          displayName: null,
          gatewayId: null,
          gatewayName: null,
          environment: null,
          available: false,
        },
      }),
    ])

    expect(await screen.findByText('Retired chat')).toBeVisible()
    expect(screen.getByText('Resource no longer available')).toBeVisible()
    expect(screen.getAllByText('No longer available')).toHaveLength(2)
    // Neither title names the kind, so each description does.
    expect(screen.getAllByText(`Model API · ${opened}`)).toHaveLength(2)
    expect(screen.queryByText(/modelApi_removed_123|modelApi_unknown_456/)).not.toBeInTheDocument()
  })

  it('heads each request with the name the catalog shows and keeps its kind visible', async () => {
    renderPage([
      accessRequest({ id: 'request-1', resourceDisplayName: 'Chat model' }),
      accessRequest({
        id: 'request-2',
        resource: { kind: 'mcpServer', id: 'docs-mcp', scopeId: null },
        resourceDisplayName: 'Docs search',
      }),
    ])

    expect(await screen.findByRole('heading', { level: 2, name: 'Chat model' })).toBeVisible()
    expect(screen.getByText(`Model API · ${opened}`)).toBeVisible()
    expect(screen.getByRole('heading', { level: 2, name: 'Docs search' })).toBeVisible()
    expect(screen.getByText(`MCP server · ${opened}`)).toBeVisible()
    expect(screen.queryByText(/chat-completions|docs-mcp/)).not.toBeInTheDocument()
  })

  it.each([
    ['an older API omits the name', {}],
    ['the API cannot resolve the name', { resourceDisplayName: null }],
    ['the API sends a blank name', { resourceDisplayName: '  ' }],
  ] satisfies [string, Partial<AccessRequest>][])(
    'falls back to the kind, never the ID, when %s',
    async (_, name) => {
      renderPage([accessRequest({ resourceSnapshot: null, resourceSummary: null, ...name })])

      expect(
        await screen.findByRole('heading', { level: 2, name: 'Model API resource' }),
      ).toBeVisible()
      expect(screen.getByText(opened)).toBeVisible()
      expect(screen.queryByText(/chat-completions/)).not.toBeInTheDocument()
    },
  )
})
