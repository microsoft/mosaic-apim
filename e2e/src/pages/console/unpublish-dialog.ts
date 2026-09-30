import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import { responseTo } from '../common.ts'

const unpublishPath = /^\/api\/v1\/publications\/[^/]+\/unpublish$/

/**
 * The console's unpublish review, an alert dialog. Opening it plans the unpublish, which removes nothing: it shows
 * who loses access and what MOSAIC deletes, and only "Unpublish model" runs that plan. MOSAIC refuses a plan the
 * publication has outgrown; the dialog then says so and plans again.
 */
export class UnpublishDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets, displayName: string) {
    this.page = page
    this.root = page.getByRole('alertdialog', { name: `Unpublish ${displayName}?`, exact: true })
    this.#apiOrigin = targets.origins.api
  }

  async visible(): Promise<void> {
    await expect(this.root).toBeVisible()
  }

  // Review

  get review(): Locator {
    return this.root.getByRole('region', { name: 'Unpublish review' })
  }

  get losingGrants(): Locator {
    return this.root.getByRole('table', { name: 'Grants that lose access' })
  }

  get planSteps(): Locator {
    return this.root.getByRole('table', { name: 'Unpublish plan steps' })
  }

  /** The plan's rows, less the header. */
  get planStepRows(): Locator {
    return this.planSteps.getByRole('row').filter({ hasNot: this.page.getByRole('columnheader') })
  }

  get confirmButton(): Locator {
    return this.root.getByRole('button', { name: 'Unpublish model', exact: true })
  }

  /** Everything the dialog shows, for checking that it names no other model. */
  text(): Promise<string> {
    return this.root.innerText()
  }

  /** Runs the reviewed plan. The response is the run MOSAIC started, or its refusal. */
  confirm(): Promise<Response> {
    return responseTo(this.page, this.#apiOrigin, 'POST', unpublishPath, () => this.confirmButton.click(), 120_000)
  }

  get stalePlan(): Locator {
    return this.root.getByText("MOSAIC didn't unpublish with the plan you reviewed", { exact: true })
  }

  // Run

  get finished(): Locator {
    return this.root.getByText('Unpublish finished', { exact: true })
  }

  get runSteps(): Locator {
    return this.root.getByRole('table', { name: 'Unpublish run steps' })
  }

  /** Closes the dialog: its secondary button reads Cancel while there is a plan to review, and Close otherwise. */
  async close(): Promise<void> {
    await this.root.getByRole('button', { name: /^(Cancel|Close)$/ }).last().click()
    await expect(this.root).toBeHidden()
  }
}
