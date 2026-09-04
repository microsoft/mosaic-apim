import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { PublishModelDialog } from './PublishModelDialog'
import type { Gateway, Publication, PublishableModel, PublishPlan, PublishRun } from '../types'

function gateway(overrides: Partial<Gateway> = {}): Gateway {
  return {
    id: 'gateway_1',
    tenantId: 'tenant-test',
    name: 'Managed gateway',
    provider: 'apim',
    azureResourceId: '/subscriptions/s/resourceGroups/rg/providers/Microsoft.ApiManagement/service/apim',
    subscriptionId: 's',
    resourceGroup: 'rg',
    serviceName: 'apim',
    environmentLabel: null,
    managementMode: 'manage',
    status: 'connected',
    access: { canRead: true, canWrite: true, evaluation: 'effectivePermissions', missingActions: [], remediation: null, message: 'Ready.' },
    capabilities: { managementApiVersion: '2024-05-01', aiGatewayPolicies: 'available', mcpServers: 'available', identityObserved: true, notes: [] },
    inventory: { apis: 0, aiApis: 0, mcpServers: 0, operations: 0, products: 0, subscriptions: 0, users: 0, groups: 0, backends: 0, namedValues: 0, policyDocuments: 0, policyFragments: 0, recognizedFacets: 0, unrecognizedFacets: 0, mosaicManagedFacets: 0 },
    lastSyncedAt: '2026-09-01T12:00:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

const publishableModel: PublishableModel = {
  modelEndpointId: 'endpoint_1',
  endpointName: 'Contoso models',
  provider: 'azureOpenAi',
  deploymentName: 'gpt-4o-prod',
  modelName: 'gpt-4o',
  modelVersion: '2024-11-20',
  publicationId: null,
  publicationStatus: null,
  suggestedApiName: 'gpt-4o-api',
  suggestedApiPath: 'models/gpt-4o',
  runtimeAccess: { gatewayId: 'gateway_1', gatewayName: 'Managed gateway', apimPrincipalId: 'principal', canInvoke: true, evaluation: 'roleAssignments', checkedAt: '2026-09-01T12:00:00Z', requiredRoleName: 'Cognitive Services OpenAI User', requiredRoleDefinitionId: 'role', assignmentScope: null, inherited: false, remediation: null, message: 'Gateway can invoke this endpoint.' },
}

const publication: Publication = {
  id: 'pub_1', tenantId: 'tenant-test', entityType: 'publication', gatewayId: 'gateway_1', modelEndpointId: 'endpoint_1', deploymentName: 'gpt-4o-prod', provider: 'azureOpenAi', displayName: 'GPT-4o', apiName: 'gpt-4o-api', apiPath: 'models/gpt-4o', backendName: 'backend', fragmentName: 'fragment', productName: 'product', subscriptionName: 'subscription', subscriptionRequired: true, enforcement: { counterKeyExpression: '@(context.Subscription.Id)', tokensPerMinute: 12000, estimatePromptTokens: true }, shapeVersion: '1', status: 'planned', resources: [], lastPlanId: 'plan_1', lastPlanDigest: 'digest', lastRunId: null, lastAppliedAt: null, lastError: null, createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
}

const plan: PublishPlan = {
  id: 'plan_1', tenantId: 'tenant-test', entityType: 'publishPlan', publicationId: 'pub_1', gatewayId: 'gateway_1', digest: 'digest', policyContentSha256: null, warnings: ['The API path already exists and will be updated.'], createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
  steps: [{ kind: 'api', name: 'gpt-4o-api', action: 'create', reason: 'Create the API for this deployment.', resourceId: '/apis/gpt-4o-api', existed: false }],
  facets: [{ kind: 'tokenLimit', element: 'azure-openai-token-limit', section: 'inbound', summary: 'Limits tokens per caller.', details: ['12000 tokens per minute.'], attributes: {}, confidence: 'recognized', managedByMosaic: true }],
}

function run(overrides: Partial<PublishRun> = {}): PublishRun {
  return {
    id: 'run_1', tenantId: 'tenant-test', entityType: 'publishRun', publicationId: 'pub_1', gatewayId: 'gateway_1', planId: 'plan_1', planDigest: 'digest', status: 'succeeded', startedAt: '2026-09-01T12:00:00Z', completedAt: '2026-09-01T12:00:02Z', durationMs: 2000, rolledBack: false, orphanedResources: [], errors: [], createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:02Z',
    steps: [{ kind: 'api', name: 'gpt-4o-api', action: 'create', status: 'succeeded', resourceId: '/apis/gpt-4o-api', createdByMosaic: true, error: null }],
    ...overrides,
  }
}

const api = {
  listGateways: vi.fn(),
  listPublishableModels: vi.fn(),
  createPublication: vi.fn(),
  createPublishPlan: vi.fn(),
  applyPublishPlan: vi.fn(),
  getPublishRun: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderDialog() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={queryClient}>
      <PublishModelDialog open onClose={vi.fn()} onPublished={vi.fn()} />
    </QueryClientProvider>,
  )
}

async function advanceToReview(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole('checkbox', { name: 'Publish gpt-4o-prod' }))
  await user.click(screen.getByRole('button', { name: 'Configure' }))
  await user.click(screen.getByRole('button', { name: 'Review plan' }))
}

describe('PublishModelDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([gateway(), gateway({ id: 'gateway_2', name: 'Observe gateway', managementMode: 'observe' })])
    api.listPublishableModels.mockResolvedValue([publishableModel])
    api.createPublication.mockResolvedValue(publication)
    api.createPublishPlan.mockResolvedValue(plan)
    api.applyPublishPlan.mockResolvedValue(run())
    api.getPublishRun.mockResolvedValue(run())
  })

  it('renders publishable models', async () => {
    renderDialog()

    const table = await screen.findByRole('table', { name: 'Publishable models' })
    expect(within(table).getByText('gpt-4o-prod')).toBeVisible()
    expect(within(table).getByText('gpt-4o')).toBeVisible()
  })

  it('does not allow choosing a gateway outside manage mode', async () => {
    renderDialog()

    const option = await screen.findByRole('option', { name: /Observe gateway/ })
    expect(option).toBeDisabled()
    expect(option).toHaveTextContent(/switch to managed mode/i)
  })

  it('shows warnings, steps, and policy facets in the plan review', async () => {
    const user = userEvent.setup()
    renderDialog()

    await advanceToReview(user)

    await waitFor(() => expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1'))
    expect(await screen.findByText('The API path already exists and will be updated.')).toBeVisible()
    expect(screen.getByText('Create the API for this deployment.')).toBeVisible()
    expect(screen.getByText('Limits tokens per caller.')).toBeVisible()
  })

  it('reports a rolled-back run as rolled back', async () => {
    const user = userEvent.setup()
    const rolledBack = run({ status: 'rolledBack', rolledBack: true, steps: [{ ...run().steps[0], status: 'rolledBack' }] })
    api.applyPublishPlan.mockResolvedValue(rolledBack)
    api.getPublishRun.mockResolvedValue(rolledBack)
    renderDialog()

    await advanceToReview(user)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText(/undid what it created/i)).toBeVisible()
    // The banner title and the step's own status both read "Rolled back".
    expect(screen.getAllByText('Rolled back').length).toBeGreaterThan(1)
  })

  it('explains that a replaced resource was left in place rather than reverted', async () => {
    const user = userEvent.setup()
    const rolledBack = run({
      status: 'rolledBack',
      rolledBack: true,
      steps: [{ ...run().steps[0], kind: 'backend', status: 'skipped', createdByMosaic: false }],
    })
    api.applyPublishPlan.mockResolvedValue(rolledBack)
    api.getPublishRun.mockResolvedValue(rolledBack)
    renderDialog()

    await advanceToReview(user)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText(/left in place/i)).toBeVisible()
  })

  it('lists orphaned resources left behind in API Management', async () => {
    const user = userEvent.setup()
    const failed = run({
      status: 'rollbackFailed',
      orphanedResources: [{ kind: 'api', name: 'orphan-api', resourceId: '/apis/orphan-api', createdByMosaic: true, appliedAt: '2026-09-01T12:00:01Z' }],
    })
    api.applyPublishPlan.mockResolvedValue(failed)
    api.getPublishRun.mockResolvedValue(failed)
    renderDialog()

    await advanceToReview(user)
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(await screen.findByText(/Resources left behind in API Management/)).toBeVisible()
    expect(screen.getByText(/orphan-api/)).toBeVisible()
  })
})
