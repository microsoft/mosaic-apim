import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import type { ApiCatalogEntry } from '../../mosaic-api.ts'
import { portalCatalogResourceKey, portalCatalogTitle } from '../../suite.ts'
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
    return this.page.locator('.catalog-list > .resource-row')
  }

  get empty(): Locator {
    return this.page.getByRole('heading', { name: 'No catalog entries', exact: true })
  }

  card(displayName: string): Locator {
    return this.cards.filter({ has: this.page.getByRole('heading', { level: 2, name: displayName, exact: true }) })
  }

  /** Shown for the exact offering and cost center selected in the drawer. */
  entitled(_card: Locator): Locator {
    return this.page.getByText(/^Already entitled( for this cost center)?$/)
  }

  /** Shown when the person has an open request for the resource under the selected cost center. */
  requestOpen(_card: Locator): Locator {
    return this.page.getByText(/^A request is already open( for this cost center)?\.$/)
  }

  /** The cost center a request charges. It starts on the person's default cost center. */
  costCenter(_card: Locator, displayName: string): Locator {
    return this.page.getByRole('dialog').getByRole('combobox', { name: `Cost center for ${displayName}`, exact: true })
  }

  requestButton(card: Locator): Locator {
    return card.getByRole('button', { name: /^View access options for / })
  }

  async openOptions(card: Locator, entry?: ApiCatalogEntry): Promise<void> {
    const dialog = this.page.getByRole('dialog')
    if (await dialog.isVisible()) {
      await dialog.getByRole('button', { name: 'Close details', exact: true }).click()
      await expect(dialog).toHaveCount(0)
    }
    if (entry) await expect(this.card(portalCatalogTitle(entry)), 'an unambiguous model row for the exact offering').toHaveCount(1)
    await this.requestButton(card).click()
    await expect(dialog).toBeVisible()
    const options = dialog.getByRole('combobox', { name: 'Access option', exact: true })
    if (await options.count()) {
      if (!entry) throw new Error('An exact resource is required to select grouped access options.')
      await options.selectOption(portalCatalogResourceKey(entry))
    }
  }

  /**
   * Requests access with a justification, charged to the person's default cost center unless `costCenter` names
   * another of theirs by its option text, such as "Research (RES)". The response says whether MOSAIC opened it.
   */
  async request(entry: ApiCatalogEntry, justification: string, costCenter?: string): Promise<Response> {
    const { displayName } = entry
    const card = this.card(portalCatalogTitle(entry))
    await this.openOptions(card, entry)
    if (costCenter) await this.page.getByRole('combobox', { name: `Cost center for ${displayName}`, exact: true }).selectOption({ label: costCenter })
    await this.page.getByRole('textbox', { name: `Justification for ${displayName}`, exact: true }).fill(justification)
    return responseTo(this.page, this.targets.origins.api, 'POST', /^\/api\/v1\/portal\/access-requests$/, () => this.page.getByRole('button', { name: 'Request access', exact: true }).click())
  }

  /** Withdraws the exact offering's open request from its drawer. */
  async withdraw(entry: ApiCatalogEntry): Promise<Response> {
    await this.openOptions(this.card(portalCatalogTitle(entry)), entry)
    return responseTo(this.page, this.targets.origins.api, 'POST', /^\/api\/v1\/portal\/access-requests\/[^/]+\/withdraw$/, () =>
      this.page.getByRole('button', { name: 'Withdraw', exact: true }).click(),
    )
  }
}
