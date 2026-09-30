import { type Locator, type Page, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { expectNoLoadError } from '../common.ts'

/** The console's Settings page, where the environment catalog lives. */
export class SettingsPage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.admin): Promise<SettingsPage> {
    const settings = new SettingsPage(await personas.page(personaKey, 'web', '/settings'), targets)
    await settings.loaded()
    return settings
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'Settings', exact: true })).toBeVisible()
    await expect(this.environments).toBeVisible()
    await expectNoLoadError(this.page)
  }

  get environments(): Locator {
    return this.page.getByRole('table', { name: 'Environments' })
  }

  /** An environment's row, by its key. */
  environment(key: string): Locator {
    return this.environments.getByRole('row').filter({ has: this.page.getByRole('cell', { name: key, exact: true }) })
  }

  /** "Built-in" for the environments MOSAIC ships with, "Custom" for the rest. */
  origin(row: Locator): Locator {
    return row.getByText(/^(Built-in|Custom)$/)
  }
}
