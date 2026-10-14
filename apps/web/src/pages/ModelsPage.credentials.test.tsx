import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, configure, getConfig, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { cloneElement, isValidElement, type ReactNode, useState } from 'react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ResourceWorkbenchProps } from '../components/ResourceWorkbench'
import type { ModelApi, ModelEndpoint } from '../types'
import { ModelsPage } from './ModelsPage'

const api = vi.hoisted(() => ({
  getEnvironmentCatalog: vi.fn(),
  listGateways: vi.fn(),
  listPublications: vi.fn(),
  listModelApis: vi.fn(),
  listModelEndpoints: vi.fn(),
  listSuggestedModelEndpoints: vi.fn(),
  listEndpointPools: vi.fn(),
  updateModelEndpoint: vi.fn(),
}))
const importedCredentialStore = vi.hoisted(() => vi.fn())
const originalAsyncTimeout = getConfig().asyncUtilTimeout

vi.mock('../api', () => ({
  useMosaicApi: () => api,
  ApiError: class extends Error {},
}))

// Imported details currently have no credential control. This stateful page-seam probe exercises
// the same current-ID/local-secret failure mode without adding a production credential flow.
function ImportedCredentialProbe({ recordId }: { recordId: string }) {
  const [open, setOpen] = useState(false)
  const [credential, setCredential] = useState('')
  return <>
    <button onClick={() => setOpen(true)}>Open imported credential session</button>
    {open && <form aria-label={`Credential session for ${recordId}`} onSubmit={(event) => {
      event.preventDefault()
      if (credential.trim()) importedCredentialStore(recordId, credential.trim())
    }}>
      <label>Imported credential draft<input type="password" required value={credential}
        onChange={(event) => setCredential(event.target.value)} /></label>
      <button type="submit">Store imported credential</button>
    </form>}
  </>
}

vi.mock('../components/ResourceWorkbench', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../components/ResourceWorkbench')>()
  return {
    ...actual,
    ResourceWorkbench: (props: ResourceWorkbenchProps<{ id: string }>) =>
      <actual.ResourceWorkbench {...props} renderDetail={(record) => {
        const detail = props.renderDetail(record)
        if (props.listLabel !== 'Imported model APIs' || !isValidElement<{ children: ReactNode }>(detail)) return detail
        return cloneElement(detail, undefined, detail.props.children,
          <ImportedCredentialProbe key="credential-probe" recordId={record.id} />)
      }} />,
  }
})

function endpoint(id: string, name: string): ModelEndpoint {
  return {
    id, name, tenantId: 'fictional-tenant', provider: 'azureAiFoundry',
    endpoint: 'https://fictional-models.services.ai.azure.com/',
    azureResourceId: null, subscriptionId: null, resourceGroup: null,
    accountName: 'fictional-models', projectName: null, environment: null,
    azureEnvironmentTag: null, environmentLabel: null, authMode: 'apiKey',
    credentialReferenceId: `fictional-credential-${id}`, keyStoredByMosaic: true,
    declaredDeployments: [], status: 'connected',
    access: {
      canRead: true, evaluation: 'probe', checkedAt: '2026-09-01T12:00:00Z',
      missingActions: [], remediation: null, message: 'The fictional endpoint accepts the key.',
    },
    runtimeAccess: [],
    capabilities: { managementApiVersion: '2024-10-01', notes: [] },
    inventory: { deployments: 0, availableModels: 0, succeededDeployments: 0, deprecatedDeployments: 0 },
    lastSyncedAt: null, lastSyncError: null,
    createdAt: '2026-09-01T12:00:00Z', updatedAt: '2026-09-01T12:00:00Z',
  }
}

function importedApi(id: string, displayName: string): ModelApi {
  return {
    id, displayName, tenantId: 'fictional-tenant', gatewayId: 'fictional-gateway',
    apiName: id, path: id, serviceUrl: 'https://fictional-models.example.test/',
    protocols: ['https'], aiKind: 'azureOpenAi', aiSignals: [], subscriptionRequired: true,
    operationCount: 1, productNames: [], visibility: 'catalog', selection: 'detected',
    importedFromSnapshotId: 'fictional-snapshot', importedBy: 'fictional-admin',
    importedAt: '2026-09-01T12:00:00Z', createdAt: '2026-09-01T12:00:00Z',
    updatedAt: '2026-09-01T12:00:00Z',
  }
}

function renderPage(section: 'endpoints' | 'imported', selectedId: string) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const router = createMemoryRouter([{ path: '*', element: <ModelsPage /> }], {
    initialEntries: [`/models?tab=${section}&selected=${selectedId}`],
  })
  render(<QueryClientProvider client={queryClient}><RouterProvider router={router} /></QueryClientProvider>)
  return { queryClient, router }
}

describe('Models selected credential sessions', () => {
  afterEach(() => configure({ asyncUtilTimeout: originalAsyncTimeout }))

  beforeEach(() => {
    configure({ asyncUtilTimeout: 10_000 })
    vi.resetAllMocks()
    api.getEnvironmentCatalog.mockResolvedValue({
      environments: [], requireClassification: false,
      unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
      compatibility: [], updatedAt: null,
    })
    api.listGateways.mockResolvedValue([])
    api.listPublications.mockResolvedValue([])
    api.listModelApis.mockResolvedValue([])
    api.listModelEndpoints.mockResolvedValue([])
    api.listSuggestedModelEndpoints.mockResolvedValue({
      suggestions: [], scanIssues: [], partialScans: [], subscriptionsScanned: 0,
      scanStatus: 'notConfigured', scanMessage: null, scanRemediation: [],
    })
    api.listEndpointPools.mockResolvedValue([])
  })

  it('destroys an endpoint credential session on browser Back but preserves it on same-ID refresh', async () => {
    const user = userEvent.setup()
    const a = endpoint('endpoint_a', 'Endpoint A')
    const b = endpoint('endpoint_b', 'Endpoint B')
    api.listModelEndpoints.mockResolvedValue([b, a])
    const { queryClient, router } = renderPage('endpoints', b.id)
    const list = await screen.findByRole('list', { name: 'Registered model endpoints' })
    await user.click(within(list).getByRole('button', { name: /Endpoint A/ }))
    await user.click(await screen.findByRole('button', { name: 'Replace API key' }))
    const dialog = await screen.findByRole('dialog', { name: 'Replace the API key for Endpoint A' })
    const field = within(dialog).getByLabelText(/New API key/)
    await user.type(field, 'fictional-key-for-A')

    api.listModelEndpoints.mockResolvedValue([b, { ...a, name: 'Endpoint A refreshed' }])
    await act(() => queryClient.invalidateQueries({ queryKey: ['model-endpoints'] }))
    expect(await screen.findByRole('dialog', { name: 'Replace the API key for Endpoint A refreshed' })).toBe(dialog)
    expect(field).toHaveValue('fictional-key-for-A')

    await act(() => router.navigate(-1))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    expect(field).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Endpoint B' })).toBeVisible()
    expect(api.updateModelEndpoint).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Replace API key' }))
    const bDialog = await screen.findByRole('dialog', { name: 'Replace the API key for Endpoint B' })
    expect(within(bDialog).getByLabelText(/New API key/)).toHaveValue('')
    await user.click(within(bDialog).getByRole('button', { name: 'Store new key' }))
    expect(api.updateModelEndpoint).not.toHaveBeenCalled()
  }, 30_000)

  it('destroys imported-detail credential state on selection and history without retargeting its draft', async () => {
    const user = userEvent.setup()
    const a = importedApi('api_a', 'Imported A')
    const b = importedApi('api_b', 'Imported B')
    api.listModelApis.mockResolvedValue([b, a])
    const { queryClient, router } = renderPage('imported', b.id)
    const list = await screen.findByRole('list', { name: 'Imported model APIs' })
    await user.click(within(list).getByRole('button', { name: /Imported A/ }))
    await user.click(screen.getByRole('button', { name: 'Open imported credential session' }))
    const field = screen.getByLabelText('Imported credential draft')
    await user.type(field, 'fictional-import-key-for-A')

    api.listModelApis.mockResolvedValue([b, { ...a, displayName: 'Imported A refreshed' }])
    await act(() => queryClient.invalidateQueries({ queryKey: ['model-apis'] }))
    expect(screen.getByLabelText('Imported credential draft')).toBe(field)
    expect(field).toHaveValue('fictional-import-key-for-A')

    await act(() => router.navigate(-1))
    await waitFor(() => expect(field).not.toBeInTheDocument())
    expect(screen.queryByLabelText('Imported credential draft')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Imported B' })).toBeVisible()
    expect(importedCredentialStore).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: 'Open imported credential session' }))
    expect(screen.getByLabelText('Imported credential draft')).toHaveValue('')
    await user.click(screen.getByRole('button', { name: 'Store imported credential' }))
    expect(importedCredentialStore).not.toHaveBeenCalled()
    await user.type(screen.getByLabelText('Imported credential draft'), 'fictional-import-key-for-B')
    await user.click(within(list).getByRole('button', { name: /Imported A refreshed/ }))
    expect(screen.queryByLabelText('Imported credential draft')).not.toBeInTheDocument()
    expect(importedCredentialStore).not.toHaveBeenCalled()
  }, 30_000)
})
