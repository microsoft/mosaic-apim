import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { expectNoLoadError, field, responseTo } from '../common.ts'

export type IdentityTab = 'users' | 'agents' | 'workloads'

const tabs: Record<IdentityTab, { title: string; filter: string; add: string }> = {
  users: { title: 'People', filter: 'Filter by label or object ID', add: 'Add person' },
  agents: { title: 'Agents', filter: 'Filter by label, detail, object ID, or kind', add: 'Add agent' },
  workloads: { title: 'Applications and security groups', filter: 'Filter by label, detail, object ID, or kind', add: 'Add identity' },
}

/** The Identity tab that lists a principal of this kind. */
export function identityTabFor(kind: string): IdentityTab {
  if (kind === 'user') return 'users'
  if (kind === 'agentIdentity' || kind === 'agentUser') return 'agents'
  return 'workloads'
}

/** The console's Identity page, on one of its principal tabs. */
export class IdentityPage {
  readonly page: Page
  readonly targets: Targets
  readonly tab: IdentityTab

  constructor(page: Page, targets: Targets, tab: IdentityTab) {
    this.page = page
    this.targets = targets
    this.tab = tab
  }

  static async open(personas: PersonaPool, targets: Targets, tab: IdentityTab, personaKey = targets.roles.admin): Promise<IdentityPage> {
    const identity = new IdentityPage(await personas.page(personaKey, 'web', `/identity?tab=${tab}`), targets, tab)
    await identity.loaded()
    return identity
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'Identity', exact: true })).toBeVisible()
    await expect(this.page.getByRole('tab', { name: tabs[this.tab].title, exact: true })).toHaveAttribute('aria-selected', 'true')
    await expect(this.shown).toBeVisible()
    await expectNoLoadError(this.page)
  }

  get filter(): Locator {
    return this.page.getByRole('textbox', { name: tabs[this.tab].filter, exact: true })
  }

  /** "3 of 12 shown". */
  get shown(): Locator {
    return this.page.getByText(/^\d+ of \d+ shown$/)
  }

  /** A principal's row, found by filtering the list to its object ID. */
  async row(objectId: string): Promise<Locator> {
    await this.filter.fill(objectId)
    return this.page.getByRole('row').filter({ has: this.page.locator('code', { hasText: objectId }) })
  }

  /** "Verified in Entra" or "Entered by hand". */
  provenance(row: Locator): Locator {
    return row.getByText(/^(Verified in Entra|Entered by hand)$/)
  }

  /** Opens the add dialog on its manual form, whether or not directory lookup is on. */
  async openAddDialog(): Promise<AddPrincipalDialog> {
    await this.page.getByRole('button', { name: tabs[this.tab].add, exact: true }).click()
    const dialog = new AddPrincipalDialog(this.page, this.targets)
    await expect(dialog.root).toBeVisible()
    const manual = dialog.root.getByRole('button', { name: 'Use manual entry', exact: true })
    if (await manual.isVisible()) await manual.click()
    await expect(field(dialog.root, 'Entra object ID')).toBeVisible()
    return dialog
  }
}

export class AddPrincipalDialog {
  readonly page: Page
  readonly root: Locator
  readonly #apiOrigin: string

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.root = page.getByRole('dialog', { name: /^Add / })
    this.#apiOrigin = targets.origins.api
  }

  /** Enters a principal by hand and saves it. The response says whether MOSAIC added it. */
  async addManually(objectId: string, kind: string, label: string): Promise<Response> {
    await field(this.root, 'Entra object ID').fill(objectId)
    await field(this.root, 'Principal type').selectOption(kind)
    await field(this.root, 'Local label').fill(label)
    return responseTo(this.page, this.#apiOrigin, 'POST', /^\/api\/v1\/principals$/, () =>
      this.root.getByRole('button', { name: 'Save', exact: true }).click(),
    )
  }

  get refusal(): Locator {
    return this.root.getByText('Unable to add principal', { exact: true })
  }

  async cancel(): Promise<void> {
    await this.root.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(this.root).toBeHidden()
  }
}
