import { FluentProvider, webLightTheme } from '@fluentui/react-components'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { NOTIFICATION_CATEGORIES, type ApprovalPreview, type NotificationPreferences as Preferences } from '../admin-types'
import { BulkApprovalWorkspace, RecommendationEvidence } from './BulkApprovalWorkspace'
import { NotificationPreferences } from './NotificationPreferences'
import { limitInputError, notificationPreferencesInput, notificationTarget, unresolvedRequestIds, validTimeZone } from '../admin-governance'
import { emptyLimitForm } from '../entitlement-limits'
import { AiReadiness } from './ApprovalAiSettings'
import { CapabilityEvidence } from './NotificationSettings'
import { NotificationBell, NotificationInboxPage } from './NotificationInbox'
import { RequestNotifications } from './RequestNotifications'
import { BulkRequestSelection } from './BulkRequestSelection'
import { GatewayNotificationRun } from './GatewayNotificationRun'

const api = vi.hoisted(() => ({
  previewBulkApprovals: vi.fn(), approveBulkRequests: vi.fn(), recommendApprovals: vi.fn(),
  getApprovalRecommendations: vi.fn(),
  getNotificationPreferences: vi.fn(), saveNotificationPreferences: vi.fn(), previewNotificationPreferences: vi.fn(),
  listNotificationInbox: vi.fn(), listAccessRequests: vi.fn(), listPrincipals: vi.fn(),
  getSyncRun: vi.fn(),
  getNotificationInboxItem: vi.fn(), setNotificationRead: vi.fn(),
}))
vi.mock('../api', () => ({ useMosaicApi: () => api }))
const first: ApprovalPreview = {
  request: { id: 'r1', tenantId: 'fictional-tenant', requesterObjectId: 'fictional-requester', state: 'pending', resource: { kind: 'modelApi', id: 'model' }, requestedEnvironment: 'dev', createdAt: '2026-01-01T00:00:00Z', updatedAt: '2026-01-01T00:00:00Z' },
  resourceSummary: { kind: 'modelApi', id: 'model', displayName: 'Fictional model', gatewayId: null, gatewayName: null, environment: 'dev', available: true },
  revision: { contextRevision: 'context-1' }, currentEnvironment: 'dev', production: false,
  limitsSource: 'costCenter', effectiveEnforcement: { requests: { calls: 20, renewalPeriodSeconds: 60, counterKeyExpression: '@(context.Subscription.Id)' } }, mutationLocked: false, blockers: [], requiresPlanApply: true,
}
const second: ApprovalPreview = { ...first, request: { ...first.request, id: 'r2' }, resourceSummary: { ...first.resourceSummary!, displayName: 'Another fictional model' }, revision: { contextRevision: 'context-2' } }
const preferences: Preferences = {
  id: 'preferences-record', revision: 2, persona: 'admin', userObjectId: 'self-only', timeZone: 'UTC',
  categories: NOTIFICATION_CATEGORIES.map(({ value: category }) => ({ category, inApp: true, email: { mode: 'off', digestPeriods: [] }, teams: { mode: 'off', digestPeriods: [] } })),
  digestSchedules: [], quietHours: null, thresholds: [80, 100], repeatCooldownSeconds: 86400,
}
function mount(children: ReactNode, initialEntry = '/') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: 0 } } })
  render(<QueryClientProvider client={client}><FluentProvider theme={webLightTheme}><MemoryRouter initialEntries={[initialEntry]}>{children}</MemoryRouter></FluentProvider></QueryClientProvider>)
  return client
}
beforeEach(() => {
  vi.resetAllMocks()
  api.previewBulkApprovals.mockResolvedValue({ items: [first, second], unavailable: [] })
  api.getApprovalRecommendations.mockResolvedValue([])
  api.getNotificationPreferences.mockResolvedValue(structuredClone(preferences))
  api.listNotificationInbox.mockResolvedValue({ items: [], unreadCount: 7, continuationToken: null })
  api.listAccessRequests.mockResolvedValue([first.request, second.request])
  api.listPrincipals.mockResolvedValue([])
})
describe('bulk approval workspace', () => {
  it('retains partial successes and reviews only unresolved requests with fresh context', async () => {
    api.approveBulkRequests.mockResolvedValue({ batchId: 'batch', idempotencyKey: 'key', items: [
      { requestId: 'r1', status: 'approved', entitlementId: 'grant', requiresPlanApply: true },
      { requestId: 'r2', status: 'conflict', message: 'Request changed.', nextAction: 'Review current context.', requiresPlanApply: false },
    ] })
    const complete = vi.fn()
    mount(<BulkApprovalWorkspace requestIds={['r1', 'r2']} principals={[]} costCenters={[]} onBack={vi.fn()} onComplete={complete} />)
    expect(await screen.findAllByText(/Inherited cost-center per-person limits/)).toHaveLength(2)
    fireEvent.click(await screen.findByRole('button', { name: 'Approve 2 selected' }))
    expect(await screen.findByText(/1 approved · 0 already approved · 1 unresolved/)).toBeVisible()
    expect(screen.getByRole('link', { name: 'Review saved grant' })).toHaveAttribute('href', '/entitlements?tab=grants&selected=grant')
    expect(screen.getByText(/Request changed. Review current context./)).toBeVisible()
    const payload = api.approveBulkRequests.mock.calls[0][0]
    expect(payload.items).toEqual([
      { requestId: 'r1', expectedContext: first.revision, approval: { note: null } },
      { requestId: 'r2', expectedContext: second.revision, approval: { note: null } },
    ])
    expect(complete).toHaveBeenCalledOnce()
    api.previewBulkApprovals.mockResolvedValue({ items: [second], unavailable: [] })
    fireEvent.click(screen.getByRole('button', { name: 'Review unresolved requests only' }))
    await waitFor(() => expect(api.previewBulkApprovals).toHaveBeenLastCalledWith(['r2']))
    expect(await screen.findByRole('button', { name: 'Approve 1 selected' })).toBeEnabled()
  })
  it('reuses the idempotency key on an unchanged retry after unknown network outcome', async () => {
    api.previewBulkApprovals.mockResolvedValue({ items: [first], unavailable: [] })
    api.approveBulkRequests.mockRejectedValueOnce(new Error('Connection interrupted')).mockResolvedValueOnce({ batchId: 'batch', idempotencyKey: 'stable', items: [{ requestId: 'r1', status: 'alreadyApproved', entitlementId: 'grant', requiresPlanApply: true }] })
    mount(<BulkApprovalWorkspace requestIds={['r1']} principals={[]} costCenters={[]} onBack={vi.fn()} onComplete={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Approve 1 selected' }))
    expect(await screen.findByText('Connection interrupted')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Approve 1 selected' }))
    await screen.findByText(/0 approved · 1 already approved · 0 unresolved/)
    expect(api.approveBulkRequests.mock.calls[1][0]).toEqual(api.approveBulkRequests.mock.calls[0][0])
  })
  it('shows missing API failure explicitly and never fabricates a preview', async () => {
    api.previewBulkApprovals.mockRejectedValue(new Error('Bulk preview endpoint unavailable (404)'))
    mount(<BulkApprovalWorkspace requestIds={['r1']} principals={[]} costCenters={[]} onBack={vi.fn()} onComplete={vi.fn()} />)
    expect(await screen.findByText('Bulk preview endpoint unavailable (404)')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Approve 0 selected' })).toBeDisabled()
    expect(api.approveBulkRequests).not.toHaveBeenCalled()
  })
  it('requires an explicit per-item environment move confirmation', async () => {
    api.previewBulkApprovals.mockResolvedValue({ items: [{ ...first, currentEnvironment: 'prod', production: true }], unavailable: [] })
    mount(<BulkApprovalWorkspace requestIds={['r1']} principals={[]} costCenters={[]} onBack={vi.fn()} onComplete={vi.fn()} />)
    expect(await screen.findByRole('button', { name: 'Approve 1 selected' })).toBeDisabled()
    fireEvent.click(screen.getByRole('checkbox', { name: 'I confirm the move to prod for this request' }))
    expect(screen.getByRole('button', { name: 'Approve 1 selected' })).toBeEnabled()
  })
  it('bounds explicit selection to 100 requests', () => {
    mount(<BulkRequestSelection requests={Array.from({ length: 102 }, (_, i) => ({ ...first.request, id: `request-${i}` }))} principals={[]} onBack={vi.fn()} onReview={vi.fn()} />)
    fireEvent.click(screen.getByRole('button', { name: 'Select first 100 pending requests' }))
    expect(screen.getAllByRole('checkbox', { checked: true })).toHaveLength(100)
    expect(screen.getAllByRole('checkbox', { checked: false }).every(element => element.hasAttribute('disabled'))).toBe(true)
  })
  it('excludes all winning grants from unresolved retries', () => {
    expect(unresolvedRequestIds([
      { requestId: 'a', status: 'approved', requiresPlanApply: true }, { requestId: 'b', status: 'alreadyApproved', requiresPlanApply: true },
      { requestId: 'c', status: 'failed', requiresPlanApply: false }, { requestId: 'd', status: 'validationFailed', requiresPlanApply: false },
    ])).toEqual(['c', 'd'])
  })
})
describe('personal administrator preferences', () => {
  it('saves digest opt-in and a UTC schedule without sending identity/persona fields', async () => {
    api.saveNotificationPreferences.mockImplementation(async body => ({ ...preferences, ...body, revision: 3 }))
    mount(<NotificationPreferences />)
    await waitFor(() => expect(screen.getByRole('button', { name: 'Edit preferences' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: 'Edit preferences' }))
    fireEvent.change(screen.getByLabelText('Request lifecycle email'), { target: { value: 'digest' } })
    expect(screen.getByLabelText('daily delivery time')).toHaveValue('09:00')
    fireEvent.change(screen.getByLabelText('Usage notification thresholds (%)'), { target: { value: '75, 100' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save preferences' }))
    await waitFor(() => expect(api.saveNotificationPreferences).toHaveBeenCalledOnce())
    const body = api.saveNotificationPreferences.mock.calls[0][0]
    expect(body).not.toHaveProperty('userObjectId')
    expect(body).not.toHaveProperty('persona')
    expect(body.categories[0].email).toEqual({ mode: 'digest', digestPeriods: ['daily'] })
    expect(body.categories[0].inApp).toBe(true)
    expect(body.thresholds).toEqual([75, 100])
    expect(body.digestSchedules).toEqual([{ period: 'daily', deliveryTime: '09:00', dayOfWeek: 0, dayOfMonth: 1 }])
    expect(api.saveNotificationPreferences.mock.calls[0][1]).toBe(2)
  })
  it('does not replace unavailable server preferences with local defaults', async () => {
    api.getNotificationPreferences.mockRejectedValue(new Error('Personal preferences unavailable (503)'))
    mount(<NotificationPreferences />)
    expect(await screen.findByText('Personal preferences unavailable (503)')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Edit preferences' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Retry preferences' })).toBeEnabled()
  })
  it('shows saved preview availability and suppression reasons rather than asserting delivery', async () => {
    api.previewNotificationPreferences.mockResolvedValue({
      preferences, checks: [], channels: [{ category: 'requestLifecycle', channel: 'email', mode: 'off', digestPeriods: [], nextDeliveryAt: null, available: false, reasons: ['Directory-backed mailbox unavailable.'] }],
    })
    mount(<NotificationPreferences />)
    fireEvent.click(screen.getByRole('button', { name: 'Preview effective delivery' }))
    expect(await screen.findByText('Directory-backed mailbox unavailable.')).toBeVisible()
    expect(screen.getByText('Not scheduled')).toBeVisible()
  })
  it('projects only editable settings and keeps external channels opt-in for missing categories', () => {
    const value = notificationPreferencesInput({ ...preferences, categories: [] })
    expect(value).not.toHaveProperty('revision')
    expect(value.categories.every(c => c.inApp && c.email.mode === 'off' && c.teams.mode === 'off')).toBe(true)
  })
})
describe('readiness and notification truth', () => {
  it('resolves an inbox deep link outside the current page and persists its read status', async () => {
    const item = { id: 'historical-item', title: 'Historical request approved', summary: 'Grant intent was saved.', category: 'requestLifecycle', severity: 'information', subject: { requestId: 'request-id' }, occurredAt: '2026-01-01T00:00:00Z', readAt: null }
    api.getNotificationInboxItem.mockResolvedValue(item)
    api.setNotificationRead.mockResolvedValue({ ...item, readAt: '2026-01-01T01:00:00Z' })
    mount(<NotificationInboxPage />, '/notifications?selected=historical-item')
    fireEvent.click(await screen.findByRole('button', { name: 'Mark read' }))
    expect(await screen.findByRole('button', { name: 'Mark unread' })).toBeVisible()
    expect(api.getNotificationInboxItem).toHaveBeenCalledWith('historical-item')
    expect(api.setNotificationRead).toHaveBeenCalledWith('historical-item', true)
    expect(screen.getByRole('link', { name: 'Open related record' })).toHaveAttribute('href', '/entitlements?tab=requests&selected=request-id')
  })
  it('loads an exact notified gateway run rather than showing only current gateway state', async () => {
    api.getSyncRun.mockResolvedValue({ id: 'run/opaque', gatewayId: 'gateway', status: 'failed', startedAt: '2026-01-01T00:00:00Z', completedAt: '2026-01-01T00:01:00Z', errors: ['Inventory permissions are unavailable.'] })
    mount(<GatewayNotificationRun gatewayId="gateway" />, '/gateways/gateway?syncRun=run%2Fopaque')
    expect(await screen.findByText('Inventory permissions are unavailable.')).toBeVisible()
    expect(api.getSyncRun).toHaveBeenCalledExactlyOnceWith('gateway', 'run/opaque')
    expect(screen.getByText(/exact historical run referenced by the notification/)).toBeVisible()
  })
  it('rejects invalid time zones and partial or fractional bounded limits', () => {
    expect(validTimeZone('UTC')).toBe(true)
    expect(validTimeZone('Not/AZone')).toBe(false)
    expect(limitInputError({ ...emptyLimitForm, tokensPerMinute: '1.5' })).toBeTruthy()
    expect(limitInputError({ ...emptyLimitForm, calls: '2', renewalPeriodSeconds: '' })).toBeTruthy()
    expect(limitInputError({ ...emptyLimitForm, calls: '2' })).toBeNull()
  })
  it('distinguishes configured AI from verified readiness', () => {
    mount(<AiReadiness value={{ configured: true, ready: false, connectionVerified: false, responseContractVerified: false, blockers: [{ code: 'grantPending', message: 'Apply the caller grant plan first.', nextAction: 'Open Entitlements.' }], checkedAt: '2026-01-01T00:00:00Z', remediationLinks: ['/entitlements'] }} />)
    expect(screen.getByText('Not ready')).toBeVisible()
    expect(screen.getAllByText('Not verified')).toHaveLength(2)
    expect(screen.getByText(/Apply the caller grant plan first/)).toBeVisible()
  })
  it('does not show expired capability evidence as healthy', () => {
    mount(<CapabilityEvidence check={{ feature: 'email', scope: 'tenant', required: true, state: 'verified', checkedAt: '2020-01-01T00:00:00Z', freshUntil: '2020-01-02T00:00:00Z', evidence: ['Provider accepted a historical test.'] }} />)
    expect(screen.getByText('Stale evidence')).toBeVisible()
    expect(screen.queryByText('verified')).not.toBeInTheDocument()
  })
  it('keeps unread counts distinct from the pending request count', async () => {
    mount(<><RequestNotifications /><NotificationBell /></>)
    expect(await screen.findByRole('button', { name: 'Pending approvals, 2 pending requests' })).toBeVisible()
    expect(await screen.findByRole('button', { name: 'Notification inbox, 7 unread' })).toBeVisible()
  })
  it('links exact authenticated subjects without accepting arbitrary notification URLs', () => {
    expect(notificationTarget({ requestId: 'request/opaque' })).toBe('/entitlements?tab=requests&selected=request%2Fopaque')
    expect(notificationTarget({})).toBeNull()
    expect(notificationTarget({ gatewayId: 'gateway/id', runId: 'run/id', integration: 'gatewayInventory' })).toBe('/gateways/gateway%2Fid?syncRun=run%2Fid')
    expect(notificationTarget({ endpointId: 'endpoint', runId: 'run', integration: 'endpointInventory' })).toBeNull()
  })
  it('marks advisory evidence expired and never supplies an approval action', () => {
    mount(<RecommendationEvidence contextRevision="new-context" value={{
      id: 'recommendation', requestId: 'r1', context: { contextRevision: 'old-context' }, configRevision: 1, modelResource: { kind: 'modelApi', id: 'governed-model' },
      generatedAt: '2020-01-01T00:00:00Z', expiresAt: '2020-01-02T00:00:00Z', promptVersion: '1',
      recommendation: { suggestedOutcome: 'recommendApprove', reasons: [{ explanation: 'Bounded scope.', evidenceIds: ['scope-evidence'] }], missingInformation: ['Owner confirmation.'], riskFlags: ['Production request.'] },
      deterministicBlockers: [{ code: 'moved', message: 'Environment changed.' }],
    }} />)
    expect(screen.getByText(/Expired or changed context/)).toBeVisible()
    expect(screen.getByText(/scope-evidence/)).toBeVisible()
    expect(screen.getByText('Environment changed.')).toBeVisible()
    expect(screen.queryByRole('button', { name: /Approve/ })).not.toBeInTheDocument()
  })
})
