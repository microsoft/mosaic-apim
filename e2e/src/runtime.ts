import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'
import type { Page, TestInfo } from '@playwright/test'
import { type SuiteRuntime, type Targets, persona } from './config.ts'
import { type DeviceSignIn, deviceCodePersona, followDeviceSignIns, typeDeviceCode } from './device-code.ts'
import type { PersonaPool } from './fixtures.ts'
import { personaApiToken } from './mosaic-api.ts'
import { redact } from './redact.ts'
import {
  type ApplicationTokenSource,
  type PersonaChoice,
  type SignInPrompt,
  type UserTokenSource,
  type VerifyPersonas,
  type VerifyPlan,
  controlTokenProblem,
  controlTokenSeconds,
  forwardedEnvironment,
  parseSignInPrompt,
  planVerification,
  publicLine,
  repoRoot,
  tokenClaims,
  verifierLaunch,
  verifierScript,
  verifierTimeoutMs,
  verifyPersonas,
} from './verify.ts'

/**
 * Runs scripts/verify_model_access.py from the ordered specs. It reuses the live driver's planning and launch
 * rules from verify.ts: the MOSAIC API tokens come from the personas' browsers, the verifier's device codes are
 * entered in the right persona's browser, and every line of output is redacted before it is printed or kept.
 */

export type Proof = 'shared-budget' | 'token-limit'

export interface VerifierRun {
  userGrants?: readonly string[]
  applicationGrants?: readonly string[]
  /** Grants held by someone other than the user, which the user must not list, read or retrieve a key for. */
  foreignGrants?: readonly string[]
  userTokenSource?: UserTokenSource
  applicationTokenSource?: ApplicationTokenSource
  checkUngrantedUser?: boolean
  proof?: Proof
  watchRevocation?: string
  revocationTimeoutSeconds?: number
  revocationIntervalSeconds?: number
  runtime?: Pick<SuiteRuntime, 'apiVersion' | 'modelsApiVersion' | 'chatTokenParameter'>
}

/** The verifier flags for a run. Runs that call models acknowledge that the calls are billed. */
export function verifierArgs(run: VerifierRun): string[] {
  const args: string[] = []
  for (const id of run.userGrants ?? []) args.push('--user-entitlement', id)
  for (const id of run.applicationGrants ?? []) args.push('--application-entitlement', id)
  for (const id of run.foreignGrants ?? []) args.push('--foreign-user-entitlement', id)
  if ((run.userGrants?.length ?? 0) + (run.applicationGrants?.length ?? 0) > 0) args.push('--send-model-requests')
  if (run.userTokenSource) args.push('--user-token-source', run.userTokenSource)
  if (run.applicationTokenSource) args.push('--application-token-source', run.applicationTokenSource)
  if (run.checkUngrantedUser) args.push('--check-ungranted-user')
  if (run.proof === 'shared-budget') args.push('--prove-shared-budget')
  if (run.proof === 'token-limit') args.push('--prove-token-limit')
  if (run.watchRevocation) args.push('--watch-revocation', run.watchRevocation)
  if (run.revocationTimeoutSeconds !== undefined) args.push('--revocation-timeout', String(run.revocationTimeoutSeconds))
  if (run.revocationIntervalSeconds !== undefined) args.push('--revocation-interval', String(run.revocationIntervalSeconds))
  if (run.runtime?.apiVersion) args.push('--api-version', run.runtime.apiVersion)
  if (run.runtime?.modelsApiVersion) args.push('--models-api-version', run.runtime.modelsApiVersion)
  if (run.runtime?.chatTokenParameter) args.push('--chat-token-parameter', run.runtime.chatTokenParameter)
  return args
}

export interface TokenOwner {
  upn: string
  objectId?: string
}

/** True if a token names the person: by object ID when the manifest has one, else by sign-in name. */
export function tokenIsFor(token: string, owner: TokenOwner): boolean {
  const claims = tokenClaims(token)
  if (!claims) return false
  if (owner.objectId !== undefined && typeof claims.oid === 'string') return claims.oid.toLowerCase() === owner.objectId.toLowerCase()
  return ['preferred_username', 'upn', 'email', 'unique_name']
    .map((claim) => claims[claim])
    .some((value) => typeof value === 'string' && value.toLowerCase() === owner.upn.toLowerCase())
}

export const userRuntimeTokenVariable = 'MOSAIC_SMOKE_USER_RUNTIME_TOKEN'
export const ungrantedRuntimeTokenVariable = 'MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN'

type Environment = Record<string, string | undefined>

const present = (env: Environment, name: string) => {
  const value = env[name]?.trim()
  return value ? value : undefined
}

export interface UserTokenNeeds {
  env: Environment
  interactive: boolean
  /** Whether any of the run's user grants accepts Entra tokens: the verifier then needs the holder's model token. */
  entra: boolean
  holder: TokenOwner & { personaKey: string }
  /** Someone with no grant for these models, who signs in by device code for --check-ungranted-user. */
  stranger?: string
  wantUngranted: boolean
}

export type UserTokenPlan =
  | { source: UserTokenSource; checkUngrantedUser: boolean; notes: string[] }
  | { skip: string }

/**
 * Where the verifier gets the grant holder's model token, and whether it can check an ungranted user's too.
 * A token in the environment is used only if it is the holder's; otherwise an interactive run signs the holder
 * in with a device code. Grants that accept only keys need no model token at all.
 */
export function userTokenPlan(needs: UserTokenNeeds): UserTokenPlan {
  const { env, holder } = needs
  const held = present(env, userRuntimeTokenVariable)
  const ungrantedToken = present(env, ungrantedRuntimeTokenVariable)
  const envUngranted = ungrantedToken !== undefined && !tokenIsFor(ungrantedToken, holder)
  const notes: string[] = []
  const withEnvironment = () => {
    if (needs.wantUngranted && !envUngranted) {
      notes.push(`The ungranted-user check needs ${ungrantedRuntimeTokenVariable} set to someone else's model token, so this run leaves it out.`)
    }
    return { source: 'env' as const, checkUngrantedUser: needs.wantUngranted && envUngranted, notes }
  }
  if (held !== undefined && tokenIsFor(held, holder)) return withEnvironment()
  if (held !== undefined) notes.push(`${userRuntimeTokenVariable} isn't ${holder.personaKey}'s token, so this run doesn't use it.`)
  if (needs.entra && needs.interactive) {
    if (needs.wantUngranted && needs.stranger === undefined) {
      notes.push('The ungranted-user check signs in roles.outsider or roles.noRole, and the manifest names neither, so this run leaves it out.')
    }
    return { source: 'device-code', checkUngrantedUser: needs.wantUngranted && needs.stranger !== undefined, notes }
  }
  if (!needs.entra) return withEnvironment()
  return {
    skip:
      `These grants accept Microsoft Entra tokens, so the verifier needs ${holder.personaKey}'s model token. ` +
      `Set MOSAIC_E2E_INTERACTIVE=1 to sign them in with a device code in their browser, or set ${userRuntimeTokenVariable} to their token.`,
  }
}

/** How the verifier gets the workload's model token: its secret, a token in the environment, or neither. */
export function applicationTokenSource(env: Environment): ApplicationTokenSource | undefined {
  if (present(env, 'MOSAIC_SMOKE_APPLICATION_CLIENT_ID') && present(env, 'MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET')) {
    return 'client-credentials'
  }
  return present(env, 'MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN') ? 'env' : undefined
}

/**
 * The variables a run passes on to the verifier: the forwarded ones, less a user model token that isn't the
 * holder's (the verifier would refuse it) and an "ungranted" token that is (it would prove nothing).
 */
export function runtimeForwarded(env: Environment, holder: TokenOwner): Record<string, string> {
  return Object.fromEntries(
    Object.entries(forwardedEnvironment(env)).filter(([name, value]) => {
      if (name === userRuntimeTokenVariable) return tokenIsFor(value, holder)
      if (name === ungrantedRuntimeTokenVariable) return !tokenIsFor(value, holder)
      return true
    }),
  )
}

export interface VerifierSummary {
  passes: string[]
  skips: string[]
  waits: string[]
  failure?: string
  stopped: boolean
  /** From "Live checks passed for N grant(s)". */
  checkedGrants?: number
  /** From "Isolation checks passed for N grant(s) held by someone else". */
  isolatedGrants?: number
}

export function verifierSummary(lines: readonly string[]): VerifierSummary {
  const summary: VerifierSummary = { passes: [], skips: [], waits: [], stopped: false }
  for (const raw of lines) {
    const line = raw.trim()
    if (line.startsWith('PASS: ')) summary.passes.push(line)
    else if (line.startsWith('SKIP: ')) summary.skips.push(line)
    else if (line.startsWith('WAIT: ')) summary.waits.push(line)
    else if (line.startsWith('FAIL: ')) summary.failure ??= line.slice('FAIL: '.length)
    else if (line.startsWith('STOPPED: ')) summary.stopped = true
    const checked = /^Live checks passed for (\d+) grant\(s\)/.exec(line)
    if (checked) summary.checkedGrants = Number(checked[1])
    const isolated = /^Isolation checks passed for (\d+) grant\(s\)/.exec(line)
    if (isolated) summary.isolatedGrants = Number(isolated[1])
  }
  return summary
}

export interface VerifierOutcome {
  exitCode: number | null
  timedOut: boolean
  lines: string[]
}

/** Why a verifier run didn't pass, or an empty list if it did. */
export function verifierProblems(outcome: VerifierOutcome): string[] {
  const summary = verifierSummary(outcome.lines)
  const problems: string[] = []
  if (outcome.timedOut) problems.push('The verifier ran out of time and was stopped.')
  if (summary.failure !== undefined) problems.push(`The verifier failed: ${summary.failure}`)
  if (summary.stopped) problems.push('The verifier was stopped before its checks finished.')
  if (outcome.exitCode !== 0 && problems.length === 0) {
    problems.push(outcome.exitCode === null ? 'The verifier did not run to completion.' : `The verifier exited with code ${outcome.exitCode}.`)
  }
  if (outcome.exitCode === 0 && summary.checkedGrants === undefined && summary.isolatedGrants === undefined) {
    problems.push('The verifier exited without reporting that its checks passed.')
  }
  return problems
}

const escapeRegExp = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

/**
 * True if the verifier reported this outcome for a grant. The verifier numbers each kind of grant from 1, in
 * the order they were passed, and adds the deployment name to the label when it can.
 */
export function grantPassed(summary: VerifierSummary, kind: 'user' | 'application', index: number, outcome: string): boolean {
  const label = `${kind === 'user' ? 'User' : 'Application'} grant ${index}`
  const pattern = new RegExp(`^PASS: ${label}(?: \\([^)]*\\))? ${escapeRegExp(outcome)}`)
  return summary.passes.some((line) => pattern.test(line))
}

/**
 * The calls the verifier reported a grant's gateway rejecting, such as "an anonymous call", in its order, or
 * undefined if it reported no rejections for that grant. The names never contain a comma.
 */
export function rejectedChecks(summary: VerifierSummary, kind: 'user' | 'application', index: number): string[] | undefined {
  const label = `${kind === 'user' ? 'User' : 'Application'} grant ${index}`
  const pattern = new RegExp(`^PASS: ${label}(?: \\([^)]*\\))? rejected (.+)$`)
  for (const line of summary.passes) {
    const match = pattern.exec(line)
    if (match) return match[1].split(', ').map((name) => name.trim())
  }
  return undefined
}

/**
 * The checks the verifier skipped for a grant, such as "the ungranted user's token", each reported on its own
 * line as the check's name, a colon and the reason.
 */
export function skippedChecks(summary: VerifierSummary, kind: 'user' | 'application', index: number): string[] {
  const label = `${kind === 'user' ? 'User' : 'Application'} grant ${index}`
  const pattern = new RegExp(`^SKIP: ${label}(?: \\([^)]*\\))? (.+?): `)
  return summary.skips.flatMap((line) => {
    const match = pattern.exec(line)
    return match ? [match[1]] : []
  })
}

/** What the verifier reports for a grant, in its own words, after the grant's label. */
export const verifierOutcomes = {
  keyReached: 'reached the model with its key',
  tokenReached: 'reached the model with its Entra token',
  sharedBudget: 'primary key, Entra token and secondary key share one budget',
  tokenLimit: 'returned 429 with Retry-After after',
  keyRevoked: 'rejects its key after revocation',
  tokenRevoked: 'rejects its Entra token after revocation',
} as const

/** The verifier's line when the end user can't retrieve an application grant's key. */
export const applicationKeyWithheld = "PASS: the end user can't retrieve an application grant's key"

export interface GrantChecks {
  kind: 'user' | 'application'
  /** The grant's number among its kind, from 1, in the order the run passed them. */
  index: number
  /** The methods MOSAIC applied for the grant. */
  methods: { keysEnabled: boolean; entraEnabled: boolean }
  /** The run asked the verifier to check an ungranted user's token. */
  ungranted?: boolean
}

/**
 * What the verifier should have reported for a grant and didn't: a call by each applied method reaching the
 * model, and each call the gateway must refuse. A refusal that needs a credential the run didn't have may be
 * skipped instead, since the verifier then says why. An empty list means every check was reported.
 */
export function unreportedChecks(summary: VerifierSummary, grant: GrantChecks): string[] {
  const { kind, index, methods } = grant
  const missing: string[] = []
  if (methods.keysEnabled && !grantPassed(summary, kind, index, verifierOutcomes.keyReached)) missing.push('a call with its key reaching the model')
  if (methods.entraEnabled && !grantPassed(summary, kind, index, verifierOutcomes.tokenReached)) {
    missing.push('a call with its Entra token reaching the model')
  }
  const rejected = rejectedChecks(summary, kind, index)
  if (rejected === undefined) return [...missing, 'the calls the gateway refused']
  const skipped = skippedChecks(summary, kind, index)
  const refused = (name: string) => {
    if (!rejected.includes(name)) missing.push(`${name}, refused`)
  }
  const refusedOrSkipped = (name: string) => {
    if (!rejected.includes(name) && !skipped.includes(name)) missing.push(`${name}, refused or skipped with a reason`)
  }
  refused('an anonymous call')
  refused('an invalid key')
  refused('a MOSAIC control-plane token')
  if (methods.keysEnabled) {
    refused('an invalid token with a valid key')
    refusedOrSkipped(`the ${kind === 'user' ? 'application' : 'user'}'s token with this grant's key`)
  }
  if (grant.ungranted) refusedOrSkipped("the ungranted user's token")
  if (!methods.entraEnabled) refusedOrSkipped('a token while Entra tokens are off')
  return missing
}

/** The grant label in the verifier's revocation prompt, which asks for the grant to be revoked now. */
export function revocationPromptLabel(line: string): string | undefined {
  return /^WAIT: revoke (.+?) in MOSAIC's console, which disables it\b/.exec(line.trim())?.[1]
}

/** Hides a device code in kept output. The person who needs it is told separately if it wasn't entered for them. */
export function maskDeviceCode(line: string): string {
  return line.replace(/(\band enter the code )[A-Za-z0-9-]{4,32}/, '$1[device code]')
}

export interface RunningVerifier {
  readonly plan: VerifyPlan
  readonly people: VerifyPersonas
  /** Redacted output so far. */
  readonly lines: readonly string[]
  /** The first line, already seen or still to come, that matches. Rejects if the verifier ends first or the wait times out. */
  waitForLine(match: (line: string) => boolean, timeoutMs: number, what: string): Promise<string>
  readonly finished: Promise<VerifierOutcome>
  /** Stops the verifier, if it's still running, and ends any sign-in it asked for. */
  stop(): void
}

export interface StartVerifierOptions {
  personas: PersonaPool
  targets: Targets
  args: readonly string[]
  choice?: PersonaChoice
  /** Variables to pass on; by default the forwarded ones in this process's environment. */
  forwarded?: Record<string, string>
  /** Adds the verifier's own time limit to the test's timeout. */
  testInfo?: TestInfo
  log?: (line: string) => void
}

interface Waiter {
  match: (line: string) => boolean
  resolve: (line: string) => void
  reject: (error: Error) => void
}

const errorText = (error: unknown) => redact(error instanceof Error ? error.message : String(error))

/**
 * Starts the verifier. The control tokens come from the personas' browsers and are checked before the run;
 * device-code prompts are entered in the persona's browser, and the next line of output ends that sign-in.
 */
export async function startVerifier(options: StartVerifierOptions): Promise<RunningVerifier> {
  const { personas, targets } = options
  const log = options.log ?? ((line: string) => process.stdout.write(`[verify] ${line}\n`))
  const plan = planVerification(targets, options.args)
  const people = verifyPersonas(targets, plan, options.choice)
  const limitMs = verifierTimeoutMs(plan)
  // The test's own timeout covers its other steps, such as applying a grant before the run and revoking it after.
  if (options.testInfo && options.testInfo.timeout !== 0) options.testInfo.setTimeout(options.testInfo.timeout + limitMs)

  const controlToken = async (personaKey: string) => {
    const token = await personaApiToken(personas, targets, personaKey, controlTokenSeconds(plan))
    const { upn, objectId } = persona(targets, personaKey)
    const problem = controlTokenProblem(token, { personaKey, upn, objectId, tenantId: targets.tenantId }, Date.now() / 1_000, plan)
    if (problem) throw new Error(problem)
    return token
  }
  const user = await controlToken(people.user)
  const admin = people.admin === undefined ? undefined : await controlToken(people.admin)
  const { env, secrets } = verifierLaunch(process.env, options.forwarded ?? forwardedEnvironment(process.env), { user, admin })

  const lines: string[] = []
  const waiters = new Set<Waiter>()
  const python = process.env.MOSAIC_E2E_PYTHON || 'python'
  let settled = false
  let timedOut = false

  // Every line is redacted before it is printed or kept, whether the verifier or the suite wrote it.
  const record = (text: string) => {
    const line = redact(text)
    lines.push(line)
    log(line)
    for (const waiter of [...waiters]) {
      if (waiter.match(line)) {
        waiters.delete(waiter)
        waiter.resolve(line)
      }
    }
  }
  const startSignIn = (prompt: SignInPrompt): DeviceSignIn | undefined => {
    const yourself = redact(`Enter the code ${prompt.code} yourself at ${prompt.uri}, signed in as ${prompt.who}.`)
    const target = deviceCodePersona(prompt, people)
    if ('refused' in target) {
      // The code goes only to the terminal, for the person at the keyboard; the kept log never has it.
      if (target.refused === 'not-device-login') {
        record('That address is not a Microsoft sign-in page, so the suite did not open it.')
      } else {
        record(`The suite has no persona to sign in as ${prompt.who}.`)
        log(yourself)
      }
      return undefined
    }
    const { personaKey } = target
    const { upn } = persona(targets, personaKey)
    const state: { done: boolean; page?: Page } = { done: false }
    void (async () => {
      const context = await personas.context(personaKey)
      if (state.done) return
      const page = await context.newPage()
      state.page = page
      if (state.done) {
        await page.close().catch(() => undefined)
        return
      }
      await typeDeviceCode(page, personaKey, upn, prompt, () => state.done, record)
    })().catch((error: unknown) => {
      if (state.done) return
      record(`The suite could not enter the code in ${personaKey}'s browser: ${errorText(error)}.`)
      log(yourself)
    })
    return {
      startedAt: Date.now(),
      finish() {
        if (state.done) return
        state.done = true
        void state.page?.close().catch(() => undefined)
      },
    }
  }
  const signIns = followDeviceSignIns((line) => parseSignInPrompt(line), startSignIn)

  const child = spawn(python, [verifierScript, ...plan.argv], {
    cwd: repoRoot,
    env,
    shell: false,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  })
  const finished = new Promise<VerifierOutcome>((resolve) => {
    const timer = setTimeout(() => {
      timedOut = true
      record(`The verifier ran for ${Math.round(limitMs / 60_000)} minutes, so the suite stopped it.`)
      child.kill()
    }, limitMs)
    const finish = (exitCode: number | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signIns.end()
      const tail = lines.slice(-5).join(' | ')
      for (const waiter of waiters) waiter.reject(new Error(`The verifier ended first. Its last lines: ${tail}`))
      waiters.clear()
      resolve({ exitCode, timedOut, lines: [...lines] })
    }
    const onLine = (raw: string) => signIns.line(raw, () => record(maskDeviceCode(publicLine(raw, secrets))))
    createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', onLine)
    createInterface({ input: child.stderr, crlfDelay: Infinity }).on('line', onLine)
    child.on('error', (error) => {
      record(`Could not run the verifier with "${python}": ${errorText(error)}. Set MOSAIC_E2E_PYTHON to a Python that has httpx.`)
      if (child.pid === undefined) finish(null)
    })
    child.on('close', (code) => finish(code))
  })

  return {
    plan,
    people,
    lines,
    finished,
    waitForLine(match, timeoutMs, what) {
      const seen = lines.find(match)
      if (seen !== undefined) return Promise.resolve(seen)
      if (settled) return Promise.reject(new Error(`The verifier ended before it would ${what}.`))
      return new Promise((resolve, reject) => {
        const waiter: Waiter = {
          match,
          resolve: (line) => {
            clearTimeout(timer)
            resolve(line)
          },
          reject: (error) => {
            clearTimeout(timer)
            reject(new Error(`The verifier ended before it would ${what}. ${error.message}`))
          },
        }
        const timer = setTimeout(() => {
          waiters.delete(waiter)
          reject(new Error(`Waited ${Math.round(timeoutMs / 1_000)} seconds for the verifier to ${what}.`))
        }, timeoutMs)
        waiters.add(waiter)
      })
    },
    stop() {
      signIns.end()
      if (!settled) child.kill()
    },
  }
}

/** Attaches the verifier's output, which is already redacted, and redacts it once more on the way. */
export async function attachVerifierLog(testInfo: TestInfo, name: string, lines: readonly string[]): Promise<void> {
  await testInfo.attach(`${name}.log`, { body: redact(lines.join('\n')), contentType: 'text/plain' })
}

/** Runs the verifier to the end, attaches its log, and summarizes its output. */
export async function runVerifier(
  options: StartVerifierOptions & { name: string },
): Promise<{ outcome: VerifierOutcome; summary: VerifierSummary; people: VerifyPersonas }> {
  const running = await startVerifier(options)
  try {
    const outcome = await running.finished
    return { outcome, summary: verifierSummary(outcome.lines), people: running.people }
  } finally {
    running.stop()
    if (options.testInfo) await attachVerifierLog(options.testInfo, options.name, running.lines)
  }
}
