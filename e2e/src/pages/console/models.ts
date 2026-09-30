import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { banner, escapeRegExp, expectNoLoadError, field, responseTo } from '../common.ts'
import { PublishDialog } from './publish-dialog.ts'
import { UnpublishDialog } from './unpublish-dialog.ts'

/** The status badge the endpoints table shows for each status MOSAIC reports. */
export const endpointStatusLabels: Readonly<Record<string, string>> = {
  pending: 'Not checked',
  connected: 'Connected',
  degraded: 'Partial data',
  unauthorized: 'Access needed',
  unreachable: 'Unreachable',
}

/** The console's Models page: published models, imported model APIs, model endpoints and what MOSAIC found. */
export class ModelsPage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.admin): Promise<ModelsPage> {
    const models = new ModelsPage(await personas.page(personaKey, 'web', '/models'), targets)
    await models.loaded()
    return models
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { name: 'Models', exact: true })).toBeVisible()
    await expect(this.page.getByRole('heading', { name: 'Model endpoints', exact: true })).toBeVisible()
    await expectNoLoadError(this.page)
  }

  async reload(): Promise<void> {
    await this.page.reload()
    await this.loaded()
  }

  banner(text: string | RegExp): Locator {
    return banner(this.page, text)
  }

  #respond(method: string, path: RegExp, action: () => Promise<unknown>, timeout?: number): Promise<Response> {
    return responseTo(this.page, this.targets.origins.api, method, path, action, timeout)
  }

  // Published models

  get publishedModels(): Locator {
    return this.page.getByRole('table', { name: 'Published models' })
  }

  /** A publication's row: its display name and API path, and its gateway when two gateways publish the same path. */
  publishedRow(publication: { displayName: string; apiPath: string }, gatewayName?: string): Locator {
    let row = this.publishedModels
      .getByRole('row')
      .filter({ has: this.page.getByText(publication.displayName, { exact: true }) })
      .filter({ has: this.page.getByText(`/${publication.apiPath}`, { exact: true }) })
    if (gatewayName) row = row.filter({ has: this.page.getByRole('link', { name: gatewayName, exact: true }) })
    return row
  }

  async openPublishDialog(): Promise<PublishDialog> {
    await this.page.getByRole('button', { name: 'Publish a model', exact: true }).click()
    const dialog = new PublishDialog(this.page, this.targets, 'Publish a model')
    await dialog.visible()
    return dialog
  }

  /** Re-plans a publication; the dialog opens on the plan's review and the response is the plan. */
  async replan(row: Locator): Promise<{ dialog: PublishDialog; plan: Response }> {
    const plan = await this.#respond('POST', /^\/api\/v1\/publications\/[^/]+\/plan$/, () =>
      row.getByRole('button', { name: 'Re-plan', exact: true }).click(),
    )
    const dialog = new PublishDialog(this.page, this.targets, /^(Publish a model|Review model access)$/)
    await dialog.visible()
    return { dialog, plan }
  }

  /**
   * Opens a publication's unpublish review, which plans the unpublish as it opens and removes nothing. The response
   * is the plan, or MOSAIC's refusal to plan one.
   */
  async openUnpublish(row: Locator, displayName: string): Promise<{ dialog: UnpublishDialog; plan: Response }> {
    const plan = await this.#respond('POST', /^\/api\/v1\/publications\/[^/]+\/unpublish-plan$/, () =>
      row.getByRole('button', { name: 'Unpublish', exact: true }).click(),
    )
    const dialog = new UnpublishDialog(this.page, this.targets, displayName)
    await dialog.visible()
    return { dialog, plan }
  }

  /** Removes a publication record through its confirmation dialog. */
  async removePublication(row: Locator, displayName: string): Promise<Response> {
    await row.getByRole('button', { name: 'Remove', exact: true }).click()
    const dialog = this.page.getByRole('alertdialog', { name: `Remove ${displayName}?` })
    await expect(dialog).toBeVisible()
    const response = await this.#respond('DELETE', /^\/api\/v1\/publications\/[^/]+$/, () =>
      dialog.getByRole('button', { name: 'Remove publication', exact: true }).click(),
    )
    if (response.ok()) await expect(dialog).toBeHidden()
    return response
  }

  // Imported model APIs

  get importedModelApis(): Locator {
    return this.page.getByRole('table', { name: 'Imported model APIs' })
  }

  visibility(displayName: string): Locator {
    return this.page.getByRole('combobox', { name: `Catalog visibility for ${displayName}`, exact: true })
  }

  modelApiRow(displayName: string): Locator {
    return this.importedModelApis.getByRole('row').filter({ has: this.visibility(displayName) })
  }

  async setVisibility(displayName: string, visibility: 'catalog' | 'private'): Promise<Response> {
    return this.#respond('PATCH', /^\/api\/v1\/model-apis\/[^/]+\/catalog$/, () => this.visibility(displayName).selectOption(visibility))
  }

  /** Stops governing a model API at once: the console asks for no confirmation. */
  removeModelApi(displayName: string): Promise<Response> {
    return this.#respond('DELETE', /^\/api\/v1\/model-apis\/[^/]+$/, () =>
      this.modelApiRow(displayName).getByRole('button', { name: 'Remove', exact: true }).click(),
    )
  }

  // Model endpoints

  get endpoints(): Locator {
    return this.page.getByRole('table', { name: 'Registered model endpoints' })
  }

  endpointRow(name: string): Locator {
    return this.endpoints.getByRole('row').filter({ has: this.page.getByRole('button', { name, exact: true }) })
  }

  /** Selects an endpoint, which shows its access and its models below the table. */
  async selectEndpoint(name: string): Promise<void> {
    await this.endpointRow(name).getByRole('button', { name, exact: true }).click()
    await expect(this.page.getByRole('heading', { name: `Models on ${name}`, exact: true })).toBeVisible()
  }

  /** Checks access again. The page shows no banner, so the response is the result. */
  checkAccess(name: string): Promise<Response> {
    return this.#respond('POST', /^\/api\/v1\/model-endpoints\/[^/]+\/preflight$/, () =>
      this.endpointRow(name).getByRole('button', { name: 'Check access', exact: true }).click(),
    )
  }

  /** Syncs an endpoint's models. The page shows no banner, so the response is the result. */
  sync(name: string): Promise<Response> {
    return this.#respond(
      'POST',
      /^\/api\/v1\/model-endpoints\/[^/]+\/sync$/,
      () => this.endpointRow(name).getByRole('button', { name: 'Sync models', exact: true }).click(),
      180_000,
    )
  }

  /** Removes what MOSAIC stored about an endpoint, through its confirmation dialog. Azure is never changed. */
  async removeEndpoint(name: string): Promise<Response> {
    await this.endpointRow(name).getByRole('button', { name: 'Remove', exact: true }).click()
    const dialog = this.page.getByRole('alertdialog', { name: `Remove ${name}?` })
    await expect(dialog).toBeVisible()
    const response = await this.#respond('DELETE', /^\/api\/v1\/model-endpoints\/[^/]+$/, () =>
      dialog.getByRole('button', { name: 'Remove endpoint', exact: true }).click(),
    )
    if (response.ok()) await expect(dialog).toBeHidden()
    return response
  }

  readVerdict(canRead: boolean): Locator {
    return this.page.getByText(canRead ? 'MOSAIC can read this endpoint' : 'MOSAIC cannot read this endpoint', { exact: true })
  }

  /** The gateway's verdict on calling the selected endpoint: "can invoke", "cannot invoke" or "not confirmed". */
  gatewayVerdict(gatewayName: string): Locator {
    return this.page.getByText(new RegExp(`^${escapeRegExp(gatewayName)}: (can invoke|cannot invoke|not confirmed)$`))
  }

  get environmentVerdicts(): Locator {
    return this.page.getByText(/^Environment rules: environment (allowed|warning|blocked|not evaluated)$/)
  }

  get deployments(): Locator {
    return this.page.getByRole('table', { name: 'Discovered model deployments' })
  }

  deploymentRow(deploymentName: string): Locator {
    return this.deployments.getByRole('row').filter({ has: this.page.getByText(deploymentName, { exact: true }) })
  }

  // What MOSAIC found

  get discovery(): Locator {
    return this.page.getByRole('heading', { name: 'Endpoints MOSAIC found', exact: true })
  }

  get unscannable(): Locator {
    return this.page.getByRole('heading', { name: 'Subscriptions MOSAIC could not scan', exact: true })
  }

  get partlyReadable(): Locator {
    return this.page.getByRole('heading', { name: /^MOSAIC can read only part of / })
  }

  /** Shown when the scan found no subscription it could see or list, with the command that fixes that. */
  get cannotScan(): Locator {
    return this.page.getByRole('heading', { name: /^MOSAIC (can't see any subscriptions|couldn't list subscriptions)$/ })
  }

  get copyCommand(): Locator {
    return this.page.getByRole('button', { name: 'Copy command' })
  }

  // Registering an endpoint

  async openRegisterDialog(): Promise<RegisterEndpointDialog> {
    await this.page.getByRole('button', { name: 'Register endpoint', exact: true }).click()
    const dialog = new RegisterEndpointDialog(this.page, this.targets)
    await expect(dialog.root).toBeVisible()
    return dialog
  }
}

export class RegisterEndpointDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.root = page.getByRole('dialog', { name: 'Register model endpoint' })
    this.#apiOrigin = targets.origins.api
  }

  /** The required environment. The dialog picks the catalog's first environment once the catalog loads. */
  get environment(): Locator {
    return this.root.getByRole('combobox', { name: 'Environment', exact: true })
  }

  /**
   * Registers an Azure AI resource by its ID, in the environment the dialog picked. The dialog sends nothing
   * while the environment reads Unclassified, so this waits for it. The response says whether MOSAIC accepted it.
   */
  async registerAzureResource(resourceId: string, displayName: string): Promise<Response> {
    await this.root.getByRole('tab', { name: 'Azure AI', exact: true }).click()
    await field(this.root, 'Azure resource ID').fill(resourceId)
    await field(this.root, 'Display name').fill(displayName)
    await expect(this.environment, 'the register dialog to pick an environment').not.toHaveValue('Unclassified')
    return responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/model-endpoints$/, () =>
      this.root.getByRole('button', { name: 'Register', exact: true }).click(),
    )
  }

  get refusal(): Locator {
    return this.root.getByText("MOSAIC didn't register this endpoint", { exact: true })
  }

  async cancel(): Promise<void> {
    await this.root.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(this.root).toBeHidden()
  }
}
