import { useMsal } from '@azure/msal-react'
import { useMemo } from 'react'
import { runtimeConfig } from './runtime-config'
import type {
  AccessRequest,
  AccessRequestCreate,
  ApiErrorBody,
  CatalogEntry,
  KeyRevealResult,
  KeySlot,
  McpConnection,
  ModelConnection,
  MyUsageReport,
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
  method?: 'GET' | 'POST'
  body?: unknown
  cache?: RequestCache
  signal?: AbortSignal
}

function errorMessage(body: ApiErrorBody | undefined, status: number) {
  if (body?.message) return body.message
  if (typeof body?.detail === 'string' && body.detail) return body.detail
  return `Request failed with status ${status}`
}

function entitlementPath(entitlementId: string) {
  return `/api/v1/me/entitlements/${encodeURIComponent(entitlementId)}`
}

export interface PortalApi {
  getProfile(): Promise<PortalProfile>
  listEntitlements(): Promise<ResolvedEntitlement[]>
  listEnvironments(): Promise<PortalEnvironment[]>
  listCatalog(): Promise<CatalogEntry[]>
  listAccessRequests(): Promise<AccessRequest[]>
  createAccessRequest(payload: AccessRequestCreate): Promise<AccessRequest>
  withdrawAccessRequest(requestId: string): Promise<AccessRequest>
  getMyUsage(period: UsagePeriod): Promise<MyUsageReport>
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
      getProfile: () => request<PortalProfile>('/api/v1/portal/me'),
      listEntitlements: () => request<ResolvedEntitlement[]>('/api/v1/portal/entitlements'),
      listEnvironments: () => request<PortalEnvironment[]>('/api/v1/portal/environments'),
      listCatalog: () => request<CatalogEntry[]>('/api/v1/portal/catalog'),
      listAccessRequests: () => request<AccessRequest[]>('/api/v1/portal/access-requests'),
      createAccessRequest: (payload) =>
        request<AccessRequest>('/api/v1/portal/access-requests', { method: 'POST', body: payload }),
      withdrawAccessRequest: (requestId) =>
        request<AccessRequest>(`/api/v1/portal/access-requests/${requestId}/withdraw`, {
          method: 'POST',
        }),
      getMyUsage: (period) =>
        request<MyUsageReport>(`/api/v1/me/usage?period=${encodeURIComponent(period)}`),
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
    }
  }, [accounts, instance])
}
