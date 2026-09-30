import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { escapeRegExp, expectNoLoadError, noStore, responseTo, secretKept, secretLength } from '../common.ts'

export type KeySlot = 'primary' | 'secondary'

/** The badge a My access card shows for each runtime state. */
export const runtimeBadges = {
  pending: 'APIM changes pending',
  applying: 'Applying to APIM',
  applied: 'Applied to APIM',
  revocationPending: 'APIM revocation pending',
  revoked: 'Runtime access revoked',
  failed: 'APIM apply failed',
  unknown: 'Runtime status unknown',
} as const

/** The portal's My access page: a person's grants, their limits and how to connect. */
export class MyAccessPage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.user): Promise<MyAccessPage> {
    const access = new MyAccessPage(await personas.page(personaKey, 'portal', '/access'), targets)
    await access.loaded()
    return access
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'My access', exact: true })).toBeVisible()
    await expect(this.cards.first().or(this.empty)).toBeVisible()
    await expectNoLoadError(this.page)
  }

  async reload(): Promise<void> {
    await this.page.reload()
    await this.loaded()
  }

  get cards(): Locator {
    return this.page.locator('.access-card')
  }

  get empty(): Locator {
    return this.page.getByRole('heading', { name: 'No access granted yet', exact: true })
  }

  /** A grant's card, by the model's name. The heading can also hold a "No longer available" badge. */
  card(displayName: string): Locator {
    const heading = new RegExp(`^${escapeRegExp(displayName)}\\s*(No longer available)?$`)
    return this.cards.filter({ has: this.page.getByRole('heading', { level: 2, name: heading }) })
  }

  runtime(card: Locator, status: keyof typeof runtimeBadges): Locator {
    return card.getByText(runtimeBadges[status], { exact: true })
  }

  /** The limits a card lists, one per item, in the words limitLines() predicts. */
  limits(card: Locator): Locator {
    return card.locator('section', { has: this.page.getByRole('heading', { name: 'Configured grant limits', exact: true }) }).getByRole('listitem')
  }

  attribution(card: Locator): Locator {
    return card.getByRole('heading', { name: 'Usage attribution', exact: true })
  }

  async connectionDetails(card: Locator): Promise<ConnectionPanel> {
    const toggle = card.getByRole('button', { name: 'Connection details', exact: true })
    if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    return new ConnectionPanel(this.page, this.targets, card)
  }
}

/** A grant's connection details on My access. */
export class ConnectionPanel {
  readonly page: Page
  readonly root: Locator
  readonly keys: KeyReveal

  constructor(page: Page, targets: Targets, card: Locator) {
    this.page = page
    this.root = card.locator('.connection-panel')
    this.keys = new KeyReveal(page, targets, this.root)
  }

  /** A section, such as "Endpoint", "Authentication", "Code samples" or "Limits". */
  section(name: string): Locator {
    return this.root.getByRole('heading', { level: 3, name, exact: true })
  }
}

/** The portal's key reveal, which shows a key for 60 seconds and never stores it. */
export class KeyReveal {
  static readonly secretSelector = 'code.secret-value[data-secret="true"]'
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets, scope: Locator) {
    this.page = page
    this.root = scope.getByRole('region', { name: 'Subscription key', exact: true })
    this.#apiOrigin = targets.origins.api
  }

  get status(): Locator {
    return this.root.getByRole('status')
  }

  get secret(): Locator {
    return this.root.locator(KeyReveal.secretSelector)
  }

  /** The screen-reader text in place of a hidden key. */
  get masked(): Locator {
    return this.root.getByText('Hidden', { exact: true })
  }

  /**
   * Shows a key and reports what a person can see and what the browser kept, never the key itself: the
   * response's status and caching, how many characters the page shows, and where else the page holds it.
   */
  async show(slot: KeySlot): Promise<{ response: Response; noStore: boolean; shownLength: number; keptIn: string[] }> {
    const response = await responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/me\/entitlements\/[^/]+\/keys\/reveal$/, () =>
      this.root.getByRole('button', { name: `Show ${slot} key`, exact: true }).click(),
    )
    if (response.ok()) {
      await expect(this.secret).toBeVisible()
      await expect(this.status).toHaveText(`${slot === 'primary' ? 'Primary' : 'Secondary'} key shown. It hides automatically after 60 seconds.`)
    }
    return {
      response,
      noStore: await noStore(response),
      shownLength: await secretLength(this.page, KeyReveal.secretSelector),
      keptIn: await secretKept(this.page, KeyReveal.secretSelector),
    }
  }

  async hide(): Promise<void> {
    await this.root.getByRole('button', { name: 'Hide key', exact: true }).click()
    await expect(this.secret).toHaveCount(0)
    await expect(this.status).toHaveText('Key hidden.')
  }

  /** Waits out the 60 seconds the portal shows a key for. */
  async hidesByItself(): Promise<void> {
    await expect(this.secret).toHaveCount(0, { timeout: 75_000 })
    await expect(this.masked).toBeAttached()
    await expect(this.status).toHaveText('Key hidden automatically after 60 seconds.')
  }
}
