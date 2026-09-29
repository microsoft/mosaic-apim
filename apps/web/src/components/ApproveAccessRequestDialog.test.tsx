import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentProps } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { modelPublication } from '../test/model-access'
import { ApiError } from '../api'
import type { AccessRequest, EnvironmentCatalogView } from '../types'
import { ApproveAccessRequestDialog } from './ApproveAccessRequestDialog'

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
    expect(within(dialog).getByRole('button', { name: 'Approve and create grant' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Cancel' })).toBeDisabled()
    await user.keyboard('{Escape}')
    expect(onCancel).not.toHaveBeenCalled()
    expect(onApprove).not.toHaveBeenCalled()
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
})
