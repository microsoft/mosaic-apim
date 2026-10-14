import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import {
  budgetOverviewFixture,
  emptyBudgetOverviewFixture,
  organizationBudgetFixture,
  researchBudgetFixture,
} from '../test/budget-fixtures'
import { anthropicPool, draftPool } from '../test/pool-fixtures'
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
  listModelPools: vi.fn(),
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
      <RouterProvider router={createMemoryRouter([
        { path: '/cost-centers', element: <><CostCentersPage /><LocationProbe /></> },
        { path: '/cost-centers/:costCenterId', element: <><CostCenterDetailPage /><LocationProbe /></> },
      ], { initialEntries: [initial] })} />
    </QueryClientProvider>,
  )
}

function LocationProbe() {
  const location = useLocation()
  return <output aria-label="Cost center query">{location.search}</output>
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
    api.listModelPools.mockResolvedValue([])
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
    renderRoute('/cost-centers/cc-general?tab=budget')

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
    renderRoute('/cost-centers/cc-general?tab=budget')

    await screen.findByText('No budget')
    await user.click(screen.getByRole('button', { name: 'Set budget' }))
    expect(screen.getByText('Enter a monthly amount in US dollars, at least $0.01.')).toBeVisible()
    // Under a cent rounds to nothing.
    await user.type(screen.getByLabelText('Monthly amount (USD)'), '0.004')
    await user.click(screen.getByRole('button', { name: 'Set budget' }))
    expect(screen.getByText('Enter a monthly amount in US dollars, at least $0.01.')).toBeVisible()
    await user.clear(screen.getByLabelText('Monthly amount (USD)'))
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
    renderRoute('/cost-centers/cc-general?tab=budget')

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

    const list = await screen.findByRole('list', { name: 'Cost centers' })
    expect(await within(list).findByText('Blocked')).toBeVisible()
    expect(screen.queryByRole('form', { name: 'Organization budget' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Organization-wide budget' }))
    expect(screen.queryByRole('list', { name: 'Cost centers' })).not.toBeInTheDocument()
    expect(screen.getByText('Warn-only · Never blocks')).toBeVisible()
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
    api.createCostCenter.mockImplementation(async (payload: Pick<CostCenter, 'name' | 'code' | 'description' | 'owners' | 'keysAllowed'>) => {
      const created = { ...general, ...payload, id: 'cc-support', builtIn: false, isTenantDefault: false, defaultFor: 0, grantCount: 0, enabledGrantCount: 0 }
      api.listCostCenters.mockResolvedValue([general, created])
      api.getCostCenter.mockResolvedValue(created)
      return created
    })
    renderRoute()

    expect(await screen.findByRole('button', { name: /General/ })).toBeVisible()
    expect(screen.queryByRole('textbox', { name: 'Name' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'New cost center' }))
    expect(screen.getByRole('region', { name: 'New cost center' })).toBeVisible()
    expect(screen.queryByRole('list', { name: 'Cost centers' })).not.toBeInTheDocument()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    await user.type(screen.getByLabelText('Name'), 'Support')
    await user.type(screen.getByLabelText('Code'), 'bad code')
    expect(screen.getByText('Use only A-Z, a-z, 0-9, dot, underscore, or hyphen.')).toBeVisible()
    await user.clear(screen.getByLabelText('Code'))
    await user.type(screen.getByLabelText('Code'), 'support')
    await user.type(screen.getByLabelText('Description'), 'Support billing')
    await user.type(screen.getByLabelText('Owners'), 'owner-one@example.com, owner-two@example.com')
    await user.click(screen.getByRole('switch', { name: 'Keys allowed' }))
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    await waitFor(() => expect(api.createCostCenter).toHaveBeenCalledWith({
      name: 'Support', code: 'support', description: 'Support billing',
      owners: ['owner-one@example.com', 'owner-two@example.com'], keysAllowed: false,
    }))
    expect(await screen.findByRole('button', { name: /Support/, pressed: true })).toBeVisible()
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('selected=cc-support')
    expect(screen.queryByRole('region', { name: 'New cost center' })).not.toBeInTheDocument()
  })

  it('shows codeInUse and saves the default setting', async () => {
    const user = userEvent.setup()
    api.createCostCenter.mockRejectedValue(new ApiError('conflict', 409, { details: { reason: 'codeInUse' } }))
    renderRoute()

    await screen.findByRole('button', { name: /General/ })
    await user.click(screen.getByRole('button', { name: 'New cost center' }))
    await user.type(screen.getByLabelText('Name'), 'General two')
    await user.type(screen.getByLabelText('Code'), 'general')
    await user.click(screen.getByRole('button', { name: 'Create cost center' }))
    expect(await screen.findByText('That code is already used by another cost center. Choose a unique code.')).toBeVisible()
    expect(screen.getByLabelText('Name')).toHaveValue('General two')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument())
    await user.click(await screen.findByText('Tenant default', { selector: 'summary' }))
    const listed = api.listCostCenters.mock.calls.length
    await user.click(await screen.findByRole('button', { name: 'Save default' }))
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
    await user.click(screen.getByRole('button', { name: 'Members' }))
    expect(screen.getByText(/revokes their grants under this cost center/i)).toBeVisible()
    expect(screen.getByText(principal.objectId)).not.toBeVisible()
    await user.click(screen.getByText('Object ID', { selector: 'summary' }))
    expect(screen.getByText(principal.objectId)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Remove' }))
    expect(screen.getAllByText(/the next apply deletes their keys/i).length).toBeGreaterThan(0)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument())
    await user.click(await screen.findByRole('button', { name: 'Limits' }))
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

  it('opens persistent cost-center details and edits the complete record in the workspace', async () => {
    const user = userEvent.setup()
    const updated = { ...general, description: 'Support billing', owners: ['finance@example.com'], keysAllowed: false }
    api.updateCostCenter.mockImplementation(async () => {
      api.getCostCenter.mockResolvedValue(updated)
      return updated
    })
    renderRoute('/cost-centers?scope=kept')
    await user.click(await screen.findByRole('button', { name: /General/ }))
    expect(await screen.findByRole('heading', { name: 'General' })).toBeVisible()
    expect(screen.getByRole('list', { name: 'Cost centers' })).toBeVisible()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Open cost center' })).toHaveAttribute('href', '/cost-centers/cc-general')
    await user.click(screen.getByRole('button', { name: 'Edit cost center' }))
    expect(await screen.findByRole('region', { name: 'Edit General' })).toBeVisible()
    expect(screen.queryByRole('list', { name: 'Cost centers' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('Name')).toHaveValue('General')
    expect(screen.getByLabelText('Code')).toHaveValue('general')
    await user.type(screen.getByLabelText('Description'), 'Support billing')
    await user.clear(screen.getByLabelText('Owners'))
    await user.type(screen.getByLabelText('Owners'), 'finance@example.com')
    await user.click(screen.getByRole('switch', { name: 'Keys allowed' }))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.updateCostCenter).toHaveBeenCalledWith('cc-general', {
      name: 'General', code: 'general', description: 'Support billing', owners: ['finance@example.com'], keysAllowed: false,
    }))
    expect(await screen.findByRole('list', { name: 'Cost centers' })).toBeVisible()
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('scope=kept')
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('selected=cc-general')
    expect(screen.getByLabelText('Cost center query')).not.toHaveTextContent('mode=edit')
  })

  it('guards an unsaved New workspace across section changes and restores its draft', async () => {
    const user = userEvent.setup()
    renderRoute('/cost-centers?tab=centers&scope=kept&mode=new')
    const name = await screen.findByLabelText('Name')
    await user.type(name, 'Unsaved center')
    await user.click(screen.getByRole('button', { name: 'Organization-wide budget' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(name).toHaveValue('Unsaved center')
    await waitFor(() => expect(name).toHaveFocus())
    expect(api.createCostCenter).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Organization-wide budget' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument())
    expect(await screen.findByRole('heading', { name: 'Organization-wide budget' })).toBeVisible()
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('scope=kept')
    expect(screen.getByLabelText('Cost center query')).not.toHaveTextContent('mode=new')
  })

  it('restores selected details from the URL and keeps nested tabs scoped to the cost center', async () => {
    const user = userEvent.setup()
    renderRoute('/cost-centers?tab=centers&selected=cc-general&scope=kept')
    expect(await screen.findByRole('heading', { name: 'General' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Members' }))
    expect(await screen.findByRole('table', { name: 'Cost center members' })).toBeVisible()
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('selected=cc-general')
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('tab=centers')
    expect(screen.getByLabelText('Cost center query')).toHaveTextContent('centerTab=members')
    expect(screen.getByRole('list', { name: 'Cost centers' })).toBeVisible()
  })

  it('discards staged limits only after confirmation and returns to saved limits', async () => {
    const user = userEvent.setup()
    renderRoute('/cost-centers/cc-general?tab=limits')
    await screen.findByRole('heading', { name: 'General' })
    await user.selectOptions(screen.getByLabelText('Resource'), 'modelApi:model-1')
    await user.type(screen.getByLabelText('Tokens per minute'), '1000')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))
    await user.click(screen.getByRole('button', { name: 'Overview' }))
    expect(await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Stay' }))
    expect(screen.getByText('Chat (model API) · 1,000 tokens per minute')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Overview' }))
    await user.click(await screen.findByRole('button', { name: 'Discard changes' }))
    expect(await screen.findByRole('heading', { name: 'Summary' })).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Limits' }))
    expect(screen.queryByText('Chat (model API) · 1,000 tokens per minute')).not.toBeInTheDocument()
    expect(screen.getByText('No cost-center defaults are set.')).toBeVisible()
    expect(api.updateCostCenterLimits).not.toHaveBeenCalled()
  })

  it('adds a pool-only limit and refuses an empty limit', async () => {
    const user = userEvent.setup()
    renderRoute('/cost-centers/cc-general?tab=limits')

    expect(await screen.findByRole('heading', { name: 'General' })).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Resource'), 'modelApi:model-1')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))

    expect(screen.getByText('Set per-person limits, a pooled quota, or both.')).toBeVisible()
    expect(screen.queryByText(/Pooled quota:/)).not.toBeInTheDocument()

    await user.type(screen.getByLabelText('Monthly pooled tokens'), '40000000')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))
    expect(screen.getByText('Chat (model API) · Pooled quota: 40,000,000 tokens per month')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save limits' }))

    await waitFor(() => expect(api.updateCostCenterLimits).toHaveBeenCalledWith('cc-general', [
      {
        resource: { kind: 'modelApi', id: 'model-1' },
        person: null,
        pool: { monthlyTokens: 40_000_000, monthlyCalls: null },
      },
    ]))
  })

  it('limits a pool model, naming its pool so a model ID another pool shares stays apart', async () => {
    const user = userEvent.setup()
    // Both pools serve a model with the same ID.
    api.listModelPools.mockResolvedValue([anthropicPool, { ...draftPool, models: anthropicPool.models }])
    api.getCostCenter.mockResolvedValue({
      ...general,
      limits: [{
        resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: draftPool.id },
        person: { tokensPerMinute: 500, tokenQuota: null, tokenQuotaPeriod: null, callsPerMinute: null, callQuota: null, callQuotaPeriod: null },
        pool: null,
      }],
    })
    renderRoute('/cost-centers/cc-general?tab=limits')

    expect(await screen.findByText('Claude Opus 4.5 in OpenAI chat (pool model) · 500 tokens per minute')).toBeVisible()
    await user.selectOptions(screen.getByLabelText('Resource'), 'Claude Opus 4.5 in Anthropic Claude (pool model)')
    await user.type(screen.getByLabelText('Tokens per minute'), '1000')
    await user.type(screen.getByLabelText('Monthly pooled tokens'), '5000000')
    await user.click(screen.getByRole('button', { name: 'Add limit' }))

    // The second pool's limit is its own, so adding the first pool's leaves it in place.
    expect(screen.getByText('Claude Opus 4.5 in OpenAI chat (pool model) · 500 tokens per minute')).toBeVisible()
    expect(screen.getByText(
      'Claude Opus 4.5 in Anthropic Claude (pool model) · 1,000 tokens per minute · Pooled quota: 5,000,000 tokens per month',
    )).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save limits' }))

    await waitFor(() => expect(api.updateCostCenterLimits).toHaveBeenCalledWith('cc-general', [
      {
        resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: draftPool.id },
        person: { tokensPerMinute: 500, tokenQuota: null, tokenQuotaPeriod: null, callsPerMinute: null, callQuota: null, callQuotaPeriod: null },
        pool: null,
      },
      {
        resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: anthropicPool.id },
        person: {
          tokensPerMinute: 1000,
          tokenQuota: null,
          tokenQuotaPeriod: null,
          callsPerMinute: null,
          callQuota: null,
          callQuotaPeriod: null,
        },
        pool: { monthlyTokens: 5_000_000, monthlyCalls: null },
      },
    ]))
  })
})
