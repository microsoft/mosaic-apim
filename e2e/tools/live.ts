import { chromium, type BrowserContext, type Locator, type Page } from '@playwright/test'
import { randomBytes, timingSafeEqual } from 'node:crypto'
import { existsSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { join } from 'node:path'
import { type AppName, type Targets, flags, loadTargets, persona, resolveTargetRef } from '../src/config.ts'
import { type LocatorOptions, describeLocator, locate } from '../src/locators.ts'
import { ensureDir, artifactsDir, liveSessionFile, stateDir } from '../src/paths.ts'
import { ensureSignedIn, launchPersona } from '../src/personas.ts'
import { redact, maskedSelectors, redactUrl, truncate } from '../src/redact.ts'

/**
 * Local control daemon for human-in-the-loop UI journeys. It owns one persistent browser profile per
 * persona and exposes a narrow, token-protected RPC surface on 127.0.0.1 for tools/drive.ts.
 * There is deliberately no arbitrary script evaluation and no way to read tokens or revealed keys.
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

class RpcError extends Error {}

const maxLogEntries = 80
const targets: Targets = loadTargets()
const sessions = new Map<string, PersonaSession>()
const signingIn = new Set<string>()
const token = randomBytes(32).toString('hex')
const allowedOrigins = new Set([targets.origins.web, targets.origins.portal])
let dialogMode: 'accept' | 'dismiss' = 'dismiss'

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
  const parsed = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(parsed) || parsed < 0) throw new RpcError(`"${key}" must be a non-negative number`)
  return Math.min(parsed, max)
}

function locatorOptions(args: Args): LocatorOptions {
  const nth = args.nth === undefined ? undefined : Number(args.nth)
  return {
    exact: args.exact === true,
    nth,
    within: str(args, 'within', false),
    row: str(args, 'row', false),
    hasText: str(args, 'has', false),
  }
}

function target(session: PersonaSession, args: Args, key = 'target'): { locator: Locator; label: string } {
  const spec = str(args, key) as string
  const options = locatorOptions(args)
  return { locator: locate(session.current, spec, options), label: describeLocator(spec, options) }
}

async function knownSecrets(page: Page): Promise<string[]> {
  return page
    .locator('[data-secret]')
    .evaluateAll((elements) =>
      elements.map((element) => ((element as HTMLInputElement).value || element.textContent || '').trim()),
    )
    .catch(() => [])
}

async function pageState(page: Page) {
  return { url: redactUrl(page.url()), title: redact(await page.title().catch(() => '')) }
}

function appUrl(app: string, path: string | undefined): string {
  if (app !== 'web' && app !== 'portal') throw new RpcError('app must be "web" or "portal"')
  const origin = targets.origins[app as AppName]
  const url = new URL(path ?? '/', origin)
  if (!allowedOrigins.has(url.origin)) throw new RpcError('Navigation is limited to the MOSAIC web and portal origins')
  return url.href
}

const timeout = (args: Args, fallback = 15_000) => num(args, 'timeout', fallback, 900_000)

const handlers: Record<string, (personaKey: string | undefined, args: Args) => Promise<unknown>> = {
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
      const session = await openSession(key)
      if (session.current.isClosed()) session.current = await session.context.newPage()
      const app = str(args, 'app') as AppName
      appUrl(app, '/')
      await session.current.bringToFront()
      await ensureSignedIn(session.current, targets, key, app, {
        interactive: true,
        path: str(args, 'path', false),
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
    const session = existingSession(personaKey as string)
    const root = args.target ? target(session, args).locator : session.current.locator('body')
    const snapshot = await root.ariaSnapshot({ timeout: timeout(args) })
    const secrets = await knownSecrets(session.current)
    return truncate(redact(snapshot, secrets), num(args, 'max', 12_000, 80_000))
  },

  async click(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    await locator.click({ timeout: timeout(args) })
    return { clicked: label, ...(await pageState(session.current)) }
  },

  async hover(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    await locator.hover({ timeout: timeout(args) })
    return { hovered: label }
  },

  async fill(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    const value = resolveTargetRef(targets, str(args, 'value') ?? '')
    await locator.fill(value, { timeout: timeout(args) })
    return { filled: label, characters: value.length }
  },

  async clear(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    await locator.clear({ timeout: timeout(args) })
    return { cleared: label }
  },

  async choose(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    const option = resolveTargetRef(targets, str(args, 'option') as string)
    await locator.click({ timeout: timeout(args) })
    await session.current.getByRole('option', { name: option, exact: args.exact === true }).first().click({ timeout: timeout(args) })
    return { chose: option, in: label }
  },

  async select(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    const value = resolveTargetRef(targets, str(args, 'value') as string)
    const selected = await locator.selectOption(value, { timeout: timeout(args) })
    return { selected, in: label }
  },

  async check(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    await locator.setChecked(args.checked !== false, { timeout: timeout(args) })
    return { [args.checked === false ? 'unchecked' : 'checked']: label }
  },

  async press(personaKey, args) {
    const session = existingSession(personaKey as string)
    const key = str(args, 'key') as string
    if (args.target) {
      const { locator, label } = target(session, args)
      await locator.press(key, { timeout: timeout(args) })
      return { pressed: key, on: label }
    }
    await session.current.keyboard.press(key)
    return { pressed: key }
  },

  async wait(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
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
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
    const secret = await locator.first().evaluate((element) => element.closest('[data-secret]') !== null)
    if (secret) return { target: label, text: '[redacted-secret]' }
    const text = await locator.first().innerText({ timeout: timeout(args) })
    return { target: label, text: redact(text, await knownSecrets(session.current)) }
  },

  async count(personaKey, args) {
    const session = existingSession(personaKey as string)
    const { locator, label } = target(session, args)
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

  async close(personaKey) {
    const session = sessions.get(personaKey as string)
    if (session) await session.context.close()
    sessions.delete(personaKey as string)
    return { closed: personaKey }
  },

  async shutdown() {
    setTimeout(() => void stop(0), 50)
    return { stopping: true }
  },
}

const globalActions = new Set(['status', 'shutdown', 'dialogs'])

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
  try {
    const body = await readBody(request)
    const action = str(body, 'action') as string
    const handler = Object.hasOwn(handlers, action) ? handlers[action] : undefined
    if (!handler) throw new RpcError(`Unknown action "${action}"`)
    const personaKey = globalActions.has(action) ? undefined : str(body, 'persona')
    const args = (typeof body.args === 'object' && body.args !== null ? body.args : {}) as Args
    const result = await handler(personaKey, args)
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
  await Promise.allSettled([...sessions.values()].map((session) => session.context.close()))
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
