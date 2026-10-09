import { createHash } from 'node:crypto'
import type { Targets } from './config.ts'

export type ModelJourney = 'R10' | 'R11' | 'R12' | 'R14'
export interface ModelGrantTarget {
  id: string
  persona: string
  costCenterId: string
  code: string
}

export interface ModelJourneys {
  ownerTag: string
  publicationId: string
  modelApiId: string
  gatewayId: string
  /** Reviewed ARM model facts and date-specific price, NOT the deployment alias. */
  price: {
    actualModel: string
    version: string
    deploymentType: string
    region: string
    date: string
    inputPerMillion: number
    cachedInputPerMillion: number
    outputPerMillion: number
    evidenceReference: string
  }
  bounds: {
    maxRequests: number
    maxPromptTokens: number
    maxOutputTokens: number
    maxUsd: number
    intervalSeconds: number
    timeoutSeconds: number
  }
  selection: { default: ModelGrantTarget; other: ModelGrantTarget }
  pool: { first: ModelGrantTarget; second: ModelGrantTarget; monthlyTokens: number }
  budget?: {
    grant: ModelGrantTarget
    amount: number
    raisedAmount: number
    managedGateways: { id: string; tier: 'classic' | 'v2' }[]
  }
  approval?: {
    reference: string
    expiresAt: string
    scopeSha256: string
    journeys: ModelJourney[]
    revealExistingKey: boolean
    budgetWritesAcrossAllManagedGateways: boolean
  }
}

export class ModelProofError extends Error {}

function object(value: unknown, keys: readonly string[], path: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ModelProofError(`${path} must be an object`)
  const result = value as Record<string, unknown>
  if (Object.keys(result).some((key) => !keys.includes(key))) throw new ModelProofError(`${path} has an unknown field (secrets do not belong in the manifest)`)
  return result
}

function text(value: unknown, path: string, pattern = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/): string {
  if (typeof value !== 'string' || !pattern.test(value)) throw new ModelProofError(`${path} has an invalid value`)
  return value
}

function number(value: unknown, path: string, min: number, max: number, integer = true): number {
  if (typeof value !== 'number' || !Number.isFinite(value) || value < min || value > max || (integer && !Number.isInteger(value))) {
    throw new ModelProofError(`${path} must be ${integer ? 'an integer' : 'a number'} from ${min} to ${max}`)
  }
  return value
}

export function cents(value: number): number {
  if (!Number.isFinite(value) || value < 0) throw new ModelProofError('Unknown or negative spend')
  // Python's round(), used by the budget service, rounds ties to even.
  const scaled = value * 100
  const floor = Math.floor(scaled)
  if (Math.abs(scaled - floor - 0.5) < 1e-9) return floor % 2 === 0 ? floor : floor + 1
  return Math.round(scaled)
}

export function parseModelJourneys(value: unknown, personas: Targets['personas']): ModelJourneys {
  const path = 'targets.modelJourneys'
  const root = object(value, ['ownerTag', 'publicationId', 'modelApiId', 'gatewayId', 'price', 'bounds', 'selection', 'pool', 'budget', 'approval'], path)
  const ownerTag = text(root.ownerTag, `${path}.ownerTag`, /^e2e-model-[a-z0-9-]{1,40}$/)
  const grant = (value: unknown): ModelGrantTarget => {
    const g = object(value, ['id', 'persona', 'costCenterId', 'code'], `${path}.grant`)
    const persona = text(g.persona, `${path}.grant.persona`)
    if (!Object.hasOwn(personas, persona) || personas[persona].expectedRole !== 'User') throw new ModelProofError('Model journey holders must be named User personas')
    return {
      id: text(g.id, `${path}.grant.id`),
      persona,
      costCenterId: text(g.costCenterId, `${path}.grant.costCenterId`),
      code: text(g.code, `${path}.grant.code`, new RegExp(`^${ownerTag}-[a-z0-9-]{1,16}$`)),
    }
  }
  const selection = object(root.selection, ['default', 'other'], `${path}.selection`)
  const pool = object(root.pool, ['first', 'second', 'monthlyTokens'], `${path}.pool`)
  const price = object(root.price, ['actualModel', 'version', 'deploymentType', 'region', 'date', 'inputPerMillion', 'cachedInputPerMillion', 'outputPerMillion', 'evidenceReference'], `${path}.price`)
  const bounds = object(root.bounds, ['maxRequests', 'maxPromptTokens', 'maxOutputTokens', 'maxUsd', 'intervalSeconds', 'timeoutSeconds'], `${path}.bounds`)
  const result: ModelJourneys = {
    ownerTag,
    publicationId: text(root.publicationId, `${path}.publicationId`),
    modelApiId: text(root.modelApiId, `${path}.modelApiId`),
    gatewayId: text(root.gatewayId, `${path}.gatewayId`),
    selection: { default: grant(selection.default), other: grant(selection.other) },
    pool: { first: grant(pool.first), second: grant(pool.second), monthlyTokens: number(pool.monthlyTokens, `${path}.pool.monthlyTokens`, 20, 500) },
    price: {
      actualModel: text(price.actualModel, `${path}.price.actualModel`),
      version: text(price.version, `${path}.price.version`),
      deploymentType: text(price.deploymentType, `${path}.price.deploymentType`),
      region: text(price.region, `${path}.price.region`),
      date: text(price.date, `${path}.price.date`, /^\d{4}-\d{2}-\d{2}$/),
      inputPerMillion: number(price.inputPerMillion, `${path}.price.inputPerMillion`, 0.000001, 100_000, false),
      cachedInputPerMillion: number(price.cachedInputPerMillion, `${path}.price.cachedInputPerMillion`, 0, 100_000, false),
      outputPerMillion: number(price.outputPerMillion, `${path}.price.outputPerMillion`, 0.000001, 100_000, false),
      evidenceReference: text(price.evidenceReference, `${path}.price.evidenceReference`),
    },
    bounds: {
      maxRequests: number(bounds.maxRequests, `${path}.bounds.maxRequests`, 10, 80),
      maxPromptTokens: number(bounds.maxPromptTokens, `${path}.bounds.maxPromptTokens`, 100, 32_000),
      maxOutputTokens: number(bounds.maxOutputTokens, `${path}.bounds.maxOutputTokens`, 1, 8),
      maxUsd: number(bounds.maxUsd, `${path}.bounds.maxUsd`, 0.000001, 0.02, false),
      intervalSeconds: number(bounds.intervalSeconds, `${path}.bounds.intervalSeconds`, 10, 300),
      timeoutSeconds: number(bounds.timeoutSeconds, `${path}.bounds.timeoutSeconds`, 60, 3600),
    },
  }
  if (root.budget !== undefined) {
    const b = object(root.budget, ['grant', 'amount', 'raisedAmount', 'managedGateways'], `${path}.budget`)
    if (!Array.isArray(b.managedGateways) || b.managedGateways.length === 0) throw new ModelProofError('Budget scope must list ALL managed gateways')
    const managedGateways = b.managedGateways.map((item): { id: string; tier: 'classic' | 'v2' } => {
      const gateway = object(item, ['id', 'tier'], `${path}.budget.managedGateways`)
      if (gateway.tier !== 'classic' && gateway.tier !== 'v2') throw new ModelProofError('Gateway tier must be classic or v2')
      return { id: text(gateway.id, 'gateway.id'), tier: gateway.tier }
    })
    const amount = number(b.amount, 'budget.amount', 0.01, 1, false)
    const raisedAmount = number(b.raisedAmount, 'budget.raisedAmount', 0.02, 10, false)
    if (cents(raisedAmount) <= cents(amount)) throw new ModelProofError('Raised budget must be higher in rounded cents')
    if (new Set(managedGateways.map((g) => g.id)).size !== managedGateways.length) throw new ModelProofError('List each managed gateway once')
    if (!managedGateways.some((g) => g.id === result.gatewayId)) throw new ModelProofError('Budget scope must include the called gateway')
    result.budget = { grant: grant(b.grant), amount, raisedAmount, managedGateways }
    if (result.budget.grant.persona !== result.selection.default.persona) throw new ModelProofError('Budget and other-center control must have the same holder')
  }
  const all = modelGrants(result)
  if (result.price.cachedInputPerMillion > result.price.inputPerMillion) throw new ModelProofError('Cached price cannot exceed the reserved input price')
  if (new Set(all.map((g) => g.id)).size !== all.length) throw new ModelProofError('Every model grant must be distinct')
  if (result.selection.default.persona !== result.selection.other.persona || result.selection.default.costCenterId === result.selection.other.costCenterId) {
    throw new ModelProofError('Selection needs the same holder under two different cost centers')
  }
  if (result.pool.first.costCenterId !== result.pool.second.costCenterId || result.pool.first.code !== result.pool.second.code ||
      personas[result.pool.first.persona].upn.toLowerCase() === personas[result.pool.second.persona].upn.toLowerCase()) {
    throw new ModelProofError('Pool proof needs two distinct callers under the same cost center')
  }
  const centers = [result.selection.default, result.selection.other, result.pool.first, ...(result.budget ? [result.budget.grant] : [])]
  if (new Set(centers.map((g) => g.costCenterId)).size !== centers.length || new Set(centers.map((g) => g.code)).size !== centers.length) {
    throw new ModelProofError('Selection, pool and budget centers must be isolated from each other')
  }
  if (root.approval !== undefined) {
    const a = object(root.approval, ['reference', 'expiresAt', 'scopeSha256', 'journeys', 'revealExistingKey', 'budgetWritesAcrossAllManagedGateways'], `${path}.approval`)
    if (!Array.isArray(a.journeys) || a.journeys.length === 0 || a.journeys.some((j) => !['R10', 'R11', 'R12', 'R14'].includes(j))) throw new ModelProofError('Approval journeys must name supported model proofs')
    if (typeof a.revealExistingKey !== 'boolean' || typeof a.budgetWritesAcrossAllManagedGateways !== 'boolean') throw new ModelProofError('Approval capabilities must be explicit booleans')
    result.approval = {
      reference: text(a.reference, 'approval.reference'),
      expiresAt: text(a.expiresAt, 'approval.expiresAt', /^\d{4}-\d{2}-\d{2}T[\d:.]+Z$/),
      scopeSha256: text(a.scopeSha256, 'approval.scopeSha256', /^[0-9a-f]{64}$/),
      journeys: a.journeys as ModelJourney[],
      revealExistingKey: a.revealExistingKey,
      budgetWritesAcrossAllManagedGateways: a.budgetWritesAcrossAllManagedGateways,
    }
  }
  return result
}

export function modelGrants(scope: ModelJourneys): ModelGrantTarget[] {
  return [scope.selection.default, scope.selection.other, scope.pool.first, scope.pool.second, ...(scope.budget ? [scope.budget.grant] : [])]
}

export function modelScopeHash(targets: Targets): string {
  const scope = targets.modelJourneys
  if (!scope) throw new ModelProofError('Missing modelJourneys')
  const { approval: _approval, ...owned } = scope
  return createHash('sha256').update(JSON.stringify({
    protocolVersion: 1, owned, origins: targets.origins, tenantId: targets.tenantId,
    admin: targets.personas[targets.roles.admin],
    holders: modelGrants(scope).map((g) => targets.personas[g.persona]),
  })).digest('hex')
}

/** This records an owner's decision; it is not a way to obtain or infer approval. */
export function modelReadiness(targets: Targets, journey: ModelJourney, env: NodeJS.ProcessEnv, now = Date.now()): string[] {
  const scope = targets.modelJourneys
  if (!scope) return ['Missing isolated targets.modelJourneys; no live work authorized']
  const problems: string[] = []
  if (env.MOSAIC_E2E_MODEL_SCOPE !== scope.ownerTag) problems.push('Set MOSAIC_E2E_MODEL_SCOPE to the exact approved ownerTag; generic flags do not authorize these proofs')
  if (env.MOSAIC_E2E_MODEL_JOURNEY !== journey) problems.push('Choose one exact MOSAIC_E2E_MODEL_JOURNEY per invocation; request/token/spend bounds are not reset across a batch')
  const approval = scope.approval
  if (!approval || !approval.journeys.includes(journey) || approval.scopeSha256 !== modelScopeHash(targets) ||
      !Number.isFinite(Date.parse(approval.expiresAt)) || Date.parse(approval.expiresAt) <= now) {
    problems.push('Missing, expired or changed exact-scope owner approval')
  }
  if (scope.price.date !== new Date(now).toISOString().slice(0, 10)) problems.push('Review actual-model/type/region/date pricing for this UTC day before billed calls')
  if (journey === 'R10' && approval?.revealExistingKey !== true) problems.push('R10 needs explicit approval to reveal an existing isolated key (no key creation)')
  if (journey === 'R12' || journey === 'R14') {
    if (!scope.budget) problems.push('Missing isolated budget fixture')
    if (approval?.budgetWritesAcrossAllManagedGateways !== true) problems.push('Budget approval must acknowledge shared named-value writes on ALL managed gateways, including Staging and Production')
  }
  return problems
}

/** Unlike initial readiness skips, lost approval during a run must fail before the next action. */
export function assertModelReadiness(targets: Targets, journey: ModelJourney, env: NodeJS.ProcessEnv, now = Date.now()): void {
  const problems = modelReadiness(targets, journey, env, now)
  if (problems.length) throw new ModelProofError(`Model approval/readiness changed before sending or saving: ${problems.join('; ')}`)
}
