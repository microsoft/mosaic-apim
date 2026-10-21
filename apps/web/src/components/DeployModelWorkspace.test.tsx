import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { DeploymentCatalogue, ModelDeploymentCreate, ModelDeploymentPlan, ModelDeploymentRun, ModelEndpoint } from '../types'
import { DeployModelWorkspace } from './DeployModelWorkspace'

const endpoint: ModelEndpoint = {
  id: 'endpoint_demo', tenantId: 'demo', name: 'Fictional Azure account',
  provider: 'azureOpenAi', endpoint: 'https://fictional.openai.azure.com/',
  azureResourceId: '/subscriptions/demo/resourceGroups/demo/providers/Microsoft.CognitiveServices/accounts/fictional',
  environment: null, authMode: 'managedIdentity', managementMode: 'observe', status: 'connected',
  access: { canRead: true, evaluation: 'effectivePermissions', missingActions: [] },
  runtimeAccess: [], capabilities: { managementApiVersion: '2024-10-01', notes: [] },
  inventory: { deployments: 0, availableModels: 0, succeededDeployments: 0, deprecatedDeployments: 0 },
  createdAt: '', updatedAt: '',
}
const target = {
  endpointId: endpoint.id, endpointName: endpoint.name, accountScope: endpoint.azureResourceId!,
  azureCloud: 'commercial' as const, subscriptionId: 'demo', resourceGroup: 'demo',
  accountName: 'fictional', location: 'eastus2',
}
const quota = { usageName: 'gpt4o-global', currentValue: 0, limit: 100, available: 100, unit: 'Count', blocked: false }
const catalogue: DeploymentCatalogue = {
  endpointId: endpoint.id, target, checkedAt: '2026-10-01T12:00:00Z',
  access: { canDeploy: true, evaluation: 'allowed', message: 'Account-scoped deployment permissions verified.',
    checkedAt: '', requiredActions: [], missingActions: [] },
  models: [{ modelFormat: 'OpenAI', modelName: 'gpt-4o', modelVersion: '2024-11-20',
    isDefaultVersion: true, capabilities: { chatCompletion: 'true' },
    skus: [{ name: 'GlobalStandard', capacity: { minimum: 1, maximum: 100, step: 1, default: 10, allowedValues: [] }, quota }] }],
  raiPolicies: ['Microsoft.DefaultV2'],
}
const plan: ModelDeploymentPlan = {
  id: 'deploymentplan_demo', endpointId: endpoint.id, target,
  configuration: {
    deploymentName: 'gpt-4o-evaluation',
    model: { modelFormat: 'OpenAI', modelName: 'gpt-4o', modelVersion: '2024-11-20' },
    skuName: 'GlobalStandard', capacity: 10,
  },
  checks: { quota, warnings: ['Provisioning is billable. Publishing through API Management is separate.'] },
}
const completed: ModelDeploymentRun = {
  id: 'deploymentrun_demo', endpointId: endpoint.id, planId: plan.id,
  configuration: plan.configuration, target, status: 'succeeded', startedAt: '',
  provisioningState: 'Succeeded', writeAttempted: true, refreshStatus: 'succeeded',
}
const api = {
  getDeployableModels: vi.fn(),
  listModelDeploymentRuns: vi.fn(),
  getModelDeploymentRun: vi.fn(),
  updateModelEndpoint: vi.fn(),
  planModelDeployment: vi.fn(),
  applyModelDeployment: vi.fn(),
  reconcileModelDeployment: vi.fn(),
  refreshModelDeployment: vi.fn(),
}
vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderWorkspace(manage = false) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  render(<QueryClientProvider client={client}><MemoryRouter>
    <DeployModelWorkspace endpoint={{ ...endpoint, managementMode: manage ? 'manage' : 'observe' }} onClose={vi.fn()} />
  </MemoryRouter></QueryClientProvider>)
}

async function configure(user: ReturnType<typeof userEvent.setup>) {
  await user.selectOptions(await screen.findByRole('combobox', { name: 'Model and exact version' }),
    screen.getByRole('option', { name: /gpt-4o · 2024-11-20/ }))
  await user.selectOptions(screen.getByRole('combobox', { name: 'Deployment type' }), 'GlobalStandard')
  const name = screen.getByLabelText(/^New deployment name/)
  await user.clear(name)
  await user.type(name, 'gpt-4o-evaluation')
}

describe('Azure model deployment workspace', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.getDeployableModels.mockResolvedValue(structuredClone(catalogue))
    api.listModelDeploymentRuns.mockResolvedValue([])
    api.getModelDeploymentRun.mockResolvedValue(completed)
    api.updateModelEndpoint.mockResolvedValue({ ...endpoint, managementMode: 'manage' })
    api.planModelDeployment.mockResolvedValue(plan)
    api.applyModelDeployment.mockResolvedValue(completed)
    api.refreshModelDeployment.mockResolvedValue(completed)
  })

  it('requires explicit endpoint opt-in and final review before creating in Azure', async () => {
    const user = userEvent.setup()
    renderWorkspace()
    await configure(user)
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeDisabled()
    expect(api.updateModelEndpoint).not.toHaveBeenCalled()
    await user.click(screen.getByRole('checkbox', { name: /^Allow MOSAIC/ }))
    await user.click(screen.getByRole('button', { name: 'Enable Manage deployments' }))
    expect(api.updateModelEndpoint).toHaveBeenCalledWith(endpoint.id, { managementMode: 'manage' })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Review deployment' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Review deployment' }))
    const deploy = await screen.findByRole('button', { name: 'Deploy to Azure' })
    expect(api.planModelDeployment).toHaveBeenCalledWith(endpoint.id, expect.objectContaining({
      deploymentName: 'gpt-4o-evaluation', capacity: 10, skuName: 'GlobalStandard',
      model: expect.objectContaining({ modelVersion: '2024-11-20' }),
    }))
    expect(api.applyModelDeployment).not.toHaveBeenCalled()
    expect(deploy).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: /^I reviewed the target/ }))
    await user.click(deploy)
    expect(api.applyModelDeployment).toHaveBeenCalledWith(endpoint.id, plan.id)
    expect(await screen.findByText('Azure deployment confirmed')).toBeVisible()
    expect(await screen.findByText(/MOSAIC inventory is up to date/)).toBeVisible()
  })

  it('blocks unknown permissions rather than treating Reader access as deployment access', async () => {
    api.getDeployableModels.mockResolvedValue({
      ...catalogue, access: { ...catalogue.access, canDeploy: false, evaluation: 'unknown',
        message: 'Effective deployment permissions could not be verified.' },
    })
    renderWorkspace()
    expect(await screen.findByText(/Effective deployment permissions could not be verified/)).toBeVisible()
    expect(screen.getByRole('checkbox', { name: /^Allow MOSAIC/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeDisabled()
  })

  it('explains when Azure advertises no deployment types for a model version', async () => {
    const user = userEvent.setup()
    api.getDeployableModels.mockResolvedValue({
      ...catalogue, models: [{ ...catalogue.models[0], skus: [] }],
    })
    renderWorkspace(true)
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Model and exact version' }),
      screen.getByRole('option', { name: /gpt-4o · 2024-11-20/ }))
    expect(await screen.findByText(/Azure advertises no deployment types/)).toBeVisible()
    expect(screen.getByRole('combobox', { name: 'Deployment type' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeDisabled()
    expect(api.planModelDeployment).not.toHaveBeenCalled()
  })

  it('blocks quota that cannot be verified and validates capacity bounds', async () => {
    const user = userEvent.setup()
    api.getDeployableModels.mockResolvedValue({
      ...catalogue, quotaError: 'Subscription quota is unreadable.',
    })
    renderWorkspace(true)
    await configure(user)
    expect(screen.getByRole('button', { name: 'Review deployment' })).toBeDisabled()
    const capacity = screen.getByLabelText(/^Capacity \(Azure SKU units\)/)
    await user.clear(capacity)
    await user.type(capacity, '101')
    expect(await screen.findByText(/Enter a positive whole number within/)).toBeVisible()
    expect(api.planModelDeployment).not.toHaveBeenCalled()
  })

  it.each(['unreadable', 'removed'])('reviews Azure default when a selected content filter becomes %s', async (reason) => {
    const user = userEvent.setup()
    api.getDeployableModels.mockResolvedValueOnce(structuredClone(catalogue)).mockResolvedValue({
      ...catalogue, raiPolicies: [],
      policyError: reason === 'unreadable' ? 'Content-filter policies could not be read.' : null,
    })
    api.planModelDeployment.mockImplementation((_endpointId: string, configuration: ModelDeploymentCreate) =>
      Promise.resolve({ ...plan, configuration }))
    renderWorkspace(true)
    await configure(user)
    await user.selectOptions(screen.getByRole('combobox', { name: 'Content-filter policy' }), 'Microsoft.DefaultV2')
    await user.click(screen.getByRole('button', { name: 'Check catalogue again' }))
    await waitFor(() => expect(screen.getByRole('combobox', { name: 'Content-filter policy' })).toHaveValue(''))
    if (reason === 'unreadable') {
      expect(screen.getByRole('combobox', { name: 'Content-filter policy' })).toBeDisabled()
    }
    await user.click(screen.getByRole('button', { name: 'Review deployment' }))
    expect(await screen.findByRole('button', { name: 'Deploy to Azure' })).toBeVisible()
    expect(api.planModelDeployment).toHaveBeenCalledWith(endpoint.id, expect.objectContaining({ raiPolicyName: null }))
    expect(screen.getByText('Content filter').parentElement).toHaveTextContent('Azure default; no override')
    expect(api.applyModelDeployment).not.toHaveBeenCalled()
  })

  it('refreshes inventory after remote success without resubmitting creation', async () => {
    const user = userEvent.setup()
    const failedRefresh = { ...completed, refreshStatus: 'failed', refreshError: 'Injected sync failure' }
    api.listModelDeploymentRuns.mockResolvedValue([failedRefresh])
    api.getModelDeploymentRun.mockResolvedValue(failedRefresh)
    renderWorkspace(true)
    await user.selectOptions(await screen.findByRole('combobox', { name: 'Deployment history' }), completed.id)
    await user.click(await screen.findByRole('button', { name: 'Retry inventory refresh' }))
    expect(api.refreshModelDeployment).toHaveBeenCalledWith(endpoint.id, completed.id)
    expect(api.applyModelDeployment).not.toHaveBeenCalled()
    expect(await screen.findByText(/MOSAIC inventory is up to date/)).toBeVisible()
  })

  it('opens the blocking operation even after its terminal outcome was recorded', async () => {
    const user = userEvent.setup()
    const failed = { ...completed, status: 'failed' as const, error: 'Azure rejected the create.' }
    api.getDeployableModels.mockResolvedValue({ ...catalogue, blockingRunId: failed.id })
    api.listModelDeploymentRuns.mockResolvedValue([failed])
    api.getModelDeploymentRun.mockResolvedValue(failed)
    api.reconcileModelDeployment.mockResolvedValue(failed)
    renderWorkspace(true)
    await user.click(await screen.findByRole('button', { name: 'Reconcile completed operation' }))
    expect(api.reconcileModelDeployment).toHaveBeenCalledWith(endpoint.id, failed.id, {
      confirmWorkerStopped: false, confirmNoPendingOperation: false,
    })
    expect(api.applyModelDeployment).not.toHaveBeenCalled()
  })

  it('requires stopped-worker confirmation before recovering a running inventory refresh', async () => {
    const user = userEvent.setup()
    const refreshing = { ...completed, refreshStatus: 'running' as const }
    api.getDeployableModels.mockResolvedValue({ ...catalogue, blockingRunId: refreshing.id })
    api.listModelDeploymentRuns.mockResolvedValue([refreshing])
    api.getModelDeploymentRun.mockResolvedValue(refreshing)
    api.reconcileModelDeployment.mockResolvedValue({ ...completed, refreshStatus: 'failed' })
    renderWorkspace(true)
    const recover = await screen.findByRole('button', { name: 'Reconcile interrupted refresh' })
    expect(recover).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: 'I verified the original inventory-refresh worker has stopped.' }))
    await user.click(recover)
    expect(api.reconcileModelDeployment).toHaveBeenCalledWith(endpoint.id, completed.id, {
      confirmWorkerStopped: true, confirmNoPendingOperation: false,
    })
    expect(api.applyModelDeployment).not.toHaveBeenCalled()
  })

  it('reconciles interrupted operations without create and requires explicit absent recovery', async () => {
    const user = userEvent.setup()
    const interrupted = { ...completed, status: 'interrupted', refreshStatus: 'notStarted',
      error: 'Azure outcome is unconfirmed.' }
    api.listModelDeploymentRuns.mockResolvedValue([interrupted])
    api.getModelDeploymentRun.mockResolvedValue(interrupted)
    api.reconcileModelDeployment.mockResolvedValue(interrupted)
    renderWorkspace(true)
    const recover = await screen.findByRole('button', { name: 'Confirm worker-stopped recovery' })
    expect(recover).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Check outcome in Azure' }))
    expect(api.reconcileModelDeployment).toHaveBeenCalledWith(endpoint.id, completed.id, {
      confirmWorkerStopped: false, confirmNoPendingOperation: false,
    })
    await user.click(screen.getByRole('checkbox', { name: /^I verified the original deployment worker/ }))
    await user.click(recover)
    expect(api.reconcileModelDeployment).toHaveBeenLastCalledWith(endpoint.id, completed.id, {
      confirmWorkerStopped: true, confirmNoPendingOperation: true,
    })
    expect(api.applyModelDeployment).not.toHaveBeenCalled()
  })
})
