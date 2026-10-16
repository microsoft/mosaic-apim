import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { AccessRequest, CatalogEntry, PortalCostCenter } from '../types'
import { CatalogPage } from './CatalogPage'
import { resourceFromCatalog } from '../entitlement-format'
import { resourceKey } from '../model-presentation'

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

const costCenters: PortalCostCenter[] = [
  { id: 'cc-general', name: 'General', code: 'GEN', isDefault: true, keysAllowed: true },
  { id: 'cc-research', name: 'Research', code: 'RES', isDefault: false, keysAllowed: true },
]

/** A model several endpoints serve behind one API. The catalog shows only the model. */
const servedModelEntry: CatalogEntry = {
  kind: 'poolModel',
  id: 'pool_model_opus',
  scopeId: 'pool_anthropic',
  displayName: 'Claude Opus',
  summary: null,
  gatewayId: 'gateway-1',
  gatewayName: 'production gateway',
  environment: 'production',
  entitled: false,
  requestState: null,
  enforced: null,
  apiStyle: 'anthropicMessages',
  capacity: 'provisionedWithOverflow',
}

function renderPage(entries: CatalogEntry[], requests: AccessRequest[], centers: PortalCostCenter[] | Error = costCenters) {
  let currentRequests = [...requests]
  const createAccessRequest = vi.fn(async (payload) => {
    const created = {
      ...pendingRequest,
      ...payload,
      id: 'request-created',
      state: 'pending' as const,
    }
    currentRequests = [...currentRequests, created]
    return created
  })
  const withdrawAccessRequest = vi.fn(async (requestId: string) => {
    currentRequests = currentRequests.map((request) =>
      request.id === requestId ? { ...request, state: 'withdrawn' as const } : request,
    )
  })
  mocks.api = {
    listCatalog: async () => entries.map((entry) => {
      if (entry.requestedCostCenterIds || entry.requestState === 'pending') return entry
      return {
        ...entry,
        requestedCostCenterIds: currentRequests
          .filter((request) =>
            request.state === 'pending' &&
            request.resource.kind === entry.kind &&
            request.resource.id === entry.id &&
            (request.resource.scopeId ?? null) === (entry.scopeId ?? null),
          )
          .map((request) => request.costCenterId)
          .filter((id): id is string => Boolean(id)),
      }
    }),
    listCostCenters: async () => {
      if (centers instanceof Error) throw centers
      return centers
    },
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
    listAccessRequests: async () => currentRequests,
    createAccessRequest,
    withdrawAccessRequest,
  } as unknown as PortalApi
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter><CatalogPage /></MemoryRouter>
    </QueryClientProvider>,
  )
  return { createAccessRequest, withdrawAccessRequest }
}

async function openAccessOptions(name = 'Weather tools') {
  await userEvent.click(await screen.findByRole('button', { name: `View access options for ${name}` }))
}

describe('CatalogPage', () => {
  it('shows Government key and network requirements before requesting and blocks key-disallowed cost centers', async () => {
    const { createAccessRequest } = renderPage(
      [{ ...catalogEntry, azureCloud: 'government' }], [],
      costCenters.map((center) => ({ ...center, keysAllowed: false })),
    )
    expect(await screen.findByLabelText('Azure Government')).toBeVisible()
    await openAccessOptions()
    expect(screen.getByText(/Azure Government requires a direct grant and a subscription key/)).toBeVisible()
    expect(screen.getByText(/This cost center does not allow keys/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Request access' })).toBeDisabled()
    expect(createAccessRequest).not.toHaveBeenCalled()
  })
  it('groups a model and requires an exact offering before requesting access', async () => {
    const first: CatalogEntry = { ...servedModelEntry, model: { name: 'claude-opus-4-5', format: 'Anthropic', version: null } }
    const second: CatalogEntry = { ...first, scopeId: 'other-pool', displayName: 'Other endpoint / Opus', environment: 'development' }
    const { createAccessRequest } = renderPage([first, second], [
      { ...pendingRequest, resource: resourceFromCatalog(first), costCenterId: 'cc-general' },
    ])
    expect(await screen.findAllByRole('heading', { name: 'claude-opus-4-5', level: 2 })).toHaveLength(1)
    expect(screen.queryByText('Other endpoint / Opus')).not.toBeInTheDocument()
    await openAccessOptions('claude-opus-4-5')
    const drawer = screen.getByRole('dialog', { name: 'Access to claude-opus-4-5' })
    expect(within(drawer).queryByRole('button', { name: 'Request access' })).not.toBeInTheDocument()
    const selector = within(drawer).getByRole('combobox', { name: 'Access option' })
    await userEvent.selectOptions(selector, resourceKey(resourceFromCatalog(first)))
    expect(await within(drawer).findByRole('button', { name: 'Withdraw' })).toBeVisible()
    await userEvent.selectOptions(selector, resourceKey(resourceFromCatalog(second)))
    expect(within(drawer).queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
    const reason = await within(drawer).findByRole('textbox', { name: 'Justification for Other endpoint / Opus' })
    await userEvent.type(reason, 'Only the second option')
    await userEvent.selectOptions(selector, resourceKey(resourceFromCatalog(first)))
    await userEvent.selectOptions(selector, resourceKey(resourceFromCatalog(second)))
    expect(within(drawer).getByRole('textbox', { name: 'Justification for Other endpoint / Opus' })).toHaveValue('')
    await userEvent.click(await within(drawer).findByRole('button', { name: 'Request access' }))
    await waitFor(() => expect(createAccessRequest).toHaveBeenCalledWith({
      resource: { kind: 'poolModel', id: second.id, scopeId: 'other-pool' },
      costCenterId: 'cc-general', justification: undefined,
    }))
  })

  it('unmounts the request form and restores focus when options close', async () => {
    renderPage([catalogEntry], [])
    const opener = await screen.findByRole('button', { name: 'View access options for Weather tools' })
    await userEvent.click(opener)
    const justification = await screen.findByRole('textbox', { name: 'Justification for Weather tools' })
    await userEvent.type(justification, 'Only this request')
    await userEvent.click(screen.getByRole('button', { name: 'Close details' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await waitFor(() => expect(opener).toHaveFocus())
    await userEvent.click(opener)
    expect(await screen.findByRole('textbox', { name: 'Justification for Weather tools' })).toHaveValue('')
  })
  it('offers request access for entries without an open request', async () => {
    renderPage([catalogEntry], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    await openAccessOptions()
    expect(screen.getByRole('button', { name: 'Request access' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Withdraw' })).not.toBeInTheDocument()
  })

  it('preselects the default cost center and sends the selected id', async () => {
    const user = userEvent.setup()
    const { createAccessRequest } = renderPage([catalogEntry], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    await openAccessOptions()
    const selector = await screen.findByRole('combobox', { name: 'Cost center for Weather tools' })
    expect(selector).toHaveValue('cc-general')
    expect(screen.getByRole('option', { name: 'General (GEN)' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Research (RES)' })).toBeInTheDocument()

    await user.selectOptions(selector, 'cc-research')
    await user.click(screen.getByRole('button', { name: 'Request access' }))

    await waitFor(() => expect(createAccessRequest).toHaveBeenCalled())
    expect(createAccessRequest).toHaveBeenCalledWith({
      resource: { kind: 'mcpServer', id: 'mcp-weather', scopeId: null },
      costCenterId: 'cc-research',
      justification: undefined,
    })
  })

  it('shows submission acknowledgement only while the created request is open for the selected cost center', async () => {
    const user = userEvent.setup()
    const { withdrawAccessRequest } = renderPage([catalogEntry], [])

    await openAccessOptions()
    const selector = await screen.findByRole('combobox', { name: 'Cost center for Weather tools' })
    await user.click(screen.getByRole('button', { name: 'Request access' }))

    expect(await screen.findByText(/Request submitted\./)).toBeVisible()
    await user.selectOptions(selector, 'cc-research')
    expect(screen.queryByText(/Request submitted\./)).not.toBeInTheDocument()

    await user.selectOptions(selector, 'cc-general')
    expect(await screen.findByText(/Request submitted\./)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Withdraw' }))

    await waitFor(() => expect(withdrawAccessRequest).toHaveBeenCalledWith('request-created'))
    await waitFor(() => expect(screen.queryByText(/Request submitted\./)).not.toBeInTheDocument())
  })

  it('marks cost centers that already have access or a request', async () => {
    renderPage([
      {
        ...catalogEntry,
        entitled: true,
        requestState: 'pending',
        entitledCostCenterIds: ['cc-general'],
        requestedCostCenterIds: ['cc-research'],
      },
    ], [{ ...pendingRequest, costCenterId: 'cc-research' }])

    await openAccessOptions()
    expect(await screen.findByRole('option', { name: 'General (GEN) — already entitled' })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: 'Research (RES) — request open' })).toBeInTheDocument()
    expect(screen.getByText('All cost centers already have access or an open request.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Request access' })).not.toBeInTheDocument()
  })

  it('falls back to the server default when cost centers cannot load', async () => {
    const user = userEvent.setup()
    const { createAccessRequest } = renderPage([catalogEntry], [], new Error('No cost centers'))

    await openAccessOptions()
    expect(await screen.findByText(/Cost centers could not load/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Request access' }))

    await waitFor(() => expect(createAccessRequest).toHaveBeenCalledWith({
      resource: { kind: 'mcpServer', id: 'mcp-weather', scopeId: null },
      justification: undefined,
    }))
  })

  it('switches to withdraw when a request is pending', async () => {
    renderPage([{ ...catalogEntry, requestState: 'pending' }], [pendingRequest])

    await openAccessOptions()
    expect(await screen.findByText('A request is already open for this cost center.')).toBeVisible()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Withdraw' })).toBeEnabled())
    expect(screen.queryByRole('button', { name: 'Request access' })).not.toBeInTheDocument()
  })

  it('offers a request under another cost center when the default already has access', async () => {
    const user = userEvent.setup()
    renderPage([{ ...catalogEntry, entitled: true, entitledCostCenterIds: ['cc-general'] }], [])

    await openAccessOptions()
    const selector = await screen.findByRole('combobox', { name: 'Cost center for Weather tools' })
    expect(selector).toHaveValue('cc-general')
    expect(screen.getByText('Already entitled for this cost center')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Request access' })).not.toBeInTheDocument()

    await user.selectOptions(selector, 'cc-research')
    expect(screen.getByRole('button', { name: 'Request access' })).toBeEnabled()
  })

  it.each([
    [true, 'Enforced by the gateway'],
    [false, 'Recorded, not enforced'],
  ])('labels MCP catalog entries with enforced=%s', async (enforced, label) => {
    renderPage([{ ...catalogEntry, enforced }], [])

    expect(await screen.findByText('Weather tools')).toBeVisible()
    await openAccessOptions()
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

  it('offers a model served behind one API as a model, with its API style and capacity', async () => {
    renderPage([servedModelEntry], [])

    expect(await screen.findByRole('heading', { level: 2, name: 'Claude Opus' })).toBeVisible()
    expect(screen.queryByText('Model · Anthropic Messages API · production gateway')).not.toBeInTheDocument()
    await openAccessOptions('Claude Opus')
    expect(screen.getByText('Model · Anthropic Messages API · production gateway')).toBeVisible()
    expect(screen.getByText('Provisioned, with pay-as-you-go overflow')).toBeVisible()
    expect(screen.queryByText('Enforced by the gateway')).not.toBeInTheDocument()
    // Nothing on the page says how the model is served.
    expect(document.body).not.toHaveTextContent(/\bpools?\b/i)
    expect(document.body.innerHTML).not.toContain('pool_anthropic')
  })

  it.each([
    ['provisioned', 'Provisioned'],
    ['payAsYouGo', 'Pay-as-you-go'],
  ] as const)('labels %s capacity', async (capacity, label) => {
    renderPage([{ ...servedModelEntry, capacity }], [])

    expect(await screen.findByText('Claude Opus')).toBeVisible()
    await openAccessOptions('Claude Opus')
    expect(screen.getByText(label)).toBeVisible()
  })

  it('omits the capacity badge when capacity is unknown', async () => {
    renderPage([{ ...servedModelEntry, capacity: null, apiStyle: null }], [])

    expect(await screen.findByText('Claude Opus')).toBeVisible()
    await openAccessOptions('Claude Opus')
    expect(screen.getByText('Model · production gateway')).toBeVisible()
    expect(screen.queryByText(/Provisioned|Pay-as-you-go/)).not.toBeInTheDocument()
  })

  it('requests a model served behind one API under the scope the catalog gave it', async () => {
    const user = userEvent.setup()
    const { createAccessRequest } = renderPage([servedModelEntry], [])

    await openAccessOptions('Claude Opus')
    const selector = await screen.findByRole('combobox', { name: 'Cost center for Claude Opus' })
    expect(selector).toHaveValue('cc-general')
    await user.click(screen.getByRole('button', { name: 'Request access' }))

    await waitFor(() => expect(createAccessRequest).toHaveBeenCalledWith({
      resource: { kind: 'poolModel', id: 'pool_model_opus', scopeId: 'pool_anthropic' },
      costCenterId: 'cc-general',
      justification: undefined,
    }))
  })

  it('lists every kind of model under Models', async () => {
    const user = userEvent.setup()
    renderPage([
      catalogEntry,
      servedModelEntry,
      { ...catalogEntry, id: 'chat', kind: 'modelApi', displayName: 'Chat model API', enforced: null },
    ], [])

    expect(await screen.findByText('Claude Opus')).toBeVisible()
    expect(screen.getAllByText('Model')).toHaveLength(2)

    await user.selectOptions(screen.getByLabelText('Resource type'), 'model')
    expect(screen.getByText('Claude Opus')).toBeVisible()
    expect(screen.getByText('Chat model API')).toBeVisible()
    expect(screen.queryByText('Weather tools')).not.toBeInTheDocument()

    await user.selectOptions(screen.getByLabelText('Resource type'), 'mcpServer')
    expect(screen.getByText('Weather tools')).toBeVisible()
    expect(screen.queryByText('Claude Opus')).not.toBeInTheDocument()
    expect(screen.queryByText('Chat model API')).not.toBeInTheDocument()
  })
})
