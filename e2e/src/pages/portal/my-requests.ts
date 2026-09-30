import { type Locator, type Page, type Response, expect } from '@playwright/test'
import type { Targets } from '../../config.ts'
import type { PersonaPool } from '../../fixtures.ts'
import { expectNoLoadError, responseTo } from '../common.ts'

export type RequestState = 'Pending' | 'Approved' | 'Denied' | 'Withdrawn'

/** The portal's My requests page: the access requests a person opened and what became of them. */
export class MyRequestsPage {
  readonly page: Page
  readonly targets: Targets

  constructor(page: Page, targets: Targets) {
    this.page = page
    this.targets = targets
  }

  static async open(personas: PersonaPool, targets: Targets, personaKey = targets.roles.user): Promise<MyRequestsPage> {
    const requests = new MyRequestsPage(await personas.page(personaKey, 'portal', '/requests'), targets)
    await requests.loaded()
    return requests
  }

  async loaded(): Promise<void> {
    await expect(this.page.getByRole('heading', { level: 1, name: 'My requests', exact: true })).toBeVisible()
    await expect(this.cards.first().or(this.empty)).toBeVisible()
    await expectNoLoadError(this.page)
  }

  async reload(): Promise<void> {
    await this.page.reload()
    await this.loaded()
  }

  get cards(): Locator {
    return this.page.locator('.request-card')
  }

  get empty(): Locator {
    return this.page.getByRole('heading', { name: 'No requests opened', exact: true })
  }

  /** A request, by the justification its requester wrote, which the suite makes unique to each run. */
  request(justification: string): Locator {
    return this.cards.filter({ has: this.page.getByText(justification, { exact: true }) })
  }

  state(card: Locator, state: RequestState): Locator {
    return card.getByText(state, { exact: true })
  }

  /** What an approved request says about its grant until an administrator applies it. */
  approvedNote(card: Locator): Locator {
    return card.getByText('Approval created your grant. It may not work until an administrator applies it.', { exact: false })
  }

  withdraw(card: Locator): Promise<Response> {
    return responseTo(this.page, this.targets.origins.api, 'POST', /^\/api\/v1\/portal\/access-requests\/[^/]+\/withdraw$/, () =>
      card.getByRole('button', { name: 'Withdraw', exact: true }).click(),
    )
  }
}
