import { type Page, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import { escapeRegExp, expectNoLoadError, expectOk, field, responseTo } from '../common.ts'

/** Only a specifically owned center's budget, never the organization budget or tenant default. */
export class CostCenterBudgetPage {
  readonly page: Page
  readonly #targets: Targets
  readonly #id: string

  constructor(page: Page, targets: Targets, id: string) {
    this.page = page
    this.#targets = targets
    this.#id = id
  }

  async loaded(name: string): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name, exact: true })).toBeVisible()
    await expect(this.form).toBeVisible()
    await expectNoLoadError(this.page)
  }

  get form() { return this.page.getByRole('form', { name: 'Cost center budget', exact: true }) }

  async save(amount: number): Promise<{ startedAt: string; savedAt: string }> {
    await field(this.form, 'Monthly amount (USD)').fill(String(amount))
    await field(this.form, 'Warn at (%)').fill('80, 100')
    await field(this.form, 'Also email').fill('')
    await this.form.getByRole('switch', { name: /^Email the owners/ }).uncheck()
    await this.form.getByRole('radio', { name: 'Block calls at the gateway until the budget is raised or the month ends', exact: true }).check()
    const startedAt = new Date().toISOString()
    const response = await responseTo(this.page, this.#targets.origins.api, 'PUT',
      new RegExp(`^/api/v1/cost-centers/${escapeRegExp(this.#id)}/budget$`),
      () => this.form.getByRole('button', { name: /^(Set|Save) budget$/ }).click(),
      300_000)
    await expectOk(response, 'Save isolated no-recipient budget')
    return { startedAt, savedAt: new Date().toISOString() }
  }
}
