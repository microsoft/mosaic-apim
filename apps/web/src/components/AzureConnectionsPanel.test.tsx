import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { MemoryRouter } from 'react-router-dom'
import { AzureConnectionsPanel } from './AzureConnectionsPanel'

const api = {
  listAzureConnections: vi.fn(),
  createAzureConnection: vi.fn(),
  renameAzureConnection: vi.fn(),
  replaceAzureConnectionSecret: vi.fn(),
  deleteAzureConnection: vi.fn(),
  testAzureConnection: vi.fn(),
}
vi.mock('../api', () => ({ useMosaicApi: () => api }))

const government = {
  id: 'connection-gov', name: 'Government management', azureCloud: 'government',
  azureTenantId: '11111111-1111-1111-1111-111111111111',
  clientId: '22222222-2222-2222-2222-222222222222',
  authentication: 'clientSecret', status: 'untested', statusMessage: null,
  revision: 1, inUse: false,
}
const host = {
  ...government, id: 'host', name: 'MOSAIC managed identity',
  azureCloud: 'commercial', authentication: 'managedIdentity', clientId: null, inUse: true,
}

function setup() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}>
    <MemoryRouter><AzureConnectionsPanel /></MemoryRouter>
  </QueryClientProvider>)
  return client
}

describe('AzureConnectionsPanel', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    window.localStorage.clear()
    window.sessionStorage.clear()
    api.listAzureConnections.mockResolvedValue([host, government])
    api.createAzureConnection.mockResolvedValue(government)
    api.replaceAzureConnectionSecret.mockResolvedValue(government)
  })

  it('keeps cloud separate from environment and explains the commercial data boundary', async () => {
    setup()
    expect(await screen.findByText('Government management')).toBeInTheDocument()
    expect(screen.getByText('AzGov')).toBeInTheDocument()
    expect(screen.getByText(/not commercial Entra tokens/)).toBeInTheDocument()
    expect(screen.getByText(/commercial MOSAIC deployment/)).toBeInTheDocument()
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(1)
  })

  it('creates a Government connection without retaining a secret in caches or browser storage', async () => {
    const user = userEvent.setup()
    const client = setup()
    await user.click(screen.getByRole('button', { name: 'Add connection' }))
    await user.type(screen.getByLabelText(/^Connection name/), 'Government management')
    await user.type(screen.getByLabelText(/^Target tenant ID/), government.azureTenantId)
    await user.type(screen.getByLabelText(/^Application \(client\) ID/), government.clientId)
    const field = screen.getByLabelText(/^Client secret value/)
    expect(field).toHaveAttribute('type', 'password')
    await user.type(field, 'fictional-secret-value')
    await user.click(screen.getByRole('button', { name: 'Save connection' }))
    await waitFor(() => expect(api.createAzureConnection).toHaveBeenCalledWith({
      name: government.name, azureCloud: 'government', azureTenantId: government.azureTenantId,
      clientId: government.clientId, clientSecret: 'fictional-secret-value',
    }))
    await screen.findByRole('button', { name: 'Add connection' })
    expect(screen.queryByDisplayValue('fictional-secret-value')).not.toBeInTheDocument()
    expect(JSON.stringify(client.getQueryCache().getAll().map((item) => item.state.data))).not.toContain('fictional-secret-value')
    expect(client.getMutationCache().getAll().every((item) => item.state.variables === undefined)).toBe(true)
    expect(JSON.stringify(window.localStorage)).not.toContain('fictional-secret-value')
    expect(JSON.stringify(window.sessionStorage)).not.toContain('fictional-secret-value')
  })

  it('guards removal and cannot remove a connection still used by a resource', async () => {
    api.listAzureConnections.mockResolvedValue([host, { ...government, inUse: true }])
    setup()
    expect(await screen.findByRole('button', { name: 'Remove' })).toBeDisabled()
    expect(screen.getByText('Registered resources use this connection.')).toBeInTheDocument()
    expect(api.deleteAzureConnection).not.toHaveBeenCalled()
  })

  it('uses a write-only replacement and reports errors without discarding the form', async () => {
    const user = userEvent.setup()
    api.replaceAzureConnectionSecret.mockRejectedValue(new Error('Authentication failed'))
    setup()
    await user.click(await screen.findByRole('button', { name: 'Replace secret' }))
    const field = screen.getByLabelText(/^Client secret value/)
    expect(field).toHaveValue('')
    await user.type(field, 'new-fictional-secret')
    await user.click(screen.getByRole('button', { name: 'Verify and replace secret' }))
    expect(await screen.findByText('Authentication failed')).toBeInTheDocument()
    expect(api.replaceAzureConnectionSecret).toHaveBeenCalledWith(government.id, 'new-fictional-secret')
    expect(field).toHaveValue('new-fictional-secret')
    expect(screen.queryByRole('button', { name: /reveal/i })).not.toBeInTheDocument()
  })
})
