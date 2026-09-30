import { type Locator, type Page, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { escapeRegExp, expectNoLoadError } from '../common.ts'

/** The portal's Usage & cost page, which reports only the signed-in person's grants. */
export class UsagePage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.user): Promise<UsagePage> {
    const usage = new UsagePage(await personas.page(personaKey, 'portal', '/usage'), targets)
    await usage.loaded()
    return usage
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'Usage & cost', exact: true })).toBeVisible()
    await expect(this.table.or(this.empty)).toBeVisible()
    await expectNoLoadError(this.page)
  }

  get table(): Locator {
    return this.page.getByRole('table', { name: 'Usage by resource' })
  }

  get empty(): Locator {
    return this.page.getByRole('heading', { name: 'No grants yet', exact: true })
  }

  /** The badge that labels simulated figures. The banner below it repeats the words, so this is the first. */
  get sampleData(): Locator {
    return this.page.getByText('Sample figures', { exact: true }).first()
  }

  /** The resources the table lists, one row header each. */
  get resources(): Locator {
    return this.table.getByRole('rowheader')
  }

  /** A resource's row. Its row header starts with the model's name, then any badges and the gateway. */
  row(displayName: string): Locator {
    return this.table.getByRole('row').filter({ has: this.page.getByRole('rowheader', { name: new RegExp(`^${escapeRegExp(displayName)}`) }) })
  }
}
