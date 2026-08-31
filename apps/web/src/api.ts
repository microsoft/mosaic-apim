import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  ApiErrorBody,
  Group,
  GroupMembership,
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
      previewPolicy: (payload) =>
        request<PolicyPreview>('/api/v1/policies/preview', {
          method: 'POST',
          body: payload,
        }),
    }
  }, [accounts, instance])
}
