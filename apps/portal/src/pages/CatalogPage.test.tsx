import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
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
  entitled: false,
  requestState: null,
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
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
}

function renderPage(entries: CatalogEntry[], requests: AccessRequest[]) {
  mocks.api = {
    listCatalog: async () => entries,
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
})
