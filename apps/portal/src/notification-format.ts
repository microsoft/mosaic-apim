import type {
  CapabilityCheck, CategoryNotificationPreference, DigestPeriod, DigestSchedule,
  NotificationInboxItem, NotificationPreferences, NotificationPreferencesInput,
} from './notification-types'

export const notificationCategories = [
  { value: 'requestLifecycle', label: 'Access requests', description: 'Submissions, decisions, and withdrawals for your requests.' },
  { value: 'accessLifecycle', label: 'My access', description: 'Grant changes and observed gateway readiness for your access.' },
  { value: 'operationalIssues', label: 'Operational issues', description: 'Failures and recovery affecting resources you can access.' },
  { value: 'usageAndBudgets', label: 'Usage & budgets', description: 'Your attributed usage and authorized shared budget totals.' },
  { value: 'integrationHealth', label: 'Notification health', description: 'Delivery and integration issues relevant to your notifications.' },
] as const

export const digestPeriods: DigestPeriod[] = ['daily', 'weekly', 'monthly']
export const weekdayLabels = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
export const notificationInboxKey = ['portal', 'notifications', 'inbox'] as const
export const notificationPreferencesKey = ['portal', 'notifications', 'preferences'] as const
export const notificationHealthKey = ['portal', 'notifications', 'health'] as const

export function defaultCategoryPreference(category: CategoryNotificationPreference['category']): CategoryNotificationPreference {
  return { category, inApp: true, email: { mode: 'off', digestPeriods: [] }, teams: { mode: 'off', digestPeriods: [] } }
}

export function preferenceInput(preferences: NotificationPreferences): NotificationPreferencesInput {
  return {
    timeZone: preferences.timeZone,
    categories: notificationCategories.map(({ value }) =>
      preferences.categories.find((item) => item.category === value) ?? defaultCategoryPreference(value)),
    digestSchedules: preferences.digestSchedules,
    quietHours: preferences.quietHours,
    thresholds: preferences.thresholds,
    repeatCooldownSeconds: preferences.repeatCooldownSeconds,
  }
}

export function defaultDigestSchedule(period: DigestPeriod): DigestSchedule {
  return { period, deliveryTime: '09:00', dayOfWeek: 0, dayOfMonth: 1 }
}

export function preferenceValidation(input: NotificationPreferencesInput): string | null {
  try {
    new Intl.DateTimeFormat('en', { timeZone: input.timeZone }).format()
  } catch {
    return 'Enter an IANA time zone, such as UTC or America/New_York.'
  }
  if (!input.thresholds.length || input.thresholds.length > 5 ||
    input.thresholds.some((value) => !Number.isInteger(value) || value < 1 || value > 1000) ||
    new Set(input.thresholds).size !== input.thresholds.length) {
    return 'Enter one to five different whole-number thresholds from 1 to 1000 percent.'
  }
  if (!Number.isInteger(input.repeatCooldownSeconds) ||
    input.repeatCooldownSeconds < 300 || input.repeatCooldownSeconds > 2_678_400) {
    return 'Repeat cooldown must be between 5 minutes and 31 days.'
  }
  for (const category of input.categories) {
    for (const channel of [category.email, category.teams]) {
      if (channel.mode === 'digest' && !channel.digestPeriods.length) {
        return 'Choose at least one digest period for every channel using digests.'
      }
      if (channel.digestPeriods.some((period) => !input.digestSchedules.some((schedule) => schedule.period === period))) {
        return 'Set a delivery schedule for every selected digest period.'
      }
    }
  }
  if (input.digestSchedules.some((schedule) =>
    !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(schedule.deliveryTime) ||
    !Number.isInteger(schedule.dayOfWeek) || schedule.dayOfWeek < 0 || schedule.dayOfWeek > 6 ||
    !Number.isInteger(schedule.dayOfMonth) || schedule.dayOfMonth < 1 || schedule.dayOfMonth > 28)) {
    return 'Choose a valid digest time, weekday, and monthly day from 1 to 28.'
  }
  if (input.quietHours && (!input.quietHours.daysOfWeek.length ||
    input.quietHours.startTime === input.quietHours.endTime)) {
    return 'Choose at least one quiet-hours day and different start and end times.'
  }
  return null
}

export function notificationLink(item: NotificationInboxItem): { to: string; label: string } | null {
  if (item.subject.requestId) return { to: `/requests?request=${encodeURIComponent(item.subject.requestId)}`, label: 'View request' }
  if (item.subject.entitlementId) return { to: `/access?grant=${encodeURIComponent(item.subject.entitlementId)}`, label: 'View access status' }
  if (item.category === 'usageAndBudgets') return { to: '/usage', label: 'View my usage & cost' }
  if (item.category === 'integrationHealth') return { to: '/notification-preferences', label: 'Check notification readiness' }
  return null
}

export function capabilityStatus(check: CapabilityCheck, now = Date.now()): string {
  if (check.state === 'verified') {
    if (!check.checkedAt || !Number.isFinite(Date.parse(check.checkedAt)) || !check.evidence.length) return 'Verification unavailable'
    if (check.freshUntil && (!Number.isFinite(Date.parse(check.freshUntil)) || Date.parse(check.freshUntil) <= now)) return 'Verification expired'
    return 'Verified'
  }
  return ({
    disabled: 'Disabled', notConfigured: 'Not configured', needsAction: 'Action needed',
    checking: 'Checking', degraded: 'Degraded', failed: 'Failed', unknown: 'Unknown',
  })[check.state]
}

export function safeRemediationPath(path: string | null): string | null {
  if (!path) return null
  if (/^\/(?!\/)/.test(path) && !path.includes('\\') && ![...path].some((character) => character.charCodeAt(0) <= 32)) return path
  try {
    const url = new URL(path)
    if (url.protocol !== 'https:' || url.username || url.password || url.port) return null
    if (['teams.microsoft.com', 'teams.cloud.microsoft', 'learn.microsoft.com'].includes(url.hostname)) return url.href
  } catch { /* A missing or unsupported remediation link is rendered as instructions only. */ }
  return null
}
