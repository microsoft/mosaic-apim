import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { expectNoLoadError, field, responseTo } from '../common.ts'

export type IdentityTab = 'users' | 'agents' | 'applications' | 'workloads' | 'security-groups'

const tabs: Record<IdentityTab, { title: string; filter: string; add: string }> = {
  users: { title: 'People', filter: 'Filter by label or object ID', add: 'Add person' },
  agents: { title: 'Agents', filter: 'Filter by label, detail, object ID, or kind', add: 'Add agent' },
  applications: { title: 'Applications & managed identities', filter: 'Filter by label, detail, object ID, or kind', add: 'Add application or managed identity' },
  workloads: { title: 'Applications & managed identities', filter: 'Filter by label, detail, object ID, or kind', add: 'Add application or managed identity' },
  'security-groups': { title: 'Entra security groups', filter: 'Filter by label, detail, object ID, or kind', add: 'Add security group' },
}

/** The Identity tab that lists a principal of this kind. */
export function identityTabFor(kind: string): IdentityTab {
  if (kind === 'user') return 'users'
  if (kind === 'agentIdentity' || kind === 'agentUser') return 'agents'
  if (kind === 'securityGroup') return 'security-groups'
  return 'applications'
}

/** The console's Identity page, on one of its principal tabs. */
export class IdentityPage {
  readonly page: Page
  readonly targets: Targets
  readonly tab: IdentityTab

  constructor(page: Page, targets: Targets, tab: IdentityTab) {
    this.page = page
    this.targets = targets
    this.tab = tab === 'workloads' ? 'applications' : tab
  }

  static async open(personas: PersonaPool, targets: Targets, tab: IdentityTab, personaKey = targets.roles.admin): Promise<IdentityPage> {
    const activeTab = tab === 'workloads' ? 'applications' : tab
    const identity = new IdentityPage(await personas.page(personaKey, 'web', `/identity?tab=${activeTab}`), targets, activeTab)
    await identity.loaded()
    return identity
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'Identity', exact: true })).toBeVisible()
    const selector = this.page.getByLabel('Identity section', { exact: true })
    if (await selector.isVisible()) {
      await expect(selector).toHaveValue(this.tab)
    } else {
      await expect(this.page.getByRole('navigation', { name: 'Identity sections' }).locator('[aria-current="page"]')).toBeVisible()
    }
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

  /** Filtering by the complete object ID identifies its native selection button, even when directory detail is shown instead. */
  async row(objectId: string): Promise<Locator> {
    await this.filter.fill(objectId)
    return this.page.getByRole('list', { name: tabs[this.tab].title, exact: true }).getByRole('button')
  }

  /** Directory verification is separate from how the MOSAIC reference was entered. */
  provenance(row: Locator): Locator {
    return row.getByText(/^(Verified in Entra|Not directory-verified)$/)
  }

  /** Retains the public method while opening the workspace's deliberate manual fallback. */
  async openAddDialog(): Promise<AddPrincipalDialog> {
    await this.page.getByRole('button', { name: tabs[this.tab].add, exact: true }).click()
    const dialog = new AddPrincipalDialog(this.page, this.targets)
    await expect(dialog.root).toBeVisible()
    const manual = dialog.root.getByRole('button', { name: 'Use manual entry', exact: true })
    await expect(manual).toBeVisible()
    await manual.click()
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
    this.root = page.getByRole('region', { name: /^Add / })
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
    const discard = this.page.getByRole('dialog', { name: 'Discard unsaved changes?', exact: true })
    if (await discard.isVisible()) await discard.getByRole('button', { name: 'Discard changes', exact: true }).click()
    await expect(this.root).toBeHidden()
  }
}
