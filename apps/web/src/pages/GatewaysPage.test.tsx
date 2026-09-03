import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GatewaysPage } from './GatewaysPage'
import type { Gateway, GatewaySuggestion } from '../types'

const RESOURCE_ID =
  '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-dev' +
  '/providers/Microsoft.ApiManagement/service/apim-contoso-dev'

function gateway(overrides: Partial<Gateway> = {}): Gateway {
  return {
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
      missingActions: ['Microsoft.ApiManagement/service/policies/write'],
      remediation: {
        roleName: 'API Management Service Contributor',
        roleDefinitionId: '312a565d-c81f-4fd8-895a-4e21e48d571c',
        scope: RESOURCE_ID,
        principalId: 'mosaic-mi',
        command: `az role assignment create --assignee-object-id "mosaic-mi" --scope "${RESOURCE_ID}"`,
      },
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
      principalId: '11111111-1111-1111-1111-111111111111',
      identityObserved: true,
      notes: [],
    },
    inventory: {
      apis: 12,
      aiApis: 4,
      mcpServers: 2,
      operations: 40,
      products: 3,
      subscriptions: 87,
      users: 5,
      groups: 3,
      backends: 2,
      namedValues: 4,
      policyDocuments: 6,
      policyFragments: 1,
      recognizedFacets: 18,
      unrecognizedFacets: 2,
      mosaicManagedFacets: 1,
    },
    lastSyncedAt: '2026-09-01T12:05:00Z',
    lastSyncError: null,
    createdAt: '2026-09-01T11:00:00Z',
    updatedAt: '2026-09-01T12:05:00Z',
    ...overrides,
  }
}

const suggestion: GatewaySuggestion = {
  azureResourceId: RESOURCE_ID,
  serviceName: 'apim-contoso-dev',
  resourceGroup: 'rg-contoso-dev',
  subscriptionId: '00000000-0000-0000-0000-000000000000',
  alreadyRegistered: false,
  gatewayId: null,
  reason: 'Deployed alongside MOSAIC in this environment.',
}

const api = {
  listGateways: vi.fn(),
  listSuggestedGateways: vi.fn(),
  registerGateway: vi.fn(),
  syncGateway: vi.fn(),
  preflightGateway: vi.fn(),
  deleteGateway: vi.fn(),
}

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: class extends Error {},
}))

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <GatewaysPage />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('GatewaysPage', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.listGateways.mockResolvedValue([])
    api.listSuggestedGateways.mockResolvedValue([])
  })

  it('explains that MOSAIC observes rather than changes the gateway', async () => {
    renderPage()

    expect(
      await screen.findByText(/It does not change your gateway/),
    ).toBeVisible()
  })

  it('invites onboarding when nothing is registered', async () => {
    renderPage()

    expect(await screen.findByText('No gateways yet')).toBeVisible()
  })

  it('offers the gateway deployed alongside MOSAIC', async () => {
    api.listSuggestedGateways.mockResolvedValue([suggestion])
    renderPage()

    expect(await screen.findByRole('heading', { name: 'Detected gateway' })).toBeVisible()
    expect(screen.getByText(/Deployed alongside MOSAIC/)).toBeVisible()
  })

  it('hides the suggestion once the gateway is onboarded', async () => {
    api.listSuggestedGateways.mockResolvedValue([
      { ...suggestion, alreadyRegistered: true, gatewayId: 'gateway_1' },
    ])
    renderPage()

    await screen.findByText(/It does not change your gateway/)
    expect(screen.queryByRole('heading', { name: 'Detected gateway' })).toBeNull()
  })

  async function onboard(user: ReturnType<typeof userEvent.setup>) {
    await user.click(await screen.findByRole('button', { name: 'Onboard gateway' }))
    const input = await screen.findByRole('textbox', { name: /API Management resource ID/ })
    await user.type(input, RESOURCE_ID)
    await user.click(screen.getByRole('button', { name: 'Check access and onboard' }))
  }

  it('registers a gateway by resource ID', async () => {
    const user = userEvent.setup()
    api.registerGateway.mockResolvedValue(gateway())
    renderPage()

    await onboard(user)

    await waitFor(() => expect(api.registerGateway).toHaveBeenCalled())
    expect(api.registerGateway.mock.calls[0][0]).toEqual({
      azureResourceId: RESOURCE_ID,
      name: undefined,
      environmentLabel: undefined,
    })
  })

  it('reports read access and the role enrollment will still need', async () => {
    const user = userEvent.setup()
    api.registerGateway.mockResolvedValue(gateway())
    renderPage()

    await onboard(user)

    expect(await screen.findByText('MOSAIC can read this gateway')).toBeVisible()
    expect(screen.getByText(/API Management Service Contributor/)).toBeVisible()
    expect(screen.getByText(/az role assignment create/)).toBeVisible()
  })

  it('shows the exact remediation when access is missing', async () => {
    const user = userEvent.setup()
    api.registerGateway.mockResolvedValue(
      gateway({
        status: 'unauthorized',
        access: {
          canRead: false,
          canWrite: false,
          evaluation: 'probe',
          checkedAt: '2026-09-01T12:00:00Z',
          missingActions: ['Microsoft.ApiManagement/service/read'],
          remediation: {
            roleName: 'API Management Service Reader Role',
            roleDefinitionId: '71522526-b88f-4d52-b57f-d31fc3546d0d',
            scope: RESOURCE_ID,
            principalId: 'mosaic-mi',
            command: 'az role assignment create --role "API Management Service Reader Role"',
          },
          message: 'MOSAIC cannot read this API Management service.',
        },
      }),
    )
    renderPage()

    await onboard(user)

    expect(await screen.findByText('MOSAIC cannot read this gateway')).toBeVisible()
    expect(screen.getAllByText(/API Management Service Reader Role/).length).toBeGreaterThan(0)
  })

  it('lists registered gateways with their AI surface', async () => {
    api.listGateways.mockResolvedValue([gateway()])
    renderPage()

    expect(await screen.findByRole('link', { name: 'Development gateway' })).toBeVisible()
    expect(screen.getByText('Connected')).toBeVisible()
    expect(screen.getByText('4 of 12')).toBeVisible()
  })

  it('says plainly that removal does not touch Azure', async () => {
    api.listGateways.mockResolvedValue([gateway()])
    renderPage()

    expect(
      await screen.findByText(/The API Management service itself is never modified/),
    ).toBeVisible()
  })

  it('does not offer sync until MOSAIC can read the gateway', async () => {
    api.listGateways.mockResolvedValue([
      gateway({
        status: 'unauthorized',
        access: {
          canRead: false,
          canWrite: false,
          evaluation: 'probe',
          checkedAt: null,
          missingActions: [],
          remediation: null,
          message: null,
        },
      }),
    ])
    renderPage()

    expect(await screen.findByRole('button', { name: 'Sync' })).toBeDisabled()
  })
})
