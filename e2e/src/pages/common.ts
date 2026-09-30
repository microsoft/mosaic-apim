import { type Locator, type Page, type Response, expect } from '@playwright/test'
import { apiPath } from '../denials.ts'
import { redact, truncate } from '../redact.ts'

/**
 * What every page object shares. Page objects find elements by the roles, names and labels the apps render, as
 * a person using a screen reader would; src/locators.ts stays the live driver's string syntax.
 */

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Matches the whole of a name, for accessible names built from data. */
export function exactly(text: string): RegExp {
  return new RegExp(`^${escapeRegExp(text)}$`)
}

/** Every console and portal page shows this title when a query fails or a change is refused. */
export async function expectNoLoadError(page: Page): Promise<void> {
  await expect(page.getByText('Unable to load data')).toHaveCount(0)
}

/**
 * How long a page may take to load its data. A console page loads its tables in parallel, and against the live
 * API one of them can take tens of seconds, far longer than an assertion's default wait.
 */
export const pageDataTimeoutMs = 90_000

/** Waits until the page shows none of the loading indicators with these names, so its tables hold data. */
export async function expectDataLoaded(page: Page, indicators: readonly string[]): Promise<void> {
  for (const name of indicators) {
    await expect(page.getByRole('progressbar', { name })).toHaveCount(0, { timeout: pageDataTimeoutMs })
  }
}

/** A form control by its field label. Fluent marks a required field's label with an asterisk that isn't part of the name. */
export function field(scope: Page | Locator, label: string): Locator {
  return scope.getByLabel(new RegExp(`^${escapeRegExp(label)}(\\s*\\*)?$`))
}

/** A success or error banner. The same text can stay from an earlier action, so actions also check their response. */
export function banner(page: Page | Locator, text: string | RegExp): Locator {
  return page.getByText(text).first()
}

/**
 * Runs an action and returns the MOSAIC API response it caused, matched by method and path only. The response
 * is how a journey knows its action reached MOSAIC, because a banner can remain from an earlier action.
 */
export async function responseTo(
  page: Page,
  apiOrigin: string,
  method: string,
  path: RegExp,
  action: () => Promise<unknown>,
  timeout = 90_000,
): Promise<Response> {
  const [response] = await Promise.all([
    page.waitForResponse(
      (candidate) => {
        if (candidate.request().method() !== method) return false
        const pathname = apiPath(candidate.url(), apiOrigin)
        return pathname !== undefined && path.test(pathname)
      },
      { timeout },
    ),
    action(),
  ])
  return response
}

/** Why MOSAIC refused a request: FastAPI's detail, which never holds a key, redacted and cut short all the same. */
async function refusalDetail(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: unknown }
    return typeof body.detail === 'string' ? ` It said: ${truncate(redact(body.detail), 300)}` : ''
  } catch {
    return ''
  }
}

/** Fails, naming the method, path, status and MOSAIC's reason, unless the response succeeded. */
export async function expectOk(response: Response, what: string): Promise<void> {
  if (response.ok()) return
  const path = new URL(response.url()).pathname
  throw new Error(`${what}: MOSAIC's API answered ${response.status()} to ${response.request().method()} ${path}.${await refusalDetail(response)}`)
}

/** The JSON body of a successful response. Never use it on a response that holds a key. */
export async function jsonOf<T>(response: Response, what: string): Promise<T> {
  await expectOk(response, what)
  return (await response.json()) as T
}

/** True if the response tells the browser and every cache on the way not to keep it. */
export async function noStore(response: Response): Promise<boolean> {
  const headers = await response.allHeaders()
  return /(^|,)\s*no-store\s*(,|$)/i.test(headers['cache-control'] ?? '')
}

/** How much of a revealed secret a page shows, without the secret leaving the browser. */
export async function secretLength(page: Page, selector: string): Promise<number> {
  return page.evaluate((css) => document.querySelector(css)?.textContent?.trim().length ?? 0, selector)
}

/**
 * The places, other than the element that shows it, where a page keeps a revealed secret: the address, the
 * history state, and local or session storage. The comparison runs in the page, so only place names come back.
 */
export async function secretKept(page: Page, selector: string): Promise<string[]> {
  return page.evaluate((css) => {
    const secret = document.querySelector(css)?.textContent?.trim() ?? ''
    if (secret.length < 8) return []
    const places: string[] = []
    const holds = (store: Storage) => {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index) ?? ''
        if (key.includes(secret) || (store.getItem(key) ?? '').includes(secret)) return true
      }
      return false
    }
    if (location.href.includes(secret)) places.push('the address')
    if (JSON.stringify(history.state ?? null).includes(secret)) places.push('the history state')
    if (holds(localStorage)) places.push('local storage')
    if (holds(sessionStorage)) places.push('session storage')
    if (document.title.includes(secret)) places.push('the page title')
    return places
  }, selector)
}
