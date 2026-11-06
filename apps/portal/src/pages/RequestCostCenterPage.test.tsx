import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { ApiError, type PortalApi } from '../api'
import { costCenterRequest } from '../test/cost-center-request-fixtures'
import { RequestCostCenterPage } from './RequestCostCenterPage'

const mocks = vi.hoisted(() => ({ api: {} as Partial<PortalApi> }))
vi.mock('../api', async (importOriginal) => ({
  ...await importOriginal<typeof import('../api')>(),
  usePortalApi: () => mocks.api,
}))

function renderPage(createCostCenterRequest = vi.fn<PortalApi['createCostCenterRequest']>(async () => costCenterRequest())) {
  mocks.api = { createCostCenterRequest, getCostCenterSelfServiceSettings: async () => ({ creationRequestsEnabled: true, membershipRequestsEnabled: false }) }
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([
    { path: '/requests/new-cost-center', element: <RequestCostCenterPage /> },
    { path: '/requests', element: <h1>My requests</h1> },
    { path: '/cost-centers', element: <h1>My cost centers</h1> },
  ], { initialEntries: ['/requests/new-cost-center'] })
  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>)
  return { createCostCenterRequest, router }
}

async function fillRequired() {
  const user = userEvent.setup()
  await user.type(await screen.findByRole('textbox', { name: 'Name' }), ' Research team ')
  await user.type(screen.getByRole('textbox', { name: 'Purpose / justification' }), ' Separate evaluation costs ')
  return user
}

describe('RequestCostCenterPage', () => {
  it('submits only required text and one submission ID without a group suggestion or code', async () => {
    const { createCostCenterRequest, router } = renderPage()
    const user = await fillRequired()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.getByText(/Submitting does not add you to the group/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/requests'))
    expect(router.state.location.search).toBe('?request=center-request-1&requestKind=costCenter')
    expect(createCostCenterRequest).toHaveBeenCalledWith({
      name: 'Research team', code: undefined, description: undefined, justification: 'Separate evaluation costs',
      suggestedGroupObjectId: undefined, idempotencyKey: expect.stringMatching(/^[0-9a-f-]{36}$/),
    })
  })

  it('submits an optional group suggestion without directory search or membership writes', async () => {
    const { createCostCenterRequest } = renderPage()
    const user = await fillRequired()
    await user.type(screen.getByRole('textbox', { name: 'Suggested group object ID (optional)' }), '11111111-2222-3333-4444-555555555555')
    await user.type(screen.getByRole('textbox', { name: 'Code (optional)' }), 'research')
    await user.type(screen.getByRole('textbox', { name: 'Description (optional)' }), 'Evaluation projects')
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    await waitFor(() => expect(createCostCenterRequest).toHaveBeenCalledOnce())
    expect(createCostCenterRequest).toHaveBeenCalledWith(expect.objectContaining({
      suggestedGroupObjectId: '11111111-2222-3333-4444-555555555555', code: 'research', description: 'Evaluation projects',
    }))
    expect(createCostCenterRequest.mock.calls[0]).not.toHaveProperty('addRequester')
  })

  it('retains the draft and the same submission ID across an uncertain network failure and retry', async () => {
    const createCostCenterRequest = vi.fn()
      .mockRejectedValueOnce(new Error('Network connection lost'))
      .mockResolvedValueOnce(costCenterRequest())
    const { router } = renderPage(createCostCenterRequest)
    const user = await fillRequired()
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    expect(await screen.findByText('Request not confirmed')).toBeVisible()
    expect(router.state.location.pathname).toBe('/requests/new-cost-center')
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue(' Research team ')
    expect(screen.getByRole('textbox', { name: 'Purpose / justification' })).toHaveValue(' Separate evaluation costs ')
    expect(screen.queryByText('Request submitted')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/requests'))
    expect(createCostCenterRequest).toHaveBeenCalledTimes(2)
    expect(createCostCenterRequest.mock.calls[0][0]).toEqual(createCostCenterRequest.mock.calls[1][0])
  })

  it('shows required-field and group GUID validation without sending a request', async () => {
    const { createCostCenterRequest } = renderPage()
    const user = userEvent.setup()
    await user.click(await screen.findByRole('button', { name: 'Submit request' }))
    expect(await screen.findByText('Enter a name for the cost center.')).toBeVisible()
    expect(screen.getByText('Explain why you need this cost center.')).toBeVisible()
    await fillRequired()
    await user.type(screen.getByRole('textbox', { name: 'Suggested group object ID (optional)' }), 'not-a-guid')
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    expect(screen.getByText('Enter a group object ID in GUID format, or leave this blank.')).toBeVisible()
    expect(createCostCenterRequest).not.toHaveBeenCalled()
  })

  it('creates a new submission ID only after leaving and opening a genuinely new form', async () => {
    const { createCostCenterRequest, router } = renderPage()
    const user = await fillRequired()
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/requests'))
    await act(() => router.navigate('/requests/new-cost-center'))
    await fillRequired()
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    await waitFor(() => expect(createCostCenterRequest).toHaveBeenCalledTimes(2))
    expect(createCostCenterRequest.mock.calls[0][0].idempotencyKey)
      .not.toBe(createCostCenterRequest.mock.calls[1][0].idempotencyKey)
  })

  it('turns server validation into an actionable field error and does not lose inputs', async () => {
    renderPage(vi.fn().mockRejectedValue(new ApiError('Request failed with status 422', 422, {
      detail: [{ loc: ['body', 'code'], msg: 'Use letters, numbers, and hyphens.' }],
    })))
    const user = await fillRequired()
    await user.type(screen.getByRole('textbox', { name: 'Code (optional)' }), 'invalid code')
    await user.click(screen.getByRole('button', { name: 'Submit request' }))
    expect(await screen.findByText('Use letters, numbers, and hyphens.')).toBeVisible()
    expect(screen.getByRole('textbox', { name: 'Code (optional)' })).toHaveValue('invalid code')
    expect(screen.getByText(/Check the highlighted fields/)).toBeVisible()
  })

  it('protects unsaved drafts and restores focus when keeping the form', async () => {
    const { router } = renderPage()
    const user = await fillRequired()
    const cancel = screen.getByRole('button', { name: 'Cancel' })
    await user.click(cancel)
    expect(await screen.findByRole('dialog')).toHaveTextContent('Discard this cost-center request?')
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(screen.getByRole('textbox', { name: 'Name' })).toHaveValue(' Research team ')
    await waitFor(() => expect(cancel).toHaveFocus())
    expect(router.state.location.pathname).toBe('/requests/new-cost-center')
    await user.click(cancel)
    await user.click(screen.getByRole('button', { name: 'Discard and leave' }))
    expect(await screen.findByRole('heading', { name: 'My cost centers' })).toBeVisible()
  })
})
