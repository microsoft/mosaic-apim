import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelsPage } from './ModelsPage'
import { accessPlan, modelPublication } from '../test/model-access'
import type {
  AccessRemediation,
  Gateway,
  GatewayRuntimeAccess,
  ModelApi,
  ModelEndpoint,
  ModelEndpointSuggestion,
  ModelEndpointSuggestionView,
  Publication,
  PublishPlan,
  PublishRun,
  SubscriptionScanIssue,
} from '../types'

const RESOURCE_ID =
  '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-dev' +
  '/providers/Microsoft.ApiManagement/service/apim-contoso-dev'

const gateway: Gateway = {
  id: 'gateway_1',
  tenantId: 'tenant-test',
  name: 'Development gateway',
  provider: 'apim',
  azureResourceId: RESOURCE_ID,
  subscriptionId: '00000000-0000-0000-0000-000000000000',
  resourceGroup: 'rg-contoso-dev',
  serviceName: 'apim-contoso-dev',
  environment: null,
  azureEnvironmentTag: null,
  environmentLabel: 'dev',
  managementMode: 'observe',
  status: 'connected',
  access: {
    canRead: true,
    canWrite: false,
    evaluation: 'effectivePermissions',
    checkedAt: '2026-09-01T12:00:00Z',
    missingActions: [],
    remediation: null,
    message: 'MOSAIC can read this gateway.',
  },
  capabilities: {
    skuName: 'Developer',
    skuCapacity: 1,
    provisioningState: 'Succeeded',
    location: 'eastus2',
    gatewayUrl: 'https://apim-contoso-dev.azure-api.net',
    managementApiVersion: '2024-05-01',
    aiGatewayPolicies: 'available',
    mcpServers: 'available',
      identityObserved: true,
    notes: [],
  },
  inventory: {
    apis: 2,
    aiApis: 1,
    mcpServers: 2,
    operations: 2,
    products: 1,
    subscriptions: 1,
    users: 1,
    groups: 1,
    backends: 1,
    namedValues: 1,
    policyDocuments: 3,
    policyFragments: 1,
    recognizedFacets: 4,
    unrecognizedFacets: 1,
    mosaicManagedFacets: 1,
  },
  lastSyncedAt: '2026-09-01T12:05:00Z',
  lastSyncError: null,
  createdAt: '2026-09-01T11:00:00Z',
  updatedAt: '2026-09-01T12:05:00Z',
}

const modelApi: ModelApi = {
  id: 'modelApi_1',
  tenantId: 'tenant-test',
  gatewayId: 'gateway_1',
  apiName: 'chat-api',
  displayName: 'Chat completions',
  path: 'openai',
  serviceUrl: 'https://contoso.openai.azure.com/openai',
  protocols: ['https'],
  aiKind: 'azureOpenAi',
  aiSignals: ['Backend URL points at Azure OpenAI.'],
  subscriptionRequired: true,
  operationCount: 1,
  productNames: ['Gold tier'],
  visibility: 'catalog',
  selection: 'detected',
  importedFromSnapshotId: 'snapshot_1',
  importedAt: '2026-09-01T12:10:00Z',
  importedBy: 'admin-object-id',
  createdAt: '2026-09-01T12:10:00Z',
  updatedAt: '2026-09-01T12:10:00Z',
}


const publication: Publication = {
  accessState: 'pending',
  id: 'pub_1',
  tenantId: 'tenant-test',
  entityType: 'publication',
  gatewayId: 'gateway_1',
  modelEndpointId: 'endpoint_1',
  deploymentName: 'gpt-4o-prod',
  provider: 'azureOpenAi',
  displayName: 'GPT-4o production',
  apiName: 'gpt-4o-api',
  apiPath: 'models/gpt-4o',
  backendName: 'backend',
  fragmentName: 'fragment',
  productName: 'product',
  subscriptionName: 'subscription',
  subscriptionRequired: true,
  enforcement: {
    counterKeyExpression: '@(context.Subscription.Id)',
    tokensPerMinute: 12000,
    estimatePromptTokens: true,
  },
  shapeVersion: '1',
  status: 'published',
  resources: [],
  lastPlanId: 'plan_1',
  lastPlanDigest: 'digest',
  lastRunId: 'run_1',
  lastAppliedAt: '2026-09-01T12:30:00Z',
  lastError: null,
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:30:00Z',
}

const publishPlan: PublishPlan = {
  id: 'plan_1',
  tenantId: 'tenant-test',
  entityType: 'publishPlan',
  publicationId: 'pub_1',
  gatewayId: 'gateway_1',
  digest: 'digest',
  steps: [
    {
      kind: 'api',
      name: 'gpt-4o-api',
      action: 'create',
      reason: 'Create the API for this deployment.',
      resourceId: '/apis/gpt-4o-api',
      existed: false,
    },
  ],
  facets: [],
  policyContentSha256: null,
  warnings: ['Review runtime access before applying.'],
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

function publishRun(overrides: Partial<PublishRun> = {}): PublishRun {
  return {
    id: 'run_1', tenantId: 'tenant-test', entityType: 'publishRun', publicationId: 'pub_1', gatewayId: 'gateway_1',
    planId: 'plan_1', planDigest: 'digest', status: 'running', startedAt: '2026-09-01T12:00:00Z', completedAt: null,
    durationMs: null, steps: [], rolledBack: false, orphanedResources: [], errors: [],
    createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
    ...overrides,
  }
}

const unpublishPlan: PublishPlan = {
  ...publishPlan,
  id: 'unpublish_plan_1',
  operation: 'unpublish',
  digest: 'unpublish-digest',
  steps: [
    {
      kind: 'api',
      name: 'gpt-4o-api',
      action: 'delete',
      reason: 'Delete the API that fronts this model. The gateway stops serving it.',
      resourceId: '/apis/gpt-4o-api',
      existed: true,
    },
  ],
  warnings: [],
  accessSnapshot: null,
}

const api = {
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  listGateways: vi.fn(),
  listModelApis: vi.fn(),
  deletePublication: vi.fn(),
  planUnpublishPublication: vi.fn(),
  unpublishPublication: vi.fn(),
  listPrincipals: vi.fn(),
  getPublishRun: vi.fn(),
  applyPublishPlan: vi.fn(),
  createPublishPlan: vi.fn(),
  createPublication: vi.fn(),
  listPublishableModels: vi.fn(),
  listPublications: vi.fn(),
  deleteModelApi: vi.fn(),
  listImportableApis: vi.fn(),
  importModelApis: vi.fn(),
  listModelEndpoints: vi.fn(),
  listSuggestedModelEndpoints: vi.fn(),
  registerModelEndpoint: vi.fn(),
  updateModelEndpoint: vi.fn(),
  syncModelEndpoint: vi.fn(),
  preflightModelEndpoint: vi.fn(),
  deleteModelEndpoint: vi.fn(),
  listModelDeployments: vi.fn(),
  declareModelDeployment: vi.fn(),
  removeDeclaredModelDeployment: vi.fn(),
}

const { TestApiError } = vi.hoisted(() => ({
  TestApiError: class ApiError extends Error {
    readonly status: number
    readonly body?: { message?: string; details?: Record<string, unknown> }

    constructor(
      message: string,
      status: number,
      body?: { message?: string; details?: Record<string, unknown> },
    ) {
      super(message)
      this.status = status
      this.body = body
    }
  },
}))

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: TestApiError,
}))

function LocationProbe() {
  const location = useLocation()
  return <span data-testid="location">{`${location.pathname}${location.search}`}</span>
}

function renderPage(entry = '/models') {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={[entry]}>
        <ModelsPage />
        <LocationProbe />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('ModelsPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([gateway])
    api.listEnvironmentFindings.mockResolvedValue({
      items: [],
      limitations: [],
      generatedAt: '2026-09-01T12:00:00Z',
    })
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [
        {
          key: 'development',
          displayName: 'Development',
          description: null,
          color: 'brand',
          production: false,
          aliases: [],
          acceptsEndpointsFrom: [],
          order: 10,
          builtIn: true,
          usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
        },
      ],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.listModelApis.mockResolvedValue([])
    api.listPublications.mockResolvedValue([])
    api.listModelEndpoints.mockResolvedValue([])
    api.listSuggestedModelEndpoints.mockResolvedValue(suggestionView())
    api.listModelDeployments.mockResolvedValue([])
    api.listImportableApis.mockResolvedValue({
      gatewayId: gateway.id,
      snapshotId: 'snapshot_1',
      lastSyncedAt: gateway.lastSyncedAt,
      candidates: [
        {
          apiName: 'chat-api',
          displayName: 'Chat completions',
          path: 'openai',
          serviceUrl: 'https://contoso.openai.azure.com/openai',
          aiKind: 'azureOpenAi',
          aiSignals: ['Backend URL points at Azure OpenAI.'],
          operationCount: 1,
          productNames: [],
          recommended: true,
          alreadyImported: false,
        },
        {
          apiName: 'echo-api',
          displayName: 'Echo',
          path: 'echo',
          serviceUrl: 'https://echo.contoso.com',
          aiKind: 'none',
          aiSignals: [],
          operationCount: 1,
          productNames: [],
          recommended: false,
          alreadyImported: false,
        },
      ],
    })
    api.importModelApis.mockResolvedValue([modelApi])
    api.listPublishableModels.mockResolvedValue([])
    api.createPublication.mockResolvedValue(publication)
    api.createPublishPlan.mockResolvedValue(publishPlan)
    api.applyPublishPlan.mockResolvedValue({ id: 'run_1', tenantId: 'tenant-test', entityType: 'publishRun', publicationId: 'pub_1', gatewayId: 'gateway_1', planId: 'plan_1', planDigest: 'digest', status: 'running', startedAt: '2026-09-01T12:00:00Z', completedAt: null, durationMs: null, steps: [], rolledBack: false, orphanedResources: [], errors: [], createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z' })
    api.getPublishRun.mockResolvedValue({ id: 'run_1', tenantId: 'tenant-test', entityType: 'publishRun', publicationId: 'pub_1', gatewayId: 'gateway_1', planId: 'plan_1', planDigest: 'digest', status: 'succeeded', startedAt: '2026-09-01T12:00:00Z', completedAt: '2026-09-01T12:00:01Z', durationMs: 1000, steps: [], rolledBack: false, orphanedResources: [], errors: [], createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:01Z' })
    api.unpublishPublication.mockResolvedValue(publishRun({ id: 'run_2', planId: 'unpublish_plan_1' }))
    api.planUnpublishPublication.mockResolvedValue(unpublishPlan)
    api.listPrincipals.mockResolvedValue([])
    api.deletePublication.mockResolvedValue(undefined)
  })

  it('invites an import when nothing has been adopted yet', async () => {
    renderPage()

    expect(await screen.findByText('No model APIs imported yet')).toBeVisible()
  })

  it('lists imported model APIs with the provider MOSAIC detected', async () => {
    api.listModelApis.mockResolvedValue([modelApi])

    renderPage()

    // Scoped to the live table: the sample preview below also mentions Azure OpenAI.
    const table = await screen.findByRole('table', { name: 'Imported model APIs' })
    expect(within(table).getByText('Chat completions')).toBeVisible()
    expect(within(table).getByText('Azure OpenAI')).toBeVisible()
    expect(within(table).getByRole('link', { name: 'Development gateway' })).toBeVisible()
  })

  it('opens the import dialog from the gateway query and preselects detected APIs', async () => {
    renderPage('/models?import=gateway_1')

    expect(await screen.findByRole('dialog')).toBeVisible()
    const recommended = await screen.findByRole('checkbox', {
      name: 'Import Chat completions',
    })
    const notRecommended = screen.getByRole('checkbox', { name: 'Import Echo' })

    // Detection pre-checks, but an unrecognised API is still offered and simply starts unchecked.
    expect(recommended).toBeChecked()
    expect(notRecommended).not.toBeChecked()
  })

  it('lets an administrator adopt an API MOSAIC did not recognise', async () => {
    const user = userEvent.setup()
    renderPage('/models?import=gateway_1')

    await user.click(await screen.findByRole('checkbox', { name: 'Import Echo' }))
    await user.click(screen.getByRole('button', { name: 'Import 2' }))

    await waitFor(() => {
      expect(api.importModelApis).toHaveBeenCalledWith('gateway_1', ['chat-api', 'echo-api'])
    })
  })

  it('clears the import query when the dialog closes', async () => {
    const user = userEvent.setup()
    renderPage('/models?import=gateway_1')

    await screen.findByRole('dialog')
    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/models')
    })
  })


  it('lists published models with gateway and API path', async () => {
    api.listPublications.mockResolvedValue([publication])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    expect(within(table).getByText('GPT-4o production')).toBeVisible()
    expect(within(table).getByText('Published')).toBeVisible()
    expect(within(table).getByRole('link', { name: 'Development gateway' })).toBeVisible()
    expect(within(table).getByText('/models/gpt-4o')).toBeVisible()
  })

  it('opens the fresh plan for review when an administrator re-plans', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))

    await waitFor(() => expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1'))
    const steps = await screen.findByRole('table', { name: 'Publish plan steps' })
    const dialog = screen.getByRole('dialog')
    expect(dialog).toContainElement(steps)
    expect(within(steps).getByText('Create the API for this deployment.')).toBeVisible()
    expect(within(dialog).getByText('Review runtime access before applying.')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Apply plan' })).toBeEnabled()
    expect(within(dialog).queryByText(/Nothing to apply/)).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('applies only the plan the administrator reviewed, never the saved one', async () => {
    const user = userEvent.setup()
    // A legacy publication with a saved plan, which the table used to apply without showing it.
    api.listPublications.mockResolvedValue([publication])
    api.createPublishPlan.mockResolvedValue({ ...publishPlan, id: 'plan_2' })

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    expect(within(table).queryByRole('button', { name: 'Apply' })).not.toBeInTheDocument()
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))

    const applyPlan = await screen.findByRole('button', { name: 'Apply plan' })
    expect(screen.getByRole('table', { name: 'Publish plan steps' })).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()

    await user.click(applyPlan)

    await waitFor(() => expect(api.applyPublishPlan).toHaveBeenCalledTimes(1))
    expect(api.applyPublishPlan).toHaveBeenCalledWith('pub_1', 'plan_2')
  })

  it('always reviews the complete governed-access snapshot before applying an existing plan', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.createPublishPlan.mockResolvedValue(accessPlan)
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))
    expect(await screen.findByRole('table', { name: 'All target model grants' })).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('moves focus into the model access review it opens', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.createPublishPlan.mockResolvedValue(accessPlan)
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))

    expect(await screen.findByRole('table', { name: 'All target model grants' })).toBeVisible()
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: 'Review model access' })).toContainElement(
        document.activeElement as HTMLElement,
      )
    })
  })

  it('moves focus into the publish plan review it opens for a legacy publication', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))

    expect(await screen.findByRole('table', { name: 'Publish plan steps' })).toBeVisible()
    await waitFor(() => {
      expect(screen.getByRole('dialog', { name: 'Publish a model' })).toContainElement(
        document.activeElement as HTMLElement,
      )
    })
  })

  it('moves focus to the result once a re-planned publication is applied, where Escape closes the dialog', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(
      await screen.findByText('Local development service reported completion; live APIM apply is not verified.'),
    ).toBeVisible()
    await waitFor(() => {
      const dialog = screen.getByRole('dialog', { name: 'Publish a model' })
      const focused = document.activeElement as HTMLElement
      expect(dialog).toContainElement(focused)
      expect(focused).not.toBe(dialog)
      expect(focused).toContainElement(screen.getByRole('table', { name: 'Publish run steps' }))
    })

    await user.keyboard('{Escape}')

    // The page stays aria-hidden for a moment after a modal closes.
    expect(await screen.findByRole('table', { name: 'Published models' })).toBeVisible()
    expect(screen.queryByRole('dialog', { hidden: true })).not.toBeInTheDocument()
  })

  it('moves focus to why MOSAIC refused the reviewed plan, above the fresh plan it made', async () => {
    const user = userEvent.setup()
    const refusal =
      'This publication changed after the plan was produced. Re-plan it and review the new changes before applying.'
    api.listPublications.mockResolvedValue([publication])
    api.createPublishPlan.mockResolvedValueOnce({ ...publishPlan, id: 'plan_2' }).mockResolvedValueOnce({ ...publishPlan, id: 'plan_3' })
    api.applyPublishPlan.mockRejectedValueOnce(new TestApiError(refusal, 409))
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(
      await screen.findByText('MOSAIC has already re-planned. Review the fresh plan below before you apply it.'),
    ).toBeVisible()
    await waitFor(() => {
      const dialog = screen.getByRole('dialog', { name: 'Publish a model' })
      const focused = document.activeElement as HTMLElement
      expect(dialog).toContainElement(focused)
      expect(focused).not.toBe(dialog)
      expect(focused).toHaveTextContent("MOSAIC didn't apply the plan you reviewed")
      expect(focused).toHaveTextContent(refusal)
      expect(focused).not.toContainElement(screen.getByRole('table', { name: 'Publish plan steps' }))
    })
  })

  it('does not describe an interrupted apply as still running or successful', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    const interrupted = publishRun({ id: 'run_2', status: 'interrupted', errors: ['Worker stopped after policy install.'] })
    api.applyPublishPlan.mockResolvedValue(interrupted)
    api.getPublishRun.mockResolvedValue(interrupted)
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))
    expect(await screen.findByText('Apply interrupted — runtime state unknown')).toBeVisible()
    expect(screen.getByText('Worker stopped after policy install.')).toBeVisible()
    expect(screen.queryByText(/Run started/)).not.toBeInTheDocument()
    expect(screen.queryByText(/reported completion|reports the model plan applied/)).not.toBeInTheDocument()
  })

  it('says why MOSAIC refused the reviewed plan and reviews the fresh one it made', async () => {
    const user = userEvent.setup()
    const refusal =
      'This publication changed after the plan was produced. Re-plan it and review the new changes before applying.'
    const freshPlan: PublishPlan = {
      ...publishPlan,
      id: 'plan_3',
      steps: [{ ...publishPlan.steps[0], action: 'update', reason: 'Replace the API that fronts this model.', existed: true }],
    }
    api.listPublications.mockResolvedValue([publication])
    api.createPublishPlan.mockResolvedValueOnce({ ...publishPlan, id: 'plan_2' }).mockResolvedValueOnce(freshPlan)
    api.applyPublishPlan.mockRejectedValueOnce(new TestApiError(refusal, 409))

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))
    await user.click(await screen.findByRole('button', { name: 'Apply plan' }))

    expect(
      await screen.findByText('MOSAIC has already re-planned. Review the fresh plan below before you apply it.'),
    ).toBeVisible()
    expect(screen.getByText("MOSAIC didn't apply the plan you reviewed")).toBeVisible()
    expect(screen.getByText(refusal)).toBeVisible()
    expect(screen.queryByText(/earlier plan was rejected/)).not.toBeInTheDocument()
    expect(screen.getByText('Replace the API that fronts this model.')).toBeVisible()
    expect(api.applyPublishPlan).toHaveBeenCalledWith('pub_1', 'plan_2')

    await user.click(screen.getByRole('button', { name: 'Apply plan' }))

    await waitFor(() => expect(api.applyPublishPlan).toHaveBeenLastCalledWith('pub_1', 'plan_3'))
  })

  it('says when API Management already matches a publication and offers nothing to apply', async () => {
    const user = userEvent.setup()
    const unchanged: PublishPlan = {
      ...publishPlan,
      id: 'plan_2',
      warnings: [],
      steps: [{ ...publishPlan.steps[0], action: 'noChange', reason: 'The API already matches this publication.', existed: true }],
    }
    api.listPublications.mockResolvedValue([publication])
    api.createPublishPlan.mockResolvedValue(unchanged)

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Re-plan' }))

    expect(
      await screen.findByText('API Management already matches this publication. Nothing to apply.'),
    ).toBeVisible()
    expect(within(screen.getByRole('table', { name: 'Publish plan steps' })).getByText('No change')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Apply plan' })).toBeDisabled()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it.each(['failed', 'rolledBack'] as const)(
    'retries a %s publication through a review of a fresh plan',
    async (status) => {
      const user = userEvent.setup()
      api.listPublications.mockResolvedValue([{ ...publication, status, lastAppliedAt: null }])
      api.createPublishPlan.mockResolvedValue({ ...publishPlan, id: 'plan_2' })

      renderPage()

      const table = await screen.findByRole('table', { name: 'Published models' })
      await user.click(within(table).getByRole('button', { name: 'Re-plan' }))
      const applyPlan = await screen.findByRole('button', { name: 'Apply plan' })
      expect(api.applyPublishPlan).not.toHaveBeenCalled()
      await user.click(applyPlan)

      await waitFor(() => expect(api.applyPublishPlan).toHaveBeenCalledWith('pub_1', 'plan_2'))
    },
  )

  it('does not claim the models page never changes API Management', async () => {
    renderPage()

    expect(
      await screen.findByText(/Reading and importing do not change Azure/i),
    ).toBeVisible()
    expect(
      screen.queryByText(/MOSAIC never changes API Management or your model resources/i),
    ).not.toBeInTheDocument()
  })

  it('asks before removing a publication record', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([{ ...publication, status: 'draft' }])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByText('Remove'))

    const dialog = await screen.findByRole('alertdialog', { name: 'Remove GPT-4o production?' })
    expect(dialog).toHaveTextContent(
      'MOSAIC deletes its record of this publication. Nothing changes in API Management.',
    )
    expect(api.deletePublication).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(api.deletePublication).not.toHaveBeenCalled()

    // The rest of the page stays hidden from assistive technology for a moment after the modal
    // dialog is gone, so the first role query outside it has to wait.
    await user.click(await within(table).findByRole('button', { name: 'Remove' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', {
        name: 'Remove publication',
      }),
    )

    await waitFor(() => expect(api.deletePublication).toHaveBeenCalledWith('pub_1'))
    expect(
      await screen.findByText(
        'Removed the publication record. Nothing changed in API Management.',
      ),
    ).toBeVisible()
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  })

  it('surfaces the conflict message when removing a publication that still owns resources', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    api.deletePublication.mockRejectedValue(
      new TestApiError('Unpublish before removing this publication.', 409),
    )

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByText('Remove'))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Remove publication' }))

    const refusal = await within(dialog).findByRole('alert')
    expect(refusal).toHaveTextContent("MOSAIC didn't remove this publication")
    expect(refusal).toHaveTextContent('Unpublish before removing this publication.')
    expect(screen.queryByText('Unable to load data')).not.toBeInTheDocument()
  })

  it('asks before unpublishing, and unpublishes only the plan it reviewed', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Unpublish' }))

    const dialog = await screen.findByRole('alertdialog', { name: 'Unpublish GPT-4o production?' })
    expect(await within(dialog).findByRole('table', { name: 'Unpublish plan steps' })).toHaveTextContent(
      'Delete the API that fronts this model. The gateway stops serving it.',
    )
    expect(api.planUnpublishPublication).toHaveBeenCalledWith('pub_1')
    expect(api.unpublishPublication).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(api.unpublishPublication).not.toHaveBeenCalled()
    // Fluent returns focus to the button that opened the dialog when it closes, as it does for Remove.
    expect(within(table).getByRole('button', { name: 'Unpublish', hidden: true })).toHaveAttribute(
      'data-tabster',
      expect.stringContaining('restorer'),
    )

    // The page stays hidden from assistive technology for a moment after the modal closes.
    await user.click(await within(table).findByRole('button', { name: 'Unpublish' }))
    const reopened = await screen.findByRole('alertdialog')
    await user.click(await within(reopened).findByRole('button', { name: 'Unpublish model' }))

    await waitFor(() => expect(api.unpublishPublication).toHaveBeenCalledWith('pub_1', 'unpublish_plan_1'))
    expect(api.unpublishPublication).toHaveBeenCalledTimes(1)
    expect(api.planUnpublishPublication).toHaveBeenCalledTimes(2)
  })

  it('shows when a publication was unpublished, and keeps Re-plan to publish it again', async () => {
    const user = userEvent.setup()
    const unpublishedAt = '2026-09-30T11:20:00Z'
    api.listPublications.mockResolvedValue([
      { ...publication, status: 'draft', unpublishedAt },
      // Unpublished before MOSAIC recorded when: a draft that was applied once and owns nothing.
      { ...publication, id: 'pub_2', displayName: 'Older unpublish', status: 'draft' },
    ])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    const [, recent, older] = within(table).getAllByRole('row')
    expect(within(recent).getByText('Unpublished')).toBeVisible()
    expect(within(recent).getByText(`Unpublished ${new Date(unpublishedAt).toLocaleString()}`)).toBeVisible()
    expect(within(older).getAllByText('Unpublished')).toHaveLength(2)
    expect(within(table).queryByText('Draft')).not.toBeInTheDocument()
    expect(within(table).queryByText(new Date(publication.lastAppliedAt!).toLocaleString())).not.toBeInTheDocument()

    await user.click(within(recent).getByRole('button', { name: 'Re-plan' }))

    await waitFor(() => expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1'))
    expect(await screen.findByRole('button', { name: 'Apply plan' })).toBeEnabled()
  })

  it('says an imported model API is hidden from the portal while its publication is unpublished', async () => {
    const unpublishedApi = { ...modelApi, id: 'modelApi_2', displayName: 'Unpublished chat', importedFromSnapshotId: null, publicationId: 'pub_1' }
    const liveApi = { ...modelApi, id: 'modelApi_3', displayName: 'Live chat', importedFromSnapshotId: null, publicationId: 'pub_2' }
    api.listModelApis.mockResolvedValue([modelApi, unpublishedApi, liveApi])
    api.listPublications.mockResolvedValue([
      { ...publication, status: 'draft', unpublishedAt: '2026-09-30T11:20:00Z' },
      {
        ...publication,
        id: 'pub_2',
        resources: [
          { kind: 'api', name: publication.apiName, resourceId: '/apis/gpt-4o-api', createdByMosaic: true, appliedAt: '2026-09-01T12:30:00Z' },
        ],
      },
    ])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Imported model APIs' })
    const note = "Hidden from the portal catalog while it isn't published."
    const rowFor = (name: string) => within(table).getByText(name).closest('tr') as HTMLElement
    expect(await within(rowFor('Unpublished chat')).findByText(note)).toBeVisible()
    expect(within(rowFor('Live chat')).queryByText(note)).not.toBeInTheDocument()
    expect(within(rowFor('Chat completions')).queryByText(note)).not.toBeInTheDocument()
  })

  it('opens the registration dialog from the shell query and clears it when closed', async () => {
    const user = userEvent.setup()
    renderPage('/models?register=1')

    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(await screen.findByLabelText(/Azure resource ID/i)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() => {
      expect(screen.getByTestId('location')).toHaveTextContent('/models')
    })
  })
})


const AI_RESOURCE_ID =
  '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-ai' +
  '/providers/Microsoft.CognitiveServices/accounts/contoso-aoai'

function modelEndpoint(overrides: Partial<ModelEndpoint> = {}): ModelEndpoint {
  return {
    id: 'endpoint_1',
    tenantId: 'tenant-test',
    name: 'Contoso models',
    provider: 'azureOpenAi',
    endpoint: 'https://contoso-aoai.openai.azure.com/',
    azureResourceId: AI_RESOURCE_ID,
    subscriptionId: '00000000-0000-0000-0000-000000000000',
    resourceGroup: 'rg-contoso-ai',
    accountName: 'contoso-aoai',
    projectName: null,
    environment: null,
    azureEnvironmentTag: null,
    environmentLabel: 'dev',
    authMode: 'managedIdentity',
    credentialReferenceId: null,
    status: 'connected',
    access: {
      canRead: true,
      evaluation: 'effectivePermissions',
      checkedAt: '2026-09-01T12:00:00Z',
      missingActions: [],
      remediation: null,
      message: 'MOSAIC can enumerate models on this endpoint.',
    },
    runtimeAccess: [],
    capabilities: {
      kind: 'OpenAI',
      skuName: 'S0',
      location: 'eastus2',
      provisioningState: 'Succeeded',
      publicNetworkAccess: 'Enabled',
      localAuthDisabled: false,
      managementApiVersion: '2024-10-01',
      notes: [],
    },
    inventory: {
      deployments: 2,
      availableModels: 3,
      succeededDeployments: 2,
      deprecatedDeployments: 0,
    },
    lastSyncedAt: '2026-09-01T12:05:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T11:00:00Z',
    updatedAt: '2026-09-01T12:05:00Z',
    ...overrides,
  }
}

function runtimeAccess(
  overrides: Partial<GatewayRuntimeAccess> = {},
): GatewayRuntimeAccess {
  return {
    gatewayId: 'gateway_1',
    gatewayName: 'Development gateway',
    apimPrincipalId: '11111111-1111-1111-1111-111111111111',
    canInvoke: false,
    evaluation: 'roleAssignments',
    checkedAt: '2026-09-01T12:00:00Z',
    requiredRoleName: 'Cognitive Services OpenAI User',
    requiredRoleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd',
    assignmentScope: null,
    inherited: false,
    remediation: {
      roleName: 'Cognitive Services OpenAI User',
      roleDefinitionId: '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd',
      scope: AI_RESOURCE_ID,
      principalId: '11111111-1111-1111-1111-111111111111',
      command: 'az role assignment create --assignee-object-id "11111111-1111-1111-1111-111111111111"',
    },
    message:
      "The gateway's managed identity does not hold Cognitive Services OpenAI User on this endpoint.",
    ...overrides,
  }
}

function suggestionView(
  overrides: Partial<ModelEndpointSuggestionView> = {},
): ModelEndpointSuggestionView {
  return {
    suggestions: [],
    scanIssues: [],
    partialScans: [],
    subscriptionsScanned: 0,
    scanStatus: 'notConfigured',
    scanMessage: null,
    scanRemediation: [],
    ...overrides,
  }
}

function gatewaySuggestion(): ModelEndpointSuggestion {
  return {
    source: 'gatewayBackend',
    endpoint: 'https://other-account.openai.azure.com/',
    azureResourceId: null,
    accountName: null,
    resourceGroup: null,
    subscriptionId: null,
    kind: null,
    location: null,
    provider: 'azureOpenAi',
    alreadyRegistered: false,
    modelEndpointId: null,
    reason: 'The gateway apim-contoso-dev routes traffic to this host.',
  }
}

function readerAtSubscription(subscriptionId: string): AccessRemediation {
  const scope = `/subscriptions/${subscriptionId}`
  return {
    roleName: 'Reader',
    roleDefinitionId: 'acdd72a7-3385-48ef-bd42-f606fba81ae7',
    scope,
    principalId: 'mosaic-mi',
    command:
      'az role assignment create --assignee-object-id "mosaic-mi"' +
      ` --assignee-principal-type ServicePrincipal --role "Reader" --scope "${scope}"`,
  }
}

const SCAN_EXPLANATION =
  'Endpoints can still be registered by pasting a resource ID, and granting Reader at ' +
  'subscription scope lets MOSAIC suggest them.'

const SCAN_ROLE_DELAY =
  'Azure can take several minutes, and occasionally longer, to apply a new role. If the scan ' +
  'still reports this right after the grant, wait a few minutes and refresh this page.'

function partialScan(subscriptionId: string, displayName: string | null): SubscriptionScanIssue {
  return {
    subscriptionId,
    displayName,
    message:
      'MOSAIC can read only some resources in this subscription, so any Azure AI resources it ' +
      'cannot read are not suggested here. Endpoints can still be registered by resource ID.',
    remediation: readerAtSubscription(subscriptionId),
  }
}

function subscriptionSuggestion(): ModelEndpointSuggestion {
  return {
    source: 'subscriptionScan',
    endpoint: 'https://contoso-aoai.openai.azure.com/',
    azureResourceId: AI_RESOURCE_ID,
    accountName: 'contoso-aoai',
    resourceGroup: 'rg-contoso-ai',
    subscriptionId: '00000000-0000-0000-0000-000000000000',
    kind: 'OpenAI',
    location: 'eastus2',
    provider: 'azureOpenAi',
    alreadyRegistered: false,
    modelEndpointId: null,
    reason: 'Found in subscription 00000000-0000-0000-0000-000000000000.',
  }
}

const PROJECT_RESOURCE_ID = `${AI_RESOURCE_ID}/projects/team-a`
const SUBSCRIPTION_SCOPE = '/subscriptions/00000000-0000-0000-0000-000000000000'
const FOUNDRY_USER = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
const OPENAI_USER = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'
const CHAT_ACTION = 'Microsoft.CognitiveServices/accounts/OpenAI/deployments/chat/completions/action'
const EMBEDDINGS_ACTION =
  'Microsoft.CognitiveServices/accounts/OpenAI/deployments/embeddings/action'

/** Matches the innermost element whose text, across child elements, is exactly `text`. */
function sentence(text: string) {
  return (_content: string, element: Element | null) =>
    element?.textContent === text &&
    Array.from(element.children).every((child) => child.textContent !== text)
}

function grantAt(scope: string, roleName: string, roleDefinitionId: string): AccessRemediation {
  return {
    roleName,
    roleDefinitionId,
    scope,
    principalId: '11111111-1111-1111-1111-111111111111',
    command:
      'az role assignment create --assignee-object-id "11111111-1111-1111-1111-111111111111"' +
      ` --assignee-principal-type ServicePrincipal --role "${roleName}" --scope "${scope}"`,
  }
}

describe('ModelsPage model endpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([gateway])
    api.listEnvironmentFindings.mockResolvedValue({
      items: [],
      limitations: [],
      generatedAt: '2026-09-01T12:00:00Z',
    })
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [
        {
          key: 'development',
          displayName: 'Development',
          description: null,
          color: 'brand',
          production: false,
          aliases: [],
          acceptsEndpointsFrom: [],
          order: 10,
          builtIn: true,
          usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
        },
      ],
      requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.listModelApis.mockResolvedValue([])
    api.listPublications.mockResolvedValue([])
    api.listModelEndpoints.mockResolvedValue([])
    api.listSuggestedModelEndpoints.mockResolvedValue(suggestionView())
    api.listModelDeployments.mockResolvedValue([])
  })

  it('states that MOSAIC reads endpoints without changing or calling them', async () => {
    renderPage()

    expect(
      await screen.findByText(/never changes them and never calls a model/i),
    ).toBeVisible()
  })

  it('invites registration when nothing is onboarded', async () => {
    renderPage()

    expect(await screen.findByText('No model endpoints yet')).toBeVisible()
  })

  it('asks before removing an endpoint and says what goes with it', async () => {
    const user = userEvent.setup()
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({ inventory: { deployments: 6, availableModels: 3, succeededDeployments: 6, deprecatedDeployments: 0 } }),
    ])
    api.deleteModelEndpoint.mockResolvedValue(undefined)

    renderPage()

    const table = await screen.findByRole('table', { name: 'Registered model endpoints' })
    await user.click(within(table).getByText('Remove'))

    const dialog = await screen.findByRole('alertdialog', { name: 'Remove Contoso models?' })
    expect(dialog).toHaveTextContent(
      'MOSAIC deletes its record of this endpoint, its 6 synced models and its sync history. ' +
        'Nothing changes in Azure: the resource and its deployments stay as they are.',
    )
    expect(api.deleteModelEndpoint).not.toHaveBeenCalled()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
    expect(api.deleteModelEndpoint).not.toHaveBeenCalled()

    // The rest of the page stays hidden from assistive technology for a moment after the modal
    // dialog is gone, so the first role query outside it has to wait.
    await user.click(await within(table).findByRole('button', { name: 'Remove' }))
    await user.click(
      within(await screen.findByRole('alertdialog')).getByRole('button', {
        name: 'Remove endpoint',
      }),
    )

    await waitFor(() => expect(api.deleteModelEndpoint).toHaveBeenCalledWith('endpoint_1'))
    expect(
      await screen.findByText('Removed Contoso models from MOSAIC. Nothing changed in Azure.'),
    ).toBeVisible()
    await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument())
  })

  it('keeps the dialog open and lists the publications when removal is refused', async () => {
    const user = userEvent.setup()
    const message =
      'Unpublish the models published from Contoso models before removing it. Their APIs ' +
      'would keep serving traffic in API Management with nothing in MOSAIC to change or ' +
      'remove them.'
    api.listModelEndpoints.mockResolvedValue([modelEndpoint()])
    api.deleteModelEndpoint.mockRejectedValue(
      new TestApiError(message, 409, {
        message,
        details: {
          id: 'endpoint_1',
          name: 'Contoso models',
          publications: [
            { id: 'pub_1', displayName: 'GPT-4o production', status: 'published', gatewayId: 'gateway_1' },
            { id: 'pub_2', displayName: 'Embeddings', status: 'failed', gatewayId: 'gateway_1' },
          ],
        },
      }),
    )

    renderPage()

    const table = await screen.findByRole('table', { name: 'Registered model endpoints' })
    await user.click(within(table).getByText('Remove'))
    const dialog = await screen.findByRole('alertdialog')
    await user.click(within(dialog).getByRole('button', { name: 'Remove endpoint' }))

    const refusal = await within(dialog).findByRole('alert')
    // Queries within a dialog that has closed still find its detached content, so check it's still
    // the open dialog on the page.
    expect(screen.getByRole('alertdialog', { name: 'Remove Contoso models?' })).toBe(dialog)
    expect(refusal).toHaveTextContent("MOSAIC didn't remove this endpoint")
    expect(refusal).toHaveTextContent(message)
    const blocking = within(dialog).getByRole('list', { name: 'Publications blocking removal' })
    expect(
      within(blocking)
        .getAllByRole('listitem')
        .map((item) => item.textContent),
    ).toEqual(['GPT-4o production (Published)', 'Embeddings (Failed)'])
    expect(screen.queryByText('Unable to load data')).not.toBeInTheDocument()
    // The open modal dialog hides the rest of the page from assistive technology, so include hidden
    // elements to find the table behind it.
    expect(
      screen.getByRole('table', { name: 'Registered model endpoints', hidden: true }),
    ).toHaveTextContent('Contoso models')
  })

  it('shows why the Register dialog was refused', async () => {
    const user = userEvent.setup()
    const message =
      "MOSAIC already lists this resource's models through Team A project, a Foundry project " +
      "on it. A Foundry project's models are deployed on its parent resource, so registering " +
      'both would list every deployment twice.'
    api.registerModelEndpoint.mockRejectedValue(
      new TestApiError(message, 409, {
        message,
        details: { id: 'endpoint_project', name: 'Team A project' },
      }),
    )

    renderPage('/models?register=1')

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByLabelText(/Azure resource ID/i))
    await user.paste(AI_RESOURCE_ID)
    await user.click(within(dialog).getByRole('button', { name: 'Register' }))

    await waitFor(() => expect(api.registerModelEndpoint).toHaveBeenCalled())
    expect(api.registerModelEndpoint.mock.calls[0][0]).toMatchObject({
      azureResourceId: AI_RESOURCE_ID,
    })
    expect(await within(dialog).findByText(message)).toBeVisible()
    expect(within(dialog).getByText("MOSAIC didn't register this endpoint")).toBeVisible()
    expect(screen.queryByText('Unable to load data')).not.toBeInTheDocument()
  })

  it("opens a suggestion's registration dialog with the suggested environment", async () => {
    const user = userEvent.setup()
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({
        suggestions: [
          {
            source: 'subscriptionScan',
            endpoint: 'https://contoso-aoai.openai.azure.com/',
            azureResourceId: AI_RESOURCE_ID,
            accountName: 'contoso-aoai',
            resourceGroup: 'rg-contoso-ai',
            subscriptionId: '00000000-0000-0000-0000-000000000000',
            kind: 'OpenAI',
            location: 'eastus2',
            provider: 'azureOpenAi',
            alreadyRegistered: false,
            modelEndpointId: null,
            reason: 'Found in subscription 00000000-0000-0000-0000-000000000000.',
            azureEnvironmentTag: 'dev',
            suggestedEnvironment: 'development',
          },
        ],
        subscriptionsScanned: 1,
        scanStatus: 'scanned',
      }),
    )

    renderPage()

    const heading = await screen.findByRole('heading', { name: 'Endpoints MOSAIC found' })
    const card = heading.closest('.fui-Card') as HTMLElement
    await user.click(within(card).getByRole('button', { name: 'Register' }))

    const dialog = await screen.findByRole('dialog', { name: 'Register model endpoint' })
    expect(within(dialog).getByDisplayValue(AI_RESOURCE_ID)).toBeVisible()
    expect(within(dialog).getByText(/Suggested: Development/)).toBeVisible()
    expect(api.registerModelEndpoint).not.toHaveBeenCalled()
  })

  it('lists registered endpoints with their discovered model count', async () => {
    api.listModelEndpoints.mockResolvedValue([modelEndpoint()])

    renderPage()

    expect(await screen.findByText('Contoso models')).toBeVisible()
    expect(screen.getByText('Azure OpenAI')).toBeVisible()
    expect(screen.getByText('Connected')).toBeVisible()
  })

  it('separates MOSAIC read access from gateway runtime access', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({ runtimeAccess: [runtimeAccess()] }),
    ])

    renderPage()

    // MOSAIC being able to read the endpoint must not imply the gateway can call it.
    expect(await screen.findByText('MOSAIC can read this endpoint')).toBeVisible()
    expect(
      screen.getByText(/does not hold Cognitive Services OpenAI User/i),
    ).toBeVisible()
  })

  it('offers a runnable command when the gateway is missing its role', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({ runtimeAccess: [runtimeAccess()] }),
    ])

    renderPage()

    const command = await screen.findByText(/az role assignment create/)
    expect(command.textContent).toContain('11111111-1111-1111-1111-111111111111')
  })

  it('labels an inherited assignment rather than showing it as direct', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        runtimeAccess: [
          runtimeAccess({
            canInvoke: true,
            inherited: true,
            assignmentScope: '/subscriptions/00000000-0000-0000-0000-000000000000',
            remediation: null,
            message: 'Holds the role through an inherited assignment.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText(/Inherited from/)).toBeVisible()
  })

  it('does not report a denial when access could not be evaluated', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        runtimeAccess: [
          runtimeAccess({
            evaluation: 'notEvaluated',
            message: 'MOSAIC cannot confirm whether the gateway can call it.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText(/cannot confirm/i)).toBeVisible()
  })

  it('names the role that satisfied the check and where it is assigned', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        runtimeAccess: [
          runtimeAccess({
            canInvoke: true,
            reason: 'granted',
            // Any sufficient role counts, not only the recommended one.
            grantedRoleName: 'Cognitive Services User',
            grantedRoleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908',
            assignmentScope: AI_RESOURCE_ID,
            evaluatedScope: AI_RESOURCE_ID,
            requiredDataActions: [CHAT_ACTION],
            roleFindings: [
              {
                kind: 'sufficient',
                roleName: 'Cognitive Services User',
                roleDefinitionId: 'a97b65f3-24c7-4388-baec-2e87135dc908',
                scope: AI_RESOURCE_ID,
                inherited: false,
                missingDataActions: [],
              },
            ],
            remediation: null,
            message:
              "The gateway's managed identity holds Cognitive Services User on this resource, " +
              'which covers every operation MOSAIC publishes from it.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText('Development gateway: can invoke')).toBeVisible()
    expect(
      screen.getByText(
        sentence('Satisfied by Cognitive Services User, assigned directly on contoso-aoai.'),
      ),
    ).toBeVisible()
    expect(screen.queryByText('Role assignments MOSAIC found')).not.toBeInTheDocument()
    expect(screen.queryByText(/Recommended: grant/)).not.toBeInTheDocument()
    // Registered at the account, so the scope checked is the scope registered.
    expect(screen.queryByText(/Checked at/)).not.toBeInTheDocument()
  })

  it('reports a grant inherited from a broader scope as inherited', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        runtimeAccess: [
          runtimeAccess({
            canInvoke: true,
            reason: 'granted',
            grantedRoleName: 'Cognitive Services OpenAI User',
            grantedRoleDefinitionId: OPENAI_USER,
            assignmentScope: SUBSCRIPTION_SCOPE,
            inherited: true,
            evaluatedScope: AI_RESOURCE_ID,
            remediation: null,
            message: 'Holds the role through an inherited assignment.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(
      await screen.findByText(
        sentence(
          'Satisfied by Cognitive Services OpenAI User, inherited from subscription ' +
            '00000000-0000-0000-0000-000000000000. It works, but it is broader than an ' +
            'assignment made directly on the resource.',
        ),
      ),
    ).toBeVisible()
    // The short name is for reading; the full scope stays available.
    expect(screen.getByTitle(SUBSCRIPTION_SCOPE)).toHaveTextContent(
      'subscription 00000000-0000-0000-0000-000000000000',
    )
  })

  it('explains that a project-scoped grant does not reach the parent resource', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        provider: 'azureAiFoundry',
        azureResourceId: PROJECT_RESOURCE_ID,
        projectName: 'team-a',
        capabilities: {
          ...modelEndpoint().capabilities,
          kind: 'AIServices',
        },
        runtimeAccess: [
          runtimeAccess({
            reason: 'narrowerScope',
            requiredRoleName: 'Foundry User',
            requiredRoleDefinitionId: FOUNDRY_USER,
            evaluatedScope: AI_RESOURCE_ID,
            requiredDataActions: [
              'Microsoft.CognitiveServices/accounts/MaaS/chat/completions/action',
              CHAT_ACTION,
            ],
            roleFindings: [
              {
                kind: 'narrowerScope',
                roleName: 'Foundry User',
                roleDefinitionId: FOUNDRY_USER,
                scope: PROJECT_RESOURCE_ID,
                inherited: false,
                missingDataActions: [],
              },
            ],
            remediation: grantAt(AI_RESOURCE_ID, 'Foundry User', FOUNDRY_USER),
            message:
              "The gateway's managed identity holds Foundry User at the project, which is " +
              'narrower than what the published API needs.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText('Development gateway: cannot invoke')).toBeVisible()
    expect(
      screen.getByText(
        sentence(
          'Checked at contoso-aoai, the resource the published API calls. ' +
            "A Foundry project's models are deployed on its parent resource.",
        ),
      ),
    ).toBeVisible()
    expect(
      screen.getByText(
        'Foundry User at project team-a is assigned below the resource the published API ' +
          'calls, so it does not apply there.',
      ),
    ).toBeVisible()
    expect(
      screen.getByText(
        sentence(
          'Recommended: grant Foundry User on contoso-aoai. Any role that grants the data ' +
            'actions the published API needs is also accepted. Someone with permission to ' +
            'assign roles must run:',
        ),
      ),
    ).toBeVisible()
    expect(screen.getByText(/--scope "[^"]*\/accounts\/contoso-aoai"$/)).toBeInTheDocument()
    expect(screen.getByText('Data actions the published API needs')).toBeVisible()
  })

  it('names the data actions a held role does not grant', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        runtimeAccess: [
          runtimeAccess({
            reason: 'missingRole',
            evaluatedScope: AI_RESOURCE_ID,
            requiredDataActions: [CHAT_ACTION, EMBEDDINGS_ACTION],
            roleFindings: [
              {
                kind: 'insufficient',
                roleName: 'Reader',
                roleDefinitionId: 'acdd72a7-3385-48ef-bd42-f606fba81ae7',
                scope: AI_RESOURCE_ID,
                inherited: false,
                missingDataActions: [CHAT_ACTION, EMBEDDINGS_ACTION],
              },
            ],
            remediation: grantAt(AI_RESOURCE_ID, 'Cognitive Services OpenAI User', OPENAI_USER),
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText('Development gateway: cannot invoke')).toBeVisible()
    expect(screen.getByText('Role assignments MOSAIC found')).toBeVisible()
    expect(
      screen.getByText(`Reader at contoso-aoai does not grant ${CHAT_ACTION} and 1 more.`),
    ).toBeVisible()
  })

  it('does not count a conditional assignment as access, nor report it as a denial', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        runtimeAccess: [
          runtimeAccess({
            evaluation: 'notEvaluated',
            reason: 'conditional',
            evaluatedScope: AI_RESOURCE_ID,
            roleFindings: [
              {
                kind: 'conditional',
                roleName: 'Cognitive Services OpenAI User',
                roleDefinitionId: OPENAI_USER,
                scope: AI_RESOURCE_ID,
                inherited: false,
                missingDataActions: [],
              },
            ],
            remediation: grantAt(AI_RESOURCE_ID, 'Cognitive Services OpenAI User', OPENAI_USER),
            message: 'Holds the role only under an ABAC condition MOSAIC cannot prove holds.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText('Development gateway: not confirmed')).toBeVisible()
    expect(screen.queryByText(/can invoke|cannot invoke/)).not.toBeInTheDocument()
    expect(
      screen.getByText(
        'Cognitive Services OpenAI User at contoso-aoai is assigned under an ABAC condition ' +
          'MOSAIC cannot prove holds for these calls, so it is not counted as access.',
      ),
    ).toBeVisible()
  })

  it('reports a gateway with no network path as unable to invoke, whatever its role', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        capabilities: {
          ...modelEndpoint().capabilities,
          publicNetworkAccess: 'Disabled',
        },
        runtimeAccess: [
          runtimeAccess({
            reason: 'networkUnreachable',
            grantedRoleName: 'Cognitive Services OpenAI User',
            grantedRoleDefinitionId: OPENAI_USER,
            assignmentScope: AI_RESOURCE_ID,
            evaluatedScope: AI_RESOURCE_ID,
            networkReachability: 'unreachable',
            roleFindings: [
              {
                kind: 'sufficient',
                roleName: 'Cognitive Services OpenAI User',
                roleDefinitionId: OPENAI_USER,
                scope: AI_RESOURCE_ID,
                inherited: false,
                missingDataActions: [],
              },
            ],
            remediation: null,
            message:
              'Public network access to this resource is disabled, and Development gateway is ' +
              'not connected to a virtual network, so it has no network path to the resource ' +
              'whatever roles it holds.',
          }),
        ],
      }),
    ])

    renderPage()

    expect(await screen.findByText('Development gateway: cannot invoke')).toBeVisible()
    expect(screen.getByText(/has no network path to the resource/)).toBeVisible()
    // The role is not what is missing, so MOSAIC neither lists findings nor recommends one.
    expect(
      screen.getByText(
        sentence(
          'The role requirement is met by Cognitive Services OpenAI User, assigned directly on ' +
            'contoso-aoai.',
        ),
      ),
    ).toBeVisible()
    expect(screen.queryByText('Role assignments MOSAIC found')).not.toBeInTheDocument()
    expect(screen.queryByText(/Recommended: grant/)).not.toBeInTheDocument()
  })

  it('shows the endpoint settings that decide whether a gateway can reach it', async () => {
    const keyNote =
      'Key authentication is enabled on this endpoint. Disabling it forces callers, including ' +
      'the gateway, onto managed identity.'
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        capabilities: {
          ...modelEndpoint().capabilities,
          networkDefaultAction: 'Deny',
          networkIpRules: ['203.0.113.0/24'],
          networkVirtualNetworkRuleCount: 2,
          notes: [keyNote],
        },
      }),
    ])

    renderPage()

    const settings = await screen.findByRole('region', { name: 'Endpoint settings' })
    const fact = (label: string) => within(settings).getByText(label).nextElementSibling
    expect(fact('Resource kind')).toHaveTextContent('OpenAI')
    expect(fact('Public network access')).toHaveTextContent('Enabled')
    expect(fact('Key authentication')).toHaveTextContent('Enabled')
    expect(fact('Firewall')).toHaveTextContent(
      'Admits only listed networks (1 address rule, 2 virtual network rules)',
    )
    expect(within(settings).getByText(keyNote)).toBeVisible()
  })

  it('says when the resource kind is not known yet', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        capabilities: {
          managementApiVersion: '2024-10-01',
          notes: [],
        },
      }),
    ])

    renderPage()

    const settings = await screen.findByRole('region', { name: 'Endpoint settings' })
    expect(
      within(settings).getByText('Not known yet. MOSAIC cannot read this resource.'),
    ).toBeVisible()
  })

  it('renders discovered deployments', async () => {
    api.listModelEndpoints.mockResolvedValue([modelEndpoint()])
    api.listModelDeployments.mockResolvedValue([
      {
        id: 'obsdeployment_1',
        endpointId: 'endpoint_1',
        deploymentName: 'gpt-4o-prod',
        modelName: 'gpt-4o',
        modelVersion: '2024-11-20',
        modelFormat: 'OpenAI',
        modelPublisher: 'OpenAI',
        skuName: 'Standard',
        skuCapacity: 50,
        provisioningState: 'Succeeded',
        raiPolicyName: 'Microsoft.DefaultV2',
        capabilities: { chatCompletion: 'true' },
        requestPaths: ['/chat/completions'],
        observedAt: '2026-09-01T12:05:00Z',
      },
    ])

    renderPage()

    expect(await screen.findByText('gpt-4o-prod')).toBeVisible()
    expect(screen.getByText('gpt-4o')).toBeVisible()
    expect(screen.getByText('/chat/completions')).toBeVisible()
  })

  it('shows MOSAIC remediation when it cannot read the endpoint', async () => {
    api.listModelEndpoints.mockResolvedValue([
      modelEndpoint({
        status: 'unauthorized',
        access: {
          canRead: false,
          evaluation: 'effectivePermissions',
          checkedAt: '2026-09-01T12:00:00Z',
          missingActions: ['Microsoft.CognitiveServices/accounts/deployments/read'],
          remediation: {
            roleName: 'Reader',
            roleDefinitionId: 'acdd72a7-3385-48ef-bd42-f606fba81ae7',
            scope: AI_RESOURCE_ID,
            principalId: 'mosaic-mi',
            command: 'az role assignment create --role "Reader"',
            customRoleDefinition: { properties: { roleName: 'MOSAIC Model Deployment Reader' } },
          },
          message:
            "MOSAIC's managed identity is missing permissions needed to enumerate models on " +
            'this endpoint. Grant it the role shown below. Azure can take several minutes, and ' +
            'occasionally longer, to apply a new role. If Check access still fails right after ' +
            'the grant, wait a few minutes and try again.',
        },
      }),
    ])

    renderPage()

    expect(await screen.findByText('MOSAIC cannot read this endpoint')).toBeVisible()
    // The API's message carries the advice to wait, since a new role is rarely applied at once.
    expect(screen.getByText(/If Check access still fails right after the grant/)).toBeVisible()
    expect(screen.getByText(/without also granting/i)).toBeVisible()
  })

  it('surfaces suggestions and labels where each came from', async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({
        suggestions: [
          gatewaySuggestion(),
          {
            source: 'subscriptionScan',
            endpoint: 'https://contoso-aoai.openai.azure.com/',
            azureResourceId: AI_RESOURCE_ID,
            accountName: 'contoso-aoai',
            resourceGroup: 'rg-contoso-ai',
            subscriptionId: '00000000-0000-0000-0000-000000000000',
            kind: 'OpenAI',
            location: 'eastus2',
            provider: 'azureOpenAi',
            alreadyRegistered: false,
            modelEndpointId: null,
            reason: 'Found in subscription 00000000-0000-0000-0000-000000000000.',
          },
        ],
        subscriptionsScanned: 1,
        scanStatus: 'scanned',
      }),
    )

    renderPage()

    expect(await screen.findByText('Used by a gateway')).toBeVisible()
    expect(screen.getByText('Found in a subscription')).toBeVisible()
    // A hostname alone cannot be registered, so no action is offered for it.
    expect(screen.getByText('Needs a resource ID')).toBeVisible()
    expect(screen.getByText('Scanned 1 subscription.')).toBeVisible()
  })

  it('explains a subscription it could not scan instead of hiding it', async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({
        scanIssues: [
          {
            subscriptionId: '00000000-0000-0000-0000-000000000000',
            displayName: 'Contoso dev',
            message: 'MOSAIC could not list Azure AI resources in this subscription.',
            remediation: {
              roleName: 'Reader',
              roleDefinitionId: 'acdd72a7-3385-48ef-bd42-f606fba81ae7',
              scope: '/subscriptions/00000000-0000-0000-0000-000000000000',
              principalId: 'mosaic-mi',
              command: 'az role assignment create --role "Reader" --scope "/subscriptions/x"',
            },
          },
        ],
        subscriptionsScanned: 0,
        scanStatus: 'scanned',
      }),
    )

    renderPage()

    const heading = await screen.findByText('Subscriptions MOSAIC could not scan')
    expect(heading).toBeVisible()
    expect(screen.getByText(/az role assignment create/)).toBeVisible()
    const card = heading.closest('.fui-Card') as HTMLElement
    expect(within(card).getByText(SCAN_ROLE_DELAY)).toBeVisible()
    // Scanning none of them is already what the card above says, so no count restates it.
    expect(screen.queryByText('Endpoints MOSAIC found')).not.toBeInTheDocument()
  })

  it("explains that MOSAIC can't see any subscriptions and offers Reader on each", async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({
        scanStatus: 'noVisibleSubscriptions',
        scanRemediation: [
          readerAtSubscription('11111111-2222-3333-4444-555555555555'),
          readerAtSubscription('66666666-7777-8888-9999-000000000000'),
        ],
      }),
    )

    renderPage()

    const heading = await screen.findByRole('heading', {
      name: "MOSAIC can't see any subscriptions",
    })
    const card = heading.closest('.fui-Card') as HTMLElement
    expect(card).toHaveTextContent(SCAN_EXPLANATION)
    expect(
      within(card)
        .getAllByText(/az role assignment create/)
        .map((command) => command.textContent),
    ).toEqual([
      readerAtSubscription('11111111-2222-3333-4444-555555555555').command,
      readerAtSubscription('66666666-7777-8888-9999-000000000000').command,
    ])
    expect(within(card).getAllByRole('button', { name: 'Copy command' })).toHaveLength(2)
    // Said once for the card, not once per command.
    expect(within(card).getAllByText(SCAN_ROLE_DELAY)).toHaveLength(1)
    expect(
      screen.queryByRole('heading', { name: "MOSAIC couldn't list subscriptions" }),
    ).not.toBeInTheDocument()
  })

  it("explains why MOSAIC couldn't list subscriptions", async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({
        scanStatus: 'listFailed',
        scanMessage: "MOSAIC's identity is not authorized for this Azure resource.",
        scanRemediation: [readerAtSubscription('<subscription-id>')],
      }),
    )

    renderPage()

    const heading = await screen.findByRole('heading', {
      name: "MOSAIC couldn't list subscriptions",
    })
    const card = heading.closest('.fui-Card') as HTMLElement
    expect(
      within(card).getByText("MOSAIC's identity is not authorized for this Azure resource."),
    ).toBeVisible()
    expect(card).toHaveTextContent(SCAN_EXPLANATION)
    expect(
      within(card).getByText(/--scope "\/subscriptions\/<subscription-id>"/),
    ).toBeVisible()
    expect(within(card).getByText(SCAN_ROLE_DELAY)).toBeVisible()
    expect(
      screen.queryByRole('heading', { name: "MOSAIC can't see any subscriptions" }),
    ).not.toBeInTheDocument()
  })

  it('reports how many subscriptions it scanned even when nothing is new', async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({ scanStatus: 'scanned', subscriptionsScanned: 3 }),
    )

    renderPage()

    expect(
      await screen.findByText('Scanned 3 subscriptions. Nothing new to register.'),
    ).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Endpoints MOSAIC found' })).toBeVisible()
    // Nothing asks for a grant, so there is nothing to wait for.
    expect(screen.queryByText(SCAN_ROLE_DELAY)).not.toBeInTheDocument()
  })

  it("says MOSAIC can read only part of a subscription instead of 'nothing new'", async () => {
    // Observed live: ARM answered with no accounts because it had silently left out every account
    // MOSAIC could not read.
    const subscriptionId = '00000000-0000-0000-0000-000000000000'
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({
        scanStatus: 'scanned',
        subscriptionsScanned: 1,
        partialScans: [partialScan(subscriptionId, 'Contoso dev')],
      }),
    )

    renderPage()

    expect(
      await screen.findByText(
        'Scanned 1 subscription. MOSAIC can read only some resources in it, so any Azure AI ' +
          "resources it can't read are missing from this list.",
      ),
    ).toBeVisible()
    expect(screen.queryByText(/Nothing new to register/)).not.toBeInTheDocument()
    const heading = screen.getByRole('heading', {
      name: 'MOSAIC can read only part of a subscription',
    })
    const card = heading.closest('.fui-Card') as HTMLElement
    expect(card).toHaveTextContent(SCAN_EXPLANATION)
    expect(within(card).getByText('Contoso dev')).toBeVisible()
    expect(
      within(card).getByText(readerAtSubscription(subscriptionId).command),
    ).toBeVisible()
    expect(within(card).getAllByRole('button', { name: 'Copy command' })).toHaveLength(1)
    expect(within(card).getByText(SCAN_ROLE_DELAY)).toBeVisible()
    // It was listed, so it is not among the subscriptions MOSAIC could not scan.
    expect(
      screen.queryByRole('heading', { name: 'Subscriptions MOSAIC could not scan' }),
    ).not.toBeInTheDocument()
  })

  it.each([
    [2, 1, 'in 1 of them'],
    [3, 2, 'in 2 of them'],
    [2, 2, 'in each of them'],
  ])(
    'counts %i scanned subscriptions with %i readable only in part',
    async (scanned, partial, where) => {
      const partialScans = [
        partialScan('11111111-2222-3333-4444-555555555555', 'Contoso prod'),
        partialScan('66666666-7777-8888-9999-000000000000', null),
      ].slice(0, partial)
      api.listSuggestedModelEndpoints.mockResolvedValue(
        suggestionView({
          suggestions: [subscriptionSuggestion()],
          scanStatus: 'scanned',
          subscriptionsScanned: scanned,
          partialScans,
        }),
      )

      renderPage()

      expect(
        await screen.findByText(
          `Scanned ${scanned} subscriptions. MOSAIC can read only some resources ${where}, so ` +
            "any Azure AI resources it can't read are missing from this list.",
        ),
      ).toBeVisible()
      // What MOSAIC could read is still offered.
      expect(screen.getByRole('button', { name: 'Register' })).toBeVisible()
      const heading = screen.getByRole('heading', {
        name:
          partial === 1
            ? 'MOSAIC can read only part of a subscription'
            : `MOSAIC can read only part of ${partial} subscriptions`,
      })
      const card = heading.closest('.fui-Card') as HTMLElement
      expect(
        within(card)
          .getAllByText(/az role assignment create/)
          .map((command) => command.textContent),
      ).toEqual(partialScans.map((scan) => scan.remediation?.command))
      expect(within(card).getAllByRole('button', { name: 'Copy command' })).toHaveLength(
        partial,
      )
      // Said once for the card, however many subscriptions it lists.
      expect(within(card).getAllByText(SCAN_ROLE_DELAY)).toHaveLength(1)
      // A subscription without a display name is named by its ID.
      if (partial === 2) {
        expect(within(card).getByText('66666666-7777-8888-9999-000000000000')).toBeVisible()
      }
    },
  )

  it('says nothing about subscriptions when the scan is not configured', async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue(
      suggestionView({ suggestions: [gatewaySuggestion()], scanStatus: 'notConfigured' }),
    )

    renderPage()

    // The gateway suggestion proves the view arrived before asserting what it left out.
    expect(await screen.findByText('Used by a gateway')).toBeVisible()
    expect(screen.queryByText(/^Scanned \d+ subscription/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /subscriptions/i })).not.toBeInTheDocument()
  })

  describe('an Azure endpoint reached with an API key', () => {
    const PROJECT_URL =
      'https://fabrikam-foundry.services.ai.azure.com/api/projects/partner-models'
    const SECRET_URI = 'https://kv-contoso-ai.vault.azure.net/secrets/fabrikam-foundry-key'
    const VAULT_ID =
      '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-ai' +
      '/providers/Microsoft.KeyVault/vaults/kv-contoso-ai'
    const GRANT_COMMAND =
      'az role assignment create --assignee-object-id "11111111-1111-1111-1111-111111111111"' +
      ' --assignee-principal-type ServicePrincipal --role "Key Vault Secrets User"' +
      ` --scope "${VAULT_ID}"`

    function keyEndpoint(overrides: Partial<ModelEndpoint> = {}): ModelEndpoint {
      return modelEndpoint({
        id: 'endpoint_key',
        name: 'Fabrikam partner Foundry',
        provider: 'azureAiFoundry',
        endpoint: 'https://fabrikam-foundry.services.ai.azure.com/',
        azureResourceId: null,
        subscriptionId: null,
        resourceGroup: null,
        accountName: 'fabrikam-foundry',
        projectName: 'partner-models',
        authMode: 'apiKey',
        credentialReferenceId: 'credential_1',
        declaredDeployments: [
          {
            deploymentName: 'claude-sonnet-4-5',
            modelName: 'claude-sonnet-4-5',
            apiShape: 'anthropicMessages',
            declaredAt: '2026-09-01T12:00:00Z',
            declaredBy: 'admin-object-id',
          },
        ],
        access: {
          canRead: true,
          evaluation: 'probe',
          checkedAt: '2026-09-01T12:00:00Z',
          missingActions: [],
          remediation: null,
          message: 'MOSAIC read the API key from Key Vault and checked it with a request that runs no model.',
        },
        runtimeAccess: [
          runtimeAccess({
            reason: 'missingRole',
            requiredRoleName: 'Key Vault Secrets User',
            requiredRoleDefinitionId: '4633458b-17de-408a-b874-0445c86b69e6',
            evaluatedScope: VAULT_ID,
            requiredDataActions: ['Microsoft.KeyVault/vaults/secrets/getSecret/action'],
            remediation: {
              roleName: 'Key Vault Secrets User',
              roleDefinitionId: '4633458b-17de-408a-b874-0445c86b69e6',
              scope: VAULT_ID,
              principalId: '11111111-1111-1111-1111-111111111111',
              command: GRANT_COMMAND,
            },
            message:
              "Development gateway's managed identity holds no role on Key Vault kv-contoso-ai " +
              "that reads secrets, so the gateway can't read this endpoint's API key.",
          }),
        ],
        capabilities: { managementApiVersion: '2024-10-01', notes: [] },
        inventory: {
          deployments: 0,
          availableModels: 0,
          succeededDeployments: 0,
          deprecatedDeployments: 0,
        },
        lastSyncedAt: null,
        ...overrides,
      })
    }

    async function openKeyTab(user: ReturnType<typeof userEvent.setup>) {
      renderPage('/models?register=1')
      const dialog = await screen.findByRole('dialog', { name: 'Register model endpoint' })
      await user.click(within(dialog).getByRole('tab', { name: 'Azure AI with an API key' }))
      return dialog
    }

    // Fictional, and recognisable, so a test can tell where it went.
    const API_KEY = 'fictional-key-VALUE-for-tests-1234'

    it('offers the key path as an explicit alternative to the resource ID', async () => {
      const user = userEvent.setup()
      const dialog = await openKeyTab(user)

      expect(
        within(dialog).getByText("Only when MOSAIC can't reach the resource"),
      ).toBeVisible()
      expect(within(dialog).getByText(/for example when the resource is in another/)).toBeVisible()
      expect(within(dialog).getByLabelText(/Endpoint URL/)).toBeVisible()
      expect(within(dialog).queryByLabelText(/Azure resource ID/)).not.toBeInTheDocument()
      // Pasting the key is the default; a key already in Key Vault is the alternative.
      expect(within(dialog).getByRole('radio', { name: 'Paste the API key' })).toBeChecked()
      const key = within(dialog).getByLabelText(/^API key/)
      expect(key).toHaveAttribute('type', 'password')
      expect(key).toHaveAttribute('autocomplete', 'new-password')
      expect(within(dialog).getByText(/stores it as a secret in its own Key Vault/)).toBeVisible()
      expect(within(dialog).getByText(/never shows the key again/)).toBeVisible()
      expect(within(dialog).queryByLabelText(/Key Vault secret URI/)).not.toBeInTheDocument()

      await user.click(within(dialog).getByRole('radio', { name: 'Use a key already in Key Vault' }))

      expect(within(dialog).getByLabelText(/Key Vault secret URI/)).toBeVisible()
      expect(within(dialog).getByText(/The secret you stored the resource's API key in/)).toBeVisible()
      expect(within(dialog).queryByLabelText(/^API key/)).not.toBeInTheDocument()
    })

    it('registers the URL, the secret URI and the declared deployments', async () => {
      const user = userEvent.setup()
      api.registerModelEndpoint.mockResolvedValue(keyEndpoint())
      const dialog = await openKeyTab(user)

      await user.click(within(dialog).getByLabelText(/Endpoint URL/))
      await user.paste(PROJECT_URL)
      await user.click(within(dialog).getByRole('radio', { name: 'Use a key already in Key Vault' }))
      await user.click(within(dialog).getByLabelText(/Key Vault secret URI/))
      await user.paste(SECRET_URI)
      await user.type(within(dialog).getByLabelText('Deployment 1 name'), 'claude-sonnet-4-5')
      await user.type(within(dialog).getByLabelText('Deployment 1 model'), 'claude-sonnet-4-5')
      // A Claude model takes the Anthropic Messages API unless the administrator says otherwise.
      expect(within(dialog).getByLabelText('Deployment 1 API')).toHaveValue('anthropicMessages')
      await user.click(within(dialog).getByRole('button', { name: 'Add a deployment' }))
      await user.type(within(dialog).getByLabelText('Deployment 2 name'), 'gpt-4-1')
      await user.type(within(dialog).getByLabelText('Deployment 2 model'), 'gpt-4.1')
      await user.selectOptions(within(dialog).getByLabelText('Deployment 2 API'), 'azureOpenAi')
      await user.click(within(dialog).getByRole('button', { name: 'Add a deployment' }))
      await user.click(within(dialog).getByRole('button', { name: 'Remove deployment 3' }))
      await user.click(within(dialog).getByRole('button', { name: 'Register' }))

      await waitFor(() => expect(api.registerModelEndpoint).toHaveBeenCalledTimes(1))
      expect(api.registerModelEndpoint.mock.calls[0][0]).toEqual({
        endpoint: PROJECT_URL,
        credentialSecretUri: SECRET_URI,
        name: undefined,
        environment: 'development',
        deployments: [
          {
            deploymentName: 'claude-sonnet-4-5',
            modelName: 'claude-sonnet-4-5',
            apiShape: 'anthropicMessages',
          },
          { deploymentName: 'gpt-4-1', modelName: 'gpt-4.1', apiShape: 'azureOpenAi' },
        ],
      })
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    })

    it('registers a pasted key, and never keeps it in the form', async () => {
      const user = userEvent.setup()
      api.registerModelEndpoint.mockResolvedValue(keyEndpoint({ keyStoredByMosaic: true }))
      const dialog = await openKeyTab(user)

      await user.click(within(dialog).getByLabelText(/Endpoint URL/))
      await user.paste(PROJECT_URL)
      await user.click(within(dialog).getByLabelText(/^API key/))
      // A key copied from the portal often brings a space or line break along.
      await user.paste(`  ${API_KEY}\n`)
      await user.type(within(dialog).getByLabelText('Deployment 1 name'), 'claude-sonnet-4-5')
      await user.type(within(dialog).getByLabelText('Deployment 1 model'), 'claude-sonnet-4-5')
      await user.click(within(dialog).getByRole('button', { name: 'Register' }))

      await waitFor(() => expect(api.registerModelEndpoint).toHaveBeenCalledTimes(1))
      expect(api.registerModelEndpoint.mock.calls[0][0]).toEqual({
        endpoint: PROJECT_URL,
        apiKey: API_KEY,
        name: undefined,
        environment: 'development',
        deployments: [
          {
            deploymentName: 'claude-sonnet-4-5',
            modelName: 'claude-sonnet-4-5',
            apiShape: 'anthropicMessages',
          },
        ],
      })
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

      await user.click(await screen.findByRole('button', { name: 'Register endpoint' }))
      const reopened = await screen.findByRole('dialog', { name: 'Register model endpoint' })
      await user.click(within(reopened).getByRole('tab', { name: 'Azure AI with an API key' }))
      expect(within(reopened).getByLabelText(/^API key/)).toHaveValue('')
    })

    it('asks for the key before registering', async () => {
      const user = userEvent.setup()
      const dialog = await openKeyTab(user)

      await user.click(within(dialog).getByLabelText(/Endpoint URL/))
      await user.paste(PROJECT_URL)
      // An empty field is caught by the form itself; one holding only spaces by MOSAIC.
      expect(within(dialog).getByLabelText(/^API key/)).toBeRequired()
      await user.click(within(dialog).getByLabelText(/^API key/))
      await user.paste('   ')
      await user.click(within(dialog).getByRole('button', { name: 'Register' }))

      expect(await within(dialog).findByText("Paste the resource's API key.")).toBeVisible()
      expect(api.registerModelEndpoint).not.toHaveBeenCalled()
    })

    it('forgets a key typed into a dialog that was cancelled', async () => {
      const user = userEvent.setup()
      const dialog = await openKeyTab(user)

      await user.click(within(dialog).getByLabelText(/^API key/))
      await user.paste(API_KEY)
      await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

      await user.click(await screen.findByRole('button', { name: 'Register endpoint' }))
      const reopened = await screen.findByRole('dialog', { name: 'Register model endpoint' })
      await user.click(within(reopened).getByRole('tab', { name: 'Azure AI with an API key' }))
      expect(within(reopened).getByLabelText(/^API key/)).toHaveValue('')
    })

    it('offers an Azure OpenAI resource only the Azure OpenAI API', async () => {
      const user = userEvent.setup()
      const dialog = await openKeyTab(user)

      await user.click(within(dialog).getByLabelText(/Endpoint URL/))
      await user.paste('https://fabrikam-aoai.openai.azure.com')

      const shape = within(dialog).getByLabelText('Deployment 1 API')
      expect(within(shape).getAllByRole('option').map((option) => option.textContent)).toEqual([
        'Azure OpenAI',
      ])
    })

    it('keeps the Register button focusable while busy and moves focus to a refusal', async () => {
      const user = userEvent.setup()
      let refuse: (reason: unknown) => void = () => undefined
      api.registerModelEndpoint.mockReturnValue(
        new Promise((_, reject) => {
          refuse = reject
        }),
      )
      const dialog = await openKeyTab(user)
      await user.click(within(dialog).getByLabelText(/Endpoint URL/))
      await user.paste(PROJECT_URL)
      await user.click(within(dialog).getByLabelText(/^API key/))
      await user.paste(API_KEY)
      await user.click(within(dialog).getByRole('button', { name: 'Register' }))

      const busy = await within(dialog).findByRole('button', { name: 'Registering…' })
      expect(busy).toHaveAttribute('aria-disabled', 'true')

      const message = 'This resource is already registered with an API key, as Fabrikam.'
      refuse(new TestApiError(message, 409, { message }))
      const refusal = await within(dialog).findByText(message)
      await waitFor(() => expect(refusal.closest('[tabindex="-1"]')).toHaveFocus())
      expect(within(dialog).getByText("MOSAIC didn't register this endpoint")).toBeVisible()
    })

    it('shows the key path, what to grant, and the declared deployments', async () => {
      api.listModelEndpoints.mockResolvedValue([keyEndpoint()])

      renderPage()

      const table = await screen.findByRole('table', { name: 'Registered model endpoints' })
      const row = within(table).getByText('Fabrikam partner Foundry').closest('tr') as HTMLElement
      expect(within(row).getByText('API key from Key Vault')).toBeVisible()
      expect(within(row).getByText('1 declared')).toBeVisible()
      // An API key can't list deployments, so there is nothing to sync.
      expect(within(row).queryByRole('button', { name: 'Sync models' })).not.toBeInTheDocument()
      expect(within(row).getByRole('button', { name: 'Check access' })).toBeVisible()

      expect(screen.getByText('The endpoint accepts the key')).toBeVisible()
      expect(screen.getByText(/Authentication: API key from Key Vault/)).toBeVisible()
      // MOSAIC doesn't hold this key, so it can't replace it.
      expect(screen.queryByRole('button', { name: 'Replace API key' })).not.toBeInTheDocument()
      expect(screen.getByText("Development gateway: can't read the key")).toBeVisible()
      expect(screen.getByText(/sends the endpoint's API key, which it reads from Key Vault/)).toBeVisible()
      expect(screen.getByText(GRANT_COMMAND)).toBeVisible()
      expect(screen.getByText('Key Vault kv-contoso-ai')).toBeVisible()
      expect(screen.getByText('Any role that can read secrets is also accepted.', { exact: false })).toBeVisible()

      const declared = screen.getByRole('table', { name: 'Declared model deployments' })
      const [, declaredRow] = within(declared).getAllByRole('row')
      expect(within(declaredRow).getAllByText('claude-sonnet-4-5')).toHaveLength(2)
      expect(within(declaredRow).getByText('Anthropic Messages API (Claude)')).toBeVisible()
      expect(within(declaredRow).getByText('Declared, not discovered')).toBeVisible()
      expect(screen.queryByRole('heading', { name: /Models on/ })).not.toBeInTheDocument()
      expect(api.listModelDeployments).not.toHaveBeenCalled()
    })

    it('declares a deployment and returns focus to the button that opened the dialog', async () => {
      const user = userEvent.setup()
      api.listModelEndpoints.mockResolvedValue([keyEndpoint()])
      api.declareModelDeployment.mockResolvedValue(keyEndpoint())

      renderPage()

      const opener = await screen.findByRole('button', { name: 'Declare a deployment' })
      await user.click(opener)
      const dialog = await screen.findByRole('dialog', {
        name: 'Declare a deployment on Fabrikam partner Foundry',
      })
      await user.type(within(dialog).getByLabelText(/Deployment name/), 'phi-4')
      await user.type(within(dialog).getByLabelText(/^Model/), 'Phi-4')
      expect(within(dialog).getByLabelText(/^API/)).toHaveValue('foundryModels')
      await user.click(within(dialog).getByRole('button', { name: 'Declare deployment' }))

      await waitFor(() =>
        expect(api.declareModelDeployment).toHaveBeenCalledWith('endpoint_key', {
          deploymentName: 'phi-4',
          modelName: 'Phi-4',
          apiShape: 'foundryModels',
        }),
      )
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      // Fluent returns focus to the button that opened the dialog when it closes.
      expect(opener).toHaveAttribute('data-tabster', expect.stringContaining('restorer'))
    })

    it('says why a deployment was not removed and moves focus there', async () => {
      const user = userEvent.setup()
      const message =
        'Unpublish claude-sonnet-4-5 before removing it. Its API would keep serving traffic in ' +
        'API Management with nothing in MOSAIC to change or remove it.'
      api.listModelEndpoints.mockResolvedValue([keyEndpoint()])
      api.removeDeclaredModelDeployment.mockRejectedValue(
        new TestApiError(message, 409, { message }),
      )

      renderPage()

      await user.click(await screen.findByRole('button', { name: 'Remove claude-sonnet-4-5' }))

      await waitFor(() =>
        expect(api.removeDeclaredModelDeployment).toHaveBeenCalledWith(
          'endpoint_key',
          'claude-sonnet-4-5',
        ),
      )
      const refusal = await screen.findByText(message)
      expect(screen.getByText("MOSAIC didn't remove this deployment")).toBeVisible()
      await waitFor(() => expect(refusal.closest('[tabindex="-1"]')).toHaveFocus())
    })

    it('moves focus to the outcome when a deployment is removed', async () => {
      const user = userEvent.setup()
      api.listModelEndpoints.mockResolvedValue([keyEndpoint()])
      api.removeDeclaredModelDeployment.mockResolvedValue(keyEndpoint({ declaredDeployments: [] }))

      renderPage()

      await user.click(await screen.findByRole('button', { name: 'Remove claude-sonnet-4-5' }))

      const outcome = await screen.findByRole('status')
      expect(outcome).toHaveTextContent(
        'Removed claude-sonnet-4-5 from Fabrikam partner Foundry. Nothing changed in Azure.',
      )
      await waitFor(() => expect(outcome).toHaveFocus())
    })

    describe('a key MOSAIC keeps', () => {
      it('says where the key is and replaces it as the next version of the same secret', async () => {
        const user = userEvent.setup()
        api.listModelEndpoints.mockResolvedValue([keyEndpoint({ keyStoredByMosaic: true })])
        api.updateModelEndpoint.mockResolvedValue(keyEndpoint({ keyStoredByMosaic: true }))

        renderPage()

        expect(
          await screen.findByText(/Authentication: API key MOSAIC keeps in its own Key Vault/),
        ).toBeVisible()
        expect(screen.getByText(/Nobody can read it back from MOSAIC/)).toBeVisible()
        const opener = screen.getByRole('button', { name: 'Replace API key' })
        await user.click(opener)
        const dialog = await screen.findByRole('dialog', {
          name: 'Replace the API key for Fabrikam partner Foundry',
        })
        expect(within(dialog).getByText(/next version of the same Key Vault secret/)).toBeVisible()
        expect(within(dialog).getByText(/paste the resource's other key/)).toBeVisible()
        const field = within(dialog).getByLabelText(/New API key/)
        expect(field).toHaveAttribute('type', 'password')
        await user.click(field)
        await user.paste(` ${API_KEY} `)
        await user.click(within(dialog).getByRole('button', { name: 'Store new key' }))

        await waitFor(() =>
          expect(api.updateModelEndpoint).toHaveBeenCalledWith('endpoint_key', { apiKey: API_KEY }),
        )
        await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
        expect(
          await screen.findByText(
            'Stored the new key for Fabrikam partner Foundry, and the endpoint accepts it. API ' +
              'Management picks it up within four hours.',
          ),
        ).toBeVisible()
        // Fluent returns focus to the button that opened the dialog when it closes.
        expect(opener).toHaveAttribute('data-tabster', expect.stringContaining('restorer'))
      })

      it('keeps the button focusable while busy and moves focus to a refusal', async () => {
        const user = userEvent.setup()
        let refuse: (reason: unknown) => void = () => undefined
        api.listModelEndpoints.mockResolvedValue([keyEndpoint({ keyStoredByMosaic: true })])
        api.updateModelEndpoint.mockReturnValue(
          new Promise((_, reject) => {
            refuse = reject
          }),
        )

        renderPage()

        await user.click(await screen.findByRole('button', { name: 'Replace API key' }))
        const dialog = await screen.findByRole('dialog')
        await user.click(within(dialog).getByLabelText(/New API key/))
        await user.paste(API_KEY)
        await user.click(within(dialog).getByRole('button', { name: 'Store new key' }))

        const busy = await within(dialog).findByRole('button', { name: 'Storing…' })
        expect(busy).toHaveAttribute('aria-disabled', 'true')
        expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveAttribute(
          'aria-disabled',
          'true',
        )

        const message =
          "MOSAIC isn't allowed to store secrets in Key Vault kv-contoso-ai. Grant its identity " +
          'Key Vault Secrets Officer on the vault.'
        refuse(new TestApiError(message, 403, { message }))
        const refusal = await within(dialog).findByText(message)
        await waitFor(() => expect(refusal.closest('[tabindex="-1"]')).toHaveFocus())
        expect(within(dialog).getByText("MOSAIC didn't replace the key")).toBeVisible()
      })

      it("treats an unrecorded replacement as using the new key", async () => {
        const user = userEvent.setup()
        const message =
          "MOSAIC stored the new key in Key Vault, and MOSAIC and API Management use it from " +
          "now on, but MOSAIC couldn't record the change. Check access to refresh this " +
          "endpoint's status."
        api.listModelEndpoints.mockResolvedValue([keyEndpoint({ keyStoredByMosaic: true })])
        api.updateModelEndpoint.mockRejectedValueOnce(
          new TestApiError(message, 503, {
            message,
            details: { reason: 'keyReplacedNotRecorded' },
          }),
        )

        renderPage()

        await user.click(await screen.findByRole('button', { name: 'Replace API key' }))
        const dialog = await screen.findByRole('dialog')
        await user.click(within(dialog).getByLabelText(/New API key/))
        await user.paste(API_KEY)
        await user.click(within(dialog).getByRole('button', { name: 'Store new key' }))

        await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
        expect(await screen.findByText(message)).toBeVisible()
        expect(screen.queryByText("MOSAIC didn't replace the key")).not.toBeInTheDocument()
      })

      it('asks for the new key before sending anything', async () => {
        const user = userEvent.setup()
        api.listModelEndpoints.mockResolvedValue([keyEndpoint({ keyStoredByMosaic: true })])

        renderPage()

        await user.click(await screen.findByRole('button', { name: 'Replace API key' }))
        const dialog = await screen.findByRole('dialog')
        expect(within(dialog).getByLabelText(/New API key/)).toBeRequired()
        await user.click(within(dialog).getByLabelText(/New API key/))
        await user.paste('  ')
        await user.click(within(dialog).getByRole('button', { name: 'Store new key' }))

        expect(await within(dialog).findByText("Paste the resource's new API key.")).toBeVisible()
        expect(api.updateModelEndpoint).not.toHaveBeenCalled()
      })
    })
  })
})
