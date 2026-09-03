import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  ApiErrorBody,
  Gateway,
  GatewayPolicyView,
  GatewaySuggestion,
  GatewaySyncRun,
  Group,
  GroupMembership,
  McpServer,
  McpServerCandidateList,
  ModelApi,
  ModelApiCandidateList,
  ObservedApi,
  ObservedApimGroup,
  ObservedApimUser,
  ObservedBackend,
  ObservedMcpServer,
  ObservedNamedValue,
  ObservedOperation,
  ObservedProduct,
  ObservedSubscription,
  PolicyPreview,
  Principal,
  PrincipalKind,
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
  previewPolicy(payload: {
    enforcement: TokenEnforcement
    backendResource?: string
  }): Promise<PolicyPreview>
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
      previewPolicy: (payload) =>
        request<PolicyPreview>('/api/v1/policies/preview', {
          method: 'POST',
          body: payload,
        }),
    }
  }, [accounts, instance])
}
