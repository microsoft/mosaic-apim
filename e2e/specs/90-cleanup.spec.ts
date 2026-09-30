import { modelLabel } from '../src/config.ts'
import { expect, requireWrites, test } from '../src/fixtures.ts'
import { applyTimeoutMs, cleanupDisposable, note, requireDisposable, restoreSuiteGrants, skip, suiteContext } from '../src/journeys.ts'
import { MosaicApi } from '../src/mosaic-api.ts'
import { expectOk } from '../src/pages/common.ts'
import { EntitlementsPage } from '../src/pages/console/entitlements.ts'
import { ModelsPage } from '../src/pages/console/models.ts'
import { isDisposableName } from '../src/suite.ts'

/**
 * Puts back what the ordered suite changed, with journey A15 as the disposable model is unpublished. Afterwards
 * the disposable model is gone, and the grants the suite keeps on other models are disabled and revoked. Each test
 * stands alone and acts only on what the suite made, which it finds by the names, notes and justifications it
 * writes. So this spec also recovers the environment after a failed run when it runs on its own:
 * npx playwright test specs/90-cleanup.spec.ts
 */

test.describe('90 cleanup', { tag: ['@console', '@writes', '@cleanup'] }, () => {
  requireWrites()

  test("the suite's open access requests are denied", async ({ personas, targets }) => {
    const api = await MosaicApi.as(personas, targets, targets.roles.admin)
    const open = (await api.accessRequests('pending')).filter((request) => isDisposableName(request.justification))
    if (open.length === 0) {
      note('cleanup', 'The suite left no open access requests.')
      return
    }
    const entitlements = await EntitlementsPage.open(personas, targets)
    for (const request of open) {
      await expectOk(await entitlements.deny(entitlements.requestRow(request.justification ?? '')), "Denying one of the suite's open requests")
    }
    note('cleanup', `Denied ${open.length} open access request(s) the suite made.`)
  })

  test('A15 unpublishing the disposable model deletes only what MOSAIC created, then its records are removed', async ({ personas, targets }) => {
    test.setTimeout(applyTimeoutMs)
    const ctx = await suiteContext(personas, targets)
    const disposable = requireDisposable(targets)
    const did = await cleanupDisposable(ctx, disposable)
    if (did.length === 0) skip(`MOSAIC has no publication or records of ${modelLabel(disposable)}, so there was nothing to unpublish.`)
    for (const line of did) note('A15', line)
  })

  test('the throwaway grants the suite added are revoked', async ({ personas, targets }) => {
    test.setTimeout(applyTimeoutMs)
    const ctx = await suiteContext(personas, targets)
    const did = await restoreSuiteGrants(ctx)
    for (const line of did) note('cleanup', line)
    if (did.length === 0) note('cleanup', 'Every grant the suite added is already disabled and revoked.')
  })

  test('endpoint registrations the suite made by mistake are removed', async ({ personas, targets }) => {
    const api = await MosaicApi.as(personas, targets, targets.roles.admin)
    const left = (await api.endpoints()).filter((endpoint) => isDisposableName(endpoint.name))
    if (left.length === 0) {
      note('cleanup', 'The suite left no endpoint registrations.')
      return
    }
    const models = await ModelsPage.open(personas, targets)
    for (const endpoint of left) {
      await expectOk(await models.removeEndpoint(endpoint.name), 'Removing an endpoint registration the suite made')
      await models.reload()
    }
    note('cleanup', `Removed ${left.length} endpoint registration(s) the suite made. Azure wasn't changed.`)
  })

  test('no identity the suite added is left in MOSAIC', async ({ personas, targets }) => {
    const api = await MosaicApi.as(personas, targets, targets.roles.admin)
    const left = (await api.principals()).filter((principal) => isDisposableName(principal.label))
    // The suite never deletes an identity: one it added by mistake shares its object ID with a real person's.
    expect(left.map((principal) => principal.label), "identities the suite added, to delete by hand on the Identity page").toEqual([])
  })
})
