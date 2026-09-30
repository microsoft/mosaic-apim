import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { expectNoLoadError, responseTo } from '../common.ts'

/** The portal's catalog, where a person finds models and requests access to them. */
export class CatalogPage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.user): Promise<CatalogPage> {
    const catalog = new CatalogPage(await personas.page(personaKey, 'portal', '/catalog'), targets)
    await catalog.loaded()
    return catalog
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'Catalog', exact: true })).toBeVisible()
    await expect(this.cards.first().or(this.empty)).toBeVisible()
    await expectNoLoadError(this.page)
  }

  async reload(): Promise<void> {
    await this.page.reload()
    await this.loaded()
  }

  get cards(): Locator {
    return this.page.locator('.catalog-card')
  }

  get empty(): Locator {
    return this.page.getByRole('heading', { name: 'No catalog entries', exact: true })
  }

  card(displayName: string): Locator {
    return this.cards.filter({ has: this.page.getByRole('heading', { level: 2, name: displayName, exact: true }) })
  }

  entitled(card: Locator): Locator {
    return card.getByText('Already entitled', { exact: true })
  }

  requestOpen(card: Locator): Locator {
    return card.getByText('A request is already open.', { exact: true })
  }

  requestButton(card: Locator): Locator {
    return card.getByRole('button', { name: 'Request access', exact: true })
  }

  /** Requests access with a justification. The response says whether MOSAIC opened the request. */
  async request(displayName: string, justification: string): Promise<Response> {
    const card = this.card(displayName)
    await card.getByRole('textbox', { name: `Justification for ${displayName}`, exact: true }).fill(justification)
    return responseTo(this.page, this.targets.origins.api, 'POST', /^\/api\/v1\/portal\/access-requests$/, () => this.requestButton(card).click())
  }

  /** Withdraws the open request from the catalog card. */
  withdraw(displayName: string): Promise<Response> {
    return responseTo(this.page, this.targets.origins.api, 'POST', /^\/api\/v1\/portal\/access-requests\/[^/]+\/withdraw$/, () =>
      this.card(displayName).getByRole('button', { name: 'Withdraw', exact: true }).click(),
    )
  }
}
