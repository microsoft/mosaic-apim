import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import { apiPath } from '../../denials.ts'
import { expectOk, field } from '../common.ts'

const publicationsPath = /^\/api\/v1\/publications$/
const planPath = /^\/api\/v1\/publications\/[^/]+\/plan$/
const applyPath = /^\/api\/v1\/publications\/[^/]+\/apply$/

/**
 * The console's publish dialog. It publishes a model in four steps (choose, configure, review, apply), and it
 * is also where a re-plan or a governed-access review is read and applied.
 */
export class PublishDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets, title: string | RegExp) {
    this.page = page
    this.root = page.getByRole('dialog', { name: title })
    this.#apiOrigin = targets.origins.api
  }

  async visible(): Promise<void> {
    await expect(this.root).toBeVisible()
  }

  #response(method: string, path: RegExp, timeout: number): Promise<Response> {
    return this.page.waitForResponse(
      (candidate) => candidate.request().method() === method && path.test(apiPath(candidate.url(), this.#apiOrigin) ?? ''),
      { timeout },
    )
  }

  // Choose

  get gateway(): Locator {
    return this.root.getByRole('combobox', { name: 'Gateway', exact: true })
  }

  get publishableModels(): Locator {
    return this.root.getByRole('table', { name: 'Publishable models' })
  }

  /** A deployment's row. Two endpoints can have deployments with the same name, so the row is found by its path. */
  modelRow(suggestedApiPath: string): Locator {
    return this.publishableModels.getByRole('row').filter({ has: this.page.getByText(`/${suggestedApiPath}`, { exact: true }) })
  }

  async choose(gatewayId: string, deploymentName: string, suggestedApiPath: string): Promise<void> {
    await expect(this.root.getByText('Step 1 of 4', { exact: true })).toBeVisible()
    await this.gateway.selectOption(gatewayId)
    const row = this.modelRow(suggestedApiPath)
    await expect(row).toHaveCount(1)
    await row.getByRole('checkbox', { name: `Publish ${deploymentName}`, exact: true }).check()
    await this.root.getByRole('button', { name: 'Configure', exact: true }).click()
    await expect(this.root.getByText('Step 2 of 4', { exact: true })).toBeVisible()
  }

  // Configure

  field(label: string): Locator {
    return field(this.root, label)
  }

  /**
   * Saves the publication and plans it. The dialog creates the publication first; if MOSAIC refuses that, the
   * error names why instead of waiting for a plan that never comes.
   */
  async reviewPlan(): Promise<Response> {
    const created = this.#response('POST', publicationsPath, 120_000)
    const planned = this.#response('POST', planPath, 300_000)
    created.catch(() => undefined)
    planned.catch(() => undefined)
    await this.root.getByRole('button', { name: 'Review plan', exact: true }).click()
    await expectOk(await created, 'Saving the publication')
    return planned
  }

  // Review

  get planSteps(): Locator {
    return this.root.getByRole('table', { name: 'Publish plan steps' })
  }

  /** The plan's rows, less the header. */
  get planStepRows(): Locator {
    return this.planSteps.getByRole('row').filter({ hasNot: this.page.getByRole('columnheader') })
  }

  get accessReview(): Locator {
    return this.root.getByRole('region', { name: 'Model-wide access review' })
  }

  get nothingToApply(): Locator {
    return this.root.getByText('API Management already matches this publication. Nothing to apply.', { exact: true })
  }

  get applyButton(): Locator {
    return this.root.getByRole('button', { name: 'Apply plan', exact: true })
  }

  /** Everything the dialog shows, for checking that it names no other model. */
  text(): Promise<string> {
    return this.root.innerText()
  }

  /** Applies the reviewed plan. The response is the run MOSAIC started, or its refusal. */
  async apply(): Promise<Response> {
    const [applied] = await Promise.all([this.#response('POST', applyPath, 120_000), this.applyButton.click()])
    return applied
  }

  get stalePlan(): Locator {
    return this.root.getByText("MOSAIC didn't apply the plan you reviewed", { exact: true })
  }

  get runSteps(): Locator {
    return this.root.getByRole('table', { name: 'Publish run steps' })
  }

  get appliedMessage(): Locator {
    return this.root.getByText(
      'The service reports the plan applied. Allow for gateway propagation; a live model invocation has not been verified.',
      { exact: true },
    )
  }

  async close(): Promise<void> {
    await this.root.getByRole('button', { name: 'Close', exact: true }).first().click()
    await expect(this.root).toBeHidden()
  }
}
