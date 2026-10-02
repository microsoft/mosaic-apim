import { API_SHAPE_SHORT_LABELS } from './key-endpoint'
import type {
  ApiShape,
  BreakerPreset,
  CapacityType,
  Gateway,
  ModelPool,
  ModelPoolType,
  ModelPoolVisibility,
  PoolCandidateDeployment,
  PoolCandidateModel,
  PoolCapacityBadge,
  PoolMemberView,
  PoolModelSpec,
  PoolReadiness,
  PoolSafeguard,
  PublishedResourceKind,
  PublishRun,
  PublishRunStatus,
  QuotaPeriod,
} from './types'

export const POOL_TYPES: ModelPoolType[] = ['breaker', 'preferential', 'linear']

export const POOL_TYPE_LABELS: Record<ModelPoolType, string> = {
  breaker: 'Breaker',
  preferential: 'Preferential',
  linear: 'Linear',
}

export const POOL_TYPE_DESCRIPTIONS: Record<ModelPoolType, string> = {
  breaker:
    'Spreads requests across every member by weight. A member that throttles is skipped until it recovers, and the request is retried on another member.',
  preferential:
    'Sends requests to provisioned members first. When they throttle, the overflow spreads across pay-as-you-go members until they recover.',
  linear:
    'Tries members in the order you set. Every request starts with the first member and moves down the list when one throttles or is unavailable.',
}

export const BREAKER_PRESETS: BreakerPreset[] = ['throttling', 'throttlingAndErrors']

export const BREAKER_PRESET_LABELS: Record<BreakerPreset, string> = {
  throttling: 'Throttling',
  throttlingAndErrors: 'Throttling and errors',
}

export const BREAKER_PRESET_DESCRIPTIONS: Record<BreakerPreset, string> = {
  throttling:
    "Skip a member after one 429 response, for its Retry-After, or 10 seconds when it sends none. Requests move on after a 429 or 503.",
  throttlingAndErrors:
    'Skip a member after three 429 or 5xx responses within a minute, for the Retry-After, or 30 seconds. Requests also move on after a 500, 502, or 504.',
}

/** A linear pool has no breaker, so a preset only chooses which responses move a request on. */
export const LINEAR_PRESET_DESCRIPTIONS: Record<BreakerPreset, string> = {
  throttling: 'Move on to the next member after a 429 or 503 response.',
  throttlingAndErrors: 'Move on to the next member after a 429, 500, 502, 503, or 504 response.',
}

export const POOL_CAPACITY_LABELS: Record<PoolCapacityBadge, string> = {
  provisioned: 'Provisioned',
  payAsYouGo: 'Pay-as-you-go',
  provisionedWithOverflow: 'Provisioned, with pay-as-you-go overflow',
  unknown: 'Capacity unknown',
}

export const POOL_READINESS_LABELS: Record<PoolReadiness, string> = {
  ready: 'Ready',
  notConfirmed: 'Not confirmed',
  cannotInvoke: 'Cannot invoke',
}

export const POOL_VISIBILITY_LABELS: Record<ModelPoolVisibility, string> = {
  listed: 'Listed in the portal catalog',
  hidden: 'Hidden from the portal catalog',
}

export const POOL_RESOURCE_KIND_LABELS: Record<PublishedResourceKind, string> = {
  namedValue: 'Named value',
  policyFragment: 'Policy fragment',
  backend: 'Backend',
  backendPool: 'Backend pool',
  api: 'API',
  apiOperation: 'Operation',
  apiPolicy: 'API policy',
  product: 'Product',
  productApi: 'Product link',
  subscription: 'Subscription',
}

export const POOL_RUN_STATUS_LABELS: Record<PublishRunStatus, string> = {
  running: 'Running',
  succeeded: 'Succeeded',
  failed: 'Failed',
  rolledBack: 'Rolled back',
  rollbackFailed: 'Rollback failed',
  interrupted: 'Interrupted',
}

export type PoolAccessState = NonNullable<ModelPool['accessState']>

export const POOL_ACCESS_STATE_LABELS: Record<PoolAccessState, string> = {
  pending: 'Governed, not applied yet',
  applying: 'Governed, applying',
  applied: 'Governed',
  failed: 'Governed, last apply failed',
  unknown: 'Governed, gateway state unknown',
}

/** How callers reach a pool, as its badge says: the shared key, or governed access and its state. */
export function poolAccessLabel(pool: Pick<ModelPool, 'governedAccess' | 'accessState'>): string {
  return pool.governedAccess ? POOL_ACCESS_STATE_LABELS[pool.accessState ?? 'pending'] : 'Shared key'
}

/** The grants a pool's last apply put in force, on every model. */
export function poolGrantCount(pool: Pick<ModelPool, 'appliedAccess'>): number {
  return pool.appliedAccess?.grants.filter((grant) => grant.enabled).length ?? 0
}

/** API Management caps a backend pool at 30 members. */
export const MAX_POOL_MEMBERS = 30
/** No request makes more than 10 attempts, on any kind of pool. */
export const MAX_POOL_ATTEMPTS = 10
/** A linear pool tries each active member once per request, so a model has at most 10 active members. */
export const MAX_LINEAR_MEMBERS = MAX_POOL_ATTEMPTS
/** Each model adds a branch to the pool's policy, which API Management caps in size. */
export const MAX_POOL_MODELS = 40
export const MAX_POOL_RETRIES = 9
export const DEFAULT_POOL_RETRIES = 3
export const MIN_POOL_WEIGHT = 1
export const MAX_POOL_WEIGHT = 100

const PUBLIC_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/

/** The same slug the API uses for a pool's default resource names. */
export function apimSlug(value: string, maxLength = 40): string {
  const slug = value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
  return slug.slice(0, maxLength).replace(/^-+|-+$/g, '')
}

export function defaultPoolApiName(displayName: string): string {
  return `mosaic-pool-${apimSlug(displayName) || 'models'}`
}

export function defaultPoolApiPath(displayName: string): string {
  return `mosaic/pool-${apimSlug(displayName) || 'models'}`
}

/** Why a model name callers would send can't be used, or null when it can. */
export function publicNameProblem(name: string, otherNames: string[]): string | null {
  const trimmed = name.trim()
  if (!trimmed) return 'Enter the model name callers send.'
  if (!PUBLIC_NAME_PATTERN.test(trimmed)) {
    return 'Start with a letter or digit, and use only letters, digits, periods, underscores, and hyphens, up to 64 characters.'
  }
  const folded = trimmed.toLowerCase()
  if (otherNames.some((other) => other.trim().toLowerCase() === folded)) {
    return 'Another model in this pool already uses this name.'
  }
  return null
}

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b)
}

const RESOURCE_NAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9-]*$/
const API_PATH_PATTERN = /^[A-Za-z0-9][A-Za-z0-9/-]*$/

/** Why an API Management resource name can't be used, or null when it can. The API checks the same. */
export function apiNameProblem(value: string): string | null {
  const trimmed = value.trim()
  if (!trimmed) return 'Enter a name.'
  if (trimmed.length > 80) return 'Use at most 80 characters.'
  if (!RESOURCE_NAME_PATTERN.test(trimmed)) {
    return 'Start with a letter or digit, and use only letters, digits, and hyphens.'
  }
  return null
}

/** Why an API path can't be used, or null when it can. Leading and trailing slashes are ignored. */
export function apiPathProblem(value: string): string | null {
  const trimmed = value.trim().replace(/^\/+|\/+$/g, '')
  if (!trimmed) return 'Enter a path.'
  if (trimmed.length > 200) return 'Use at most 200 characters.'
  if (!API_PATH_PATTERN.test(trimmed)) {
    return 'Start with a letter or digit, and use only letters, digits, hyphens, and forward slashes.'
  }
  return null
}

/** A safeguard as typed. Strings, so an empty field means no limit rather than zero. */
export interface SafeguardFields {
  tokensPerMinute: string
  tokenQuota: string
  tokenQuotaPeriod: QuotaPeriod | ''
}

export function safeguardFields(safeguard: PoolSafeguard | null | undefined): SafeguardFields {
  return {
    tokensPerMinute: safeguard?.tokensPerMinute ? String(safeguard.tokensPerMinute) : '',
    tokenQuota: safeguard?.tokenQuota ? String(safeguard.tokenQuota) : '',
    tokenQuotaPeriod: safeguard?.tokenQuotaPeriod ?? '',
  }
}

/** The safeguard the fields describe, or why they can't be saved. Empty fields mean none. */
export function safeguardFromFields(fields: SafeguardFields): {
  safeguard: PoolSafeguard | null
  problem: string | null
} {
  const parse = (value: string) => (value.trim() === '' ? null : Number(value.trim()))
  const tokensPerMinute = parse(fields.tokensPerMinute)
  const tokenQuota = parse(fields.tokenQuota)
  if (tokensPerMinute != null && (!Number.isInteger(tokensPerMinute) || tokensPerMinute < 1)) {
    return { safeguard: null, problem: 'Enter tokens per minute as a whole number above zero.' }
  }
  if (tokenQuota != null && (!Number.isInteger(tokenQuota) || tokenQuota < 1)) {
    return { safeguard: null, problem: 'Enter the token quota as a whole number above zero.' }
  }
  if (tokenQuota != null && !fields.tokenQuotaPeriod) {
    return { safeguard: null, problem: 'Choose the period the token quota covers.' }
  }
  if (tokenQuota == null && fields.tokenQuotaPeriod) {
    return { safeguard: null, problem: 'Enter a token quota for the period, or set the period to None.' }
  }
  if (tokensPerMinute == null && tokenQuota == null) return { safeguard: null, problem: null }
  return {
    safeguard: { tokensPerMinute, tokenQuota, tokenQuotaPeriod: fields.tokenQuotaPeriod || null },
    problem: null,
  }
}

const QUOTA_PERIOD_UNITS: Record<QuotaPeriod, string> = {
  Hourly: 'hour',
  Daily: 'day',
  Weekly: 'week',
  Monthly: 'month',
  Yearly: 'year',
}

/** A safeguard in words. Each pool model has its own counters. */
export function describeSafeguard(safeguard: PoolSafeguard | null | undefined): string {
  const parts: string[] = []
  if (safeguard?.tokensPerMinute) parts.push(`${safeguard.tokensPerMinute.toLocaleString()} tokens per minute`)
  if (safeguard?.tokenQuota && safeguard.tokenQuotaPeriod) {
    parts.push(`${safeguard.tokenQuota.toLocaleString()} tokens per ${QUOTA_PERIOD_UNITS[safeguard.tokenQuotaPeriod]}`)
  }
  return parts.length ? `${parts.join(' and ')}, for each model` : 'None'
}

/**
 * Weights proportional to each deployment's capacity. Members are only comparable within one
 * priority group and one capacity type: provisioned units and pay-as-you-go thousands of tokens per
 * minute measure different things. A group with unknown or mixed capacity gets equal weights. A
 * breaker or preferential pool tries a member reached with an API key once, outside its backend
 * pool, so that member keeps weight 1 and doesn't count toward any group.
 */
export function suggestedWeights(
  deployments: (Pick<PoolCandidateDeployment, 'capacityType' | 'skuCapacity'> &
    Partial<Pick<PoolCandidateDeployment, 'apiKey'>>)[],
  poolType: ModelPoolType,
): number[] {
  const weights = deployments.map(() => 1)
  if (poolType === 'linear') return weights
  const groups = new Map<string, number[]>()
  deployments.forEach((deployment, index) => {
    if (deployment.apiKey) return
    const group = poolType === 'preferential' && deployment.capacityType === 'provisioned' ? 'first' : 'rest'
    groups.set(group, [...(groups.get(group) ?? []), index])
  })
  for (const indexes of groups.values()) {
    const types = new Set(indexes.map((index) => deployments[index].capacityType))
    const capacities = indexes.map((index) => deployments[index].skuCapacity ?? 0)
    if (types.size !== 1 || types.has('unknown') || capacities.some((value) => value <= 0)) continue
    const divisor = capacities.reduce((left, right) => gcd(left, right))
    const reduced = capacities.map((value) => value / divisor)
    const largest = Math.max(...reduced)
    indexes.forEach((deploymentIndex, position) => {
      weights[deploymentIndex] =
        largest <= 100 ? reduced[position] : Math.max(1, Math.round((reduced[position] * 100) / largest))
    })
  }
  return weights
}

/** Which priority group a preferential pool puts a member in. Unknown capacity can't go first. */
export function preferentialPriority(capacityType: CapacityType): 1 | 2 {
  return capacityType === 'provisioned' ? 1 : 2
}

export interface DraftPoolMember {
  modelEndpointId: string
  deploymentName: string
  weight: number
  drained: boolean
}

/** A pool model as the console edits it, before it becomes a spec the API accepts. */
export interface DraftPoolModel {
  candidateKey: string
  modelName: string
  modelFormat: string | null
  apiShape: ApiShape | null
  publicName: string
  displayName: string
  listed: boolean
  allowMixedVersions: boolean
  members: DraftPoolMember[]
}

export function memberKey(modelEndpointId: string, deploymentName: string): string {
  return `${modelEndpointId}::${deploymentName.toLowerCase()}`
}

export function candidateKey(
  model: Pick<PoolCandidateModel, 'modelName' | 'modelFormat' | 'apiShape'>,
): string {
  return [model.modelFormat ?? '', model.modelName, model.apiShape ?? '']
    .map((part) => part.toLowerCase())
    .join('|')
}

/** The vendor and API a pool is fixed to once it has a model: one of each per pool. */
export interface PoolFamily {
  vendor: string | null
  apiShape: ApiShape | null
}

export function sameFamily(
  model: Pick<PoolCandidateModel, 'modelFormat' | 'apiShape'>,
  family: PoolFamily,
): boolean {
  return (
    (model.modelFormat ?? '').toLowerCase() === (family.vendor ?? '').toLowerCase() &&
    (model.apiShape ?? null) === (family.apiShape ?? null)
  )
}

/** What a pool serves, such as "Anthropic · Anthropic Messages". */
export function poolFamilyLabel(vendor: string | null | undefined, apiShape: ApiShape | null | undefined): string {
  const name = vendor || 'Unknown vendor'
  return apiShape ? `${name} · ${API_SHAPE_SHORT_LABELS[apiShape]}` : name
}

/**
 * Whether the pool may still own something in API Management, so MOSAIC won't delete its record. The
 * API makes the same check.
 */
export function mayOwnGatewayState(pool: Pick<ModelPool, 'status' | 'resources'>): boolean {
  return pool.status === 'applying' || pool.resources.some((resource) => resource.createdByMosaic)
}

/** Why MOSAIC can't publish a pool to the gateway, or null when it can. A draft can still be saved. */
export function poolPublishBlocker(
  gateway: Pick<Gateway, 'managementMode' | 'access'> | null | undefined,
): string | null {
  if (!gateway) return null
  if (gateway.managementMode !== 'manage') {
    return 'MOSAIC only observes this gateway, so it can’t publish the pool there. Save it as a draft, and switch the gateway to manage mode before you publish.'
  }
  if (!gateway.access.canWrite) {
    return 'MOSAIC hasn’t confirmed it can write to this gateway. Save the pool as a draft, and verify the gateway’s write access before you publish.'
  }
  return null
}

/** Why MOSAIC can't change a saved pool in API Management, publishing or unpublishing, or null. */
export function poolWriteBlocker(
  gateway: Pick<Gateway, 'managementMode' | 'access'> | null | undefined,
): string | null {
  if (!gateway) return null
  if (gateway.managementMode !== 'manage') {
    return 'MOSAIC only observes this gateway, so it can’t publish or unpublish the pool. Switch the gateway to manage mode first.'
  }
  if (!gateway.access.canWrite) {
    return 'MOSAIC hasn’t confirmed it can write to this gateway, so it can’t publish or unpublish the pool. Grant write access, then re-run the gateway’s access check.'
  }
  return null
}

/** What the pools list asks a pool's page to do once the editor has saved a new pool. */
export interface PoolLocationState {
  openPlan?: boolean
}

/** Whether a run published the pool or removed it. A run that only deleted things unpublished it. */
export function poolRunOperation(run: Pick<PublishRun, 'steps'>): 'publish' | 'unpublish' {
  return run.steps.length > 0 && run.steps.every((step) => step.action === 'delete') ? 'unpublish' : 'publish'
}

/** Runs newest first. The API doesn't promise an order. */
export function newestRunsFirst(runs: PublishRun[]): PublishRun[] {
  return [...runs].sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt))
}

type PositionedMember = Pick<PoolMemberView, 'order' | 'priority'> & Partial<Pick<PoolMemberView, 'drained' | 'apiKey'>>

/**
 * Each member's position in its model: the order a linear pool tries it in, or its priority group.
 * A breaker or preferential pool tries a member reached with an API key after every group its
 * backend pool holds, so that member's position follows the last of them.
 */
export function memberPositions(poolType: ModelPoolType, members: PositionedMember[]): string[] {
  const groups = members
    .filter((member) => !member.drained && !member.apiKey)
    .map((member) => member.priority ?? 1)
  const afterBackendPool = groups.length ? Math.max(...groups) : 0
  return members.map((member) => {
    const position =
      poolType === 'linear'
        ? member.order
        : member.apiKey
          ? member.order && afterBackendPool + member.order
          : member.priority
    return position ? `${position}` : '—'
  })
}

/** One member's position in its model. See `memberPositions`, which places members reached with an API key. */
export function memberPosition(poolType: ModelPoolType, member: PositionedMember): string {
  return memberPositions(poolType, [member])[0]
}

/**
 * Each member's share of the requests its priority group gets, as a whole percentage, or null for a
 * member that gets none by weight: a drained one, one reached with an API key, which a breaker or
 * preferential pool tries once after its backend pool, or any member of a linear pool, which has no
 * weights.
 */
export function memberShares(
  poolType: ModelPoolType,
  members: (Pick<PoolMemberView, 'weight' | 'drained' | 'priority'> & Partial<Pick<PoolMemberView, 'apiKey'>>)[],
): (number | null)[] {
  if (poolType === 'linear') return members.map(() => null)
  const weighted = (member: (typeof members)[number]) => !member.drained && !member.apiKey
  const totals = new Map<number, number>()
  for (const member of members) {
    if (!weighted(member)) continue
    const group = member.priority ?? 1
    totals.set(group, (totals.get(group) ?? 0) + member.weight)
  }
  return members.map((member) => {
    if (!weighted(member)) return null
    const total = totals.get(member.priority ?? 1) ?? 0
    return total > 0 ? Math.round((member.weight / total) * 100) : null
  })
}

const ORDINALS = ['first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh', 'eighth', 'ninth', 'tenth']

function ordinal(value: number): string {
  if (value >= 1 && value <= ORDINALS.length) return ORDINALS[value - 1]
  const lastTwo = value % 100
  const suffix = lastTwo >= 11 && lastTwo <= 13 ? 'th' : ({ 1: 'st', 2: 'nd', 3: 'rd' }[value % 10] ?? 'th')
  return `${value}${suffix}`
}

/**
 * When a breaker or preferential pool tries a member it reaches with an API key: once per request,
 * in the pool's order, after the backend pool's attempts. `order` is the member's position among
 * the model's active members reached with an API key, and `keyed` is how many there are.
 */
export function keyedTurn(order: number | null | undefined, keyed: number, afterBackendPool: boolean): string {
  if (!order) return 'No requests'
  if (keyed <= 1) return afterBackendPool ? 'Tried once, after the backend pool' : 'Tried once per request'
  return afterBackendPool ? `Tried ${ordinal(order)} after the backend pool` : `Tried ${ordinal(order)}`
}

/**
 * How a pool's active members reached with an API key change what a request tries: there are none,
 * they follow the backend pool's attempts, or there's no backend pool because every one has a key.
 */
export type KeyedRouting = 'none' | 'afterBackendPool' | 'only'

export function keyedRouting(models: { members: Pick<PoolMemberView, 'drained' | 'apiKey'>[] }[]): KeyedRouting {
  const active = models.flatMap((model) => model.members.filter((member) => !member.drained))
  if (!active.some((member) => member.apiKey)) return 'none'
  return active.every((member) => member.apiKey) ? 'only' : 'afterBackendPool'
}

/**
 * How many more deployments a request may try. A linear pool tries every active one, in order, and
 * so does any pool with no backend pool. Each deployment reached with an API key gets one attempt
 * after the backend pool's.
 */
export function describeRetries(poolType: ModelPoolType, maxRetries: number, keyed: KeyedRouting = 'none'): string {
  if (poolType === 'linear' || keyed === 'only') return 'Every active deployment, in order, until one answers'
  if (keyed === 'afterBackendPool') {
    const then = 'then one attempt on each deployment reached with an API key'
    if (maxRetries <= 0) return `One attempt in the backend pool, ${then}`
    if (maxRetries === 1) return `Up to 1 retry in the backend pool, ${then}`
    return `Up to ${maxRetries} retries in the backend pool, ${then}`
  }
  if (maxRetries <= 0) return 'None. Each request makes one attempt.'
  if (maxRetries === 1) return 'Up to 1 retry, on another deployment'
  return `Up to ${maxRetries} retries, each on another deployment`
}

/** How long a run took, such as "850 ms", "20.0 s", or "2 min 5 s". */
export function formatRunDuration(durationMs: number | null | undefined): string {
  if (durationMs == null || durationMs < 0) return '—'
  if (durationMs < 999.5) return `${Math.round(durationMs)} ms`
  if (durationMs < 59_950) return `${(durationMs / 1000).toFixed(1)} s`
  const totalSeconds = Math.round(durationMs / 1000)
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = totalSeconds % 60
  return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`
}

/** The name callers send by default: the members' shared deployment name, or the model's. */
export function defaultPublicName(model: PoolCandidateModel, members: DraftPoolMember[]): string {
  const names = new Set(members.map((member) => member.deploymentName.toLowerCase()))
  if (names.size === 1) {
    const shared = members[0].deploymentName
    if (PUBLIC_NAME_PATTERN.test(shared)) return shared
  }
  return PUBLIC_NAME_PATTERN.test(model.modelName) ? model.modelName : apimSlug(model.modelName, 64)
}

/** A new pool model with every eligible deployment preselected and weighted by capacity. */
export function draftFromCandidate(model: PoolCandidateModel, poolType: ModelPoolType): DraftPoolModel {
  const eligible = model.deployments.filter((deployment) => deployment.eligible)
  const weights = suggestedWeights(eligible, poolType)
  const members = eligible.map((deployment, index) => ({
    modelEndpointId: deployment.modelEndpointId,
    deploymentName: deployment.deploymentName,
    weight: weights[index],
    drained: false,
  }))
  return {
    candidateKey: candidateKey(model),
    modelName: model.modelName,
    modelFormat: model.modelFormat ?? null,
    apiShape: model.apiShape ?? null,
    publicName: defaultPublicName(model, members),
    displayName: model.modelName,
    listed: true,
    allowMixedVersions: false,
    members,
  }
}

/** An existing pool's models, ready to edit. */
export function draftsFromPool(pool: ModelPool): DraftPoolModel[] {
  return pool.models.map((model) => ({
    candidateKey: candidateKey({
      modelName: model.modelName ?? model.publicName,
      modelFormat: model.modelFormat ?? pool.vendor ?? null,
      apiShape: pool.apiShape ?? null,
    }),
    modelName: model.modelName ?? model.publicName,
    modelFormat: model.modelFormat ?? pool.vendor ?? null,
    apiShape: pool.apiShape ?? null,
    publicName: model.publicName,
    displayName: model.displayName,
    listed: model.listed,
    allowMixedVersions: model.allowMixedVersions,
    members: model.members.map((member) => ({
      modelEndpointId: member.modelEndpointId,
      deploymentName: member.deploymentName,
      weight: member.weight,
      drained: member.drained,
    })),
  }))
}

export function specsFromDrafts(drafts: DraftPoolModel[]): PoolModelSpec[] {
  return drafts.map((draft) => ({
    publicName: draft.publicName.trim(),
    displayName: draft.displayName.trim() || draft.publicName.trim(),
    listed: draft.listed,
    allowMixedVersions: draft.allowMixedVersions,
    members: draft.members.map((member) => ({
      modelEndpointId: member.modelEndpointId,
      deploymentName: member.deploymentName,
      weight: member.weight,
      drained: member.drained,
    })),
  }))
}

/**
 * The pool's models as specs, with one member drained or restored. Display names are passed
 * explicitly so the update keeps them.
 */
export function specsWithMemberDrained(
  pool: ModelPool,
  modelId: string,
  modelEndpointId: string,
  deploymentName: string,
  drained: boolean,
): PoolModelSpec[] {
  const target = memberKey(modelEndpointId, deploymentName)
  const specs = specsFromDrafts(draftsFromPool(pool))
  return pool.models.map((model, index) => {
    const spec = specs[index]
    if (model.id !== modelId) return spec
    return {
      ...spec,
      members: spec.members.map((member) =>
        memberKey(member.modelEndpointId, member.deploymentName) === target
          ? { ...member, drained }
          : member,
      ),
    }
  })
}

export interface DraftCheck {
  /** What stops the models being saved. */
  problems: string[]
  /** What would stop the pool being published, or what an administrator should know first. */
  cautions: string[]
}

export type DeploymentLookup = (member: DraftPoolMember) => PoolCandidateDeployment | undefined

/** Index a gateway's candidate deployments by member key, for checking and describing members. */
export function deploymentLookup(models: PoolCandidateModel[] | undefined): DeploymentLookup {
  const index = new Map<string, PoolCandidateDeployment>()
  for (const model of models ?? []) {
    for (const deployment of model.deployments) {
      index.set(memberKey(deployment.modelEndpointId, deployment.deploymentName), deployment)
    }
  }
  return (member) => index.get(memberKey(member.modelEndpointId, member.deploymentName))
}

/**
 * Check pool models the way the API will, each finding a sentence an administrator can act on.
 * Problems match what the API refuses to save; cautions match what it refuses to publish.
 * `maxRetries` is the pool's, which bounds the attempts a breaker or preferential pool makes.
 */
export function checkDrafts(
  drafts: DraftPoolModel[],
  poolType: ModelPoolType,
  inventory?: DeploymentLookup,
  maxRetries: number = DEFAULT_POOL_RETRIES,
): DraftCheck {
  const lookup: DeploymentLookup = inventory ?? (() => undefined)
  const problems: string[] = []
  const cautions: string[] = []
  if (drafts.length > MAX_POOL_MODELS) {
    problems.push(`A pool can serve at most ${MAX_POOL_MODELS} models. Remove ${drafts.length - MAX_POOL_MODELS}.`)
  }
  drafts.forEach((draft, index) => {
    const label = draft.displayName.trim() || draft.modelName
    const otherNames = drafts.filter((_, other) => other !== index).map((other) => other.publicName)
    const nameProblem = publicNameProblem(draft.publicName, otherNames)
    if (nameProblem) problems.push(`${label}: ${nameProblem}`)
    const active = draft.members.filter((member) => !member.drained)
    if (draft.members.length === 0) {
      problems.push(`${label}: choose at least one deployment.`)
    } else if (active.length === 0) {
      cautions.push(`${label}: every deployment is drained, so it can't serve requests. Undrain one, or remove the model.`)
    }
    if (draft.members.length > MAX_POOL_MEMBERS) {
      problems.push(
        `${label}: an API Management backend pool holds at most ${MAX_POOL_MEMBERS} deployments. Remove ${draft.members.length - MAX_POOL_MEMBERS}.`,
      )
    }
    for (const member of draft.members) {
      if (inventory && !inventory(member)) {
        problems.push(`${label}: MOSAIC no longer finds ${member.deploymentName}. Remove it, or sync its endpoint.`)
      }
      if (!Number.isInteger(member.weight) || member.weight < MIN_POOL_WEIGHT || member.weight > MAX_POOL_WEIGHT) {
        problems.push(
          `${label}: give ${member.deploymentName} a whole-number weight from ${MIN_POOL_WEIGHT} to ${MAX_POOL_WEIGHT}.`,
        )
      }
    }
    const versions = new Set(
      draft.members.map((member) => lookup(member)?.modelVersion).filter((version): version is string => Boolean(version)),
    )
    if (versions.size > 1 && !draft.allowMixedVersions) {
      problems.push(
        `${label}: the deployments serve different versions (${[...versions].sort().join(', ')}). Choose deployments of one version, or allow mixed versions.`,
      )
    }
    if (poolType === 'linear' && active.length > MAX_LINEAR_MEMBERS) {
      problems.push(
        `${label}: a linear pool tries at most ${MAX_LINEAR_MEMBERS} deployments per request, and ${active.length} are active. Drain or remove ${active.length - MAX_LINEAR_MEMBERS}, or use a breaker pool.`,
      )
    }
    // A breaker or preferential pool tries each member reached with an API key once, as its own
    // target after the backend pool, so only the backend pool's members share a deployment name.
    const keyed = poolType === 'linear' ? [] : active.filter((member) => lookup(member)?.apiKey)
    const pooled = active.filter((member) => !keyed.includes(member))
    const balanced = pooled.length ? Math.max(1, Math.min(maxRetries + 1, pooled.length)) : 0
    if (keyed.length && balanced + keyed.length > MAX_POOL_ATTEMPTS) {
      const each = keyed.length === 1 ? 'one on the deployment' : `one on each of the ${keyed.length} deployments`
      cautions.push(
        balanced
          ? `${label}: a request makes at most ${MAX_POOL_ATTEMPTS} attempts, and this model would make ${balanced + keyed.length}: ${balanced} on its backend pool, then ${each} reached with an API key. Lower the pool's retries, or drain some deployments.`
          : `${label}: a request makes at most ${MAX_POOL_ATTEMPTS} attempts, and this model would make ${keyed.length}, one on each of its deployments reached with an API key. Drain some deployments.`,
      )
    }
    const names = new Set(pooled.map((member) => member.deploymentName.toLowerCase()))
    if (poolType !== 'linear' && names.size > 1 && draft.apiShape !== null && draft.apiShape !== 'azureOpenAi') {
      cautions.push(
        `${label}: callers name the model in the request body, so every active deployment in a ${POOL_TYPE_LABELS[poolType].toLowerCase()} pool must share one deployment name. Rename them, or use a linear pool.`,
      )
    }
    const scopes = new Set(
      active.map((member) => lookup(member)?.processingScope).filter((scope) => scope && scope !== 'unknown'),
    )
    if (scopes.size > 1) {
      cautions.push(`${label}: the deployments process data in different scopes, and any of them may serve a request.`)
    }
    for (const member of active) {
      const deployment = lookup(member)
      if (deployment && !deployment.eligible) {
        cautions.push(`${member.deploymentName} on ${deployment.endpointName}: ${deployment.reason ?? "can't be pooled."}`)
      }
    }
  })
  return { problems, cautions: [...new Set(cautions)] }
}

/** The problems a 409 from planning a pool lists, if it lists any. */
export function planProblems(error: unknown): string[] {
  if ((error as { status?: unknown } | null | undefined)?.status !== 409) return []
  const details = (error as { body?: { details?: { problems?: unknown } } } | null | undefined)?.body?.details
  const problems = details?.problems
  return Array.isArray(problems) ? problems.filter((item): item is string => typeof item === 'string') : []
}

export function capacitySummary(capacity: Partial<Record<CapacityType, number>>): string {
  const parts: string[] = []
  if (capacity.provisioned) parts.push(`${capacity.provisioned} provisioned`)
  if (capacity.payAsYouGo) parts.push(`${capacity.payAsYouGo} pay-as-you-go`)
  if (capacity.batch) parts.push(`${capacity.batch} batch`)
  if (capacity.unknown) parts.push(`${capacity.unknown} unknown`)
  return parts.join(' · ') || '—'
}

const V2_SKUS = new Set(['basicv2', 'standardv2', 'premiumv2'])
const CLASSIC_SKUS = new Set(['developer', 'basic', 'standard', 'premium', 'isolated'])

/**
 * Why the gateway can't apply a pool's safeguard, or null when it can. API Management meters the
 * Anthropic Messages API only on the v2 tiers; the API makes the same check before it plans.
 */
export function safeguardTierNote(apiShape: ApiShape | null | undefined, skuName: string | null | undefined): string | null {
  if (apiShape !== 'anthropicMessages') return null
  const sku = (skuName ?? '').trim().toLowerCase()
  if (V2_SKUS.has(sku)) return null
  const lead =
    'API Management applies token limits to the Anthropic Messages API only on v2 tiers (Basic v2, Standard v2, and Premium v2).'
  if (sku === 'consumption') {
    return `${lead} This gateway uses the Consumption tier, so it can't apply the safeguard. Remove it to publish the pool here.`
  }
  if (CLASSIC_SKUS.has(sku)) {
    return `${lead} This gateway uses the ${skuName?.trim()} tier, a classic tier, so it can't apply the safeguard. Remove it to publish the pool here.`
  }
  return `${lead} MOSAIC hasn't read this gateway's tier. Re-run the gateway's access check, or remove the safeguard.`
}

/** How a caller reaches one pool model: the request line, and where the model's name goes. */
export interface PoolRequestExample {
  method: 'POST'
  url: string
  body: string | null
  note: string
}

/** The request a caller sends to use one pool model, by the API the pool serves. */
export function poolRequestExample(
  apiShape: ApiShape | null | undefined,
  baseUrl: string,
  publicName: string,
): PoolRequestExample | null {
  const base = baseUrl.replace(/\/+$/, '')
  switch (apiShape) {
    case 'azureOpenAi':
      return {
        method: 'POST',
        url: `${base}/openai/deployments/${publicName}/chat/completions?api-version=2024-10-21`,
        body: null,
        note: 'Azure OpenAI SDKs put the model in the route. Use the base URL as the endpoint and the model name as the deployment.',
      }
    case 'foundryModels':
      return {
        method: 'POST',
        url: `${base}/models/chat/completions`,
        body: `{ "model": "${publicName}", "messages": [...] }`,
        note: 'Name the model in the request body.',
      }
    case 'anthropicMessages':
      return {
        method: 'POST',
        url: `${base}/anthropic/v1/messages`,
        body: `{ "model": "${publicName}", "max_tokens": 1024, "messages": [...] }`,
        note: `With an Anthropic SDK, use ${base}/anthropic as the base URL, and send the key in the Ocp-Apim-Subscription-Key header. The gateway removes x-api-key, and adds anthropic-version when a request omits it.`,
      }
    default:
      return null
  }
}

export type ReadinessTone = 'success' | 'warning' | 'danger' | 'muted'

export function readinessSummary(readiness: Partial<Record<PoolReadiness, number>>): {
  label: string
  tone: ReadinessTone
} {
  if (readiness.cannotInvoke) return { label: `${readiness.cannotInvoke} cannot invoke`, tone: 'danger' }
  if (readiness.notConfirmed) return { label: `${readiness.notConfirmed} not confirmed`, tone: 'warning' }
  if (readiness.ready) return { label: 'All ready', tone: 'success' }
  return { label: 'No active members', tone: 'muted' }
}
