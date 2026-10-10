import { type Page, type Route, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import { ModelProofError, assertModelBudgetTarget } from '../../model-config.ts'
import { expectNoLoadError, expectOk, field } from '../common.ts'

/** Only a specifically owned center's budget, never the organization budget or tenant default. */
export class CostCenterBudgetPage {
  readonly page: Page
  readonly #targets: Targets
  readonly #id: string

  constructor(page: Page, targets: Targets, id: string) {
    if (!targets.modelJourneys) throw new ModelProofError('Missing isolated budget scope')
    assertModelBudgetTarget(targets.modelJourneys, id)
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

  async save(amount: number, authorize: () => void | Promise<void>): Promise<{ startedAt: string; savedAt: string }> {
    assertModelBudgetTarget(this.#targets.modelJourneys!, this.#id)
    await field(this.form, 'Monthly amount (USD)').fill(String(amount))
    await field(this.form, 'Warn at (%)').fill('80, 100')
    await field(this.form, 'Also email').fill('')
    await this.form.getByRole('switch', { name: /^Email the owners/ }).uncheck()
    await this.form.getByRole('radio', { name: 'Block calls at the gateway until the budget is raised or the month ends', exact: true }).check()
    await authorize()
    const startedAt = new Date().toISOString()
    const path = `/api/v1/cost-centers/${this.#id}/budget`
    const apiUrl = (url: URL) => url.origin === this.#targets.origins.api
    const isWrite = (method: string) => !['GET', 'HEAD', 'OPTIONS'].includes(method)
    let denied: { error: unknown } | undefined
    // Click actionability can wait after the pre-click check; guard the outgoing write as well.
    const guard = async (route: Route) => {
      if (isWrite(route.request().method())) {
        try {
          const url = new URL(route.request().url())
          if (route.request().method() !== 'PUT' || url.pathname !== path || url.search) {
            throw new ModelProofError('Budget form attempted an unapproved write target; no default, General or organization writes permitted')
          }
          assertModelBudgetTarget(this.#targets.modelJourneys!, this.#id)
          await authorize()
        } catch (error) {
          denied = { error }
          await route.fulfill({ status: 403, body: 'Budget save authorization failed' })
          return
        }
      }
      await route.fallback()
    }
    await this.page.route(apiUrl, guard)
    try {
      const [response] = await Promise.all([
        this.page.waitForResponse((candidate) => apiUrl(new URL(candidate.url())) && isWrite(candidate.request().method()), { timeout: 300_000 }),
        this.form.getByRole('button', { name: /^(Set|Save) budget$/ }).click(),
      ])
      if (denied) throw denied.error
      await expectOk(response, 'Save isolated no-recipient budget')
      return { startedAt, savedAt: new Date().toISOString() }
    } finally {
      await this.page.unroute(apiUrl, guard)
    }
  }
}
