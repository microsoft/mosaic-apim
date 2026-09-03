import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ModelsPage } from './ModelsPage'
import type { Gateway, ModelApi } from '../types'

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

  it('keeps the sample preview clearly separated from live data', async () => {
    renderPage()

    expect(await screen.findByText(/Everything below this line is a preview/)).toBeVisible()
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

  it('opens the local deploy dialog from the shell query and clears it when closed', async () => {
    const user = userEvent.setup()
    renderPage('/models?deploy=1')

    expect(await screen.findByRole('dialog')).toBeVisible()
    expect(screen.getByText(/does not call Azure AI Foundry/i)).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(screen.getByTestId('location')).toHaveTextContent('/models')
  })
})
