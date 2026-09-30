import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GatewayDetailPage } from './GatewayDetailPage'
import { modelPublication } from '../test/model-access'
import type { Gateway, GatewayAccess, ManagementMode } from '../types'

const RESOURCE_ID =
  '/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg-contoso-dev' +
  '/providers/Microsoft.ApiManagement/service/apim-contoso-dev'

const readOnly: GatewayAccess = {
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
}

const writable: GatewayAccess = {
  canRead: true,
  canWrite: true,
  evaluation: 'effectivePermissions',
  checkedAt: '2026-09-01T12:30:00Z',
  missingActions: [],
  remediation: null,
  message: 'MOSAIC can read and write this gateway.',
}

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
    environment: null,
    azureEnvironmentTag: null,
    environmentLabel: 'dev',
    managementMode: 'observe',
    status: 'connected',
    access: readOnly,
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

const { TestApiError } = vi.hoisted(() => ({
  TestApiError: class ApiError extends Error {
    readonly status: number

    constructor(message: string, status: number) {
      super(message)
      this.status = status
    }
  },
}))

const api = {
  getEnvironmentCatalog: vi.fn(),
  listEnvironmentFindings: vi.fn(),
  getGateway: vi.fn(),
  listPublications: vi.fn(),
  updateGateway: vi.fn(),
  preflightGateway: vi.fn(),
  syncGateway: vi.fn(),
}

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: TestApiError,
}))

// The server's copy of the gateway, so a refetch after a change sees what the change saved.
let stored: Gateway

function serve(initial: Gateway) {
  stored = initial
  api.getGateway.mockImplementation(() => Promise.resolve(stored))
  api.updateGateway.mockImplementation(
    (_id: string, changes: { managementMode?: ManagementMode }) => {
      stored = { ...stored, ...changes }
      return Promise.resolve(stored)
    },
  )
}

function renderPage() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  // The Publish dialog lists gateways from this cache entry.
  queryClient.setQueryData(['gateways'], [stored])
  const invalidate = vi.spyOn(queryClient, 'invalidateQueries')
  render(
    <QueryClientProvider client={queryClient}>
      <FluentProvider theme={webLightTheme}>
        <MemoryRouter initialEntries={['/gateways/gateway_1']}>
          <Routes>
            <Route path="/gateways/:gatewayId" element={<GatewayDetailPage />} />
          </Routes>
        </MemoryRouter>
      </FluentProvider>
    </QueryClientProvider>,
  )
  return { queryClient, invalidate }
}

async function modeControl() {
  return screen.findByRole('radiogroup', { name: 'Management mode' })
}

async function dialogClosed() {
  await waitFor(() => {
    expect(screen.queryByRole('alertdialog', { hidden: true })).not.toBeInTheDocument()
  })
}

describe('GatewayDetailPage management mode', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [],
      requireClassification: false,
      unclassified: { gateways: 1, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [],
      updatedAt: null,
    })
    api.listEnvironmentFindings.mockResolvedValue({
      items: [],
      limitations: [],
      generatedAt: '2026-09-01T12:00:00Z',
    })
    api.listPublications.mockResolvedValue([])
  })

  it('shows the gateway’s real management mode', async () => {
    serve(gateway({ access: writable }))
    renderPage()

    const control = await modeControl()
    expect(screen.getByText('apim-contoso-dev · Observe mode')).toBeVisible()
    expect(screen.queryByText(/observe only/i)).not.toBeInTheDocument()
    expect(within(control).getByRole('radio', { name: 'Observe' })).toBeChecked()
    expect(within(control).getByRole('radio', { name: 'Manage' })).not.toBeChecked()
  })

  it('shows manage mode for a managed gateway', async () => {
    serve(gateway({ managementMode: 'manage', access: writable }))
    renderPage()

    const control = await modeControl()
    expect(screen.getByText('apim-contoso-dev · Manage mode')).toBeVisible()
    expect(within(control).getByRole('radio', { name: 'Manage' })).toBeChecked()
  })

  it('disables the control and names the missing role and scope without write access', async () => {
    serve(gateway())
    renderPage()

    const control = await modeControl()
    const manage = within(control).getByRole('radio', { name: 'Manage' })
    expect(manage).toBeDisabled()
    expect(within(control).getByRole('radio', { name: 'Observe' })).toBeDisabled()
    expect(manage).toHaveAccessibleDescription(/didn’t confirm it/)
    expect(screen.getByText('MOSAIC can’t manage this gateway yet')).toBeVisible()
    expect(
      screen.getByText('API Management Service Contributor', { selector: 'dd' }),
    ).toBeVisible()
    expect(screen.getByText(RESOURCE_ID)).toBeVisible()
    expect(screen.getByText(/MOSAIC’s managed identity \(mosaic-mi\)/)).toBeVisible()
  })

  it('re-runs the access check and enables the control once write access is granted', async () => {
    const user = userEvent.setup()
    serve(gateway())
    api.preflightGateway.mockImplementation(() => {
      stored = { ...stored, access: writable }
      return Promise.resolve(stored)
    })
    renderPage()

    const control = await modeControl()
    await user.click(screen.getByRole('button', { name: 'Check access' }))

    expect(api.preflightGateway).toHaveBeenCalledWith('gateway_1')
    expect(
      await screen.findByText('Write access confirmed. You can switch this gateway to Manage.'),
    ).toBeVisible()
    const manage = within(control).getByRole('radio', { name: 'Manage' })
    expect(manage).toBeEnabled()
    // The button that had focus is gone, so focus moves to the option it unlocked.
    await waitFor(() => expect(manage).toHaveFocus())
    expect(manage).toHaveAccessibleDescription(/Write access confirmed/)
    expect(screen.queryByText('MOSAIC can’t manage this gateway yet')).not.toBeInTheDocument()
  })

  it('keeps the gateway in observe mode when the confirmation is cancelled', async () => {
    const user = userEvent.setup()
    serve(gateway({ access: writable }))
    renderPage()

    const control = await modeControl()
    const observe = within(control).getByRole('radio', { name: 'Observe' })
    const manage = within(control).getByRole('radio', { name: 'Manage' })
    act(() => observe.focus())
    await user.keyboard('{ArrowRight}')
    const dialog = await screen.findByRole('alertdialog', { name: 'Switch to manage mode?' })
    // Focus starts on the safe choice, so pressing Enter right away doesn't switch the mode.
    await waitFor(() => {
      expect(within(dialog).getByRole('button', { name: 'Cancel' })).toHaveFocus()
    })
    expect(within(dialog).getByRole('button', { name: 'Switch to manage' })).toBeVisible()
    await user.keyboard('{Escape}')

    await dialogClosed()
    expect(api.updateGateway).not.toHaveBeenCalled()
    expect(observe).toBeChecked()
    // Keyboard users land back on the option they chose.
    await waitFor(() => expect(manage).toHaveFocus())
  })

  it('switches to manage mode after confirmation and refreshes every view of the gateway', async () => {
    const user = userEvent.setup()
    serve(gateway({ access: writable }))
    const { queryClient, invalidate } = renderPage()

    const control = await modeControl()
    await user.click(within(control).getByRole('radio', { name: 'Manage' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Switch to manage mode?' })
    expect(within(dialog).getByText(/changes nothing in apim-contoso-dev by itself/)).toBeVisible()
    expect(within(dialog).getByText(/won’t replace an API, API policy or policy fragment it didn’t create/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Switch to manage' }))

    await waitFor(() => {
      expect(api.updateGateway).toHaveBeenCalledWith('gateway_1', { managementMode: 'manage' })
    })
    expect(await screen.findByText('apim-contoso-dev · Manage mode')).toBeVisible()
    await dialogClosed()
    // The rest of the page is hidden from assistive technology until the modal dialog is gone.
    const manage = await within(control).findByRole('radio', { name: 'Manage' })
    expect(manage).toBeChecked()
    expect(screen.getByText(/Switched to manage mode\. Nothing in API Management changed\./)).toBeVisible()
    expect(manage).toHaveAccessibleDescription(/Switched to manage mode/)
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['gateway', 'gateway_1'] })
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ['gateways'] })
    expect(queryClient.getQueryData<Gateway[]>(['gateways'])?.[0].managementMode).toBe('manage')
  })

  it('lists the models that stay published before switching back to observe mode', async () => {
    const user = userEvent.setup()
    serve(gateway({ managementMode: 'manage', access: writable }))
    api.listPublications.mockResolvedValue([modelPublication])
    renderPage()

    const control = await modeControl()
    await user.click(within(control).getByRole('radio', { name: 'Observe' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Switch to observe mode?' })
    const published = within(dialog).getByRole('list', {
      name: 'Models published through this gateway',
    })
    expect(within(published).getByText('Published chat (/models/chat)')).toBeVisible()
    expect(within(dialog).getByText(/stays in API Management and keeps handling calls/)).toBeVisible()
    expect(within(dialog).getByText(/unpublish it before you switch/)).toBeVisible()
    await user.click(within(dialog).getByRole('button', { name: 'Switch to observe' }))

    await waitFor(() => {
      expect(api.updateGateway).toHaveBeenCalledWith('gateway_1', { managementMode: 'observe' })
    })
    expect(await screen.findByText('apim-contoso-dev · Observe mode')).toBeVisible()
  })

  it('shows when a model published through the gateway was unpublished', async () => {
    const unpublishedAt = '2026-09-30T11:20:00Z'
    serve(gateway({ managementMode: 'manage', access: writable }))
    api.listPublications.mockResolvedValue([
      modelPublication,
      { ...modelPublication, id: 'pub_2', displayName: 'Retired chat', status: 'draft', unpublishedAt },
    ])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Gateway published models' })
    const [, live, retired] = within(table).getAllByRole('row')
    expect(live).toHaveTextContent('Published')
    expect(retired).toHaveTextContent(
      `Retired chatUnpublished/models/chatUnpublished ${new Date(unpublishedAt).toLocaleString()}`,
    )
  })

  it('shows the API’s refusal and leaves the mode unchanged', async () => {
    const user = userEvent.setup()
    const refusal =
      'An apply or mutation is already running for this publication. Interrupted runs require explicit recovery.'
    serve(gateway({ managementMode: 'manage', access: writable }))
    api.updateGateway.mockRejectedValue(new TestApiError(refusal, 409))
    renderPage()

    const control = await modeControl()
    await user.click(within(control).getByRole('radio', { name: 'Observe' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Switch to observe mode?' })
    await user.click(within(dialog).getByRole('button', { name: 'Switch to observe' }))

    expect(await within(dialog).findByText('The management mode was not changed')).toBeVisible()
    expect(within(dialog).getByRole('alert')).toHaveTextContent(refusal)
    expect(screen.getByRole('alertdialog')).toBeInTheDocument()

    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    await dialogClosed()
    expect(screen.getByText('apim-contoso-dev · Manage mode')).toBeVisible()
    expect(await within(control).findByRole('radio', { name: 'Manage' })).toBeChecked()
  })

  it('still lets a managed gateway without write access go back to observe mode', async () => {
    serve(gateway({ managementMode: 'manage' }))
    renderPage()

    const control = await modeControl()
    expect(within(control).getByRole('radio', { name: 'Observe' })).toBeEnabled()
    expect(screen.getByText('MOSAIC refuses to publish to this gateway')).toBeVisible()
  })
})
