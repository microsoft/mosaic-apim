import { NOTIFICATION_CATEGORIES, type BulkApprovalItemResult, type DigestPeriod, type DigestSchedule, type NotificationPreferencesInput, type NotificationSubject } from './admin-types'
import { callRateError, type LimitForm } from './entitlement-limits'

export function unresolvedRequestIds(items: BulkApprovalItemResult[]) {
  return items.filter(item => item.status !== 'approved' && item.status !== 'alreadyApproved').map(item => item.requestId)
}
export function validTimeZone(value: string) {
  try {
    new Intl.DateTimeFormat('en', { timeZone: value })
    return Boolean(value.trim())
  } catch { return false }
}
export function limitInputError(form: LimitForm): string | null {
  if ([form.calls, form.tokensPerMinute, form.tokenQuota].some(v => v !== '' && (!Number.isSafeInteger(Number(v)) || Number(v) <= 0))) return 'Limits must be positive whole numbers, or left empty.'
  if (form.calls && (!Number.isSafeInteger(Number(form.renewalPeriodSeconds)) || Number(form.renewalPeriodSeconds) <= 0)) return 'Call renewal period must be a positive whole number.'
  return callRateError(form)
}
export function schedulesFor(periods: DigestPeriod[], saved: DigestSchedule[]) {
  return [...new Set(periods)].map(period => saved.find(s => s.period === period) ?? { period, deliveryTime: '09:00', dayOfWeek: 0, dayOfMonth: 1 })
}
export function notificationPreferencesInput(value: NotificationPreferencesInput): NotificationPreferencesInput {
  return {
    timeZone: value.timeZone,
    digestSchedules: structuredClone(value.digestSchedules),
    quietHours: value.quietHours ? structuredClone(value.quietHours) : null,
    thresholds: [...value.thresholds],
    repeatCooldownSeconds: value.repeatCooldownSeconds,
    categories: NOTIFICATION_CATEGORIES.map(({ value: category }) =>
      structuredClone(value.categories.find(c => c.category === category) ?? {
        category, inApp: true, email: { mode: 'off', digestPeriods: [] }, teams: { mode: 'off', digestPeriods: [] },
      })),
  }
}
export function notificationTarget(subject: NotificationSubject) {
  if (subject.requestId) return `/entitlements?tab=requests&selected=${encodeURIComponent(subject.requestId)}`
  if (subject.entitlementId) return `/entitlements?tab=grants&selected=${encodeURIComponent(subject.entitlementId)}`
  if (subject.publicationId && subject.publicationTarget) {
    const publicationId = encodeURIComponent(subject.publicationId)
    const run = subject.runId ? `&runId=${encodeURIComponent(subject.runId)}` : ''
    if (subject.publicationTarget === 'pool') return `/pools/${publicationId}?tab=plans${run}`
    return `/${subject.publicationTarget === 'mcp' ? 'mcps' : 'models'}?tab=published&selected=${publicationId}${run}`
  }
  if (subject.endpointId && subject.endpointKind) {
    const run = subject.runId ? `&syncRun=${encodeURIComponent(subject.runId)}` : ''
    return `/${subject.endpointKind === 'mcp' ? 'mcps?tab=registered' : 'models?tab=endpoints'}&selected=${encodeURIComponent(subject.endpointId)}${run}`
  }
  if (subject.gatewayId && subject.integration === 'gatewayInventory') return `/gateways/${encodeURIComponent(subject.gatewayId)}${subject.runId ? `?syncRun=${encodeURIComponent(subject.runId)}` : ''}`
  if (subject.costCenterId) return `/cost-centers/${encodeURIComponent(subject.costCenterId)}`
  if (subject.resource?.kind === 'poolModel' && subject.resource.scopeId) return `/pools/${encodeURIComponent(subject.resource.scopeId)}`
  if (subject.resource?.kind === 'modelApi') return `/models?tab=imported&selected=${encodeURIComponent(subject.resource.id)}`
  if (subject.resource?.kind === 'mcpServer') return `/mcps?tab=imported&selected=${encodeURIComponent(subject.resource.id)}`
  if (subject.gatewayId) return `/gateways/${encodeURIComponent(subject.gatewayId)}`
  if (subject.endpointId) return null
  if (subject.integration) return '/settings?tab=notifications'
  return null
}
