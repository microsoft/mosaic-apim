import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import type { ReviewDetail } from '../approvals'
import { ApprovalsPage } from './ApprovalsPage'

const mocks = vi.hoisted(() => ({ getApproval: vi.fn(), decideApproval: vi.fn() }))
vi.mock('@azure/msal-react', () => ({ useMsal: () => ({ accounts: [{ homeAccountId: 'reviewer' }] }) }))
vi.mock('../api', async importOriginal => ({
  ...await importOriginal<typeof import('../api')>(),
  usePortalApi: () => mocks,
}))

const pending: ReviewDetail = {
  id: 'request-1', kind: 'resourceAccess', title: 'Fictional model', requester: 'Fictional requester',
  scope: { center: { id: 'support', code: 'SUP', name: 'Customer Support' }, roles: ['billing'] },
  state: 'pending', createdAt: '2026-01-01T10:00:00Z', justification: 'Run internal tests',
  recordedForm: null, environment: 'Production', canDecide: true, approvalBlocked: false,
  hasPersonLimits: false, decisionAt: null, decisionReason: null, decidedByYou: false,
  membershipState: null, revision: 'review-v1',
}

function openDeepLink() {
  const router = createMemoryRouter([{ path: '/approvals/:kind/:requestId', element: <ApprovalsPage /> }],
    { initialEntries: ['/approvals/resourceAccess/request-1'] })
  return render(<QueryClientProvider client={new QueryClient()}>
    <RouterProvider router={router} />
  </QueryClientProvider>)
}

describe('center-first approvals', () => {
  it('loads the authorized detail and sends only the reviewed revision and reason', async () => {
    mocks.getApproval.mockResolvedValue(pending)
    mocks.decideApproval.mockResolvedValue({
      ...pending, canDecide: false, state: 'approved', decisionAt: '2026-01-01T11:00:00Z',
      decisionReason: 'Reviewed', decidedByYou: true,
    })
    openDeepLink()
    expect(await screen.findByText(/Billing POC · This cost center only/)).toBeVisible()
    expect(screen.getByText(/No per-person limits are configured/)).toBeVisible()
    fireEvent.change(screen.getByRole('textbox', { name: /decision reason/i }), { target: { value: 'Reviewed' } })
    fireEvent.click(screen.getByRole('button', { name: 'Approve resource access' }))
    await waitFor(() => expect(mocks.decideApproval).toHaveBeenCalledWith(
      'resourceAccess', 'request-1', 'approve',
      { revision: 'review-v1', reason: 'Reviewed', acknowledgeMembershipChanges: false },
    ))
    expect(await screen.findByText(/Approved by you/)).toBeVisible()
  })

  it('shows no request evidence if assignment access is revoked', async () => {
    mocks.getApproval.mockRejectedValue(new ApiError('Not found', 404))
    openDeepLink()
    expect(await screen.findByRole('heading', { name: 'Approval unavailable' })).toBeVisible()
    expect(screen.queryByText('Fictional requester')).not.toBeInTheDocument()
  })
})
