import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState, type ReactNode } from 'react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { centerCapabilities, centerGroupObjectId, costCenterCreationRequest, directoryMemberPage, memberObjectId, memberOperation, readyCostCenter } from '../test/cost-center-fixtures'
import { modelPublication, publishedModelApi } from '../test/model-access'
import { governedAnthropicPool } from '../test/pool-fixtures'
import type { AdminCostCenterRequest, CostCenterResourceAccess, DirectoryObject } from '../types'
import { CostCenterDirectoryMembers } from './CostCenterDirectoryMembers'
import { CostCenterDirectoryOperations } from './CostCenterDirectoryOperations'
import { CostCenterEditor } from './CostCenterSetupForm'
import { CostCenterResourceAccessFields } from './CostCenterResourceAccess'
import { CostCenterRequestReview, CostCenterRequestSummary } from './CostCenterRequestReview'

const api = {
  getCostCenterGroupCapabilities: vi.fn(), createCostCenter: vi.fn(), updateCostCenter: vi.fn(),
  listModelApis: vi.fn(), listMcpServers: vi.fn(), listModelPools: vi.fn(), listPublications: vi.fn(),
  listMcpPublications: vi.fn(), listGateways: vi.fn(), listEntitlements: vi.fn(),
  searchDirectory: vi.fn(), getDirectoryObject: vi.fn(),
  listCostCenterDirectoryMembers: vi.fn(), addCostCenterDirectoryMember: vi.fn(), removeCostCenterDirectoryMember: vi.fn(),
  listCostCenterDirectoryOperations: vi.fn(), getCostCenterDirectoryOperation: vi.fn(),
  retryCostCenterDirectoryOperation: vi.fn(), recoverCostCenterDirectoryOperation: vi.fn(),
  approveCostCenterRequest: vi.fn(), denyCostCenterRequest: vi.fn(), getCostCenterRequest: vi.fn(),
  createPrincipal: vi.fn(), addCostCenterMember: vi.fn(), createEntitlement: vi.fn(),
}
vi.mock('../api', async (original) => ({ ...await original<typeof import('../api')>(), useMosaicApi: () => api }))

function mount(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <QueryClientProvider client={client}>
    <FluentProvider theme={webLightTheme}>{children}</FluentProvider>
  </QueryClientProvider> }])
  render(<RouterProvider router={router} />)
  return client
}

const group: DirectoryObject = { objectId: centerGroupObjectId, kind: 'securityGroup', displayName: 'Fictional Research', detail: 'Dedicated security group' }
const person: DirectoryObject = { objectId: memberObjectId, kind: 'user', displayName: 'Fictional analyst' }
const published = { ...modelPublication, resources: [{ kind: 'api', name: modelPublication.apiName, resourceId: 'fictional-api', createdByMosaic: true, appliedAt: '2026-11-01T10:00:00Z' }] }

beforeEach(() => {
  vi.resetAllMocks()
  api.getCostCenterGroupCapabilities.mockResolvedValue(centerCapabilities)
  api.createCostCenter.mockResolvedValue({ ...readyCostCenter, setupState: 'setupRequired', entraBinding: null })
  api.updateCostCenter.mockResolvedValue(readyCostCenter)
  api.listModelApis.mockResolvedValue([])
  api.listMcpServers.mockResolvedValue([])
  api.listModelPools.mockResolvedValue([])
  api.listPublications.mockResolvedValue([])
  api.listMcpPublications.mockResolvedValue([])
  api.listGateways.mockResolvedValue([{ id: publishedModelApi.gatewayId, azureCloud: 'commercial' }, { id: governedAnthropicPool.gatewayId, azureCloud: 'commercial' }])
  api.listEntitlements.mockResolvedValue([])
  api.searchDirectory.mockResolvedValue([person])
  api.getDirectoryObject.mockResolvedValue(group)
  api.listCostCenterDirectoryMembers.mockResolvedValue(directoryMemberPage)
  api.listCostCenterDirectoryOperations.mockResolvedValue([])
  api.addCostCenterDirectoryMember.mockResolvedValue(memberOperation)
  api.removeCostCenterDirectoryMember.mockResolvedValue({ ...memberOperation, kind: 'removeMember' })
  api.getCostCenterRequest.mockResolvedValue(costCenterCreationRequest)
  api.approveCostCenterRequest.mockResolvedValue({
    ...costCenterCreationRequest, state: 'approved', setupState: 'setupRequired', needsAdministratorAttention: true,
    costCenter: { id: readyCostCenter.id, name: readyCostCenter.name, code: readyCostCenter.code },
  })
})

async function enterCenter(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText('Name'), 'Research')
  await user.type(screen.getByLabelText('Code'), 'research')
}

async function verifyIdentity(user: ReturnType<typeof userEvent.setup>, label: 'Group object ID' | 'Member object ID', id: string) {
  await user.click(screen.getByText('Use an object ID'))
  await user.type(screen.getByLabelText(label), id)
  await user.click(screen.getByRole('button', { name: 'Verify directory identity' }))
  expect(api.getDirectoryObject).toHaveBeenCalledWith(id)
}

describe('shared cost-center setup', () => {
  it('publishes only an existing ready custom center with explicit group-impact acknowledgment', async () => {
    const user = userEvent.setup()
    mount(<CostCenterEditor costCenter={readyCostCenter} onCancel={vi.fn()} onSaved={vi.fn()} />)
    await user.click(await screen.findByRole('switch', { name: 'Discoverable in My cost centers' }))
    await user.type(screen.getByLabelText('Public purpose'), 'Shared research work')
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /approved join requests change this real Entra group/ }))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.updateCostCenter).toHaveBeenCalledWith(readyCostCenter.id, expect.objectContaining({
      discoverable: true, publicPurpose: 'Shared research work', acknowledgeMembershipChanges: true,
    })))
  })

  it('does not expose publication controls while creating a center', async () => {
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    expect(screen.queryByRole('switch', { name: 'Discoverable in My cost centers' })).not.toBeInTheDocument()
    expect(screen.getByText(/New centers start private/)).toBeVisible()
  })

  it('saves incomplete setup without a directory write or implicit owner enrollment', async () => {
    const user = userEvent.setup()
    const saved = vi.fn()
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={saved} />)
    await enterCenter(user)
    await user.type(screen.getByLabelText('Owners'), 'owner@example.com')
    expect(screen.getByText(/do not assign Entra group ownership or membership/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    await waitFor(() => expect(api.createCostCenter).toHaveBeenCalledWith({
      name: 'Research', code: 'research', description: null, owners: ['owner@example.com'],
      keysAllowed: true, resourceAccess: [],
    }))
    expect(saved).toHaveBeenCalledWith(readyCostCenter.id)
    expect(api.addCostCenterDirectoryMember).not.toHaveBeenCalled()
    expect(api.createPrincipal).not.toHaveBeenCalled()
  })

  it('requires explicit real-membership acknowledgment before attaching a looked-up group', async () => {
    const user = userEvent.setup()
    api.searchDirectory.mockResolvedValue([group])
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    await enterCenter(user)
    await user.click(screen.getByRole('radio', { name: 'Attach existing group' }))
    await user.type(screen.getByLabelText('Find Entra security group'), 'Research')
    const results = await screen.findByRole('list', { name: 'Security group results' })
    await user.click(within(results).getByRole('button'))
    expect(screen.getByRole('button', { name: 'Create cost center' })).toBeDisabled()
    expect(api.createCostCenter).not.toHaveBeenCalled()
    await user.click(screen.getByRole('checkbox', { name: /I acknowledge that membership edits change this real Entra group/ }))
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    await waitFor(() => expect(api.createCostCenter).toHaveBeenCalledWith(expect.objectContaining({
      groupSetup: { mode: 'attach', groupObjectId: centerGroupObjectId, acknowledgeMembershipChanges: true },
    })))
    expect(api.addCostCenterMember).not.toHaveBeenCalled()
  })

  it('ignores stale group verification after a replacement object ID is verified', async () => {
    const user = userEvent.setup()
    let finish!: (value: DirectoryObject) => void
    const pending = new Promise<DirectoryObject>((resolve) => { finish = resolve })
    const replacement = { ...group, objectId: '77777777-7777-4777-8777-777777777777', displayName: 'Replacement fictional group' }
    api.getDirectoryObject.mockReturnValueOnce(pending).mockResolvedValueOnce(replacement)
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    await enterCenter(user)
    await user.click(screen.getByRole('radio', { name: 'Attach existing group' }))
    await verifyIdentity(user, 'Group object ID', group.objectId)
    await user.clear(screen.getByLabelText('Group object ID'))
    await user.type(screen.getByLabelText('Group object ID'), replacement.objectId)
    await user.click(screen.getByRole('button', { name: 'Verify directory identity' }))
    expect(await screen.findByText(replacement.displayName)).toBeVisible()
    await act(async () => { finish(group); await pending })
    expect(screen.getByText(/Selected:/)).toHaveTextContent(replacement.displayName)
    await user.click(screen.getByRole('checkbox', { name: /I acknowledge that membership edits change this real Entra group/ }))
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    await waitFor(() => expect(api.createCostCenter).toHaveBeenCalledWith(expect.objectContaining({
      groupSetup: { mode: 'attach', groupObjectId: replacement.objectId, acknowledgeMembershipChanges: true },
    })))
  })

  it('previews ordinary group creation only after a read-only capability check', async () => {
    const user = userEvent.setup()
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    await enterCenter(user)
    await user.click(screen.getByRole('radio', { name: 'Create with MOSAIC' }))
    const preview = screen.getByRole('region', { name: 'Group creation preview' })
    expect(within(preview).getByText('Research')).toBeVisible()
    expect(within(preview).getByText(/Assigned-membership security group/)).toBeVisible()
    expect(within(preview).getByText(/API service principal, not the contact/)).toBeVisible()
    expect(api.getCostCenterGroupCapabilities).toHaveBeenCalled()
    expect(api.createCostCenter).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    await waitFor(() => expect(api.createCostCenter).toHaveBeenCalledWith(expect.objectContaining({
      groupSetup: { mode: 'create', displayName: null },
    })))
  })

  it('does not equate Admin with consent and still lets the center be saved incomplete', async () => {
    const user = userEvent.setup()
    api.getCostCenterGroupCapabilities.mockResolvedValue({ ...centerCapabilities, canCreateGroups: false, canAddUsers: false, canAddServicePrincipals: false, canRemoveMembers: false })
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    await enterCenter(user)
    await user.click(screen.getByRole('radio', { name: 'Create with MOSAIC' }))
    expect(screen.getByText(/MOSAIC Admin is not Graph admin consent/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Create cost center' })).toBeDisabled()
    await user.click(screen.getByRole('radio', { name: 'Finish setup later' }))
    expect(screen.getByRole('button', { name: 'Create cost center' })).toBeEnabled()
  })

  it('retains the saved center after verification fails and prevents duplicate creation', async () => {
    const user = userEvent.setup()
    api.createCostCenter.mockRejectedValue(new ApiError('Dynamic groups are unsupported.', 422, { details: { savedCostCenterId: readyCostCenter.id } }))
    const saved = vi.fn()
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={saved} />)
    await enterCenter(user)
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    expect(await screen.findByText('Dynamic groups are unsupported.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Create cost center' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Inspect saved cost center' }))
    expect(saved).toHaveBeenCalledWith(readyCostCenter.id)
    expect(api.createCostCenter).toHaveBeenCalledOnce()
  })

  it('never retries a create after an unrecorded network outcome', async () => {
    const user = userEvent.setup()
    api.createCostCenter.mockRejectedValue(new Error('Connection interrupted'))
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    await enterCenter(user)
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    expect(await screen.findByText(/Creation outcome is unknown/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Create cost center' })).toBeDisabled()
    expect(api.createCostCenter).toHaveBeenCalledOnce()
  })

  it('does not resubmit group creation from an existing saved center after a network timeout', async () => {
    const user = userEvent.setup()
    const saved = vi.fn()
    api.updateCostCenter.mockRejectedValue(new Error('Connection interrupted'))
    mount(<CostCenterEditor costCenter={{ ...readyCostCenter, setupState: 'setupRequired', entraBinding: null }} onCancel={vi.fn()} onSaved={saved} />)
    await user.click(await screen.findByRole('radio', { name: 'Create with MOSAIC' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText(/Group creation outcome is unknown/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Inspect cost-center records' }))
    expect(saved).toHaveBeenCalledWith(readyCostCenter.id)
    expect(api.updateCostCenter).toHaveBeenCalledOnce()
  })

  it('surfaces invalid owner contacts without assigning group ownership', async () => {
    const user = userEvent.setup()
    mount(<CostCenterEditor onCancel={vi.fn()} onSaved={vi.fn()} />)
    await enterCenter(user)
    await user.type(screen.getByLabelText('Owners'), 'not-an-email')
    expect(screen.getByText('Enter up to 20 valid contact email addresses.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Create cost center' })).toBeDisabled()
    expect(api.createCostCenter).not.toHaveBeenCalled()
  })

  it('does not exempt a custom center merely because its name is General', async () => {
    mount(<CostCenterEditor costCenter={{ ...readyCostCenter, name: 'General', setupState: 'setupRequired', entraBinding: null }} onCancel={vi.fn()} onSaved={vi.fn()} />)
    expect(await screen.findByText('Setup required')).toBeVisible()
    expect(screen.getByRole('radio', { name: 'Attach existing group' })).toBeInTheDocument()
    expect(screen.queryByText(/General is exempt by its stable built-in identity/)).not.toBeInTheDocument()
  })

  it('keeps verified binding usable without membership-write consent and offers no rebinding or deletion', async () => {
    api.getCostCenterGroupCapabilities.mockResolvedValue({ ...centerCapabilities, canCreateGroups: false, canAddUsers: false, canAddServicePrincipals: false, canRemoveMembers: false })
    mount(<CostCenterEditor costCenter={readyCostCenter} onCancel={vi.fn()} onSaved={vi.fn()} />)
    expect(await screen.findByText('Verified dedicated group')).toBeVisible()
    expect(screen.queryByRole('radio', { name: 'Attach existing group' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Delete.*group/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled()
  })
})

describe('directory members and durable operations', () => {
  it.each(['object ID', 'search text', 'identity type'].flatMap((input) =>
    ['success', 'unsupported', 'error'].map((outcome) => ({ input, outcome })),
  ))('ignores stale member verification $outcome after editing the $input', async ({ input, outcome }) => {
    const user = userEvent.setup()
    let finish!: (value: DirectoryObject) => void
    let fail!: (reason: Error) => void
    const pending = new Promise<DirectoryObject>((resolve, reject) => { finish = resolve; fail = reject })
    api.getDirectoryObject.mockReturnValueOnce(pending)
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    await waitFor(() => expect(screen.getByLabelText('Find directory member')).toBeEnabled())
    await verifyIdentity(user, 'Member object ID', person.objectId)
    if (input === 'object ID') await user.type(screen.getByLabelText('Member object ID'), '0')
    else if (input === 'search text') await user.type(screen.getByLabelText('Find directory member'), 'Other')
    else await user.selectOptions(screen.getByLabelText('Directory identity type'), 'application')
    await act(async () => {
      if (outcome === 'error') fail(new Error('Earlier verification failed'))
      else finish(outcome === 'unsupported' ? group : person)
      await pending.catch(() => undefined)
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Verify directory identity' })).toBeEnabled())
    expect(screen.queryByText(/Selected:/)).not.toBeInTheDocument()
    expect(screen.queryByText('Identity verification failed')).not.toBeInTheDocument()
    expect(screen.queryByText(/Unsupported identity type/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Add to Entra group' })).toBeDisabled()
    expect(api.addCostCenterDirectoryMember).not.toHaveBeenCalled()
  })

  it.each(['success', 'unsupported', 'error'] as const)('ignores a stale verification %s after selecting another member', async (outcome) => {
    const user = userEvent.setup()
    let finish!: (value: DirectoryObject) => void
    let fail!: (reason: Error) => void
    const pending = new Promise<DirectoryObject>((resolve, reject) => { finish = resolve; fail = reject })
    const previous = { ...person, objectId: '77777777-7777-4777-8777-777777777777', displayName: 'Earlier fictional analyst' }
    api.getDirectoryObject.mockReturnValueOnce(pending)
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    const search = screen.getByLabelText('Find directory member')
    await waitFor(() => expect(search).toBeEnabled())
    await verifyIdentity(user, 'Member object ID', previous.objectId)
    await user.type(search, 'Analyst')
    await user.click(within(await screen.findByRole('list', { name: 'Directory member results' })).getByRole('button'))
    await act(async () => {
      if (outcome === 'error') fail(new Error('Earlier verification failed'))
      else finish(outcome === 'unsupported' ? group : previous)
      await pending.catch(() => undefined)
    })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Verify directory identity' })).toBeEnabled())
    expect(screen.getByText(/Selected:/)).toHaveTextContent('Fictional analyst')
    expect(screen.queryByText('Identity verification failed')).not.toBeInTheDocument()
    expect(screen.queryByText(/Unsupported identity type/)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Add to Entra group' }))
    await waitFor(() => expect(api.addCostCenterDirectoryMember).toHaveBeenCalledWith(readyCostCenter.id, memberObjectId, 'user', expect.any(String)))
  })

  it('pages direct users and workloads without a local roster, and restarts paging explicitly on refresh', async () => {
    const user = userEvent.setup()
    api.listCostCenterDirectoryMembers.mockImplementation(async (_id, options) => options.cursor
      ? { ...directoryMemberPage, members: [{ ...directoryMemberPage.members[1], displayName: 'Page two workload' }], nextCursor: null }
      : { ...directoryMemberPage, nextCursor: 'opaque+cursor/==' })
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    expect(await screen.findByText('Fictional workload')).toBeVisible()
    expect(screen.getByText(/Graph beta preview includes service principals/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Remove Nested team' })).toBeDisabled()
    expect(screen.getByText(/Nested groups and unknown directory types are read-only/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Next member page' }))
    expect(await screen.findByText('Page two workload')).toBeVisible()
    expect(api.listCostCenterDirectoryMembers).toHaveBeenLastCalledWith(readyCostCenter.id, { pageSize: 100, cursor: 'opaque+cursor/==' })
    await user.click(screen.getByRole('button', { name: 'Refresh members from first page' }))
    expect(await screen.findByText('Fictional analyst')).toBeVisible()
    expect(api.listCostCenterDirectoryMembers).toHaveBeenLastCalledWith(readyCostCenter.id, { pageSize: 100 })
    expect(api.addCostCenterMember).not.toHaveBeenCalled()
  })

  it('surfaces member-list failures rather than presenting an empty healthy roster', async () => {
    api.listCostCenterDirectoryMembers.mockRejectedValue(new ApiError('Group read denied', 403))
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    expect(await screen.findByText('Group read denied')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Retry this member page' })).toBeVisible()
    expect(screen.queryByText(/No direct members on this page/)).not.toBeInTheDocument()
  })

  it('blocks new membership writes until durable operation history is available', async () => {
    const user = userEvent.setup()
    api.listCostCenterDirectoryOperations.mockRejectedValue(new Error('History read denied'))
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    expect(await screen.findByText(/New membership writes are disabled until it can be refreshed/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Remove Fictional analyst' })).toBeDisabled()
    expect(screen.getByLabelText('Find directory member')).toBeDisabled()
    api.listCostCenterDirectoryOperations.mockResolvedValue([])
    await user.click(screen.getByRole('button', { name: 'Refresh operation history before editing' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove Fictional analyst' })).toBeEnabled())
    expect(api.addCostCenterDirectoryMember).not.toHaveBeenCalled()
  })

  it('reuses one idempotency key after an interrupted add and never creates per-member grants', async () => {
    const user = userEvent.setup()
    api.addCostCenterDirectoryMember.mockRejectedValueOnce(new Error('Response lost')).mockResolvedValueOnce(memberOperation)
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    const search = await screen.findByLabelText('Find directory member')
    await waitFor(() => expect(search).toBeEnabled())
    await user.type(search, 'Analyst')
    await user.click(within(await screen.findByRole('list', { name: 'Directory member results' })).getByRole('button'))
    await user.click(screen.getByRole('button', { name: 'Add to Entra group' }))
    await user.click(await screen.findByRole('button', { name: 'Retrieve same member operation' }))
    await waitFor(() => expect(api.addCostCenterDirectoryMember).toHaveBeenCalledTimes(2))
    const [first, second] = api.addCostCenterDirectoryMember.mock.calls
    expect(first).toEqual(second)
    expect(first.slice(0, 3)).toEqual([readyCostCenter.id, memberObjectId, 'user'])
    expect(first[3]).toMatch(/^[0-9a-f-]{36}$/)
    expect(api.createEntitlement).not.toHaveBeenCalled()
    expect(api.createPrincipal).not.toHaveBeenCalled()
    expect(await screen.findByText(/Directory updated. Propagation and token refresh remain pending/)).toBeVisible()
  })

  it('shows token/key consequences on removal and reuses the recorded removal key after a network error', async () => {
    const user = userEvent.setup()
    const unknown = { ...memberOperation, kind: 'removeMember', state: 'unknown', directoryUpdated: false, errorMessage: 'Outcome unconfirmed' }
    api.removeCostCenterDirectoryMember.mockRejectedValueOnce(new Error('Response lost')).mockResolvedValueOnce(unknown)
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    await user.click(await screen.findByRole('button', { name: 'Remove Fictional analyst' }))
    const dialog = screen.getByRole('dialog', { name: 'Remove real Entra membership' })
    expect(within(dialog).getByText(/60–90 minutes from issuance, not from removal/)).toBeVisible()
    expect(within(dialog).getByText(/around 24 hours/)).toBeVisible()
    expect(within(dialog).getByText(/Already copied individual subscription keys are independent/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Remove from Entra group' }))
    api.listCostCenterDirectoryOperations.mockResolvedValue([unknown])
    await user.click(await within(dialog).findByRole('button', { name: 'Retrieve same removal operation' }))
    await waitFor(() => expect(api.removeCostCenterDirectoryMember).toHaveBeenCalledTimes(2))
    expect(api.removeCostCenterDirectoryMember.mock.calls[0]).toEqual(api.removeCostCenterDirectoryMember.mock.calls[1])
    expect(await screen.findByText(/Directory change not confirmed: unknown/)).toBeVisible()
    expect(screen.queryByText('Directory updated')).not.toBeInTheDocument()
  })

  it('keeps a closed, interrupted removal explicitly separate from a later fresh removal', async () => {
    const user = userEvent.setup()
    api.removeCostCenterDirectoryMember.mockRejectedValueOnce(new Error('Response lost')).mockResolvedValueOnce({ ...memberOperation, kind: 'removeMember' })
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    await user.click(await screen.findByRole('button', { name: 'Remove Fictional analyst' }))
    await user.click(screen.getByRole('button', { name: 'Remove from Entra group' }))
    await screen.findByText('Response lost')
    await user.click(screen.getByRole('button', { name: 'Close without another write' }))
    await user.click(await screen.findByRole('button', { name: 'Retrieve prior removal for Fictional analyst' }))
    expect(screen.getByText(/Continue only the original removal intent/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Retrieve same removal operation' }))
    await waitFor(() => expect(api.removeCostCenterDirectoryMember).toHaveBeenCalledTimes(2))
    expect(api.removeCostCenterDirectoryMember.mock.calls[0]).toEqual(api.removeCostCenterDirectoryMember.mock.calls[1])
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove Fictional analyst' })).toBeEnabled())
  })

  it('shows verified groups as read-only when user and workload write consent are absent', async () => {
    api.getCostCenterGroupCapabilities.mockResolvedValue({ ...centerCapabilities, canAddUsers: false, canAddServicePrincipals: false, canRemoveMembers: false })
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    expect(await screen.findByText(/Membership is read-only with the observed permissions/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Remove Fictional workload' })).toBeDisabled()
    expect(api.addCostCenterMember).not.toHaveBeenCalled()
  })

  it('uses a fresh removal key after recovered success and a later explicit re-add', async () => {
    const user = userEvent.setup()
    const unknown = { ...memberOperation, kind: 'removeMember' as const, state: 'unknown' as const, directoryUpdated: false }
    const recovered = { ...unknown, state: 'succeeded' as const, directoryUpdated: true }
    const readded = { ...memberOperation, id: 'later-add' }
    api.removeCostCenterDirectoryMember.mockImplementationOnce(async () => {
      api.listCostCenterDirectoryOperations.mockResolvedValue([unknown])
      return unknown
    }).mockResolvedValueOnce({ ...recovered, id: 'later-remove' })
    api.recoverCostCenterDirectoryOperation.mockImplementation(async () => {
      api.listCostCenterDirectoryOperations.mockResolvedValue([recovered])
      return recovered
    })
    api.addCostCenterDirectoryMember.mockImplementation(async () => {
      api.listCostCenterDirectoryOperations.mockResolvedValue([readded, recovered])
      return readded
    })
    mount(<CostCenterDirectoryMembers costCenter={readyCostCenter} onSetup={vi.fn()} />)
    const removeButton = await screen.findByRole('button', { name: 'Remove Fictional analyst' })
    await waitFor(() => expect(removeButton).toBeEnabled())
    await user.click(removeButton)
    await user.click(screen.getByRole('button', { name: 'Remove from Entra group' }))
    await user.click(await screen.findByRole('button', { name: 'Recover without repeating write' }))
    const search = screen.getByLabelText('Find directory member')
    await waitFor(() => expect(search).toBeEnabled())
    await user.type(search, 'Analyst')
    await user.click(within(await screen.findByRole('list', { name: 'Directory member results' })).getByRole('button'))
    await user.click(screen.getByRole('button', { name: 'Add to Entra group' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Remove Fictional analyst' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Remove Fictional analyst' }))
    await user.click(screen.getByRole('button', { name: 'Remove from Entra group' }))
    await waitFor(() => expect(api.removeCostCenterDirectoryMember).toHaveBeenCalledTimes(2))
    expect(api.removeCostCenterDirectoryMember.mock.calls[0][2]).not.toBe(api.removeCostCenterDirectoryMember.mock.calls[1][2])
    expect(api.recoverCostCenterDirectoryOperation).toHaveBeenCalledWith(readyCostCenter.id, unknown.id)
  })

  it('recovers unknown creation without ever exposing or calling retry-create', async () => {
    const user = userEvent.setup()
    const unknown = { ...memberOperation, kind: 'createGroup', state: 'unknown', directoryUpdated: false, groupObjectId: null, memberObjectId: null, memberKind: null, displayName: 'Research', canRetry: false }
    api.listCostCenterDirectoryOperations.mockResolvedValue([unknown])
    api.recoverCostCenterDirectoryOperation.mockResolvedValue(unknown)
    mount(<CostCenterDirectoryOperations costCenterId={readyCostCenter.id} />)
    expect(await screen.findByText('Unknown outcome')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry recorded failed operation' })).not.toBeInTheDocument()
    expect(screen.getByText(/display-name match is not proof of identity/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Recover without repeating write' }))
    await waitFor(() => expect(api.recoverCostCenterDirectoryOperation).toHaveBeenCalledWith(readyCostCenter.id, memberOperation.id))
    expect(api.retryCostCenterDirectoryOperation).not.toHaveBeenCalled()
    expect(api.createCostCenter).not.toHaveBeenCalled()
  })

  it('distinguishes a failed HTTP-200 operation from success and only exposes a server-authorized safe retry', async () => {
    const user = userEvent.setup()
    const failed = { ...memberOperation, state: 'failed', directoryUpdated: false, canRetry: true, errorCode: 'Authorization_RequestDenied', errorMessage: 'Membership write permission denied', errorDetails: { httpStatus: 403, graphCode: 'Authorization_RequestDenied', requestId: 'graph-request' } }
    const unknown = { ...failed, state: 'unknown', canRetry: false }
    api.listCostCenterDirectoryOperations.mockResolvedValue([failed])
    api.retryCostCenterDirectoryOperation.mockImplementation(async () => { api.listCostCenterDirectoryOperations.mockResolvedValue([unknown]); return unknown })
    mount(<CostCenterDirectoryOperations costCenterId={readyCostCenter.id} />)
    expect(await screen.findByText('Failed · Not updated')).toBeVisible()
    expect(screen.queryByText('Directory updated')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Retry recorded failed operation' }))
    expect(await screen.findByText('Unknown outcome')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry recorded failed operation' })).not.toBeInTheDocument()
  })

  it('requires a new durable observation after a retry response is lost, including failed group creation', async () => {
    const user = userEvent.setup()
    const failed = { ...memberOperation, kind: 'createGroup' as const, state: 'failed' as const, directoryUpdated: false, canRetry: true, memberObjectId: null, memberKind: null }
    const unknown = { ...failed, state: 'unknown' as const, canRetry: false }
    api.listCostCenterDirectoryOperations.mockResolvedValue([failed])
    api.retryCostCenterDirectoryOperation.mockRejectedValue(new Error('Retry response lost'))
    api.getCostCenterDirectoryOperation.mockRejectedValueOnce(new Error('Observation unavailable')).mockImplementation(async () => {
      api.listCostCenterDirectoryOperations.mockResolvedValue([unknown])
      return unknown
    })
    mount(<CostCenterDirectoryOperations costCenterId={readyCostCenter.id} />)
    await user.click(await screen.findByRole('button', { name: 'Retry recorded failed operation' }))
    expect(await screen.findByText('Outcome needs refresh')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry recorded failed operation' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh operation' }))
    expect(await screen.findByText('Observation unavailable')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry recorded failed operation' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Refresh operation' }))
    expect(await screen.findByText('Unknown outcome')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Recover without repeating write' })).toBeVisible()
    expect(api.retryCostCenterDirectoryOperation).toHaveBeenCalledOnce()
    expect(api.createCostCenter).not.toHaveBeenCalled()
  })
})

describe('exact resource modes and allowance', () => {
  it('preserves both IDs for equal model IDs in different pools and does not change saved limits', async () => {
    const user = userEvent.setup()
    const first = { ...governedAnthropicPool, id: 'pool-one', displayName: 'First pool' }
    const second = { ...governedAnthropicPool, id: 'pool-two', displayName: 'Second pool' }
    api.listModelPools.mockResolvedValue([first, second])
    const definitions: CostCenterResourceAccess[] = [
      { resource: { kind: 'poolModel', id: first.models[0].id, scopeId: first.id }, mode: 'automatic' },
      { resource: { kind: 'poolModel', id: second.models[0].id, scopeId: second.id }, mode: 'requestOnly' },
    ]
    const center = { ...readyCostCenter, resourceAccess: definitions, limits: [{
      resource: definitions[1].resource, person: null, pool: { monthlyTokens: 123000, monthlyCalls: 4000 },
    }] }
    const change = vi.fn()
    function Editor() {
      const [value, setValue] = useState(definitions)
      return <CostCenterResourceAccessFields value={value} costCenter={center} onChange={(next) => { change(next); setValue(next) }} />
    }
    mount(<Editor />)
    await user.click(await screen.findByRole('button', { name: /Second pool/ }))
    expect(screen.getByText('123,000 pooled tokens/month · 4,000 pooled calls/month')).toBeVisible()
    await user.selectOptions(screen.getByRole('combobox', { name: /Access mode for/ }), 'notAvailable')
    expect(change).toHaveBeenLastCalledWith([definitions[0], { resource: definitions[1].resource, mode: 'notAvailable' }])
    expect(center.limits[0].pool.monthlyTokens).toBe(123000)
    expect(screen.getByText('pool-two')).toBeVisible()
  })

  it('disables Automatic for caller-token-incompatible models without treating backend key auth as incompatibility', async () => {
    const user = userEvent.setup()
    api.listModelApis.mockResolvedValue([publishedModelApi])
    api.listPublications.mockResolvedValue([{ ...published, governedAccess: { keysEnabled: true, entraEnabled: false }, backendKeyName: 'key-backed-backend' }])
    mount(<CostCenterResourceAccessFields value={[]} onChange={vi.fn()} />)
    await user.click(within(await screen.findByRole('list', { name: 'Exact resource offerings' })).getByRole('button'))
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'Request only' })).toBeEnabled()
    expect(screen.getByText(/Backend API-key authentication is independent/)).toBeVisible()
    expect(screen.getByRole('combobox', { name: /Access mode for/ })).toHaveValue('notAvailable')
  })

  it('shows the one managed group grant, limits and unapplied state for an exact token-enabled resource', async () => {
    const user = userEvent.setup()
    api.listModelApis.mockResolvedValue([publishedModelApi])
    api.listPublications.mockResolvedValue([{ ...published, backendKeyName: 'key-backed-backend', accessState: 'pending' }])
    api.listEntitlements.mockResolvedValue([{
      id: 'managed-group-grant', managedBy: 'costCenterAutomatic', costCenterId: readyCostCenter.id,
      resource: { kind: 'modelApi', id: publishedModelApi.id }, subject: { kind: 'securityGroup', id: 'primary-group' },
      enabled: true, runtime: { publicationId: published.id, status: 'pending' },
    }])
    mount(<CostCenterResourceAccessFields value={[{ resource: { kind: 'modelApi', id: publishedModelApi.id }, mode: 'automatic' }]} costCenter={readyCostCenter} onChange={vi.fn()} />)
    await user.click(within(await screen.findByRole('list', { name: 'Exact resource offerings' })).getByRole('button'))
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeEnabled()
    expect(await screen.findByText('Saved, not applied')).toBeVisible()
    expect(screen.getByText('Managed automatic group grant')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Review resource configuration and plan/apply' })).toHaveAttribute('href', '/entitlements?tab=settings&selected=pub_1')
    expect(screen.queryByRole('button', { name: 'Edit grant' })).not.toBeInTheDocument()
  })

  it('blocks Government resources for every custom-center mode', async () => {
    const user = userEvent.setup()
    api.listMcpServers.mockResolvedValue([{ id: 'mcp-1', displayName: 'Fictional tools', apiName: published.apiName, gatewayId: 'gov-gateway', publicationId: 'mcp-publication' }])
    api.listMcpPublications.mockResolvedValue([{ ...published, id: 'mcp-publication', mcpServerId: 'mcp-1', gatewayId: 'gov-gateway', governedAccess: undefined }])
    api.listGateways.mockResolvedValue([{ id: 'gov-gateway', azureCloud: 'government' }])
    mount(<CostCenterResourceAccessFields value={[]} onChange={vi.fn()} />)
    await user.click(within(await screen.findByRole('list', { name: 'Exact resource offerings' })).getByRole('button'))
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'Request only' })).toBeDisabled()
    expect(screen.getByText('Government resources are unavailable in this workflow.')).toBeVisible()
  })

  it('supports commercial published MCP caller tokens without an invented auth toggle or backend-key restriction', async () => {
    const user = userEvent.setup()
    api.listMcpServers.mockResolvedValue([{ id: 'mcp-1', displayName: 'Fictional tools', apiName: published.apiName, gatewayId: published.gatewayId, publicationId: 'mcp-publication' }])
    api.listMcpPublications.mockResolvedValue([{ ...published, id: 'mcp-publication', mcpServerId: 'mcp-1', governedAccess: undefined, backendAuthMode: 'apiKey' }])
    mount(<CostCenterResourceAccessFields value={[]} onChange={vi.fn()} />)
    await user.click(await screen.findByRole('button', { name: /Fictional tools/ }))
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeEnabled()
    expect(screen.getByRole('option', { name: 'Request only' })).toBeEnabled()
  })

  it('does not infer publication linkage for an adopted model API', async () => {
    const user = userEvent.setup()
    api.listModelApis.mockResolvedValue([{ ...publishedModelApi, publicationId: null }])
    api.listPublications.mockResolvedValue([published])
    mount(<CostCenterResourceAccessFields value={[]} onChange={vi.fn()} />)
    await user.click(await screen.findByRole('button', { name: /Chat completions/ }))
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'Request only' })).toBeDisabled()
    expect(screen.getByText('Requires an available MOSAIC-published model API.')).toBeVisible()
  })

  it('does not offer an unapplied pool model, and retains missing saved definitions with their scopes', async () => {
    const user = userEvent.setup()
    const definitions: CostCenterResourceAccess[] = [{ resource: { kind: 'poolModel', id: 'removed-model', scopeId: 'removed-pool' }, mode: 'requestOnly' }]
    api.listModelPools.mockResolvedValue([{ ...governedAnthropicPool, appliedModelIds: [] }])
    const change = vi.fn()
    mount(<CostCenterResourceAccessFields value={definitions} onChange={change} />)
    await user.click(await screen.findByRole('button', { name: /Pool model · Anthropic Claude/ }))
    expect(screen.getByRole('option', { name: 'Automatic' })).toBeDisabled()
    expect(screen.getByRole('option', { name: 'Request only' })).toBeDisabled()
    expect(screen.getByText('This exact pool model must be present in the last applied pool offering.')).toBeVisible()
    await user.click(screen.getByRole('button', { name: /Unavailable poolModel/ }))
    expect(screen.getByText('removed-pool')).toBeVisible()
    expect(screen.getByRole('combobox', { name: /Access mode for/ })).toHaveValue('requestOnly')
    expect(change).not.toHaveBeenCalled()
  })
})

describe('cost-center approval safety', () => {
  it('reuses shared setup and defaults requester enrollment off', async () => {
    const user = userEvent.setup()
    mount(<CostCenterRequestReview request={costCenterCreationRequest} requester="Fictional analyst" onBack={vi.fn()} />)
    expect(screen.getByRole('checkbox', { name: 'Add requester to the Entra group' })).not.toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Approve cost-center request' }))
    await waitFor(() => expect(api.approveCostCenterRequest).toHaveBeenCalledWith(costCenterCreationRequest.id, {
      costCenter: { name: 'Research', code: 'research', description: 'Fictional research center', owners: [], keysAllowed: true, resourceAccess: [] },
      addRequester: false, note: null,
    }))
    expect(api.addCostCenterDirectoryMember).not.toHaveBeenCalled()
    expect(api.createCostCenter).not.toHaveBeenCalled()
  })

  it('requires explicit use or ignore of a group suggestion and still requires adoption acknowledgment', async () => {
    const user = userEvent.setup()
    mount(<CostCenterRequestReview request={{ ...costCenterCreationRequest, suggestedGroupObjectId: centerGroupObjectId }} requester="Fictional analyst" onBack={vi.fn()} />)
    expect(screen.getByRole('button', { name: 'Approve cost-center request' })).toBeDisabled()
    await user.click(screen.getByRole('radio', { name: 'Use suggested group after verification' }))
    await user.click(await screen.findByRole('button', { name: 'Select suggested identity: Fictional Research' }))
    expect(screen.getByRole('button', { name: 'Approve cost-center request' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /I acknowledge that membership edits/ }))
    await user.click(screen.getByRole('checkbox', { name: 'Add requester to the Entra group' }))
    await user.click(screen.getByRole('button', { name: 'Approve cost-center request' }))
    await waitFor(() => expect(api.approveCostCenterRequest).toHaveBeenCalledWith(costCenterCreationRequest.id, expect.objectContaining({
      costCenter: expect.objectContaining({ groupSetup: { mode: 'attach', groupObjectId: centerGroupObjectId, acknowledgeMembershipChanges: true } }),
      addRequester: true,
    })))
  })

  it('explicitly ignores a suggestion when switching from adoption to another setup mode', async () => {
    const user = userEvent.setup()
    mount(<CostCenterRequestReview request={{ ...costCenterCreationRequest, suggestedGroupObjectId: centerGroupObjectId }} requester="Fictional analyst" onBack={vi.fn()} />)
    await user.click(screen.getByRole('radio', { name: 'Use suggested group after verification' }))
    await user.click(await screen.findByRole('button', { name: 'Select suggested identity: Fictional Research' }))
    await user.click(screen.getByRole('radio', { name: 'Finish setup later' }))
    expect(screen.getByRole('radio', { name: 'Ignore suggestion and choose setup below' })).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Approve cost-center request' }))
    await waitFor(() => expect(api.approveCostCenterRequest).toHaveBeenCalled())
    expect(api.approveCostCenterRequest.mock.calls[0][1].costCenter).not.toHaveProperty('groupSetup')
  })

  it('locks reserved configuration and resumes the exact original payload including a successful requester-add choice', async () => {
    const user = userEvent.setup()
    const approval = {
      costCenter: { name: 'Reviewed research', code: 'reviewed', groupSetup: { mode: 'create' as const, displayName: 'Reviewed group' }, resourceAccess: [] },
      addRequester: true, note: 'Original note', costCenterId: readyCostCenter.id, decidedByObjectId: 'admin', startedAt: '2026-11-01T10:00:00Z',
    }
    const request: AdminCostCenterRequest = { ...costCenterCreationRequest, approval, state: 'approved', setupState: 'setupRequired', needsAdministratorAttention: true, requesterMembershipState: 'succeeded' }
    mount(<CostCenterRequestReview request={request} requester="Fictional analyst" onBack={vi.fn()} />)
    expect(screen.getByLabelText('Name')).toHaveValue('Reviewed research')
    expect(screen.getByLabelText('Name')).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'Add requester to the Entra group' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Add requester to the Entra group' })).toBeDisabled()
    expect(screen.getByText('Recorded group creation configuration')).toBeVisible()
    expect(screen.queryByText('Creation preview · no directory write yet')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Resume unchanged approval' }))
    await waitFor(() => expect(api.approveCostCenterRequest).toHaveBeenCalledWith(request.id, {
      costCenter: approval.costCenter, addRequester: true, note: 'Original note',
    }))
    expect(api.addCostCenterDirectoryMember).not.toHaveBeenCalled()
  })

  it('retains the first approval payload on a transport retry instead of making a second center', async () => {
    const user = userEvent.setup()
    api.approveCostCenterRequest.mockRejectedValueOnce(new Error('Connection interrupted')).mockResolvedValueOnce(costCenterCreationRequest)
    mount(<CostCenterRequestReview request={costCenterCreationRequest} requester="Fictional analyst" onBack={vi.fn()} />)
    await user.click(screen.getByRole('checkbox', { name: 'Add requester to the Entra group' }))
    await user.click(screen.getByRole('button', { name: 'Approve cost-center request' }))
    await user.click(await screen.findByRole('button', { name: 'Resume unchanged approval' }))
    await waitFor(() => expect(api.approveCostCenterRequest).toHaveBeenCalledTimes(2))
    expect(api.approveCostCenterRequest.mock.calls[0]).toEqual(api.approveCostCenterRequest.mock.calls[1])
    expect(screen.getByLabelText('Name')).toBeDisabled()
  })

  it('shows approved setup/membership failures as partial outcomes', () => {
    const partial: AdminCostCenterRequest = {
      ...costCenterCreationRequest, state: 'approved', setupState: 'setupRequired', needsAdministratorAttention: true,
      costCenter: { id: readyCostCenter.id, name: 'Research', code: 'research' },
      preparationError: 'Consent not granted', requesterMembershipState: 'unknown', requesterMembershipError: 'Membership timeout',
    }
    mount(<CostCenterRequestSummary request={partial} requester="Fictional analyst" />)
    expect(screen.getByText('Administrator action needed')).toBeVisible()
    expect(screen.getByText(/Approval is saved, but setup is incomplete/)).toBeVisible()
    expect(screen.getByText(/Do not repeat requester enrollment/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open saved center: Research' })).toHaveAttribute('href', `/cost-centers/${readyCostCenter.id}`)
  })

  it.each(['denied', 'withdrawn'] as const)('preserves a %s preparation record without offering approval or deleting its local center', (state) => {
    const cancelled: AdminCostCenterRequest = {
      ...costCenterCreationRequest, state,
      approval: {
        costCenter: { name: 'Reviewed research', code: 'reviewed' }, addRequester: false,
        costCenterId: readyCostCenter.id, decidedByObjectId: 'admin', startedAt: '2026-11-01T10:00:00Z',
      },
    }
    mount(<CostCenterRequestSummary request={cancelled} requester="Fictional analyst" onReview={vi.fn()} />)
    expect(screen.getByText(/A reserved or saved unbound local center may remain/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Inspect reserved cost-center record' })).toHaveAttribute('href', `/cost-centers/${readyCostCenter.id}`)
    expect(screen.queryByRole('button', { name: /approval|Review cost-center/ })).not.toBeInTheDocument()
  })
})
