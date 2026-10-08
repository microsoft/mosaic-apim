import type { Page, Request } from '@playwright/test'
import { isLoginHost, signInErrorCode } from './personas.ts'
import { redact, truncate } from './redact.ts'
import { type SignInPrompt, type VerifyPersonas, bearerToken, isDeviceLoginUrl } from './verify.ts'

/**
 * Device-code sign-ins for scripts/verify_model_access.py, shared by the live driver (tools/live.ts) and the
 * ordered specs. The code is entered in the persona's own browser; the person at the keyboard confirms the
 * sign-in and completes MFA, as with every other sign-in.
 */

// The verifier waits at least a second before its first poll, so output within this window of a prompt was
// written before it, on the other stream, and doesn't mean the sign-in is over.
export const deviceSignInGraceMs = 750

// What the verifier prints when the sign-in service fails a poll for a moment, and it keeps waiting.
const stillWaiting = /^INFO: The sign-in service answered HTTP \d{3}; still waiting for .{1,200} to sign in$/

/**
 * Whether a line of verifier output, this long after a device-code prompt, means that sign-in is over. Any line
 * after the grace window does, except the verifier's note that it's still waiting.
 */
export function endsDeviceSignIn(line: string, sincePromptMs: number): boolean {
  return sincePromptMs >= deviceSignInGraceMs && !stillWaiting.test(line.trim())
}

/** A device-code sign-in the driver started: when, and how to end it. */
export interface DeviceSignIn {
  startedAt: number
  finish(): void
}

/** The device-code sign-ins of one verifier run, followed through its output. */
export interface DeviceSignIns {
  /** Reads one line of output, which `show` prints, ending and starting sign-ins around it. */
  line(raw: string, show: () => void): void
  /** Ends the sign-in in progress, as when the verifier exits. */
  end(): void
}

/**
 * Follows a verifier's output for its device-code sign-ins, one at a time. Before a line is shown, it ends the
 * sign-in in progress if endsDeviceSignIn says so. After a prompt is shown, it ends that sign-in anyway and starts
 * the one the prompt asks for. So every prompt is entered, even a second one for the same person in one run.
 */
export function followDeviceSignIns(
  parse: (line: string) => SignInPrompt | undefined,
  start: (prompt: SignInPrompt) => DeviceSignIn | undefined,
  now: () => number = Date.now,
): DeviceSignIns {
  let current: DeviceSignIn | undefined
  const end = () => {
    current?.finish()
    current = undefined
  }
  return {
    line(raw, show) {
      if (current && endsDeviceSignIn(raw, now() - current.startedAt)) end()
      show()
      const prompt = parse(raw)
      if (prompt) {
        end()
        current = start(prompt)
      }
    },
    end,
  }
}

/** True for a MOSAIC API call that carries a bearer token: where a persona's MOSAIC API token is taken from. */
export async function carriesApiToken(request: Request, apiOrigin: string): Promise<boolean> {
  return (
    request.url().startsWith(`${apiOrigin}/api/`) &&
    bearerToken(await request.headerValue('authorization').catch(() => null)) !== undefined
  )
}

export type DeviceCodeRefusal = 'not-device-login' | 'no-persona'

/**
 * Whose browser a verifier sign-in prompt is entered in, or why it isn't: the address isn't a Microsoft device
 * sign-in page, or the run has no persona for whoever the verifier asked for.
 */
export function deviceCodePersona(
  prompt: SignInPrompt,
  people: VerifyPersonas,
): { personaKey: string } | { refused: DeviceCodeRefusal } {
  if (!isDeviceLoginUrl(prompt.uri)) return { refused: 'not-device-login' }
  const personaKey = prompt.subject === 'user' ? people.user : prompt.subject === 'stranger' ? people.stranger : undefined
  if (personaKey === undefined) return { refused: 'no-persona' }
  return { personaKey }
}

/**
 * Enters a device code on a page in the persona's browser and picks the persona's account if Entra asks. It keeps
 * watching the page, reporting sign-in errors, until isDone() says the sign-in is over or the page closes.
 */
export async function typeDeviceCode(
  page: Page,
  personaKey: string,
  upn: string,
  prompt: SignInPrompt,
  isDone: () => boolean,
  emit: (line: string) => void,
): Promise<void> {
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
  while (!isDone() && !page.isClosed()) {
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
