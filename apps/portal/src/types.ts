export type QuotaPeriod = 'Hourly' | 'Daily' | 'Weekly' | 'Monthly' | 'Yearly'

export interface PortalProfile {
  objectId: string
  tenantId: string
  roles: string[]
  isAdmin: boolean
  principalId: string | null
  displayLabel: string | null
  entitlementCount: number
  pendingRequestCount: number
}

export type PortalResourceKind = 'modelApi' | 'mcpServer'

export interface CatalogEntry {
  kind: PortalResourceKind
  id: string
  displayName: string
  summary: string | null
  gatewayId: string
  gatewayName: string | null
  entitled: boolean
  requestState: AccessRequestState | null
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
  createdAt: string
  updatedAt: string
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
  gatewayId: string | null
  productName: string | null
  subscriptionName: string | null
  source: 'inferred' | 'manual' | 'orchestrated' | null
}

export interface Entitlement {
  id: string
  tenantId: string
  entityType: 'entitlement'
  subject: { kind: 'user' | 'group' | 'application'; id: string }
  resource: EntitlementResource
  enabled: boolean
  enforcement: EntitlementEnforcement | null
  binding: EntitlementBinding | null
  notes: string | null
  createdAt: string
  updatedAt: string
}

export interface ResolvedEntitlement {
  entitlement: Entitlement
  via: 'direct' | 'group'
  viaGroupId: string | null
  viaGroupName: string | null
}

export interface ApiErrorBody {
  code?: string
  message?: string
  details?: Record<string, unknown>
}
