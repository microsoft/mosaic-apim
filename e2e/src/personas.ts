import type { BrowserContext, BrowserType, Page } from '@playwright/test'
import { join } from 'node:path'
import { type AppName, type Targets, persona, TargetsError } from './config.ts'
import { ensureDir, profilesDir } from './paths.ts'

const personaKeyPattern = /^[a-z][a-z0-9-]{0,31}$/
const loginHosts = ['login.microsoftonline.com', 'login.microsoft.com', 'login.live.com', 'account.live.com']

export const primaryNavigation = { role: 'navigation', name: 'Primary navigation' } as const
export const signInButtonName = 'Sign in with Microsoft'
export const noPortalAccessTitle = 'You do not have access to the portal yet'
/** The console shows one of these cards instead of its shell when the API does not confirm the Admin role. */
export const consoleUserOnlyTitle = 'This console is for MOSAIC administrators'
export const noConsoleAccessTitle = 'You do not have access to MOSAIC yet'
export const consoleAccessCheckFailedTitle = 'Unable to check your access'
const accessPageTitles = [noPortalAccessTitle, consoleUserOnlyTitle, noConsoleAccessTitle, consoleAccessCheckFailedTitle]

export class SignInError extends Error {}

export function profileDir(personaKey: string): string {
  if (!personaKeyPattern.test(personaKey)) throw new TargetsError(`Invalid persona key "${personaKey}"`)
  return join(profilesDir(), personaKey)
}

export interface LaunchOptions {
  headless?: boolean
  channel?: string
}

export async function launchPersona(
  chromium: BrowserType,
  targets: Targets,
  personaKey: string,
  options: LaunchOptions = {},
): Promise<BrowserContext> {
  const configured = persona(targets, personaKey).channel ?? options.channel ?? process.env.MOSAIC_E2E_BROWSER_CHANNEL
  const channel = configured && configured !== 'chromium' ? configured : undefined
  return chromium.launchPersistentContext(ensureDir(profileDir(personaKey)), {
    channel,
    headless: options.headless ?? false,
    viewport: { width: 1440, height: 900 },
    locale: 'en-US',
    colorScheme: 'light',
    acceptDownloads: false,
    args: ['--no-first-run', '--no-default-browser-check'],
  })
}

const closeWaitMs = 20_000

/**
 * Closes a persona's browser, waiting at most `waitMs`. On a busy Windows machine an exited browser can
 * stay in the process table for a minute or more after it has saved the profile, and close() waits for
 * that. Returns false if it stopped waiting; the browser finishes exiting on its own.
 */
export async function closePersona(context: Pick<BrowserContext, 'close'>, waitMs = closeWaitMs): Promise<boolean> {
  let timer: ReturnType<typeof setTimeout> | undefined
  const gaveUp = new Promise<false>((resolve) => {
    timer = setTimeout(() => resolve(false), waitMs)
  })
  const closed = context.close().then(
    () => true,
    () => true,
  )
  try {
    return await Promise.race([closed, gaveUp])
  } finally {
    clearTimeout(timer)
  }
}

/**
 * Leaves the persona's browser with one blank tab, which the harness keeps open between tests. A headed
 * Chromium quits a moment after its last tab closes, so a persona whose pages all closed at the end of one
 * test had no browser left for its next test once another persona's test ran in between.
 */
export async function keepOneBlankTab(context: Pick<BrowserContext, 'pages' | 'newPage'>): Promise<Page> {
  const [first, ...others] = context.pages()
  const tab = first ?? (await context.newPage())
  for (const page of others) await page.close()
  if (tab.url() !== 'about:blank') await tab.goto('about:blank')
  return tab
}

export function browserStillExiting(personaKey: string): string {
  return `${personaKey}: the browser hadn't finished exiting, so the harness moved on. It finishes on its own.\n`
}

export function isLoginHost(url: string): boolean {
  try {
    return loginHosts.includes(new URL(url).hostname)
  } catch {
    return false
  }
}

function onOrigin(url: string, origin: string): boolean {
  try {
    return new URL(url).origin === origin
  } catch {
    return false
  }
}

/** True when the URL is on the MOSAIC web console or portal. */
export function isAppUrl(targets: Targets, url: string): boolean {
  return onOrigin(url, targets.origins.web) || onOrigin(url, targets.origins.portal)
}

/**
 * Resolves a landing path against the app's origin. Absolute, scheme-relative, javascript: and file: values
 * resolve somewhere else, so they're refused.
 */
export function appDestination(targets: Targets, app: AppName, path = '/'): URL {
  const origin = targets.origins[app]
  let destination: URL
  try {
    destination = new URL(path, origin)
  } catch {
    throw new SignInError(`"${path}" is not a valid path on the ${app} app`)
  }
  if (destination.origin !== origin) throw new SignInError(`Paths must stay on the ${app} app (${origin})`)
  return destination
}

async function visible(page: Page, selector: string): Promise<boolean> {
  return page.locator(selector).first().isVisible().catch(() => false)
}

export async function signInErrorCode(page: Page): Promise<string | undefined> {
  const text = await page.locator('body').innerText({ timeout: 1_000 }).catch(() => '')
  return /AADSTS\d{5,}/.exec(text)?.[0]
}

/**
 * Silent SSO passes through the login host before any page renders there, so a login page must show
 * nothing to automate for this long before a non-interactive sign-in decides a person is needed. A B2B
 * guest's silent sign-in has sat on the login host for more than 10 seconds before finishing on its own.
 */
const loginSettleMs = 30_000

/**
 * Drives the Microsoft Entra redirect for one persona. Account pickers, the UPN prompt, and
 * "Stay signed in?" are automated; passwords, MFA, and consent are left to the human at the keyboard.
 */
export async function completeEntraSignIn(
  page: Page,
  origin: string,
  upn: string,
  interactive: boolean,
  timeoutMs: number,
  settleMs = loginSettleMs,
): Promise<void> {
  const deadline = Date.now() + timeoutMs
  let announced = false
  let idleSince: number | undefined
  const tile = `[data-test-id="${upn.replace(/["\\]/g, '')}" i]`
  while (Date.now() < deadline) {
    const url = page.url()
    if (onOrigin(url, origin)) return
    if (isLoginHost(url)) {
      const code = await signInErrorCode(page)
      if (code) throw new SignInError(`Microsoft Entra rejected the sign-in with ${code}`)
      if (await visible(page, tile)) {
        idleSince = undefined
        await page.locator(tile).first().click()
      } else if (await visible(page, 'input[name="loginfmt"]:not([aria-hidden="true"])')) {
        idleSince = undefined
        const input = page.locator('input[name="loginfmt"]').first()
        if ((await input.inputValue()).toLowerCase() !== upn.toLowerCase()) await input.fill(upn)
        await page.locator('input[type="submit"]').first().click()
      } else if (await visible(page, '#KmsiCheckboxField, #KmsiDescription')) {
        idleSince = undefined
        await page.locator('#idSIButton9').first().click()
      } else if (!interactive) {
        idleSince ??= Date.now()
        if (Date.now() - idleSince >= settleMs) {
          throw new SignInError(
            `Sign-in for ${upn} needs a password, MFA, or consent. Run "npm run login -- <persona>" first or set MOSAIC_E2E_INTERACTIVE=1.`,
          )
        }
      } else if (!announced) {
        announced = true
        process.stderr.write(`Waiting for ${upn} to finish signing in in the browser window…\n`)
      }
    } else {
      idleSince = undefined
    }
    await page.waitForTimeout(750)
  }
  throw new SignInError(`Timed out waiting for ${upn} to sign in`)
}

export async function signedInUsernames(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const names = new Set<string>()
    for (const storage of [window.sessionStorage, window.localStorage]) {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index)
        if (!key) continue
        try {
          const value = JSON.parse(storage.getItem(key) ?? 'null') as Record<string, unknown> | null
          if (value && typeof value.username === 'string' && typeof value.homeAccountId === 'string') {
            names.add(value.username.toLowerCase())
          }
        } catch {
          // Non-JSON storage entries are not MSAL accounts.
        }
      }
    }
    return [...names]
  })
}

export interface SignInOptions {
  interactive: boolean
  path?: string
  timeoutMs?: number
}

export async function ensureSignedIn(
  page: Page,
  targets: Targets,
  personaKey: string,
  app: AppName,
  options: SignInOptions,
): Promise<void> {
  const origin = targets.origins[app]
  const destination = appDestination(targets, app, options.path)
  const { upn } = persona(targets, personaKey)
  if (!onOrigin(page.url(), origin)) {
    await page.goto(destination.href)
  }
  const shell = page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })
  const landed = accessPageTitles.reduce((locator, title) => locator.or(page.getByText(title)), shell)
  const signIn = page.getByRole('button', { name: signInButtonName })
  await landed.or(signIn).first().waitFor({ state: 'visible', timeout: 45_000 })
  if (await signIn.isVisible()) {
    await signIn.click()
    await completeEntraSignIn(page, origin, upn, options.interactive, options.timeoutMs ?? (options.interactive ? 600_000 : 60_000))
    await landed.or(signIn).first().waitFor({ state: 'visible', timeout: 60_000 })
    if (await signIn.isVisible()) {
      throw new SignInError(`${personaKey} returned to ${app} without an authenticated session`)
    }
  }
  const usernames = await signedInUsernames(page)
  if (!usernames.includes(upn.toLowerCase())) {
    throw new SignInError(
      `${app} is signed in as ${usernames.join(', ') || 'an unknown account'}, not persona ${personaKey}. Sign out of that profile and retry.`,
    )
  }
  if (options.path && new URL(page.url()).pathname !== destination.pathname) {
    await page.goto(destination.href)
    await landed.first().waitFor({ state: 'visible', timeout: 45_000 })
  }
}
