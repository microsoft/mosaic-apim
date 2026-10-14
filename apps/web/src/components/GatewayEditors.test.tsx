import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { poolGateway } from '../test/pool-fixtures'
import { GatewayRecordEditor, GatewayRegistrationEditor } from './GatewayEditors'

const api = { registerGateway: vi.fn(), updateGateway: vi.fn(), assignEnvironments: vi.fn(), getEnvironmentCatalog: vi.fn() }
vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, useMosaicApi: () => api }
})
function renderEditor(kind: 'register' | 'edit' = 'edit') {
  const onClose = vi.fn()
  const onSaved = vi.fn()
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(<QueryClientProvider client={client}><FluentProvider theme={webLightTheme}><MemoryRouter>
    {kind === 'register' ? <GatewayRegistrationEditor onClose={onClose} onSaved={onSaved} />
      : <GatewayRecordEditor gateway={poolGateway} onClose={onClose} onSaved={onSaved} />}
  </MemoryRouter></FluentProvider></QueryClientProvider>)
  return { onClose, onSaved }
}
describe('Gateway workspace editors', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: ['development', 'production'].map((key) => ({ key, displayName: key === 'development' ? 'Development' : 'Production',
        color: 'brand', description: null, production: key === 'production', aliases: [], acceptsEndpointsFrom: [],
        order: 10, builtIn: true, usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 } })),
      requireClassification: false, unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [], updatedAt: null,
    })
    api.assignEnvironments.mockResolvedValue({ results: [], warnings: [], grantsCarried: 0 })
    api.updateGateway.mockResolvedValue(poolGateway)
    api.registerGateway.mockResolvedValue({ ...poolGateway, managementMode: 'observe' })
  })
  it('registers only the MOSAIC record with no management-mode or APIM mutation', async () => {
    const user = userEvent.setup()
    const { onSaved } = renderEditor('register')
    await user.type(screen.getByRole('textbox', { name: /API Management resource ID/ }), poolGateway.azureResourceId)
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Environment/ })).toHaveValue('Development'))
    await user.click(screen.getByRole('button', { name: 'Check access and onboard' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(api.registerGateway.mock.calls[0][0]).toEqual({ azureResourceId: poolGateway.azureResourceId,
      name: undefined, environment: 'development' })
    expect(api.updateGateway).not.toHaveBeenCalled()
    expect(api.assignEnvironments).not.toHaveBeenCalled()
  })
  it('saves a display name without changing management mode or environment', async () => {
    const user = userEvent.setup()
    const { onSaved } = renderEditor()
    await user.clear(screen.getByRole('textbox', { name: 'Display name' }))
    await user.type(screen.getByRole('textbox', { name: 'Display name' }), 'Gateway record')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(api.updateGateway).toHaveBeenCalledWith(poolGateway.id, { name: 'Gateway record' })
    expect(api.assignEnvironments).not.toHaveBeenCalled()
  })
  it('requires explicit grant acknowledgment before retrying an environment change', async () => {
    const user = userEvent.setup()
    api.assignEnvironments.mockRejectedValueOnce(new ApiError('Acknowledge grants', 409, {
      details: { reason: 'grantsAcknowledgmentRequired', grants: [], grantCount: 1, principalCount: 1, truncated: false },
    }))
    renderEditor()
    await user.click(await screen.findByRole('combobox', { name: /^Environment/ }))
    await user.click(await screen.findByRole('option', { name: 'Development' }))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    const acknowledgment = await screen.findByRole('checkbox', { name: /active grants/ })
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
    expect(api.assignEnvironments).toHaveBeenCalledTimes(1)
    await user.click(acknowledgment)
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    await waitFor(() => expect(api.assignEnvironments).toHaveBeenCalledTimes(2))
    expect(api.assignEnvironments.mock.calls[1][0]).toEqual({
      assignments: [{ resourceKind: 'gateway', resourceId: poolGateway.id, environment: 'development' }],
      acknowledgeGrants: true,
    })
  })
  it('reviews blocking publications before explicitly accepting related classifications', async () => {
    const user = userEvent.setup()
    api.assignEnvironments.mockRejectedValueOnce(new ApiError('Blocked', 409, {
      details: { reason: 'publicationsBlocked', publications: [], suggestedAssignments: [
        { resourceKind: 'modelEndpoint', resourceId: 'endpoint_1', environment: 'development' },
      ] },
    }))
    const { onSaved } = renderEditor()
    await user.click(await screen.findByRole('combobox', { name: /^Environment/ }))
    await user.click(await screen.findByRole('option', { name: 'Development' }))
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    const alsoClassify = await screen.findByRole('button', { name: 'Also classify this resource as Development' })
    expect(api.assignEnvironments).toHaveBeenCalledTimes(1)
    expect(onSaved).not.toHaveBeenCalled()
    await user.click(alsoClassify)
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    expect(api.assignEnvironments.mock.calls[1][0]).toEqual({
      assignments: [
        { resourceKind: 'gateway', resourceId: poolGateway.id, environment: 'development' },
        { resourceKind: 'modelEndpoint', resourceId: 'endpoint_1', environment: 'development' },
      ], acknowledgeGrants: false,
    })
  })
  it('keeps a failed save in the workspace and protects a dirty cancel', async () => {
    const user = userEvent.setup()
    const { onClose, onSaved } = renderEditor()
    api.updateGateway.mockRejectedValue(new Error('Gateway record is locked.'))
    await user.type(screen.getByRole('textbox', { name: 'Display name' }), ' revised')
    await user.click(screen.getByRole('button', { name: 'Save changes' }))
    expect(await screen.findByText('Gateway record is locked.')).toBeVisible()
    expect(onSaved).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    const confirm = await screen.findByRole('dialog', { name: 'Discard unsaved changes?' })
    await user.click(within(confirm).getByRole('button', { name: 'Stay' }))
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: 'Display name' })).toHaveValue(`${poolGateway.name} revised`)
  })
  it('does not permit abandoning a registration while its access check is pending', async () => {
    const user = userEvent.setup()
    api.registerGateway.mockImplementation(() => new Promise(() => {}))
    const { onClose } = renderEditor('register')
    await user.type(screen.getByRole('textbox', { name: /API Management resource ID/ }), poolGateway.azureResourceId)
    await waitFor(() => expect(screen.getByRole('combobox', { name: /^Environment/ })).toHaveValue('Development'))
    await user.click(screen.getByRole('button', { name: 'Check access and onboard' }))
    await user.click(screen.getByRole('button', { name: 'Back to gateways' }))
    expect(await screen.findByRole('dialog', { name: 'Operation in progress' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Discard changes' })).not.toBeInTheDocument()
    expect(onClose).not.toHaveBeenCalled()
  })
})
