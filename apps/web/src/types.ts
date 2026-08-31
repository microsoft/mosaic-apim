export type PrincipalKind = 'user' | 'servicePrincipal' | 'managedIdentity'

export interface Principal {
  id: string
  tenantId: string
  objectId: string
  kind: PrincipalKind
  label?: string
  createdAt: string
  updatedAt: string
}

export interface Group {
  id: string
  tenantId: string
  name: string
  description?: string
  createdAt: string
  updatedAt: string
}

export interface GroupMembership {
  id: string
  tenantId: string
  groupId: string
  principalId: string
  createdAt: string
  updatedAt: string
}

export interface TokenEnforcement {
  counterKeyExpression: string
  tokensPerMinute?: number
  tokenQuota?: number
  tokenQuotaPeriod?: 'Hourly' | 'Daily' | 'Weekly' | 'Monthly' | 'Yearly'
  estimatePromptTokens: boolean
}

export interface PolicyPreview {
  policyXml: string
  contentSha256: string
  warnings: string[]
}

export interface ApiErrorBody {
  code?: string
  message?: string
  detail?: string
  details?: Record<string, unknown>
}
