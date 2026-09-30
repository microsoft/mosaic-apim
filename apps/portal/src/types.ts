export type QuotaPeriod = 'Hourly' | 'Daily' | 'Weekly' | 'Monthly' | 'Yearly'

export type EnvironmentColor =
  | 'brand'
  | 'danger'
  | 'important'
  | 'informative'
  | 'severe'
  | 'subtle'
  | 'success'
  | 'warning'

export interface PortalEnvironment {
  key: string
  displayName: string
  description: string | null
  color: EnvironmentColor
  production: boolean
  order: number
}

export interface PortalProfile {
  objectId: string
  tenantId: string
  roles: string[]
  isAdmin: boolean
  principalId: string | null
  displayLabel: string | null
  entitlementCount: number
  pendingRequestCount: number
  groupsOverage: boolean
}

export type PortalResourceKind = 'modelApi' | 'mcpServer'

export interface CatalogEntry {
  kind: PortalResourceKind
  id: string
  displayName: string
  summary: string | null
  gatewayId: string
  gatewayName: string | null
  environment: string | null
  entitled: boolean
  requestState: AccessRequestState | null
  enforced?: boolean | null
}

export interface EntitlementResource {
  kind: 'modelApi' | 'mcpServer' | 'modelDeployment' | 'product'
  id: string
  scopeId: string | null
}

export interface AccessRequestCreate {
  resource: EntitlementResource
  justification?: string
}

export interface ResourceSummary {
  kind: EntitlementResource['kind']
  id: string
  scopeId: string | null
  displayName: string | null
  gatewayId: string | null
  gatewayName: string | null
  environment: string | null
  available: boolean
}

export interface AccessRequestResourceSnapshot {
  displayName: string | null
  gatewayId: string | null
  gatewayName: string | null
}

export type AccessRequestState = 'pending' | 'approved' | 'denied' | 'withdrawn'

export interface AccessRequest {
  id: string
  tenantId: string
  entityType: 'accessRequest'
  requesterObjectId: string
  requesterPrincipalId: string | null
  resource: EntitlementResource
  justification: string | null
  state: AccessRequestState
  decidedByObjectId: string | null
  decidedAt: string | null
  decisionNote: string | null
  grantedEntitlementId: string | null
  requestedEnvironment: string | null
  resourceSnapshot: AccessRequestResourceSnapshot | null
  resourceSummary?: ResourceSummary | null
  createdAt: string
  updatedAt: string
  /**
   * The name the catalog shows for the requested resource. Null when the API can't resolve it,
   * for example because the resource was deleted. Older APIs omit it.
   */
  resourceDisplayName?: string | null
}

export interface TokenEnforcement {
  counterKeyExpression: string
  tokensPerMinute: number | null
  tokenQuota: number | null
  tokenQuotaPeriod: QuotaPeriod | null
  estimatePromptTokens: boolean
}

export interface RequestEnforcement {
  counterKeyExpression: string
  calls: number | null
  renewalPeriodSeconds: number | null
  callQuota: number | null
  callQuotaPeriod: QuotaPeriod | null
}

export interface EntitlementEnforcement {
  tokens: TokenEnforcement | null
  requests: RequestEnforcement | null
}

export interface EntitlementBinding {
  gatewayId: string
  apimProductName: string | null
  apimSubscriptionName: string | null
  attributionKey?: string | null
  attributionPerMember?: boolean
  source: 'inferred' | 'manual' | 'orchestrated' | null
}

export interface ModelAccessSettings {
  keysEnabled: boolean
  entraEnabled: boolean
}

export interface EntitlementRuntime {
  publicationId: string
  status: 'pending' | 'applying' | 'applied' | 'revocationPending' | 'revoked' | 'failed' | 'unknown'
  appliedMethods: ModelAccessSettings | null
  subscriptionName: string | null
  appliedAt: string | null
  /**
   * API Management's raw error for the publication's last apply. The portal's routes send null.
   * An older API may still send the text, so it is never shown: `status` explains a failed apply.
   */
  error: string | null
}

export interface Entitlement {
  id: string
  tenantId: string
  entityType: 'entitlement'
  subject: { kind: 'user' | 'group' | 'application' | 'securityGroup'; id: string }
  resource: EntitlementResource
  enabled: boolean
  enforcement: EntitlementEnforcement | null
  binding: EntitlementBinding | null
  runtime?: EntitlementRuntime | null
  notes: string | null
  createdAt: string
  updatedAt: string
}

export interface ResolvedEntitlement {
  entitlement: Entitlement
  resourceSummary: ResourceSummary | null
  via: 'direct' | 'group' | 'securityGroup'
  viaGroupId: string | null
  viaGroupName: string | null
  effective?: boolean
  shadowedBy?: string | null
  /**
   * The name the catalog shows for the granted resource. Null when the API can't resolve it,
   * for example because the resource was deleted. Older APIs omit it.
   */
  resourceDisplayName?: string | null
}

export type UsagePeriod = '7d' | '30d' | '90d'
export type UsageDataSource = 'simulated' | 'logAnalytics'
export type UsageAttribution = 'simulated' | 'measured' | 'unattributed'

export interface UsageTotals {
  requests: number
  promptTokens: number
  completionTokens: number
  totalTokens: number
  estimatedCost: number | null
  costExcludedResources: number
}

export interface UsageTimelinePoint {
  date: string
  entitlementId: string
  environment: string | null
  requests: number | null
  promptTokens: number | null
  completionTokens: number | null
  totalTokens: number | null
  estimatedCost: number | null
}

export interface UsageEnvironmentBreakdown {
  environment: string | null
  resources: number
  requests: number
  promptTokens: number
  completionTokens: number
  totalTokens: number
  estimatedCost: number | null
  costExcludedResources: number
}

export interface UsageQuota {
  metric: 'tokens' | 'requests'
  limit: number
  period: QuotaPeriod
  windowStart: string
  windowEnd: string
  used: number | null
  utilization: number | null
}

export interface UsageRateLimit {
  metric: 'tokens' | 'requests'
  limit: number
  windowSeconds: number
}

export interface UsageResourceRow {
  entitlementId: string
  resource: EntitlementResource
  resourceSummary: ResourceSummary
  environment: string | null
  via: 'direct' | 'group' | 'securityGroup'
  viaGroupName: string | null
  enabled: boolean
  bound: boolean
  linkedBy: 'gatewayLog' | 'subscription' | null
  attribution: UsageAttribution
  model: string | null
  requests: number | null
  promptTokens: number | null
  completionTokens: number | null
  totalTokens: number | null
  estimatedCost: number | null
  costNote: string | null
  quotas: UsageQuota[]
  rateLimits: UsageRateLimit[]
}

export interface MyUsageReport {
  dataSource: UsageDataSource
  period: UsagePeriod
  start: string
  end: string
  generatedAt: string
  currency: 'USD'
  totals: UsageTotals
  timeline: UsageTimelinePoint[]
  byEnvironment: UsageEnvironmentBreakdown[]
  byResource: UsageResourceRow[]
  notes: string[]
}

export interface ConnectionOperation {
  name: string
  method: string
  /** Relative to the connection endpoint. */
  path: string
}

/** The API a published model speaks. Claude models use the Anthropic Messages API (ADR 0012). */
export type ApiShape = 'azureOpenAi' | 'foundryModels' | 'anthropicMessages'

export interface ModelConnection {
  entitlementId: string
  publicationId: string
  gatewayId: string
  endpoint: string
  deploymentName: string
  tenantId: string
  runtime?: EntitlementRuntime | null
  appliedMethods?: ModelAccessSettings | null
  entraAudience?: string | null
  entraScope?: string | null
  /**
   * Public client ID that people sign in with to request a model token. Null when the deployment
   * has no MOSAIC model client or the grant was applied for an earlier runtime registration.
   * Older APIs omit it.
   */
  entraClientId?: string | null
   principalKind?: PrincipalKind | null
   requiredAppRole?: string | null
   entraApplicationScope?: string | null
   keysAvailable?: boolean
   viaGroupId?: string | null
   viaGroupName?: string | null
   subscriptionHeader: string
  /** Older APIs omit it. */
  apiShape?: ApiShape | null
  operations: ConnectionOperation[]
  /** Null when the gateway's tier can't apply token limits to this model's API. */
  publicationLimits: TokenEnforcement | null
  grantLimits?: EntitlementEnforcement | null
}

export type PrincipalKind =
  | 'user'
  | 'servicePrincipal'
  | 'managedIdentity'
  | 'agentIdentity'
  | 'agentUser'
  | 'securityGroup'

export interface McpConnection {
  entitlementId: string
  mcpServerId: string
  publicationId?: string | null
  gatewayId: string
  displayName: string
  tenantId: string
  serverUrl?: string | null
  transport: string
  enforced: boolean
  statusMessage: string
  runtime?: EntitlementRuntime | null
  entraAudience?: string | null
  delegatedScope?: string | null
  applicationScope?: string | null
  requiredAppRole?: string | null
  clientId?: string | null
  principalKind?: PrincipalKind | null
  viaGroupId?: string | null
  viaGroupName?: string | null
  resourceMetadataUrl?: string | null
  limits?: EntitlementEnforcement | null
}

export type KeySlot = 'primary' | 'secondary'

export interface KeyRevealResult {
  entitlementId: string
  subscriptionName: string
  slot: KeySlot
  key: string
}

export interface ApiErrorBody {
  code?: string
  message?: string
  /** FastAPI's own errors carry a string, or a list for request validation failures. */
  detail?: unknown
  details?: Record<string, unknown>
}
