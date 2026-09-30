import type { SuiteLimits, SuiteModel, SuiteRuntime, SuiteTargets, Targets } from './config.ts'
import { modelLabel, sameModel } from './config.ts'
import type { PersonaPool } from './fixtures.ts'
import type {
  AccessMethods,
  ApiEnforcement,
  ApiEntitlement,
  ApiGateway,
  ApiModelApi,
  ApiPrincipal,
  ApiPublication,
  ApiResolvedEntitlement,
  MosaicApi,
} from './mosaic-api.ts'
import { personaObjectId } from './mosaic-api.ts'

/**
 * What the ordered specs share: the note that marks what the suite created, the rules for reusing a throwaway
 * grant, the skip reasons, and resolving the manifest's `suite` section to MOSAIC's records. MOSAIC's IDs are
 * looked up again in every test, because Playwright starts a new worker after a failure.
 */

export const suiteMarker = 'Created by the MOSAIC e2e suite'

/** The note on every grant the suite adds. 90-cleanup and the next run recognize the suite's grants by it. */
export function suiteNote(journey: string): string {
  return `${suiteMarker} (${journey}). Safe to revoke.`
}

export function isSuiteGrant(grant: Pick<ApiEntitlement, 'notes'>): boolean {
  return (grant.notes ?? '').includes(suiteMarker)
}

export const cleanupHint = 'Run 90-cleanup on its own to put the disposable targets back, then run this spec again.'

/** The skip reason for a journey whose part of the manifest's `suite` section is missing. */
export function missingSuite(field: string): string {
  return `The targets manifest has no suite.${field}, so this journey is skipped. docs/e2e/runbook.md, "Run the ordered suite", describes the field.`
}

const unset = (value: number | null | undefined) => value ?? undefined

/**
 * True if a grant's limits are exactly the manifest's: the same tokens per minute and call rate, and no token
 * or call quota, which the suite never sets.
 */
export function limitsMatch(enforcement: ApiEnforcement | null | undefined, limits: SuiteLimits): boolean {
  const tokens = enforcement?.tokens
  const requests = enforcement?.requests
  return (
    unset(tokens?.tokensPerMinute) === limits.tokensPerMinute &&
    unset(tokens?.tokenQuota) === undefined &&
    unset(requests?.calls) === limits.calls &&
    unset(requests?.callQuota) === undefined &&
    (limits.calls === undefined || unset(requests?.renewalPeriodSeconds) === limits.perSeconds)
  )
}

export function describeLimits(limits: SuiteLimits): string {
  const parts = [
    limits.tokensPerMinute === undefined ? undefined : `${limits.tokensPerMinute} tokens per minute`,
    limits.calls === undefined ? undefined : `${limits.calls} calls per ${limits.perSeconds} seconds`,
  ].filter((part) => part !== undefined)
  return parts.length > 0 ? parts.join(' and ') : 'no limits'
}

export type ThrowawayGrant =
  | { action: 'add' }
  | { action: 'keep'; grant: ApiEntitlement }
  | { action: 're-enable'; grant: ApiEntitlement }
  | { action: 'skip'; reason: string }

export interface ThrowawayOptions {
  /**
   * The model is the suite's disposable publication, so every grant on it is the suite's, including one an
   * approved access request created, which has no note.
   */
  ownedModel?: boolean
}

/**
 * What to do about the throwaway grant a journey needs, given the grants the persona already holds on the model.
 * MOSAIC keeps one grant per person and model, and a grant that has been applied stays until its model is
 * unpublished, so the suite reuses its own grant: it re-enables a revoked one and adds one only if there is
 * none. A grant the suite didn't create, or one with other limits, is left alone.
 */
export function throwawayGrant(
  grants: readonly ApiEntitlement[],
  limits: SuiteLimits,
  who: string,
  options: ThrowawayOptions = {},
): ThrowawayGrant {
  if (grants.length === 0) return { action: 'add' }
  if (grants.length > 1) return { action: 'skip', reason: `${who} holds ${grants.length} grants on this model, so the suite leaves them alone.` }
  const [grant] = grants
  if (!options.ownedModel && !isSuiteGrant(grant)) {
    return {
      action: 'skip',
      reason: `${who} already holds a grant on this model that the suite didn't create, so the suite leaves it alone. Name another persona or model in the manifest.`,
    }
  }
  if (!limitsMatch(grant.enforcement, limits)) {
    return {
      action: 'skip',
      reason: `${who}'s suite grant on this model doesn't have the manifest's limits (${describeLimits(limits)}). Change the manifest to match it, or name another model.`,
    }
  }
  return grant.enabled ? { action: 'keep', grant } : { action: 're-enable', grant }
}

/** True once a grant's applied state matches its intent: enabled and applied, or disabled and revoked. */
export function grantSettled(grant: Pick<ApiEntitlement, 'enabled' | 'runtime'>): boolean {
  const status = grant.runtime?.status
  return grant.enabled ? status === 'applied' : status === 'revoked'
}

/** The access methods a runtime journey needs on a model, and why. */
export interface MethodNeeds {
  keys: boolean
  entra: boolean
}

export function methodProblems(publication: Pick<ApiPublication, 'appliedAccess' | 'accessState'>, needs: MethodNeeds): string[] {
  const applied = publication.appliedAccess?.settings
  const problems: string[] = []
  if (publication.accessState !== 'applied') problems.push(`its access reads ${publication.accessState}, not applied`)
  if (needs.keys && !applied?.keysEnabled) problems.push('subscription keys are off')
  if (needs.entra && !applied?.entraEnabled) problems.push('Microsoft Entra tokens are off')
  return problems
}

/** A model's access methods in the console's words, as the governed-access card and the connection dialog show them. */
export function methodsLabel(methods: AccessMethods | null | undefined): string {
  if (!methods) return 'Not applied'
  if (methods.keysEnabled && methods.entraEnabled) return 'Subscription key OR Entra token'
  if (methods.keysEnabled) return 'Subscription key only'
  if (methods.entraEnabled) return 'Entra token only'
  return 'Deny all — both methods disabled'
}

export interface ResolvedModel {
  model: SuiteModel
  label: string
  publication?: ApiPublication
  modelApi?: ApiModelApi
}

/** Every model the manifest keeps published, in the manifest's order. */
export function keptModels(targets: Pick<Targets, 'endpoints'>): SuiteModel[] {
  return Object.entries(targets.endpoints).flatMap(([endpoint, target]) => target.publish.map((deployment) => ({ endpoint, deployment })))
}

/** The models the runtime journeys rely on, each once: the user's grants, then the workload's and the foreign grant's. */
export function runtimeModels(runtime: SuiteRuntime): SuiteModel[] {
  const models: SuiteModel[] = []
  for (const grant of [...runtime.userGrants, runtime.applicationGrant, runtime.foreignGrant]) {
    if (grant && !models.some((model) => sameModel(model, grant))) models.push({ endpoint: grant.endpoint, deployment: grant.deployment })
  }
  return models
}

/** The suite's gateway, or a skip reason. */
export async function suiteGateway(api: MosaicApi, suite: SuiteTargets | undefined): Promise<ApiGateway | string> {
  if (!suite) return missingSuite('gateway')
  const gateway = await api.gateway(suite.gateway)
  return gateway ?? `MOSAIC has no gateway named ${suite.gateway}, which suite.gateway names.`
}

/** A manifest model's publication on the gateway and the model API it materialized, if they exist. */
export async function resolveModel(api: MosaicApi, model: SuiteModel, gatewayId: string, all?: ApiPublication[]): Promise<ResolvedModel> {
  const publication = await api.publicationOf(model, gatewayId, all)
  const modelApi = publication ? await api.modelApiOf(publication) : undefined
  return { model, label: modelLabel(model), publication, modelApi }
}

/** The MOSAIC principal of a persona, if MOSAIC has registered them. */
export async function personaPrincipal(
  api: MosaicApi,
  personas: PersonaPool,
  targets: Targets,
  personaKey: string,
): Promise<ApiPrincipal | undefined> {
  return api.principalByObjectId(await personaObjectId(personas, targets, personaKey))
}

/** A persona's grant on a model API, if they hold one. */
export async function grantOf(
  api: MosaicApi,
  personas: PersonaPool,
  targets: Targets,
  personaKey: string,
  modelApiId: string,
): Promise<ApiEntitlement | undefined> {
  const principal = await personaPrincipal(api, personas, targets, personaKey)
  if (!principal) return undefined
  const grants = await api.grantsOf(principal.id, modelApiId)
  return grants[0]
}

/** Short, stable text the suite writes, so a run can find its own request among others. */
export function suiteJustification(journey: string, runTag: string): string {
  return `E2E suite ${journey} request ${runTag}. Safe to deny.`
}

/** A tag that tells this run's writes apart from an earlier run's, such as 20260930-091500. */
export function runTag(now: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${now.getUTCFullYear()}${pad(now.getUTCMonth() + 1)}${pad(now.getUTCDate())}-${pad(now.getUTCHours())}${pad(now.getUTCMinutes())}${pad(now.getUTCSeconds())}`
}

/**
 * The start of the display name the suite gives its disposable publication. Cleanup acts only on a publication
 * and model API whose name starts with it, so it can never remove a model someone else published.
 */
export const disposablePrefix = 'E2E suite '

export function disposableDisplayName(model: SuiteModel): string {
  return `${disposablePrefix}${model.endpoint} ${model.deployment}`
}

export function isDisposableName(displayName: string | null | undefined): boolean {
  return (displayName ?? '').startsWith(disposablePrefix)
}

const periodLabels: Readonly<Record<string, string>> = {
  Hourly: 'hour',
  Daily: 'day',
  Weekly: 'week',
  Monthly: 'month',
  Yearly: 'year',
}

const formatNumber = (value: number) => new Intl.NumberFormat('en-US').format(value)

/** The limits the portal's My access card lists for a grant, in the same words and order. */
export function limitLines(enforcement: ApiEnforcement | null | undefined): string[] {
  const none = ['No additional grant limits configured']
  if (!enforcement) return none
  const { tokens, requests } = enforcement
  const lines: string[] = []
  if (tokens?.tokensPerMinute != null) lines.push(`${formatNumber(tokens.tokensPerMinute)} tokens per minute`)
  if (tokens?.tokenQuota != null && tokens.tokenQuotaPeriod) {
    lines.push(`${formatNumber(tokens.tokenQuota)} tokens per ${periodLabels[tokens.tokenQuotaPeriod] ?? tokens.tokenQuotaPeriod}`)
  }
  if (requests?.calls != null && requests.renewalPeriodSeconds != null) {
    lines.push(`${formatNumber(requests.calls)} calls per ${formatNumber(requests.renewalPeriodSeconds)} seconds`)
  }
  if (requests?.callQuota != null && requests.callQuotaPeriod) {
    lines.push(`${formatNumber(requests.callQuota)} calls per ${periodLabels[requests.callQuotaPeriod] ?? requests.callQuotaPeriod}`)
  }
  return lines.length > 0 ? lines : none
}

const resourceKinds: Readonly<Record<string, string>> = {
  modelApi: 'Model API',
  mcpServer: 'MCP server',
  modelDeployment: 'Model deployment',
  product: 'Product',
}

/**
 * The heading of a grant's My access card, picked as the portal picks it: the catalog's name for the resource, then
 * the resource's own name, then what became of it or its kind. The portal never heads a card with an ID.
 */
export function portalCardTitle(resolved: Pick<ApiResolvedEntitlement, 'entitlement' | 'resourceDisplayName' | 'resourceSummary'>): string {
  const usable = (name: string | null | undefined) => name?.trim() || undefined
  const kind = resolved.entitlement.resource.kind
  return (
    usable(resolved.resourceDisplayName) ??
    usable(resolved.resourceSummary?.displayName) ??
    (resolved.resourceSummary?.available === false ? 'Resource no longer available' : `${resourceKinds[kind] ?? kind} resource`)
  )
}
