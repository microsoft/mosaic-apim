import { join } from 'node:path'
import { type Targets, grantIdPattern, persona, resolveTargetRef } from './config.ts'
import {
  type FlagSpec,
  type PersonaChoice,
  type TokenNeeds,
  type ValueFlag,
  type VerifyPersonas,
  VerifyError,
  manifestFlags,
  parseVerifierFlags,
  repoRoot,
  samePerson,
} from './verify.ts'

/**
 * Plans runs of scripts/verify_mcp_access.py, Phase 11's MCP client, for the live driver's "verify-mcp" action.
 * It mirrors verify.ts: the driver signs the personas in, passes their MOSAIC API tokens to the verifier in its
 * environment, and enters its device codes in the right persona's browser, so nobody copies a token by hand.
 */

export const mcpVerifierScript = join(repoRoot, 'scripts', 'verify_mcp_access.py')

export type McpUserTokenSource = 'env' | 'device-code'
export type McpApplicationTokenSource = 'env' | 'client-credentials'

export interface McpVerifyPlan {
  /** Arguments for the verifier, starting with the manifest's API and gateway origins. */
  argv: string[]
  /** People's grants on servers with the echo and add tools (M5), held by one user. */
  userEntitlements: string[]
  /** An application's grants on servers with the echo and add tools (M5). The admin reads their details. */
  applicationEntitlements: string[]
  /** The user's grant on a server whose ask_model tool calls a governed model (M9). */
  onBehalfEntitlement?: string
  /** The model grant of the application that server calls models as, which the attribution must name. */
  modelCallerEntitlement?: string
  userTokenSource: McpUserTokenSource
  applicationTokenSource: McpApplicationTokenSource
  checkUngrantedUser: boolean
  checkMissingScope: boolean
  /** M6: the grant whose call limit the run spends. */
  proveCallLimit?: string
  /** M6: the grant whose cost center's pooled quota on its server the run spends. */
  provePooledQuota?: string
  /** M7: the grant the run waits to see revoked. */
  watchRevocation?: string
  revocationTimeoutSeconds: number
  revocationIntervalSeconds: number
  /** M9: wait for the user's usage report to attribute the model call. */
  awaitAttribution: boolean
  attributionTimeoutSeconds: number
  attributionIntervalSeconds: number
}

const grant: ValueFlag = { pattern: grantIdPattern, expects: 'a grant ID' }

// The ranges and defaults match the verifier's own checks, so a bad run fails before anyone signs in.
const valueFlags: Readonly<Record<string, ValueFlag>> = {
  '--user-entitlement': { ...grant, repeat: true },
  '--application-entitlement': { ...grant, repeat: true },
  '--on-behalf-entitlement': grant,
  '--model-caller-entitlement': grant,
  '--prove-call-limit': grant,
  '--prove-pooled-quota': grant,
  '--watch-revocation': grant,
  '--user-token-source': { choices: ['env', 'device-code'], expects: 'env or device-code' },
  '--application-token-source': { choices: ['env', 'client-credentials'], expects: 'env or client-credentials' },
  '--revocation-timeout': { range: [60, 3600], expects: 'a whole number of seconds from 60 to 3600' },
  '--revocation-interval': { range: [10, 300], expects: 'a whole number of seconds from 10 to 300' },
  '--attribution-timeout': { range: [60, 3600], expects: 'a whole number of seconds from 60 to 3600' },
  '--attribution-interval': { range: [30, 600], expects: 'a whole number of seconds from 30 to 600' },
}
const switchFlags = new Set(['--send-model-requests', '--check-ungranted-user', '--check-missing-scope', '--await-attribution'])
const mcpFlags: FlagSpec = { valueFlags, switchFlags, manifestFlags }

/** Every MCP verifier flag the harness knows, including the two it always sets from the manifest. */
export const mcpVerifierFlags: readonly string[] = [...Object.keys(valueFlags), ...switchFlags, ...manifestFlags]

export const mcpDefaults = {
  revocationTimeoutSeconds: 900,
  revocationIntervalSeconds: 30,
  attributionTimeoutSeconds: 1800,
  attributionIntervalSeconds: 60,
} as const

/**
 * Checks the verifier flags passed to "verify-mcp" and builds the verifier's arguments. Only known flags are
 * accepted, a grant ID can be an @target: reference into the manifest, such as @target:mcp.grants.tools-user.id,
 * and the API and gateway origins always come from the manifest.
 */
export function planMcpVerification(targets: Targets, args: readonly string[]): McpVerifyPlan {
  const { values, switches } = parseVerifierFlags(mcpFlags, args, (value) => resolveTargetRef(targets, value))
  const single = (name: string) => values.get(name)?.[0]
  const userEntitlements = values.get('--user-entitlement') ?? []
  const applicationEntitlements = values.get('--application-entitlement') ?? []
  const onBehalfEntitlement = single('--on-behalf-entitlement')
  const modelCallerEntitlement = single('--model-caller-entitlement')
  const proveCallLimit = single('--prove-call-limit')
  const provePooledQuota = single('--prove-pooled-quota')
  const watchRevocation = single('--watch-revocation')
  const awaitAttribution = switches.has('--await-attribution')
  const checkUngrantedUser = switches.has('--check-ungranted-user')
  const checkMissingScope = switches.has('--check-missing-scope')
  // The grants the run calls the echo and add tools with, which proofs and the revocation watch run on.
  const own = [...userEntitlements, ...applicationEntitlements]
  const listed = [...own, ...(onBehalfEntitlement === undefined ? [] : [onBehalfEntitlement])]
  const named = [...listed, ...(modelCallerEntitlement === undefined ? [] : [modelCallerEntitlement])]
  if (listed.length === 0) {
    throw new VerifyError('Name at least one --user-entitlement, --application-entitlement or --on-behalf-entitlement.')
  }
  if (new Set(named).size !== named.length) throw new VerifyError('List each grant only once.')
  if (onBehalfEntitlement !== undefined && !switches.has('--send-model-requests')) {
    throw new VerifyError('Add --send-model-requests to acknowledge that ask_model sends a real, billed model request.')
  }
  if ((awaitAttribution || modelCallerEntitlement !== undefined) && onBehalfEntitlement === undefined) {
    throw new VerifyError('--await-attribution and --model-caller-entitlement need an --on-behalf-entitlement.')
  }
  if (modelCallerEntitlement !== undefined && !awaitAttribution) {
    throw new VerifyError('--model-caller-entitlement is only used with --await-attribution.')
  }
  if ((checkUngrantedUser || checkMissingScope) && userEntitlements.length === 0) {
    throw new VerifyError('--check-ungranted-user and --check-missing-scope need a --user-entitlement.')
  }
  if (proveCallLimit !== undefined && provePooledQuota !== undefined) {
    throw new VerifyError('Choose one proof per run: --prove-call-limit or --prove-pooled-quota.')
  }
  const onListed: [string, string | undefined][] = [
    ['--prove-call-limit', proveCallLimit],
    ['--prove-pooled-quota', provePooledQuota],
    ['--watch-revocation', watchRevocation],
  ]
  for (const [flag, value] of onListed) {
    if (value !== undefined && !own.includes(value)) {
      throw new VerifyError(`${flag} must name a --user-entitlement or --application-entitlement in this run.`)
    }
  }
  if (watchRevocation !== undefined && awaitAttribution) {
    throw new VerifyError('Wait for one thing per run: --watch-revocation or --await-attribution.')
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
    onBehalfEntitlement,
    modelCallerEntitlement,
    userTokenSource: (single('--user-token-source') ?? 'env') as McpUserTokenSource,
    applicationTokenSource: (single('--application-token-source') ?? 'env') as McpApplicationTokenSource,
    checkUngrantedUser,
    checkMissingScope,
    proveCallLimit,
    provePooledQuota,
    watchRevocation,
    revocationTimeoutSeconds: seconds('--revocation-timeout', mcpDefaults.revocationTimeoutSeconds),
    revocationIntervalSeconds: seconds('--revocation-interval', mcpDefaults.revocationIntervalSeconds),
    awaitAttribution,
    attributionTimeoutSeconds: seconds('--attribution-timeout', mcpDefaults.attributionTimeoutSeconds),
    attributionIntervalSeconds: seconds('--attribution-interval', mcpDefaults.attributionIntervalSeconds),
  }
}

/**
 * Picks the personas a run needs: the choices given, or else the manifest's journey roles. The user's MOSAIC API
 * token reads their grants' connection details. The admin's reads an application's, what the server's last
 * apply compiled for a pooled-quota proof, and whether the on-behalf server passes references on.
 */
export function mcpVerifyPersonas(targets: Targets, plan: McpVerifyPlan, choice: PersonaChoice = {}): VerifyPersonas {
  const user = choice.user ?? targets.roles.user
  persona(targets, user)

  let admin: string | undefined
  const needsAdmin =
    plan.applicationEntitlements.length > 0 || plan.provePooledQuota !== undefined || plan.onBehalfEntitlement !== undefined
  if (needsAdmin) {
    admin = choice.admin ?? targets.roles.admin
    persona(targets, admin)
  } else if (choice.admin !== undefined) {
    throw new VerifyError('--admin is only used with --application-entitlement, --prove-pooled-quota or --on-behalf-entitlement.')
  }

  let stranger: string | undefined
  if (plan.checkUngrantedUser && plan.userTokenSource === 'device-code') {
    stranger = choice.stranger ?? targets.roles.outsider ?? targets.roles.noRole
    if (stranger === undefined) {
      throw new VerifyError(
        '--check-ungranted-user signs in someone with no grant for these MCP servers. Name them with --stranger <persona>, or set roles.outsider in the manifest.',
      )
    }
    persona(targets, stranger)
    if (samePerson(targets, stranger, user)) throw new VerifyError(`The ungranted user must be someone other than ${user}, who holds the grants.`)
  } else if (choice.stranger !== undefined) {
    throw new VerifyError('--stranger is only used with --check-ungranted-user and --user-token-source device-code.')
  }
  return { user, admin, stranger }
}

/**
 * How long each MOSAIC API token must stay valid: time for sign-ins and checks, plus a revocation watch or the
 * wait for an attribution, which read MOSAIC with it until the end.
 */
export function mcpControlTokenNeeds(plan: McpVerifyPlan): TokenNeeds {
  const signInsAndChecks = 20 * 60
  if (plan.watchRevocation !== undefined) {
    return {
      seconds: signInsAndChecks + plan.revocationTimeoutSeconds + plan.revocationIntervalSeconds + 60,
      shorten: 'Lower --revocation-timeout and run it again.',
    }
  }
  if (plan.awaitAttribution) {
    return {
      seconds: signInsAndChecks + plan.attributionTimeoutSeconds + plan.attributionIntervalSeconds + 60,
      shorten: 'Lower --attribution-timeout and run it again.',
    }
  }
  return { seconds: signInsAndChecks }
}

/**
 * How long the driver lets the verifier run: two device sign-ins, whose codes last up to 15 minutes each, the
 * checks, and a revocation watch or the wait for an attribution.
 */
export function mcpVerifierTimeoutMs(plan: McpVerifyPlan): number {
  const wait = plan.watchRevocation !== undefined
    ? plan.revocationTimeoutSeconds + plan.revocationIntervalSeconds
    : plan.awaitAttribution
      ? plan.attributionTimeoutSeconds + plan.attributionIntervalSeconds
      : 0
  return (45 * 60 + wait) * 1_000
}

/**
 * Variables drive.ts may pass from its own environment to one MCP verifier run. MOSAIC_SMOKE_USER_RUNTIME_TOKEN is
 * the user's model token, which --check-missing-scope sends to show a token without Mcp.Invoke is refused. The
 * MOSAIC API tokens are never among them: they always come from the personas' browsers.
 */
export const mcpForwardedVariables = [
  'MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_MCP_APPLICATION_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_MCP_UNGRANTED_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_USER_RUNTIME_TOKEN',
  'MOSAIC_SMOKE_APPLICATION_CLIENT_ID',
  'MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET',
] as const

/** The forwarded variables that hold credentials, which are redacted from the verifier's output by value. */
export const mcpSecretVariables: ReadonlySet<string> = new Set<string>(
  mcpForwardedVariables.filter((name) => name !== 'MOSAIC_SMOKE_APPLICATION_CLIENT_ID'),
)

/** How the MCP verifier names the people it signs in, mapped to the persona that plays each part. */
export const mcpSignInSubjects: Readonly<Record<string, 'user' | 'stranger'>> = {
  'the user who holds these grants': 'user',
  'a different user, one with no grant for these MCP servers': 'stranger',
}
