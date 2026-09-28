import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { AccessRequest, Entitlement } from '../types'
import { callRateError, describeLimits, describePublicationLimits } from '../entitlement-limits'
import { EntitlementsPage } from './EntitlementsPage'
import { includeAriaHiddenInRoleQueries } from '../test/dialogs'
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
  approveAccessRequest: vi.fn(),
  denyAccessRequest: vi.fn(),
}

const pendingRequest: AccessRequest = {
  id: 'request_1',
  tenantId: 'tenant',
  // Entra object IDs are matched to principals without regard to letter case.
  requesterObjectId: 'USER-OBJECT-1',
  requesterPrincipalId: null,
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  justification: 'Support bot evaluation',
  state: 'pending',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

async function openApproval(user: ReturnType<typeof userEvent.setup>) {
  const requests = await screen.findByRole('table', { name: 'Pending access requests' })
  const approve = within(requests).getByRole('button', { name: 'Approve' })
  await waitFor(() => expect(approve).toBeEnabled())
  await user.click(approve)
  return screen.findByRole('dialog')
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
    // Tabster can mark the open dialog aria-hidden in happy-dom; see includeAriaHiddenInRoleQueries.
    const dialog = await screen.findByRole('dialog', { hidden: true })
    expect(within(dialog).queryByRole('option', { name: 'Engineering (group)', hidden: true })).not.toBeInTheDocument()
    expect(within(dialog).getByRole('combobox', { name: 'Resource', hidden: true })).toBeDisabled()
    fireEvent.change(within(dialog).getByRole('combobox', { name: 'Subject', hidden: true }), { target: { value: 'principal_1' } })
    await user.click(within(dialog).getByRole('button', { name: 'Grant access', hidden: true }))
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

  describe('access requests', () => {
    includeAriaHiddenInRoleQueries()

    it('approves through the limits dialog, creating linked grant intent that still needs review and apply', async () => {
      const user = userEvent.setup()
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      api.approveAccessRequest.mockImplementation(async () => {
        api.listAccessRequests.mockResolvedValue([])
        return {
          ...pendingRequest, state: 'approved', requesterPrincipalId: 'principal_1',
          grantedEntitlementId: 'granted_1',
        }
      })
      renderPage()

      const dialog = await openApproval(user)
      expect(api.approveAccessRequest).not.toHaveBeenCalled()
      expect(within(dialog).getByRole('heading', { name: 'Approve access request' })).toBeVisible()
      expect(within(dialog).getByText('Ada Lovelace')).toBeVisible()
      expect(within(dialog).getByText('Chat completions (model API)')).toBeVisible()
      expect(within(dialog).getByText('Support bot evaluation')).toBeVisible()
      expect(within(dialog).queryByText(/Not registered in MOSAIC/)).not.toBeInTheDocument()
      expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(12000)
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      await waitFor(() => expect(api.approveAccessRequest).toHaveBeenCalledWith(pendingRequest.id, {
        note: null,
        enforcement: {
          tokens: {
            counterKeyExpression: '@(context.Subscription.Id)',
            estimatePromptTokens: true,
            tokensPerMinute: 12000,
          },
        },
      }))
      expect(await screen.findByText(
        'Approved the request and created grant intent for Ada Lovelace. API Management is unchanged; review and apply the Published chat model plan to activate it.',
      )).toBeVisible()
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
      expect(await screen.findByText('No pending requests')).toBeVisible()
      expect(api.createEntitlement).not.toHaveBeenCalled()

      await user.click(screen.getByRole('link', { name: 'Go to review and apply for Published chat' }))
      const publishedModel = screen.getByRole('combobox', { name: 'Published model' })
      expect(publishedModel).toHaveValue(modelPublication.id)
      expect(publishedModel).toHaveFocus()
      expect(await screen.findByRole('button', { name: 'Review model changes' })).toBeVisible()
      expect(api.createPublishPlan).not.toHaveBeenCalled()
      expect(api.applyPublishPlan).not.toHaveBeenCalled()
    })

    it('says approval registers a requester MOSAIC does not know yet', async () => {
      const user = userEvent.setup()
      api.listAccessRequests.mockResolvedValue([
        { ...pendingRequest, requesterObjectId: 'new-object-1', justification: null },
      ])
      api.approveAccessRequest.mockResolvedValue({
        ...pendingRequest, requesterObjectId: 'new-object-1', state: 'approved',
        requesterPrincipalId: 'principal_new', grantedEntitlementId: 'granted_1',
      })
      renderPage()

      const dialog = await openApproval(user)
      expect(within(dialog).getByText(
        'Not registered in MOSAIC yet. Approving registers them as a user principal.',
      )).toBeVisible()
      expect(within(dialog).getByText('No justification given')).toBeVisible()
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      // The model API is imported-only here, so nothing prefills and the grant stays desired state.
      await waitFor(() => expect(api.approveAccessRequest).toHaveBeenCalledWith(pendingRequest.id, {
        note: null,
        enforcement: null,
      }))
      expect(await screen.findByText(
        'Approved the request, registered new-object-1 as a user principal, and created their grant intent. The grant is desired state only; MOSAIC does not apply grants for this resource to API Management.',
      )).toBeVisible()
      expect(screen.queryByRole('link', { name: /Go to review and apply/ })).not.toBeInTheDocument()
    })

    it('keeps a failed approval open in the dialog with the reason', async () => {
      const user = userEvent.setup()
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      api.approveAccessRequest.mockRejectedValue(
        new Error('An apply or mutation is already running for this publication.'),
      )
      renderPage()

      const dialog = await openApproval(user)
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      expect(await within(dialog).findByText(
        'An apply or mutation is already running for this publication.',
      )).toBeVisible()
      expect(screen.getByRole('dialog')).toBeVisible()
      expect(screen.queryByText(/Approved the request/)).not.toBeInTheDocument()
    })

    it('warns instead of approving when the requester already holds a direct grant', async () => {
      const user = userEvent.setup()
      api.listEntitlements.mockResolvedValue([directGrant])
      api.listPublications.mockResolvedValue([modelPublication])
      api.listModelApis.mockResolvedValue([publishedModelApi])
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      renderPage()

      const dialog = await openApproval(user)
      expect(within(dialog).getByText(
        'Ada Lovelace already has a direct grant for Chat completions (model API). Deny this request, or change the existing grant instead.',
      )).toBeVisible()
      expect(within(dialog).getByRole('button', { name: 'Approve and create grant' })).toBeDisabled()
      expect(api.approveAccessRequest).not.toHaveBeenCalled()
    })

    it('denies without a dialog, and says no grant was created', async () => {
      const user = userEvent.setup()
      api.listAccessRequests.mockResolvedValue([pendingRequest])
      api.denyAccessRequest.mockImplementation(async () => {
        api.listAccessRequests.mockResolvedValue([])
        return { ...pendingRequest, state: 'denied' }
      })
      renderPage()

      const requests = await screen.findByRole('table', { name: 'Pending access requests' })
      await user.click(within(requests).getByRole('button', { name: 'Deny' }))

      await waitFor(() => expect(api.denyAccessRequest).toHaveBeenCalledWith(pendingRequest.id))
      expect(await screen.findByText(
        'Denied the access request. No grant was created, and API Management is unchanged.',
      )).toBeVisible()
      expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
      expect(api.approveAccessRequest).not.toHaveBeenCalled()
    })
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

  it('says a publication without token enforcement has no token limits, and stays silent when the publication is unknown', () => {
    expect(describeLimits({ enforcement: null }, null)).toEqual([
      'No grant-specific limit is configured.',
      "Publication: Token limits are unavailable for this model on this gateway's tier.",
    ])
    expect(describeLimits({ enforcement: null }, undefined)).toEqual([
      'No grant-specific limit is configured.',
    ])
    expect(describePublicationLimits(null)).toEqual([
      "Token limits are unavailable for this model on this gateway's tier.",
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
