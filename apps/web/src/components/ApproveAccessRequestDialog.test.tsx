import { FluentProvider, textClassNames, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider, useMutation } from '@tanstack/react-query'
import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { GOVERNED_COUNTER_KEY } from '../entitlement-limits'
import { modelPublication } from '../test/model-access'
import { ApiError } from '../api'
import type { AccessRequest, AccessRequestApproval, CostCenter, EnvironmentCatalogView } from '../types'
import { ApproveAccessRequestDialog, type ApprovalRequester } from './ApproveAccessRequestDialog'

const accessRequest: AccessRequest = {
  id: 'request_1',
  tenantId: 'tenant',
  requesterObjectId: 'user-object-1',
  requesterPrincipalId: 'principal_1',
  resource: { kind: 'modelApi', id: 'modelApi_1' },
  justification: 'Support bot evaluation',
  state: 'pending',
  createdAt: '2026-09-01T12:00:00Z',
  updatedAt: '2026-09-01T12:00:00Z',
}

const catalog: EnvironmentCatalogView = {
  environments: [
    {
      key: 'development',
      displayName: 'Development',
      description: null,
      color: 'brand',
      production: false,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 10,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
    {
      key: 'production',
      displayName: 'Production',
      description: null,
      color: 'danger',
      production: true,
      aliases: [],
      acceptsEndpointsFrom: [],
      order: 50,
      builtIn: true,
      usage: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
    },
  ],
  requireClassification: false,
  unclassified: { gateways: 0, modelEndpoints: 0, mcpEndpoints: 0 },
  compatibility: [],
  updatedAt: null,
}

const supportCostCenter: CostCenter = {
  id: 'cc-support',
  tenantId: 'tenant',
  entityType: 'costCenter',
  name: 'Support',
  code: 'support',
  description: null,
  owners: [],
  members: [],
  keysAllowed: true,
  limits: [],
  builtIn: false,
  createdAt: '',
  updatedAt: '',
  isTenantDefault: false,
  memberDetails: [],
  grantCount: 0,
  enabledGrantCount: 0,
  defaultFor: 0,
}

type DialogProps = ComponentProps<typeof ApproveAccessRequestDialog>

function renderDialog(props: Partial<DialogProps> = {}) {
  const onApprove = vi.fn()
  const onCancel = vi.fn()
  render(
    <FluentProvider theme={webLightTheme}>
      <ApproveAccessRequestDialog
        accessRequest={accessRequest}
        requester={{ label: 'Ada Lovelace', objectId: 'user-object-1', registered: true }}
        resourceLabel="Chat completions (model API)"
        environmentCatalog={catalog}
        publication={modelPublication}
        governed
        existingGrant={false}
        pending={false}
        error={null}
        onCancel={onCancel}
        onApprove={onApprove}
        {...props}
      />
    </FluentProvider>,
  )
  return { onApprove, onCancel }
}

// Approves through a mutation, as the entitlements page does, so the dialog sees the approval start,
// fail, and start again.
function renderApproval(
  approveAccessRequest: (approval: AccessRequestApproval) => Promise<unknown>,
  props: Partial<DialogProps> = {},
) {
  const queryClient = new QueryClient()
  const onCancel = vi.fn()
  function Approval() {
    const approve = useMutation({ mutationFn: approveAccessRequest })
    return (
      <ApproveAccessRequestDialog
        accessRequest={accessRequest}
        requester={{ label: 'Ada Lovelace', objectId: 'user-object-1', registered: true }}
        resourceLabel="Chat completions (model API)"
        publication={modelPublication}
        governed
        existingGrant={false}
        {...props}
        pending={approve.isPending}
        error={approve.error}
        onCancel={onCancel}
        onApprove={(approval) => approve.mutate(approval)}
      />
    )
  }
  render(
    <FluentProvider theme={webLightTheme}>
      <QueryClientProvider client={queryClient}>
        <Approval />
      </QueryClientProvider>
    </FluentProvider>,
  )
  return { onCancel }
}

// The element that has focus, which has to be inside the open dialog: Fluent closes a dialog on Escape
// only when the key is pressed inside it. The dialog itself contains everything, so it doesn't count.
function focused() {
  const element = document.activeElement as HTMLElement
  const dialog = screen.getByRole('dialog')
  expect(element).not.toBe(document.body)
  expect(element).not.toBe(dialog)
  expect(dialog).toContainElement(element)
  return element
}

// Each line of the open dialog's Requester entry, in order.
function requesterLines() {
  const entry = within(screen.getByRole('dialog')).getByText('Requester').nextElementSibling as HTMLElement
  return [...entry.querySelectorAll(`.${textClassNames.root}`)].map((line) => line.textContent)
}

describe('ApproveAccessRequestDialog', () => {
  it('shows who asked for what and why, with limits prefilled from the publication', async () => {
    renderDialog()

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByRole('heading', { name: 'Approve access request' })).toBeVisible()
    expect(within(dialog).getByText('Ada Lovelace')).toBeVisible()
    expect(within(dialog).getByText('user-object-1')).toBeVisible()
    expect(within(dialog).getByText('Chat completions (model API)')).toBeVisible()
    expect(within(dialog).getByText('Support bot evaluation')).toBeVisible()
    expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(12000)
    expect(within(dialog).getByRole('spinbutton', { name: 'Token quota' })).toHaveValue(null)
    expect(within(dialog).getByText(/prefilled from the Published chat publication's token limit/)).toBeVisible()
    expect(within(dialog).getByText(
      'Approving creates grant intent only. API Management is unchanged until the Published chat model plan is reviewed and applied.',
    )).toBeVisible()
    expect(within(dialog).queryByText(/Not registered in MOSAIC/)).not.toBeInTheDocument()
    expect(within(dialog).getByRole('button', { name: 'Approve and create grant' })).toBeEnabled()
  })

  it.each<[shows: string, requester: ApprovalRequester, lines: string[]]>([
    [
      'a registered requester by label, then object ID',
      { label: 'Megan Bowen', objectId: 'user-object-1', registered: true },
      ['Megan Bowen', 'user-object-1'],
    ],
    [
      'the object ID once when the label is that ID in another letter case',
      { label: 'USER-OBJECT-1', objectId: 'user-object-1', registered: true },
      ['user-object-1'],
    ],
    [
      'an unregistered requester by object ID once, and that approving registers them',
      { label: 'user-object-1', objectId: 'user-object-1', registered: false },
      ['user-object-1', 'Not registered in MOSAIC yet. Approving registers them as a user principal.'],
    ],
  ])('shows %s', async (_, requester, lines) => {
    renderDialog({ requester })

    await screen.findByRole('dialog')
    expect(requesterLines()).toEqual(lines)
  })

  it('shows the requested cost center', async () => {
    renderDialog({
      accessRequest: {
        ...accessRequest,
        costCenterId: supportCostCenter.id,
        costCenter: { id: supportCostCenter.id, name: supportCostCenter.name, code: supportCostCenter.code },
      },
      costCenters: [supportCostCenter],
    })

    expect((await screen.findAllByText('Support (support)')).length).toBeGreaterThan(0)
  })

  it('sends the selected cost center in the approval', async () => {
    const user = userEvent.setup()
    const researchCostCenter = { ...supportCostCenter, id: 'cc-research', name: 'Research', code: 'research' }
    const { onApprove } = renderDialog({ costCenters: [supportCostCenter, researchCostCenter] })

    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Cost center' }), researchCostCenter.id)
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledWith(expect.objectContaining({ costCenterId: researchCostCenter.id }))
  })

  it('charges the requested cost center, marked as requested, unless another is chosen', async () => {
    const user = userEvent.setup()
    const researchCostCenter = { ...supportCostCenter, id: 'cc-research', name: 'Research', code: 'research' }
    const { onApprove } = renderDialog({
      accessRequest: {
        ...accessRequest,
        costCenterId: researchCostCenter.id,
        costCenter: { id: researchCostCenter.id, name: researchCostCenter.name, code: researchCostCenter.code },
      },
      costCenters: [supportCostCenter, researchCostCenter],
    })

    const dialog = await screen.findByRole('dialog')
    const select = within(dialog).getByRole('combobox', { name: 'Cost center' })
    expect(select).toHaveValue(researchCostCenter.id)
    expect(within(select).getAllByRole('option').map((option) => option.textContent)).toEqual([
      'Support (support)',
      'Research (research) — requested',
    ])
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledWith(expect.objectContaining({ costCenterId: researchCostCenter.id }))
  })

  it("offers the requester's default only when the request names no cost center", async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog({ costCenters: [supportCostCenter] })

    const dialog = await screen.findByRole('dialog')
    const select = within(dialog).getByRole('combobox', { name: 'Cost center' })
    expect(select).toHaveValue('')
    expect(within(select).getByRole('option', { name: "Requester's default cost center" })).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove.mock.calls[0][0]).not.toHaveProperty('costCenterId')
  })

  it("leaves limits empty for the cost center's per-person defaults, and follows the choice until edited", async () => {
    const user = userEvent.setup()
    const withDefaults: CostCenter = {
      ...supportCostCenter,
      limits: [
        {
          resource: { kind: 'modelApi', id: 'modelApi_1' },
          person: { tokensPerMinute: 5000, tokenQuota: null, tokenQuotaPeriod: null, callsPerMinute: null, callQuota: null, callQuotaPeriod: null },
          pool: null,
        },
      ],
    }
    const researchCostCenter = { ...supportCostCenter, id: 'cc-research', name: 'Research', code: 'research' }
    const { onApprove } = renderDialog({
      accessRequest: {
        ...accessRequest,
        costCenterId: withDefaults.id,
        costCenter: { id: withDefaults.id, name: withDefaults.name, code: withDefaults.code },
      },
      costCenters: [withDefaults, researchCostCenter],
    })

    const dialog = await screen.findByRole('dialog')
    const tokensPerMinute = within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })
    expect(tokensPerMinute).toHaveValue(null)
    expect(within(dialog).getByText(/Support sets per-person defaults here: 5,000 tokens per minute\./)).toBeVisible()

    // Research sets none, so the publication's limit prefills, until the administrator edits it.
    const select = within(dialog).getByRole('combobox', { name: 'Cost center' })
    await user.selectOptions(select, researchCostCenter.id)
    expect(tokensPerMinute).toHaveValue(12000)
    await user.clear(tokensPerMinute)
    await user.type(tokensPerMinute, '7000')
    await user.selectOptions(select, withDefaults.id)
    expect(tokensPerMinute).toHaveValue(7000)

    await user.clear(tokensPerMinute)
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))
    expect(onApprove).toHaveBeenCalledWith(expect.objectContaining({ costCenterId: withDefaults.id, enforcement: null }))
  })

  it('sends the confirmed limits on the governed counter, with the decision note', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog()

    const dialog = await screen.findByRole('dialog')
    const tokensPerMinute = within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })
    await user.clear(tokensPerMinute)
    await user.type(tokensPerMinute, '5000')
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Token quota' }), '100000')
    await user.selectOptions(within(dialog).getByRole('combobox', { name: 'Quota period' }), 'Daily')
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Calls' }), '30')
    await user.type(within(dialog).getByRole('textbox', { name: 'Decision note' }), '  Pilot team  ')
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledExactlyOnceWith({
      note: 'Pilot team',
      enforcement: {
        tokens: {
          counterKeyExpression: '@(context.Subscription.Id)',
          estimatePromptTokens: true,
          tokensPerMinute: 5000,
          tokenQuota: 100000,
          tokenQuotaPeriod: 'Daily',
        },
        requests: {
          counterKeyExpression: '@(context.Subscription.Id)',
          calls: 30,
          renewalPeriodSeconds: 60,
        },
      },
    })
  })

  it('approves with no grant-specific limit when nothing prefills, and says who it registers', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog({
      accessRequest: { ...accessRequest, requesterPrincipalId: null, justification: '  ' },
      requester: { label: 'new-object-1', objectId: 'new-object-1', registered: false },
      resourceLabel: 'Docs search (MCP server)',
      publication: undefined,
      governed: false,
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getAllByText('new-object-1')).toHaveLength(1)
    expect(within(dialog).getByText(
      'Not registered in MOSAIC yet. Approving registers them as a user principal.',
    )).toBeVisible()
    expect(within(dialog).getByText('No justification given')).toBeVisible()
    expect(within(dialog).getByText(/This resource has no default limits to prefill\./)).toBeVisible()
    expect(within(dialog).getByText(/MOSAIC does not apply grants for this resource to API Management/)).toBeVisible()
    expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(null)
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledExactlyOnceWith({ note: null, enforcement: null })
  })

  it('approves with no grant-specific limit once the prefilled limit is cleared', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog()

    const dialog = await screen.findByRole('dialog')
    await user.clear(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }))
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledExactlyOnceWith({ note: null, enforcement: null })
  })

  it('requires confirmation when the resource moved and sends the confirmed environment', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog({
      accessRequest: {
        ...accessRequest,
        requestedEnvironment: 'development',
        resourceSummary: {
          kind: 'modelApi',
          id: 'modelApi_1',
          scopeId: null,
          displayName: 'Chat completions',
          gatewayId: 'gateway_1',
          gatewayName: 'Gateway',
          environment: 'production',
          available: true,
        },
      },
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/moved from Development to Production/)).toBeVisible()
    expect(within(dialog).getByText('This approval grants production-class access.')).toBeVisible()
    const approve = within(dialog).getByRole('button', { name: 'Approve and create grant' })
    expect(approve).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Approve access to Production' }))
    await user.clear(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }))
    await user.click(approve)

    expect(onApprove).toHaveBeenCalledExactlyOnceWith({
      note: null,
      enforcement: null,
      confirmedEnvironment: 'production',
    })
  })

  it('uses the unclassified sentinel when current environment is unclassified', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog({
      accessRequest: {
        ...accessRequest,
        requestedEnvironment: 'development',
        resourceSummary: {
          kind: 'modelApi',
          id: 'modelApi_1',
          scopeId: null,
          displayName: 'Chat completions',
          gatewayId: 'gateway_1',
          gatewayName: 'Gateway',
          environment: null,
          available: true,
        },
      },
      publication: undefined,
    })

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('checkbox', { name: 'Approve access to Unclassified' }))
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledWith({
      note: null,
      enforcement: null,
      confirmedEnvironment: 'unclassified',
    })
  })

  it('updates the current environment after an environmentChanged conflict', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog({
      accessRequest: {
        ...accessRequest,
        requestedEnvironment: 'development',
        resourceSummary: {
          kind: 'modelApi',
          id: 'modelApi_1',
          scopeId: null,
          displayName: 'Chat completions',
          gatewayId: 'gateway_1',
          gatewayName: 'Gateway',
          environment: 'development',
          available: true,
        },
      },
      publication: undefined,
      error: new ApiError('Environment changed', 409, {
        details: {
          reason: 'environmentChanged',
          requestedEnvironment: 'development',
          currentEnvironment: 'production',
        },
      }),
    })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/moved from Development to Production/)).toBeVisible()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Approve access to Production' }))
    await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

    expect(onApprove).toHaveBeenCalledWith({
      note: null,
      enforcement: null,
      confirmedEnvironment: 'production',
    })
  })

  it('refuses a half-filled call rate instead of dropping it', async () => {
    const user = userEvent.setup()
    const { onApprove } = renderDialog()

    const dialog = await screen.findByRole('dialog')
    await user.type(within(dialog).getByRole('spinbutton', { name: 'Calls' }), '30')
    await user.clear(within(dialog).getByRole('spinbutton', { name: 'Per how many seconds' }))

    expect(within(dialog).getByText(
      'Enter how many seconds the call limit is measured over, or clear the call count.',
    )).toBeVisible()
    const approve = within(dialog).getByRole('button', { name: 'Approve and create grant' })
    expect(approve).toBeDisabled()
    await user.click(approve)
    expect(onApprove).not.toHaveBeenCalled()
  })

  it('blocks approval when the requester already holds a grant for the resource', async () => {
    renderDialog({ existingGrant: true })

    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(
      'Ada Lovelace already has a direct grant for Chat completions (model API). Deny this request, or change the existing grant instead.',
    )).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Approve and create grant' })).toBeDisabled()
  })

  it('shows a failed approval inside the dialog', async () => {
    renderDialog({
      error: new Error('An apply or mutation is already running for this publication.'),
    })

    const dialog = await screen.findByRole('dialog')
    expect(
      within(dialog).getByText('An apply or mutation is already running for this publication.'),
    ).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Approve and create grant' })).toBeEnabled()
  })

  it('cannot be submitted or dismissed while the approval is in flight', async () => {
    const user = userEvent.setup()
    const { onApprove, onCancel } = renderDialog({ pending: true })

    const dialog = await screen.findByRole('dialog')
    const approve = within(dialog).getByRole('button', { name: 'Approve and create grant' })
    expect(approve).toHaveAttribute('aria-disabled', 'true')
    expect(approve).not.toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.click(approve)
    await user.keyboard('{Escape}')
    expect(onCancel).not.toHaveBeenCalled()
    expect(onApprove).not.toHaveBeenCalled()
  })

  it('keeps focus on Approve while it works, without letting it submit again', async () => {
    const user = userEvent.setup()
    let refuse: (error: Error) => void = () => {}
    const approveAccessRequest = vi.fn(() => new Promise((_, reject) => { refuse = reject }))
    const { onCancel } = renderApproval(approveAccessRequest)
    const approve = within(await screen.findByRole('dialog')).getByRole('button', { name: 'Approve and create grant' })

    await user.click(approve)

    // A browser takes focus off a button that becomes disabled, so a busy button stays focusable instead.
    await waitFor(() => expect(approve).toHaveAttribute('aria-disabled', 'true'))
    expect(approve).toHaveFocus()
    expect(approve).not.toBeDisabled()
    expect(within(screen.getByRole('dialog')).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    // A focusable submit button still submits its form, so the dialog refuses a second submission itself.
    await user.click(approve)
    await user.keyboard('{Enter}')
    expect(approveAccessRequest).toHaveBeenCalledTimes(1)
    // Approving can't be dismissed, so its outcome is always shown.
    await user.keyboard('{Escape}')
    expect(onCancel).not.toHaveBeenCalled()

    await act(async () => {
      refuse(new Error('An apply or mutation is already running for this publication.'))
      await new Promise((resolve) => setTimeout(resolve, 50))
    })

    await waitFor(() => expect(focused()).toHaveTextContent('An apply or mutation is already running for this publication.'))
    expect(approve).not.toHaveAttribute('aria-disabled')
    await user.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalledOnce()
  })

  it('refuses a second submission from a field, then moves focus from the field to the reason it failed', async () => {
    const user = userEvent.setup()
    let refuse: (error: Error) => void = () => {}
    const approveAccessRequest = vi.fn(() => new Promise((_, reject) => { refuse = reject }))
    renderApproval(approveAccessRequest)
    const note = within(await screen.findByRole('dialog')).getByRole('textbox', { name: 'Decision note' })

    await user.type(note, 'Pilot team{Enter}')
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Approve and create grant' })).toHaveAttribute('aria-disabled', 'true')
    })
    await user.keyboard('{Enter}')
    expect(approveAccessRequest).toHaveBeenCalledTimes(1)
    expect(note).toHaveFocus()

    await act(async () => {
      refuse(new Error('An apply or mutation is already running for this publication.'))
      await new Promise((resolve) => setTimeout(resolve, 50))
    })

    // The message bar doesn't announce itself, so the reason takes focus even from a field, to be read.
    await waitFor(() => expect(focused()).toHaveTextContent('An apply or mutation is already running for this publication.'))
    expect(note).toHaveValue('Pilot team')
  })

  it('keeps focus on Approve while it tries again after failing, then moves it to the new reason', async () => {
    const user = userEvent.setup()
    let refuse: (error: Error) => void = () => {}
    const approveAccessRequest = vi.fn()
      .mockRejectedValueOnce(new Error('An apply or mutation is already running for this publication.'))
      .mockReturnValueOnce(new Promise((_, reject) => { refuse = reject }))
    renderApproval(approveAccessRequest)
    const approve = within(await screen.findByRole('dialog')).getByRole('button', { name: 'Approve and create grant' })

    await user.click(approve)
    await waitFor(() => expect(focused()).toHaveTextContent('An apply or mutation is already running for this publication.'))
    await user.click(approve)

    await waitFor(() => expect(approve).toHaveAttribute('aria-disabled', 'true'))
    expect(approve).toHaveFocus()
    expect(screen.queryByText('An apply or mutation is already running for this publication.')).not.toBeInTheDocument()

    await act(async () => {
      refuse(new Error('The requester is no longer in the directory.'))
      await new Promise((resolve) => setTimeout(resolve, 50))
    })

    await waitFor(() => expect(focused()).toHaveTextContent('The requester is no longer in the directory.'))
    expect(approveAccessRequest).toHaveBeenCalledTimes(2)
  })

  it('moves focus to a refusal for a moved resource, then approves only once the move is confirmed', async () => {
    const user = userEvent.setup()
    const approveAccessRequest = vi.fn()
      .mockRejectedValueOnce(new ApiError('The resource moved to Production after this request was made.', 409, {
        details: {
          reason: 'environmentChanged',
          requestedEnvironment: 'development',
          currentEnvironment: 'production',
        },
      }))
      .mockResolvedValueOnce({})
    renderApproval(approveAccessRequest, {
      accessRequest: {
        ...accessRequest,
        requestedEnvironment: 'development',
        resourceSummary: {
          kind: 'modelApi',
          id: 'modelApi_1',
          scopeId: null,
          displayName: 'Chat completions',
          gatewayId: 'gateway_1',
          gatewayName: 'Gateway',
          environment: 'development',
          available: true,
        },
      },
      environmentCatalog: catalog,
    })
    const dialog = await screen.findByRole('dialog')
    const approve = within(dialog).getByRole('button', { name: 'Approve and create grant' })

    await user.click(approve)

    await waitFor(() => expect(focused()).toHaveTextContent('The resource moved to Production after this request was made.'))
    expect(within(dialog).getByText(/moved from Development to Production/)).toBeVisible()
    expect(approve).toBeDisabled()
    await user.click(within(dialog).getByRole('checkbox', { name: 'Approve access to Production' }))
    await user.click(approve)

    await waitFor(() => expect(approveAccessRequest).toHaveBeenCalledTimes(2))
    expect(approveAccessRequest.mock.calls[0][0]).not.toHaveProperty('confirmedEnvironment')
    expect(approveAccessRequest.mock.calls[1][0]).toMatchObject({ confirmedEnvironment: 'production' })
  })

  it('leaves focus where Fluent puts it when the dialog opens showing a failure', async () => {
    renderDialog({
      error: new Error('An apply or mutation is already running for this publication.'),
    })

    const dialog = await screen.findByRole('dialog')
    // Fluent focuses the first control Tab reaches.
    await waitFor(() => expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveFocus())
    expect(focused()).not.toHaveTextContent('An apply or mutation is already running for this publication.')
  })

  it('cancels without approving', async () => {
    const user = userEvent.setup()
    const { onApprove, onCancel } = renderDialog()

    const dialog = await screen.findByRole('dialog')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(onCancel).toHaveBeenCalledOnce()
    await user.keyboard('{Escape}')
    expect(onCancel).toHaveBeenCalledTimes(2)
    expect(onApprove).not.toHaveBeenCalled()
  })

  describe('pool models', () => {
    const poolRequest: AccessRequest = {
      ...accessRequest,
      resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: 'modelpool_anthropic' },
    }
    const poolProps: Partial<DialogProps> = {
      accessRequest: poolRequest,
      resourceLabel: 'Claude Opus 4.5 in Anthropic Claude (pool model)',
      publication: undefined,
      pool: { displayName: 'Anthropic Claude', governedAccess: { keysEnabled: true, entraEnabled: true } },
      governed: true,
    }

    it("says the pool's plan applies the grant, and limits it on the governed counter", async () => {
      const user = userEvent.setup()
      const { onApprove } = renderDialog(poolProps)

      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText(
        'Approving creates grant intent only. API Management is unchanged until the Anthropic Claude plan is reviewed and applied.',
      )).toBeVisible()
      expect(within(dialog).getByText(/The pool's shared token limit still applies; this does not mean unrestricted gateway access\./)).toBeVisible()
      expect(within(dialog).queryByText(/publication safeguards/)).not.toBeInTheDocument()
      await user.type(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' }), '20000')
      await user.click(within(dialog).getByRole('button', { name: 'Approve and create grant' }))

      expect(onApprove).toHaveBeenCalledExactlyOnceWith({
        note: null,
        enforcement: {
          tokens: { counterKeyExpression: GOVERNED_COUNTER_KEY, estimatePromptTokens: true, tokensPerMinute: 20000 },
        },
      })
    })

    it("says the grant waits while the pool doesn't govern access yet", async () => {
      renderDialog({ ...poolProps, pool: { displayName: 'Anthropic Claude', governedAccess: null } })

      expect(within(await screen.findByRole('dialog')).getByText(
        "Approving creates grant intent only. Anthropic Claude doesn't use governed access yet, so the grant waits until it does and the pool's plan is applied.",
      )).toBeVisible()
    })

    it("takes the cost center's defaults for this pool's model, not for the same model in another pool", async () => {
      const person = (tokensPerMinute: number) => ({
        tokensPerMinute, tokenQuota: null, tokenQuotaPeriod: null, callsPerMinute: null, callQuota: null, callQuotaPeriod: null,
      })
      const withDefaults: CostCenter = {
        ...supportCostCenter,
        limits: [
          { resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: 'modelpool_openai' }, person: person(9000), pool: null },
          { resource: { kind: 'poolModel', id: 'poolmodel_opus', scopeId: 'modelpool_anthropic' }, person: person(5000), pool: null },
        ],
      }
      renderDialog({
        ...poolProps,
        accessRequest: {
          ...poolRequest,
          costCenterId: withDefaults.id,
          costCenter: { id: withDefaults.id, name: withDefaults.name, code: withDefaults.code },
        },
        costCenters: [withDefaults],
      })

      const dialog = await screen.findByRole('dialog')
      expect(within(dialog).getByText(/Support sets per-person defaults here: 5,000 tokens per minute\./)).toBeVisible()
      expect(within(dialog).getByText(/The pool's shared token limit still applies\./)).toBeVisible()
      expect(within(dialog).getByRole('spinbutton', { name: 'Tokens per minute' })).toHaveValue(null)
    })
  })
})
