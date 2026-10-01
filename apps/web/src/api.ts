import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  AccessRequest,
  AccessRequestApproval,
  AnalyticsConsumers,
  AnalyticsFilters,
  AnalyticsGatewayHealth,
  AnalyticsHygiene,
  AnalyticsLimits,
  AnalyticsModels,
  AnalyticsOverview,
  AnalyticsReliability,
  AnalyticsStatus,
  AnalyticsUnattributed,
  ApiErrorBody,
  CatalogVisibility,
  ConsoleAccess,
  DeclaredDeploymentInput,
  DirectoryMemberPage,
  DirectoryObject,
  DirectorySearchKind,
  DirectoryStatus,
  Entitlement,
  EntitlementBinding,
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementSubject,
  EnvironmentAssignmentRequest,
  EnvironmentAssignmentResult,
  EnvironmentCatalogView,
  EnvironmentCreate,
  EnvironmentFindingList,
  EnvironmentSettingsUpdate,
  EnvironmentSuggestionList,
  EnvironmentUpdate,
  ExportView,
  GrantOverlapReport,
  Gateway,
  GatewayPolicyView,
  GatewayRuntimeAccess,
  GatewaySuggestion,
  GatewaySyncRun,
  GatewayTelemetry,
  Group,
  GroupMembership,
  KeyRevealResult,
  KeySlot,
  ManagementMode,
  McpAuthMode,
  McpConnection,
  McpEndpoint,
  McpEndpointSyncRun,
  McpPublication,
  McpPublicationCreate,
  McpPublicationUpdate,
  McpPublishingCapability,
  McpServer,
  McpServerCandidateList,
  ModelApi,
  ModelApiCandidateList,
  ModelAccessSettings,
  ModelConnection,
  ModelEndpoint,
  ModelEndpointSuggestionView,
  ModelEndpointSyncRun,
  ObservedApi,
  ObservedApimGroup,
  ObservedApimUser,
  ObservedAvailableModel,
  ObservedBackend,
  ObservedMcpServer,
  ObservedMcpTool,
  ObservedModelDeployment,
  ObservedNamedValue,
  ObservedOperation,
  ObservedProduct,
  ObservedSubscription,
  Publication,
  PublicationLockInfo,
  PublishableModel,
  PublishPlan,
  PublishRun,
  PolicyPreview,
  Principal,
  PrincipalKind,
  ResolvedEntitlement,
  TokenEnforcement,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly body?: ApiErrorBody

  constructor(
    message: string,
    status: number,
    body?: ApiErrorBody,
  ) {
    super(message)
    this.status = status
    this.body = body
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
  body?: unknown
  cache?: RequestCache
  signal?: AbortSignal
}

export interface DownloadedFile {
  blob: Blob
  /** The name the API gave the file, or null when it named none. */
  filename: string | null
}

function attachmentName(disposition: string | null) {
  return disposition?.match(/filename="([^"]+)"/)?.[1] ?? null
}

export interface MosaicApi {
  getConsoleAccess(): Promise<ConsoleAccess>
  getAnalyticsStatus(): Promise<AnalyticsStatus>
  refreshAnalytics(): Promise<AnalyticsStatus>
  getAnalyticsOverview(filters?: AnalyticsFilters): Promise<AnalyticsOverview>
  getAnalyticsConsumers(filters?: AnalyticsFilters): Promise<AnalyticsConsumers>
  getAnalyticsModels(filters?: AnalyticsFilters): Promise<AnalyticsModels>
  getAnalyticsReliability(filters?: AnalyticsFilters): Promise<AnalyticsReliability>
  getAnalyticsLimits(filters?: AnalyticsFilters): Promise<AnalyticsLimits>
  getAnalyticsHygiene(filters?: AnalyticsFilters): Promise<AnalyticsHygiene>
  getAnalyticsUnattributed(filters?: AnalyticsFilters): Promise<AnalyticsUnattributed>
  exportAnalytics(view: ExportView, filters?: AnalyticsFilters): Promise<DownloadedFile>
  getEnvironmentCatalog(): Promise<EnvironmentCatalogView>
  createEnvironment(payload: EnvironmentCreate): Promise<EnvironmentCatalogView>
  updateEnvironment(key: string, payload: EnvironmentUpdate): Promise<EnvironmentCatalogView>
  deleteEnvironment(key: string): Promise<EnvironmentCatalogView>
  updateEnvironmentSettings(payload: EnvironmentSettingsUpdate): Promise<EnvironmentCatalogView>
  listEnvironmentSuggestions(): Promise<EnvironmentSuggestionList>
  assignEnvironments(payload: EnvironmentAssignmentRequest): Promise<EnvironmentAssignmentResult>
  listEnvironmentFindings(gatewayId?: string): Promise<EnvironmentFindingList>
  listPrincipals(): Promise<Principal[]>
  createPrincipal(payload: {
    objectId: string
    kind: PrincipalKind
    label?: string
    identityParentId?: string
  }): Promise<Principal>
  updatePrincipal(
    principalId: string,
    payload: { kind?: PrincipalKind; label?: string | null },
  ): Promise<Principal>
  deletePrincipal(principalId: string): Promise<void>
  listGroups(): Promise<Group[]>
  createGroup(payload: { name: string; description?: string }): Promise<Group>
  updateGroup(
    groupId: string,
    payload: { description?: string | null },
  ): Promise<Group>
  deleteGroup(groupId: string): Promise<void>
  listMemberships(groupId: string): Promise<GroupMembership[]>
  addMembership(groupId: string, principalId: string): Promise<GroupMembership>
  removeMembership(groupId: string, principalId: string): Promise<void>
  getDirectoryStatus(): Promise<DirectoryStatus>
  searchDirectory(kind: DirectorySearchKind, query: string, limit?: number): Promise<DirectoryObject[]>
  getDirectoryObject(objectId: string): Promise<DirectoryObject>
  listPrincipalMembers(principalId: string, limit?: number): Promise<DirectoryMemberPage>
  listGateways(): Promise<Gateway[]>
  registerGateway(payload: {
    azureResourceId: string
    name?: string
    environmentLabel?: string
    environment?: string | null
  }): Promise<Gateway>
  getGateway(gatewayId: string): Promise<Gateway>
  getGatewayTelemetry(gatewayId: string): Promise<GatewayTelemetry>
  enableGatewayTelemetry(gatewayId: string): Promise<GatewayTelemetry>
  refreshGatewayTelemetry(gatewayId: string): Promise<AnalyticsGatewayHealth>
  backfillGatewayTelemetry(gatewayId: string, days?: number | null): Promise<AnalyticsGatewayHealth>
  updateGateway(
    gatewayId: string,
    payload: {
      name?: string
      environmentLabel?: string | null
      managementMode?: ManagementMode
    },
  ): Promise<Gateway>
  deleteGateway(gatewayId: string): Promise<void>
  preflightGateway(gatewayId: string): Promise<Gateway>
  syncGateway(gatewayId: string): Promise<GatewaySyncRun>
  getSyncRun(gatewayId: string, runId: string): Promise<GatewaySyncRun>
  listSyncRuns(gatewayId: string): Promise<GatewaySyncRun[]>
  listSuggestedGateways(): Promise<GatewaySuggestion[]>
  listPublishableModels(gatewayId: string): Promise<PublishableModel[]>
  listPublications(gatewayId?: string): Promise<Publication[]>
  createPublication(payload: {
    gatewayId: string
    modelEndpointId: string
    deploymentName: string
    displayName?: string
    apiName?: string
    apiPath?: string
    productName?: string
    subscriptionRequired: boolean
    /** Null exactly when the deployment's `tokenLimitsSupported` is false. */
    enforcement: TokenEnforcement | null
    governedAccess?: ModelAccessSettings | null
  }): Promise<Publication>
  getPublication(publicationId: string): Promise<Publication>
  getPublicationLock(publicationId: string): Promise<PublicationLockInfo>
  updatePublication(
    publicationId: string,
    payload: {
      displayName?: string
      subscriptionRequired?: boolean
      enforcement?: TokenEnforcement
      governedAccess?: ModelAccessSettings | null
    },
  ): Promise<Publication>
  linkPublicationModelApi(publicationId: string): Promise<ModelApi>
  deletePublication(publicationId: string): Promise<void>
  createPublishPlan(publicationId: string): Promise<PublishPlan>
  applyPublishPlan(publicationId: string, planId: string): Promise<PublishRun>
  /** Plans an unpublish for review: what it deletes and whose access it ends. Deletes nothing. */
  planUnpublishPublication(publicationId: string): Promise<PublishPlan>
  /** Runs the reviewed unpublish plan, which the service refuses once it no longer matches. */
  unpublishPublication(publicationId: string, planId: string): Promise<PublishRun>
  listPublishRuns(publicationId: string): Promise<PublishRun[]>
  getPublishRun(publicationId: string, runId: string): Promise<PublishRun>
  diagnosePublicationRecovery(publicationId: string, runId: string): Promise<PublishRun>
  getMcpPublishingCapability(gatewayId: string): Promise<McpPublishingCapability>
  listMcpPublications(gatewayId?: string): Promise<McpPublication[]>
  createMcpPublication(payload: McpPublicationCreate): Promise<McpPublication>
  getMcpPublication(publicationId: string): Promise<McpPublication>
  updateMcpPublication(publicationId: string, payload: McpPublicationUpdate): Promise<McpPublication>
  deleteMcpPublication(publicationId: string): Promise<void>
  planMcpPublication(publicationId: string): Promise<PublishPlan>
  applyMcpPublication(publicationId: string, planId: string): Promise<PublishRun>
  planUnpublishMcpPublication(publicationId: string): Promise<PublishPlan>
  unpublishMcpPublication(publicationId: string, planId: string): Promise<PublishRun>
  listMcpPublishRuns(publicationId: string): Promise<PublishRun[]>
  getMcpPublishRun(publicationId: string, runId: string): Promise<PublishRun>
  recoverMcpPublication(publicationId: string, payload: { runId: string; confirmQuiesced: boolean }): Promise<PublishRun>
  getMcpPublicationLock(publicationId: string): Promise<PublicationLockInfo>
  getMcpPublishPlan(planId: string): Promise<PublishPlan>
  listGatewayApis(gatewayId: string): Promise<ObservedApi[]>
  listGatewayOperations(gatewayId: string, apiName?: string): Promise<ObservedOperation[]>
  listGatewayProducts(gatewayId: string): Promise<ObservedProduct[]>
  listGatewaySubscriptions(gatewayId: string): Promise<ObservedSubscription[]>
  listGatewayUsers(gatewayId: string): Promise<ObservedApimUser[]>
  listGatewayGroups(gatewayId: string): Promise<ObservedApimGroup[]>
  listGatewayBackends(gatewayId: string): Promise<ObservedBackend[]>
  listGatewayNamedValues(gatewayId: string): Promise<ObservedNamedValue[]>
  getGatewayPolicies(gatewayId: string): Promise<GatewayPolicyView>
  listGatewayMcpServers(gatewayId: string): Promise<ObservedMcpServer[]>
  listImportableApis(gatewayId: string): Promise<ModelApiCandidateList>
  listImportableMcpServers(gatewayId: string): Promise<McpServerCandidateList>
  importModelApis(gatewayId: string, apiNames: string[]): Promise<ModelApi[]>
  importMcpServers(gatewayId: string, apiNames: string[]): Promise<McpServer[]>
  listModelApis(gatewayId?: string): Promise<ModelApi[]>
  listMcpServers(gatewayId?: string): Promise<McpServer[]>
  deleteModelApi(modelApiId: string): Promise<void>
  deleteMcpServer(mcpServerId: string): Promise<void>
  updateModelApiCatalog(
    modelApiId: string,
    payload: { visibility?: CatalogVisibility; summary?: string | null },
  ): Promise<ModelApi>
  updateMcpServerCatalog(
    mcpServerId: string,
    payload: { visibility?: CatalogVisibility; summary?: string | null },
  ): Promise<McpServer>
  listEntitlements(filters?: { subject?: string; resource?: string }): Promise<Entitlement[]>
  createEntitlement(payload: {
    subject: EntitlementSubject
    resource: EntitlementResource
    enabled?: boolean
    enforcement?: EntitlementEnforcement | null
    binding?: EntitlementBinding | null
    notes?: string | null
  }): Promise<Entitlement>
  updateEntitlement(
    entitlementId: string,
    payload: {
      enabled?: boolean
      enforcement?: EntitlementEnforcement | null
      binding?: EntitlementBinding | null
      notes?: string | null
    },
  ): Promise<Entitlement>
  deleteEntitlement(entitlementId: string): Promise<void>
  getEntitlementConnection(entitlementId: string): Promise<ModelConnection>
  getMcpConnection(entitlementId: string): Promise<McpConnection>
  revealEntitlementKey(entitlementId: string, slot: KeySlot, signal?: AbortSignal): Promise<KeyRevealResult>
  listMyEntitlements(): Promise<Entitlement[]>
  getMyEntitlementConnection(entitlementId: string): Promise<ModelConnection>
  revealMyEntitlementKey(entitlementId: string, slot: KeySlot, signal?: AbortSignal): Promise<KeyRevealResult>
  resolveEntitlements(principalId: string): Promise<ResolvedEntitlement[]>
  getGrantOverlaps(filters?: { resource?: string }): Promise<GrantOverlapReport>
  listAccessRequests(state?: string): Promise<AccessRequest[]>
  approveAccessRequest(requestId: string, approval: AccessRequestApproval): Promise<AccessRequest>
  denyAccessRequest(requestId: string, note?: string): Promise<AccessRequest>
  previewPolicy(payload: {
    enforcement: TokenEnforcement
    backendResource?: string
  }): Promise<PolicyPreview>
  listModelEndpoints(): Promise<ModelEndpoint[]>
  registerModelEndpoint(payload: {
    azureResourceId?: string
    endpoint?: string
    name?: string
    environmentLabel?: string
    credentialSecretUri?: string
    environment?: string | null
    deployments?: DeclaredDeploymentInput[]
  }): Promise<ModelEndpoint>
  getModelEndpoint(endpointId: string): Promise<ModelEndpoint>
  updateModelEndpoint(
    endpointId: string,
    payload: {
      name?: string
      environmentLabel?: string | null
      credentialSecretUri?: string
    },
  ): Promise<ModelEndpoint>
  deleteModelEndpoint(endpointId: string): Promise<void>
  preflightModelEndpoint(endpointId: string): Promise<ModelEndpoint>
  syncModelEndpoint(endpointId: string): Promise<ModelEndpointSyncRun>
  listModelEndpointSyncRuns(endpointId: string): Promise<ModelEndpointSyncRun[]>
  listModelDeployments(endpointId: string): Promise<ObservedModelDeployment[]>
  declareModelDeployment(
    endpointId: string,
    payload: DeclaredDeploymentInput,
  ): Promise<ModelEndpoint>
  removeDeclaredModelDeployment(endpointId: string, deploymentName: string): Promise<ModelEndpoint>
  listAvailableModels(endpointId: string): Promise<ObservedAvailableModel[]>
  getModelEndpointRuntimeAccess(endpointId: string): Promise<GatewayRuntimeAccess[]>
  listSuggestedModelEndpoints(): Promise<ModelEndpointSuggestionView>
  listMcpEndpoints(): Promise<McpEndpoint[]>
  registerMcpEndpoint(payload: {
    endpoint: string
    name?: string
    environmentLabel?: string
    authMode?: McpAuthMode
    credentialSecretUri?: string
    resourceAudience?: string
    environment?: string | null
  }): Promise<McpEndpoint>
  getMcpEndpoint(endpointId: string): Promise<McpEndpoint>
  updateMcpEndpoint(
    endpointId: string,
    payload: {
      name?: string
      environmentLabel?: string | null
      credentialSecretUri?: string
      resourceAudience?: string
    },
  ): Promise<McpEndpoint>
  deleteMcpEndpoint(endpointId: string): Promise<void>
  preflightMcpEndpoint(endpointId: string): Promise<McpEndpoint>
  syncMcpEndpoint(endpointId: string): Promise<McpEndpointSyncRun>
  listMcpEndpointSyncRuns(endpointId: string): Promise<McpEndpointSyncRun[]>
  listMcpEndpointTools(endpointId: string): Promise<ObservedMcpTool[]>
}

export function useMosaicApi(): MosaicApi {
  const { instance, accounts } = useMsal()

  return useMemo(() => {
    async function requestHeaders(hasBody: boolean): Promise<Headers> {
      const headers = new Headers({ Accept: 'application/json' })
      if (hasBody) {
        headers.set('Content-Type', 'application/json')
      }
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
      return headers
    }

    async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
      const headers = await requestHeaders(options.body !== undefined)
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
        throw new ApiError(
          body?.message ?? body?.detail ?? `Request failed with status ${response.status}`,
          response.status,
          body,
        )
      }
      if (response.status === 204) {
        return undefined as T
      }
      return (await response.json()) as T
    }

    async function requestBlob(path: string, options: RequestOptions = {}): Promise<DownloadedFile> {
      const headers = await requestHeaders(options.body !== undefined)
      headers.set('Accept', 'text/csv')
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
        throw new ApiError(
          body?.message ?? body?.detail ?? `Request failed with status ${response.status}`,
          response.status,
          body,
        )
      }
      return {
        blob: await response.blob(),
        filename: attachmentName(response.headers.get('Content-Disposition')),
      }
    }

    function analyticsQuery(filters?: AnalyticsFilters, extra?: Record<string, string | undefined>) {
      const params = new URLSearchParams()
      const range = filters?.range ?? '30d'
      params.set('range', range)
      if (range === 'custom') {
        if (filters?.start) params.set('start', filters.start)
        if (filters?.end) params.set('end', filters.end)
      }
      if (filters?.gatewayId) params.set('gatewayId', filters.gatewayId)
      if (filters?.environment) params.set('environment', filters.environment)
      if (filters?.resourceId) params.set('resourceId', filters.resourceId)
      if (filters?.subjectKind) params.set('subjectKind', filters.subjectKind)
      for (const [key, value] of Object.entries(extra ?? {})) {
        if (value) params.set(key, value)
      }
      const query = params.toString()
      return query ? `?${query}` : ''
    }

    return {
      getConsoleAccess: () => request<ConsoleAccess>('/api/v1/console/me'),
      getAnalyticsStatus: () => request<AnalyticsStatus>('/api/v1/analytics/status'),
      refreshAnalytics: () =>
        request<AnalyticsStatus>('/api/v1/analytics/refresh', { method: 'POST' }),
      getAnalyticsOverview: (filters) =>
        request<AnalyticsOverview>(`/api/v1/analytics/overview${analyticsQuery(filters)}`),
      getAnalyticsConsumers: (filters) =>
        request<AnalyticsConsumers>(`/api/v1/analytics/consumers${analyticsQuery(filters)}`),
      getAnalyticsModels: (filters) =>
        request<AnalyticsModels>(`/api/v1/analytics/models${analyticsQuery(filters)}`),
      getAnalyticsReliability: (filters) =>
        request<AnalyticsReliability>(`/api/v1/analytics/reliability${analyticsQuery(filters)}`),
      getAnalyticsLimits: (filters) =>
        request<AnalyticsLimits>(`/api/v1/analytics/limits${analyticsQuery(filters)}`),
      getAnalyticsHygiene: (filters) =>
        request<AnalyticsHygiene>(`/api/v1/analytics/hygiene${analyticsQuery(filters)}`),
      getAnalyticsUnattributed: (filters) =>
        request<AnalyticsUnattributed>(`/api/v1/analytics/unattributed${analyticsQuery(filters)}`),
      exportAnalytics: (view, filters) =>
        requestBlob(`/api/v1/analytics/export${analyticsQuery(filters, { view })}`),
      getEnvironmentCatalog: () => request<EnvironmentCatalogView>('/api/v1/environment-catalog'),
      createEnvironment: (payload) =>
        request<EnvironmentCatalogView>('/api/v1/environment-catalog/environments', {
          method: 'POST',
          body: payload,
        }),
      updateEnvironment: (key, payload) =>
        request<EnvironmentCatalogView>(
          `/api/v1/environment-catalog/environments/${encodeURIComponent(key)}`,
          { method: 'PATCH', body: payload },
        ),
      deleteEnvironment: (key) =>
        request<EnvironmentCatalogView>(
          `/api/v1/environment-catalog/environments/${encodeURIComponent(key)}`,
          { method: 'DELETE' },
        ),
      updateEnvironmentSettings: (payload) =>
        request<EnvironmentCatalogView>('/api/v1/environment-catalog/settings', {
          method: 'PATCH',
          body: payload,
        }),
      listEnvironmentSuggestions: () =>
        request<EnvironmentSuggestionList>('/api/v1/environment-suggestions'),
      assignEnvironments: (payload) =>
        request<EnvironmentAssignmentResult>('/api/v1/environment-assignments', {
          method: 'POST',
          body: payload,
        }),
      listEnvironmentFindings: (gatewayId) =>
        request<EnvironmentFindingList>(
          `/api/v1/environment-findings${gatewayId ? `?gatewayId=${encodeURIComponent(gatewayId)}` : ''}`,
        ),
      listPrincipals: () => request<Principal[]>('/api/v1/principals'),
      createPrincipal: (payload) =>
        request<Principal>('/api/v1/principals', { method: 'POST', body: payload }),
      updatePrincipal: (id, payload) =>
        request<Principal>(`/api/v1/principals/${id}`, {
          method: 'PATCH',
          body: payload,
        }),
      deletePrincipal: (id) =>
        request<void>(`/api/v1/principals/${id}`, { method: 'DELETE' }),
      listGroups: () => request<Group[]>('/api/v1/groups'),
      createGroup: (payload) =>
        request<Group>('/api/v1/groups', { method: 'POST', body: payload }),
      updateGroup: (id, payload) =>
        request<Group>(`/api/v1/groups/${id}`, { method: 'PATCH', body: payload }),
      deleteGroup: (id) => request<void>(`/api/v1/groups/${id}`, { method: 'DELETE' }),
      listMemberships: (groupId) =>
        request<GroupMembership[]>(`/api/v1/groups/${groupId}/members`),
      addMembership: (groupId, principalId) =>
        request<GroupMembership>(`/api/v1/groups/${groupId}/members/${principalId}`, {
          method: 'PUT',
        }),
      removeMembership: (groupId, principalId) =>
        request<void>(`/api/v1/groups/${groupId}/members/${principalId}`, {
          method: 'DELETE',
        }),
      getDirectoryStatus: () => request<DirectoryStatus>('/api/v1/directory/status'),
      searchDirectory: (kind, query, limit = 20) =>
        request<DirectoryObject[]>(
          `/api/v1/directory/search?kind=${encodeURIComponent(kind)}&q=${encodeURIComponent(query)}&limit=${encodeURIComponent(String(limit))}`,
        ),
      getDirectoryObject: (objectId) =>
        request<DirectoryObject>(`/api/v1/directory/objects/${encodeURIComponent(objectId)}`),
      listPrincipalMembers: (principalId, limit = 200) =>
        request<DirectoryMemberPage>(
          `/api/v1/principals/${encodeURIComponent(principalId)}/members?limit=${encodeURIComponent(String(limit))}`,
        ),
      listGateways: () => request<Gateway[]>('/api/v1/gateways'),
      registerGateway: (payload) =>
        request<Gateway>('/api/v1/gateways', { method: 'POST', body: payload }),
      getGateway: (id) => request<Gateway>(`/api/v1/gateways/${id}`),
      updateGateway: (id, payload) =>
        request<Gateway>(`/api/v1/gateways/${id}`, { method: 'PATCH', body: payload }),
      deleteGateway: (id) => request<void>(`/api/v1/gateways/${id}`, { method: 'DELETE' }),
      preflightGateway: (id) =>
        request<Gateway>(`/api/v1/gateways/${id}/preflight`, { method: 'POST' }),
      syncGateway: (id) =>
        request<GatewaySyncRun>(`/api/v1/gateways/${id}/sync`, { method: 'POST' }),
      getSyncRun: (id, runId) =>
        request<GatewaySyncRun>(`/api/v1/gateways/${id}/sync-runs/${runId}`),
      listSyncRuns: (id) => request<GatewaySyncRun[]>(`/api/v1/gateways/${id}/sync-runs`),
      listSuggestedGateways: () =>
        request<GatewaySuggestion[]>('/api/v1/gateways/suggested'),
      getGatewayTelemetry: (id) =>
        request<GatewayTelemetry>(`/api/v1/gateways/${encodeURIComponent(id)}/telemetry`),
      enableGatewayTelemetry: (id) =>
        request<GatewayTelemetry>(`/api/v1/gateways/${encodeURIComponent(id)}/telemetry/enable`, {
          method: 'POST',
        }),
      refreshGatewayTelemetry: (id) =>
        request<AnalyticsGatewayHealth>(
          `/api/v1/gateways/${encodeURIComponent(id)}/telemetry/refresh`,
          { method: 'POST' },
        ),
      backfillGatewayTelemetry: (id, days) =>
        request<AnalyticsGatewayHealth>(
          `/api/v1/gateways/${encodeURIComponent(id)}/telemetry/backfill`,
          { method: 'POST', body: { days } },
        ),
      listPublishableModels: (id) =>
        request<PublishableModel[]>(`/api/v1/gateways/${id}/publishable-models`),
      listPublications: (gatewayId) =>
        request<Publication[]>(
          `/api/v1/publications${gatewayId ? `?gateway=${encodeURIComponent(gatewayId)}` : ''}`,
        ),
      createPublication: (payload) =>
        request<Publication>('/api/v1/publications', { method: 'POST', body: payload }),
      getPublication: (id) => request<Publication>(`/api/v1/publications/${id}`),
      getPublicationLock: (id) =>
        request<PublicationLockInfo>(`/api/v1/publications/${encodeURIComponent(id)}/lock`),
      updatePublication: (id, payload) =>
        request<Publication>(`/api/v1/publications/${id}`, { method: 'PATCH', body: payload }),
      linkPublicationModelApi: (id) =>
        request<ModelApi>(`/api/v1/publications/${id}/model-api`, { method: 'POST' }),
      deletePublication: (id) =>
        request<void>(`/api/v1/publications/${id}`, { method: 'DELETE' }),
      createPublishPlan: (id) =>
        request<PublishPlan>(`/api/v1/publications/${id}/plan`, { method: 'POST' }),
      applyPublishPlan: (id, planId) =>
        request<PublishRun>(`/api/v1/publications/${id}/apply?plan=${encodeURIComponent(planId)}`, {
          method: 'POST',
        }),
      unpublishPublication: (id, planId) =>
        request<PublishRun>(
          `/api/v1/publications/${id}/unpublish?plan=${encodeURIComponent(planId)}`,
          { method: 'POST' },
        ),
      planUnpublishPublication: (id) =>
        request<PublishPlan>(`/api/v1/publications/${id}/unpublish-plan`, { method: 'POST' }),
      listPublishRuns: (id) => request<PublishRun[]>(`/api/v1/publications/${id}/runs`),
      getPublishRun: (id, runId) =>
        request<PublishRun>(`/api/v1/publications/${id}/runs/${runId}`),
      diagnosePublicationRecovery: (id, runId) =>
        request<PublishRun>(`/api/v1/publications/${id}/recover`, {
          method: 'POST',
          body: { runId, confirmQuiesced: false },
        }),
      getMcpPublishingCapability: (gatewayId) =>
        request<McpPublishingCapability>(
          `/api/v1/gateways/${encodeURIComponent(gatewayId)}/mcp-publishing`,
        ),
      listMcpPublications: (gatewayId) =>
        request<McpPublication[]>(
          `/api/v1/mcp-publications${gatewayId ? `?gateway=${encodeURIComponent(gatewayId)}` : ''}`,
        ),
      createMcpPublication: (payload) =>
        request<McpPublication>('/api/v1/mcp-publications', { method: 'POST', body: payload }),
      getMcpPublication: (id) =>
        request<McpPublication>(`/api/v1/mcp-publications/${encodeURIComponent(id)}`),
      updateMcpPublication: (id, payload) =>
        request<McpPublication>(`/api/v1/mcp-publications/${encodeURIComponent(id)}`, {
          method: 'PATCH',
          body: payload,
        }),
      deleteMcpPublication: (id) =>
        request<void>(`/api/v1/mcp-publications/${encodeURIComponent(id)}`, {
          method: 'DELETE',
        }),
      planMcpPublication: (id) =>
        request<PublishPlan>(`/api/v1/mcp-publications/${encodeURIComponent(id)}/plan`, {
          method: 'POST',
        }),
      applyMcpPublication: (id, planId) =>
        request<PublishRun>(
          `/api/v1/mcp-publications/${encodeURIComponent(id)}/apply?plan=${encodeURIComponent(planId)}`,
          { method: 'POST' },
        ),
      unpublishMcpPublication: (id, planId) =>
        request<PublishRun>(
          `/api/v1/mcp-publications/${encodeURIComponent(id)}/unpublish?plan=${encodeURIComponent(planId)}`,
          { method: 'POST' },
        ),
      planUnpublishMcpPublication: (id) =>
        request<PublishPlan>(`/api/v1/mcp-publications/${encodeURIComponent(id)}/unpublish-plan`, {
          method: 'POST',
        }),
      listMcpPublishRuns: (id) =>
        request<PublishRun[]>(`/api/v1/mcp-publications/${encodeURIComponent(id)}/runs`),
      getMcpPublishRun: (id, runId) =>
        request<PublishRun>(
          `/api/v1/mcp-publications/${encodeURIComponent(id)}/runs/${encodeURIComponent(runId)}`,
        ),
      recoverMcpPublication: (id, payload) =>
        request<PublishRun>(`/api/v1/mcp-publications/${encodeURIComponent(id)}/recover`, {
          method: 'POST',
          body: payload,
        }),
      getMcpPublicationLock: (id) =>
        request<PublicationLockInfo>(`/api/v1/mcp-publications/${encodeURIComponent(id)}/lock`),
      getMcpPublishPlan: (planId) =>
        request<PublishPlan>(`/api/v1/mcp-publish-plans/${encodeURIComponent(planId)}`),
      listGatewayApis: (id) => request<ObservedApi[]>(`/api/v1/gateways/${id}/apis`),
      listGatewayOperations: (id, apiName) =>
        request<ObservedOperation[]>(
          `/api/v1/gateways/${id}/operations${apiName ? `?api=${encodeURIComponent(apiName)}` : ''}`,
        ),
      listGatewayProducts: (id) => request<ObservedProduct[]>(`/api/v1/gateways/${id}/products`),
      listGatewaySubscriptions: (id) =>
        request<ObservedSubscription[]>(`/api/v1/gateways/${id}/subscriptions`),
      listGatewayUsers: (id) => request<ObservedApimUser[]>(`/api/v1/gateways/${id}/users`),
      listGatewayGroups: (id) => request<ObservedApimGroup[]>(`/api/v1/gateways/${id}/groups`),
      listGatewayBackends: (id) => request<ObservedBackend[]>(`/api/v1/gateways/${id}/backends`),
      listGatewayNamedValues: (id) =>
        request<ObservedNamedValue[]>(`/api/v1/gateways/${id}/named-values`),
      getGatewayPolicies: (id) => request<GatewayPolicyView>(`/api/v1/gateways/${id}/policies`),
      listGatewayMcpServers: (id) =>
        request<ObservedMcpServer[]>(`/api/v1/gateways/${id}/mcp-servers`),
      listImportableApis: (id) =>
        request<ModelApiCandidateList>(`/api/v1/gateways/${id}/importable-apis`),
      listImportableMcpServers: (id) =>
        request<McpServerCandidateList>(`/api/v1/gateways/${id}/importable-mcp-servers`),
      importModelApis: (id, apiNames) =>
        request<ModelApi[]>(`/api/v1/gateways/${id}/import-apis`, {
          method: 'POST',
          body: { apiNames },
        }),
      importMcpServers: (id, apiNames) =>
        request<McpServer[]>(`/api/v1/gateways/${id}/import-mcp-servers`, {
          method: 'POST',
          body: { apiNames },
        }),
      listModelApis: (gatewayId) =>
        request<ModelApi[]>(
          `/api/v1/model-apis${gatewayId ? `?gateway=${encodeURIComponent(gatewayId)}` : ''}`,
        ),
      listMcpServers: (gatewayId) =>
        request<McpServer[]>(
          `/api/v1/mcp-servers${gatewayId ? `?gateway=${encodeURIComponent(gatewayId)}` : ''}`,
        ),
      deleteModelApi: (id) => request<void>(`/api/v1/model-apis/${id}`, { method: 'DELETE' }),
      deleteMcpServer: (id) => request<void>(`/api/v1/mcp-servers/${id}`, { method: 'DELETE' }),
      updateModelApiCatalog: (id, payload) =>
        request<ModelApi>(`/api/v1/model-apis/${id}/catalog`, {
          method: 'PATCH',
          body: payload,
        }),
      updateMcpServerCatalog: (id, payload) =>
        request<McpServer>(`/api/v1/mcp-servers/${id}/catalog`, {
          method: 'PATCH',
          body: payload,
        }),
      listEntitlements: (filters) => {
        const params = new URLSearchParams()
        if (filters?.subject) {
          params.set('subject', filters.subject)
        }
        if (filters?.resource) {
          params.set('resource', filters.resource)
        }
        const query = params.toString()
        return request<Entitlement[]>(`/api/v1/entitlements${query ? `?${query}` : ''}`)
      },
      createEntitlement: (payload) =>
        request<Entitlement>('/api/v1/entitlements', { method: 'POST', body: payload }),
      updateEntitlement: (id, payload) =>
        request<Entitlement>(`/api/v1/entitlements/${id}`, { method: 'PATCH', body: payload }),
      deleteEntitlement: (id) =>
        request<void>(`/api/v1/entitlements/${id}`, { method: 'DELETE' }),
      getEntitlementConnection: (id) =>
        request<ModelConnection>(`/api/v1/entitlements/${id}/connection`),
      getMcpConnection: (id) =>
        request<McpConnection>(`/api/v1/entitlements/${encodeURIComponent(id)}/mcp-connection`),
      revealEntitlementKey: (id, slot, signal) =>
        request<KeyRevealResult>(`/api/v1/entitlements/${id}/keys/reveal`, {
          method: 'POST', body: { slot }, cache: 'no-store', signal,
        }),
      listMyEntitlements: () => request<Entitlement[]>('/api/v1/me/entitlements'),
      getMyEntitlementConnection: (id) =>
        request<ModelConnection>(`/api/v1/me/entitlements/${id}/connection`),
      revealMyEntitlementKey: (id, slot, signal) =>
        request<KeyRevealResult>(`/api/v1/me/entitlements/${id}/keys/reveal`, {
          method: 'POST', body: { slot }, cache: 'no-store', signal,
        }),
      resolveEntitlements: (principalId) =>
        request<ResolvedEntitlement[]>(
          `/api/v1/entitlements/resolve?principalId=${encodeURIComponent(principalId)}`,
        ),
      getGrantOverlaps: (filters) =>
        request<GrantOverlapReport>(
          `/api/v1/entitlements/overlaps${filters?.resource ? `?resource=${encodeURIComponent(filters.resource)}` : ''}`,
        ),
      listAccessRequests: (state) =>
        request<AccessRequest[]>(
          `/api/v1/access-requests${state ? `?state=${encodeURIComponent(state)}` : ''}`,
        ),
      approveAccessRequest: (id, approval) =>
        request<AccessRequest>(`/api/v1/access-requests/${id}/approve`, {
          method: 'POST',
          body: approval,
        }),
      denyAccessRequest: (id, note) =>
        request<AccessRequest>(`/api/v1/access-requests/${id}/deny`, {
          method: 'POST',
          body: { note },
        }),
      previewPolicy: (payload) =>
        request<PolicyPreview>('/api/v1/policies/preview', {
          method: 'POST',
          body: payload,
        }),
      listModelEndpoints: () => request<ModelEndpoint[]>('/api/v1/model-endpoints'),
      registerModelEndpoint: (payload) =>
        request<ModelEndpoint>('/api/v1/model-endpoints', { method: 'POST', body: payload }),
      getModelEndpoint: (id) => request<ModelEndpoint>(`/api/v1/model-endpoints/${id}`),
      updateModelEndpoint: (id, payload) =>
        request<ModelEndpoint>(`/api/v1/model-endpoints/${id}`, {
          method: 'PATCH',
          body: payload,
        }),
      deleteModelEndpoint: (id) =>
        request<void>(`/api/v1/model-endpoints/${id}`, { method: 'DELETE' }),
      preflightModelEndpoint: (id) =>
        request<ModelEndpoint>(`/api/v1/model-endpoints/${id}/preflight`, { method: 'POST' }),
      syncModelEndpoint: (id) =>
        request<ModelEndpointSyncRun>(`/api/v1/model-endpoints/${id}/sync`, { method: 'POST' }),
      listModelEndpointSyncRuns: (id) =>
        request<ModelEndpointSyncRun[]>(`/api/v1/model-endpoints/${id}/sync-runs`),
      listModelDeployments: (id) =>
        request<ObservedModelDeployment[]>(`/api/v1/model-endpoints/${id}/deployments`),
      declareModelDeployment: (id, payload) =>
        request<ModelEndpoint>(`/api/v1/model-endpoints/${id}/declared-deployments`, {
          method: 'POST',
          body: payload,
        }),
      removeDeclaredModelDeployment: (id, deploymentName) =>
        request<ModelEndpoint>(
          `/api/v1/model-endpoints/${id}/declared-deployments/${encodeURIComponent(deploymentName)}`,
          { method: 'DELETE' },
        ),
      listAvailableModels: (id) =>
        request<ObservedAvailableModel[]>(`/api/v1/model-endpoints/${id}/available-models`),
      getModelEndpointRuntimeAccess: (id) =>
        request<GatewayRuntimeAccess[]>(`/api/v1/model-endpoints/${id}/runtime-access`),
      listSuggestedModelEndpoints: () =>
        request<ModelEndpointSuggestionView>('/api/v1/model-endpoints/suggested'),
      listMcpEndpoints: () => request<McpEndpoint[]>('/api/v1/mcp-endpoints'),
      registerMcpEndpoint: (payload) =>
        request<McpEndpoint>('/api/v1/mcp-endpoints', { method: 'POST', body: payload }),
      getMcpEndpoint: (id) => request<McpEndpoint>(`/api/v1/mcp-endpoints/${id}`),
      updateMcpEndpoint: (id, payload) =>
        request<McpEndpoint>(`/api/v1/mcp-endpoints/${id}`, { method: 'PATCH', body: payload }),
      deleteMcpEndpoint: (id) =>
        request<void>(`/api/v1/mcp-endpoints/${id}`, { method: 'DELETE' }),
      preflightMcpEndpoint: (id) =>
        request<McpEndpoint>(`/api/v1/mcp-endpoints/${id}/preflight`, { method: 'POST' }),
      syncMcpEndpoint: (id) =>
        request<McpEndpointSyncRun>(`/api/v1/mcp-endpoints/${id}/sync`, { method: 'POST' }),
      listMcpEndpointSyncRuns: (id) =>
        request<McpEndpointSyncRun[]>(`/api/v1/mcp-endpoints/${id}/sync-runs`),
      listMcpEndpointTools: (id) =>
        request<ObservedMcpTool[]>(`/api/v1/mcp-endpoints/${id}/tools`),
    }
  }, [accounts, instance])
}
