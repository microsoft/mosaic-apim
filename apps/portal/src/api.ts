import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  AccessRequest,
  AccessRequestCreate,
  ApiErrorBody,
  CatalogEntry,
  PortalProfile,
  ResolvedEntitlement,
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
  method?: 'GET' | 'POST'
  body?: unknown
}

export interface PortalApi {
  getProfile(): Promise<PortalProfile>
  listEntitlements(): Promise<ResolvedEntitlement[]>
  listCatalog(): Promise<CatalogEntry[]>
  listAccessRequests(): Promise<AccessRequest[]>
  createAccessRequest(payload: AccessRequestCreate): Promise<AccessRequest>
  withdrawAccessRequest(requestId: string): Promise<AccessRequest>
}

export function usePortalApi(): PortalApi {
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
          body?.message ?? `Request failed with status ${response.status}`,
          response.status,
          body,
        )
      }
      return (await response.json()) as T
    }

    return {
      getProfile: () => request<PortalProfile>('/api/v1/portal/me'),
      listEntitlements: () => request<ResolvedEntitlement[]>('/api/v1/portal/entitlements'),
      listCatalog: () => request<CatalogEntry[]>('/api/v1/portal/catalog'),
      listAccessRequests: () => request<AccessRequest[]>('/api/v1/portal/access-requests'),
      createAccessRequest: (payload) =>
        request<AccessRequest>('/api/v1/portal/access-requests', { method: 'POST', body: payload }),
      withdrawAccessRequest: (requestId) =>
        request<AccessRequest>(`/api/v1/portal/access-requests/${requestId}/withdraw`, {
          method: 'POST',
        }),
    }
  }, [accounts, instance])
}
