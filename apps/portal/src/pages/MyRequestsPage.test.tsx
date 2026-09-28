import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
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
    createdAt: '2026-01-01T00:00:00Z',
    updatedAt: '2026-01-01T00:00:00Z',
    ...overrides,
  }
}

function renderPage(requests: AccessRequest[]) {
  mocks.api = {
    listAccessRequests: async () => requests,
    withdrawAccessRequest: vi.fn(),
  } as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <MyRequestsPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('MyRequestsPage', () => {
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
    expect(
      request.getByText(/Approval created your grant. It may not work until an administrator applies it./),
    ).toBeVisible()
    expect(request.getByRole('link', { name: 'See its status in My access' })).toHaveAttribute(
      'href',
      '/access',
    )
    expect(request.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
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
})
