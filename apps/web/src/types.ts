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
  contentSha256: string
  facets: PolicyFacet[]
  unrecognizedElements: string[]
  warnings: string[]
}

export interface ApiErrorBody {
  code?: string
  message?: string
  detail?: string
  details?: Record<string, unknown>
}

export type GatewayStatus =
  | 'pending'
  | 'connected'
  | 'degraded'
  | 'unauthorized'
  | 'unreachable'

export type ManagementMode = 'observe' | 'manage'
export type CapabilitySupport = 'available' | 'unavailable' | 'unknown'
export type AccessEvaluation = 'effectivePermissions' | 'probe' | 'notEvaluated'

export type AiBackendKind =
  | 'azureOpenAi'
  | 'azureAiFoundry'
  | 'azureAiInference'
  | 'openAi'
  | 'anthropic'
  | 'googleVertex'
  | 'awsBedrock'
  | 'otherLlm'
  | 'none'

export type ImportSelection = 'detected' | 'manual'
export type McpTransportType = 'streamable' | 'sse' | 'unknown'
export type McpServerKind = 'restApiBacked' | 'passthrough'

export interface McpEndpoint {
  name: string
  uriTemplate: string
}

export interface McpTool {
  name: string
  displayName: string
  description?: string | null
  backingApiName?: string | null
  backingOperationName?: string | null
}

export type PolicyScope = 'global' | 'product' | 'api' | 'operation'
export type PolicySection = 'inbound' | 'backend' | 'outbound' | 'onError' | 'unknown'
export type FacetConfidence = 'recognized' | 'partial' | 'unrecognized'

export type PolicyFacetKind =
  | 'rateLimit'
  | 'tokenLimit'
  | 'quota'
  | 'authentication'
  | 'authorization'
  | 'routing'
  | 'caching'
  | 'contentSafety'
  | 'transformation'
  | 'observability'
  | 'network'
  | 'fragmentInclude'
  | 'unrecognized'

export interface AccessRemediation {
  roleName: string
  roleDefinitionId: string
  scope: string
  principalId?: string | null
  command: string
}

export interface GatewayAccess {
  canRead: boolean
  canWrite: boolean
  evaluation: AccessEvaluation
  checkedAt?: string | null
  missingActions: string[]
  remediation?: AccessRemediation | null
  message?: string | null
}

export interface GatewayCapabilities {
  skuName?: string | null
  skuCapacity?: number | null
  provisioningState?: string | null
  location?: string | null
  gatewayUrl?: string | null
  managementApiVersion: string
  aiGatewayPolicies: CapabilitySupport
  mcpServers: CapabilitySupport
  notes: string[]
}

export interface GatewayInventorySummary {
  apis: number
  aiApis: number
  mcpServers: number
  operations: number
  products: number
  subscriptions: number
  users: number
  groups: number
  backends: number
  namedValues: number
  policyDocuments: number
  policyFragments: number
  recognizedFacets: number
  unrecognizedFacets: number
  mosaicManagedFacets: number
}

export interface Gateway {
  id: string
  tenantId: string
  name: string
  provider: 'apim'
  azureResourceId: string
  subscriptionId: string
  resourceGroup: string
  serviceName: string
  environmentLabel?: string | null
  managementMode: ManagementMode
  status: GatewayStatus
  access: GatewayAccess
  capabilities: GatewayCapabilities
  inventory: GatewayInventorySummary
  lastSyncedAt?: string | null
  lastSyncError?: string | null
  createdAt: string
  updatedAt: string
}

export type GatewaySyncStatus = 'running' | 'succeeded' | 'partial' | 'failed'

export interface GatewaySyncRun {
  id: string
  tenantId: string
  gatewayId: string
  status: GatewaySyncStatus
  startedAt: string
  completedAt?: string | null
  durationMs?: number | null
  counts: GatewayInventorySummary
  removed: number
  errors: string[]
}

export interface GatewaySuggestion {
  azureResourceId: string
  serviceName: string
  resourceGroup: string
  subscriptionId: string
  alreadyRegistered: boolean
  gatewayId?: string | null
  reason: string
}

export interface ObservedApi {
  id: string
  name: string
  displayName: string
  path: string
  protocols: string[]
  serviceUrl?: string | null
  apiType?: string | null
  apiRevision?: string | null
  apiVersion?: string | null
  isCurrent: boolean
  subscriptionRequired: boolean
  aiKind: AiBackendKind
  aiSignals: string[]
  operationCount: number
  productNames: string[]
}

export interface ObservedOperation {
  id: string
  apiName: string
  name: string
  displayName: string
  method: string
  urlTemplate: string
}

export interface ObservedMcpServer {
  id: string
  name: string
  displayName: string
  path: string
  protocols: string[]
  serviceUrl?: string | null
  kind: McpServerKind
  transportType: McpTransportType
  endpoints: McpEndpoint[]
  tools: McpTool[]
  toolCount: number
  subscriptionRequired: boolean
  productNames: string[]
}

export interface ModelApi {
  id: string
  tenantId: string
  gatewayId: string
  apiName: string
  displayName: string
  path: string
  serviceUrl?: string | null
  protocols: string[]
  aiKind: AiBackendKind
  aiSignals: string[]
  subscriptionRequired: boolean
  operationCount: number
  productNames: string[]
  selection: ImportSelection
  importedFromSnapshotId: string
  importedAt: string
  importedBy?: string | null
  createdAt: string
  updatedAt: string
}

export interface McpServer {
  id: string
  tenantId: string
  gatewayId: string
  apiName: string
  displayName: string
  path: string
  serviceUrl?: string | null
  protocols: string[]
  kind: McpServerKind
  transportType: McpTransportType
  endpoints: McpEndpoint[]
  tools: McpTool[]
  toolCount: number
  subscriptionRequired: boolean
  productNames: string[]
  selection: ImportSelection
  importedFromSnapshotId: string
  importedAt: string
  importedBy?: string | null
  createdAt: string
  updatedAt: string
}

export interface ModelApiCandidate {
  apiName: string
  displayName: string
  path: string
  serviceUrl?: string | null
  aiKind: AiBackendKind
  aiSignals: string[]
  operationCount: number
  productNames: string[]
  recommended: boolean
  alreadyImported: boolean
}

export interface McpServerCandidate {
  apiName: string
  displayName: string
  path: string
  serviceUrl?: string | null
  kind: McpServerKind
  transportType: McpTransportType
  toolCount: number
  recommended: boolean
  alreadyImported: boolean
}

export interface ModelApiCandidateList {
  gatewayId: string
  snapshotId?: string | null
  lastSyncedAt?: string | null
  candidates: ModelApiCandidate[]
}

export interface McpServerCandidateList {
  gatewayId: string
  snapshotId?: string | null
  lastSyncedAt?: string | null
  support: CapabilitySupport
  candidates: McpServerCandidate[]
}

export interface ObservedProduct {
  id: string
  name: string
  displayName: string
  description?: string | null
  state?: string | null
  subscriptionRequired: boolean
  approvalRequired: boolean
  subscriptionsLimit?: number | null
  apiNames: string[]
}

export interface ObservedSubscription {
  id: string
  name: string
  displayName?: string | null
  scope: string
  scopeKind: 'allApis' | 'product' | 'api' | 'unknown'
  scopeName?: string | null
  state?: string | null
  ownerLabel?: string | null
  createdDate?: string | null
}

export interface ObservedApimUser {
  id: string
  name: string
  displayName?: string | null
  email?: string | null
  state?: string | null
  identityProviders: string[]
  entraObjectId?: string | null
  groupNames: string[]
}

export interface ObservedApimGroup {
  id: string
  name: string
  displayName: string
  description?: string | null
  groupType?: string | null
  builtIn: boolean
}

export interface ObservedBackend {
  id: string
  name: string
  title?: string | null
  url?: string | null
  protocol?: string | null
  aiKind: AiBackendKind
}

export interface ObservedNamedValue {
  id: string
  name: string
  displayName: string
  secret: boolean
  tags: string[]
  keyVaultSecretIdentifier?: string | null
}

export interface PolicyFacet {
  kind: PolicyFacetKind
  element: string
  section: PolicySection
  summary: string
  details: string[]
  attributes: Record<string, string>
  confidence: FacetConfidence
  managedByMosaic: boolean
}

export interface ObservedPolicyDocument {
  id: string
  scope: PolicyScope
  scopeId: string
  scopeLabel: string
  contentSha256: string
  elementCount: number
  facets: PolicyFacet[]
  unrecognizedElements: string[]
}

export interface ObservedPolicyFragment {
  id: string
  name: string
  description?: string | null
  contentSha256: string
  managedByMosaic: boolean
  facets: PolicyFacet[]
  unrecognizedElements: string[]
}

export interface GatewayPolicyView {
  documents: ObservedPolicyDocument[]
  fragments: ObservedPolicyFragment[]
  recognizedCount: number
  unrecognizedCount: number
  mosaicManagedCount: number
}
