import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type PortalApi } from '../api'
import type { MembershipCostCenter, PortalCostCenterMembershipRequest } from '../types'
import { MyCostCentersPage } from './MyCostCentersPage'
import { RequestCostCenterMembershipPage } from './RequestCostCenterMembershipPage'
import { RequestCostCenterPage } from './RequestCostCenterPage'
import { MyRequestsPage } from './MyRequestsPage'

const mocks = vi.hoisted(() => ({ api: {} as Partial<PortalApi> }))
vi.mock('../api', async (original) => ({
  ...await original<typeof import('../api')>(), usePortalApi: () => mocks.api,
}))
const center: MembershipCostCenter = {
  id: 'cc-research', name: 'Research', code: 'RES', publicPurpose: 'Evaluate models together.',
  builtIn: false, membershipState: 'notMember', tokenEligible: false,
}
const request: PortalCostCenterMembershipRequest = {
  id: 'join/1', requestKind: 'costCenterMembership', costCenter: { id: center.id, name: center.name, code: center.code },
  justification: 'Evaluate safely', state: 'pending', createdAt: '2026-11-01T00:00:00Z', updatedAt: '2026-11-01T00:00:00Z',
  membershipState: null, needsAdministratorAttention: false,
}
function renderPage(path = '/cost-centers') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([
    { path: '/cost-centers', element: <MyCostCentersPage /> },
    { path: '/cost-centers/:id/join', element: <RequestCostCenterMembershipPage /> },
    { path: '/cost-centers/new', element: <RequestCostCenterPage /> },
    { path: '/requests', element: <MyRequestsPage /> },
  ], { initialEntries: [path] })
  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>)
  return router
}
beforeEach(() => {
  mocks.api = {
    getCostCenterSelfServiceSettings: vi.fn(async () => ({ creationRequestsEnabled: true, membershipRequestsEnabled: true })),
    listMyCostCenters: vi.fn(async () => ({ items: [{ ...center, membershipState: 'member' as const }], nextCursor: null, membershipError: null })),
    searchCostCenters: vi.fn(async () => ({ items: [center], nextCursor: null, membershipError: null })),
    getDiscoverableCostCenter: vi.fn(async () => center),
    listCostCenterMembershipRequests: vi.fn(async () => []),
    getCostCenterMembershipRequest: vi.fn(async () => request),
    createCostCenterMembershipRequest: vi.fn(async () => request),
    withdrawCostCenterMembershipRequest: vi.fn(async () => ({ ...request, state: 'withdrawn' as const })),
    listAccessRequests: vi.fn(async () => []),
    listCostCenterRequests: vi.fn(async () => []),
    getRequestCounts: vi.fn(async () => ({ pendingCount: 1, pendingResourceCount: 0, pendingCostCenterCount: 0, pendingMembershipCount: 1 })),
    listEnvironments: vi.fn(async () => []),
  }
})

describe('My cost centers', () => {
  it('opens membership and creation in their own area, and searches only the directory endpoint', async () => {
    const user = userEvent.setup()
    renderPage()
    expect(await screen.findByRole('button', { name: 'Research' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Request a cost center' })).toHaveAttribute('href', '/cost-centers/new')
    expect(screen.queryByRole('link', { name: 'Request to join' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: 'Find cost centers' }))
    expect(await screen.findByRole('link', { name: 'Request to join' })).toHaveAttribute('href', '/cost-centers/cc-research/join')
    await user.type(screen.getByRole('textbox', { name: 'Search published cost centers' }), 'Research')
    await user.click(screen.getByRole('button', { name: 'Search' }))
    await waitFor(() => expect(mocks.api.searchCostCenters).toHaveBeenCalledWith({ q: 'Research', cursor: undefined }))
  })
  it('shows confirmed directory membership separately from token eligibility', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('button', { name: 'Research' }))
    expect(await screen.findByText(/The directory confirms membership/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'View my resource access' })).toHaveAttribute('href', '/access?costCenter=cc-research')
    expect(screen.getByRole('link', { name: 'Browse resource offerings' })).toHaveAttribute('href', '/catalog?costCenter=cc-research')
  })
  it('retains memberships and history when new requests are disabled', async () => {
    mocks.api.getCostCenterSelfServiceSettings = async () => ({ creationRequestsEnabled: false, membershipRequestsEnabled: false })
    renderPage('/cost-centers?view=discover')
    expect(await screen.findByText(/turned off new membership requests/)).toBeVisible()
    expect(await screen.findByRole('button', { name: 'Research' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Request a cost center' })).toBeDisabled()
    expect(screen.queryByRole('link', { name: 'Request to join' })).not.toBeInTheDocument()
  })
  it('tracks an approved but failed membership instead of offering another join', async () => {
    mocks.api.listCostCenterMembershipRequests = async () => [{ ...request, state: 'approved', membershipState: 'failed', needsAdministratorAttention: true }]
    renderPage('/cost-centers?view=discover')
    expect(await screen.findByRole('link', { name: 'View membership progress' })).toHaveAttribute('href', '/requests?request=join%2F1&requestKind=costCenterMembership')
    expect(screen.getByText(/Membership addition failed/)).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Request to join' })).not.toBeInTheDocument()
  })
  it('does not turn failed membership verification into a no-membership success', async () => {
    mocks.api.listMyCostCenters = async () => ({
      items: [{ ...center, membershipState: 'unknown' }], nextCursor: null, membershipError: 'Current membership could not be checked.',
    })
    renderPage()
    expect(await screen.findByText(/Current membership could not be checked/)).toBeVisible()
    expect(screen.getByText('Membership unavailable')).toBeVisible()
    expect(screen.queryByText('No memberships found')).not.toBeInTheDocument()
  })
  it('paginates search with the server cursor without replacing already-loaded rows', async () => {
    mocks.api.searchCostCenters = vi.fn(async ({ cursor } = {}) => ({
      items: [cursor ? { ...center, id: 'cc-other', name: 'Engineering' } : center],
      nextCursor: cursor ? null : 'next/+',
      membershipError: null,
    }))
    const user = userEvent.setup()
    renderPage('/cost-centers?view=discover')
    await user.click(await screen.findByRole('button', { name: 'Load more cost centers' }))
    expect(await screen.findByRole('button', { name: 'Engineering' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Research' })).toBeVisible()
    expect(mocks.api.searchCostCenters).toHaveBeenCalledWith({ q: '', cursor: 'next/+' })
  })
})

describe('Membership request', () => {
  it('uses one submission ID across uncertain retries and tracks the exact request', async () => {
    mocks.api.createCostCenterMembershipRequest = vi.fn()
      .mockRejectedValueOnce(new Error('Connection lost')).mockResolvedValueOnce(request)
    const router = renderPage('/cost-centers/cc-research/join')
    const user = userEvent.setup()
    await user.type(await screen.findByRole('textbox', { name: 'Purpose / justification' }), ' Evaluate safely ')
    await user.click(screen.getByRole('button', { name: 'Submit join request' }))
    expect(await screen.findByText('Connection lost')).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Purpose / justification' })).toHaveValue(' Evaluate safely ')
    await user.click(screen.getByRole('button', { name: 'Submit join request' }))
    await waitFor(() => expect(router.state.location.search).toBe('?request=join%2F1&requestKind=costCenterMembership'))
    const create = vi.mocked(mocks.api.createCostCenterMembershipRequest!)
    expect(create.mock.calls[0][0]).toEqual(create.mock.calls[1][0])
    expect(create.mock.calls[0][0]).toEqual({ costCenterId: center.id, justification: 'Evaluate safely', idempotencyKey: expect.stringMatching(/^[a-f0-9-]{36}$/) })
    expect(await screen.findByText('Cost-center membership', { exact: false })).toBeVisible()
  })
  it('does not submit a blank justification', async () => {
    renderPage('/cost-centers/cc-research/join')
    await userEvent.click(await screen.findByRole('button', { name: 'Submit join request' }))
    expect(await screen.findByText('Explain why you need to join this cost center.')).toBeVisible()
    expect(mocks.api.createCostCenterMembershipRequest).not.toHaveBeenCalled()
  })
  it('does not show a form when membership is unknown or already present', async () => {
    mocks.api.getDiscoverableCostCenter = async () => ({ ...center, membershipState: 'unknown' })
    renderPage('/cost-centers/cc-research/join')
    expect(await screen.findByText(/Membership could not be verified/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Submit join request' })).not.toBeInTheDocument()
  })
  it('does not expose a private center after an owner-scoped 404', async () => {
    mocks.api.getDiscoverableCostCenter = async () => { throw new ApiError('Cost center not found', 404) }
    renderPage('/cost-centers/private/join')
    expect(await screen.findByText('Cost center not found')).toBeVisible()
    expect(screen.queryByText('Research')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Submit join request' })).not.toBeInTheDocument()
  })
  it('explains disabled creation on a direct link', async () => {
    mocks.api.getCostCenterSelfServiceSettings = async () => ({ creationRequestsEnabled: false, membershipRequestsEnabled: false })
    renderPage('/cost-centers/new')
    expect(await screen.findByText(/turned off new cost-center creation requests/)).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Name' })).not.toBeInTheDocument()
  })
  it('withdraws the exact join request, not a resource request sharing its ID', async () => {
    const router = renderPage('/requests?request=join%2F1&requestKind=costCenterMembership')
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Withdraw' }))
    await user.click(screen.getByRole('button', { name: 'Confirm withdrawal' }))
    await waitFor(() => expect(mocks.api.withdrawCostCenterMembershipRequest).toHaveBeenCalledWith('join/1'))
    expect(router.state.location.pathname).toBe('/requests')
  })
})
