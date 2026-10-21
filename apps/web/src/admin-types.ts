import type { AccessRequest, AccessRequestApproval, EntitlementEnforcement, EntitlementResource, EntitlementResourceKind, ResourceSummary } from './types'

export interface RecordMetadata { id: string; revision: number; createdAt?: string; updatedAt?: string }
export interface ApprovalBlocker { code: string; message: string; nextAction?: string | null }
export interface ApprovalContextRevision {
  contextRevision: string; requestEtag?: string | null; resourceEtag?: string | null
  costCenterEtag?: string | null; environmentRevision?: string | null; eligibilityCheckedAt?: string | null
}
export interface ApprovalPreview {
  request: AccessRequest; resourceSummary?: ResourceSummary | null; revision: ApprovalContextRevision
  currentEnvironment?: string | null; production?: boolean | null; effectiveCostCenterId?: string | null
  effectiveEnforcement?: EntitlementEnforcement | null; limitsSource: 'explicit' | 'costCenter' | 'unrestricted'
  existingEntitlementId?: string | null; mutationLocked: boolean; blockers: ApprovalBlocker[]; requiresPlanApply: true
}
export interface BulkApprovalPreview { items: ApprovalPreview[]; unavailable: { requestId: string; blocker: ApprovalBlocker }[] }
export interface BulkApprovalInput { idempotencyKey: string; items: { requestId: string; approval: AccessRequestApproval; expectedContext?: ApprovalContextRevision }[] }
export interface BulkApprovalItemResult {
  requestId: string; status: 'approved' | 'alreadyApproved' | 'conflict' | 'validationFailed' | 'failed'
  entitlementId?: string | null; message?: string | null; nextAction?: string | null; requiresPlanApply: boolean
}
export interface BulkApprovalResult { batchId: string; idempotencyKey: string; items: BulkApprovalItemResult[] }
export interface WeeklyTimeWindow { daysOfWeek: number[]; startTime: string; endTime: string }
export interface ApprovalPolicyInput {
  name: string; description?: string | null; enabled: boolean
  scope: { environments: string[]; resourceKinds: EntitlementResourceKind[]; resourceIds: string[]; costCenterIds: string[] }
  productionOptIn: boolean; schedule: { timeZone: string; windows: WeeklyTimeWindow[] }
  minimumRequestAgeSeconds: number
  grantTemplate: { enforcement: EntitlementEnforcement; costCenterId?: string | null; note?: string | null }
}
export interface ApprovalPolicy extends ApprovalPolicyInput, RecordMetadata {}
export interface ApprovalPolicySaveInput extends ApprovalPolicyInput { expectedRevision?: number; enableConfirmed: boolean }
export interface ApprovalPolicyEvaluation {
  id: string; requestId: string; policyId: string; policyRevision: number
  outcome: 'eligible' | 'notMatched' | 'waiting' | 'blocked' | 'ambiguous'
  evaluatedAt: string; nextEligibleAt?: string | null; explanation: string; blockers: ApprovalBlocker[]
}
export interface ApprovalEvaluatorState { paused: boolean; lastSucceededAt?: string | null; pendingCount: number; lastError?: string | null }
export interface AiApprovalSettingsInput {
  enabled: boolean; resource: EntitlementResource | null; callerPrincipalId: string | null; entitlementId: string | null
  costCenterId: string | null; operation: 'chatCompletions' | 'responses'; maxInputCharacters: number
  maxOutputTokens: number; timeoutSeconds: number; concurrency: number; maxBatchSize: number
}
export interface AiApprovalSettings extends AiApprovalSettingsInput, RecordMetadata { promptVersion: string; verifiedAt?: string | null }
export interface AiApprovalReadiness {
  ready: boolean; configured: boolean; connectionVerified: boolean; responseContractVerified: boolean
  blockers: ApprovalBlocker[]; checkedAt: string; remediationLinks: string[]
  lastSuccessfulCallAt?: string | null; lastErrorAt?: string | null; lastErrorCode?: string | null
}
export interface AiApprovalOffering { resource: EntitlementResource; summary: ResourceSummary | null; supportedOperations: string[]; compatible: boolean; blockers: ApprovalBlocker[] }
export interface AiApprovalRecommendation {
  id: string; requestId: string; context: ApprovalContextRevision; configRevision: number; modelResource: EntitlementResource
  generatedAt: string; expiresAt: string; promptVersion: string
  recommendation: {
    suggestedOutcome: 'recommendApprove' | 'needsReview' | 'recommendDeny'
    reasons: { explanation: string; evidenceIds: string[] }[]; missingInformation: string[]; riskFlags: string[]
  }
  deterministicBlockers: ApprovalBlocker[]
}

export const NOTIFICATION_CATEGORIES = [
  { value: 'requestLifecycle', label: 'Request lifecycle' }, { value: 'accessLifecycle', label: 'Access lifecycle' },
  { value: 'operationalIssues', label: 'Operational issues' }, { value: 'usageAndBudgets', label: 'Usage and budgets' },
  { value: 'integrationHealth', label: 'Integration health' },
] as const
export type NotificationCategory = typeof NOTIFICATION_CATEGORIES[number]['value']
export type DigestPeriod = 'daily' | 'weekly' | 'monthly'
export interface ChannelCadence { mode: 'off' | 'immediate' | 'digest'; digestPeriods: DigestPeriod[] }
export interface DigestSchedule { period: DigestPeriod; deliveryTime: string; dayOfWeek: number; dayOfMonth: number }
export interface CategoryNotificationPreference { category: NotificationCategory; inApp: boolean; email: ChannelCadence; teams: ChannelCadence }
export interface NotificationPreferencesInput {
  timeZone: string; categories: CategoryNotificationPreference[]; digestSchedules: DigestSchedule[]
  quietHours: WeeklyTimeWindow | null; thresholds: number[]; repeatCooldownSeconds: number
}
export interface NotificationPreferences extends NotificationPreferencesInput, RecordMetadata { persona: 'admin'; userObjectId: string }
export interface NotificationPreferencesPreview {
  preferences: NotificationPreferences
  channels: { category: NotificationCategory; channel: 'inApp' | 'email' | 'teamsPersonal'; mode: ChannelCadence['mode']; digestPeriods: DigestPeriod[]; nextDeliveryAt: string | null; available: boolean; reasons: string[] }[]
  checks: CapabilityCheck[]
}
export interface NotificationSubject {
  resource?: EntitlementResource | null; requestId?: string | null; entitlementId?: string | null
  budgetId?: string | null; costCenterId?: string | null; integration?: string | null
  gatewayId?: string | null; runId?: string | null; endpointId?: string | null
  publicationId?: string | null; publicationTarget?: 'model' | 'mcp' | 'pool' | null
  endpointKind?: 'model' | 'mcp' | null
}
export interface NotificationInboxItem {
  id: string; category: NotificationCategory; severity: 'information' | 'warning' | 'error'
  title: string; summary: string; subject: NotificationSubject; occurredAt: string; readAt: string | null
}
export interface NotificationInboxPage { items: NotificationInboxItem[]; continuationToken?: string | null; unreadCount: number }
export interface NotificationChannelSettingsInput {
  inAppEnabled: boolean; emailEnabled: boolean; teamsChannelEnabled: boolean; teamsPersonalEnabled: boolean; retentionDays: number
}
export interface NotificationChannelSettings extends NotificationChannelSettingsInput, RecordMetadata {}
export interface NotificationRouteInput {
  name: string; enabled: boolean; audience: 'selectedAdmins' | 'allAdmins' | 'budgetRecipients' | 'teamsDestination'
  administratorObjectIds: string[]; budgetId: string | null; teamsDestinationId: string | null
  categories: NotificationCategory[]; eventTypes: string[]; channel: 'inApp' | 'email' | 'teamsChannel'
  cadence: ChannelCadence; timeZone: string; digestSchedules: DigestSchedule[]
}
export interface NotificationRoute extends NotificationRouteInput, RecordMetadata {}
export interface AdministratorChoices {
  administrators: { objectId: string; displayName: string; mailbox: string | null; available: boolean; reason: string | null }[]
  complete: boolean; checkedAt: string | null; freshUntil: string | null; reason: string | null
}
export interface NotificationRoutePreview {
  routeId: string
  recipients: { recipientReference: string; channel: string; eligible: boolean; routeIds: string[]; reasons: string[]; nextDeliveryAt: string | null }[]
  unavailable: string[]; checkedAt: string
}
export interface PersonalTeamsSettingsInput { enabled: boolean; appId: string | null; botAppId: string | null; cloud: 'public' | 'usGovernment' }
export interface PersonalTeamsSettings extends PersonalTeamsSettingsInput, RecordMetadata {}
export interface TeamsTestResult { outcome: 'providerAccepted' | 'confirmedDelivered' | 'retryableFailure' | 'permanentFailure' | 'unknownOutcome' | 'suppressed'; message: string; testedAt: string; confirmedDelivered: boolean; configurationRevision?: string | null; errorCode?: string | null }
export interface PersonalTeamsStatus { enabled: boolean; state: 'notInstalled' | 'installed' | 'uninstalled' | 'blocked'; checks: CapabilityCheck[]; lastTest?: TeamsTestResult | null }
export interface TeamsDestinationInput { name: string; enabled: boolean; allowedCategories: NotificationCategory[]; webhookUrl?: string }
export interface TeamsDestination extends Omit<TeamsDestinationInput, 'webhookUrl'>, RecordMetadata { credentialReferenceId: string }
export interface CapabilityCheck {
  feature: string; scope: string; required: boolean
  state: 'disabled' | 'notConfigured' | 'needsAction' | 'checking' | 'verified' | 'degraded' | 'failed' | 'unknown'
  reason?: string | null; evidence: string[]; checkedAt?: string | null; freshUntil?: string | null
  lastTestAt?: string | null; configurationRevision?: string | null
  remediation?: { action: string; path?: string | null; command?: string | null; owner: string } | null
}
export interface NotificationChannelHealth {
  id: string; channel: string; destinationId?: string | null; checks: CapabilityCheck[]
  lastProviderAcceptedAt?: string | null; lastConfirmedDeliveredAt?: string | null; lastFailureAt?: string | null
}
export interface NotificationDelivery {
  id: string; channel: string; recipientReference: string; attempts: number
  state: 'queued' | 'sending' | 'providerAccepted' | 'confirmedDelivered' | 'suppressed' | 'retrying' | 'permanentlyFailed' | 'unknownOutcome'
  nextAttemptAt?: string | null; lastError?: string | null; suppressionReason?: string | null
  providerAcceptedAt?: string | null; confirmedDeliveredAt?: string | null
}
