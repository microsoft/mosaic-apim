import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { Entitlement } from '../types'
import { callRateError, describeLimits } from '../entitlement-limits'
import { EntitlementsPage } from './EntitlementsPage'
import { accessPlan, directGrant, modelPublication, publishedModelApi } from '../test/model-access'

const entitlement: Entitlement = {
  id: 'entitlement_1',
  tenantId: 'tenant',
  subject: { kind: 'group', id: 'group_1' },
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  enabled: true,
  enforcement: {
    tokens: {
      counterKeyExpression: '@(context.Subscription?.Key)',
      tokensPerMinute: 10000,
      tokenQuota: 5000000,
      tokenQuotaPeriod: 'Monthly',
      estimatePromptTokens: true,
    },
  },
  binding: null,
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
}

const api = {
  listEntitlements: vi.fn(),
  listPrincipals: vi.fn(),
  listGroups: vi.fn(),
  listModelApis: vi.fn(),
  listMcpServers: vi.fn(),
  listAccessRequests: vi.fn(),
  listPublications: vi.fn(),
  resolveEntitlements: vi.fn(),
  createEntitlement: vi.fn(),
  updateEntitlement: vi.fn(),
  deleteEntitlement: vi.fn(),
  getPublication: vi.fn(),
  createPublishPlan: vi.fn(),
  applyPublishPlan: vi.fn(),
  updatePublication: vi.fn(),
  linkPublicationModelApi: vi.fn(),
}

vi.mock('../api', () => ({ useMosaicApi: () => api }))

function renderPage() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter>
        <FluentProvider theme={webLightTheme}>
          <EntitlementsPage />
        </FluentProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

describe('EntitlementsPage', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    api.listEntitlements.mockResolvedValue([entitlement])
    api.listPrincipals.mockResolvedValue([{
      id: 'principal_1', tenantId: 'tenant', objectId: 'user-object-1', kind: 'user',
      label: 'Ada Lovelace', createdAt: '', updatedAt: '',
    }])
    api.listGroups.mockResolvedValue([{
      id: 'group_1', tenantId: 'tenant', name: 'Engineering', createdAt: '', updatedAt: '',
    }])
    api.listModelApis.mockResolvedValue([{ ...publishedModelApi, publicationId: null, importedFromSnapshotId: 'snapshot_1' }])
    api.listMcpServers.mockResolvedValue([])
    api.listAccessRequests.mockResolvedValue([])
    api.listPublications.mockResolvedValue([])
    api.resolveEntitlements.mockResolvedValue([])
    api.createPublishPlan.mockResolvedValue(accessPlan)
    api.getPublication.mockResolvedValue(modelPublication)
  })

  it('renders live grants with their subject, resource, and binding state', async () => {
    renderPage()

    expect(await screen.findByText('Engineering (group)')).toBeVisible()
    expect(screen.getByText('Chat completions (model API)')).toBeVisible()
    // A grant with no binding must say so: consumption cannot be attributed without one.
    expect(screen.getByText('Not bound')).toBeVisible()
    expect(screen.getByText('Live data')).toBeVisible()
    expect(screen.queryByText('Sample data')).not.toBeInTheDocument()
  })

  it('states limits as sentences rather than policy markup', async () => {
    renderPage()

    expect(await screen.findByText('Limits usage to 10,000 tokens per minute.')).toBeVisible()
    expect(screen.getByText('Allows 5,000,000 tokens per month.')).toBeVisible()
  })

  it('distinguishes pending desired intent from earlier applied access and inherited limits', async () => {
    api.listEntitlements.mockResolvedValue([{
      ...directGrant, runtime: { ...directGrant.runtime, status: 'pending' },
    }])
    api.listPublications.mockResolvedValue([{ ...modelPublication, accessState: 'pending' }])
    renderPage()

    const table = await screen.findByRole('table', { name: 'Entitlements' })
    expect(within(table).getByText('Desired: enabled')).toBeVisible()
    expect(within(table).getByText('Saved, not applied')).toBeVisible()
    expect(within(table).getByText('An earlier configuration was applied; saved changes are pending.')).toBeVisible()
    expect(within(table).getByText('Last applied methods: Subscription key OR Entra token')).toBeVisible()
    expect(within(table).getAllByText('Publication: Limits usage to 12,000 tokens per minute.')).toHaveLength(2)
    expect(within(table).queryByText(/unrestricted/i)).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('saves revocation intent instead of deleting managed access, and can re-enable it', async () => {
    const user = userEvent.setup()
    api.listEntitlements.mockResolvedValue([directGrant])
    api.listPublications.mockResolvedValue([modelPublication])
    api.updateEntitlement.mockImplementation(async (_id, { enabled }: { enabled: boolean }) => {
      const updated = {
        ...directGrant, enabled,
        runtime: { ...directGrant.runtime, status: enabled ? 'pending' : 'revocationPending' },
      }
      api.listEntitlements.mockResolvedValue([updated])
      return updated
    })
    renderPage()

    await user.click(await screen.findByRole('button', { name: 'Revoke' }))
    expect(api.updateEntitlement).toHaveBeenCalledWith(directGrant.id, { enabled: false })
    expect(api.deleteEntitlement).not.toHaveBeenCalled()
    expect(await screen.findByText('Revocation pending')).toBeVisible()
    expect(screen.getByText(/Access may still work until/)).toBeVisible()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Re-enable' }))
    expect(api.updateEntitlement).toHaveBeenLastCalledWith(directGrant.id, { enabled: true })
    expect(await screen.findByText('Saved, not applied')).toBeVisible()
  })

  it('preserves desired-only group grant deletion without claiming APIM revocation', async () => {
    const user = userEvent.setup()
    api.deleteEntitlement.mockResolvedValue(undefined)
    renderPage()
    expect(await screen.findByText('Desired state only')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Revoke' }))
    expect(api.deleteEntitlement).toHaveBeenCalledWith(entitlement.id)
    expect(api.updateEntitlement).not.toHaveBeenCalled()
    expect(await screen.findByText('Removed the desired-state grant. API Management is unchanged.')).toBeVisible()
  })

  it('keeps imported direct and MCP grants desired-only', async () => {
    api.listEntitlements.mockResolvedValue([
      { ...directGrant, binding: { gatewayId: 'gateway_1', source: 'manual' }, runtime: null },
      { ...directGrant, id: 'mcp-grant', resource: { kind: 'mcpServer', id: 'mcp_1' }, binding: null, runtime: null },
    ])
    renderPage()
    expect(await screen.findAllByText('Desired state only')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: 'Connection info' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Manage model' })).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('creates direct grant intent on the canonical model without changing Azure', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.listModelApis.mockResolvedValue([publishedModelApi])
    api.createEntitlement.mockResolvedValue(directGrant)
    renderPage()
    await screen.findByRole('option', { name: /Published chat/ })
    fireEvent.change(screen.getByLabelText('Published model'), { target: { value: modelPublication.id } })
    await user.click(await screen.findByRole('button', { name: 'Add direct grant' }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).queryByRole('option', { name: 'Engineering (group)' })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('combobox', { name: 'Resource' })).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith({
      subject: { kind: 'user', id: 'principal_1' },
      resource: { kind: 'modelApi', id: publishedModelApi.id },
      enforcement: null,
      notes: null,
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
    expect(await screen.findByText(/Saved grant intent. API Management is unchanged/)).toBeVisible()
  })

  it('reviews the full model snapshot, not a row-scoped apply', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.listEntitlements.mockResolvedValue([directGrant])
    api.createPublishPlan.mockResolvedValue({
      ...accessPlan,
      accessSnapshot: {
        ...accessPlan.accessSnapshot,
        grants: [...accessPlan.accessSnapshot!.grants, {
          ...accessPlan.accessSnapshot!.grants[0], entitlementId: 'other-grant', displayName: 'Another pending application',
          subject: { kind: 'application', id: 'app_1' }, enabled: false,
        }],
      },
    })
    renderPage()
    await screen.findByRole('option', { name: /Published chat/ })
    fireEvent.change(screen.getByLabelText('Published model'), { target: { value: modelPublication.id } })
    await user.click(await screen.findByRole('button', { name: 'Review model changes' }))
    const table = await screen.findByRole('table', { name: 'All target model grants' })
    expect(within(table).getByText('Ada Lovelace')).toBeVisible()
    expect(within(table).getByText('Another pending application')).toBeVisible()
    expect(within(table).getByText('Disabled — revoke both methods')).toBeVisible()
    expect(api.createPublishPlan).toHaveBeenCalledWith(modelPublication.id)
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it.each([true, false])('uses the supported shared counter for governed direct limits (governed: %s)', async (governed) => {
    const user = userEvent.setup()
    if (governed) {
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
    }
    api.createEntitlement.mockResolvedValue(directGrant)
    renderPage()
    const add = await screen.findByRole('button', { name: 'Add entitlement' })
    await waitFor(() => expect(add).toBeEnabled())
    if (governed) await screen.findByRole('option', { name: /Published chat/ })
    await user.click(add)
    const dialog = await screen.findByRole('dialog')
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject' }), { target: { value: 'principal_1' } })
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Resource' }), { target: { value: publishedModelApi.id } })
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }), '1000')
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Calls' }), '20')
    await user.click(within(dialog).getByRole('button', { name: 'Grant access' }))
    await waitFor(() => expect(api.createEntitlement).toHaveBeenCalledWith({
      subject: directGrant.subject,
      resource: directGrant.resource,
      enforcement: {
        tokens: {
          counterKeyExpression: governed ? '@(context.Subscription.Id)' : '@(context.Subscription?.Key)',
          tokensPerMinute: 1000,
          estimatePromptTokens: true,
        },
        requests: {
          counterKeyExpression: governed ? '@(context.Subscription.Id)' : '@(context.Subscription?.Key)',
          calls: 20,
          renewalPeriodSeconds: 60,
        },
      },
      notes: null,
    }))
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })

  it('shows planning failures without inventing a successful apply', async () => {
    const user = userEvent.setup()
    api.listPublications.mockResolvedValue([modelPublication])
    api.createPublishPlan.mockRejectedValue(new Error('This gateway does not support the selected policy.'))
    renderPage()
    await screen.findByRole('option', { name: /Published chat/ })
    fireEvent.change(screen.getByLabelText('Published model'), { target: { value: modelPublication.id } })
    await user.click(await screen.findByRole('button', { name: 'Review model changes' }))
    expect(await screen.findByText('This gateway does not support the selected policy.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Apply plan' })).not.toBeInTheDocument()
    expect(api.applyPublishPlan).not.toHaveBeenCalled()
  })
})

describe('describeLimits', () => {
  it('does not confuse an absent grant limit with unrestricted gateway access', () => {
    expect(describeLimits({ ...entitlement, enforcement: null })).toEqual([
      'No grant-specific limit is configured.',
    ])
    expect(describeLimits({ ...entitlement, enforcement: {} })).toEqual([
      'No grant-specific limit is configured.',
    ])
    expect(describeLimits({ enforcement: null }, modelPublication.enforcement)).toEqual([
      'No grant-specific limit is configured.',
      'Publication: Limits usage to 12,000 tokens per minute.',
    ])
  })

  it('describes request limits separately from token limits', () => {
    expect(
      describeLimits({
        ...entitlement,
        enforcement: {
          requests: {
            counterKeyExpression: '@(context.Subscription?.Key)',
            calls: 60,
            renewalPeriodSeconds: 60,
            callQuota: 100000,
            callQuotaPeriod: 'Monthly',
          },
        },
      }),
    ).toEqual(['Limits traffic to 60 calls per minute.', 'Allows 100,000 calls per month.'])
  })
})

describe('callRateError', () => {
  it('refuses a half-filled call rate rather than dropping it silently', () => {
    // Both halves are needed to build the limit; dropping one would create an unrestricted
    // grant while the UI reported success.
    expect(callRateError({ calls: '100', renewalPeriodSeconds: '' })).toBeTruthy()
    expect(callRateError({ calls: '100', renewalPeriodSeconds: '0' })).toBeTruthy()
    expect(callRateError({ calls: '0', renewalPeriodSeconds: '60' })).toBeTruthy()
  })

  it('accepts an empty pair and a complete pair', () => {
    expect(callRateError({ calls: '', renewalPeriodSeconds: '60' })).toBeNull()
    expect(callRateError({ calls: '', renewalPeriodSeconds: '' })).toBeNull()
    expect(callRateError({ calls: '100', renewalPeriodSeconds: '60' })).toBeNull()
  })
})
