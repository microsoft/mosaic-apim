import { chromium, type BrowserContext, type Locator, type Page } from '@playwright/test'
import { type ChildProcess, spawn } from 'node:child_process'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { join } from 'node:path'
import { createInterface } from 'node:readline'
import { type AppName, type Targets, flags, loadTargets, persona, resolveTargetRef } from '../src/config.ts'
import { type LocatorOptions, describeLocator, locate } from '../src/locators.ts'
import { ensureDir, artifactsDir, liveSessionFile, stateDir } from '../src/paths.ts'
import {
  appDestination,
  browserStillExiting,
  closePersona,
  ensureSignedIn,
  isAppUrl,
  isLoginHost,
  launchPersona,
  signInErrorCode,
} from '../src/personas.ts'
import { maskedSelectors, pageSecrets, redact, redactUrl, truncate } from '../src/redact.ts'
import {
  type SignInPrompt,
  type VerifyPersonas,
  type VerifyPlan,
  bearerToken,
  checkForwarded,
  controlTokenProblem,
  isDeviceLoginUrl,
  parseSignInPrompt,
  planVerification,
  publicLine,
  repoRoot,
  verifierLaunch,
  verifierScript,
  verifierTimeoutMs,
  verifyPersonas,
} from '../src/verify.ts'

/**
 * Local control daemon for human-in-the-loop UI journeys. It owns one persistent browser profile per
 * persona and exposes a narrow, token-protected RPC surface on 127.0.0.1 for tools/drive.ts.
 * There is deliberately no arbitrary script evaluation and no way to read tokens or revealed keys.
 * Navigation stays on the MOSAIC origins. Snapshots and text reads run only on MOSAIC pages, because
 * accessibility snapshots include input values, such as a password typed on a Microsoft sign-in page.
 * The one exception to "no tokens" is "verify": it hands the personas' MOSAIC API tokens straight to
 * scripts/verify_model_access.py in its environment, and never returns or prints them.
 */

interface LogEntry {
  at: string
  kind: 'console' | 'pageerror' | 'api' | 'requestfailed' | 'dialog'
  message: string
}

interface PersonaSession {
  context: BrowserContext
  current: Page
  logs: LogEntry[]
}

type Args = Record<string, unknown>
type Handler = (personaKey: string | undefined, args: Args, signal: AbortSignal) => Promise<unknown>

interface Verification {
  child?: ChildProcess
}

class RpcError extends Error {}

const maxLogEntries = 80
const maxVerifyLines = 500
const signInTimeoutMs = 600_000
const targets: Targets = loadTargets()
const sessions = new Map<string, PersonaSession>()
const signingIn = new Set<string>()
const token = randomBytes(32).toString('hex')
const allowedOrigins = new Set([targets.origins.web, targets.origins.portal])
let dialogMode: 'accept' | 'dismiss' = 'dismiss'
let activeVerification: Verification | undefined

function log(session: PersonaSession, kind: LogEntry['kind'], message: string) {
  session.logs.push({ at: new Date().toISOString(), kind, message: truncate(redact(message), 800) })
  if (session.logs.length > maxLogEntries) session.logs.splice(0, session.logs.length - maxLogEntries)
}

function watchPage(session: PersonaSession, page: Page) {
  page.on('console', (message) => {
    if (message.type() === 'error' || message.type() === 'warning') log(session, 'console', `${message.type()}: ${message.text()}`)
  })
  page.on('pageerror', (error) => log(session, 'pageerror', error.message))
  page.on('dialog', (dialog) => {
    log(session, 'dialog', `${dialog.type()} "${dialog.message()}" -> ${dialogMode}`)
    void (dialogMode === 'accept' ? dialog.accept() : dialog.dismiss())
  })
}

async function openSession(personaKey: string): Promise<PersonaSession> {
  persona(targets, personaKey)
  const existing = sessions.get(personaKey)
  if (existing) return existing
  const context = await launchPersona(chromium, targets, personaKey, { headless: flags.headless() })
  const current = context.pages()[0] ?? (await context.newPage())
  const session: PersonaSession = { context, current, logs: [] }
  sessions.set(personaKey, session)
  for (const page of context.pages()) watchPage(session, page)
  context.on('page', (page) => {
    watchPage(session, page)
    session.current = page
  })
  context.on('response', async (response) => {
    const url = response.url()
    if (!url.startsWith(targets.origins.api) || response.status() < 400) return
    const body = await response.text().catch(() => '')
    const path = new URL(url).pathname
    log(session, 'api', `${response.request().method()} ${path} -> ${response.status()} ${truncate(body, 600)}`)
  })
  context.on('requestfailed', (request) => {
    if (request.url().startsWith(targets.origins.api)) {
      log(session, 'requestfailed', `${request.method()} ${new URL(request.url()).pathname}: ${request.failure()?.errorText ?? 'failed'}`)
    }
  })
  context.on('close', () => sessions.delete(personaKey))
  return session
}

function existingSession(personaKey: string): PersonaSession {
  const session = sessions.get(personaKey)
  if (!session) throw new RpcError(`No browser is open for ${personaKey}. Run "open" first.`)
  if (session.current.isClosed()) {
    const open = session.context.pages().filter((page) => !page.isClosed())
    if (open.length === 0) throw new RpcError(`${personaKey} has no open tabs. Run "open" again.`)
    session.current = open[open.length - 1]
  }
  return session
}

function str(args: Args, key: string, required = true): string | undefined {
  const value = args[key]
  if (value === undefined || value === null || value === '') {
    if (required) throw new RpcError(`Missing "${key}"`)
    return undefined
  }
  if (typeof value !== 'string') throw new RpcError(`"${key}" must be a string`)
  return value
}

function num(args: Args, key: string, fallback: number, max: number): number {
  const value = args[key]
  if (value === undefined) return fallback
  if (typeof value !== 'number' || !Number.isInteger(value) || value < 0) {
    throw new RpcError(`"${key}" must be a whole number of 0 or more`)
  }
  return Math.min(value, max)
}

function locatorOptions(args: Args): LocatorOptions {
  let nth: number | undefined
  if (args.nth !== undefined) {
    if (typeof args.nth !== 'number' || !Number.isInteger(args.nth) || args.nth < -1) {
      throw new RpcError('"nth" must be a whole number of 0 or more, or -1 for the last match')
    }
    nth = args.nth
  }
  return {
    exact: args.exact === true,
    nth,
    within: str(args, 'within', false),
    row: str(args, 'row', false),
    hasText: str(args, 'has', false),
  }
}

function target(page: Page, args: Args, key = 'target'): { locator: Locator; label: string } {
  const spec = str(args, key) as string
  const options = locatorOptions(args)
  return { locator: locate(page, spec, options), label: describeLocator(spec, options) }
}

async function pageState(page: Page) {
  return { url: redactUrl(page.url()), title: redact(await page.title().catch(() => '')) }
}

async function readablePage(personaKey: string, session: PersonaSession): Promise<Page> {
  const page = session.current
  if (signingIn.has(personaKey)) {
    throw new RpcError(
      `${personaKey} is signing in, so snapshots and text reads are paused until "signin" returns. Use "url" or "logs" to follow along.`,
    )
  }
  if (!isAppUrl(targets, page.url())) {
    const { url, title } = await pageState(page)
    const code = isLoginHost(page.url()) ? await signInErrorCode(page) : undefined
    throw new RpcError(
      `Snapshots and text reads only work on MOSAIC web and portal pages. ${personaKey} is on ${url} ("${title}")` +
        `${code ? ` showing ${code}` : ''}. Use "url", "shot" or "logs" instead.`,
    )
  }
  return page
}

function confirmStillReadable(personaKey: string, page: Page) {
  if (signingIn.has(personaKey) || !isAppUrl(targets, page.url())) {
    throw new RpcError(`${personaKey} left MOSAIC while the page was being read, so the result was discarded.`)
  }
}

function appUrl(app: string, path: string | undefined): string {
  if (app !== 'web' && app !== 'portal') throw new RpcError('app must be "web" or "portal"')
  const origin = targets.origins[app as AppName]
  const url = new URL(path ?? '/', origin)
  if (!allowedOrigins.has(url.origin)) throw new RpcError('Navigation is limited to the MOSAIC web and portal origins')
  return url.href
}

const timeout = (args: Args, fallback = 15_000) => num(args, 'timeout', fallback, 900_000)

function errorText(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return truncate(redact(message.split('\n')[0]), 300)
}

async function within<T>(promise: Promise<T>, ms: number, message: string): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const expired = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => reject(new RpcError(message)), ms)
  })
  try {
    return await Promise.race([promise, expired])
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Signs a persona in to MOSAIC in a new tab and takes the MOSAIC API token from the first API call the app
 * makes. A new tab starts with empty session storage, where MSAL keeps its cache, so the app signs in again
 * and the token has its full lifetime. The token goes only to the verifier.
 */
async function captureApiToken(personaKey: string, plan: VerifyPlan, signal: AbortSignal): Promise<string> {
  const { upn, objectId, expectedRole } = persona(targets, personaKey)
  if (signingIn.has(personaKey)) {
    throw new RpcError(`${personaKey} is already signing in. Wait for that to finish, then run "verify" again.`)
  }
  signingIn.add(personaKey)
  let session: PersonaSession | undefined
  let previous: Page | undefined
  let page: Page | undefined
  const closePage = () => void page?.close().catch(() => undefined)
  signal.addEventListener('abort', closePage, { once: true })
  try {
    session = await openSession(personaKey)
    previous = session.current
    page = await session.context.newPage()
    signal.throwIfAborted()
    const app: AppName = expectedRole === 'Admin' ? 'web' : 'portal'
    const apiCall = page.waitForRequest(
      async (request) =>
        request.url().startsWith(`${targets.origins.api}/api/`) &&
        bearerToken(await request.headerValue('authorization').catch(() => null)) !== undefined,
      { timeout: 0 },
    )
    apiCall.catch(() => undefined)
    process.stdout.write(`[verify] Signing ${personaKey} in to the ${app} app for a MOSAIC API token.\n`)
    await page.bringToFront()
    await ensureSignedIn(page, targets, personaKey, app, { interactive: true, timeoutMs: signInTimeoutMs })
    const request = await within(apiCall, 60_000, `${personaKey} signed in, but the ${app} app didn't call the MOSAIC API within a minute.`)
    const apiToken = bearerToken(await request.headerValue('authorization'))
    if (!apiToken) throw new RpcError(`${personaKey}'s browser sent no MOSAIC API token.`)
    const holder = { personaKey, upn, objectId, tenantId: targets.tenantId }
    const problem = controlTokenProblem(apiToken, holder, Date.now() / 1_000, plan)
    if (problem) throw new RpcError(problem)
    return apiToken
  } finally {
    signal.removeEventListener('abort', closePage)
    await page?.close().catch(() => undefined)
    if (session && previous) session.current = previous
    signingIn.delete(personaKey)
  }
}

interface DeviceSignInState {
  done: boolean
  session?: PersonaSession
  previous?: Page
  page?: Page
}

interface DeviceSignIn {
  startedAt: number
  finish(): void
}

// The verifier waits at least a second before its first poll, so output within this window of a prompt was
// written before it, on the other stream, and doesn't mean the sign-in is over.
const deviceSignInGraceMs = 750

/**
 * Enters a device code in the right persona's browser and picks the persona's account if Entra asks. The
 * person at the keyboard confirms the sign-in and completes MFA, as with every other sign-in.
 */
async function enterDeviceCode(
  personaKey: string,
  prompt: SignInPrompt,
  state: DeviceSignInState,
  emit: (line: string) => void,
): Promise<void> {
  const { upn } = persona(targets, personaKey)
  const session = await openSession(personaKey)
  if (state.done) return
  state.session = session
  state.previous = session.current
  const page = await session.context.newPage()
  state.page = page
  if (state.done) {
    await page.close().catch(() => undefined)
    return
  }
  await page.bringToFront()
  await page.goto(prompt.uri)
  const box = page.locator('input[name="otc"]').first()
  await box.waitFor({ state: 'visible', timeout: 30_000 })
  await box.fill(prompt.code)
  await page.locator('input[type="submit"]').first().click()
  emit(`Entered the code in ${personaKey}'s browser. If it asks, confirm the sign-in there and complete MFA.`)
  const tile = page.locator(`[data-test-id="${upn.replace(/["\\]/g, '')}" i]`).first()
  // The device page shows a rejected or expired code here, with no AADSTS code.
  const alert = page.locator('#error[role="alert"]').first()
  let reported: string | undefined
  let alerted: string | undefined
  while (!state.done && !page.isClosed()) {
    if (isLoginHost(page.url())) {
      const code = await signInErrorCode(page)
      if (code && code !== reported) {
        reported = code
        emit(`${personaKey}'s sign-in page shows ${code}.`)
      }
      const message = (await alert.isVisible().catch(() => false))
        ? (await alert.innerText({ timeout: 1_000 }).catch(() => '')).trim()
        : ''
      if (message && message !== alerted) {
        alerted = message
        emit(`${personaKey}'s sign-in page says: ${truncate(redact(message), 200)}`)
      }
      if (await tile.isVisible().catch(() => false)) await tile.click().catch(() => undefined)
    }
    await page.waitForTimeout(750).catch(() => undefined)
  }
}

function startDeviceSignIn(
  prompt: SignInPrompt,
  people: VerifyPersonas,
  emit: (line: string) => void,
): DeviceSignIn | undefined {
  const personaKey = prompt.subject === 'user' ? people.user : prompt.subject === 'stranger' ? people.stranger : undefined
  const yourself = `Enter the code yourself at that address, signed in as ${prompt.who}.`
  if (!isDeviceLoginUrl(prompt.uri)) {
    emit('That address is not a Microsoft sign-in page, so the driver did not open it. Check it before you use it.')
    return undefined
  }
  if (personaKey === undefined) {
    emit(`The driver has no persona to sign in as ${prompt.who}. ${yourself}`)
    return undefined
  }
  if (signingIn.has(personaKey)) {
    emit(`${personaKey} is already signing in, so the driver did not open the page. ${yourself}`)
    return undefined
  }
  signingIn.add(personaKey)
  const state: DeviceSignInState = { done: false }
  void enterDeviceCode(personaKey, prompt, state, emit).catch((error: unknown) => {
    if (!state.done) emit(`The driver could not enter the code in ${personaKey}'s browser: ${errorText(error)}. ${yourself}`)
  })
  return {
    startedAt: Date.now(),
    finish() {
      if (state.done) return
      state.done = true
      void state.page?.close().catch(() => undefined)
      if (state.session && state.previous) state.session.current = state.previous
      signingIn.delete(personaKey)
    },
  }
}

interface VerifierOutcome {
  exitCode: number | null
  timedOut: boolean
  lines: string[]
}

/**
 * Runs the verifier and collects its redacted output. Device-code prompts are entered in the persona's
 * browser; the next line of output means that sign-in ended, so its tab closes.
 */
function runVerifier(
  plan: VerifyPlan,
  people: VerifyPersonas,
  env: Record<string, string>,
  secrets: string[],
  signal: AbortSignal,
  verification: Verification,
): Promise<VerifierOutcome> {
  return new Promise((resolve) => {
    const lines: string[] = []
    const limitMs = verifierTimeoutMs(plan)
    const python = process.env.MOSAIC_E2E_PYTHON || 'python'
    let dropped = 0
    let timedOut = false
    let settled = false
    let signIn: DeviceSignIn | undefined
    let timer: ReturnType<typeof setTimeout> | undefined

    const emit = (line: string) => {
      lines.push(line)
      if (lines.length > maxVerifyLines) dropped += lines.splice(0, lines.length - maxVerifyLines).length
      process.stdout.write(`[verify] ${line}\n`)
    }
    const endSignIn = () => {
      signIn?.finish()
      signIn = undefined
    }
    const child = spawn(python, [verifierScript, ...plan.argv], {
      cwd: repoRoot,
      env,
      shell: false,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
    verification.child = child
    const onAbort = () => {
      emit('drive.ts disconnected, so the driver stopped the verifier.')
      child.kill()
    }
    const finish = (exitCode: number | null) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      signal.removeEventListener('abort', onAbort)
      endSignIn()
      if (dropped > 0) lines.unshift(`… ${dropped} earlier lines are in the live driver's terminal.`)
      resolve({ exitCode, timedOut, lines })
    }
    const onLine = (raw: string) => {
      if (signIn && Date.now() - signIn.startedAt >= deviceSignInGraceMs) endSignIn()
      emit(publicLine(raw, secrets))
      const prompt = parseSignInPrompt(raw)
      if (prompt) {
        endSignIn()
        signIn = startDeviceSignIn(prompt, people, emit)
      }
    }
    createInterface({ input: child.stdout, crlfDelay: Infinity }).on('line', onLine)
    createInterface({ input: child.stderr, crlfDelay: Infinity }).on('line', onLine)
    signal.addEventListener('abort', onAbort, { once: true })
    timer = setTimeout(() => {
      timedOut = true
      emit(`The verifier ran for ${Math.round(limitMs / 60_000)} minutes, so the driver stopped it.`)
      child.kill()
    }, limitMs)
    child.on('error', (error) => {
      emit(`Could not run the verifier with "${python}": ${errorText(error)}. Set MOSAIC_E2E_PYTHON to a Python that has httpx.`)
      if (child.pid === undefined) finish(null)
    })
    child.on('close', (code) => finish(code))
  })
}

const handlers: Record<string, Handler> = {
  async status() {
    const personas = await Promise.all(
      [...sessions.entries()].map(async ([key, session]) => ({
        persona: key,
        tabs: session.context.pages().length,
        current: await pageState(session.current),
      })),
    )
    return { environment: targets.environment, dialogMode, personas }
  },

  async open(personaKey, args) {
    const session = await openSession(personaKey as string)
    if (session.current.isClosed()) session.current = await session.context.newPage()
    await session.current.goto(appUrl(str(args, 'app') as string, str(args, 'path', false)))
    await session.current.bringToFront()
    return pageState(session.current)
  },

  async signin(personaKey, args) {
    const key = personaKey as string
    if (signingIn.has(key)) {
      throw new RpcError(`${key} is already signing in. Finish in the browser window or wait for that attempt to time out.`)
    }
    signingIn.add(key)
    try {
      const app = str(args, 'app') as AppName
      appUrl(app, '/')
      const requested = str(args, 'path', false)
      const destination = requested === undefined ? undefined : appDestination(targets, app, requested)
      const session = await openSession(key)
      if (session.current.isClosed()) session.current = await session.context.newPage()
      await session.current.bringToFront()
      await ensureSignedIn(session.current, targets, key, app, {
        interactive: true,
        path: destination && `${destination.pathname}${destination.search}`,
        timeoutMs: timeout(args, 600_000),
      })
      return pageState(session.current)
    } finally {
      signingIn.delete(key)
    }
  },

  async reload(personaKey) {
    const session = existingSession(personaKey as string)
    await session.current.reload()
    return pageState(session.current)
  },

  async back(personaKey) {
    const session = existingSession(personaKey as string)
    await session.current.goBack()
    return pageState(session.current)
  },

  async url(personaKey) {
    return pageState(existingSession(personaKey as string).current)
  },

  async snapshot(personaKey, args) {
    const key = personaKey as string
    const page = await readablePage(key, existingSession(key))
    const root = args.target ? target(page, args).locator : page.locator('body')
    const snapshot = await root.ariaSnapshot({ timeout: timeout(args) })
    const secrets = await pageSecrets(page)
    confirmStillReadable(key, page)
    return truncate(redact(snapshot, secrets), num(args, 'max', 12_000, 80_000))
  },

  async click(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    await locator.click({ timeout: timeout(args) })
    return { clicked: label, ...(await pageState(session.current)) }
  },

  async hover(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    await locator.hover({ timeout: timeout(args) })
    return { hovered: label }
  },

  async fill(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    const value = resolveTargetRef(targets, str(args, 'value') ?? '')
    await locator.fill(value, { timeout: timeout(args) })
    return { filled: label, characters: value.length }
  },

  async clear(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    await locator.clear({ timeout: timeout(args) })
    return { cleared: label }
  },

  async choose(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    const option = resolveTargetRef(targets, str(args, 'option') as string)
    await locator.click({ timeout: timeout(args) })
    await session.current.getByRole('option', { name: option, exact: args.exact === true }).first().click({ timeout: timeout(args) })
    return { chose: option, in: label }
  },

  async select(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    const value = resolveTargetRef(targets, str(args, 'value') as string)
    const selected = await locator.selectOption(value, { timeout: timeout(args) })
    return { selected, in: label }
  },

  async check(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    await locator.setChecked(args.checked !== false, { timeout: timeout(args) })
    return { [args.checked === false ? 'unchecked' : 'checked']: label }
  },

  async press(personaKey, args) {
    const session = existingSession(personaKey as string)
    const key = str(args, 'key') as string
    if (args.target) {
      const { locator, label } = target(session.current, args)
      await locator.press(key, { timeout: timeout(args) })
      return { pressed: key, on: label }
    }
    await session.current.keyboard.press(key)
    return { pressed: key }
  },

  async wait(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    const state = (str(args, 'state', false) ?? 'visible') as 'visible' | 'hidden' | 'attached' | 'detached'
    if (!['visible', 'hidden', 'attached', 'detached'].includes(state)) throw new RpcError('Invalid wait state')
    const started = Date.now()
    await locator.first().waitFor({ state, timeout: timeout(args, 30_000) })
    return { waited: label, state, ms: Date.now() - started }
  },

  async waitUrl(personaKey, args) {
    const session = existingSession(personaKey as string)
    const fragment = str(args, 'contains') as string
    await session.current.waitForURL((url) => url.href.includes(fragment), { timeout: timeout(args, 30_000) })
    return pageState(session.current)
  },

  async text(personaKey, args) {
    const key = personaKey as string
    const page = await readablePage(key, existingSession(key))
    const { locator, label } = target(page, args)
    const secret = await locator
      .first()
      .evaluate((element, selector) => element.closest(selector) !== null, maskedSelectors.join(', '), { timeout: timeout(args) })
    if (secret) return { target: label, text: '[redacted-secret]' }
    const text = await locator.first().innerText({ timeout: timeout(args) })
    const secrets = await pageSecrets(page)
    confirmStillReadable(key, page)
    return { target: label, text: redact(text, secrets) }
  },

  async count(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session.current, args)
    return { target: label, count: await locator.count() }
  },

  async shot(personaKey, args) {
    const session = existingSession(personaKey as string)
    const name = (str(args, 'name', false) ?? 'screen').toLowerCase().replace(/[^a-z0-9-]+/g, '-').slice(0, 60)
    const stamp = new Date().toISOString().replace(/[:.]/g, '-')
    const file = join(ensureDir(join(artifactsDir(), 'screens')), `${stamp}-${personaKey}-${name}.png`)
    await session.current.screenshot({
      path: file,
      fullPage: args.full === true,
      mask: maskedSelectors.map((selector) => session.current.locator(selector)),
      animations: 'disabled',
    })
    return { saved: file }
  },

  async pages(personaKey) {
    const session = existingSession(personaKey as string)
    return Promise.all(
      session.context.pages().map(async (page, index) => ({ index, current: page === session.current, ...(await pageState(page)) })),
    )
  },

  async tab(personaKey, args) {
    const session = existingSession(personaKey as string)
    const index = num(args, 'index', 0, 50)
    const page = session.context.pages()[index]
    if (!page) throw new RpcError(`No tab ${index}`)
    session.current = page
    await page.bringToFront()
    return pageState(page)
  },

  async logs(personaKey, args) {
    const session = existingSession(personaKey as string)
    const entries = [...session.logs]
    if (args.clear === true) session.logs.length = 0
    return entries
  },

  async dialogs(_personaKey, args) {
    const mode = str(args, 'mode') as string
    if (mode !== 'accept' && mode !== 'dismiss') throw new RpcError('mode must be accept or dismiss')
    dialogMode = mode
    return { dialogMode }
  },

  async verify(_personaKey, args, signal) {
    if (activeVerification) throw new RpcError('A verification is already running. Wait for it to finish, or stop its drive.ts.')
    const verifierArgs = args.verifierArgs
    if (!Array.isArray(verifierArgs) || !verifierArgs.every((item): item is string => typeof item === 'string')) {
      throw new RpcError('"verifierArgs" must be an array of strings')
    }
    const forwarded = checkForwarded(args.env)
    const plan = planVerification(targets, verifierArgs)
    const people = verifyPersonas(targets, plan, {
      admin: str(args, 'admin', false),
      user: str(args, 'user', false),
      stranger: str(args, 'stranger', false),
    })
    const busy = [people.user, people.admin, people.stranger].find((key) => key !== undefined && signingIn.has(key))
    if (busy) throw new RpcError(`${busy} is signing in. Wait for that to finish, then run "verify" again.`)
    const verification: Verification = {}
    activeVerification = verification
    try {
      const user = await captureApiToken(people.user, plan, signal)
      const admin = people.admin === undefined ? undefined : await captureApiToken(people.admin, plan, signal)
      signal.throwIfAborted()
      const { env, secrets } = verifierLaunch(process.env, forwarded, { user, admin })
      const outcome = await runVerifier(plan, people, env, secrets, signal, verification)
      return { ...outcome, personas: people }
    } catch (error) {
      if (signal.aborted) process.stdout.write('[verify] drive.ts disconnected before the verifier started, so the driver stopped the run.\n')
      throw error
    } finally {
      if (activeVerification === verification) activeVerification = undefined
    }
  },

  async close(personaKey) {
    const key = personaKey as string
    const session = sessions.get(key)
    const exited = session ? await closePersona(session.context) : true
    sessions.delete(key)
    return exited ? { closed: key } : { closed: key, stillExiting: true }
  },

  async shutdown() {
    setTimeout(() => void stop(0), 50)
    return { stopping: true }
  },
}

const globalActions = new Set(['status', 'shutdown', 'dialogs', 'verify'])

function authorized(request: IncomingMessage, port: number): boolean {
  if (request.headers.host !== `127.0.0.1:${port}`) return false
  if (request.headers.origin || request.headers['sec-fetch-site']) return false
  const header = request.headers.authorization ?? ''
  const presented = Buffer.from(header.startsWith('Bearer ') ? header.slice(7) : '')
  const expected = Buffer.from(token)
  return presented.length === expected.length && timingSafeEqual(presented, expected)
}

async function readBody(request: IncomingMessage): Promise<Args> {
  const chunks: Buffer[] = []
  let size = 0
  for await (const chunk of request) {
    size += (chunk as Buffer).length
    if (size > 64 * 1024) throw new RpcError('Request body too large')
    chunks.push(chunk as Buffer)
  }
  const parsed: unknown = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}')
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new RpcError('Body must be a JSON object')
  return parsed as Args
}

function send(response: ServerResponse, status: number, body: unknown) {
  response.writeHead(status, { 'content-type': 'application/json', 'cache-control': 'no-store' })
  response.end(JSON.stringify(body))
}

let serverPort = 0
const server = createServer(async (request, response) => {
  if (!authorized(request, serverPort)) return send(response, 401, { ok: false, error: 'unauthorized' })
  if (request.method !== 'POST' || request.url !== '/rpc') return send(response, 404, { ok: false, error: 'not found' })
  // drive.ts waits for the reply, so a connection that closes first means it stopped. Long actions end early.
  const abort = new AbortController()
  response.on('close', () => {
    if (!response.writableFinished) abort.abort()
  })
  try {
    const body = await readBody(request)
    const action = str(body, 'action') as string
    const handler = Object.hasOwn(handlers, action) ? handlers[action] : undefined
    if (!handler) throw new RpcError(`Unknown action "${action}"`)
    const personaKey = globalActions.has(action) ? undefined : str(body, 'persona')
    const args = (typeof body.args === 'object' && body.args !== null ? body.args : {}) as Args
    const result = await handler(personaKey, args, abort.signal)
    send(response, 200, { ok: true, result })
  } catch (error) {
    const message = error instanceof Error ? error.message.split('\n=========================== logs')[0] : String(error)
    send(response, 200, { ok: false, error: truncate(redact(message), 4_000) })
  }
})

function processAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

async function stop(code: number) {
  activeVerification?.child?.kill()
  await Promise.all(
    [...sessions.entries()].map(async ([personaKey, session]) => {
      if (!(await closePersona(session.context))) process.stderr.write(browserStillExiting(personaKey))
    }),
  )
  sessions.clear()
  try {
    const current = JSON.parse(readFileSync(liveSessionFile(), 'utf8')) as { pid?: number }
    if (current.pid === process.pid) rmSync(liveSessionFile(), { force: true })
  } catch {
    // Already removed.
  }
  server.close()
  process.exit(code)
}

if (existsSync(liveSessionFile())) {
  const previous = JSON.parse(readFileSync(liveSessionFile(), 'utf8')) as { pid?: number }
  if (previous.pid && previous.pid !== process.pid && processAlive(previous.pid)) {
    process.stderr.write(`Another live daemon (pid ${previous.pid}) is running. Stop it with "npm run drive -- shutdown".\n`)
    process.exit(1)
  }
}

server.listen(0, '127.0.0.1', () => {
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Unable to bind the control server')
  serverPort = address.port
  ensureDir(stateDir())
  writeFileSync(
    liveSessionFile(),
    JSON.stringify({ pid: process.pid, port: serverPort, token, environment: targets.environment, startedAt: new Date().toISOString() }),
    { mode: 0o600 },
  )
  process.stdout.write(`MOSAIC live driver ready for ${targets.environment} (pid ${process.pid}, port ${serverPort}).\n`)
})

process.on('SIGINT', () => void stop(0))
process.on('SIGTERM', () => void stop(0))
