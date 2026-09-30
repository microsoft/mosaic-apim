import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { SuiteLimits, Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { banner, exactly, expectNoLoadError, field, noStore, responseTo, secretKept, secretLength } from '../common.ts'
import { PublishDialog } from './publish-dialog.ts'

const publicationPath = /^\/api\/v1\/publications\/[^/]+$/
const planPath = /^\/api\/v1\/publications\/[^/]+\/plan$/
const entitlementPath = /^\/api\/v1\/entitlements\/[^/]+$/

export type KeySlot = 'primary' | 'secondary'

/** The runtime badge the grants table shows for each state MOSAIC reports. */
export const grantBadges = {
  pending: 'Saved, not applied',
  applying: 'Applying — not confirmed',
  applied: 'Applied',
  revocationPending: 'Revocation pending',
  revoked: 'Revoked in last apply',
  failed: 'Apply failed',
  unknown: 'Runtime unknown',
} as const

export type GrantBadge = keyof typeof grantBadges

/** Fills a grant's limits exactly: a limit the manifest leaves out is cleared, and no quota is ever set. */
async function fillLimits(scope: Locator, limits: SuiteLimits): Promise<void> {
  await field(scope, 'Tokens per minute').fill(limits.tokensPerMinute === undefined ? '' : String(limits.tokensPerMinute))
  await field(scope, 'Token quota').fill('')
  await field(scope, 'Calls').fill(limits.calls === undefined ? '' : String(limits.calls))
  await field(scope, 'Per how many seconds').fill(String(limits.perSeconds ?? 60))
}

/** The console's Entitlements page: governed model access, the grants table and the access requests. */
export class EntitlementsPage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.admin): Promise<EntitlementsPage> {
    const entitlements = new EntitlementsPage(await personas.page(personaKey, 'web', '/entitlements'), targets)
    await entitlements.loaded()
    return entitlements
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { name: 'Entitlements', exact: true })).toBeVisible()
    await expect(this.grants.or(this.page.getByText('Nothing has been granted yet', { exact: true }))).toBeVisible()
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

  // Governed model access

  get governed(): Locator {
    return this.page.locator('#governed-model-access')
  }

  get publishedModel(): Locator {
    return field(this.governed, 'Published model')
  }

  async selectPublication(publicationId: string, displayName: string): Promise<void> {
    await this.publishedModel.selectOption(publicationId)
    await expect(this.governed.getByRole('heading', { name: displayName, exact: true })).toBeVisible()
  }

  /** "Access: applied" and the like, or "Legacy publication" before the model is opted in. */
  get accessBadge(): Locator {
    return this.governed.getByText(/^(Access: [A-Za-z]+|Legacy publication)$/)
  }

  method(name: 'Dedicated subscription keys' | 'Microsoft Entra bearer tokens'): Locator {
    return this.governed.getByRole('switch', { name, exact: true })
  }

  get savedMethods(): Locator {
    return this.governed.getByText(/^Saved desired methods: /)
  }

  get appliedMethods(): Locator {
    return this.governed.getByText(/^Last applied methods: /)
  }

  button(name: 'Save access settings' | 'Add direct grant' | 'Review model changes' | 'Link published model'): Locator {
    return this.governed.getByRole('button', { name, exact: true })
  }

  saveAccessSettings(): Promise<Response> {
    return this.#respond('PATCH', publicationPath, () => this.button('Save access settings').click())
  }

  async addDirectGrant(): Promise<AddEntitlementDialog> {
    await this.button('Add direct grant').click()
    const dialog = new AddEntitlementDialog(this.page, this.targets)
    await expect(dialog.root).toBeVisible()
    return dialog
  }

  /**
   * Plans the selected model's access and opens the review. The response is the plan. The console opens the
   * review only when the plan has a model-wide access review, and shows an error otherwise, so a plan without
   * one fails here, by name, rather than as a dialog that never opens.
   */
  async reviewModelChanges(): Promise<{ dialog: PublishDialog; plan: Response }> {
    const plan = await this.#respond('POST', planPath, () => this.button('Review model changes').click(), 300_000)
    const dialog = new PublishDialog(this.page, this.targets, 'Review model access')
    if (plan.ok()) {
      const body = (await plan.json()) as { accessSnapshot?: unknown }
      if (!body.accessSnapshot) {
        throw new Error("MOSAIC's plan has no model-wide access review, so the console opened no review and nothing can be applied from it.")
      }
      await dialog.visible()
    }
    return { dialog, plan }
  }

  // Grants

  get grants(): Locator {
    return this.page.getByRole('table', { name: 'Entitlements' })
  }

  grantRow(entitlementId: string): Locator {
    return this.page.locator(`[id="grant-${entitlementId}"]`)
  }

  /** The grant's runtime badge for a state MOSAIC reports, such as "Applied" for applied. */
  grantState(entitlementId: string, state: GrantBadge): Locator {
    return this.grantRow(entitlementId).getByText(grantBadges[state], { exact: true })
  }

  /** Revokes a grant MOSAIC applies, which saves disabled intent until the model's plan is applied. */
  disable(entitlementId: string): Promise<Response> {
    return this.#respond('PATCH', entitlementPath, () =>
      this.grantRow(entitlementId).getByRole('button', { name: 'Revoke', exact: true }).click(),
    )
  }

  /** Revokes a grant MOSAIC no longer applies, such as one whose model was unpublished, which removes it. */
  remove(entitlementId: string): Promise<Response> {
    return this.#respond('DELETE', entitlementPath, () =>
      this.grantRow(entitlementId).getByRole('button', { name: 'Revoke', exact: true }).click(),
    )
  }

  reEnable(entitlementId: string): Promise<Response> {
    return this.#respond('PATCH', entitlementPath, () =>
      this.grantRow(entitlementId).getByRole('button', { name: 'Re-enable', exact: true }).click(),
    )
  }

  async connectionInfo(entitlementId: string): Promise<ConnectionDialog> {
    await this.grantRow(entitlementId).getByRole('button', { name: 'Connection info', exact: true }).click()
    const dialog = new ConnectionDialog(this.page, this.targets)
    await expect(dialog.root).toBeVisible()
    return dialog
  }

  // Access requests

  get requests(): Locator {
    return this.page.getByRole('table', { name: 'Pending access requests' })
  }

  /** A pending request, by the justification the requester wrote. */
  requestRow(justification: string): Locator {
    return this.requests.getByRole('row').filter({ has: this.page.getByText(justification, { exact: true }) })
  }

  async approve(row: Locator): Promise<ApproveRequestDialog> {
    await row.getByRole('button', { name: 'Approve', exact: true }).click()
    const dialog = new ApproveRequestDialog(this.page, this.targets)
    await expect(dialog.root).toBeVisible()
    return dialog
  }

  /** Denies a request at once: the console asks for no confirmation. */
  deny(row: Locator): Promise<Response> {
    return this.#respond('POST', /^\/api\/v1\/access-requests\/[^/]+\/deny$/, () =>
      row.getByRole('button', { name: 'Deny', exact: true }).click(),
    )
  }
}

export class AddEntitlementDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.root = page.getByRole('dialog', { name: 'Add entitlement' })
    this.#apiOrigin = targets.origins.api
  }

  /** Grants a principal the model the dialog opened for, with exactly these limits and a note. */
  async grant(principalId: string, limits: SuiteLimits, notes: string): Promise<Response> {
    await field(this.root, 'Subject').selectOption(principalId)
    await fillLimits(this.root, limits)
    await field(this.root, 'Notes').fill(notes)
    return responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/entitlements$/, () =>
      this.root.getByRole('button', { name: 'Grant access', exact: true }).click(),
    )
  }

  async cancel(): Promise<void> {
    await this.root.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(this.root).toBeHidden()
  }
}

export class ApproveRequestDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.root = page.getByRole('dialog', { name: 'Approve access request' })
    this.#apiOrigin = targets.origins.api
  }

  get approveButton(): Locator {
    return this.root.getByRole('button', { name: 'Approve and create grant', exact: true })
  }

  /** Approves with exactly these limits. The limits the dialog prefills from the publication are replaced. */
  async approve(limits: SuiteLimits, note: string): Promise<Response> {
    await fillLimits(this.root, limits)
    await field(this.root, 'Decision note').fill(note)
    return responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/access-requests\/[^/]+\/approve$/, () => this.approveButton.click())
  }

  async cancel(): Promise<void> {
    await this.root.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(this.root).toBeHidden()
  }
}

/** The console's connection dialog for a grant, where an admin hands off a workload's key. */
export class ConnectionDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.root = page.getByRole('dialog', { name: 'Model connection and keys' })
    this.#apiOrigin = targets.origins.api
  }

  /** A connection detail's value, such as "Deployment" or "Runtime state". */
  detail(term: string): Locator {
    return this.root.locator('dt', { hasText: exactly(term) }).locator('xpath=following-sibling::dd[1]')
  }

  static secretSelector(slot: KeySlot): string {
    return `pre[data-secret="true"][aria-label="Revealed ${slot} key"]`
  }

  /**
   * Reveals a key and reports what a person can see and what the browser kept, never the key itself: the
   * response's status and caching, how many characters the page shows, and where else the page holds it.
   */
  async reveal(slot: KeySlot): Promise<{ status: number; noStore: boolean; shownLength: number; keptIn: string[] }> {
    const response = await responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/entitlements\/[^/]+\/keys\/reveal$/, () =>
      this.root.getByRole('button', { name: `Reveal ${slot} key`, exact: true }).click(),
    )
    const selector = ConnectionDialog.secretSelector(slot)
    if (response.ok()) {
      await expect(this.page.locator(selector)).toBeVisible()
      await expect(this.root.getByText(`${slot === 'primary' ? 'Primary' : 'Secondary'} key revealed.`, { exact: true })).toBeAttached()
    }
    return {
      status: response.status(),
      noStore: await noStore(response),
      shownLength: await secretLength(this.page, selector),
      keptIn: await secretKept(this.page, selector),
    }
  }

  async close(): Promise<void> {
    await this.root.getByRole('button', { name: 'Close', exact: true }).last().click()
    await expect(this.root).toBeHidden()
    await expect(this.page.locator('[data-secret]')).toHaveCount(0)
  }
}
