export type PrincipalKind =
  | 'user'
  | 'servicePrincipal'
  | 'managedIdentity'
  | 'agentIdentity'
  | 'agentUser'
  | 'securityGroup'

/** The caller's MOSAIC roles, from `GET /api/v1/console/me`. Only a caller with a MOSAIC role gets
 * one, so `isAdmin` false means the caller holds the User role alone. */
export interface ConsoleAccess {
  roles: string[]
  isAdmin: boolean
}

export interface Principal {
  id: string
  tenantId: string
  objectId: string
  kind: PrincipalKind
  label?: string
  detail?: string | null
  identityParentId?: string | null
  blueprintId?: string | null
  defaultCostCenterId?: string | null
  directoryVerifiedAt?: string | null
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
  tokenQuotaPeriod?: QuotaPeriod
  estimatePromptTokens: boolean
}

export type QuotaPeriod = 'Hourly' | 'Daily' | 'Weekly' | 'Monthly' | 'Yearly'

export interface CostCenterRef {
  id: string
  name: string
  code: string
}

export interface PersonLimits {
  tokensPerMinute: number | null
  tokenQuota: number | null
  tokenQuotaPeriod: QuotaPeriod | null
  callsPerMinute: number | null
  callQuota: number | null
  callQuotaPeriod: QuotaPeriod | null
}

export interface PooledQuota {
  monthlyTokens: number | null
  monthlyCalls: number | null
}

export interface CostCenterLimit {
  resource: { kind: 'modelApi' | 'mcpServer'; id: string; scopeId?: string | null }
  person: PersonLimits | null
  pool: PooledQuota | null
}

export interface CostCenterMember {
  principalId: string
  addedAt: string
  addedBy: string | null
}

export interface CostCenterMemberView {
  principalId: string
  objectId: string | null
  label: string | null
  kind: PrincipalKind | null
  explicit: boolean
  isDefault: boolean
  addedAt: string | null
}

export interface CostCenter {
  id: string
  tenantId: string
  entityType: 'costCenter'
  name: string
  code: string
  description: string | null
  owners: string[]
  members: CostCenterMember[]
  keysAllowed: boolean
  limits: CostCenterLimit[]
  builtIn: boolean
  createdAt: string
  updatedAt: string
  isTenantDefault: boolean
  memberDetails: CostCenterMemberView[]
  grantCount: number
  enabledGrantCount: number
  defaultFor: number
}

export interface CostCenterSettings {
  id: string
  tenantId: string
  defaultCostCenterId: string
  createdAt?: string
  updatedAt?: string
}

export interface GrantKey {
  entitlementId: string
  subscriptionName: string
  exists: boolean
  costCenter: CostCenterRef | null
  rotated: KeySlot | null
}

export interface GrantRevocation {
  reason: 'costCenterMembership'
  costCenterId: string
  revokedAt: string
  revokedBy: string
}

export type CatalogVisibility = 'catalog' | 'private'

export interface RequestEnforcement {
  counterKeyExpression: string
  calls?: number
  renewalPeriodSeconds?: number
  callQuota?: number
  callQuotaPeriod?: QuotaPeriod
}

export interface EntitlementEnforcement {
  tokens?: TokenEnforcement | null
  requests?: RequestEnforcement | null
}

export type EntitlementSubjectKind = 'user' | 'group' | 'application' | 'securityGroup'

export type EntitlementResourceKind =
  | 'modelApi'
  | 'mcpServer'
  | 'modelDeployment'
  | 'product'

export interface EntitlementSubject {
  kind: EntitlementSubjectKind
  id: string
}

export interface EntitlementResource {
  kind: EntitlementResourceKind
  id: string
  scopeId?: string | null
}


export type EnvironmentColor =
  'brand' | 'danger' | 'important' | 'informative' | 'severe' | 'subtle' | 'success' | 'warning'
export type EnvironmentResourceKind = 'gateway' | 'modelEndpoint' | 'mcpEndpoint'
export type VerdictLevel = 'allowed' | 'warning' | 'blocked'

export interface EnvironmentDefinition {
  key: string
  displayName: string
  description?: string | null
  color: EnvironmentColor
  production: boolean
  aliases: string[]
  acceptsEndpointsFrom: string[]
  order: number
  builtIn: boolean
}

export interface EnvironmentUsage {
  gateways: number
  modelEndpoints: number
  mcpEndpoints: number
}

export interface EnvironmentWithUsage extends EnvironmentDefinition {
  usage: EnvironmentUsage
}

export interface EnvironmentVerdict {
  level: VerdictLevel
  reason: string
  gatewayEnvironment: string | null
  endpointEnvironment: string | null
  viaException: boolean
}

export interface EnvironmentCompatibilityCell extends EnvironmentVerdict {}

export interface EnvironmentCatalogView {
  environments: EnvironmentWithUsage[]
  requireClassification: boolean
  unclassified: EnvironmentUsage
  compatibility: EnvironmentCompatibilityCell[]
  updatedAt?: string | null
}

export interface EnvironmentCreate {
  key: string
  displayName: string
  description?: string | null
  color: EnvironmentColor
  production?: boolean
  aliases?: string[]
  acceptsEndpointsFrom?: string[]
  order?: number
}

export interface EnvironmentUpdate {
  displayName?: string
  description?: string | null
  color?: EnvironmentColor
  production?: boolean
  aliases?: string[]
  acceptsEndpointsFrom?: string[]
  order?: number
}

export interface EnvironmentSettingsUpdate {
  requireClassification: boolean
}

export interface EnvironmentSuggestion {
  resourceKind: EnvironmentResourceKind
  resourceId: string
  resourceName: string
  suggestedEnvironment: string | null
  source: 'azureTag' | 'legacyLabel' | null
  evidence: string | null
}

export interface EnvironmentSuggestionList {
  items: EnvironmentSuggestion[]
}

export interface EnvironmentAssignment {
  resourceKind: EnvironmentResourceKind
  resourceId: string
  environment: string | null
}

export interface EnvironmentAssignmentRequest {
  assignments: EnvironmentAssignment[]
  acknowledgeGrants?: boolean
}

export interface EnvironmentAssignmentOutcome extends EnvironmentAssignment {
  resourceName: string
  previousEnvironment: string | null
  status: 'applied' | 'unchanged' | 'failed'
  message?: string | null
}

export interface EnvironmentAssignmentResult {
  results: EnvironmentAssignmentOutcome[]
  grantsCarried: number
  warnings: string[]
}

export interface BlockedPublication {
  kind?: 'model' | 'mcp'
  publicationId: string
  displayName?: string | null
  status: string
  gatewayId: string
  gatewayName: string
  gatewayEnvironment: string | null
  modelEndpointId?: string | null
  modelEndpointName?: string | null
  deploymentName?: string | null
  mcpEndpointId?: string | null
  mcpEndpointName?: string | null
  endpointEnvironment: string | null
  verdict: EnvironmentVerdict
}

export interface PublicationsBlockedDetails {
  reason: 'publicationsBlocked'
  publications: BlockedPublication[]
  suggestedAssignments: EnvironmentAssignment[]
}

export interface AffectedGrant {
  entitlementId: string
  subject: EntitlementSubject
  subjectLabel?: string | null
  resource: EntitlementResource
  resourceName?: string | null
  movedResource: {
    resourceKind: EnvironmentResourceKind
    resourceId: string
    resourceName: string
  }
  fromEnvironment: string | null
  toEnvironment: string | null
}

export interface GrantsAcknowledgmentRequiredDetails {
  reason: 'grantsAcknowledgmentRequired'
  grants: AffectedGrant[]
  grantCount: number
  principalCount: number
  truncated: boolean
}

export interface EnvironmentInUseDetails {
  reason: 'environmentInUse'
  environment: string
  usage: EnvironmentUsage
  resources: Array<{ resourceKind: EnvironmentResourceKind; resourceId: string; resourceName: string }>
  referencedBy: string[]
}

export interface EnvironmentChangedDetails {
  reason: 'environmentChanged'
  requestedEnvironment: string | null
  currentEnvironment: string | null
}

export interface EnvironmentFinding {
  id: string
  kind:
    | 'blockedPublication'
    | 'backendCrossesEnvironments'
    | 'apiCrossesEnvironments'
    | 'mcpServerCrossesEnvironments'
  confidence: 'certain' | 'high' | 'medium'
  gatewayId: string
  gatewayName: string
  gatewayEnvironment: string | null
  subject: {
    kind: 'publication' | 'backend' | 'api' | 'mcpServer'
    id: string
    name: string
    apiName: string | null
  }
  target: {
    resourceKind: EnvironmentResourceKind
    resourceId: string
    resourceName: string
    environment: string | null
  }
  verdict: EnvironmentVerdict
  evidence: string
  message: string
}

export interface EnvironmentFindingList {
  items: EnvironmentFinding[]
  limitations: string[]
  generatedAt: string
}

export interface ResourceSummary {
  kind: EntitlementResourceKind
  id: string
  scopeId?: string | null
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

export type BindingSource = 'inferred' | 'manual' | 'orchestrated'

export interface EntitlementBinding {
  gatewayId: string
  apimProductName?: string | null
  apimSubscriptionName?: string | null
  attributionKey?: string | null
  attributionPerMember?: boolean
  counterKeyExpression?: string | null
  source: BindingSource
  boundAt?: string | null
}

export interface ModelAccessSettings {
  keysEnabled: boolean
  entraEnabled: boolean
}

export interface EntitlementRuntime {
  publicationId: string
  status: 'pending' | 'applying' | 'applied' | 'revocationPending' | 'revoked' | 'failed' | 'unknown'
  appliedMethods?: ModelAccessSettings | null
  subscriptionName?: string | null
  keyExists?: boolean
  appliedAt?: string | null
  error?: string | null
}

export interface Entitlement {
  id: string
  tenantId: string
  subject: EntitlementSubject
  resource: EntitlementResource
  costCenterId?: string
  costCenter?: CostCenterRef | null
  revocation?: GrantRevocation | null
  enabled: boolean
  enforcement?: EntitlementEnforcement | null
  binding?: EntitlementBinding | null
  notes?: string | null
  runtime?: EntitlementRuntime | null
  createdAt: string
  updatedAt: string
}

export interface ResolvedEntitlement {
  entitlement: Entitlement
  via: 'direct' | 'group' | 'securityGroup'
  viaGroupId?: string | null
  viaGroupName?: string | null
  costCenter?: CostCenterRef | null
  effective: boolean
  shadowedBy?: string | null
}

export type AccessRequestState = 'pending' | 'approved' | 'denied' | 'withdrawn'

export interface AccessRequest {
  id: string
  tenantId: string
  requesterObjectId: string
  requesterPrincipalId?: string | null
  resource: EntitlementResource
  costCenterId?: string | null
  costCenter?: CostCenterRef | null
  costCenterGroupIds?: string[]
  requestedEnvironment?: string | null
  resourceSnapshot?: AccessRequestResourceSnapshot | null
  resourceSummary?: ResourceSummary | null
  justification?: string | null
  state: AccessRequestState
  decidedByObjectId?: string | null
  decidedAt?: string | null
  decisionNote?: string | null
  grantedEntitlementId?: string | null
  createdAt: string
  updatedAt: string
}

/** Approving creates the requester's grant intent with these limits, never an APIM change. */
export interface AccessRequestApproval {
  note?: string | null
  enforcement?: EntitlementEnforcement | null
  confirmedEnvironment?: string
  costCenterId?: string
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

/** One named URI template an API Management MCP server is reachable on. */
export interface McpServerRoute {
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
  customRoleDefinition?: Record<string, unknown> | null
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
  principalId?: string | null
  identityObserved: boolean
  /** `None`, `External`, or `Internal`; absent when MOSAIC has not recorded it. */
  virtualNetworkType?: string | null
  /** Where the gateway's calls to a public endpoint come from; empty when not deterministic. */
  egressIpAddresses?: string[]
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
  environment: string | null
  azureEnvironmentTag?: string | null
  /** @deprecated Use environment. Legacy display-only data. */
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
  azureEnvironmentTag?: string | null
  suggestedEnvironment?: string | null
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
  endpoints: McpServerRoute[]
  tools: McpTool[]
  toolCount: number
  subscriptionRequired: boolean
  productNames: string[]
}

interface ModelApiDetails {
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
  visibility: CatalogVisibility
  summary?: string | null
  selection: ImportSelection
  importedAt: string
  importedBy?: string | null
  createdAt: string
  updatedAt: string
}

export type ModelApi = ModelApiDetails & (
  | { importedFromSnapshotId: string; publicationId?: string | null }
  | { importedFromSnapshotId: null; publicationId: string }
)

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
  endpoints: McpServerRoute[]
  tools: McpTool[]
  toolCount: number
  subscriptionRequired: boolean
  productNames: string[]
  visibility: CatalogVisibility
  summary?: string | null
  selection: ImportSelection
  importedFromSnapshotId?: string | null
  publicationId?: string | null
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

export type ModelProvider = 'azureOpenAi' | 'azureAiFoundry' | 'openAiCompatible'
export type EndpointAuthMode = 'managedIdentity' | 'apiKey'
export type ModelEndpointStatus =
  | 'pending'
  | 'connected'
  | 'degraded'
  | 'unauthorized'
  | 'unreachable'

/**
 * How MOSAIC established whether a gateway can invoke an endpoint.
 *
 * `notEvaluated` is deliberately distinct from a negative answer: MOSAIC not being able to read
 * role assignments is not the same as the gateway lacking the role. It is also used whenever
 * something MOSAIC cannot evaluate stands in the way, such as an ABAC condition.
 */
export type RuntimeAccessEvaluation =
  | 'roleAssignments'
  | 'noGatewayIdentity'
  | 'notApplicable'
  | 'notEvaluated'

/** What the runtime check found. Absent on results recorded before it was introduced. */
export type RuntimeAccessReason =
  | 'granted'
  | 'missingRole'
  | 'narrowerScope'
  | 'conditional'
  | 'roleUnreadable'
  | 'denyAssignment'
  | 'networkUnreachable'
  | 'networkUnverified'
  | 'assignmentsUnreadable'
  | 'noGatewayIdentity'
  | 'identityNotObserved'

export type RuntimeRoleFindingKind =
  | 'sufficient'
  | 'insufficient'
  | 'narrowerScope'
  | 'conditional'
  | 'unreadable'

/** One of the gateway's role assignments, and what it does for the published API. */
export interface RuntimeRoleFinding {
  kind: RuntimeRoleFindingKind
  roleName?: string | null
  roleDefinitionId?: string | null
  scope: string
  inherited: boolean
  missingDataActions: string[]
}

export type NetworkReachability = 'reachable' | 'unreachable' | 'unverified' | 'unknown'

export type SuggestionSource = 'bootstrap' | 'gatewayBackend' | 'subscriptionScan'

/** Whether MOSAIC's own identity can enumerate models on an endpoint. */
export interface EndpointAccess {
  canRead: boolean
  evaluation: AccessEvaluation
  checkedAt?: string | null
  missingActions: string[]
  remediation?: AccessRemediation | null
  message?: string | null
}

/** Whether one gateway's managed identity can call an endpoint at runtime. */
export interface GatewayRuntimeAccess {
  gatewayId: string
  gatewayName: string
  apimPrincipalId?: string | null
  canInvoke: boolean
  evaluation: RuntimeAccessEvaluation
  reason?: RuntimeAccessReason | null
  checkedAt?: string | null
  /** The role MOSAIC recommends. Any role covering `requiredDataActions` is accepted. */
  requiredRoleName?: string | null
  requiredRoleDefinitionId?: string | null
  /** The role that satisfied the check, which need not be the recommended one. */
  grantedRoleName?: string | null
  grantedRoleDefinitionId?: string | null
  assignmentScope?: string | null
  inherited: boolean
  /** The scope the published API calls: always the account, even for a Foundry project. */
  evaluatedScope?: string | null
  requiredDataActions?: string[]
  roleFindings?: RuntimeRoleFinding[]
  networkReachability?: NetworkReachability
  remediation?: AccessRemediation | null
  message?: string | null
}

export interface ModelEndpointCapabilities {
  kind?: string | null
  skuName?: string | null
  location?: string | null
  provisioningState?: string | null
  publicNetworkAccess?: string | null
  /** `networkAcls.defaultAction`: `Deny` admits only the listed addresses and networks. */
  networkDefaultAction?: string | null
  networkIpRules?: string[]
  networkVirtualNetworkRuleCount?: number
  localAuthDisabled?: boolean | null
  managementApiVersion: string
  notes: string[]
}

export interface ModelInventorySummary {
  deployments: number
  availableModels: number
  succeededDeployments: number
  deprecatedDeployments: number
}

/**
 * A deployment an administrator declared on an Azure endpoint MOSAIC reaches with an API key. An
 * API key can't list a resource's deployments, so these are declared, never discovered (ADR 0018).
 */
export interface DeclaredDeployment {
  deploymentName: string
  modelName: string
  modelVersion?: string | null
  apiShape: ApiShape
  declaredAt: string
  declaredBy?: string | null
}

export interface DeclaredDeploymentInput {
  deploymentName: string
  modelName: string
  modelVersion?: string
  apiShape: ApiShape
}

export interface ModelEndpoint {
  id: string
  tenantId: string
  name: string
  provider: ModelProvider
  endpoint: string
  azureResourceId?: string | null
  subscriptionId?: string | null
  resourceGroup?: string | null
  accountName?: string | null
  projectName?: string | null
  environment: string | null
  azureEnvironmentTag?: string | null
  /** @deprecated Use environment. Legacy display-only data. */
  environmentLabel?: string | null
  authMode: EndpointAuthMode
  credentialReferenceId?: string | null
  /** Present only on an Azure endpoint registered with an API key. */
  declaredDeployments?: DeclaredDeployment[]
  status: ModelEndpointStatus
  access: EndpointAccess
  runtimeAccess: GatewayRuntimeAccess[]
  capabilities: ModelEndpointCapabilities
  inventory: ModelInventorySummary
  lastSyncedAt?: string | null
  lastSyncError?: string | null
  createdAt: string
  updatedAt: string
}


export type PublicationStatus = 'draft' | 'planned' | 'applying' | 'published' | 'failed' | 'rolledBack'
export type PublishAction = 'create' | 'update' | 'delete' | 'noChange'
export type PublishStepStatus =
  | 'pending'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'rolledBack'
  | 'rollbackFailed'
export type PublishRunStatus = 'running' | 'succeeded' | 'failed' | 'rolledBack' | 'rollbackFailed' | 'interrupted'
export type PublishStage = 'prepare' | 'policy' | 'activate'
export type PublishedResourceKind =
  | 'namedValue'
  | 'policyFragment'
  | 'backend'
  | 'api'
  | 'apiOperation'
  | 'apiPolicy'
  | 'product'
  | 'productApi'
  | 'subscription'

export interface PublishedResource {
  kind: PublishedResourceKind
  name: string
  resourceId: string
  createdByMosaic: boolean
  appliedAt: string
}

/**
 * The curated operation set, backend host, and runtime auth a published model API uses. A Foundry
 * resource serves Anthropic models through the Anthropic Messages API (ADR 0012).
 */
export type ApiShape = 'azureOpenAi' | 'foundryModels' | 'anthropicMessages'

export type DeploymentCapability =
  | 'chat'
  | 'responses'
  | 'completion'
  | 'embeddings'
  | 'image'
  | 'transcription'
  | 'speech'
  | 'realtime'
  | 'video'
  | 'rerank'
  | 'unknown'

export interface PublishableModel {
  modelEndpointId: string
  endpointName: string
  provider: ModelProvider
  deploymentName: string
  modelName: string | null
  modelVersion: string | null
  modelFormat?: string | null
  modelPublisher?: string | null
  capability?: DeploymentCapability
  apiShape?: ApiShape | null
  /** False when MOSAIC has no curated shape for this deployment; the reason says why. */
  publishable?: boolean
  unpublishableReason?: string | null
  /** False when the gateway's tier can't meter this shape; the note says why. */
  tokenLimitsSupported?: boolean
  tokenLimitsNote?: string | null
  publicationId: string | null
  publicationStatus: PublicationStatus | null
  suggestedApiName: string
  suggestedApiPath: string
  runtimeAccess: GatewayRuntimeAccess | null
  environmentVerdict: EnvironmentVerdict
  /** True when an administrator declared this deployment rather than MOSAIC reading it. */
  declared?: boolean
}

export interface ModelAccessGrant {
  entitlementId: string
  subject: EntitlementSubject
  objectId: string
  displayName: string
  subscriptionName?: string | null
  /** The cost center the grant charges, and its code as the cost-center header names it. */
  costCenterId?: string | null
  costCenterCode?: string | null
  enabled: boolean
  enforcement?: EntitlementEnforcement | null
  intentDigest: string
  /** False when the grant's cost center turned keys off, so its key is suspended. */
  keysAllowed?: boolean
}

export interface ModelAccessSnapshot {
  version: number
  settings: ModelAccessSettings
  audience?: string | null
  /** Null when the publication's shape can't be token-metered on its gateway's tier. */
  publicationEnforcement: TokenEnforcement | null
  grants: ModelAccessGrant[]
}

export interface McpAccessGrant {
  entitlementId: string
  subject: EntitlementSubject
  objectId: string
  displayName: string
  costCenterId?: string | null
  costCenterCode?: string | null
  enabled: boolean
  enforcement?: EntitlementEnforcement | null
  intentDigest: string
}

export interface McpAccessSnapshot {
  version: number
  audience: string
  delegatedScope: string
  applicationRole: string
  grants: McpAccessGrant[]
}

export interface PublicationLockInfo {
  publicationId: string
  ownerId: string | null
}

export interface Publication {
  id: string
  tenantId: string
  entityType: 'publication'
  gatewayId: string
  modelEndpointId: string
  deploymentName: string
  provider: string
  displayName: string
  apiName: string
  apiPath: string
  backendName: string
  fragmentName: string
  productName: string
  subscriptionName: string
  /** The Key Vault-backed named value a key-authenticated publication's backend key comes from. */
  backendKeyName?: string | null
  subscriptionRequired: boolean
  /** Null when the publication's shape can't be token-metered on its gateway's tier. */
  enforcement: TokenEnforcement | null
  shapeVersion: string
  apiShape?: ApiShape | null
  status: PublicationStatus
  resources: PublishedResource[]
  lastPlanId: string | null
  lastPlanDigest: string | null
  lastRunId: string | null
  lastAppliedAt: string | null
  /** When an unpublish last removed everything MOSAIC created; cleared by the next successful apply. */
  unpublishedAt?: string | null
  lastError: string | null
  modelApiId?: string | null
  governedAccess?: ModelAccessSettings | null
  appliedAccess?: ModelAccessSnapshot | null
  accessState: 'pending' | 'applying' | 'applied' | 'failed' | 'unknown'
  createdAt: string
  updatedAt: string
}

export interface McpPublication {
  id: string
  tenantId: string
  entityType: 'mcpPublication'
  gatewayId: string
  mcpEndpointId: string
  displayName: string
  apiName: string
  apiPath: string
  backendName: string
  fragmentName: string
  metadataApiName: string
  mcpServerId: string
  status: PublicationStatus
  resources: PublishedResource[]
  lastPlanId: string | null
  lastPlanDigest: string | null
  lastRunId: string | null
  lastAppliedAt: string | null
  /** When an unpublish last removed everything MOSAIC created; cleared by the next successful apply. */
  unpublishedAt?: string | null
  lastError: string | null
  appliedAccess?: McpAccessSnapshot | null
  accessState: 'pending' | 'applying' | 'applied' | 'failed' | 'unknown'
  createdAt: string
  updatedAt: string
  etag?: string | null
}

export interface McpPublishingCapability {
  gatewayId: string
  supported: boolean
  reasons: string[]
  warnings: string[]
}

export interface McpPublicationCreate {
  gatewayId: string
  mcpEndpointId: string
  displayName?: string
  apiName?: string
  apiPath?: string
}

export interface McpPublicationUpdate {
  displayName?: string
}

export interface PublishPlanStep {
  kind: PublishedResourceKind
  name: string
  action: PublishAction
  reason: string
  resourceId: string
  existed: boolean
  entitlementId?: string | null
  subscriptionState?: 'active' | 'suspended' | null
  stage?: PublishStage | null
}

export interface PublishPlan {
  id: string
  tenantId: string
  entityType: 'publishPlan'
  publicationId: string
  gatewayId: string
  digest: string
  steps: PublishPlanStep[]
  facets: PolicyFacet[]
  policyContentSha256: string | null
  warnings: string[]
  target?: 'model' | 'mcp'
  /** Publish plans are applied; unpublish plans delete what MOSAIC created. Older plans omit it. */
  operation?: 'publish' | 'unpublish'
  accessSnapshot?: ModelAccessSnapshot | null
  mcpAccessSnapshot?: McpAccessSnapshot | null
  previousAccessVersion?: number | null
  createdAt: string
  updatedAt: string
}

export interface PublishStepResult {
  kind: PublishedResourceKind
  name: string
  action: PublishAction
  status: PublishStepStatus
  resourceId: string
  createdByMosaic: boolean
  error: string | null
  stage?: PublishStage | null
}

export interface PublishRun {
  id: string
  tenantId: string
  entityType: 'publishRun'
  publicationId: string
  gatewayId: string
  planId: string
  planDigest: string
  status: PublishRunStatus
  startedAt: string
  completedAt: string | null
  durationMs: number | null
  steps: PublishStepResult[]
  rolledBack: boolean
  orphanedResources: PublishedResource[]
  errors: string[]
  target?: 'model' | 'mcp'
  accessSnapshot?: ModelAccessSnapshot | null
  mcpAccessSnapshot?: McpAccessSnapshot | null
  createdAt: string
  updatedAt: string
}

export interface ModelConnection {
  entitlementId: string
  publicationId: string
  gatewayId: string
  endpoint: string
  deploymentName: string
  tenantId: string
  costCenter?: CostCenterRef | null
  costCenterHeader?: 'x-mosaic-cost-center'
  keyExists?: boolean
  keysAllowedByCostCenter?: boolean
  runtime?: EntitlementRuntime | null
  appliedMethods?: ModelAccessSettings | null
  entraAudience?: string | null
  entraScope?: string | null
  entraClientId?: string | null
  subscriptionHeader: 'Ocp-Apim-Subscription-Key'
  apiShape?: ApiShape | null
  operations: { name: string; method: string; path: string }[]
  publicationLimits: TokenEnforcement | null
  grantLimits?: EntitlementEnforcement | null
  principalKind?: PrincipalKind | null
  requiredAppRole?: string | null
  entraApplicationScope?: string | null
  keysAvailable: boolean
  viaGroupId?: string | null
  viaGroupName?: string | null
}

export interface McpConnection {
  entitlementId: string
  mcpServerId: string
  publicationId?: string | null
  gatewayId: string
  displayName: string
  tenantId: string
  costCenter?: CostCenterRef | null
  costCenterHeader?: 'x-mosaic-cost-center'
  serverUrl?: string | null
  transport: McpTransportType
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

export type DirectorySearchKind = 'user' | 'group' | 'agent'

export interface DirectoryObject {
  objectId: string
  kind: PrincipalKind
  displayName?: string | null
  detail?: string | null
  appId?: string | null
  identityParentId?: string | null
  blueprintId?: string | null
  principalId?: string | null
}

export interface DirectoryMemberPage {
  groupObjectId: string
  members: DirectoryObject[]
  truncated: boolean
}

export interface DirectoryStatus {
  lookupEnabled: boolean
  groupClaimsEnabled: boolean
  message?: string | null
}

export type GrantOverlapKind = 'groups' | 'directAndGroup' | 'multipleGroups'

export interface OverlapGrant {
  entitlementId: string
  subject: EntitlementSubject
  subjectLabel: string
  costCenterId?: string | null
  enabled: boolean
  enforcement?: EntitlementEnforcement | null
}

export interface GrantOverlap {
  kind: GrantOverlapKind
  resource: EntitlementResource
  resourceLabel: string
  costCenterId?: string | null
  principalId?: string | null
  principalLabel?: string | null
  winner: OverlapGrant
  shadowed: OverlapGrant[]
  reason: string
}

export interface GrantOverlapReport {
  overlaps: GrantOverlap[]
  membershipChecked: boolean
  skipped: string[]
  generatedAt: string
}

export type KeySlot = 'primary' | 'secondary'

export interface KeyRevealResult {
  entitlementId: string
  subscriptionName: string
  slot: KeySlot
  key: string
  costCenter?: CostCenterRef | null
}

export interface ModelEndpointSyncRun {
  id: string
  tenantId: string
  endpointId: string
  status: GatewaySyncStatus
  startedAt: string
  completedAt?: string | null
  durationMs?: number | null
  counts: ModelInventorySummary
  removed: number
  errors: string[]
}

export interface ObservedModelDeployment {
  id: string
  endpointId: string
  deploymentName: string
  modelName?: string | null
  modelVersion?: string | null
  modelFormat?: string | null
  modelPublisher?: string | null
  skuName?: string | null
  skuCapacity?: number | null
  provisioningState?: string | null
  raiPolicyName?: string | null
  capabilities: Record<string, string>
  requestPaths: string[]
  observedAt: string
}

export interface ObservedAvailableModel {
  id: string
  endpointId: string
  modelName: string
  modelFormat?: string | null
  modelVersion?: string | null
  lifecycleStatus?: string | null
  maxCapacity?: number | null
  capabilities: Record<string, string>
  deprecationInference?: string | null
  deprecationFineTune?: string | null
  observedAt: string
}

export interface ModelEndpointSuggestion {
  source: SuggestionSource
  endpoint?: string | null
  azureResourceId?: string | null
  accountName?: string | null
  resourceGroup?: string | null
  subscriptionId?: string | null
  kind?: string | null
  location?: string | null
  provider?: ModelProvider | null
  alreadyRegistered: boolean
  modelEndpointId?: string | null
  reason: string
  azureEnvironmentTag?: string | null
  suggestedEnvironment?: string | null
}

export interface SubscriptionScanIssue {
  subscriptionId: string
  displayName?: string | null
  message: string
  remediation?: AccessRemediation | null
}

export type SubscriptionScanStatus =
  | 'notConfigured'
  | 'listFailed'
  | 'noVisibleSubscriptions'
  | 'scanned'

export interface ModelEndpointSuggestionView {
  suggestions: ModelEndpointSuggestion[]
  /** Subscriptions whose Azure AI resources MOSAIC could not list at all. */
  scanIssues: SubscriptionScanIssue[]
  /**
   * Subscriptions MOSAIC listed without a role that reads every Azure AI resource in them. Azure
   * leaves out what MOSAIC cannot read without saying so, so these still count in
   * `subscriptionsScanned` and whatever they yielded is still in `suggestions`.
   */
  partialScans: SubscriptionScanIssue[]
  subscriptionsScanned: number
  scanStatus: SubscriptionScanStatus
  /** Why MOSAIC could not list subscriptions; set only when `scanStatus` is `listFailed`. */
  scanMessage?: string | null
  /** Reader at subscription scope, offered when the scan could see no subscription at all. */
  scanRemediation: AccessRemediation[]
}

export type McpAuthMode = 'none' | 'apiKey' | 'managedIdentity'

/**
 * `unsupportedProtocol` and `unsupportedTransport` are deliberately distinct from the failure
 * states: the server answered, and the answer is that it speaks something MOSAIC does not. Neither
 * is cleared by retrying.
 */
export type McpEndpointStatus =
  | 'pending'
  | 'connected'
  | 'degraded'
  | 'unauthorized'
  | 'unreachable'
  | 'unsupportedProtocol'
  | 'unsupportedTransport'

export type McpDiscoveryEvaluation = 'handshake' | 'authorizationRequired' | 'notEvaluated'

/** What a 401 asked for, so "needs authorization" never reads as "unreachable". */
export interface McpAuthChallenge {
  scheme?: string | null
  resourceMetadataUrl?: string | null
  scope?: string | null
}

/** Whether MOSAIC can reach and read a registered MCP server. */
export interface McpDiscoveryAccess {
  canDiscover: boolean
  evaluation: McpDiscoveryEvaluation
  checkedAt?: string | null
  challenge?: McpAuthChallenge | null
  message?: string | null
}

export interface McpEndpointCapabilities {
  protocolVersion?: string | null
  offeredProtocolVersion: string
  transportType: McpTransportType
  serverName?: string | null
  serverTitle?: string | null
  serverVersion?: string | null
  instructions?: string | null
  supportsTools: CapabilitySupport
  sessionManaged: boolean
  notes: string[]
}

/**
 * Counts only what a server actually stated. There is no destructive count on purpose:
 * `destructiveHint` defaults to true when absent, so counting it would report silence as a claim.
 */
export interface McpInventorySummary {
  tools: number
  readOnlyTools: number
  unannotatedTools: number
}

export interface McpEndpoint {
  id: string
  tenantId: string
  name: string
  endpoint: string
  environment: string | null
  /** @deprecated Use environment. Legacy display-only data. */
  environmentLabel?: string | null
  authMode: McpAuthMode
  credentialReferenceId?: string | null
  resourceAudience?: string | null
  status: McpEndpointStatus
  access: McpDiscoveryAccess
  capabilities: McpEndpointCapabilities
  inventory: McpInventorySummary
  lastSyncedAt?: string | null
  lastSyncError?: string | null
  createdAt: string
  updatedAt: string
}

export interface McpEndpointSyncRun {
  id: string
  tenantId: string
  endpointId: string
  status: GatewaySyncStatus
  startedAt: string
  completedAt?: string | null
  durationMs?: number | null
  counts: McpInventorySummary
  removed: number
  errors: string[]
}

/**
 * A server's own claims about a tool. Every hint is tri-state: `null` means the server said
 * nothing, which is not the same as saying `false`. The MCP specification defaults
 * `destructiveHint` and `openWorldHint` to *true*, and states that clients must treat annotations
 * as untrusted, so these are never rendered as guarantees.
 */
export interface McpToolAnnotations {
  title?: string | null
  readOnlyHint?: boolean | null
  destructiveHint?: boolean | null
  idempotentHint?: boolean | null
  openWorldHint?: boolean | null
}

export interface ObservedMcpTool {
  id: string
  tenantId: string
  endpointId: string
  snapshotId: string
  observedAt: string
  name: string
  displayName: string
  title?: string | null
  description?: string | null
  inputSchema?: Record<string, unknown> | null
  outputSchema?: Record<string, unknown> | null
  annotations?: McpToolAnnotations | null
}

export type AnalyticsRange = '24h' | '7d' | '30d' | '90d' | '12m' | 'custom'
export type AnalyticsDataSource = 'logAnalytics' | 'notConfigured'
export type AnalyticsGranularity = 'hour' | 'day' | 'month'
export type FreshnessStatus = 'current' | 'delayed' | 'failing' | 'pending' | 'notLinked'
export type ExportView =
  | 'trend'
  | 'people'
  | 'applications'
  | 'groups'
  | 'grants'
  | 'clientApps'
  | 'apis'
  | 'models'
  | 'deployments'
  | 'denials'
  | 'limits'
  | 'unusedGrants'
  | 'unusedKeys'
  | 'untrackedGrants'
  | 'unattributed'
  | 'costDeployments'
  | 'chargeback'
  | 'costCenters'

export interface AnalyticsFilters {
  range?: AnalyticsRange
  start?: string
  end?: string
  gatewayId?: string
  environment?: string
  resourceId?: string
  subjectKind?: EntitlementSubjectKind
  costCenterId?: string
}

export interface UsageFreshness {
  status: FreshnessStatus
  updatedAt?: string | null
  dataFrom?: string | null
  gateways: number
  intervalMinutes: number | null
  message?: string | null
}

export interface AnalyticsWindow {
  range: AnalyticsRange
  granularity: AnalyticsGranularity
  start: string
  end: string
  breakdownStart: string
  breakdownEnd: string
  previousStart: string
  previousEnd: string
}

export interface AnalyticsKpis {
  requests: number
  promptTokens: number | null
  completionTokens: number | null
  totalTokens: number
  ok: number
  throttled: number
  quotaRefused: number
  denied: number
  errors: number
  clientErrors: number | null
  serverErrors: number | null
  backendThrottled: number | null
  successRate: number | null
  errorRate: number | null
  throttleRate: number | null
  denialRate: number | null
  p50LatencyMs: number | null
  p95LatencyMs: number | null
  averageLatencyMs: number | null
  averageBackendMs: number | null
  activeCallers: number | null
  activeGrants: number | null
  activeApis: number | null
  unattributedRequests: number | null
  /** US dollars at list prices. Null when nothing could be priced, or the window is by the hour. */
  cost?: number | null
}

export interface AnalyticsTrendPoint {
  start: string
  requests: number | null
  totalTokens: number | null
  throttled: number | null
  quotaRefused: number | null
  denied: number | null
  errors: number | null
  cost?: number | null
}

export interface AnalyticsSeries {
  key: string
  label: string
  values: Array<number | null>
}

export interface AnalyticsRankRow {
  key: string
  label: string
  detail?: string | null
  requests: number
  totalTokens: number
  requestShare: number | null
  tokenShare: number | null
  cost?: number | null
}

export interface AnalyticsGatewayHealth {
  gatewayId: string
  name: string
  environment: string | null
  environmentName: string
  status: FreshnessStatus
  governedApis: number
  instrumentedApis: number
  lastRunAt: string | null
  lastSuccessAt: string | null
  queriedThrough: string | null
  lagMinutes: number | null
  dataAvailableFrom: string | null
  lastError: string | null
  lastErrorAt: string | null
  backfillStatus: 'idle' | 'running' | 'done' | 'failed'
  backfillFrom: string | null
  backfillNext: string | null
  unknownTraceVersions: number
  diagnosticsError: string | null
}

export interface AnalyticsReport {
  dataSource: AnalyticsDataSource
  generatedAt: string
  window: AnalyticsWindow
  freshness: UsageFreshness
  notes: string[]
}

export interface AnalyticsOverview extends AnalyticsReport {
  kpis: AnalyticsKpis
  previous: AnalyticsKpis | null
  trend: AnalyticsTrendPoint[]
  modelTrend: AnalyticsSeries[]
  topModels: AnalyticsRankRow[]
  topCallers: AnalyticsRankRow[]
  topCostCenters?: AnalyticsRankRow[]
  topApis: AnalyticsRankRow[]
  gateways: AnalyticsGatewayHealth[]
  cost?: AnalyticsCostSummary | null
  spend?: AnalyticsSpend | null
}

export interface AnalyticsUsage {
  requests: number
  promptTokens: number
  completionTokens: number
  totalTokens: number
  throttled: number
  quotaRefused: number
  errors: number
  lastSeen: string | null
  requestShare: number | null
  tokenShare: number | null
  /** US dollars at list prices. Null when the row can't be priced or carries no tokens. */
  cost?: number | null
}

export interface AnalyticsConsumerRow extends AnalyticsUsage {
  key: string
  kind: 'person' | 'application' | 'group'
  label: string
  detail?: string | null
  principalId?: string | null
  /** What the Entra object is; agent users are counted with people. */
  principalKind?: PrincipalKind | null
  grants: number
  resources: number
  members?: number | null
}

export interface AnalyticsGrantRow extends AnalyticsUsage {
  key: string
  entitlementId: string | null
  state: 'active' | 'disabled' | 'removed'
  subjectKind: EntitlementSubjectKind | null
  subjectLabel: string
  subjectDetail?: string | null
  subjectPrincipalKind?: PrincipalKind | null
  costCenterId?: string | null
  costCenterCode?: string | null
  costCenterName?: string | null
  resourceKind: EntitlementResourceKind | null
  resourceLabel: string
  gatewayId: string | null
  gatewayName: string | null
  callers: number
  keyRequests: number
  peakMinuteTokens: number
  peakMinuteRequests: number
}

export interface AnalyticsClientAppRow extends AnalyticsUsage {
  clientAppId: string
  label: string
  principalId?: string | null
  apis: number
}

export interface AnalyticsConsumers extends AnalyticsReport {
  linkedRequests: number
  linkedTokens: number
  unidentifiedRequests: number
  people: AnalyticsConsumerRow[]
  applications: AnalyticsConsumerRow[]
  groups: AnalyticsConsumerRow[]
  costCenters?: Array<{
    key: string
    label: string
    code: string
    grants: number
    callers: number
  } & AnalyticsUsage>
  grants: AnalyticsGrantRow[]
  clientApps: AnalyticsClientAppRow[]
  truncated: boolean
  cost?: AnalyticsCostSummary | null
}

export interface AnalyticsApiRow extends AnalyticsUsage {
  key: string
  gatewayId: string
  gatewayName: string
  apiName: string
  label: string
  kind: 'model' | 'mcp' | null
  resourceId: string | null
  removed: boolean
  meteredRequests: number
  denied: number
  clientErrors: number
  serverErrors: number
  backendThrottled: number
  p95LatencyMs: number | null
  averageLatencyMs: number | null
  errorRate: number | null
  models: string[] | null
}

export interface AnalyticsModelRow extends AnalyticsUsage {
  model: string
  apis: number
}

export interface AnalyticsDeploymentRow extends AnalyticsUsage {
  key: string
  endpointId: string
  endpointName: string | null
  deploymentName: string
  modelName: string | null
  skuName: string | null
  capacityTokensPerMinute: number | null
  backendThrottled: number
  peakMinuteTokens: number
  peakMinuteRequests: number
  utilization: number | null
  gateways: number
  hourlyPeakTokens: number[] | null
}

export interface AnalyticsBreakdownRow extends AnalyticsUsage {
  key: string
  label: string
  denied: number
}

export interface AnalyticsModels extends AnalyticsReport {
  apis: AnalyticsApiRow[]
  models: AnalyticsModelRow[]
  deployments: AnalyticsDeploymentRow[]
  gateways: AnalyticsBreakdownRow[]
  environments: AnalyticsBreakdownRow[]
  cost?: AnalyticsCostSummary | null
}

export interface AnalyticsStatusMix {
  requests: number
  ok: number
  throttled: number
  quotaRefused: number
  denied: number
  clientErrors: number
  serverErrors: number
  backendThrottled: number
}

export interface AnalyticsLatencyBucket {
  upperMs: number | null
  count: number
}

export interface AnalyticsLatency {
  p50Ms: number | null
  p95Ms: number | null
  p99Ms: number | null
  averageMs: number | null
  averageBackendMs: number | null
  buckets: AnalyticsLatencyBucket[]
}

export interface AnalyticsDenialReason {
  reason: string
  label: string
  requests: number
  share: number | null
}

export interface AnalyticsDenialRow {
  reason: string
  reasonLabel: string
  callerObjectId: string | null
  callerLabel: string | null
  clientAppId: string | null
  clientAppLabel: string | null
  gatewayId: string
  gatewayName: string
  apiName: string
  apiLabel: string
  requests: number
  lastSeen: string | null
}

export interface AnalyticsReliability extends AnalyticsReport {
  statusMix: AnalyticsStatusMix
  latency: AnalyticsLatency
  trend: AnalyticsTrendPoint[]
  apis: AnalyticsApiRow[]
  denialReasons: AnalyticsDenialReason[]
  denials: AnalyticsDenialRow[]
}

export interface AnalyticsLimitUse {
  kind: 'quota' | 'rateLimit'
  metric: 'requests' | 'tokens'
  limit: number
  period: QuotaPeriod | null
  windowSeconds: number | null
  windowStart: string | null
  windowEnd: string | null
  used: number | null
  utilization: number | null
  partial: boolean
}

export interface AnalyticsLimitRow {
  key: string
  entitlementId: string
  subjectKind: EntitlementSubjectKind
  subjectLabel: string
  subjectDetail: string | null
  subjectPrincipalKind?: PrincipalKind | null
  costCenterCode?: string | null
  costCenterName?: string | null
  memberObjectId: string | null
  memberLabel: string | null
  resourceKind: EntitlementResourceKind
  resourceLabel: string
  gatewayId: string | null
  gatewayName: string | null
  limits: AnalyticsLimitUse[]
  utilization: number | null
  status: 'ok' | 'near' | 'reached' | 'unknown'
  throttled: number
  quotaRefused: number
}

export interface AnalyticsLimits extends AnalyticsReport {
  threshold: number
  near: number
  reached: number
  rows: AnalyticsLimitRow[]
  truncated: boolean
}

export interface AnalyticsGrantRef {
  entitlementId: string
  subjectKind: EntitlementSubjectKind
  subjectLabel: string
  subjectDetail: string | null
  subjectPrincipalKind?: PrincipalKind | null
  costCenterCode?: string | null
  costCenterName?: string | null
  resourceKind: EntitlementResourceKind
  resourceLabel: string
  gatewayId: string | null
  gatewayName: string | null
}

export interface AnalyticsUnusedGrant extends AnalyticsGrantRef {
  grantedAt: string
  lastUsedAt: string | null
}

export interface AnalyticsUnusedKey extends AnalyticsGrantRef {
  subscriptionName: string | null
  tokenRequests: number
}

export interface AnalyticsUntrackedGrant extends AnalyticsGrantRef {
  reason: 'mosaicGroup' | 'notApplied' | 'noLink'
}

export interface AnalyticsHygiene extends AnalyticsReport {
  judgedGrants: number
  unusedGrants: AnalyticsUnusedGrant[]
  unusedKeys: AnalyticsUnusedKey[]
  deniedCallers: AnalyticsDenialRow[]
  untrackedGrants: AnalyticsUntrackedGrant[]
  truncated: boolean
}

export interface AnalyticsUnattributedRow {
  gatewayId: string
  gatewayName: string
  apiName: string
  apiLabel: string
  subscription: string | null
  reason: 'noSubscription' | 'unknownSubscription' | 'sharedKey'
  requests: number
  totalTokens: number
  lastSeen: string | null
  share: number | null
  cost?: number | null
}

export interface AnalyticsUnattributed extends AnalyticsReport {
  requests: number
  totalTokens: number
  admittedRequests: number
  share: number | null
  rows: AnalyticsUnattributedRow[]
  truncated: boolean
  cost?: AnalyticsCostSummary | null
}

export interface AnalyticsStatus {
  dataSource: AnalyticsDataSource
  rollupsEnabled: boolean
  generatedAt: string
  freshness: UsageFreshness
  gateways: AnalyticsGatewayHealth[]
}

export interface TelemetryCheck {
  id: 'logger' | 'logRouting' | 'logAccess' | 'apiDiagnostics' | 'rollups'
  status: 'ok' | 'warning' | 'error' | 'unknown'
  title: string
  detail: string
  command?: string | null
}

export type ApiDiagnosticGap = 'missing' | 'logger' | 'verbosity' | 'sampling' | 'llmLogs'

export interface ApiTelemetry {
  apiName: string
  displayName: string
  kind: 'model' | 'mcp'
  published: boolean
  // The API has no diagnostic of its own and logs as the gateway's All APIs setting says.
  allApis: boolean
  gaps: ApiDiagnosticGap[]
}

export interface TelemetryProbe {
  hours: number
  gatewayRows: number
  tracedRows: number
  llmRows: number
  lastSeen: string | null
}

export interface RollupStatus {
  lastRunAt: string | null
  lastSuccessAt: string | null
  lastDurationMs: number | null
  queriedThrough: string | null
  lagMinutes: number | null
  dataAvailableFrom: string | null
  lastError: string | null
  lastErrorAt: string | null
  backfillStatus: 'idle' | 'running' | 'done' | 'failed'
  backfillFrom: string | null
  backfillNext: string | null
  lastRows: number
  lastWritten: number
  unknownTraceVersions: number
  diagnosticsError: string | null
}

export interface GatewayTelemetry {
  gatewayId: string
  gatewayName: string
  managementMode: ManagementMode
  ready: boolean
  canEnable: boolean
  rollupsEnabled: boolean
  checkedAt: string
  workspaceId: string | null
  checks: TelemetryCheck[]
  apis: ApiTelemetry[]
  probe: TelemetryProbe | null
  rollup: RollupStatus | null
}

export interface AnalyticsUnpricedUse {
  key: string
  kind: 'deployment' | 'api' | 'grant'
  label: string
  detail?: string | null
  reason: string
  message: string
  requests: number
  totalTokens: number
}

export interface AnalyticsCostSummary {
  currency: 'USD'
  /** Null when nothing could be priced. Never zero for usage MOSAIC couldn't price. */
  total: number | null
  reserved?: number | null
  pricedTokens: number
  unpricedTokens: number
  unpricedRequests: number
  unpricedItems: number
  unpriced: AnalyticsUnpricedUse[]
  notes: string[]
}

export interface AnalyticsSpend {
  currency: 'USD'
  monthStart: string
  daysInMonth: number
  daysElapsed: number
  through?: string | null
  monthToDate: number | null
  reserved?: number | null
  /** A projection, not a bill. Null until MOSAIC has a day of this month's figures. */
  forecast: number | null
  projected: boolean
  unpricedTokens: number
  unpricedItems: number
}

export interface AnalyticsCostTrendPoint {
  start: string
  cost: number | null
  reserved?: number | null
  totalTokens?: number | null
}

export interface AnalyticsCostRow {
  key: string
  label: string
  detail?: string | null
  kind?: string | null
  requests: number
  totalTokens: number
  cost: number | null
  costShare?: number | null
}

export interface AnalyticsCostDeploymentRow {
  key: string
  endpointId: string | null
  endpointName: string | null
  deploymentName: string
  modelName: string | null
  modelVersion: string | null
  cloud: string | null
  cloudLabel: string
  deploymentType: string | null
  region: string | null
  pricing: 'tokens' | 'provisioned' | 'unpriced'
  priceId?: string | null
  priceOrigin?: 'seed' | 'admin' | null
  inputPerMillion?: number | null
  cachedInputPerMillion?: number | null
  outputPerMillion?: number | null
  ptuHourly?: number | null
  monthlyAmount?: number | null
  capacity?: number | null
  monthCost?: number | null
  utilization?: number | null
  idleCost?: number | null
  unpricedReason?: string | null
  unpricedMessage?: string | null
  requests: number
  promptTokens: number
  completionTokens: number
  totalTokens: number
  cost: number | null
  costShare?: number | null
  gateways: number
}

export interface AnalyticsCost extends AnalyticsReport {
  spend: AnalyticsSpend | null
  cost: AnalyticsCostSummary
  trend: AnalyticsCostTrendPoint[]
  models: AnalyticsCostRow[]
  deployments: AnalyticsCostDeploymentRow[]
  consumers: AnalyticsCostRow[]
  costCenters?: AnalyticsCostRow[]
  apis: AnalyticsCostRow[]
  priced: boolean
}

export type PriceOrigin = 'seed' | 'admin'
export type PriceStatus = 'current' | 'upcoming' | 'past' | 'corrected'
export type UnpricedReason =
  | 'unknownDeployment'
  | 'noCloud'
  | 'noModel'
  | 'noDeploymentType'
  | 'noCapacity'
  | 'noPrice'
  | 'notYetEffective'
  | 'noOutputPrice'
  | 'beforeDeployment'

export interface PriceSource {
  title: string
  url: string
  retrievedOn?: string | null
}

export interface PriceView {
  id: string
  lineId: string
  origin: PriceOrigin
  status: PriceStatus
  cloud: string
  cloudLabel: string
  publisher: string | null
  model: string
  aliases: string[]
  version: string | null
  deploymentType: string | null
  regions: string[] | null
  deployment: string | null
  inputPerMillion: number | null
  cachedInputPerMillion: number | null
  outputPerMillion: number | null
  ptuHourly: number | null
  monthlyAmount: number | null
  effectiveFrom: string
  /** Only a seeded price that a later seed replaced ends. */
  effectiveUntil?: string | null
  recordedAt: string
  recordedBy: string | null
  sources: PriceSource[]
  note: string | null
  overrides: string | null
}

export interface PriceLineView {
  lineId: string
  current: PriceView | null
  upcoming: PriceView | null
  versions: number
}

export interface PriceListView {
  cloud: string
  cloudLabel: string
  currency: 'USD'
  asOf: string
  lines: PriceLineView[]
}

export interface PriceHistoryView {
  lineId: string
  versions: PriceView[]
}

export interface PricingCloud {
  key: string
  label: string
  builtIn: boolean
  prices: number
  endpoints: number
}

export interface PricingOverview {
  currency: 'USD'
  asOf: string
  seedLastUpdated: string
  sources: PriceSource[]
  clouds: PricingCloud[]
  deploymentTypes: string[]
  deployments: number
  pricedDeployments: number
}

export interface PriceCreate {
  cloud: string
  publisher?: string | null
  model: string
  aliases?: string[]
  version?: string | null
  deploymentType?: string | null
  regions?: string[] | null
  deployment?: string | null
  inputPerMillion?: number | null
  cachedInputPerMillion?: number | null
  outputPerMillion?: number | null
  ptuHourly?: number | null
  monthlyAmount?: number | null
  effectiveFrom: string
  sourceUrl: string
  note: string
  overrides?: string | null
}

export interface UnpricedDeploymentRow {
  key: string
  kind: 'deployment' | 'api'
  label: string
  endpointId?: string | null
  endpointName?: string | null
  deploymentName?: string | null
  gatewayName?: string | null
  model?: string | null
  version?: string | null
  deploymentType?: string | null
  cloud?: string | null
  region?: string | null
  declared: boolean
  reason: UnpricedReason
  message: string
  requests: number
  totalTokens: number
}

export interface UnpricedReport {
  asOf: string
  days: number
  deployments: number
  pricedDeployments: number
  rows: UnpricedDeploymentRow[]
}

export interface DeploymentPricingView {
  deploymentName: string
  model: string | null
  version: string | null
  deploymentType: string | null
  deploymentTypeSource: 'observed' | 'admin' | null
  capacity: number | null
  declared: boolean
  priced: boolean
  priceId?: string | null
  reason?: UnpricedReason | null
  message?: string | null
}

export interface EndpointPricingView {
  endpointId: string
  name: string
  provider: string
  host: string | null
  detectedCloud: string | null
  cloud: string | null
  cloudLabel: string
  cloudSource: 'detected' | 'override' | null
  region: string | null
  regionSource: 'detected' | 'override' | null
  deployments: DeploymentPricingView[]
  updatedBy?: string | null
  updatedAt?: string | null
  /** The saved facts' version, sent back with an update. Null until any are saved. */
  version?: string | null
}

export interface EndpointPricingUpdate {
  cloud?: string | null
  region?: string | null
  deployments?: Array<{
    deploymentName: string
    deploymentType?: string | null
    capacity?: number | null
  }>
  /** The version the form was opened on. A save over someone else's newer change is refused. */
  version?: string | null
}