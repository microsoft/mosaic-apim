import { test as base, type BrowserContext, type BrowserType, type Page } from '@playwright/test'
import { type AppName, type Targets, flags, loadTargets } from './config.ts'
import { ensureSignedIn, launchPersona } from './personas.ts'
import { maskedSelectors } from './redact.ts'

interface OpenPage {
  label: string
  page: Page
}

export class PersonaPool {
  readonly #chromium: BrowserType
  readonly #targets: Targets
  readonly #contexts = new Map<string, BrowserContext>()
  readonly #pages: OpenPage[] = []

  constructor(chromium: BrowserType, targets: Targets) {
    this.#chromium = chromium
    this.#targets = targets
  }

  async context(personaKey: string): Promise<BrowserContext> {
    let context = this.#contexts.get(personaKey)
    if (!context) {
      context = await launchPersona(this.#chromium, this.#targets, personaKey, { headless: flags.headless() })
      this.#contexts.set(personaKey, context)
      for (const page of context.pages()) await page.close()
    }
    return context
  }

  /** Opens a fresh tab for the persona, signed in to the app, at the given path. */
  async page(personaKey: string, app: AppName, path = '/'): Promise<Page> {
    const context = await this.context(personaKey)
    const page = await context.newPage()
    this.#pages.push({ label: `${personaKey}-${app}-${this.#pages.length}`, page })
    await ensureSignedIn(page, this.#targets, personaKey, app, { interactive: flags.interactive(), path })
    return page
  }

  openPages(): OpenPage[] {
    return this.#pages.filter(({ page }) => !page.isClosed())
  }

  async closePages(): Promise<void> {
    await Promise.allSettled(this.#pages.map(({ page }) => page.close()))
    this.#pages.length = 0
  }

  async closeAll(): Promise<void> {
    await this.closePages()
    await Promise.allSettled([...this.#contexts.values()].map((context) => context.close()))
    this.#contexts.clear()
  }
}

interface TestFixtures {
  personaPages: void
}

interface WorkerFixtures {
  targets: Targets
  personas: PersonaPool
}

export const test = base.extend<TestFixtures, WorkerFixtures>({
  targets: [
    // oxlint-disable-next-line no-empty-pattern -- Playwright requires a destructured fixture argument.
    async ({}, use) => {
      await use(loadTargets())
    },
    { scope: 'worker' },
  ],
  personas: [
    async ({ playwright, targets }, use) => {
      const pool = new PersonaPool(playwright.chromium, targets)
      await use(pool)
      await pool.closeAll()
    },
    { scope: 'worker' },
  ],
  personaPages: [
    async ({ personas }, use, testInfo) => {
      await use()
      if (testInfo.status !== testInfo.expectedStatus) {
        for (const { label, page } of personas.openPages()) {
          const body = await page
            .screenshot({ mask: maskedSelectors.map((selector) => page.locator(selector)), animations: 'disabled' })
            .catch(() => undefined)
          if (body) await testInfo.attach(`${label}.png`, { body, contentType: 'image/png' })
        }
      }
      await personas.closePages()
    },
    { auto: true },
  ],
})

export { expect } from '@playwright/test'

export function requireWrites(): void {
  test.skip(!flags.allowWrites(), 'Set MOSAIC_E2E_ALLOW_WRITES=1 to run journeys that change MOSAIC or Azure state.')
}

export function requireModelRequests(): void {
  test.skip(
    !flags.sendModelRequests(),
    'Set MOSAIC_E2E_SEND_MODEL_REQUESTS=1 to run journeys that send billable model requests.',
  )
}
