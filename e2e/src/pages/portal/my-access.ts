import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { escapeRegExp, expectNoLoadError, noStore, responseTo, secretKept, secretLength } from '../common.ts'

export type KeySlot = 'primary' | 'secondary'

/** The badge an exact grant's drawer shows for each runtime state. */
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
    await expect(this.page.getByRole('heading', { level: 1, name: 'My access', exact: true, includeHidden: true })).toBeVisible()
    await expect(this.cards.first().or(this.empty)).toBeVisible()
    await expectNoLoadError(this.page)
  }

  async reload(): Promise<void> {
    await this.page.reload()
    await this.loaded()
  }

  get cards(): Locator {
    return this.page.locator('.access-list > .resource-row')
  }

  get empty(): Locator {
    return this.page.getByRole('heading', { name: 'No access granted yet', exact: true })
  }

  /** A model row; choose the exact grant before checking limits or connection details. */
  card(displayName: string): Locator {
    const heading = new RegExp(`^${escapeRegExp(displayName)}\\s*(No longer available)?$`)
    return this.cards.filter({ has: this.page.getByRole('heading', { level: 2, name: heading, includeHidden: true }) })
  }

  runtime(_card: Locator, status: keyof typeof runtimeBadges): Locator {
    return this.page.getByRole('dialog').getByText(runtimeBadges[status], { exact: true })
  }

  /** The selected grant's limits, in the words limitLines() predicts. */
  limits(_card: Locator): Locator {
    return this.page.getByRole('heading', { name: 'Configured grant limits', exact: true }).locator('..').getByRole('listitem')
  }

  attribution(_card: Locator): Locator {
    return this.page.getByRole('heading', { name: 'Usage attribution', exact: true })
  }

  async grantDetails(card: Locator, grantId?: string): Promise<void> {
    const dialog = this.page.getByRole('dialog')
    if (await dialog.isVisible()) {
      await dialog.getByRole('button', { name: 'Close details', exact: true }).click()
      await expect(dialog).toHaveCount(0)
    }
    await card.getByRole('button', { name: /^View access for / }).click()
    await expect(dialog).toBeVisible()
    const grants = dialog.getByRole('combobox', { name: 'Grant', exact: true })
    if (await grants.count()) {
      if (!grantId) throw new Error('An exact grant ID is required to select grouped access.')
      await grants.selectOption(grantId)
    }
    await expect(this.page.getByRole('heading', { name: 'Configured grant limits', exact: true })).toBeVisible()
    if (grantId) await expect.poll(() => new URL(this.page.url()).searchParams.get('grant')).toBe(grantId)
  }

  async connectionDetails(card: Locator, grantId?: string): Promise<ConnectionPanel> {
    await this.grantDetails(card, grantId)
    return this.#expandConnection()
  }

  async connectionForGrant(grantId: string): Promise<ConnectionPanel> {
    const url = new URL(this.page.url())
    url.searchParams.delete('resource')
    url.searchParams.set('grant', grantId)
    await this.page.goto(url.href)
    await expect(this.page.getByRole('heading', { name: 'Configured grant limits', exact: true })).toBeVisible()
    await expect.poll(() => new URL(this.page.url()).searchParams.get('grant')).toBe(grantId)
    return this.#expandConnection()
  }

  async #expandConnection(): Promise<ConnectionPanel> {
    const toggle = this.page.getByRole('button', { name: 'Connection details', exact: true })
    if ((await toggle.getAttribute('aria-expanded')) !== 'true') await toggle.click()
    await expect(toggle).toHaveAttribute('aria-expanded', 'true')
    return new ConnectionPanel(this.page, this.targets)
  }
}

/** A grant's connection details on My access. */
export class ConnectionPanel {
  readonly page: Page
  readonly root: Locator
  readonly keys: KeyReveal

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.root = page.locator('.connection-panel')
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

  /** Shown while the grant has no key. Applies don't create keys: the grant's holder asks for one. */
  get createButton(): Locator {
    return this.root.getByRole('button', { name: 'Create key', exact: true })
  }

  /** Creates the grant's key when it has none yet, and waits until a slot can be shown. */
  async ensureKey(): Promise<Response | undefined> {
    const show = this.root.getByRole('button', { name: 'Show primary key', exact: true })
    await expect(show.or(this.createButton).first()).toBeVisible()
    if (!(await this.createButton.isVisible())) return undefined
    const response = await responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/me\/entitlements\/[^/]+\/keys$/, () =>
      this.createButton.click(),
    )
    if (response.ok()) await expect(show).toBeEnabled()
    return response
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
