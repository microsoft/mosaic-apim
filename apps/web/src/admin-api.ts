import type * as T from './admin-types'

type Request = <R>(path: string, options?: { method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'; body?: unknown; ifMatch?: number }) => Promise<R>
export function adminApi(request: Request) {
  const path = (value: string) => `/api/v1${value}`
  const id = encodeURIComponent
  return {
    previewBulkApprovals: (requestIds: string[]) => request<T.BulkApprovalPreview>(path('/access-requests/bulk/preview'), { method: 'POST', body: { requestIds } }),
    approveBulkRequests: (body: T.BulkApprovalInput) => request<T.BulkApprovalResult>(path('/access-requests/bulk/approve'), { method: 'POST', body }),
    listApprovalPolicies: () => request<T.ApprovalPolicy[]>(path('/approval-policies')),
    saveApprovalPolicy: (policyId: string | null, body: T.ApprovalPolicySaveInput) => request<T.ApprovalPolicy>(path(`/approval-policies${policyId ? `/${id(policyId)}` : ''}`), { method: policyId ? 'PUT' : 'POST', body }),
    deleteApprovalPolicy: (policyId: string, expectedRevision: number) => request<void>(path(`/approval-policies/${id(policyId)}?expected_revision=${expectedRevision}`), { method: 'DELETE' }),
    previewApprovalPolicy: (policyId: string, requestIds: string[]) => request<T.ApprovalPolicyEvaluation[]>(path(`/approval-policies/${id(policyId)}/preview`), { method: 'POST', body: { requestIds } }),
    dryRunApprovalPolicy: (requestIds: string[], policy: T.ApprovalPolicyInput) => request<T.ApprovalPolicyEvaluation[]>(path('/approval-policies/dry-run'), { method: 'POST', body: { requestIds, policy } }),
    getApprovalEvaluator: () => request<T.ApprovalEvaluatorState>(path('/approval-evaluator')),
    setApprovalEvaluator: (paused: boolean) => request<T.ApprovalEvaluatorState>(path('/approval-evaluator'), { method: 'PUT', body: { paused } }),
    getApprovalAiSettings: () => request<T.AiApprovalSettings>(path('/settings/approval-ai')),
    saveApprovalAiSettings: (body: T.AiApprovalSettingsInput & { expectedRevision?: number }) => request<T.AiApprovalSettings>(path('/settings/approval-ai'), { method: 'PUT', body }),
    getApprovalAiReadiness: () => request<T.AiApprovalReadiness>(path('/approval-ai/readiness')),
    listApprovalAiOfferings: () => request<T.AiApprovalOffering[]>(path('/approval-ai/offerings')),
    testApprovalAi: () => request<T.AiApprovalReadiness>(path('/approval-ai/test'), { method: 'POST' }),
    getApprovalRecommendations: (requestId: string) => request<T.AiApprovalRecommendation[]>(path(`/access-requests/${id(requestId)}/recommendation`)),
    recommendApprovals: (requestIds: string[]) => request<{ requestId: string; recommendation?: T.AiApprovalRecommendation | null; blocker?: T.ApprovalBlocker | null }[]>(path('/approval-ai/recommend'), { method: 'POST', body: { requestIds } }),
    getNotificationPreferences: () => request<T.NotificationPreferences>(path('/notifications/me/preferences')),
    previewNotificationPreferences: () => request<T.NotificationPreferencesPreview>(path('/notifications/me/preferences/preview')),
    saveNotificationPreferences: (body: T.NotificationPreferencesInput, expectedRevision?: number) => request<T.NotificationPreferences>(path('/notifications/me/preferences'), { method: 'PUT', body, ifMatch: expectedRevision }),
    listNotificationInbox: (filters: { category?: string; unreadOnly?: boolean; continuationToken?: string } = {}) => {
      const query = new URLSearchParams()
      if (filters.category) query.set('category', filters.category)
      if (filters.unreadOnly) query.set('unread_only', 'true')
      if (filters.continuationToken) query.set('continuation_token', filters.continuationToken)
      return request<T.NotificationInboxPage>(path(`/notifications/me/inbox?${query}`))
    },
    setNotificationRead: (itemId: string, read: boolean) => request<T.NotificationInboxItem>(path(`/notifications/me/inbox/${id(itemId)}`), { method: 'PATCH', body: { read } }),
    getNotificationInboxItem: (itemId: string) => request<T.NotificationInboxItem>(path(`/notifications/me/inbox/${id(itemId)}`)),
    getNotificationSettings: () => request<T.NotificationChannelSettings>(path('/settings/notifications')),
    saveNotificationSettings: (body: T.NotificationChannelSettingsInput, expectedRevision?: number) => request<T.NotificationChannelSettings>(path('/settings/notifications'), { method: 'PUT', body, ifMatch: expectedRevision }),
    listNotificationRoutes: () => request<T.NotificationRoute[]>(path('/notification-routes')),
    getNotificationAdministratorChoices: () => request<T.AdministratorChoices>(path('/notification-routes/administrator-choices')),
    previewNotificationRoute: (routeId: string) => request<T.NotificationRoutePreview>(path(`/notification-routes/${id(routeId)}/preview`)),
    saveNotificationRoute: (routeId: string | null, body: T.NotificationRouteInput, expectedRevision?: number) => request<T.NotificationRoute>(path(`/notification-routes${routeId ? `/${id(routeId)}` : ''}`), { method: routeId ? 'PUT' : 'POST', body, ifMatch: expectedRevision }),
    deleteNotificationRoute: (routeId: string, expectedRevision?: number) => request<void>(path(`/notification-routes/${id(routeId)}`), { method: 'DELETE', ifMatch: expectedRevision }),
    listTeamsDestinations: () => request<T.TeamsDestination[]>(path('/settings/teams/destinations')),
    saveTeamsDestination: (destinationId: string | null, body: T.TeamsDestinationInput) => request<T.TeamsDestination>(path(`/settings/teams/destinations${destinationId ? `/${id(destinationId)}` : ''}`), { method: destinationId ? 'PUT' : 'POST', body }),
    deleteTeamsDestination: (destinationId: string) => request<void>(path(`/settings/teams/destinations/${id(destinationId)}`), { method: 'DELETE' }),
    testTeamsDestination: (destinationId: string) => request<T.TeamsTestResult>(path(`/settings/teams/destinations/${id(destinationId)}/test`), { method: 'POST' }),
    checkTeamsDestination: (destinationId: string) => request<T.CapabilityCheck[]>(path(`/settings/teams/destinations/${id(destinationId)}/checks`)),
    getPersonalTeamsSettings: () => request<T.PersonalTeamsSettings>(path('/settings/teams/personal')),
    savePersonalTeamsSettings: (body: T.PersonalTeamsSettingsInput) => request<T.PersonalTeamsSettings>(path('/settings/teams/personal'), { method: 'PUT', body }),
    getMyTeamsStatus: () => request<T.PersonalTeamsStatus>(path('/notifications/me/teams')),
    checkMyTeamsAccess: () => request<T.CapabilityCheck[]>(path('/notifications/me/teams/checks')),
    testMyTeamsConnection: () => request<T.TeamsTestResult>(path('/notifications/me/teams/test'), { method: 'POST' }),
    listNotificationDeliveries: () => request<T.NotificationDelivery[]>(path('/notifications/deliveries')),
    getNotificationHealth: () => request<T.CapabilityCheck[]>(path('/notifications/health')),
    retryNotificationDelivery: (deliveryId: string) => request<T.NotificationDelivery>(path(`/notifications/deliveries/${id(deliveryId)}/retry`), { method: 'POST', body: {} }),
  }
}
export type AdminApi = ReturnType<typeof adminApi>
