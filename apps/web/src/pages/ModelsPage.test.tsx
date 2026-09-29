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

const api = {
  listGateways: vi.fn(),
  listModelApis: vi.fn(),
  deletePublication: vi.fn(),
  unpublishPublication: vi.fn(),
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
  syncModelEndpoint: vi.fn(),
  preflightModelEndpoint: vi.fn(),
  deleteModelEndpoint: vi.fn(),
  listModelDeployments: vi.fn(),
}

const { TestApiError } = vi.hoisted(() => ({
  TestApiError: class ApiError extends Error {
    readonly status: number

    constructor(message: string, status: number) {
      super(message)
      this.status = status
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
    api.unpublishPublication.mockResolvedValue({ id: 'run_2' })
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

  it('opens plan review instead of applying when a publication has no current plan', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([{ ...publication, lastPlanId: null }])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1'))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(await screen.findByRole('table', { name: 'Publish plan steps' })).toBeVisible()
    expect(screen.getByText('Review runtime access before applying.')).toBeVisible()
  })

  it('applies the existing plan when a publication has a current plan', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(api.applyPublishPlan).toHaveBeenCalledWith('pub_1', 'plan_1')
    })
    expect(api.createPublishPlan).not.toHaveBeenCalled()
  })

  it('always reviews the complete governed-access snapshot before applying an existing plan', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.createPublishPlan.mockResolvedValue(accessPlan)
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Apply' }))
    expect(await screen.findByRole('table', { name: 'All target model grants' })).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('does not describe an interrupted apply as still running or successful', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    api.applyPublishPlan.mockResolvedValue({ id: 'interrupted-run', status: 'interrupted', errors: [] })
    renderPage()
    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Apply' }))
    expect(await screen.findByText(/Apply interrupted — runtime state unknown/)).toBeVisible()
    expect(screen.queryByText(/Run started/)).not.toBeInTheDocument()
  })

  it('routes a stale direct apply back to fresh plan review', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    api.applyPublishPlan.mockRejectedValue(new TestApiError('The publish plan is stale.', 409))

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Apply' }))

    await waitFor(() => {
      expect(api.createPublishPlan).toHaveBeenCalledWith('pub_1')
    })
    expect(await screen.findByText('The publish plan is stale.')).toBeVisible()
    expect(screen.getByRole('table', { name: 'Publish plan steps' })).toBeVisible()
  })

  it('does not claim the models page never changes API Management', async () => {
    renderPage()

    expect(
      await screen.findByText(/Reading and importing do not change Azure/i),
    ).toBeVisible()
    expect(
      screen.queryByText(/MOSAIC never changes API Management or your model resources/i),
    ).not.toBeInTheDocument()
  })

  it('surfaces the conflict message when removing a publication that still owns resources', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([publication])
    api.deletePublication.mockRejectedValue(new Error('Unpublish before removing this publication.'))

    renderPage()

    const table = await screen.findByRole('table', { name: 'Published models' })
    await user.click(within(table).getByRole('button', { name: 'Remove' }))

    expect(await screen.findByText('Unpublish before removing this publication.')).toBeVisible()
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
})
