/**
 * The live run's stop condition, as assertions: a plan, or the "Review model changes" step list, must touch only
 * the model being changed. Each function returns the problems it finds as sentences naming MOSAIC and API
 * Management resources; an empty list means the plan is in scope.
 */

export interface ScopeResource {
  kind: string
  name: string
  resourceId: string
}

/** The parts of a MOSAIC publication that name its API Management resources. */
export interface ScopePublication {
  id: string
  gatewayId: string
  displayName?: string
  deploymentName?: string
  apiName: string
  backendName: string
  fragmentName: string
  productName: string
  subscriptionName: string
  modelApiId?: string | null
  resources?: readonly ScopeResource[]
}

export interface ScopeStep {
  kind: string
  name: string
  action: string
  resourceId: string
  entitlementId?: string | null
}

export interface ScopeGrant {
  entitlementId: string
  subscriptionName?: string | null
  enabled?: boolean
  /** MOSAIC's digest of what the grant asks for: its subject, limits and state. */
  intentDigest?: string | null
}

/** A model's access as a plan would apply it, or as API Management last had it applied. */
export interface AccessState {
  settings?: { keysEnabled: boolean; entraEnabled: boolean } | null
  audience?: string | null
  publicationEnforcement?: unknown
  grants: readonly ScopeGrant[]
}

export interface ScopePlan {
  publicationId: string
  /** What running the plan does: "publish" for a plan that Apply runs, "unpublish" for one that Unpublish runs. */
  operation?: string
  steps: readonly ScopeStep[]
  accessSnapshot?: AccessState | null
}

export interface ScopeContext {
  /** The publication being changed. */
  target: ScopePublication
  /** Every other publication MOSAIC knows, on any gateway. */
  others: readonly ScopePublication[]
  /** The target gateway's Azure resource ID. When given, every step must be on that API Management service. */
  gatewayResourceId?: string
  /** The grants on the target's model API. When given, every grant the plan touches must be one of them. */
  targetGrantIds?: ReadonlySet<string>
}

export interface ScopeRunStep {
  kind: string
  name: string
  action: string
  status: string
  resourceId: string
}

export interface ScopeRun {
  status: string
  steps: readonly ScopeRunStep[]
  errors?: readonly string[]
}

const servicePath = /^(\/subscriptions\/[^/]+\/resourceGroups\/[^/]+\/providers\/Microsoft\.ApiManagement\/service\/[^/]+)(\/.+)$/i

/** Splits an API Management resource ID into its service and the path under it. */
export function splitServiceResourceId(resourceId: string): { service: string; path: string } | undefined {
  const match = servicePath.exec(resourceId)
  return match ? { service: match[1], path: match[2] } : undefined
}

const lower = (value: string) => value.toLowerCase()

function stepLabel(step: { kind: string; name: string }): string {
  return `${step.kind} ${step.name}`
}

function targetLabel(target: ScopePublication): string {
  return target.displayName ?? target.apiName
}

/** The subscriptions a plan for the target may touch: its own, and its grants'. */
function allowedSubscriptions(plan: ScopePlan, target: ScopePublication): Set<string> {
  const names = [
    target.subscriptionName,
    ...(plan.accessSnapshot?.grants ?? []).map((grant) => grant.subscriptionName ?? ''),
    ...(target.resources ?? []).filter((resource) => resource.kind === 'subscription').map((resource) => resource.name),
  ]
  return new Set(names.filter((name) => name !== '').map(lower))
}

function segmentName(segment: string): string {
  try {
    return lower(decodeURIComponent(segment))
  } catch {
    return lower(segment)
  }
}

function pathInScope(path: string, target: ScopePublication, subscriptions: ReadonlySet<string>): boolean {
  const segments = path.split('/').slice(1).map(segmentName)
  const [collection, name, ...rest] = segments
  if (name === undefined || name === '') return false
  switch (collection) {
    case 'backends':
      return rest.length === 0 && name === lower(target.backendName)
    case 'policyfragments':
      return rest.length === 0 && name === lower(target.fragmentName)
    case 'apis':
      return name === lower(target.apiName)
    case 'products':
      return name === lower(target.productName) && (rest.length === 0 || (rest[0] === 'apis' && rest[1] === lower(target.apiName)))
    case 'subscriptions':
      return rest.length === 0 && subscriptions.has(name)
    default:
      return false
  }
}

/**
 * Checks a publish or review plan for the target: it must be the target's plan, change only the target's
 * backend, policy fragment, API, product and subscriptions on the target's gateway, and touch only grants on
 * the target's model.
 */
export function planScopeProblems(plan: ScopePlan, context: ScopeContext): string[] {
  const { target, gatewayResourceId, targetGrantIds } = context
  const problems: string[] = []
  if (plan.publicationId !== target.id) problems.push(`The plan is for another publication, not ${targetLabel(target)}.`)
  const subscriptions = allowedSubscriptions(plan, target)
  for (const step of plan.steps) {
    const split = splitServiceResourceId(step.resourceId)
    if (!split) {
      problems.push(`Step ${stepLabel(step)} doesn't name an API Management resource.`)
      continue
    }
    if (gatewayResourceId !== undefined && lower(split.service) !== lower(gatewayResourceId)) {
      problems.push(`Step ${stepLabel(step)} is on another API Management service.`)
      continue
    }
    if (!pathInScope(split.path, target, subscriptions)) {
      problems.push(`Step ${stepLabel(step)} changes ${split.path}, which isn't part of ${targetLabel(target)}.`)
    }
    if (step.entitlementId && targetGrantIds && !targetGrantIds.has(step.entitlementId)) {
      problems.push(`Step ${stepLabel(step)} changes a grant on another model.`)
    }
  }
  for (const grant of plan.accessSnapshot?.grants ?? []) {
    if (targetGrantIds && !targetGrantIds.has(grant.entitlementId)) {
      problems.push('The access review includes a grant on another model.')
    }
  }
  return problems
}

const nameCharacter = /[A-Za-z0-9._-]/
const uniqueKinds = new Set(['backend', 'policyFragment', 'api', 'product', 'subscription'])

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * True when name appears in text as a whole name: not inside a longer API Management name. A full stop ends a
 * name only when nothing that could continue the name follows it.
 */
export function mentions(text: string, name: string): boolean {
  if (name === '') return false
  const pattern = new RegExp(escapeRegExp(name), 'gi')
  for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
    const end = match.index + match[0].length
    const before = match.index === 0 ? '' : text[match.index - 1]
    const after = text[end] ?? ''
    const sentenceEnd = after === '.' && !nameCharacter.test(text[end + 1] ?? '')
    if (!nameCharacter.test(before) && (!nameCharacter.test(after) || sentenceEnd)) return true
    pattern.lastIndex = match.index + 1
  }
  return false
}

function uniqueNames(publication: ScopePublication): string[] {
  return [
    publication.apiName,
    publication.backendName,
    publication.fragmentName,
    publication.productName,
    publication.subscriptionName,
    ...(publication.resources ?? []).filter((resource) => uniqueKinds.has(resource.kind)).map((resource) => resource.name),
  ]
}

/**
 * The other publications' API Management names: the names that must not appear in the target's plan. Names the
 * target shares, or that appear inside the target's own names, can't tell the two apart, so they are left out.
 */
export function foreignNames(target: ScopePublication, others: readonly ScopePublication[]): string[] {
  const own = [...uniqueNames(target), target.displayName ?? '', target.deploymentName ?? ''].filter((name) => name !== '')
  const names = new Set<string>()
  for (const other of others) {
    if (other.id === target.id) continue
    for (const name of uniqueNames(other)) {
      if (name.length < 3 || own.some((mine) => mentions(mine, name))) continue
      names.add(name)
    }
  }
  return [...names].sort()
}

/** The other publications' names that a plan's visible text mentions. */
export function foreignMentions(text: string, target: ScopePublication, others: readonly ScopePublication[]): string[] {
  return foreignNames(target, others).filter((name) => mentions(text, name))
}

/** A re-plan of a publication that is already applied must not create or delete anything. */
export function replanProblems(plan: ScopePlan): string[] {
  return plan.steps
    .filter((step) => step.action === 'create' || step.action === 'delete')
    .map((step) => `Re-planning would ${step.action} ${stepLabel(step)}.`)
}

/** A first publish creates everything it plans: anything else already exists, perhaps left by an earlier run. */
export function firstPublishProblems(plan: ScopePlan): string[] {
  const verbs: Record<string, string> = { update: 'update', delete: 'delete', noChange: 'leave unchanged' }
  const problems = plan.steps
    .filter((step) => step.action !== 'create')
    .map((step) => `A first publish would ${verbs[step.action] ?? step.action} ${stepLabel(step)}, which shouldn't exist yet.`)
  if (plan.steps.length === 0) problems.push('A first publish should create resources, and this plan has no steps.')
  return problems
}

/** Every step of an applied run succeeded; steps with nothing to change may be skipped. */
export function runProblems(run: ScopeRun): string[] {
  const problems: string[] = []
  if (run.status !== 'succeeded') problems.push(`The run ended ${run.status}.`)
  for (const step of run.steps) {
    if (step.status === 'succeeded' || (step.status === 'skipped' && step.action === 'noChange')) continue
    problems.push(`Step ${stepLabel(step)} ended ${step.status}.`)
  }
  return problems
}

/**
 * Unpublishing may suspend and delete, and must delete only what the publication owned before it started. It
 * never creates anything.
 */
export function unpublishProblems(steps: readonly ScopeRunStep[], owned: readonly ScopeResource[]): string[] {
  const ownedIds = new Set(owned.map((resource) => lower(resource.resourceId)))
  const problems: string[] = []
  for (const step of steps) {
    if (step.action === 'create') problems.push(`Unpublishing created ${stepLabel(step)}.`)
    if (step.action === 'delete' && !ownedIds.has(lower(step.resourceId))) {
      problems.push(`Unpublishing deleted ${stepLabel(step)}, which the publication didn't own.`)
    }
  }
  return problems
}

/**
 * An unpublish plan, before it runs: an unpublish that deletes, and only deletes, every resource the publication
 * owns. MOSAIC denies calls and suspends subscriptions itself as the run starts, so the plan lists no updates.
 */
export function unpublishPlanProblems(plan: ScopePlan, owned: readonly ScopeResource[]): string[] {
  const verbs: Record<string, string> = { create: 'create', update: 'update', noChange: 'leave unchanged' }
  const ownedIds = new Set(owned.map((resource) => lower(resource.resourceId)))
  const planned = new Set<string>()
  const problems: string[] = []
  if (plan.operation !== 'unpublish') problems.push(`MOSAIC's plan is a ${plan.operation ?? 'publish'} plan, not an unpublish plan.`)
  for (const step of plan.steps) {
    if (step.action !== 'delete') {
      problems.push(`Unpublishing would ${verbs[step.action] ?? step.action} ${stepLabel(step)}.`)
      continue
    }
    planned.add(lower(step.resourceId))
    if (!ownedIds.has(lower(step.resourceId))) problems.push(`Unpublishing would delete ${stepLabel(step)}, which the publication doesn't own.`)
  }
  for (const resource of owned) {
    if (!planned.has(lower(resource.resourceId))) problems.push(`The unpublish plan leaves ${stepLabel(resource)} behind.`)
  }
  return problems
}

/** True once a run has stopped, whether it succeeded or not. */
export function runFinished(status: string): boolean {
  return status !== 'running'
}

function describeMethods(settings: AccessState['settings']): string {
  if (!settings) return 'none'
  return `keys ${settings.keysEnabled ? 'on' : 'off'} and Entra tokens ${settings.entraEnabled ? 'on' : 'off'}`
}

const sameValue = (left: unknown, right: unknown) => JSON.stringify(left ?? null) === JSON.stringify(right ?? null)

export interface ForeignChangeOptions {
  /** The suite owns the model's access methods as well, as it does on its disposable publication. */
  methods?: boolean
}

/**
 * On a model the suite shares with other people, the suite may apply only its own grants. A governed plan
 * applies the whole model at once, so it would also apply anything saved since the model was last applied:
 * another grant's limits, a revocation, or new access methods. This compares the plan's access with what was
 * last applied and names every difference that isn't one of the suite's grants.
 */
export function foreignAccessChanges(
  planned: AccessState | null | undefined,
  applied: AccessState | null | undefined,
  ownGrantIds: ReadonlySet<string>,
  options: ForeignChangeOptions = {},
): string[] {
  if (!planned) return ["The plan has no model-wide access review, so the suite can't tell what else it would apply."]
  if (!applied) return ["MOSAIC has never applied this model's access, so the plan would apply every grant on it, not only the suite's."]
  const problems: string[] = []
  const before = applied.settings
  const after = planned.settings
  if (!options.methods && (before?.keysEnabled !== after?.keysEnabled || before?.entraEnabled !== after?.entraEnabled)) {
    problems.push(`The plan would change the model's access methods from ${describeMethods(before)} to ${describeMethods(after)}.`)
  }
  if (!sameValue(planned.audience, applied.audience)) problems.push("The plan would change the model's Microsoft Entra audience.")
  if (!sameValue(planned.publicationEnforcement, applied.publicationEnforcement)) {
    problems.push("The plan would change the model's token enforcement.")
  }
  const appliedGrants = new Map(applied.grants.map((grant) => [grant.entitlementId, grant]))
  const plannedIds = new Set(planned.grants.map((grant) => grant.entitlementId))
  let changed = 0
  for (const grant of planned.grants) {
    if (ownGrantIds.has(grant.entitlementId)) continue
    const last = appliedGrants.get(grant.entitlementId)
    const digestChanged =
      typeof last?.intentDigest === 'string' && typeof grant.intentDigest === 'string' && last.intentDigest !== grant.intentDigest
    if (!last || last.enabled !== grant.enabled || digestChanged) changed += 1
  }
  for (const grant of applied.grants) {
    if (!ownGrantIds.has(grant.entitlementId) && !plannedIds.has(grant.entitlementId)) changed += 1
  }
  if (changed > 0) {
    problems.push(
      `The plan would also apply changes to ${changed} grant(s) the suite didn't make, saved since the model was last applied.`,
    )
  }
  return problems
}
