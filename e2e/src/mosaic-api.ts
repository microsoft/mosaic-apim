import type { BrowserContext, Request } from '@playwright/test'
import { type AppName, type SuiteModel, type Targets, modelLabel, persona } from './config.ts'
import type { PersonaPool } from './fixtures.ts'
import type { ScopePlan, ScopePublication, ScopeResource, ScopeRun } from './plan-scope.ts'
import { done, pending, pollUntil } from './poll.ts'
import { bearerToken, tokenClaims } from './verify.ts'

/**
 * A read-only view of MOSAIC's API as a signed-in persona sees it, for checking what the UI shows and for
 * finding the records a journey acts on. It borrows the MOSAIC API token the persona's own app sends, and it
 * only ever sends GETs: every change the suite makes goes through the UI, like a person's would. Errors name
 * the path and status, never the token or a response body.
 */

export interface ApiPrincipal {
  id: string
  objectId: string
  kind: string
  label?: string | null
  detail?: string | null
}

export interface ApiRuntimeAccess {
  gatewayId: string
  gatewayName: string
  canInvoke: boolean
  evaluation: string
  reason?: string | null
}

export interface ApiEndpoint {
  id: string
  name: string
  endpoint: string
  azureResourceId?: string | null
  status: string
  /** Whether MOSAIC can read the endpoint, and the command that grants it when it can't. */
  access: { canRead: boolean; remediation?: { command: string } | null }
  runtimeAccess: ApiRuntimeAccess[]
}

export interface ApiDeployment {
  deploymentName: string
}

export interface ApiSuggestion {
  azureResourceId?: string | null
  alreadyRegistered: boolean
}

export interface ApiScanIssue {
  subscriptionId: string
  remediation?: { command: string } | null
}

export interface ApiSuggestions {
  suggestions: ApiSuggestion[]
  scanIssues: ApiScanIssue[]
  partialScans: ApiScanIssue[]
  /** The commands that let MOSAIC see subscriptions, shown when it can see or list none. */
  scanRemediation?: { scope: string; command: string }[] | null
  subscriptionsScanned: number
  scanStatus: string
}

export interface ApiGateway {
  id: string
  name: string
  serviceName: string
  azureResourceId: string
  managementMode: 'observe' | 'manage'
  access: { canRead: boolean; canWrite: boolean }
}

export interface AccessMethods {
  keysEnabled: boolean
  entraEnabled: boolean
}

export interface ApiPublishedResource extends ScopeResource {
  createdByMosaic: boolean
}

export interface ApiPublication extends ScopePublication {
  modelEndpointId: string
  deploymentName: string
  displayName: string
  apiPath: string
  status: 'draft' | 'planned' | 'applying' | 'published' | 'failed' | 'rolledBack'
  resources: ApiPublishedResource[]
  lastAppliedAt: string | null
  lastRunId: string | null
  governedAccess?: AccessMethods | null
  appliedAccess?: {
    settings: AccessMethods
    audience?: string | null
    publicationEnforcement?: unknown
    grants: {
      entitlementId: string
      enabled: boolean
      subscriptionName?: string | null
      intentDigest?: string
      costCenterId?: string
      costCenterCode?: string
      defaultCostCenter?: boolean
    }[]
    pools?: { costCenterId: string; monthlyTokens: number | null; monthlyCalls: number | null }[]
  } | null
  accessState: 'pending' | 'applying' | 'applied' | 'failed' | 'unknown'
}

export interface ApiModelApi {
  id: string
  gatewayId: string
  apiName: string
  displayName: string
  path?: string | null
  visibility: 'catalog' | 'private'
  publicationId?: string | null
}

export interface ApiEnforcement {
  tokens?: { tokensPerMinute?: number | null; tokenQuota?: number | null; tokenQuotaPeriod?: string | null } | null
  requests?: {
    calls?: number | null
    renewalPeriodSeconds?: number | null
    callQuota?: number | null
    callQuotaPeriod?: string | null
  } | null
}

export interface ApiEntitlement {
  id: string
  subject: { kind: string; id: string }
  resource: { kind: string; id: string }
  enabled: boolean
  enforcement?: ApiEnforcement | null
  notes?: string | null
  runtime?: {
    publicationId: string
    status: 'pending' | 'applying' | 'applied' | 'revocationPending' | 'revoked' | 'failed' | 'unknown'
    appliedMethods?: AccessMethods | null
    subscriptionName?: string | null
  } | null
}

export interface ApiAccessRequest {
  id: string
  requesterObjectId: string
  requesterPrincipalId?: string | null
  justification?: string | null
  resource: { kind: string; id: string }
  state: 'pending' | 'approved' | 'denied' | 'withdrawn'
  decisionNote?: string | null
  grantedEntitlementId?: string | null
}

export interface ApiPlan extends ScopePlan {
  id: string
  warnings: string[]
}

export interface ApiPublishableModel {
  modelEndpointId: string
  endpointName: string
  deploymentName: string
  publishable: boolean
  unpublishableReason?: string | null
  publicationId?: string | null
  publicationStatus?: ApiPublication['status'] | null
  suggestedApiName: string
  suggestedApiPath: string
  runtimeAccess?: { canInvoke: boolean } | null
  /** The publish dialog hides a deployment whose environment pairing is blocked. */
  environmentVerdict?: { level: 'allowed' | 'warning' | 'blocked'; reason?: string } | null
}

export interface ApiEnvironment {
  key: string
  displayName: string
  production: boolean
  builtIn: boolean
}

/** A grant as the portal resolves it for the signed-in person. */
export interface ApiResolvedEntitlement {
  entitlement: ApiEntitlement
  via: 'direct' | 'group' | 'securityGroup'
  effective?: boolean
  resourceDisplayName?: string | null
  resourceSummary?: { displayName?: string | null; available?: boolean } | null
}

export interface ApiCatalogEntry {
  kind: string
  id: string
  displayName: string
  gatewayName: string | null
  entitled: boolean
  requestState: ApiAccessRequest['state'] | null
}

export interface ApiUsageReport {
  dataSource: 'simulated' | 'logAnalytics'
  byResource: { entitlementId: string; enabled: boolean }[]
}

export interface ApiRun extends ScopeRun {
  id: string
  publicationId: string
  planId: string
  errors: string[]
}

export class MosaicApiError extends Error {
  readonly status: number

  constructor(path: string, status: number) {
    super(`MOSAIC's API answered ${status} to GET ${path}.`)
    this.name = 'MosaicApiError'
    this.status = status
  }
}

interface Tapped {
  token?: string
  expiresAt?: number
}

const taps = new WeakMap<BrowserContext, Tapped>()

/**
 * Keeps the newest MOSAIC API token a persona's browser sends. The token stays in memory; it is never
 * logged, attached or returned except to the verifier's environment.
 */
function tap(context: BrowserContext, apiOrigin: string): Tapped {
  let tapped = taps.get(context)
  if (!tapped) {
    const state: Tapped = {}
    tapped = state
    taps.set(context, state)
    context.on('request', (request: Request) => {
      if (!request.url().startsWith(`${apiOrigin}/api/`)) return
      void request
        .headerValue('authorization')
        .then((header) => {
          const token = bearerToken(header)
          const expiry = token === undefined ? undefined : tokenClaims(token)?.exp
          if (token !== undefined && typeof expiry === 'number' && (state.expiresAt === undefined || expiry >= state.expiresAt)) {
            state.token = token
            state.expiresAt = expiry
          }
        })
        .catch(() => undefined)
    })
  }
  return tapped
}

/** The app a persona signs in to: the console for admins, the portal for everyone else. */
export function homeApp(targets: Targets, personaKey: string): { app: AppName; path: string } {
  return persona(targets, personaKey).expectedRole === 'Admin' ? { app: 'web', path: '/models' } : { app: 'portal', path: '/access' }
}

/**
 * A MOSAIC API token for the persona that stays valid for at least minSeconds. If the newest token the
 * persona's browser sent won't last, it opens a new tab: a new tab starts with empty session storage, where
 * MSAL keeps its cache, so the app signs in again and gets a token with its full lifetime.
 */
export async function personaApiToken(
  personas: PersonaPool,
  targets: Targets,
  personaKey: string,
  minSeconds = 300,
): Promise<string> {
  const context = await personas.context(personaKey)
  const tapped = tap(context, targets.origins.api)
  const lasts = () => tapped.token !== undefined && (tapped.expiresAt ?? 0) - Date.now() / 1_000 >= minSeconds
  if (lasts()) return tapped.token as string
  const { app, path } = homeApp(targets, personaKey)
  await personas.page(personaKey, app, path)
  return pollUntil(
    `${personaKey}'s ${app} app to call the MOSAIC API with a token that lasts ${minSeconds} seconds`,
    () => (lasts() ? done(tapped.token as string) : pending()),
    { timeoutMs: 60_000, intervalMs: 500 },
  )
}

const lower = (value: string | null | undefined) => (value ?? '').toLowerCase()

/**
 * The workload's MOSAIC identity among the principals: by its service principal's object ID when the manifest
 * gives one, since an admin may label it anything, or else by a label equal to its app registration's name.
 * People and security groups are never the workload.
 */
export function findWorkloadPrincipal(
  principals: readonly ApiPrincipal[],
  workload: NonNullable<Targets['workload']>,
): ApiPrincipal | undefined {
  const candidates = principals.filter((principal) => principal.kind !== 'user' && principal.kind !== 'securityGroup')
  if (workload.objectId) return candidates.find((principal) => lower(principal.objectId) === lower(workload.objectId))
  return candidates.find((principal) => lower(principal.label) === lower(workload.displayName))
}

export class MosaicApi {
  readonly #personas: PersonaPool
  readonly #targets: Targets
  readonly #personaKey: string

  private constructor(personas: PersonaPool, targets: Targets, personaKey: string) {
    this.#personas = personas
    this.#targets = targets
    this.#personaKey = personaKey
  }

  /** A reader that sees what personaKey may see. It signs the persona in if their browser has no token yet. */
  static async as(personas: PersonaPool, targets: Targets, personaKey: string): Promise<MosaicApi> {
    await personaApiToken(personas, targets, personaKey, 120)
    return new MosaicApi(personas, targets, personaKey)
  }

  async get<T>(path: string): Promise<T> {
    const context = await this.#personas.context(this.#personaKey)
    const url = new URL(path, this.#targets.origins.api).href
    for (let attempt = 1; ; attempt += 1) {
      const token = await personaApiToken(this.#personas, this.#targets, this.#personaKey, attempt === 1 ? 120 : 600)
      const response = await context.request.get(url, {
        headers: { Accept: 'application/json', Authorization: `Bearer ${token}` },
        failOnStatusCode: false,
        timeout: 60_000,
      })
      if (response.ok()) return (await response.json()) as T
      if (response.status() !== 401 || attempt > 1) throw new MosaicApiError(new URL(url).pathname, response.status())
    }
  }

  gateways(): Promise<ApiGateway[]> {
    return this.get('/api/v1/gateways')
  }

  /** The suite's gateway, by its MOSAIC name or its API Management service name. */
  async gateway(name: string): Promise<ApiGateway | undefined> {
    return (await this.gateways()).find((gateway) => lower(gateway.name) === lower(name) || lower(gateway.serviceName) === lower(name))
  }

  endpoints(): Promise<ApiEndpoint[]> {
    return this.get('/api/v1/model-endpoints')
  }

  /** The MOSAIC endpoints registered for a manifest endpoint, by account or project resource ID. */
  async endpointsFor(endpointKey: string): Promise<ApiEndpoint[]> {
    const target = this.#targets.endpoints[endpointKey]
    const ids = new Set([lower(target.resourceId), lower(target.projectResourceId)].filter((id) => id !== ''))
    return (await this.endpoints()).filter((endpoint) => ids.has(lower(endpoint.azureResourceId)))
  }

  deployments(endpointId: string): Promise<ApiDeployment[]> {
    return this.get(`/api/v1/model-endpoints/${encodeURIComponent(endpointId)}/deployments`)
  }

  suggestions(): Promise<ApiSuggestions> {
    return this.get('/api/v1/model-endpoints/suggested')
  }

  publications(): Promise<ApiPublication[]> {
    return this.get('/api/v1/publications')
  }

  publication(id: string): Promise<ApiPublication> {
    return this.get(`/api/v1/publications/${encodeURIComponent(id)}`)
  }

  /** The publication of a manifest model on a gateway, or undefined if MOSAIC has none. */
  async publicationOf(model: SuiteModel, gatewayId: string, all?: ApiPublication[]): Promise<ApiPublication | undefined> {
    const endpointIds = new Set((await this.endpointsFor(model.endpoint)).map((endpoint) => endpoint.id))
    const matches = (all ?? (await this.publications())).filter(
      (publication) =>
        endpointIds.has(publication.modelEndpointId) && publication.deploymentName === model.deployment && publication.gatewayId === gatewayId,
    )
    if (matches.length > 1) throw new Error(`MOSAIC has ${matches.length} publications of ${modelLabel(model)} on one gateway.`)
    return matches[0]
  }

  plan(id: string): Promise<ApiPlan> {
    return this.get(`/api/v1/publish-plans/${encodeURIComponent(id)}`)
  }

  /** The deployments the publish dialog offers for a gateway. */
  publishableModels(gatewayId: string): Promise<ApiPublishableModel[]> {
    return this.get(`/api/v1/gateways/${encodeURIComponent(gatewayId)}/publishable-models`)
  }

  /** The publish dialog's row for a manifest model on a gateway, if the dialog offers it. */
  async publishableModel(model: SuiteModel, gatewayId: string): Promise<ApiPublishableModel | undefined> {
    const endpointIds = new Set((await this.endpointsFor(model.endpoint)).map((endpoint) => endpoint.id))
    const rows = (await this.publishableModels(gatewayId)).filter(
      (row) => endpointIds.has(row.modelEndpointId) && row.deploymentName === model.deployment,
    )
    if (rows.length > 1) throw new Error(`The publish dialog offers ${modelLabel(model)} ${rows.length} times on one gateway.`)
    return rows[0]
  }

  run(publicationId: string, runId: string): Promise<ApiRun> {
    return this.get(`/api/v1/publications/${encodeURIComponent(publicationId)}/runs/${encodeURIComponent(runId)}`)
  }

  runs(publicationId: string): Promise<ApiRun[]> {
    return this.get(`/api/v1/publications/${encodeURIComponent(publicationId)}/runs`)
  }

  modelApis(): Promise<ApiModelApi[]> {
    return this.get('/api/v1/model-apis')
  }

  /** The model API a publication materialized, if it still exists. */
  async modelApiOf(publication: Pick<ApiPublication, 'id' | 'modelApiId' | 'gatewayId' | 'apiName'>): Promise<ApiModelApi | undefined> {
    const apis = await this.modelApis()
    return (
      apis.find((api) => publication.modelApiId && api.id === publication.modelApiId) ??
      apis.find((api) => api.publicationId === publication.id) ??
      apis.find((api) => api.gatewayId === publication.gatewayId && lower(api.apiName) === lower(publication.apiName))
    )
  }

  principals(): Promise<ApiPrincipal[]> {
    return this.get('/api/v1/principals')
  }

  async principalByObjectId(objectId: string): Promise<ApiPrincipal | undefined> {
    return (await this.principals()).find((principal) => lower(principal.objectId) === lower(objectId))
  }

  /** The workload's principal, by the display name in the manifest. */
  async workloadPrincipal(): Promise<ApiPrincipal | undefined> {
    const workload = this.#targets.workload
    if (!workload) return undefined
    return findWorkloadPrincipal(await this.principals(), workload)
  }

  entitlements(filter: { subject?: string; resource?: string } = {}): Promise<ApiEntitlement[]> {
    const query = new URLSearchParams()
    if (filter.subject) query.set('subject', filter.subject)
    if (filter.resource) query.set('resource', filter.resource)
    const suffix = query.size > 0 ? `?${query}` : ''
    return this.get(`/api/v1/entitlements${suffix}`)
  }

  entitlement(id: string): Promise<ApiEntitlement> {
    return this.get(`/api/v1/entitlements/${encodeURIComponent(id)}`)
  }

  /** The direct grants a principal holds on a model API. */
  async grantsOf(principalId: string, modelApiId: string): Promise<ApiEntitlement[]> {
    return (await this.entitlements({ resource: modelApiId })).filter(
      (grant) => grant.subject.id === principalId && grant.resource.kind === 'modelApi' && grant.resource.id === modelApiId,
    )
  }

  accessRequests(state?: ApiAccessRequest['state']): Promise<ApiAccessRequest[]> {
    return this.get(`/api/v1/access-requests${state ? `?state=${state}` : ''}`)
  }

  async environments(): Promise<ApiEnvironment[]> {
    return (await this.get<{ environments: ApiEnvironment[] }>('/api/v1/environment-catalog')).environments
  }

  /** The persona's own grants, as the portal's My access page reads them. */
  portalEntitlements(): Promise<ApiResolvedEntitlement[]> {
    return this.get('/api/v1/portal/entitlements')
  }

  portalCatalog(): Promise<ApiCatalogEntry[]> {
    return this.get('/api/v1/portal/catalog')
  }

  /** The persona's own access requests, as the portal's My requests page reads them. */
  portalAccessRequests(): Promise<ApiAccessRequest[]> {
    return this.get('/api/v1/portal/access-requests')
  }

  myUsage(): Promise<ApiUsageReport> {
    return this.get('/api/v1/me/usage?period=30d')
  }
}

/**
 * A persona's Entra object ID: from the manifest, or else the oid claim of the MOSAIC API token their browser
 * sends, which signs them in if needed.
 */
export async function personaObjectId(personas: PersonaPool, targets: Targets, personaKey: string): Promise<string> {
  const known = persona(targets, personaKey).objectId
  if (known) return known
  const oid = tokenClaims(await personaApiToken(personas, targets, personaKey, 60))?.oid
  if (typeof oid !== 'string') throw new Error(`The MOSAIC API token ${personaKey}'s browser sent has no object ID.`)
  return oid
}
