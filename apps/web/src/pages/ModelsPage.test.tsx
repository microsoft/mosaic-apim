import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelsPage } from './ModelsPage'
import type {
  Gateway,
  GatewayRuntimeAccess,
  ModelApi,
  ModelEndpoint,
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
  selection: 'detected',
  importedFromSnapshotId: 'snapshot_1',
  importedAt: '2026-09-01T12:10:00Z',
  importedBy: 'admin-object-id',
  createdAt: '2026-09-01T12:10:00Z',
  updatedAt: '2026-09-01T12:10:00Z',
}

const api = {
  listGateways: vi.fn(),
  listModelApis: vi.fn(),
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

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: class extends Error {},
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
    api.listModelEndpoints.mockResolvedValue([])
    api.listSuggestedModelEndpoints.mockResolvedValue({
      suggestions: [],
      scanIssues: [],
      subscriptionsScanned: 0,
    })
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

describe('ModelsPage model endpoints', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([gateway])
    api.listModelApis.mockResolvedValue([])
    api.listModelEndpoints.mockResolvedValue([])
    api.listSuggestedModelEndpoints.mockResolvedValue({
      suggestions: [],
      scanIssues: [],
      subscriptionsScanned: 0,
    })
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
          message: 'MOSAIC is missing permissions needed to enumerate models.',
        },
      }),
    ])

    renderPage()

    expect(await screen.findByText('MOSAIC cannot read this endpoint')).toBeVisible()
    expect(screen.getByText(/without also granting/i)).toBeVisible()
  })

  it('surfaces suggestions and labels where each came from', async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue({
      suggestions: [
        {
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
        },
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
      scanIssues: [],
      subscriptionsScanned: 1,
    })

    renderPage()

    expect(await screen.findByText('Used by a gateway')).toBeVisible()
    expect(screen.getByText('Found in a subscription')).toBeVisible()
    // A hostname alone cannot be registered, so no action is offered for it.
    expect(screen.getByText('Needs a resource ID')).toBeVisible()
  })

  it('explains a subscription it could not scan instead of hiding it', async () => {
    api.listSuggestedModelEndpoints.mockResolvedValue({
      suggestions: [],
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
    })

    renderPage()

    expect(await screen.findByText('Subscriptions MOSAIC could not scan')).toBeVisible()
    expect(screen.getByText(/az role assignment create/)).toBeVisible()
  })
})

