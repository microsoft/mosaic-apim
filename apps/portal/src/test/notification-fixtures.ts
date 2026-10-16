import { defaultCategoryPreference, notificationCategories } from '../notification-format'
import type { CapabilityCheck, NotificationInboxItem, NotificationPreferences } from '../notification-types'

export function notificationItem(overrides: Partial<NotificationInboxItem> = {}): NotificationInboxItem {
  return {
    id: 'notice-1', tenantId: 'tenant-1', userObjectId: 'user-1', persona: 'portal',
    entityType: 'notificationInboxItem', eventId: 'event-1', category: 'requestLifecycle',
    severity: 'information', title: 'Request approved', summary: 'Your request for Research chat was approved.',
    subject: { requestId: 'request/1', entitlementId: 'grant/1' },
    occurredAt: '2026-10-01T12:00:00Z', sortKey: '2026-10-01T12:00:00Z|notice-1', readAt: null, ttl: 7776000,
    ...overrides,
  }
}

export function notificationPreferences(overrides: Partial<NotificationPreferences> = {}): NotificationPreferences {
  return {
    id: 'preference-1', tenantId: 'tenant-1', userObjectId: 'user-1', persona: 'portal',
    entityType: 'notificationPreferences', revision: 1, timeZone: 'UTC',
    categories: notificationCategories.map(({ value }) => defaultCategoryPreference(value)),
    digestSchedules: [], quietHours: null, thresholds: [80, 100], repeatCooldownSeconds: 86400,
    ...overrides,
  }
}

export function notificationCheck(overrides: Partial<CapabilityCheck> = {}): CapabilityCheck {
  return {
    feature: 'Personal Teams', scope: 'currentUser', required: true, state: 'needsAction',
    reason: 'Install the personal Teams app for your signed-in account.', evidence: [],
    checkedAt: '2026-10-01T12:00:00Z', freshUntil: null, configurationRevision: null,
    lastTestAt: null, dependencies: [], remediation: {
      action: 'Ask your Teams administrator to allow the MOSAIC personal app, then install it.',
      path: 'https://teams.microsoft.com/l/app/example', command: null, owner: 'teamsAdmin',
    },
    ...overrides,
  }
}
