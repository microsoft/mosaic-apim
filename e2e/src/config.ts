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

export interface Targets {
  environment: string
  tenantId: string
  origins: Record<'web' | 'portal' | 'api' | 'gateway', string>
  roles: JourneyRoles
  personas: Record<string, PersonaTarget>
  workload?: { displayName: string; appId?: string }
  endpoints: Record<string, EndpointTarget>
  negatives?: Record<string, NegativeTarget>
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
    workload = { displayName: requireString(workloadInput.displayName, 'targets.workload.displayName'), appId }
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
