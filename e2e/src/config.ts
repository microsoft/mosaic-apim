import { existsSync, readFileSync } from 'node:fs'
import { targetsFile } from './paths.ts'

export type AppName = 'web' | 'portal'
export type MosaicRole = 'Admin' | 'User' | 'None'
export type EndpointKind = 'azure-openai' | 'foundry'
export type RegisterVia = 'paste' | 'suggestion'
export type BrowserChannel = 'chromium' | 'msedge' | 'chrome'

export interface PersonaTarget {
  upn: string
  objectId?: string
  expectedRole: MosaicRole
  guest?: boolean
  channel?: BrowserChannel
  description?: string
}

export interface EndpointTarget {
  kind: EndpointKind
  resourceId: string
  projectResourceId?: string
  registerVia: RegisterVia
  publish: string[]
  notes?: string
}

export interface NegativeTarget {
  kind: EndpointKind
  resourceId: string
  reason: string
}

export interface JourneyRoles {
  admin: string
  user: string
  guest?: string
  noRole?: string
  outsider?: string
}

/** A model deployment named by its manifest endpoint key and deployment name. */
export interface SuiteModel {
  endpoint: string
  deployment: string
}

/** A grant on a model, held by a persona. */
export interface SuiteGrant extends SuiteModel {
  persona: string
}

export interface SuiteLimits {
  tokensPerMinute?: number
  calls?: number
  perSeconds?: number
}

/** The grants the runtime journeys check. They already exist; the suite only reads them. */
export interface SuiteRuntime {
  /** Grants held by roles.user, on models the manifest publishes. */
  userGrants: SuiteModel[]
  /** The workload's grant. */
  applicationGrant?: SuiteModel
  /** A grant held by someone other than roles.user, which the user must not be able to read. */
  foreignGrant?: SuiteGrant
  apiVersion?: string
  modelsApiVersion?: string
  chatTokenParameter?: 'max_tokens' | 'max_completion_tokens'
}

/** A deployment the suite publishes, grants, unpublishes and removes again. It is never one the manifest keeps. */
export interface SuiteDisposablePublication extends SuiteModel {
  /** Requests access to it in the portal and gets the throwaway grant. Defaults to roles.user. */
  requester: string
  limits: SuiteLimits
}

/** R5's throwaway grant: exactly the verifier's shared budget of 2 calls per 300 seconds. */
export interface SuiteSharedBudget extends SuiteGrant {
  calls: number
  perSeconds: number
}

/** R6's throwaway grant: a tokens-per-minute limit no higher than the verifier's ceiling. */
export interface SuiteTokenLimit extends SuiteGrant {
  tokensPerMinute: number
}

export interface SuiteDisposables {
  publication?: SuiteDisposablePublication
  sharedBudget?: SuiteSharedBudget
  tokenLimit?: SuiteTokenLimit
}

/** What the ordered specs act on. Journeys whose part is missing are skipped. */
export interface SuiteTargets {
  /** The MOSAIC gateway, by name, that the suite publishes through. */
  gateway: string
  runtime?: SuiteRuntime
  disposable?: SuiteDisposables
}

export interface Targets {
  environment: string
  tenantId: string
  origins: Record<'web' | 'portal' | 'api' | 'gateway', string>
  roles: JourneyRoles
  personas: Record<string, PersonaTarget>
  /**
   * The workload application. `displayName` is its app registration's name. `objectId`, its service principal's
   * object ID, finds its MOSAIC identity whatever an admin labelled it; without it, the label must equal the name.
   */
  workload?: { displayName: string; appId?: string; objectId?: string }
  endpoints: Record<string, EndpointTarget>
  negatives?: Record<string, NegativeTarget>
  suite?: SuiteTargets
}

export class TargetsError extends Error {}

const personaKeyPattern = /^[a-z][a-z0-9-]{0,31}$/
const guidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i
const resourceIdPattern = /^\/subscriptions\/[0-9a-f-]{36}\/resourceGroups\/[^/]+\/providers\/Microsoft\.CognitiveServices\/accounts\/[^/]+(\/projects\/[^/]+)?$/i

type Json = Record<string, unknown>

function isObject(value: unknown): value is Json {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function requireObject(value: unknown, path: string): Json {
  if (!isObject(value)) throw new TargetsError(`${path} must be an object`)
  return value
}

function requireString(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new TargetsError(`${path} must be a non-empty string`)
  }
  return value.trim()
}

function optionalString(value: unknown, path: string): string | undefined {
  return value === undefined ? undefined : requireString(value, path)
}

function requireOneOf<T extends string>(value: unknown, allowed: readonly T[], path: string): T {
  if (typeof value !== 'string' || !(allowed as readonly string[]).includes(value)) {
    throw new TargetsError(`${path} must be one of ${allowed.join(', ')}`)
  }
  return value as T
}

export function normalizeOrigin(value: unknown, path: string): string {
  const raw = requireString(value, path)
  let url: URL
  try {
    url = new URL(raw)
  } catch {
    throw new TargetsError(`${path} must be an absolute URL`)
  }
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1'
  if (url.protocol !== 'https:' && !(local && url.protocol === 'http:')) {
    throw new TargetsError(`${path} must use https (http is only allowed for localhost)`)
  }
  if ((url.pathname !== '/' && url.pathname !== '') || url.search || url.hash || url.username || url.password) {
    throw new TargetsError(`${path} must be an origin without a path, query, fragment, or credentials`)
  }
  return url.origin
}

function parseResourceId(value: unknown, path: string, allowProject: boolean): string {
  const id = requireString(value, path)
  const match = resourceIdPattern.exec(id)
  if (!match || (!allowProject && match[1])) {
    throw new TargetsError(`${path} must be a Microsoft.CognitiveServices/accounts resource ID`)
  }
  return id
}

function parsePersona(value: unknown, path: string): PersonaTarget {
  const persona = requireObject(value, path)
  const upn = requireString(persona.upn, `${path}.upn`)
  if (!upn.includes('@')) throw new TargetsError(`${path}.upn must be a user principal name`)
  const objectId = optionalString(persona.objectId, `${path}.objectId`)
  if (objectId && !guidPattern.test(objectId)) throw new TargetsError(`${path}.objectId must be a GUID`)
  return {
    upn,
    objectId,
    expectedRole: requireOneOf(persona.expectedRole, ['Admin', 'User', 'None'] as const, `${path}.expectedRole`),
    guest: persona.guest === true,
    channel: persona.channel === undefined
      ? undefined
      : requireOneOf(persona.channel, ['chromium', 'msedge', 'chrome'] as const, `${path}.channel`),
    description: optionalString(persona.description, `${path}.description`),
  }
}

function parseEndpoint(value: unknown, path: string): EndpointTarget {
  const endpoint = requireObject(value, path)
  const kind = requireOneOf(endpoint.kind, ['azure-openai', 'foundry'] as const, `${path}.kind`)
  const publish = endpoint.publish
  if (!Array.isArray(publish) || publish.some((item) => typeof item !== 'string' || item.trim() === '')) {
    throw new TargetsError(`${path}.publish must be an array of deployment names`)
  }
  const projectResourceId = endpoint.projectResourceId === undefined
    ? undefined
    : parseResourceId(endpoint.projectResourceId, `${path}.projectResourceId`, true)
  return {
    kind,
    resourceId: parseResourceId(endpoint.resourceId, `${path}.resourceId`, false),
    projectResourceId,
    registerVia: requireOneOf(endpoint.registerVia, ['paste', 'suggestion'] as const, `${path}.registerVia`),
    publish: publish.map((item: string) => item.trim()),
    notes: optionalString(endpoint.notes, `${path}.notes`),
  }
}

/** The verifier's shared-budget proof needs exactly this call limit (BUDGET_CALLS and BUDGET_PERIOD_SECONDS). */
export const sharedBudgetCalls = 2
export const sharedBudgetSeconds = 300
/** The verifier's token-limit proof refuses a higher limit (TOKEN_LIMIT_CEILING). */
export const tokenLimitCeiling = 100

const apiVersionPattern = /^[A-Za-z0-9][A-Za-z0-9.-]{0,31}$/

function positiveInteger(value: unknown, path: string, max = 1_000_000_000): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 1 || value > max) {
    throw new TargetsError(`${path} must be a whole number from 1 to ${max}`)
  }
  return value
}

function optionalPositiveInteger(value: unknown, path: string): number | undefined {
  return value === undefined ? undefined : positiveInteger(value, path)
}

export function sameModel(first: SuiteModel, second: SuiteModel): boolean {
  return first.endpoint === second.endpoint && first.deployment === second.deployment
}

export function modelLabel(model: SuiteModel): string {
  return `${model.endpoint}/${model.deployment}`
}

interface SuiteContext {
  endpoints: Record<string, EndpointTarget>
  personas: Record<string, PersonaTarget>
  roles: Partial<JourneyRoles>
  workload?: Targets['workload']
}

function suiteModel(value: unknown, path: string, context: SuiteContext): SuiteModel {
  const ref = requireObject(value, path)
  const endpoint = requireString(ref.endpoint, `${path}.endpoint`)
  if (!Object.hasOwn(context.endpoints, endpoint)) {
    throw new TargetsError(`${path}.endpoint references unknown endpoint "${endpoint}"`)
  }
  return { endpoint, deployment: requireString(ref.deployment, `${path}.deployment`) }
}

function publishedModel(value: unknown, path: string, context: SuiteContext): SuiteModel {
  const model = suiteModel(value, path, context)
  if (!context.endpoints[model.endpoint].publish.includes(model.deployment)) {
    throw new TargetsError(`${path}.deployment must be one of targets.endpoints.${model.endpoint}.publish`)
  }
  return model
}

function suitePersona(value: unknown, path: string, context: SuiteContext, needsRole = true): string {
  const key = requireString(value, path)
  if (!Object.hasOwn(context.personas, key)) throw new TargetsError(`${path} references unknown persona "${key}"`)
  if (needsRole && context.personas[key].expectedRole === 'None') {
    throw new TargetsError(`${path} must be a persona with a MOSAIC role, not "${key}"`)
  }
  return key
}

function parseSuiteRuntime(value: unknown, path: string, context: SuiteContext): SuiteRuntime {
  const runtime = requireObject(value, path)
  if (!Array.isArray(runtime.userGrants) || runtime.userGrants.length === 0) {
    throw new TargetsError(`${path}.userGrants must be a non-empty array`)
  }
  const userGrants = runtime.userGrants.map((item, index) => publishedModel(item, `${path}.userGrants[${index}]`, context))
  userGrants.forEach((model, index) => {
    if (userGrants.findIndex((other) => sameModel(other, model)) !== index) {
      throw new TargetsError(`${path}.userGrants lists ${modelLabel(model)} more than once`)
    }
  })
  let applicationGrant: SuiteModel | undefined
  if (runtime.applicationGrant !== undefined) {
    if (!context.workload) throw new TargetsError(`${path}.applicationGrant needs targets.workload`)
    applicationGrant = publishedModel(runtime.applicationGrant, `${path}.applicationGrant`, context)
  }
  let foreignGrant: SuiteGrant | undefined
  if (runtime.foreignGrant !== undefined) {
    const grantPath = `${path}.foreignGrant`
    const persona = suitePersona(requireObject(runtime.foreignGrant, grantPath).persona, `${grantPath}.persona`, context, false)
    const user = context.roles.user as string
    if (persona === user || context.personas[persona].upn.toLowerCase() === context.personas[user].upn.toLowerCase()) {
      throw new TargetsError(`${grantPath}.persona must be someone other than roles.user`)
    }
    foreignGrant = { persona, ...publishedModel(runtime.foreignGrant, grantPath, context) }
  }
  const version = (name: 'apiVersion' | 'modelsApiVersion') => {
    const value = optionalString(runtime[name], `${path}.${name}`)
    if (value !== undefined && !apiVersionPattern.test(value)) throw new TargetsError(`${path}.${name} must be an API version`)
    return value
  }
  return {
    userGrants,
    applicationGrant,
    foreignGrant,
    apiVersion: version('apiVersion'),
    modelsApiVersion: version('modelsApiVersion'),
    chatTokenParameter: runtime.chatTokenParameter === undefined
      ? undefined
      : requireOneOf(runtime.chatTokenParameter, ['max_tokens', 'max_completion_tokens'] as const, `${path}.chatTokenParameter`),
  }
}

function parseSuiteLimits(value: unknown, path: string): SuiteLimits {
  if (value === undefined) return {}
  const limits = requireObject(value, path)
  const parsed = {
    tokensPerMinute: optionalPositiveInteger(limits.tokensPerMinute, `${path}.tokensPerMinute`),
    calls: optionalPositiveInteger(limits.calls, `${path}.calls`),
    perSeconds: optionalPositiveInteger(limits.perSeconds, `${path}.perSeconds`),
  }
  if ((parsed.calls === undefined) !== (parsed.perSeconds === undefined)) {
    throw new TargetsError(`${path}.calls and ${path}.perSeconds go together`)
  }
  return parsed
}

function parseSuiteDisposables(value: unknown, path: string, context: SuiteContext, runtime?: SuiteRuntime): SuiteDisposables {
  const input = requireObject(value, path)
  const disposables: SuiteDisposables = {}
  if (input.publication !== undefined) {
    const publicationPath = `${path}.publication`
    const publication = requireObject(input.publication, publicationPath)
    const model = suiteModel(publication, publicationPath, context)
    // The suite unpublishes and removes this one, so it must never be a model the environment keeps.
    if (context.endpoints[model.endpoint].publish.includes(model.deployment)) {
      throw new TargetsError(`${publicationPath}.deployment must not be in targets.endpoints.${model.endpoint}.publish`)
    }
    disposables.publication = {
      ...model,
      requester: publication.requester === undefined
        ? suitePersona(context.roles.user, `${publicationPath}.requester`, context)
        : suitePersona(publication.requester, `${publicationPath}.requester`, context),
      limits: parseSuiteLimits(publication.limits, `${publicationPath}.limits`),
    }
  }
  const grant = (name: 'sharedBudget' | 'tokenLimit'): { grant: SuiteGrant; input: Json } => {
    const grantPath = `${path}.${name}`
    const grantInput = requireObject(input[name], grantPath)
    return {
      grant: { persona: suitePersona(grantInput.persona, `${grantPath}.persona`, context), ...publishedModel(grantInput, grantPath, context) },
      input: grantInput,
    }
  }
  if (input.sharedBudget !== undefined) {
    const { grant: sharedBudget, input: grantInput } = grant('sharedBudget')
    const calls = positiveInteger(grantInput.calls, `${path}.sharedBudget.calls`)
    const perSeconds = positiveInteger(grantInput.perSeconds, `${path}.sharedBudget.perSeconds`)
    if (calls !== sharedBudgetCalls || perSeconds !== sharedBudgetSeconds) {
      throw new TargetsError(`${path}.sharedBudget must allow ${sharedBudgetCalls} calls per ${sharedBudgetSeconds} seconds, the verifier's proof`)
    }
    disposables.sharedBudget = { ...sharedBudget, calls, perSeconds }
  }
  if (input.tokenLimit !== undefined) {
    const { grant: tokenLimit, input: grantInput } = grant('tokenLimit')
    disposables.tokenLimit = {
      ...tokenLimit,
      tokensPerMinute: positiveInteger(grantInput.tokensPerMinute, `${path}.tokenLimit.tokensPerMinute`, tokenLimitCeiling),
    }
  }
  const { sharedBudget, tokenLimit } = disposables
  if (sharedBudget && tokenLimit && sameModel(sharedBudget, tokenLimit)) {
    throw new TargetsError(`${path}.sharedBudget and ${path}.tokenLimit must be on different models`)
  }
  // Each run re-enables and revokes these grants and applies their models' plans, and every apply briefly refuses
  // calls to the model, so they mustn't be models the runtime journeys rely on.
  const relied = [...(runtime?.userGrants ?? []), runtime?.applicationGrant, runtime?.foreignGrant].filter(
    (model): model is SuiteModel => model !== undefined,
  )
  for (const [name, throwaway] of [['sharedBudget', sharedBudget], ['tokenLimit', tokenLimit]] as const) {
    if (throwaway && relied.some((model) => sameModel(model, throwaway))) {
      throw new TargetsError(`${path}.${name} must be on a model that suite.runtime doesn't use, because the suite applies its plan on every run`)
    }
  }
  return disposables
}

function parseSuite(value: unknown, context: SuiteContext): SuiteTargets {
  const suite = requireObject(value, 'targets.suite')
  const runtime = suite.runtime === undefined ? undefined : parseSuiteRuntime(suite.runtime, 'targets.suite.runtime', context)
  return {
    gateway: requireString(suite.gateway, 'targets.suite.gateway'),
    runtime,
    disposable: suite.disposable === undefined
      ? undefined
      : parseSuiteDisposables(suite.disposable, 'targets.suite.disposable', context, runtime),
  }
}

export function parseTargets(input: unknown): Targets {
  const root = requireObject(input, 'targets')
  const tenantId = requireString(root.tenantId, 'targets.tenantId')
  if (!guidPattern.test(tenantId)) throw new TargetsError('targets.tenantId must be a GUID')

  const originsInput = requireObject(root.origins, 'targets.origins')
  const origins = {
    web: normalizeOrigin(originsInput.web, 'targets.origins.web'),
    portal: normalizeOrigin(originsInput.portal, 'targets.origins.portal'),
    api: normalizeOrigin(originsInput.api, 'targets.origins.api'),
    gateway: normalizeOrigin(originsInput.gateway, 'targets.origins.gateway'),
  }

  const personasInput = requireObject(root.personas, 'targets.personas')
  const personas: Record<string, PersonaTarget> = {}
  for (const [key, value] of Object.entries(personasInput)) {
    if (!personaKeyPattern.test(key)) {
      throw new TargetsError(`targets.personas key "${key}" must match ${personaKeyPattern}`)
    }
    personas[key] = parsePersona(value, `targets.personas.${key}`)
  }

  const rolesInput = requireObject(root.roles, 'targets.roles')
  const roleKeys = ['admin', 'user', 'guest', 'noRole', 'outsider'] as const
  const roles: Partial<JourneyRoles> = {}
  for (const role of roleKeys) {
    const personaKey = role === 'admin' || role === 'user'
      ? requireString(rolesInput[role], `targets.roles.${role}`)
      : optionalString(rolesInput[role], `targets.roles.${role}`)
    if (personaKey === undefined) continue
    if (!personas[personaKey]) {
      throw new TargetsError(`targets.roles.${role} references unknown persona "${personaKey}"`)
    }
    roles[role] = personaKey
  }

  const endpointsInput = requireObject(root.endpoints ?? {}, 'targets.endpoints')
  const endpoints: Record<string, EndpointTarget> = {}
  for (const [name, value] of Object.entries(endpointsInput)) {
    endpoints[name] = parseEndpoint(value, `targets.endpoints.${name}`)
  }

  let negatives: Record<string, NegativeTarget> | undefined
  if (root.negatives !== undefined) {
    negatives = {}
    for (const [name, value] of Object.entries(requireObject(root.negatives, 'targets.negatives'))) {
      const negative = requireObject(value, `targets.negatives.${name}`)
      negatives[name] = {
        kind: requireOneOf(negative.kind, ['azure-openai', 'foundry'] as const, `targets.negatives.${name}.kind`),
        resourceId: parseResourceId(negative.resourceId, `targets.negatives.${name}.resourceId`, false),
        reason: requireString(negative.reason, `targets.negatives.${name}.reason`),
      }
    }
  }

  let workload: Targets['workload']
  if (root.workload !== undefined) {
    const workloadInput = requireObject(root.workload, 'targets.workload')
    const appId = optionalString(workloadInput.appId, 'targets.workload.appId')
    if (appId && !guidPattern.test(appId)) throw new TargetsError('targets.workload.appId must be a GUID')
    const objectId = optionalString(workloadInput.objectId, 'targets.workload.objectId')
    if (objectId && !guidPattern.test(objectId)) throw new TargetsError('targets.workload.objectId must be a GUID')
    workload = { displayName: requireString(workloadInput.displayName, 'targets.workload.displayName'), appId, objectId }
  }

  return {
    environment: requireString(root.environment, 'targets.environment'),
    tenantId,
    origins,
    roles: roles as JourneyRoles,
    personas,
    workload,
    endpoints,
    negatives,
    suite: root.suite === undefined ? undefined : parseSuite(root.suite, { endpoints, personas, roles, workload }),
  }
}

export function loadTargets(path = targetsFile()): Targets {
  if (!existsSync(path)) {
    throw new TargetsError(
      `No targets manifest at ${path}. Copy e2e/targets.example.json to e2e/targets.local.json and fill in your environment.`,
    )
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(readFileSync(path, 'utf8'))
  } catch (error) {
    throw new TargetsError(`${path} is not valid JSON: ${(error as Error).message}`)
  }
  return parseTargets(parsed)
}

export function persona(targets: Targets, key: string): PersonaTarget {
  const found = targets.personas[key]
  if (!found) throw new TargetsError(`Unknown persona "${key}". Known personas: ${Object.keys(targets.personas).join(', ')}`)
  return found
}

const refPrefix = '@target:'

export function resolveTargetRef(targets: Targets, value: string): string {
  if (!value.startsWith(refPrefix)) return value
  const path = value.slice(refPrefix.length).split('.')
  let current: unknown = targets
  for (const segment of path) {
    if (!isObject(current) || !Object.hasOwn(current, segment)) {
      throw new TargetsError(`${value} does not resolve to a manifest value`)
    }
    current = current[segment]
  }
  if (typeof current !== 'string' && typeof current !== 'number') {
    throw new TargetsError(`${value} must resolve to a string or number`)
  }
  return String(current)
}

export function flag(name: string): boolean {
  return /^(1|true|yes)$/i.test(process.env[name] ?? '')
}

export const flags = {
  interactive: () => flag('MOSAIC_E2E_INTERACTIVE'),
  allowWrites: () => flag('MOSAIC_E2E_ALLOW_WRITES'),
  sendModelRequests: () => flag('MOSAIC_E2E_SEND_MODEL_REQUESTS'),
  headless: () => flag('MOSAIC_E2E_HEADLESS'),
  htmlReport: () => flag('MOSAIC_E2E_HTML_REPORT'),
}
