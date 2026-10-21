import type { EntitlementResource } from './types'

export type NotificationCategory = 'requestLifecycle' | 'accessLifecycle' | 'operationalIssues' | 'usageAndBudgets' | 'integrationHealth'
export type DigestPeriod = 'daily' | 'weekly' | 'monthly'
export type PersonalNotificationChannel = 'inApp' | 'email' | 'teamsPersonal'

export interface ChannelCadence {
  mode: 'off' | 'immediate' | 'digest'
  digestPeriods: DigestPeriod[]
}

export interface CategoryNotificationPreference {
  category: NotificationCategory
  inApp: boolean
  email: ChannelCadence
  teams: ChannelCadence
}

export interface DigestSchedule {
  period: DigestPeriod
  deliveryTime: string
  dayOfWeek: number
  dayOfMonth: number
}

export interface NotificationPreferencesInput {
  timeZone: string
  categories: CategoryNotificationPreference[]
  digestSchedules: DigestSchedule[]
  quietHours: { daysOfWeek: number[]; startTime: string; endTime: string } | null
  thresholds: number[]
  repeatCooldownSeconds: number
}

export interface NotificationPreferences extends NotificationPreferencesInput {
  id: string
  tenantId: string
  userObjectId: string
  persona: 'portal'
  entityType: 'notificationPreferences'
  revision: number
}

export interface NotificationSubject {
  resource?: EntitlementResource | null
  requestId?: string | null
  entitlementId?: string | null
  budgetId?: string | null
  costCenterId?: string | null
  incidentId?: string | null
  integration?: string | null
}

export interface NotificationInboxItem {
  id: string
  tenantId: string
  userObjectId: string
  persona: 'portal'
  entityType: 'notificationInboxItem'
  eventId: string
  category: NotificationCategory
  severity: 'information' | 'warning' | 'error'
  title: string
  summary: string
  subject: NotificationSubject
  occurredAt: string
  sortKey: string
  readAt: string | null
  ttl: number
}

export interface NotificationInboxPage {
  items: NotificationInboxItem[]
  continuationToken: string | null
  unreadCount: number
}

export interface NotificationInboxFilter {
  category?: NotificationCategory
  unreadOnly?: boolean
  limit?: number
  continuationToken?: string
}

export type CapabilityState = 'disabled' | 'notConfigured' | 'needsAction' | 'checking' | 'verified' | 'degraded' | 'failed' | 'unknown'

export interface CapabilityCheck {
  feature: string
  scope: string
  required: boolean
  state: CapabilityState
  reason: string | null
  evidence: string[]
  checkedAt: string | null
  freshUntil: string | null
  configurationRevision: string | null
  lastTestAt: string | null
  dependencies: string[]
  remediation: {
    action: string
    path: string | null
    command: string | null
    owner: 'mosaicAdmin' | 'azureAdmin' | 'entraAdmin' | 'teamsAdmin' | 'user'
  } | null
}

export interface NotificationPreferencePreview {
  preferences: NotificationPreferences
  channels: {
    category: NotificationCategory
    channel: PersonalNotificationChannel | 'teamsChannel'
    mode: ChannelCadence['mode']
    digestPeriods: DigestPeriod[]
    nextDeliveryAt: string | null
    available: boolean
    reasons: string[]
  }[]
  checks: CapabilityCheck[]
}

export interface PersonalTeamsStatus {
  enabled: boolean
  state: 'notInstalled' | 'installed' | 'uninstalled' | 'blocked'
  checks: CapabilityCheck[]
  lastTest?: PersonalTeamsTestResult | null
}

export interface PersonalTeamsTestResult {
  outcome: 'providerAccepted' | 'confirmedDelivered' | 'retryableFailure' | 'permanentFailure' | 'unknownOutcome' | 'suppressed'
  message: string
  testedAt: string
  confirmedDelivered: boolean
  configurationRevision?: string | null
  errorCode?: string | null
}
