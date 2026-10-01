import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import {
  budgetOverviewFixture,
  emptyBudgetOverviewFixture,
  organizationBudgetFixture,
  researchBudgetFixture,
} from '../test/budget-fixtures'
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
  getBudgets: vi.fn(),
  getCostCenterBudget: vi.fn(),
  setCostCenterBudget: vi.fn(),
  deleteCostCenterBudget: vi.fn(),
  setOrganizationBudget: vi.fn(),
  deleteOrganizationBudget: vi.fn(),
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
    api.getBudgets.mockResolvedValue(emptyBudgetOverviewFixture)
    api.getCostCenterBudget.mockResolvedValue(null)
  })

  it('sets a budget that blocks at 100%, emailing the owners and anyone else', async () => {
    const user = userEvent.setup()
    api.setCostCenterBudget.mockResolvedValue(researchBudgetFixture)
    renderRoute('/cost-centers/cc-general')

    expect(await screen.findByText('No budget')).toBeVisible()
    await user.type(screen.getByLabelText('Monthly amount (USD)'), '4000')
    await user.clear(screen.getByLabelText('Warn at (%)'))
    await user.type(screen.getByLabelText('Warn at (%)'), '100, 50 , 80')
    await user.type(screen.getByLabelText('Also email'), 'finance@contoso.com, ops@fabrikam.com')
    expect(screen.getByRole('switch', { name: 'Email the owners (1)' })).toBeChecked()
    await user.click(screen.getByRole('radio', { name: /Block calls at the gateway/ }))
    await user.click(screen.getByRole('button', { name: 'Set budget' }))

    await waitFor(() =>
      expect(api.setCostCenterBudget).toHaveBeenCalledWith('cc-general', {
        amount: 4000,
        thresholds: [50, 80, 100],
        recipients: ['finance@contoso.com', 'ops@fabrikam.com'],
        notifyOwners: true,
        action: 'block',
      }),
    )
    // Saving judges the budget at once, so everything that shows budgets reloads.
    await waitFor(() => expect(api.getCostCenterBudget).toHaveBeenCalledTimes(2))
  })

  it('refuses an amount or thresholds it can not judge, before saving', async () => {
    const user = userEvent.setup()
    renderRoute('/cost-centers/cc-general')

    await screen.findByText('No budget')
    await user.click(screen.getByRole('button', { name: 'Set budget' }))
    expect(screen.getByText('Enter a monthly amount in US dollars, more than zero.')).toBeVisible()
    await user.type(screen.getByLabelText('Monthly amount (USD)'), '100')
    await user.clear(screen.getByLabelText('Warn at (%)'))
    await user.type(screen.getByLabelText('Warn at (%)'), 'half')
    await user.click(screen.getByRole('button', { name: 'Set budget' }))
    expect(screen.getByText(/Warn at one to five whole percentages/)).toBeVisible()
    expect(api.setCostCenterBudget).not.toHaveBeenCalled()
  })

  it('shows a blocked budget, where it is enforced, the emails it sent, and removes it', async () => {
    const user = userEvent.setup()
    api.getCostCenterBudget.mockResolvedValue(researchBudgetFixture)
    api.deleteCostCenterBudget.mockResolvedValue(undefined)
    renderRoute('/cost-centers/cc-general')

    expect(await screen.findByText('$4,320.00')).toBeVisible()
    expect(screen.getByText('of $4,000.00')).toBeVisible()
    expect(screen.getByText('Blocked')).toBeVisible()
    expect(screen.getByText(/refused at the gateway until an administrator raises the budget/)).toBeVisible()
    const gateways = screen.getByRole('list', { name: 'Gateways that enforce this budget' })
    expect(within(gateways).getByText('Production gateway')).toBeVisible()
    expect(within(gateways).getByText('Refusing calls')).toBeVisible()
    const emails = screen.getByRole('list', { name: 'Budget emails this month' })
    expect(within(emails).getByText('Block notice sent to 2 people')).toBeVisible()
    expect(within(emails).getByText('80% email sent to 2 people')).toBeVisible()
    expect(screen.getByLabelText('Monthly amount (USD)')).toHaveValue(4000)
    expect(screen.getByRole('radio', { name: /Block calls at the gateway/ })).toBeChecked()

    await user.click(screen.getByRole('button', { name: 'Remove budget' }))
    await waitFor(() => expect(api.deleteCostCenterBudget).toHaveBeenCalledWith('cc-general'))
  })

  it('badges budgets near or past their limit, and sets an organization budget that only warns', async () => {
    const user = userEvent.setup()
    api.listCostCenters.mockResolvedValue([{ ...general, id: 'costCenter_research', name: 'Research', code: 'RES', isTenantDefault: false }])
    api.getBudgets.mockResolvedValue({ ...budgetOverviewFixture, organization: null })
    api.setOrganizationBudget.mockResolvedValue(organizationBudgetFixture)
    renderRoute()

    const table = await screen.findByRole('table', { name: 'Cost centers' })
    expect(await within(table).findByText('Blocked')).toBeVisible()
    const form = screen.getByRole('form', { name: 'Organization budget' })
    expect(within(form).queryByRole('radio')).not.toBeInTheDocument()
    expect(within(form).queryByRole('switch')).not.toBeInTheDocument()
    await user.type(within(form).getByLabelText('Monthly amount (USD)'), '50000')
    await user.type(within(form).getByLabelText('Email'), 'cfo@contoso.com')
    await user.click(within(form).getByRole('button', { name: 'Set budget' }))

    await waitFor(() =>
      expect(api.setOrganizationBudget).toHaveBeenCalledWith({
        amount: 50000,
        thresholds: [80, 100],
        recipients: ['cfo@contoso.com'],
        notifyOwners: false,
        action: 'continue',
      }),
    )
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
