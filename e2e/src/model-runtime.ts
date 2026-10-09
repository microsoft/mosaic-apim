import { ModelProofError, type ModelGrantTarget, type ModelJourneys } from './model-config.ts'

export interface ModelConnection {
  entitlementId: string
  publicationId: string
  gatewayId: string
  endpoint: string
  deploymentName: string
  tenantId: string
  entraAudience: string
  entraScope: string
  subscriptionHeader: string
  costCenter: { id: string; code: string }
  costCenterHeader: string
  runtime: { status: string }
  appliedMethods: { keysEnabled: boolean; entraEnabled: boolean }
  keyExists: boolean
  keysAllowedByCostCenter: boolean
  tokenMetering: boolean
  operations: { name: string; method: string; path: string }[]
  grantLimits: { tokens?: { tokensPerMinute?: number; tokenQuota?: number; tokenQuotaPeriod?: string }; requests?: { calls?: number; renewalPeriodSeconds?: number } }
  publicationLimits: { tokensPerMinute?: number } | null
}

export interface ModelReply {
  status: number
  headers: Record<string, string>
  body: unknown
}
export interface ModelObservation {
  case: string
  grant: string
  status: number
  at: string
  requestId?: string
  promptTokens?: number
  completionTokens?: number
  pricedUsageUsd?: number
  poolRemaining?: number
  ownRemaining?: number
  ownQuotaRemaining?: number
}
export type ModelTransport = (url: string, headers: Record<string, string>, body: object) => Promise<ModelReply>
export type ModelCaller = (grant: ModelGrantTarget, code: string | undefined, label: string, expected: 200 | 403 | 429 | 'pool' | 'budget' | 'budget-spend', key?: string) => Promise<ModelObservation>

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new ModelProofError('Expected model JSON object')
  return value as Record<string, unknown>
}

function count(value: unknown): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < 0) throw new ModelProofError('Unknown model token usage; no further requests are safe')
  return value
}

export function modelUsage(body: unknown): { prompt: number; completion: number; cached?: number } {
  const result = record(body)
  if (!Array.isArray(result.choices) || result.choices.length !== 1 ||
      typeof record(record(result.choices[0]).message).content !== 'string') throw new ModelProofError('200 did not contain a real chat completion')
  const usage = record(result.usage)
  const prompt = count(usage.prompt_tokens)
  const completion = count(usage.completion_tokens)
  if (prompt === 0 || count(usage.total_tokens) !== prompt + completion) throw new ModelProofError('Missing or inconsistent model usage')
  const details = usage.prompt_tokens_details === undefined ? undefined : record(usage.prompt_tokens_details)
  const cached = details === undefined ? undefined : count(details.cached_tokens)
  if (cached !== undefined && cached > prompt) throw new ModelProofError('Cached usage exceeds prompt usage')
  return { prompt, completion, cached }
}

export function remaining(headers: Record<string, string>, name: string): number {
  const value = headers[name]
  if (!/^\d+$/.test(value ?? '') || !Number.isSafeInteger(Number(value))) throw new ModelProofError(`Missing or invalid ${name}`)
  return Number(value)
}

/** No custom payloads, tools, streams, reasoning or redirects: a bounded OpenAI chat leg only. */
export function modelRoute(connection: ModelConnection, origin: string): string {
  let endpoint: URL
  try { endpoint = new URL(connection.endpoint) } catch { throw new ModelProofError('Invalid approved model endpoint') }
  if (endpoint.origin !== origin || endpoint.username || endpoint.password || endpoint.search || endpoint.hash ||
      !/^[A-Za-z0-9._-]{1,128}$/.test(connection.deploymentName)) throw new ModelProofError('Connection is outside the approved gateway route')
  const chat = connection.operations.find((op) => op.method === 'POST' && op.name === 'chat-completions')
  if (!chat || !/^\/openai\/deployments\/[A-Za-z0-9._-]+\/chat\/completions$/.test(chat.path) ||
      chat.path.split('/')[3] !== connection.deploymentName) throw new ModelProofError('Only a deployment-scoped Azure OpenAI chat-completions route is supported')
  return `${endpoint.href.replace(/\/$/, '')}${chat.path}?api-version=2024-10-21`
}

export function connectionProblems(c: ModelConnection, grant: ModelGrantTarget, scope: ModelJourneys): string[] {
  const problems: string[] = []
  if (c.entitlementId !== grant.id || c.publicationId !== scope.publicationId || c.gatewayId !== scope.gatewayId ||
      c.costCenter?.id !== grant.costCenterId || typeof c.costCenter?.code !== 'string' ||
      c.costCenter.code.toLowerCase() !== grant.code.toLowerCase()) problems.push('Connection does not name the exact isolated grant/publication/cost center')
  if (c.runtime?.status !== 'applied' || c.appliedMethods?.entraEnabled !== true || c.tokenMetering !== true) problems.push('Grant must have applied Entra access and model token metering')
  if (c.costCenterHeader !== 'x-mosaic-cost-center') problems.push('Connection has no supported cost-center header')
  const own = c.grantLimits?.tokens
  if (!own?.tokensPerMinute || own.tokensPerMinute <= scope.pool.monthlyTokens ||
      !own.tokenQuota || own.tokenQuota <= scope.pool.monthlyTokens || own.tokenQuotaPeriod !== 'Monthly') problems.push('Own TPM and monthly quota must exceed the pool, for independent counters')
  if (!c.publicationLimits?.tokensPerMinute || !own?.tokensPerMinute || c.publicationLimits.tokensPerMinute < own.tokensPerMinute) problems.push('Keep an applied publication safeguard at least as high as each grant TPM')
  const requests = c.grantLimits?.requests
  if (requests?.calls && requests.renewalPeriodSeconds &&
      requests.calls <= Math.ceil(requests.renewalPeriodSeconds / scope.bounds.intervalSeconds)) problems.push('Own request window can mask the pool or budget proof; prepare a higher bounded grant limit')
  return problems
}

export const prompt = 'Reply with OK.'
// Conservative upper bound for this fixed ASCII chat message, including chat framing.
export const promptReservation = 160
export const budgetPrompt = `${prompt}\n${'. '.repeat(240)}`
export const budgetPromptReservation = promptReservation + Buffer.byteLength(budgetPrompt, 'utf8')

export class ModelAllowance {
  #requests = 0
  #prompt = 0
  #usd = 0
  #stopped = false
  readonly #scope: ModelJourneys
  readonly #start = Date.now()

  constructor(scope: ModelJourneys) {
    this.#scope = scope
    if (![scope.price.inputPerMillion, scope.price.outputPerMillion].every((n) => Number.isFinite(n) && n > 0)) {
      throw new ModelProofError('Unknown price; requests are not safe')
    }
    if (!Number.isFinite(scope.price.cachedInputPerMillion) || scope.price.cachedInputPerMillion < 0 ||
        scope.price.cachedInputPerMillion > scope.price.inputPerMillion) throw new ModelProofError('Unknown cached price; requests are not safe')
  }

  reserve(promptTokens = promptReservation): void {
    const { bounds, price } = this.#scope
    const usd = (promptTokens * price.inputPerMillion + bounds.maxOutputTokens * price.outputPerMillion) / 1_000_000
    if ((this.#scope.approval && Date.now() >= Date.parse(this.#scope.approval.expiresAt)) ||
        this.#stopped || this.#requests + 1 > bounds.maxRequests || this.#prompt + promptTokens > bounds.maxPromptTokens ||
        this.#usd + usd > bounds.maxUsd || Date.now() - this.#start >= bounds.timeoutSeconds * 1000) {
      throw new ModelProofError('Request/token/spend/time bound reached BEFORE sending; proof incomplete')
    }
    this.#requests += 1
    this.#prompt += promptTokens
    this.#usd += usd
  }

  stop(): void { this.#stopped = true }
  summary(): { requests: number; reservedPromptTokens: number; reservedUsd: number } {
    return { requests: this.#requests, reservedPromptTokens: this.#prompt, reservedUsd: this.#usd }
  }
}

export function boundedModelCaller(
  scope: ModelJourneys,
  origin: string,
  connections: ReadonlyMap<string, ModelConnection>,
  tokens: ReadonlyMap<string, string>,
  transport: ModelTransport,
  allowance: ModelAllowance,
  observe: (entry: ModelObservation) => void,
): ModelCaller {
  return async (grant, code, label, expected, key) => {
    const connection = connections.get(grant.id)
    const token = tokens.get(grant.persona)
    if (!connection || !token) throw new ModelProofError('Missing approved in-memory connection or runtime credential')
    const url = modelRoute(connection, origin)
    const headers: Record<string, string> = { 'Content-Type': 'application/json' }
    if (key === undefined) headers.Authorization = `Bearer ${token}`
    else {
      if (scope.approval?.revealExistingKey !== true || connection.appliedMethods.keysEnabled !== true ||
          !connection.keyExists || !connection.keysAllowedByCostCenter || connection.subscriptionHeader !== 'Ocp-Apim-Subscription-Key') {
        throw new ModelProofError('Existing key method is not explicitly approved and ready')
      }
      headers[connection.subscriptionHeader] = key
    }
    if (code !== undefined) headers['x-mosaic-cost-center'] = code
    const isBudget = expected === 'budget' || expected === 'budget-spend'
    const reservedPrompt = expected === 'budget-spend' ? budgetPromptReservation : promptReservation
    allowance.reserve(reservedPrompt)
    let reply: ModelReply
    try {
      reply = await transport(url, headers, { model: connection.deploymentName, messages: [{ role: 'user', content: expected === 'budget-spend' ? budgetPrompt : prompt }], max_tokens: scope.bounds.maxOutputTokens, stream: false })
    } catch {
      allowance.stop()
      throw new ModelProofError('Model transport failed; usage is unknown and no automatic retry is allowed')
    }
    const entry: ModelObservation = { case: label, grant: grant.id, status: reply.status, at: new Date().toISOString() }
    const requestId = reply.headers['apim-request-id']
    if (requestId && /^[a-zA-Z0-9-]{1,128}$/.test(requestId)) entry.requestId = requestId
    try {
      const statuses = expected === 'pool' ? [200, 429] : isBudget ? [200, 403] : [expected]
      if (!statuses.includes(reply.status)) throw new ModelProofError(`${label}: expected ${expected}, received ${reply.status}; stopped`)
      if (reply.status === 200) {
        const usage = modelUsage(reply.body)
        if (usage.prompt > reservedPrompt || usage.completion > scope.bounds.maxOutputTokens) throw new ModelProofError('Model exceeded its reserved token bound')
        entry.promptTokens = usage.prompt
        entry.completionTokens = usage.completion
        if (isBudget) {
          if (usage.cached === undefined) throw new ModelProofError('Budget calls require measured cached-token usage; unknown usage must stop')
          entry.pricedUsageUsd = ((usage.prompt - usage.cached) * scope.price.inputPerMillion +
            usage.cached * scope.price.cachedInputPerMillion + usage.completion * scope.price.outputPerMillion) / 1_000_000
        }
      } else if (reply.body && typeof reply.body === 'object' && record(reply.body).usage !== undefined) {
        throw new ModelProofError('A denied request reported billable usage; stopped')
      }
      if (isBudget && reply.status === 403 &&
          (typeof reply.body !== 'string' || !reply.body.includes(`Cost center ${grant.code.toLowerCase()} has used its monthly budget`))) {
        throw new ModelProofError('Budget 403 does not name the selected cost center and budget; independent denial trace still required')
      }
      for (const [name, field] of [
        ['x-mosaic-cost-center-remaining-quota-tokens', 'poolRemaining'],
        ['x-mosaic-remaining-tokens', 'ownRemaining'],
        ['x-mosaic-remaining-quota-tokens', 'ownQuotaRemaining'],
      ] as const) if (reply.headers[name] !== undefined) entry[field] = remaining(reply.headers, name)
      observe(entry)
      return entry
    } catch (error) {
      allowance.stop()
      observe(entry)
      throw error
    }
  }
}

export async function selectionProof(scope: ModelJourneys, call: ModelCaller, key: string): Promise<ModelObservation[]> {
  const { default: primary, other } = scope.selection
  const entries: ModelObservation[] = []
  for (const [grant, code, label, expected, credential] of [
    [primary, primary.code, 'entra-default-explicit', 200],
    [other, other.code.toUpperCase(), 'entra-other-case-insensitive', 200],
    [primary, undefined, 'entra-default-implicit', 200],
    [primary, `${scope.ownerTag}-unknown`, 'entra-unknown', 403],
    [primary, 'invalid,code', 'entra-malformed', 403],
    [primary, other.code, 'key-other', 403, key],
    [primary, primary.code.toUpperCase(), 'key-own-case-insensitive', 200, key],
    [primary, undefined, 'key-implicit', 200, key],
  ] as const) entries.push(await call(grant, code, label, expected, credential))
  return entries
}

export async function pooledTokenProof(
  scope: ModelJourneys,
  connections: ReadonlyMap<string, ModelConnection>,
  call: ModelCaller,
  pause: () => Promise<void>,
): Promise<ModelObservation[]> {
  const grants = [scope.pool.first, scope.pool.second]
  const entries: ModelObservation[] = []
  const ownUsed = new Map<string, number>()
  let poolUsed = 0
  let exhausted = false
  for (let index = 0; index < scope.bounds.maxRequests - 3; index += 1) {
    const grant = grants[index % 2]
    // The gateway's prompt estimator may refuse the next fixed prompt before the counter reaches zero.
    const entry = await call(grant, grant.code, `pool-${index + 1}`, 'pool')
    entries.push(entry)
    if (entry.status === 429) {
      if (index < 2) throw new ModelProofError('Pool too small to prove consumption by both callers with this conservative prompt bound')
      if (entry.poolRemaining === undefined || entry.poolRemaining >= promptReservation ||
          entry.ownRemaining === undefined || entry.ownRemaining < promptReservation ||
          entry.ownQuotaRemaining === undefined || entry.ownQuotaRemaining < promptReservation) {
        throw new ModelProofError('429 does not prove exhaustion of the pool rather than the grant counters')
      }
      exhausted = true
      break
    }
    if (entry.promptTokens === undefined || entry.completionTokens === undefined ||
        !Number.isSafeInteger(entry.promptTokens) || entry.promptTokens <= 0 ||
        !Number.isSafeInteger(entry.completionTokens) || entry.completionTokens < 0) {
      throw new ModelProofError('Unknown pooled model usage; never substitute zero')
    }
    const used = entry.promptTokens + entry.completionTokens
    poolUsed += used
    const own = (ownUsed.get(grant.id) ?? 0) + used
    ownUsed.set(grant.id, own)
    const quota = connections.get(grant.id)?.grantLimits.tokens?.tokenQuota
    if (entry.poolRemaining !== scope.pool.monthlyTokens - poolUsed ||
        quota === undefined || entry.ownQuotaRemaining !== quota - own || entry.ownRemaining === undefined || entry.ownRemaining <= 0) {
      throw new ModelProofError('Pool is not one fresh shared counter, or own token counters are not independent')
    }
    await pause()
  }
  if (!exhausted || ownUsed.size !== 2) throw new ModelProofError('Pool exhaustion was not proved across both distinct grants')
  for (const [index, grant] of grants.entries()) {
    const entry = await call(grant, grant.code, `pool-exhausted-${index + 1}`, 429)
    if (entry.poolRemaining === undefined || entry.poolRemaining >= promptReservation ||
        entry.ownRemaining === undefined || entry.ownRemaining < promptReservation ||
        entry.ownQuotaRemaining === undefined || entry.ownQuotaRemaining < promptReservation) throw new ModelProofError('Both exhausted callers must report pool and independent own counters')
    entries.push(entry)
  }
  const control = scope.selection.other
  entries.push(await call(control, control.code, 'pool-other-cost-center', 200))
  return entries
}
