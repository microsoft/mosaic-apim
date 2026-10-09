import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { parseTargets } from '../src/config.ts'
import type { ModelBudgetView } from '../src/model-budget.ts'
import type { ModelGrantTarget, ModelJourneys } from '../src/model-config.ts'
import type { ModelConnection, ModelObservation, ModelReply } from '../src/model-runtime.ts'
import { e2eRoot } from '../src/paths.ts'

export function modelTargets() {
  return parseTargets(JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')))
}
export const modelScope = () => modelTargets().modelJourneys!
export function connection(g: ModelGrantTarget, scope = modelScope()): ModelConnection {
  return {
    entitlementId: g.id, publicationId: scope.publicationId, gatewayId: scope.gatewayId,
    endpoint: 'https://gateway.invalid/proof', deploymentName: 'deployment-alias',
    tenantId: '00000000-0000-0000-0000-000000000000', entraAudience: '11111111-1111-1111-1111-111111111111', entraScope: 'api://11111111-1111-1111-1111-111111111111/Models.Invoke',
    subscriptionHeader: 'Ocp-Apim-Subscription-Key', costCenter: { id: g.costCenterId, code: g.code },
    costCenterHeader: 'x-mosaic-cost-center', runtime: { status: 'applied' },
    appliedMethods: { keysEnabled: true, entraEnabled: true }, keyExists: true, keysAllowedByCostCenter: true, tokenMetering: true,
    operations: [{ method: 'POST', name: 'chat-completions', path: '/openai/deployments/deployment-alias/chat/completions' }],
    grantLimits: { tokens: { tokensPerMinute: 2000, tokenQuota: 1000, tokenQuotaPeriod: 'Monthly' } },
    publicationLimits: { tokensPerMinute: 12000 },
  }
}
export const success = (): ModelReply => ({
  status: 200, headers: {},
  body: { choices: [{ message: { role: 'assistant', content: 'OK' } }], usage: { prompt_tokens: 12, completion_tokens: 2, total_tokens: 14, prompt_tokens_details: { cached_tokens: 0 } } },
})
export const observation = (grant: ModelGrantTarget, label: string, status: number): ModelObservation => ({
  case: label, grant: grant.id, status, at: '2026-10-01T12:00:00.000Z', requestId: `request-${label}`,
  pricedUsageUsd: status === 200 ? 0.00001 : undefined,
})
export function budget(scope: ModelJourneys, spend: number, blocked = false): ModelBudgetView {
  return {
    id: 'budget_fictional', scope: 'costCenter', costCenter: { id: scope.budget!.grant.costCenterId, name: 'Fictional budget', code: scope.budget!.grant.code },
    amount: blocked ? scope.budget!.amount : scope.budget!.raisedAmount, action: 'block', recipients: [], notifyOwners: false,
    status: {
      month: new Date().toISOString().slice(0, 7), monthToDate: spend, unpricedTokens: 0, through: new Date().toISOString(),
      blocked, error: null, crossed: blocked ? [80, 100] : [], notifications: [],
      gateways: scope.budget!.managedGateways.map((g) => ({ gatewayId: g.id, enforcing: blocked, syncedAt: new Date().toISOString(), error: null })),
    },
  }
}
