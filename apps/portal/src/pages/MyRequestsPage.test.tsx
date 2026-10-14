import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { AccessRequest } from '../types'
import { MyRequestsPage } from './MyRequestsPage'

const mocks = vi.hoisted(() => ({
  api: {} as PortalApi,
}))

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {
    status = 500
  },
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

function renderPage(requests: AccessRequest[], withdrawAccessRequest = vi.fn()) {
  mocks.api = {
    listAccessRequests: async () => requests,
    listEnvironments: async () => [
      { key: 'production', displayName: 'Production', description: null, color: 'danger', production: true, order: 50 },
    ],
    withdrawAccessRequest,
  } as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <MyRequestsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
  return { withdrawAccessRequest }
}

describe('MyRequestsPage', () => {
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
