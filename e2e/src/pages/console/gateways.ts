import { type Locator, type Page, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { expectDataLoaded, expectNoLoadError, pageDataTimeoutMs } from '../common.ts'

export type ManagementMode = 'Observe' | 'Manage'

/** A gateway's detail page in the console, on its Overview tab. */
export class GatewayPage {
  readonly page: Page
  readonly targets: Targets
  readonly name: string

  constructor(page: Page, targets: Targets, name: string) {
    this.page = page
    this.targets = targets
    this.name = name
  }

  static async open(
    personas: PersonaPool,
    targets: Targets,
    gateway: { id: string; name: string },
    personaKey = targets.roles.admin,
  ): Promise<GatewayPage> {
    const page = await personas.page(personaKey, 'web', `/gateways/${encodeURIComponent(gateway.id)}`)
    const detail = new GatewayPage(page, targets, gateway.name)
    await detail.loaded()
    return detail
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: this.name, exact: true })).toBeVisible({ timeout: pageDataTimeoutMs })
    await expect(this.modeHeading).toBeVisible()
    await expectDataLoaded(this.page, ['Loading gateway', 'Loading published models'])
    await expectNoLoadError(this.page)
  }

  /** The header's description, such as "contoso-apim · Manage mode". */
  get description(): Locator {
    return this.page.getByText(/ · (Observe|Manage) mode$/).first()
  }

  get modeHeading(): Locator {
    return this.page.getByRole('heading', { level: 2, name: 'Management mode', exact: true })
  }

  get modes(): Locator {
    return this.page.getByRole('radiogroup', { name: 'Management mode', exact: true })
  }

  mode(name: ManagementMode): Locator {
    return this.modes.getByRole('radio', { name, exact: true })
  }

  /** What the mode means for this gateway, which also says whether MOSAIC can publish to it. */
  get modeExplanation(): Locator {
    return this.page.getByText(/^This gateway is in (manage|observe) mode\./)
  }

  /** Shown, with the role MOSAIC's identity needs, when MOSAIC can't confirm write access to an observed gateway. */
  get cannotManage(): Locator {
    return this.page.getByText('MOSAIC can’t manage this gateway yet', { exact: true })
  }

  /** Shown instead of cannotManage when the gateway is already in manage mode but write access isn't confirmed. */
  get refusesToPublish(): Locator {
    return this.page.getByText('MOSAIC refuses to publish to this gateway', { exact: true })
  }

  get publishedModels(): Locator {
    return this.page.getByRole('table', { name: 'Gateway published models' })
  }

  get nothingPublished(): Locator {
    return this.page.getByRole('heading', { name: 'No models published into this gateway', exact: true })
  }

  /** A row of the models MOSAIC published into this gateway, by its display name and API path. */
  publishedRow(displayName: string, apiPath: string): Locator {
    return this.publishedModels
      .getByRole('row')
      .filter({ has: this.page.getByRole('cell', { name: displayName, exact: true }) })
      .filter({ has: this.page.getByRole('cell', { name: `/${apiPath}`, exact: true }) })
  }
}
