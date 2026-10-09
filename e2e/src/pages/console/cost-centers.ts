import { type Page, type Route, expect } from '@playwright/test'
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

  async save(amount: number, authorize: () => void): Promise<{ startedAt: string; savedAt: string }> {
    await field(this.form, 'Monthly amount (USD)').fill(String(amount))
    await field(this.form, 'Warn at (%)').fill('80, 100')
    await field(this.form, 'Also email').fill('')
    await this.form.getByRole('switch', { name: /^Email the owners/ }).uncheck()
    await this.form.getByRole('radio', { name: 'Block calls at the gateway until the budget is raised or the month ends', exact: true }).check()
    authorize()
    const startedAt = new Date().toISOString()
    const path = `/api/v1/cost-centers/${this.#id}/budget`
    const budgetUrl = (url: URL) => url.origin === this.#targets.origins.api && url.pathname === path
    let denied: { error: unknown } | undefined
    // Click actionability can wait after the pre-click check; guard the outgoing write as well.
    const guard = async (route: Route) => {
      if (route.request().method() === 'PUT') {
        try { authorize() } catch (error) {
          denied = { error }
          await route.fulfill({ status: 403, body: 'Budget save authorization failed' })
          return
        }
      }
      await route.fallback()
    }
    await this.page.route(budgetUrl, guard)
    try {
      const response = await responseTo(this.page, this.#targets.origins.api, 'PUT',
        new RegExp(`^/api/v1/cost-centers/${escapeRegExp(this.#id)}/budget$`),
        () => this.form.getByRole('button', { name: /^(Set|Save) budget$/ }).click(),
        300_000)
      if (denied) throw denied.error
      await expectOk(response, 'Save isolated no-recipient budget')
      return { startedAt, savedAt: new Date().toISOString() }
    } finally {
      await this.page.unroute(budgetUrl, guard)
    }
  }
}
