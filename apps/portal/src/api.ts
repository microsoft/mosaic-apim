import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  CapabilityCheck, NotificationInboxFilter, NotificationInboxItem, NotificationInboxPage,
  NotificationPreferences, NotificationPreferencesInput, NotificationPreferencePreview,
  PersonalTeamsStatus, PersonalTeamsTestResult,
} from './notification-types'
import type {
  AccessRequest,
  AccessRequestCreate,
  ApiErrorBody,
  CatalogEntry,
  CostCenterRequestCreate,
  CostCenterSelfServiceSettings,
  CostCenterDirectoryPage,
  MembershipCostCenter,
  CostCenterMembershipRequestCreate,
  PortalCostCenterMembershipRequest,
  GrantKey,
  KeyRevealResult,
  KeySlot,
  McpConnection,
  ModelConnection,
  MyUsageReport,
  PortalBudgetAlert,
  PortalCostCenter,
  PortalCostCenterRequest,
  PortalRequestCounts,
  PortalProfile,
  PortalEnvironment,
  ResolvedEntitlement,
  UsagePeriod,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly body?: ApiErrorBody

  constructor(message: string, status: number, body?: ApiErrorBody) {
    super(message)
    this.status = status
    this.body = body
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'DELETE' | 'PUT' | 'PATCH'
  body?: unknown
  cache?: RequestCache
  signal?: AbortSignal
  ifMatch?: number
}

function errorMessage(body: ApiErrorBody | undefined, status: number) {
  if (body?.message) return body.message
  if (typeof body?.detail === 'string' && body.detail) return body.detail
  return `Request failed with status ${status}`
}

function entitlementPath(entitlementId: string) {
  return `/api/v1/me/entitlements/${encodeURIComponent(entitlementId)}`
}

function costCenterSearchPath(path: string, filter: { q?: string; cursor?: string }) {
  const params = new URLSearchParams({ limit: '20' })
  if (filter.q) params.set('q', filter.q)
  if (filter.cursor) params.set('cursor', filter.cursor)
  return `${path}?${params}`
}

export interface PortalApi {
  listNotifications(filter?: NotificationInboxFilter): Promise<NotificationInboxPage>
  setNotificationRead(itemId: string, read: boolean): Promise<NotificationInboxItem>
  getNotificationPreferences(): Promise<NotificationPreferences>
  saveNotificationPreferences(input: NotificationPreferencesInput, revision?: number): Promise<NotificationPreferences>
  getNotificationHealth(): Promise<CapabilityCheck[]>
  getNotificationPreferencePreview(): Promise<NotificationPreferencePreview>
  getPersonalTeamsStatus(): Promise<PersonalTeamsStatus>
  checkPersonalTeamsAccess(): Promise<CapabilityCheck[]>
  testPersonalTeams(): Promise<PersonalTeamsTestResult>
  getProfile(): Promise<PortalProfile>
  listEntitlements(): Promise<ResolvedEntitlement[]>
  listEnvironments(): Promise<PortalEnvironment[]>
  listCatalog(): Promise<CatalogEntry[]>
  listCostCenters(): Promise<PortalCostCenter[]>
  getCostCenterSelfServiceSettings(): Promise<CostCenterSelfServiceSettings>
  listMyCostCenters(filter?: { q?: string; cursor?: string }): Promise<CostCenterDirectoryPage>
  searchCostCenters(filter?: { q?: string; cursor?: string }): Promise<CostCenterDirectoryPage>
  getDiscoverableCostCenter(id: string): Promise<MembershipCostCenter>
  listCostCenterMembershipRequests(): Promise<PortalCostCenterMembershipRequest[]>
  getCostCenterMembershipRequest(id: string): Promise<PortalCostCenterMembershipRequest>
  createCostCenterMembershipRequest(payload: CostCenterMembershipRequestCreate): Promise<PortalCostCenterMembershipRequest>
  withdrawCostCenterMembershipRequest(id: string): Promise<PortalCostCenterMembershipRequest>
  listAccessRequests(): Promise<AccessRequest[]>
  getRequestCounts(): Promise<PortalRequestCounts>
  listCostCenterRequests(): Promise<PortalCostCenterRequest[]>
  getCostCenterRequest(requestId: string): Promise<PortalCostCenterRequest>
  createCostCenterRequest(payload: CostCenterRequestCreate): Promise<PortalCostCenterRequest>
  withdrawCostCenterRequest(requestId: string): Promise<PortalCostCenterRequest>
  createAccessRequest(payload: AccessRequestCreate): Promise<AccessRequest>
  withdrawAccessRequest(requestId: string): Promise<AccessRequest>
  getMyUsage(period: UsagePeriod): Promise<MyUsageReport>
  /** Your cost centers whose monthly budgets are near, at, or past their limits. Totals only. */
  getMyBudgets(): Promise<PortalBudgetAlert[]>
  /** Connection metadata for one of the caller's own direct model grants. Contains no secret. */
  getMyEntitlementConnection(entitlementId: string): Promise<ModelConnection>
  /** Connection metadata for one of the caller's own MCP grants. Contains no secret. */
  getMcpConnection(entitlementId: string): Promise<McpConnection>
  /** Reads one current key from APIM. Callers must keep the result out of caches and storage. */
  revealMyEntitlementKey(
    entitlementId: string,
    slot: KeySlot,
    signal?: AbortSignal,
  ): Promise<KeyRevealResult>
  createMyEntitlementKey(entitlementId: string): Promise<GrantKey>
  rotateMyEntitlementKey(entitlementId: string, slot: KeySlot): Promise<GrantKey>
  deleteMyEntitlementKey(entitlementId: string): Promise<GrantKey>
}

export function usePortalApi(): PortalApi {
  const { instance, accounts } = useMsal()

  return useMemo(() => {
    async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
      const headers = new Headers({ Accept: 'application/json' })
      if (options.body !== undefined) {
        headers.set('Content-Type', 'application/json')
      }
      if (options.ifMatch !== undefined) headers.set('If-Match', String(options.ifMatch))
      if (runtimeConfig.authMode === 'entra') {
        const account = accounts[0]
        if (!account) {
          throw new ApiError('No signed-in account is available', 401)
        }
        const token = await instance.acquireTokenSilent({
          account,
          scopes: [runtimeConfig.entraApiScope],
        })
        headers.set('Authorization', `Bearer ${token.accessToken}`)
      }
      const response = await fetch(`${runtimeConfig.apiBaseUrl}${path}`, {
        method: options.method ?? 'GET',
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        headers,
        cache: options.cache,
        signal: options.signal,
      })
      if (!response.ok) {
        let body: ApiErrorBody | undefined
        try {
          body = (await response.json()) as ApiErrorBody
        } catch {
          body = undefined
        }
        throw new ApiError(errorMessage(body, response.status), response.status, body)
      }
      return (await response.json()) as T
    }

    return {
      listNotifications: (filter = {}) => {
        const params = new URLSearchParams()
        if (filter.category) params.set('category', filter.category)
        if (filter.unreadOnly !== undefined) params.set('unread_only', String(filter.unreadOnly))
        if (filter.limit !== undefined) params.set('limit', String(filter.limit))
        if (filter.continuationToken) params.set('continuation_token', filter.continuationToken)
        const query = params.toString()
        return request<NotificationInboxPage>(`/api/v1/me/notifications/inbox${query ? `?${query}` : ''}`, { cache: 'no-store' })
      },
      setNotificationRead: (itemId, read) =>
        request<NotificationInboxItem>(`/api/v1/me/notifications/inbox/${encodeURIComponent(itemId)}`, {
          method: 'PATCH', body: { read }, cache: 'no-store',
        }),
      getNotificationPreferences: () => request<NotificationPreferences>('/api/v1/me/notifications/preferences', { cache: 'no-store' }),
      saveNotificationPreferences: (input, revision) => request<NotificationPreferences>('/api/v1/me/notifications/preferences', {
        method: 'PUT', body: {
          timeZone: input.timeZone, categories: input.categories, digestSchedules: input.digestSchedules,
          quietHours: input.quietHours, thresholds: input.thresholds, repeatCooldownSeconds: input.repeatCooldownSeconds,
        }, cache: 'no-store', ifMatch: revision,
      }),
      getNotificationHealth: () => request<CapabilityCheck[]>('/api/v1/me/notifications/health', { cache: 'no-store' }),
      getNotificationPreferencePreview: () => request<NotificationPreferencePreview>('/api/v1/me/notifications/preferences/preview', { cache: 'no-store' }),
      getPersonalTeamsStatus: () => request<PersonalTeamsStatus>('/api/v1/me/notifications/teams', { cache: 'no-store' }),
      checkPersonalTeamsAccess: () => request<CapabilityCheck[]>('/api/v1/me/notifications/teams/checks', { cache: 'no-store' }),
      testPersonalTeams: () => request<PersonalTeamsTestResult>('/api/v1/me/notifications/teams/test', { method: 'POST', cache: 'no-store' }),
      getProfile: () => request<PortalProfile>('/api/v1/portal/me'),
      listEntitlements: () => request<ResolvedEntitlement[]>('/api/v1/portal/entitlements'),
      listEnvironments: () => request<PortalEnvironment[]>('/api/v1/portal/environments'),
      listCatalog: () => request<CatalogEntry[]>('/api/v1/portal/catalog'),
      listCostCenters: () => request<PortalCostCenter[]>('/api/v1/portal/cost-centers'),
      getCostCenterSelfServiceSettings: () =>
        request<CostCenterSelfServiceSettings>('/api/v1/portal/cost-center-self-service-settings', { cache: 'no-store' }),
      listMyCostCenters: (filter = {}) => request<CostCenterDirectoryPage>(
        costCenterSearchPath('/api/v1/portal/my-cost-centers', filter), { cache: 'no-store' }),
      searchCostCenters: (filter = {}) => request<CostCenterDirectoryPage>(
        costCenterSearchPath('/api/v1/portal/cost-center-directory', filter), { cache: 'no-store' }),
      getDiscoverableCostCenter: (id) => request<MembershipCostCenter>(
        `/api/v1/portal/cost-center-directory/${encodeURIComponent(id)}`, { cache: 'no-store' }),
      listCostCenterMembershipRequests: () =>
        request<PortalCostCenterMembershipRequest[]>('/api/v1/portal/cost-center-membership-requests', { cache: 'no-store' }),
      getCostCenterMembershipRequest: (id) => request<PortalCostCenterMembershipRequest>(
        `/api/v1/portal/cost-center-membership-requests/${encodeURIComponent(id)}`, { cache: 'no-store' }),
      createCostCenterMembershipRequest: (payload) => request<PortalCostCenterMembershipRequest>(
        '/api/v1/portal/cost-center-membership-requests', { method: 'POST', body: payload }),
      withdrawCostCenterMembershipRequest: (id) => request<PortalCostCenterMembershipRequest>(
        `/api/v1/portal/cost-center-membership-requests/${encodeURIComponent(id)}/withdraw`, { method: 'POST' }),
      listAccessRequests: () => request<AccessRequest[]>('/api/v1/portal/access-requests'),
      getRequestCounts: () => request<PortalRequestCounts>('/api/v1/portal/request-counts', { cache: 'no-store' }),
      listCostCenterRequests: () => request<PortalCostCenterRequest[]>('/api/v1/portal/cost-center-requests'),
      getCostCenterRequest: (requestId) =>
        request<PortalCostCenterRequest>(`/api/v1/portal/cost-center-requests/${encodeURIComponent(requestId)}`),
      createCostCenterRequest: (payload) =>
        request<PortalCostCenterRequest>('/api/v1/portal/cost-center-requests', { method: 'POST', body: payload }),
      withdrawCostCenterRequest: (requestId) =>
        request<PortalCostCenterRequest>(`/api/v1/portal/cost-center-requests/${encodeURIComponent(requestId)}/withdraw`, { method: 'POST' }),
      createAccessRequest: (payload) =>
        request<AccessRequest>('/api/v1/portal/access-requests', { method: 'POST', body: payload }),
      withdrawAccessRequest: (requestId) =>
        request<AccessRequest>(`/api/v1/portal/access-requests/${encodeURIComponent(requestId)}/withdraw`, {
          method: 'POST',
        }),
      getMyUsage: (period) =>
        request<MyUsageReport>(`/api/v1/me/usage?period=${encodeURIComponent(period)}`),
      getMyBudgets: () => request<PortalBudgetAlert[]>('/api/v1/me/budgets'),
      getMyEntitlementConnection: (entitlementId) =>
        request<ModelConnection>(`${entitlementPath(entitlementId)}/connection`),
      getMcpConnection: (entitlementId) =>
        request<McpConnection>(`${entitlementPath(entitlementId)}/mcp-connection`),
      revealMyEntitlementKey: (entitlementId, slot, signal) =>
        request<KeyRevealResult>(`${entitlementPath(entitlementId)}/keys/reveal`, {
          method: 'POST',
          body: { slot },
          cache: 'no-store',
          signal,
        }),
      createMyEntitlementKey: (entitlementId) =>
        request<GrantKey>(`${entitlementPath(entitlementId)}/keys`, {
          method: 'POST',
          cache: 'no-store',
        }),
      rotateMyEntitlementKey: (entitlementId, slot) =>
        request<GrantKey>(`${entitlementPath(entitlementId)}/keys/rotate`, {
          method: 'POST',
          body: { slot },
          cache: 'no-store',
        }),
      deleteMyEntitlementKey: (entitlementId) =>
        request<GrantKey>(`${entitlementPath(entitlementId)}/keys`, {
          method: 'DELETE',
          cache: 'no-store',
        }),
    }
  }, [accounts, instance])
}
