import { join, resolve } from 'node:path'
import { type Targets, persona } from './config.ts'
import { e2eRoot } from './paths.ts'
import { redact, truncate } from './redact.ts'

/**
 * Plans runs of scripts/verify_model_access.py for the live driver's "verify" action. The driver signs the
 * personas in, passes their MOSAIC API tokens to the verifier in its environment, and enters the verifier's
 * device codes in the right persona's browser, so nobody copies a token by hand.
 */

export class VerifyError extends Error {}

export const repoRoot = resolve(e2eRoot, '..')
export const verifierScript = join(repoRoot, 'scripts', 'verify_model_access.py')

export type UserTokenSource = 'env' | 'device-code'
export type ApplicationTokenSource = 'env' | 'client-credentials'

export interface VerifyPlan {
  /** Arguments for the verifier, starting with the manifest's API and gateway origins. */
  argv: string[]
  userEntitlements: string[]
  applicationEntitlements: string[]
  /** Grants held by someone other than the user, which the user must not list, read or retrieve a key for. */
  foreignUserEntitlements: string[]
  /** Entra Agent ID grants, called with MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN. The admin reads their connection details. */
  agentEntitlements: string[]
  /** Security-group grants, called with MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN. The admin reads their connection details. */
  groupEntitlements: string[]
  userTokenSource: UserTokenSource
  applicationTokenSource: ApplicationTokenSource
  checkUngrantedUser: boolean
  watchRevocation?: string
  revocationTimeoutSeconds: number
  revocationIntervalSeconds: number
}

interface ValueFlag {
  pattern?: RegExp
  choices?: readonly string[]
  range?: readonly [number, number]
  repeat?: boolean
  expects: string
}

// MOSAIC grant IDs look like ent_<32 hex digits>. A leading letter or digit keeps a value from being read as a flag.
const identifier = /^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$/
const apiVersion = /^[A-Za-z0-9][A-Za-z0-9.-]{0,31}$/

// The ranges and defaults match the verifier's own checks, so a bad run fails before anyone signs in.
const valueFlags: Readonly<Record<string, ValueFlag>> = {
  '--user-entitlement': { pattern: identifier, repeat: true, expects: 'a grant ID' },
  '--application-entitlement': { pattern: identifier, repeat: true, expects: 'a grant ID' },
  '--foreign-user-entitlement': { pattern: identifier, repeat: true, expects: 'a grant ID' },
  '--agent-entitlement': { pattern: identifier, repeat: true, expects: 'a grant ID' },
  '--group-entitlement': { pattern: identifier, repeat: true, expects: 'a grant ID' },
  '--watch-revocation': { pattern: identifier, expects: 'a grant ID' },
  '--api-version': { pattern: apiVersion, expects: 'an API version, such as 2024-10-21' },
  '--models-api-version': { pattern: apiVersion, expects: 'an API version, such as 2024-05-01-preview' },
  '--chat-token-parameter': { choices: ['max_tokens', 'max_completion_tokens'], expects: 'max_tokens or max_completion_tokens' },
  '--user-token-source': { choices: ['env', 'device-code'], expects: 'env or device-code' },
  '--application-token-source': { choices: ['env', 'client-credentials'], expects: 'env or client-credentials' },
  '--revocation-timeout': { range: [60, 3600], expects: 'a whole number of seconds from 60 to 3600' },
  '--revocation-interval': { range: [10, 300], expects: 'a whole number of seconds from 10 to 300' },
}
const switchFlags = new Set(['--send-model-requests', '--check-ungranted-user', '--prove-shared-budget', '--prove-token-limit'])
const manifestFlags = new Set(['--api-base-url', '--gateway-origin'])

/** Every verifier flag the harness knows, including the two it always sets from the manifest. */
export const verifierFlags: readonly string[] = [...Object.keys(valueFlags), ...switchFlags, ...manifestFlags]

const defaultRevocationTimeoutSeconds = 900
const defaultRevocationIntervalSeconds = 30

function checkValue(name: string, flag: ValueFlag, value: string): void {
  const valid = flag.choices
    ? flag.choices.includes(value)
    : flag.range
      ? /^\d{1,5}$/.test(value) && Number(value) >= flag.range[0] && Number(value) <= flag.range[1]
      : flag.pattern?.test(value) === true
  if (!valid) throw new VerifyError(`${name} needs ${flag.expects}, not "${truncate(value, 60)}"`)
}

/**
 * Checks the verifier flags passed to "verify" and builds the verifier's arguments. Only known flags are
 * accepted, so nothing else reaches the verifier's command line. The API and gateway origins always come from
 * the manifest.
 */
export function planVerification(targets: Targets, args: readonly string[]): VerifyPlan {
  const values = new Map<string, string[]>()
  const switches = new Set<string>()
  for (let index = 0; index < args.length; index += 1) {
    const raw = args[index]
    if (!raw.startsWith('--')) {
      throw new VerifyError(`Unexpected argument "${truncate(raw, 60)}". Pass verifier flags, such as --user-entitlement <id>.`)
    }
    const equals = raw.indexOf('=')
    const name = equals === -1 ? raw : raw.slice(0, equals)
    if (manifestFlags.has(name)) throw new VerifyError(`${name} comes from the targets manifest, so don't pass it.`)
    if (switchFlags.has(name)) {
      if (equals !== -1) throw new VerifyError(`${name} takes no value`)
      switches.add(name)
      continue
    }
    const flag = Object.hasOwn(valueFlags, name) ? valueFlags[name] : undefined
    if (!flag) throw new VerifyError(`Unknown verifier flag "${truncate(name, 60)}". Use the full flag name.`)
    let value: string | undefined
    if (equals === -1) {
      index += 1
      value = args[index]
    } else {
      value = raw.slice(equals + 1)
    }
    if (value === undefined || value === '') throw new VerifyError(`${name} needs ${flag.expects}`)
    checkValue(name, flag, value)
    const seen = values.get(name) ?? []
    if (seen.length > 0 && !flag.repeat) throw new VerifyError(`Give ${name} only once`)
    values.set(name, [...seen, value])
  }

  const single = (name: string) => values.get(name)?.[0]
  const userEntitlements = values.get('--user-entitlement') ?? []
  const applicationEntitlements = values.get('--application-entitlement') ?? []
  const foreignUserEntitlements = values.get('--foreign-user-entitlement') ?? []
  const agentEntitlements = values.get('--agent-entitlement') ?? []
  const groupEntitlements = values.get('--group-entitlement') ?? []
  // The grants the run reads keys and tokens for, which proofs and the revocation watch run on.
  const own = [...userEntitlements, ...applicationEntitlements]
  // Every grant the run calls models with. Grants held by someone else are only checked through MOSAIC's API.
  const calling = [...own, ...agentEntitlements, ...groupEntitlements]
  const listed = [...calling, ...foreignUserEntitlements]
  const watchRevocation = single('--watch-revocation')
  if (listed.length === 0) {
    throw new VerifyError(
      'Name at least one --user-entitlement, --application-entitlement, --agent-entitlement, --group-entitlement or --foreign-user-entitlement.',
    )
  }
  if (calling.length > 0 && !switches.has('--send-model-requests')) {
    throw new VerifyError('Add --send-model-requests to acknowledge that the checks send real, billed model requests.')
  }
  if (new Set(listed).size !== listed.length) throw new VerifyError('List each grant only once.')
  if (switches.has('--check-ungranted-user') && userEntitlements.length === 0) {
    throw new VerifyError('--check-ungranted-user needs a --user-entitlement in the same run.')
  }
  if (switches.has('--prove-shared-budget') && switches.has('--prove-token-limit')) {
    throw new VerifyError('Choose one proof per run: --prove-shared-budget or --prove-token-limit.')
  }
  if ((switches.has('--prove-shared-budget') || switches.has('--prove-token-limit')) && own.length === 0) {
    throw new VerifyError('A proof needs a --user-entitlement or --application-entitlement to run on.')
  }
  if (watchRevocation !== undefined && !own.includes(watchRevocation)) {
    throw new VerifyError('--watch-revocation must name a --user-entitlement or --application-entitlement in this run.')
  }

  const argv = ['--api-base-url', targets.origins.api, '--gateway-origin', targets.origins.gateway]
  for (const [name, list] of values) {
    for (const value of list) argv.push(name, value)
  }
  argv.push(...switches)
  const seconds = (name: string, fallback: number) => Number(single(name) ?? fallback)
  return {
    argv,
    userEntitlements,
    applicationEntitlements,
    foreignUserEntitlements,
    agentEntitlements,
    groupEntitlements,
    userTokenSource: (single('--user-token-source') ?? 'env') as UserTokenSource,
    applicationTokenSource: (single('--application-token-source') ?? 'env') as ApplicationTokenSource,
    checkUngrantedUser: switches.has('--check-ungranted-user'),
    watchRevocation,
    revocationTimeoutSeconds: seconds('--revocation-timeout', defaultRevocationTimeoutSeconds),
    revocationIntervalSeconds: seconds('--revocation-interval', defaultRevocationIntervalSeconds),
  }
}

export interface VerifyPersonas {
  /** Holds the user grants. Its MOSAIC API token reads their connection details on every run. */
  user: string
  /**
   * Only for application grants, whose connection details and keys only an admin can read, and for grants held
   * by someone else, which the admin confirms are real before the user's refusals count.
   */
  admin?: string
  /** Signs in for --check-ungranted-user when the verifier signs users in with device codes. */
  stranger?: string
}

export interface PersonaChoice {
  admin?: string
  user?: string
  stranger?: string
}

function samePerson(targets: Targets, first: string, second: string): boolean {
  return first === second || persona(targets, first).upn.toLowerCase() === persona(targets, second).upn.toLowerCase()
}

/** Picks the personas a run needs: the choices given, or else the manifest's journey roles. */
export function verifyPersonas(targets: Targets, plan: VerifyPlan, choice: PersonaChoice = {}): VerifyPersonas {
  const user = choice.user ?? targets.roles.user
  persona(targets, user)

  let admin: string | undefined
  // The verifier reads these grants' connection details with the admin's MOSAIC API token.
  const needsAdmin =
    plan.applicationEntitlements.length > 0 ||
    plan.foreignUserEntitlements.length > 0 ||
    plan.agentEntitlements.length > 0 ||
    plan.groupEntitlements.length > 0
  if (needsAdmin) {
    admin = choice.admin ?? targets.roles.admin
    persona(targets, admin)
    // The verifier checks that the end user can't read an application's key, so they must be different people.
    if (plan.applicationEntitlements.length > 0 && samePerson(targets, admin, user)) {
      throw new VerifyError(`The admin and the user must be different people, not both ${user}.`)
    }
  } else if (choice.admin !== undefined) {
    throw new VerifyError(
      '--admin is only used with --application-entitlement, --foreign-user-entitlement, --agent-entitlement or --group-entitlement.',
    )
  }

  let stranger: string | undefined
  if (plan.checkUngrantedUser && plan.userTokenSource === 'device-code') {
    stranger = choice.stranger ?? targets.roles.outsider ?? targets.roles.noRole
    if (stranger === undefined) {
      throw new VerifyError(
        '--check-ungranted-user signs in someone with no grant for these models. Name them with --stranger <persona>, or set roles.outsider in the manifest.',
      )
    }
    persona(targets, stranger)
    if (samePerson(targets, stranger, user)) throw new VerifyError(`The ungranted user must be someone other than ${user}, who holds the grants.`)
  } else if (choice.stranger !== undefined) {
    throw new VerifyError('--stranger is only used with --check-ungranted-user and --user-token-source device-code.')
  }
  return { user, admin, stranger }
}

/** How the verifier names the people it signs in, mapped to the persona that plays each part. */
export const signInSubjects: Readonly<Record<string, 'user' | 'stranger'>> = {
  'the user who holds these grants': 'user',
  'a different user, one with no grant for these models': 'stranger',
}

export interface SignInPrompt {
  who: string
  subject?: 'user' | 'stranger'
  uri: string
  code: string
}

const signInPrompt = /^SIGN IN as (.{1,200}): open (\S{1,500}) and enter the code ([A-Za-z0-9-]{4,32})$/

/** Reads the verifier's device-code prompt: "SIGN IN as <who>: open <uri> and enter the code <code>". */
export function parseSignInPrompt(line: string): SignInPrompt | undefined {
  const match = signInPrompt.exec(line.trim())
  if (!match) return undefined
  const [, who, uri, code] = match
  return { who, subject: Object.hasOwn(signInSubjects, who) ? signInSubjects[who] : undefined, uri, code }
}

const deviceLoginHosts = new Set(['microsoft.com', 'www.microsoft.com', 'login.microsoft.com', 'login.microsoftonline.com'])

/** True for Microsoft's device sign-in pages, the only pages the driver opens from verifier output. */
export function isDeviceLoginUrl(value: string): boolean {
  let url: URL
  try {
    url = new URL(value)
  } catch {
    return false
  }
  return url.protocol === 'https:' && url.port === '' && url.username === '' && url.password === '' && deviceLoginHosts.has(url.hostname)
}

const jwtShape = /^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*$/

/** The JWT in an "Authorization: Bearer" header, or undefined. */
export function bearerToken(header: string | null | undefined): string | undefined {
  const match = /^Bearer\s+(\S+)$/i.exec(header ?? '')
  return match && jwtShape.test(match[1]) ? match[1] : undefined
}

export function tokenClaims(token: string): Record<string, unknown> | undefined {
  const parts = token.split('.')
  if (parts.length !== 3) return undefined
  try {
    const claims: unknown = JSON.parse(Buffer.from(parts[1], 'base64url').toString('utf8'))
    return typeof claims === 'object' && claims !== null && !Array.isArray(claims) ? (claims as Record<string, unknown>) : undefined
  } catch {
    return undefined
  }
}

export interface TokenHolder {
  personaKey: string
  upn: string
  objectId?: string
  tenantId: string
}

/**
 * Seconds each MOSAIC API token must stay valid: time for sign-ins and checks, plus the whole revocation watch,
 * which reads the grant's status with it until the end.
 */
export function controlTokenSeconds(plan: VerifyPlan): number {
  const signInsAndChecks = 20 * 60
  const watch = plan.watchRevocation ? plan.revocationTimeoutSeconds + plan.revocationIntervalSeconds + 60 : 0
  return signInsAndChecks + watch
}

/**
 * Checks a MOSAIC API token taken from a persona's browser before handing it to the verifier: it must be for
 * the persona, from the manifest's tenant, and last the run. Returns the problem, or undefined. The messages
 * never include the token or its claims.
 */
export function controlTokenProblem(token: string, holder: TokenHolder, nowSeconds: number, plan: VerifyPlan): string | undefined {
  const subject = `The MOSAIC API token ${holder.personaKey}'s browser sent`
  const claims = tokenClaims(token)
  if (!claims) return `${subject} isn't a readable JWT.`
  if (typeof claims.tid !== 'string' || claims.tid.toLowerCase() !== holder.tenantId.toLowerCase()) {
    return `${subject} is from a different tenant than the manifest's tenantId.`
  }
  const names = ['preferred_username', 'upn', 'email', 'unique_name']
    .map((claim) => claims[claim])
    .filter((value): value is string => typeof value === 'string')
    .map((value) => value.toLowerCase())
  const sameObject = holder.objectId !== undefined && typeof claims.oid === 'string' && claims.oid.toLowerCase() === holder.objectId.toLowerCase()
  if (!sameObject && !names.includes(holder.upn.toLowerCase())) {
    return `${subject} is for a different account than persona ${holder.personaKey}. Sign that profile out and back in as the right account.`
  }
  if (typeof claims.exp !== 'number') return `${subject} has no expiry.`
  const needed = controlTokenSeconds(plan)
  const left = Math.floor(claims.exp - nowSeconds)
  if (left < needed) {
    const shorten = plan.watchRevocation ? ' Lower --revocation-timeout and run it again.' : ''
    return `${subject} expires in ${Math.max(left, 0)} seconds, and this run needs it for ${needed}.${shorten}`
  }
  return undefined
}

/**
 * How long the driver lets the verifier run: two device sign-ins, whose codes last up to 15 minutes each, the
 * checks, and the revocation watch.
 */
export function verifierTimeoutMs(plan: VerifyPlan): number {
  const watch = plan.watchRevocation ? plan.revocationTimeoutSeconds + plan.revocationIntervalSeconds : 0
  return (45 * 60 + watch) * 1_000
}

export const controlTokenVariables = {
  user: 'MOSAIC_SMOKE_USER_CONTROL_TOKEN',
  admin: 'MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN',
} as const

/**
 * Variables drive.ts may pass from its own environment to one verifier run, such as a workload's short-lived
 * secret. The MOSAIC API tokens are never among them: they always come from the personas' browsers.
 */
export const forwardedVariables = [
  'MOSAIC_SMOKE_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_APPLICATION_CLIENT_ID',
  'MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET',
  'MOSAIC_SMOKE_PAYLOAD',
] as const

const secretVariables = new Set<string>([
  'MOSAIC_SMOKE_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_GROUP_MEMBER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET',
])

const maxForwardedLength = 16_384

/** The forwarded variables set in drive.ts's environment. */
export function forwardedEnvironment(env: Record<string, string | undefined>): Record<string, string> {
  const forwarded: Record<string, string> = {}
  for (const name of forwardedVariables) {
    const value = env[name]
    if (value !== undefined && value.trim() !== '') forwarded[name] = value
  }
  return forwarded
}

/** Checks the variables an RPC asks to pass to the verifier. */
export function checkForwarded(value: unknown): Record<string, string> {
  if (value === undefined || value === null) return {}
  if (typeof value !== 'object' || Array.isArray(value)) throw new VerifyError('"env" must be an object')
  const forwarded: Record<string, string> = {}
  for (const [name, item] of Object.entries(value)) {
    if (!(forwardedVariables as readonly string[]).includes(name)) {
      throw new VerifyError(`${truncate(name, 60)} can't be passed to the verifier. It takes ${forwardedVariables.join(', ')}.`)
    }
    if (typeof item !== 'string' || item.trim() === '' || item.length > maxForwardedLength) {
      throw new VerifyError(`${name} must be a non-empty string of at most ${maxForwardedLength} characters`)
    }
    forwarded[name] = item
  }
  return forwarded
}

/** The forwarded values that are credentials. */
function forwardedSecrets(forwarded: Record<string, string>): string[] {
  return Object.entries(forwarded)
    .filter(([name]) => secretVariables.has(name))
    .map(([, value]) => value)
}

/**
 * What the driver starts the verifier with. The environment is the driver's own, minus any MOSAIC_SMOKE_*
 * variables it was started with, plus this run's forwarded variables and the MOSAIC API tokens taken from the
 * personas' browsers. The secrets are every credential among them, to redact from the verifier's output.
 */
export function verifierLaunch(
  base: Record<string, string | undefined>,
  forwarded: Record<string, string>,
  control: { user: string; admin?: string },
): { env: Record<string, string>; secrets: string[] } {
  const env: Record<string, string> = {}
  for (const [name, value] of Object.entries(base)) {
    if (value !== undefined && !name.toUpperCase().startsWith('MOSAIC_SMOKE_')) env[name] = value
  }
  Object.assign(env, forwarded)
  env[controlTokenVariables.user] = control.user
  if (control.admin !== undefined) env[controlTokenVariables.admin] = control.admin
  env.PYTHONUNBUFFERED = '1'
  env.PYTHONIOENCODING = 'utf-8'
  const secrets = [control.user, ...(control.admin === undefined ? [] : [control.admin]), ...forwardedSecrets(forwarded)]
  return { env, secrets }
}

/** A line of verifier output, safe to print: credentials are redacted and long lines cut short. */
export function publicLine(line: string, secrets: Iterable<string>): string {
  return truncate(redact(line.trimEnd(), secrets), 2_000)
}
