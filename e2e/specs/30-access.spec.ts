import { modelLabel, sameModel } from '../src/config.ts'
import { expect, requireWrites, test } from '../src/fixtures.ts'
import {
  appliedMethods,
  applyTimeoutMs,
  note,
  publishedDisposable,
  requireDisposable,
  reviewAndApply,
  setMethods,
  skip,
  suiteContext,
  workloadGrant,
} from '../src/journeys.ts'
import { personaObjectId } from '../src/mosaic-api.ts'
import { EntitlementsPage } from '../src/pages/console/entitlements.ts'
import { IdentityPage, identityTabFor } from '../src/pages/console/identity.ts'
import { SettingsPage } from '../src/pages/console/settings.ts'
import { keptModels, methodsLabel, missingSuite, personaPrincipal, runtimeModels } from '../src/suite.ts'

/**
 * Identity and governed access, journeys A10 to A12 and A16: the people and the workload MOSAIC grants access to,
 * the access applied to the models the runtime journeys call, the workload's key handoff, and the environment rules.
 * The @writes groups try a duplicate identity that MOSAIC must refuse, and govern the disposable model.
 */

const sameId = (left: string, right: string) => left.toLowerCase() === right.toLowerCase()

test.describe('30 access', { tag: '@console' }, () => {
  test('A10 the user persona and the workload each have one identity in MOSAIC', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const user = targets.roles.user
    const objectId = await personaObjectId(personas, targets, user)
    const principals = await ctx.api.principals()
    expect(
      principals.filter((principal) => sameId(principal.objectId, objectId)).map((principal) => principal.kind),
      `MOSAIC's identities for ${user}`,
    ).toEqual(['user'])
    const people = await IdentityPage.open(personas, targets, 'users')
    const row = await people.row(objectId)
    await expect(row).toHaveCount(1)
    await expect(people.provenance(row)).toBeVisible()

    if (!targets.workload) {
      note('A10', "The manifest names no workload, so the workload's identity wasn't checked.")
      return
    }
    const workload = await ctx.api.workloadPrincipal()
    expect(workload, `a MOSAIC identity labelled ${targets.workload.displayName}`).toBeDefined()
    if (!workload) return
    const tab = await IdentityPage.open(personas, targets, identityTabFor(workload.kind))
    await expect(await tab.row(workload.objectId)).toHaveCount(1)
  })

  test('A11 the models the runtime journeys call are governed, and the console shows what was applied', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const runtime = ctx.suite.runtime ?? skip(missingSuite('runtime'))
    const user = await personaPrincipal(ctx.api, personas, targets, targets.roles.user)
    const entitlements = await EntitlementsPage.open(personas, targets)
    for (const model of runtimeModels(runtime)) {
      const label = modelLabel(model)
      const publication = await ctx.api.publicationOf(model, ctx.gateway.id)
      expect.soft(publication?.status, `${label}'s publication on ${ctx.gateway.name}`).toBe('published')
      if (publication?.status !== 'published') continue
      expect.soft(publication.governedAccess, `${label} to be governed`).toBeTruthy()
      expect.soft(publication.accessState, `${label}'s access state`).toBe('applied')
      await entitlements.selectPublication(publication.id, publication.displayName)
      const saved = publication.governedAccess ? methodsLabel(publication.governedAccess) : 'Legacy — not opted in'
      await expect.soft(entitlements.accessBadge).toHaveText(publication.governedAccess ? `Access: ${publication.accessState}` : 'Legacy publication')
      await expect.soft(entitlements.savedMethods).toHaveText(`Saved desired methods: ${saved}`)
      await expect.soft(entitlements.appliedMethods).toHaveText(`Last applied methods: ${methodsLabel(publication.appliedAccess?.settings)}`)

      // The user's grant shows the state MOSAIC last applied.
      const modelApi = await ctx.api.modelApiOf(publication)
      if (!user || !modelApi || !runtime.userGrants.some((grant) => sameModel(grant, model))) continue
      const [grant] = await ctx.api.grantsOf(user.id, modelApi.id)
      expect.soft(grant?.runtime?.status, `${targets.roles.user}'s grant on ${label}`).toBe('applied')
      if (grant?.runtime) await expect.soft(entitlements.grantState(grant.id, grant.runtime.status)).toBeVisible()
    }
  })

  test("A12 the workload's connection details and key handoff", async ({ personas, targets }) => {
    test.slow()
    const ctx = await suiteContext(personas, targets)
    const model = ctx.suite.runtime?.applicationGrant ?? skip(missingSuite('runtime.applicationGrant'))
    const held = await workloadGrant(ctx, model)
    const methods = appliedMethods(held.grant, held.publication)
    if (!methods?.keysEnabled) skip(`Subscription keys aren't applied on ${held.label}, so there is no key to hand off.`)
    expect.soft(held.grant.runtime?.status, `the workload's grant on ${held.label}`).toBe('applied')

    const entitlements = await EntitlementsPage.open(personas, targets)
    const dialog = await entitlements.connectionInfo(held.grant.id)
    await expect.soft(dialog.detail('Deployment')).toHaveText(held.publication.deploymentName)
    await expect.soft(dialog.detail('Runtime state')).toHaveText(held.grant.runtime?.status ?? 'unknown')
    await expect.soft(dialog.detail('Last applied methods')).toHaveText(methodsLabel(methods))
    const revealed = await dialog.reveal('primary')
    expect(revealed.status, "the reveal of the workload's primary key").toBe(200)
    expect(revealed.noStore, 'the reveal to forbid caching').toBe(true)
    expect(revealed.shownLength, 'the characters the dialog shows').toBeGreaterThan(0)
    expect(revealed.keptIn, 'where else the page keeps the key').toEqual([])
    await dialog.close()
  })

  test('A16 environments are listed, and an unclassified pairing warns without blocking', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const environments = await ctx.api.environments()
    expect(environments.filter((environment) => environment.builtIn).length, 'built-in environments').toBeGreaterThan(0)
    const settings = await SettingsPage.open(personas, targets)
    for (const environment of environments) {
      const row = settings.environment(environment.key)
      await expect.soft(row).toHaveCount(1)
      await expect.soft(settings.origin(row.first())).toHaveText(environment.builtIn ? 'Built-in' : 'Custom')
    }

    for (const model of keptModels(targets)) {
      const label = modelLabel(model)
      const offered = await ctx.api.publishableModel(model, ctx.gateway.id)
      if (!offered) {
        note('A16', `The publish dialog doesn't offer ${label} on ${ctx.gateway.name}, so its environment verdict wasn't checked.`)
        continue
      }
      const verdict = offered.environmentVerdict
      expect.soft(verdict?.level, `the environment verdict on ${label}`).not.toBe('blocked')
      if (verdict?.level === 'warning') expect.soft(verdict.reason ?? '', `why ${label} warns`).toMatch(/unclassified/i)
    }
  })

  test.describe('with writes', { tag: '@writes' }, () => {
    requireWrites()

    test('A10 MOSAIC refuses to add the same identity twice', async ({ personas, targets }) => {
      const objectId = await personaObjectId(personas, targets, targets.roles.user)
      const people = await IdentityPage.open(personas, targets, 'users')
      const dialog = await people.openAddDialog()
      const response = await dialog.addManually(objectId, 'user', 'E2E suite duplicate (A10)')
      if (response.ok()) {
        throw new Error(
          `MOSAIC added ${targets.roles.user} a second time (${response.status()}). Remove the entry labelled "E2E suite duplicate (A10)" on the Identity page's People tab.`,
        )
      }
      expect(response.status(), 'MOSAIC to refuse a second identity for the same object ID').toBe(409)
      await expect(dialog.refusal).toBeVisible()
      await dialog.cancel()
    })

    test('A11 governing the disposable model is reviewed, applied and shown', async ({ personas, targets }) => {
      test.setTimeout(applyTimeoutMs)
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      const label = modelLabel(disposable)
      const both = { keysEnabled: true, entraEnabled: true }
      const { publication } = await publishedDisposable(ctx, disposable)
      const entitlements = await EntitlementsPage.open(personas, targets)
      await entitlements.selectPublication(publication.id, publication.displayName)
      await setMethods(entitlements, both)
      const applied = await reviewAndApply(ctx, entitlements, publication, `access to ${label}`)
      expect(applied.governedAccess, `${label}'s saved methods`).toMatchObject(both)
      expect(applied.appliedAccess?.settings, `${label}'s applied methods`).toMatchObject(both)

      await entitlements.reload()
      await entitlements.selectPublication(publication.id, publication.displayName)
      await expect(entitlements.accessBadge).toHaveText('Access: applied')
      await expect(entitlements.savedMethods).toHaveText(`Saved desired methods: ${methodsLabel(both)}`)
      await expect(entitlements.appliedMethods).toHaveText(`Last applied methods: ${methodsLabel(both)}`)
    })
  })
})
