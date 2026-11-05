import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ReactNode } from 'react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { centerCapabilities, costCenterMembershipRequest, readyCostCenter } from '../test/cost-center-fixtures'
import { CostCenterMembershipReview, CostCenterMembershipSummary } from './CostCenterMembershipReview'
import { CostCenterSettings } from './CostCenterSettings'

const api = {
  getCostCenterSettings: vi.fn(), updateCostCenterSettings: vi.fn(), getCostCenterGroupCapabilities: vi.fn(),
  getDirectoryObject: vi.fn(), getCostCenter: vi.fn(), getCostCenterMembershipRequest: vi.fn(),
  approveCostCenterMembershipRequest: vi.fn(), denyCostCenterMembershipRequest: vi.fn(), resumeCostCenterMembershipRequest: vi.fn(),
}
vi.mock('../api', () => ({ useMosaicApi: () => api }))

function mount(element: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <QueryClientProvider client={client}>
    <FluentProvider theme={webLightTheme}>{element}</FluentProvider>
  </QueryClientProvider> }])
  render(<RouterProvider router={router} />)
  return client
}

beforeEach(() => {
  vi.resetAllMocks()
  api.getCostCenterGroupCapabilities.mockResolvedValue(centerCapabilities)
  api.getDirectoryObject.mockResolvedValue({ kind: 'user', displayName: 'Fictional analyst' })
  api.getCostCenter.mockResolvedValue(readyCostCenter)
  api.getCostCenterSettings.mockResolvedValue({ defaultCostCenterId: 'general' })
  api.updateCostCenterSettings.mockResolvedValue({ defaultCostCenterId: 'general', creationRequestsEnabled: true, membershipRequestsEnabled: true })
  api.approveCostCenterMembershipRequest.mockResolvedValue({ ...costCenterMembershipRequest, state: 'approved', membershipState: 'succeeded' })
  api.denyCostCenterMembershipRequest.mockResolvedValue({ ...costCenterMembershipRequest, state: 'denied' })
})

describe('cost-center self-service settings', () => {
  it('uses compatible defaults and sends only the changed switch', async () => {
    const user = userEvent.setup()
    mount(<CostCenterSettings />)
    expect(await screen.findByRole('switch', { name: 'Allow cost-center creation requests' })).toBeChecked()
    expect(screen.getByRole('switch', { name: 'Allow cost-center membership requests' })).not.toBeChecked()
    await user.click(screen.getByRole('switch', { name: 'Allow cost-center membership requests' }))
    await waitFor(() => expect(api.updateCostCenterSettings).toHaveBeenCalledWith({ membershipRequestsEnabled: true }))
    expect(await screen.findByText('Cost-center settings saved.')).toBeVisible()
    expect(screen.getByText(/does not grant Graph consent/)).toBeVisible()
  })

  it('retains the saved policy when persistence fails and permits retry', async () => {
    api.updateCostCenterSettings.mockRejectedValue(new Error('Policy write unavailable'))
    const user = userEvent.setup()
    mount(<CostCenterSettings />)
    await user.click(await screen.findByRole('switch', { name: 'Allow cost-center membership requests' }))
    expect(await screen.findByText('Policy write unavailable')).toBeVisible()
    expect(screen.getByRole('switch', { name: 'Allow cost-center membership requests' })).not.toBeChecked()
    await user.click(screen.getByRole('switch', { name: 'Allow cost-center membership requests' }))
    expect(api.updateCostCenterSettings).toHaveBeenCalledTimes(2)
  })
})

describe('manual membership review', () => {
  it('names the real person and center and requires explicit acknowledgment before approval', async () => {
    const user = userEvent.setup()
    const back = vi.fn()
    mount(<CostCenterMembershipReview request={costCenterMembershipRequest} onBack={back} />)
    expect(await screen.findByText('Fictional analyst')).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Join Research' })).toBeVisible()
    const approve = screen.getByRole('button', { name: 'Approve membership request' })
    expect(approve).toBeDisabled()
    await user.type(screen.getByLabelText('Decision note'), 'Reviewed team impact')
    await user.click(screen.getByRole('checkbox', { name: /changes a real Entra group/ }))
    await waitFor(() => expect(approve).toBeEnabled())
    await user.click(approve)
    await waitFor(() => expect(api.approveCostCenterMembershipRequest).toHaveBeenCalledWith(costCenterMembershipRequest.id, {
      note: 'Reviewed team impact', acknowledgeMembershipChanges: true,
    }))
    expect(back).toHaveBeenCalled()
    expect(api.resumeCostCenterMembershipRequest).not.toHaveBeenCalled()
  })

  it('allows denial without consenting to a membership write', async () => {
    const user = userEvent.setup()
    mount(<CostCenterMembershipReview request={costCenterMembershipRequest} onBack={vi.fn()} />)
    await user.click(screen.getByRole('button', { name: 'Deny membership request' }))
    await waitFor(() => expect(api.denyCostCenterMembershipRequest).toHaveBeenCalledWith(costCenterMembershipRequest.id, {}))
    expect(api.approveCostCenterMembershipRequest).not.toHaveBeenCalled()
  })

  it.each(['pending', 'failed', 'unknown'] as const)('keeps approved / %s distinct and resumes the same operation', async (membershipState) => {
    const user = userEvent.setup()
    const request = { ...costCenterMembershipRequest, state: 'approved' as const, membershipState, operationId: 'operation_recorded' }
    api.resumeCostCenterMembershipRequest.mockResolvedValue(request)
    mount(<CostCenterMembershipReview request={request} onBack={vi.fn()} />)
    expect(screen.getByText('Administrator action needed')).toBeVisible()
    expect(screen.getByText(`${membershipState} — membership not confirmed`)).toBeVisible()
    expect(screen.getByText(/operation_recorded/)).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: /changes a real Entra group/ }))
    const resume = screen.getByRole('button', { name: 'Resume recorded approval' })
    await waitFor(() => expect(resume).toBeEnabled())
    await user.click(resume)
    await waitFor(() => expect(api.resumeCostCenterMembershipRequest).toHaveBeenCalledWith(request.id, { acknowledgeMembershipChanges: true }))
    expect(api.approveCostCenterMembershipRequest).not.toHaveBeenCalled()
  })

  it('blocks approval while center readiness cannot be verified', async () => {
    api.getCostCenter.mockRejectedValue(new Error('Directory target unavailable'))
    const user = userEvent.setup()
    mount(<CostCenterMembershipReview request={costCenterMembershipRequest} onBack={vi.fn()} />)
    await user.click(screen.getByRole('checkbox', { name: /changes a real Entra group/ }))
    expect(await screen.findByText('Directory target unavailable')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Approve membership request' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Deny membership request' })).toBeEnabled()
  })

  it('requires a fresh recorded outcome after an uncertain decision and never replays automatically', async () => {
    api.approveCostCenterMembershipRequest.mockRejectedValue(new Error('Connection lost after decision'))
    api.getCostCenterMembershipRequest.mockResolvedValue({
      ...costCenterMembershipRequest, state: 'approved', membershipState: 'unknown', operationId: 'original-operation',
    })
    const user = userEvent.setup()
    mount(<CostCenterMembershipReview request={costCenterMembershipRequest} onBack={vi.fn()} />)
    await user.click(screen.getByRole('checkbox', { name: /changes a real Entra group/ }))
    const approve = screen.getByRole('button', { name: 'Approve membership request' })
    await waitFor(() => expect(approve).toBeEnabled())
    await user.click(approve)
    expect(await screen.findByText(/Outcome may be partial/)).toBeVisible()
    expect(approve).toBeDisabled()
    expect(api.approveCostCenterMembershipRequest).toHaveBeenCalledTimes(1)
    await user.click(screen.getByRole('button', { name: 'Refresh recorded request' }))
    await waitFor(() => expect(api.getCostCenterMembershipRequest).toHaveBeenCalledWith(costCenterMembershipRequest.id))
    expect(api.resumeCostCenterMembershipRequest).not.toHaveBeenCalled()
  })

  it('protects review notes when cancelling the full workspace', async () => {
    const back = vi.fn()
    const user = userEvent.setup()
    mount(<CostCenterMembershipReview request={costCenterMembershipRequest} onBack={back} />)
    await user.type(screen.getByLabelText('Decision note'), 'Keep this draft')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(back).not.toHaveBeenCalled()
    expect(screen.getByLabelText('Decision note')).toHaveValue('Keep this draft')
  })

  it('reports lookup failure without substituting an object ID as a name', async () => {
    api.getDirectoryObject.mockRejectedValue(new Error('Lookup unavailable'))
    mount(<CostCenterMembershipSummary request={costCenterMembershipRequest} />)
    expect(await screen.findByText('Requester name unavailable')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry name lookup' })).toBeVisible()
    expect(screen.queryByText(costCenterMembershipRequest.requesterObjectId)).not.toBeInTheDocument()
  })
})
