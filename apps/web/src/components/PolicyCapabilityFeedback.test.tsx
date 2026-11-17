import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../api'
import { policyReviewFields } from '../policy-capabilities'
import { MosaicThemeProvider } from '../theme'
import type { PolicyCapabilityIssue } from '../types'
import { PolicyCapabilityError, PolicyCapabilityFeedback } from './PolicyCapabilityFeedback'
import { WorkspaceEditorFrame } from './WorkspaceEditorFrame'

function refusal(support: 'unknown' | 'unavailable' = 'unknown') {
  const issue: PolicyCapabilityIssue = {
    field: 'enforcement.requests.calls', policy: 'rate-limit-by-key',
    resource: { kind: 'poolModel', id: 'chat', scopeId: 'pool' },
    gatewayId: 'gateway/one', skuName: support === 'unavailable' ? 'Consumption' : null,
    tier: support === 'unavailable' ? 'consumption' : 'unknown', apiShape: null,
    support, reason: support === 'unavailable' ? 'unsupportedTier' : 'unknownTier',
    message: support === 'unavailable'
      ? 'Consumption cannot enforce this call rate.' : 'Check access on the gateway before saving this rate.',
    nextAction: support === 'unavailable' ? 'chooseSupportedGateway' : 'checkGatewayAccess',
    documentationUrl: 'https://learn.microsoft.com/en-us/azure/api-management/rate-limit-by-key-policy',
  }
  return new ApiError('Nothing was saved.', 422, {
    code: 'validation_error', message: 'Nothing was saved.',
    details: { reason: 'gatewayPolicyLimitsRejected', boundary: 'save', issues: [issue] },
  })
}

describe('scoped capability feedback', () => {
  afterEach(() => vi.restoreAllMocks())

  it.each([
    ['unknown', 'Support not verified'],
    ['unavailable', 'Not supported'],
  ] as const)('keeps %s distinct with a named field, real recovery, and unchanged-state detail', async (support, label) => {
    render(<MosaicThemeProvider><PolicyCapabilityError title="Unable to save grant"
      error={refusal(support)} announcementKey={1} /></MosaicThemeProvider>)
    expect(await screen.findByText(label, { exact: false })).toBeInTheDocument()
    expect(screen.getByText('Calls')).toBeInTheDocument()
    expect(screen.getByText(/Previously applied gateway access is unchanged/)).toBeInTheDocument()
    const gateway = screen.getByRole('link', { name: 'View gateway in a new tab' })
    expect(gateway).toHaveAttribute('href', '/gateways/gateway%2Fone')
    expect(gateway).toHaveAttribute('target', '_blank')
    expect(screen.getByRole('link', { name: 'Policy reference' })).toHaveAttribute('href',
      'https://learn.microsoft.com/en-us/azure/api-management/rate-limit-by-key-policy')
  })

  it('uses the real frame summary and lets field recovery focus the retained input', async () => {
    vi.spyOn(HTMLElement.prototype, 'scrollIntoView').mockImplementation(() => {})
    const error = refusal()
    const back = vi.fn()
    const { container, rerender } = render(<MosaicThemeProvider>
      <WorkspaceEditorFrame title="New grant" onBack={back}>
        <label htmlFor="calls">Calls</label><input id="calls" defaultValue="20" />
      </WorkspaceEditorFrame>
    </MosaicThemeProvider>)
    await screen.findByLabelText('Calls')
    const actions = policyReviewFields(container, error)
    rerender(<MosaicThemeProvider>
      <WorkspaceEditorFrame title="New grant" onBack={back} errorKey={1}
        error={<PolicyCapabilityFeedback title="Unable to save grant" error={error} reviewFields={actions} />}>
        <label htmlFor="calls">Calls</label><input id="calls" defaultValue="20" />
      </WorkspaceEditorFrame>
    </MosaicThemeProvider>)
    expect((await screen.findByText('Unable to save grant')).closest('div[tabindex="-1"]')).toHaveFocus()
    fireEvent.click(screen.getByRole('button', { name: 'Review calls' }))
    expect(screen.getByLabelText('Calls')).toHaveFocus()
    expect(screen.getByLabelText('Calls')).toHaveValue('20')
    expect(back).not.toHaveBeenCalled()
  })

  it('does not replace an upstream error with a fabricated capability outcome', async () => {
    render(<MosaicThemeProvider><PolicyCapabilityError title="Unable to save limits"
      error={new Error('The gateway read was forbidden.')} /></MosaicThemeProvider>)
    expect(await screen.findByText('The gateway read was forbidden.')).toBeInTheDocument()
    expect(screen.queryByText('Not supported')).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Policy reference' })).not.toBeInTheDocument()
  })
})
