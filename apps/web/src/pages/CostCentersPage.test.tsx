import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import type { CostCenter, Principal } from '../types'
import { CostCenterDetailPage, CostCentersPage } from './CostCentersPage'

const general: CostCenter = {
  id: 'cc-general',
  tenantId: 'tenant',
  entityType: 'costCenter',
  name: 'General',
  code: 'general',
  description: null,
  owners: ['owner@example.com'],
  members: [],
  keysAllowed: true,
  limits: [],
  builtIn: true,
  createdAt: '',
  updatedAt: '',
  isTenantDefault: true,
  memberDetails: [],
  grantCount: 2,
  enabledGrantCount: 1,
  defaultFor: 1,
}

const principal: Principal = {
  id: 'principal-1',
  tenantId: 'tenant',
  objectId: 'object-1',
  kind: 'user',
  label: 'Ada Lovelace',
  createdAt: '',
  updatedAt: '',
}

const api = {
  listCostCenters: vi.fn(),
  createCostCenter: vi.fn(),
  getCostCenter: vi.fn(),
  updateCostCenter: vi.fn(),
  deleteCostCenter: vi.fn(),
  addCostCenterMember: vi.fn(),
  removeCostCenterMember: vi.fn(),
  updateCostCenterLimits: vi.fn(),
  recheckCostCenter: vi.fn(),
  getCostCenterSettings: vi.fn(),
  updateCostCenterSettings: vi.fn(),
  listPrincipals: vi.fn(),
  listModelApis: vi.fn(),
  listMcpServers: vi.fn(),
}

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})

function renderRoute(initial = '/cost-centers') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/cost-centers" element={<CostCentersPage />} />
          <Route path="/cost-centers/:costCenterId" element={<CostCenterDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('Cost centers pages', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.listCostCenters.mockResolvedValue([general])
    api.getCostCenter.mockResolvedValue(general)
    api.getCostCenterSettings.mockResolvedValue({ id: 'settings', tenantId: 'tenant', defaultCostCenterId: general.id })
    api.listPrincipals.mockResolvedValue([principal])
    api.listModelApis.mockResolvedValue([{ id: 'model-1', displayName: 'Chat' }])
    api.listMcpServers.mockResolvedValue([{ id: 'mcp-1', displayName: 'Ticket tools' }])
    api.createCostCenter.mockResolvedValue(general)
    api.updateCostCenterSettings.mockResolvedValue({ id: 'settings', tenantId: 'tenant', defaultCostCenterId: general.id })
    api.addCostCenterMember.mockResolvedValue(general)
    api.removeCostCenterMember.mockResolvedValue(general)
    api.updateCostCenterLimits.mockResolvedValue(general)
  })

  it('lists cost centers and validates codes before creating', async () => {
    const user = userEvent.setup()
    renderRoute()

    expect(await screen.findByRole('link', { name: 'General' })).toBeVisible()
    await user.type(screen.getByLabelText('Name'), 'Support')
    await user.type(screen.getByLabelText('Code'), 'bad code')
    expect(screen.getByText('Use only A-Z, a-z, 0-9, dot, underscore, or hyphen.')).toBeVisible()
    await user.clear(screen.getByLabelText('Code'))
    await user.type(screen.getByLabelText('Code'), 'support')
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    await waitFor(() => expect(api.createCostCenter).toHaveBeenCalledWith(expect.objectContaining({ name: 'Support', code: 'support' })))
  })

  it('shows codeInUse and saves the default setting', async () => {
    const user = userEvent.setup()
    api.createCostCenter.mockRejectedValue(new ApiError('conflict', 409, { details: { reason: 'codeInUse' } }))
    renderRoute()

    await screen.findByRole('link', { name: 'General' })
    await user.type(screen.getByLabelText('Name'), 'General two')
    await user.type(screen.getByLabelText('Code'), 'general')
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    expect(await screen.findByText('That code is already used by another cost center. Choose a unique code.')).toBeVisible()
    const listed = api.listCostCenters.mock.calls.length
    await user.click(screen.getByRole('button', { name: 'Save default' }))
    await waitFor(() => expect(api.updateCostCenterSettings).toHaveBeenCalledWith({ defaultCostCenterId: 'cc-general' }))
    // Which cost center is badged as the tenant default changes with it.
    await waitFor(() => expect(api.listCostCenters.mock.calls.length).toBeGreaterThan(listed))
  })

  it('adds members, removes with confirmation copy, saves limits, and shows delete refusal messages', async () => {
    const user = userEvent.setup()
    api.getCostCenter.mockResolvedValue({
      ...general,
      memberDetails: [{ principalId: principal.id, objectId: principal.objectId, label: principal.label ?? null, kind: principal.kind, explicit: true, isDefault: false, addedAt: null }],
    })
    api.deleteCostCenter.mockRejectedValue(new ApiError('conflict', 409, { details: { reason: 'hasGrants' } }))
    renderRoute('/cost-centers/cc-general')

    expect(await screen.findByRole('heading', { name: 'General' })).toBeVisible()
    expect(screen.getByText(/revokes their grants under this cost center/i)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.getAllByText(/the next apply deletes their keys/i).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.selectOptions(screen.getByLabelText('Resource'), 'modelApi:model-1')
    await user.type(screen.getByLabelText('Tokens per minute'), '1000')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))
    await user.click(screen.getByRole('button', { name: 'Save limits' }))
    await waitFor(() => expect(api.updateCostCenterLimits).toHaveBeenCalledWith('cc-general', expect.arrayContaining([expect.objectContaining({ resource: { kind: 'modelApi', id: 'model-1' } })])))
  })

  it('says which grants wait for a recheck, and checks them again', async () => {
    const user = userEvent.setup()
    const pending: CostCenter = {
      ...general,
      pendingRechecks: [
        { id: 'recheck-1', reason: 'memberRemoved', subjectId: null, entitlementIds: ['grant-1', 'grant-2'], requestedAt: '', requestedBy: 'admin' },
      ],
    }
    api.getCostCenter.mockResolvedValueOnce(pending).mockResolvedValue(general)
    api.recheckCostCenter.mockResolvedValue(general)
    renderRoute('/cost-centers/cc-general')

    expect(await screen.findByText(/2 grants under this cost center may no longer be chargeable here/)).toBeVisible()
    expect(screen.getAllByText('Recheck pending').length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Check grants again' }))

    await waitFor(() => expect(api.recheckCostCenter).toHaveBeenCalledWith('cc-general'))
    await waitFor(() => expect(screen.queryByText(/may no longer be chargeable here/)).not.toBeInTheDocument())
  })

  it('marks a cost center whose grants wait for a recheck in the list', async () => {
    api.listCostCenters.mockResolvedValue([
      { ...general, pendingRechecks: [{ id: 'recheck-1', reason: 'tenantDefaultChanged', subjectId: null, entitlementIds: null, requestedAt: '', requestedBy: null }] },
    ])
    renderRoute()

    expect(await screen.findByText('Recheck pending')).toBeVisible()
  })

  it('adds a pool-only limit and refuses an empty limit', async () => {
    const user = userEvent.setup()
    renderRoute('/cost-centers/cc-general')

    expect(await screen.findByRole('heading', { name: 'General' })).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Resource'), 'modelApi:model-1')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))

    expect(screen.getByText('Set per-person limits, a pooled quota, or both.')).toBeVisible()
    expect(screen.queryByText(/Pool:/)).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Monthly pooled tokens'), '40000000')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))
    expect(screen.getByText('Chat (model API) · Pool: 40,000,000 tokens per month')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save limits' }))

    await waitFor(() => expect(api.updateCostCenterLimits).toHaveBeenCalledWith('cc-general', [
      {
        resource: { kind: 'modelApi', id: 'model-1' },
        person: null,
        pool: { monthlyTokens: 40_000_000, monthlyCalls: null },
      },
    ]))
  })
})
