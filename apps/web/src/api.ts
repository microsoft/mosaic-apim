import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  AccessRequest,
  ApiErrorBody,
  CatalogVisibility,
  Entitlement,
  EntitlementBinding,
  EntitlementEnforcement,
  EntitlementResource,
  EntitlementSubject,
  Gateway,
  GatewayPolicyView,
  GatewayRuntimeAccess,
  GatewaySuggestion,
  GatewaySyncRun,
  Group,
  GroupMembership,
  McpAuthMode,
  McpEndpoint,
  McpEndpointSyncRun,
  McpServer,
  McpServerCandidateList,
  ModelApi,
  ModelApiCandidateList,
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
}

export interface MosaicApi {
  listPrincipals(): Promise<Principal[]>
  createPrincipal(payload: {
    objectId: string
    kind: PrincipalKind
    label?: string
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
  listGateways(): Promise<Gateway[]>
  registerGateway(payload: {
    azureResourceId: string
    name?: string
    environmentLabel?: string
  }): Promise<Gateway>
  getGateway(gatewayId: string): Promise<Gateway>
  updateGateway(
    gatewayId: string,
    payload: { name?: string; environmentLabel?: string | null },
  ): Promise<Gateway>
  deleteGateway(gatewayId: string): Promise<void>
  preflightGateway(gatewayId: string): Promise<Gateway>
  syncGateway(gatewayId: string): Promise<GatewaySyncRun>
  getSyncRun(gatewayId: string, runId: string): Promise<GatewaySyncRun>
  listSyncRuns(gatewayId: string): Promise<GatewaySyncRun[]>
  listSuggestedGateways(): Promise<GatewaySuggestion[]>
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
  resolveEntitlements(principalId: string): Promise<ResolvedEntitlement[]>
  listAccessRequests(state?: string): Promise<AccessRequest[]>
  approveAccessRequest(requestId: string, note?: string): Promise<AccessRequest>
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
    async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
      const headers = new Headers({ Accept: 'application/json' })
      if (options.body !== undefined) {
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
      const response = await fetch(`${runtimeConfig.apiBaseUrl}${path}`, {
        method: options.method ?? 'GET',
        body: options.body === undefined ? undefined : JSON.stringify(options.body),
        headers,
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

    return {
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
      resolveEntitlements: (principalId) =>
        request<ResolvedEntitlement[]>(
          `/api/v1/entitlements/resolve?principalId=${encodeURIComponent(principalId)}`,
        ),
      listAccessRequests: (state) =>
        request<AccessRequest[]>(
          `/api/v1/access-requests${state ? `?state=${encodeURIComponent(state)}` : ''}`,
        ),
      approveAccessRequest: (id, note) =>
        request<AccessRequest>(`/api/v1/access-requests/${id}/approve`, {
          method: 'POST',
          body: { note },
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
