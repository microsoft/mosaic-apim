import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { AccessRequest, CatalogEntry } from '../types'
import { CatalogPage } from './CatalogPage'

const mocks = vi.hoisted(() => ({
  api: {} as PortalApi,
}))

vi.mock('../api', () => ({
  ApiError: class ApiError extends Error {
    status = 500
  },
  usePortalApi: () => mocks.api,
}))

const catalogEntry: CatalogEntry = {
  kind: 'mcpServer',
  id: 'mcp-weather',
  displayName: 'Weather tools',
  summary: 'Forecast and alert tools.',
  gatewayId: 'gateway-1',
  gatewayName: 'production gateway',
  environment: 'production',
  entitled: false,
  requestState: null,
  enforced: false,
}

const pendingRequest: AccessRequest = {
  id: 'request-1',
  tenantId: 'tenant-1',
  entityType: 'accessRequest',
  requesterObjectId: 'user-1',
  requesterPrincipalId: 'principal-1',
  resource: { kind: 'mcpServer', id: 'mcp-weather', scopeId: 'gateway-1' },
  justification: null,
  state: 'pending',
  decidedByObjectId: null,
  decidedAt: null,
  decisionNote: null,
  grantedEntitlementId: null,
  requestedEnvironment: 'production',
  resourceSnapshot: { displayName: 'Weather tools', gatewayId: 'gateway-1', gatewayName: 'production gateway' },
  resourceSummary: {
    kind: 'mcpServer',
    id: 'mcp-weather',
    scopeId: null,
    displayName: 'Weather tools',
    gatewayId: 'gateway-1',
    gatewayName: 'production gateway',
    environment: 'production',
    available: true,
  },
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
}

function renderPage(entries: CatalogEntry[], requests: AccessRequest[]) {
  mocks.api = {
    listCatalog: async () => entries,
    listEnvironments: async () => [
      {
        key: 'development',
        displayName: 'Development',
        description: null,
        color: 'brand',
        production: false,
        order: 10,
      },
      {
        key: 'production',
        displayName: 'Production',
        description: null,
        color: 'danger',
        production: true,
        order: 50,
      },
    ],
    listAccessRequests: async () => requests,
    createAccessRequest: vi.fn(),
    withdrawAccessRequest: vi.fn(),
  } as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <CatalogPage />
    </QueryClientProvider>,
  )
}

describe('CatalogPage', () => {
  it('offers request access for entries without an open request', async () => {
    renderPage([catalogEntry], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Request access' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })

  it('switches to withdraw when a request is pending', async () => {
    renderPage([{ ...catalogEntry, requestState: 'pending' }], [pendingRequest])

    expect(await screen.findByText('A request is already open.')).toBeVisible()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Withdraw' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Request access' })).not.toBeInTheDocument()
  })

  it.each([
    [true, 'Enforced by the gateway'],
    [false, 'Recorded, not enforced'],
  ])('labels MCP catalog entries with enforced=%s', async (enforced, label) => {
    renderPage([{ ...catalogEntry, enforced }], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    expect(screen.getByText(label)).toBeVisible()
  })

  it('omits the MCP enforcement badge when the value is null', async () => {
    renderPage([{ ...catalogEntry, enforced: null }], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    expect(screen.queryByText('Enforced by the gateway')).not.toBeInTheDocument()
    expect(screen.queryByText('Recorded, not enforced')).not.toBeInTheDocument()
  })

  it('does not show enforcement badges for models', async () => {
    renderPage([{ ...catalogEntry, kind: 'modelApi', enforced: true }], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    expect(screen.queryByText('Enforced by the gateway')).not.toBeInTheDocument()
    expect(screen.queryByText('Recorded, not enforced')).not.toBeInTheDocument()
  })

  it('shows environment badges and filters by environment and resource type', async () => {
    const user = userEvent.setup()
    renderPage([
      catalogEntry,
      {
        ...catalogEntry,
        id: 'dev-chat',
        kind: 'modelApi',
        displayName: 'Development chat',
        gatewayName: 'development gateway',
        environment: 'development',
      },
      {
        ...catalogEntry,
        id: 'unclassified-mcp',
        displayName: 'Unclassified tools',
        environment: null,
      },
    ], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    expect(screen.getAllByText('Production')[0]).toBeVisible()
    expect(screen.getAllByText('Development')[0]).toBeVisible()
    expect(screen.getAllByText('Unclassified')[0]).toBeVisible()
    expect(screen.getByRole('option', { name: 'Unclassified' })).toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Environment'), 'development')
    expect(screen.getByText('Development chat')).toBeVisible()
    expect(screen.queryByText('Weather tools')).not.toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Resource type'), 'mcpServer')
    expect(screen.getByText('No catalog entries match these filters')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Clear filters' }))
    expect(screen.getByText('Weather tools')).toBeVisible()
    expect(screen.getByText('Development chat')).toBeVisible()
  })
})
