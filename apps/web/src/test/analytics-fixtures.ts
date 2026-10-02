import type {
  AnalyticsConsumers,
  AnalyticsCost,
  AnalyticsCostSummary,
  AnalyticsHygiene,
  AnalyticsLimits,
  AnalyticsModels,
  AnalyticsOverview,
  AnalyticsReliability,
  AnalyticsStatus,
  AnalyticsUnattributed,
  GatewayTelemetry,
} from '../types'

const generatedAt = '2026-03-18T15:30:00Z'

const reportBase = {
  dataSource: 'logAnalytics' as const,
  generatedAt,
  window: {
    range: '30d' as const,
    granularity: 'day' as const,
    start: '2026-02-17T00:00:00Z',
    end: '2026-03-19T00:00:00Z',
    breakdownStart: '2026-02-17',
    breakdownEnd: '2026-03-18',
    previousStart: '2026-01-18T00:00:00Z',
    previousEnd: '2026-02-17T00:00:00Z',
  },
  freshness: {
    status: 'current' as const,
    updatedAt: '2026-03-18T15:15:00Z',
    dataFrom: '2026-02-17',
    gateways: 1,
    intervalMinutes: 15,
    message: null,
  },
  notes: [] as string[],
}

export const overviewFixture: AnalyticsOverview = {
  ...reportBase,
  kpis: {
    requests: 165,
    promptTokens: 30140,
    completionTokens: 15030,
    totalTokens: 45170,
    ok: 162,
    throttled: 1,
    quotaRefused: 0,
    denied: 2,
    errors: 0,
    clientErrors: 2,
    serverErrors: 0,
    backendThrottled: 0,
    successRate: 0.9818,
    errorRate: 0,
    throttleRate: 0.006,
    denialRate: 0.012,
    p50LatencyMs: 300,
    p95LatencyMs: 1500,
    averageLatencyMs: 690,
    averageBackendMs: 430,
    activeCallers: 3,
    activeGrants: 4,
    activeApis: 3,
    unattributedRequests: 3,
  },
  previous: null,
  trend: [
    { start: '2026-03-17T00:00:00Z', requests: 13, totalTokens: 170, throttled: 1, quotaRefused: 0, denied: 2, errors: 0 },
    { start: '2026-03-18T00:00:00Z', requests: 15, totalTokens: 3000, throttled: 0, quotaRefused: 0, denied: 0, errors: 0 },
  ],
  modelTrend: [],
  topModels: [{ key: 'gpt-4o', label: 'gpt-4o', detail: null, requests: 153, totalTokens: 45030, requestShare: 0.93, tokenShare: 0.99 }],
  topCallers: [{ key: 'alice', label: 'Alice Admin', detail: 'alice@contoso', requests: 106, totalTokens: 15060, requestShare: 0.64, tokenShare: 0.33 }],
  topCostCenters: [{ key: 'cc-support', label: 'Support', detail: 'support', requests: 120, totalTokens: 32000, requestShare: 0.73, tokenShare: 0.71 }],
  topApis: [{ key: 'chat', label: 'Chat', detail: 'Production gateway', requests: 154, totalTokens: 45030, requestShare: 0.93, tokenShare: 0.99 }],
  gateways: [{
    gatewayId: 'gateway-prod',
    name: 'Production gateway',
    environment: 'production',
    environmentName: 'Production',
    status: 'current',
    governedApis: 3,
    instrumentedApis: 3,
    lastRunAt: generatedAt,
    lastSuccessAt: generatedAt,
    queriedThrough: generatedAt,
    lagMinutes: 15,
    dataAvailableFrom: '2026-02-17',
    lastError: null,
    lastErrorAt: null,
    backfillStatus: 'done',
    backfillFrom: null,
    backfillNext: null,
    unknownTraceVersions: 0,
    diagnosticsError: null,
  }],
}

export const notConfiguredOverview: AnalyticsOverview = {
  ...overviewFixture,
  dataSource: 'notConfigured',
  notes: ['Set MOSAIC_USAGE_SOURCE=rollups to enable usage telemetry.'],
  kpis: { ...overviewFixture.kpis, requests: 0, totalTokens: 0 },
  trend: [],
}

export const consumersFixture: AnalyticsConsumers = {
  ...reportBase,
  linkedRequests: 160,
  linkedTokens: 45140,
  unidentifiedRequests: 0,
  people: [
    { key: 'alice', kind: 'person', label: 'Alice Admin', detail: 'alice@contoso', principalId: 'principal-alice', principalKind: 'user', grants: 3, resources: 2, members: null, requests: 106, promptTokens: 10000, completionTokens: 5060, totalTokens: 15060, throttled: 1, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.66, tokenShare: 0.33 },
    { key: 'scheduler', kind: 'person', label: 'Scheduling Assistant', detail: null, principalId: 'principal-scheduler', principalKind: 'agentUser', grants: 1, resources: 1, members: null, requests: 12, promptTokens: 900, completionTokens: 300, totalTokens: 1200, throttled: 0, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.07, tokenShare: 0.03 },
  ],
  applications: [{ key: 'bot', kind: 'application', label: 'Support bot', detail: 'bot-app-id', principalId: 'principal-bot', principalKind: 'servicePrincipal', grants: 1, resources: 1, members: null, requests: 50, promptTokens: 10000, completionTokens: 5000, totalTokens: 15000, throttled: 0, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.31, tokenShare: 0.33 }],
  groups: [{ key: 'analysts', kind: 'group', label: 'Analysts', detail: null, principalId: 'principal-group', principalKind: 'securityGroup', grants: 1, resources: 1, members: 2, requests: 7, promptTokens: 70, completionTokens: 70, totalTokens: 140, throttled: 0, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.04, tokenShare: 0.01 }],
  costCenters: [{ key: 'cc-support', label: 'Support', code: 'support', grants: 3, callers: 2, requests: 120, promptTokens: 20000, completionTokens: 12000, totalTokens: 32000, throttled: 1, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.75, tokenShare: 0.71 }],
  grants: [{ key: 'grant-alice', entitlementId: 'grant-alice', state: 'active', subjectKind: 'user', subjectLabel: 'Alice Admin', subjectDetail: 'alice@contoso', subjectPrincipalKind: 'user', costCenterId: 'cc-support', costCenterCode: 'support', costCenterName: 'Support', resourceKind: 'modelApi', resourceLabel: 'Chat', gatewayId: 'gateway-prod', gatewayName: 'Production gateway', callers: 1, keyRequests: 0, peakMinuteTokens: 1500, peakMinuteRequests: 10, requests: 101, promptTokens: 10000, completionTokens: 5000, totalTokens: 15000, throttled: 1, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.63, tokenShare: 0.33 }],
  clientApps: [{ clientAppId: 'cli-app', label: 'cli-app', principalId: null, apis: 1, requests: 100, promptTokens: 10000, completionTokens: 5000, totalTokens: 15000, throttled: 0, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.62, tokenShare: 0.33 }],
  onBehalf: [{
    key: 'adele|gateway-prod/ticket-tools|support-bot',
    personObjectId: 'aaaabbbb-cccc-dddd-eeee-ffff00001111',
    personLabel: 'Adele Vance',
    personDetail: 'adele@contoso.example',
    personPrincipalId: 'principal-adele',
    personPrincipalKind: 'user',
    gatewayId: 'gateway-prod',
    gatewayName: 'Production gateway',
    mcpApiName: 'ticket-tools',
    mcpLabel: 'Ticket tools',
    mcpServerId: 'mcp-tickets',
    applicationObjectId: 'bbbbcccc-dddd-eeee-ffff-000011112222',
    applicationLabel: 'Support bot',
    applicationDetail: 'Contoso support application',
    applicationPrincipalId: 'principal-bot',
    applicationPrincipalKind: 'servicePrincipal',
    requests: 3,
    promptTokens: 1200,
    completionTokens: 800,
    totalTokens: 2000,
    throttled: 0,
    quotaRefused: 0,
    errors: 0,
    lastSeen: generatedAt,
    requestShare: 0.02,
    tokenShare: 0.04,
  }],
  onBehalfUnresolved: [
    { reason: 'malformed', requests: 1, totalTokens: 12 },
    { reason: 'missing', requests: 2, totalTokens: 20 },
    { reason: 'late', requests: 1, totalTokens: 10 },
  ],
  truncated: false,
}

export const modelsFixture: AnalyticsModels = {
  ...reportBase,
  models: [{ model: 'gpt-4o', apis: 1, requests: 153, promptTokens: 30000, completionTokens: 15030, totalTokens: 45030, throttled: 1, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.93, tokenShare: 0.99 }],
  deployments: [{ key: 'chat-prod', endpointId: 'endpoint-aoai', endpointName: 'Contoso Azure OpenAI', deploymentName: 'chat-prod', modelName: 'gpt-4o', skuName: 'Standard', capacityTokensPerMinute: 10000, backendThrottled: 0, peakMinuteTokens: 1500, peakMinuteRequests: 10, utilization: 0.15, gateways: 1, hourlyPeakTokens: null, requests: 153, promptTokens: 30000, completionTokens: 15030, totalTokens: 45030, throttled: 1, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.93, tokenShare: 0.99 }],
  apis: [{ key: 'chat', gatewayId: 'gateway-prod', gatewayName: 'Production gateway', apiName: 'chat', label: 'Chat', kind: 'model', resourceId: 'model-api-chat', removed: false, meteredRequests: 153, denied: 2, clientErrors: 2, serverErrors: 0, backendThrottled: 0, p95LatencyMs: 1500, averageLatencyMs: 690, errorRate: 0, models: ['gpt-4o'], requests: 154, promptTokens: 30000, completionTokens: 15030, totalTokens: 45030, throttled: 1, quotaRefused: 0, errors: 0, lastSeen: generatedAt, requestShare: 0.93, tokenShare: 0.99 }],
  gateways: [],
  environments: [],
}

export const reliabilityFixture: AnalyticsReliability = {
  ...reportBase,
  statusMix: { requests: 165, ok: 162, throttled: 1, quotaRefused: 0, denied: 2, clientErrors: 2, serverErrors: 0, backendThrottled: 0 },
  latency: { p50Ms: 300, p95Ms: 1500, p99Ms: 1500, averageMs: 690, averageBackendMs: 430, buckets: [{ upperMs: 500, count: 110 }, { upperMs: 2000, count: 53 }, { upperMs: null, count: 0 }] },
  trend: overviewFixture.trend,
  apis: modelsFixture.apis.map((row) => ({ ...row, models: null })),
  denialReasons: [{ reason: 'no-grant', label: 'No grant for this caller', requests: 2, share: 1 }],
  denials: [{ reason: 'no-grant', reasonLabel: 'No grant for this caller', callerObjectId: 'dave-oid', callerLabel: null, clientAppId: 'dave-app', clientAppLabel: null, gatewayId: 'gateway-prod', gatewayName: 'Production gateway', apiName: 'chat', apiLabel: 'Chat', requests: 2, lastSeen: generatedAt }],
}

export const limitsFixture: AnalyticsLimits = {
  ...reportBase,
  threshold: 0.8,
  near: 1,
  reached: 0,
  rows: [
    { key: 'grant-alice', entitlementId: 'grant-alice', subjectKind: 'user', subjectLabel: 'Alice Admin', subjectDetail: 'alice@contoso', subjectPrincipalKind: 'user', costCenterCode: 'support', costCenterName: 'Support', memberObjectId: null, memberLabel: null, resourceKind: 'modelApi', resourceLabel: 'Chat', gatewayId: 'gateway-prod', gatewayName: 'Production gateway', utilization: 0.75, status: 'ok', throttled: 1, quotaRefused: 0, limits: [{ kind: 'quota', metric: 'tokens', limit: 20000, period: 'Monthly', windowSeconds: null, windowStart: null, windowEnd: null, used: 15000, utilization: 0.75, partial: false }] },
    { key: 'grant-scheduler', entitlementId: 'grant-scheduler', subjectKind: 'user', subjectLabel: 'Scheduling Assistant', subjectDetail: null, subjectPrincipalKind: 'agentUser', memberObjectId: null, memberLabel: null, resourceKind: 'modelApi', resourceLabel: 'Chat', gatewayId: 'gateway-prod', gatewayName: 'Production gateway', utilization: 0.85, status: 'near', throttled: 0, quotaRefused: 0, limits: [{ kind: 'rateLimit', metric: 'requests', limit: 100, period: null, windowSeconds: 60, windowStart: null, windowEnd: null, used: 85, utilization: 0.85, partial: false }] },
  ],
  truncated: false,
}

export const hygieneFixture: AnalyticsHygiene = {
  ...reportBase,
  judgedGrants: 5,
  unusedGrants: [{ entitlementId: 'grant-bob', subjectKind: 'user', subjectLabel: 'Bob Builder', subjectDetail: null, resourceKind: 'mcpServer', resourceLabel: 'Ticket tools', gatewayId: 'gateway-prod', gatewayName: 'Production gateway', grantedAt: '2026-01-01T00:00:00Z', lastUsedAt: null }],
  unusedKeys: [{ entitlementId: 'grant-alice', subjectKind: 'user', subjectLabel: 'Alice Admin', subjectDetail: null, resourceKind: 'modelApi', resourceLabel: 'Chat', gatewayId: 'gateway-prod', gatewayName: 'Production gateway', subscriptionName: 'sub-alice', tokenRequests: 101 }],
  deniedCallers: reliabilityFixture.denials,
  untrackedGrants: [{ entitlementId: 'grant-team', subjectKind: 'group', subjectLabel: 'Platform team', subjectDetail: null, resourceKind: 'mcpServer', resourceLabel: 'Ticket tools', gatewayId: null, gatewayName: null, reason: 'mosaicGroup' }],
  truncated: false,
}

export const unattributedFixture: AnalyticsUnattributed = {
  ...reportBase,
  requests: 4,
  totalTokens: 38,
  admittedRequests: 164,
  share: 0.024,
  rows: [
    { gatewayId: 'gateway-prod', gatewayName: 'Production gateway', apiName: 'chat', apiLabel: 'Chat', subscription: 'unknown-sub', reason: 'unknownSubscription', requests: 3, totalTokens: 30, lastSeen: generatedAt, share: 0.75 },
    { gatewayId: 'gateway-prod', gatewayName: 'Production gateway', apiName: 'mini', apiLabel: 'Summaries', subscription: 'mini', reason: 'sharedKey', requests: 1, totalTokens: 8, lastSeen: generatedAt, share: 0.25 },
  ],
  truncated: false,
}

export const statusFixture: AnalyticsStatus = {
  dataSource: 'logAnalytics',
  rollupsEnabled: true,
  generatedAt,
  freshness: reportBase.freshness,
  gateways: overviewFixture.gateways,
}

export const telemetryFixture: GatewayTelemetry = {
  gatewayId: 'gateway-prod',
  gatewayName: 'Production gateway',
  managementMode: 'manage',
  ready: false,
  canEnable: true,
  rollupsEnabled: true,
  checkedAt: generatedAt,
  workspaceId: '/subscriptions/sub-1/resourceGroups/rg-logs/providers/Microsoft.OperationalInsights/workspaces/log-prod',
  checks: [
    { id: 'logger', status: 'ok', title: 'Azure Monitor logger', detail: 'The gateway has the azuremonitor logger.' },
    { id: 'logRouting', status: 'error', title: 'Logs sent to Log Analytics', detail: 'The gateway sends no resource logs to a Log Analytics workspace.', command: 'az monitor diagnostic-settings create --name mosaic' },
  ],
  apis: [
    { apiName: 'chat', displayName: 'Chat', kind: 'model', published: true, allApis: false, gaps: ['sampling', 'llmLogs'] },
    { apiName: 'tickets', displayName: 'Tickets', kind: 'mcp', published: false, allApis: true, gaps: [] },
  ],
  probe: null,
  rollup: {
    lastRunAt: generatedAt,
    lastSuccessAt: generatedAt,
    lastDurationMs: 1200,
    queriedThrough: generatedAt,
    lagMinutes: 15,
    dataAvailableFrom: '2026-02-17',
    lastError: null,
    lastErrorAt: null,
    backfillStatus: 'done',
    backfillFrom: null,
    backfillNext: null,
    lastRows: 10,
    lastWritten: 10,
    unknownTraceVersions: 0,
    diagnosticsError: null,
  },
}

export const costSummaryFixture: AnalyticsCostSummary = {
  currency: 'USD',
  total: 7235.25,
  reserved: 7200,
  pricedTokens: 1_000_000,
  unpricedTokens: 4000,
  unpricedRequests: 2,
  unpricedItems: 1,
  unpriced: [
    { key: 'endpoint-aoai/mystery', kind: 'deployment', label: 'mystery', detail: 'Contoso Azure OpenAI', reason: 'noPrice', message: 'No price for contoso-llm 1 (GlobalStandard) in eastus2 in Azure Commercial.', requests: 2, totalTokens: 4000 },
  ],
  notes: [
    "Costs are at list prices from MOSAIC's price list, before any discount, and are estimates, not a bill.",
    "The gateway's logs don't separate cached prompt tokens, so every prompt token is priced at the full input price.",
  ],
}

export const spendFixture = {
  currency: 'USD' as const,
  monthStart: '2026-03-01',
  daysInMonth: 31,
  daysElapsed: 17.68,
  through: '2026-03-18T15:30:00Z',
  monthToDate: 4355.25,
  reserved: 4320,
  forecast: 7502.1,
  projected: true,
  unpricedTokens: 4000,
  unpricedItems: 1,
}

export const costFixture: AnalyticsCost = {
  ...reportBase,
  priced: true,
  spend: spendFixture,
  cost: costSummaryFixture,
  trend: [
    { start: '2026-03-17T00:00:00Z', cost: 243.5, totalTokens: 1_200_000 },
    { start: '2026-03-18T00:00:00Z', cost: 243.75, totalTokens: 1_100_000 },
  ],
  models: [{ key: 'gpt-4o', label: 'gpt-4o', requests: 15, totalTokens: 11_400_000, cost: 4355.25, costShare: 0.6 }],
  consumers: [
    { key: 'person:bob', label: 'Bob', kind: 'person', requests: 3, totalTokens: 300_000, cost: 3240, costShare: 0.45 },
    { key: 'group:analysts', label: 'Analysts', kind: 'group', requests: 1, totalTokens: 100_000, cost: 1080, costShare: 0.15 },
  ],
  costCenters: [{ key: 'cc-support', label: 'Support', detail: 'support', kind: 'costCenter', requests: 11, totalTokens: 11_100_000, cost: 4355.25, costShare: 0.6 }],
  apis: [{ key: 'gateway-prod/reserved', label: 'Reserved', detail: 'Production gateway', requests: 4, totalTokens: 400_000, cost: 4320, costShare: 0.6 }],
  deployments: [
    { key: 'endpoint-aoai/chat', endpointId: 'endpoint-aoai', endpointName: 'Contoso Azure OpenAI', deploymentName: 'chat', modelName: 'gpt-4o', modelVersion: '2024-11-20', cloud: 'commercial', cloudLabel: 'Azure Commercial', deploymentType: 'GlobalStandard', region: 'eastus2', pricing: 'tokens', priceId: 'commercial.openai.gpt-4o.2024-11-20.globalstandard', priceOrigin: 'seed', inputPerMillion: 2.5, cachedInputPerMillion: 1.25, outputPerMillion: 10, requests: 11, promptTokens: 10_100_000, completionTokens: 1_000_000, totalTokens: 11_100_000, cost: 35.25, costShare: 0.005, gateways: 1 },
    { key: 'endpoint-aoai/reserved', endpointId: 'endpoint-aoai', endpointName: 'Contoso Azure OpenAI', deploymentName: 'reserved', modelName: 'gpt-4o', modelVersion: '2024-11-20', cloud: 'commercial', cloudLabel: 'Azure Commercial', deploymentType: 'GlobalProvisionedManaged', region: 'eastus2', pricing: 'provisioned', priceOrigin: 'seed', ptuHourly: 1, capacity: 10, monthCost: 7440, utilization: 0.0006, idleCost: 2880, requests: 4, promptTokens: 320_000, completionTokens: 80_000, totalTokens: 400_000, cost: 7200, costShare: 0.995, gateways: 1 },
    { key: 'endpoint-aoai/mystery', endpointId: 'endpoint-aoai', endpointName: 'Contoso Azure OpenAI', deploymentName: 'mystery', modelName: 'contoso-llm', modelVersion: '1', cloud: 'commercial', cloudLabel: 'Azure Commercial', deploymentType: 'GlobalStandard', region: 'eastus2', pricing: 'unpriced', unpricedReason: 'noPrice', unpricedMessage: 'No price for contoso-llm 1 (GlobalStandard) in eastus2 in Azure Commercial.', requests: 2, promptTokens: 2000, completionTokens: 2000, totalTokens: 4000, cost: null, gateways: 1 },
  ],
}

export const pricedOverviewFixture: AnalyticsOverview = {
  ...overviewFixture,
  kpis: { ...overviewFixture.kpis, cost: 7235.25 },
  cost: costSummaryFixture,
  spend: spendFixture,
  topModels: overviewFixture.topModels.map((row) => ({ ...row, cost: 4355.25 })),
}