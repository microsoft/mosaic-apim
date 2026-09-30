import { modelLabel } from '../src/config.ts'
import { expect, requireWrites, test } from '../src/fixtures.ts'
import {
  applyReviewed,
  applyTimeoutMs,
  cleanupDisposable,
  expectPlanInScope,
  note,
  publishDisposable,
  publishedDisposable,
  requireDisposable,
  restoringAfter,
  scopeOf,
  suiteContext,
} from '../src/journeys.ts'
import { type ApiPlan, type ApiPublication, MosaicApi } from '../src/mosaic-api.ts'
import { expectOk, jsonOf } from '../src/pages/common.ts'
import { GatewayPage } from '../src/pages/console/gateways.ts'
import { ModelsPage } from '../src/pages/console/models.ts'
import { CatalogPage } from '../src/pages/portal/catalog.ts'
import { replanProblems } from '../src/plan-scope.ts'
import { isDisposableName, keptModels } from '../src/suite.ts'

/**
 * Publishing, journeys A7 to A9: the suite's gateway is managed, every model the manifest keeps is published and
 * shown in the catalog it's meant for. The @writes groups publish and re-plan the disposable model, and hide it from
 * the catalog and show it again. Kept publications are only read: re-planning one would apply everything saved on it.
 */

const resourceIds = (publication: ApiPublication) => publication.resources.map((resource) => resource.resourceId.toLowerCase()).sort()

test.describe('20 publish', { tag: '@console' }, () => {
  test('A7 the suite gateway is in manage mode and MOSAIC can write to it', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    expect(ctx.gateway.managementMode, `${ctx.gateway.name}'s management mode`).toBe('manage')
    expect(ctx.gateway.access.canWrite, `MOSAIC's write access to ${ctx.gateway.name}`).toBe(true)
    const gateway = await GatewayPage.open(personas, targets, ctx.gateway)
    await expect(gateway.mode('Manage')).toBeChecked()
    await expect(gateway.modeExplanation).toContainText('This gateway is in manage mode. MOSAIC can publish models to it')
    await expect(gateway.cannotManage).toHaveCount(0)
    await expect(gateway.refusesToPublish).toHaveCount(0)

    // Without write access, Manage is refused: live, only a gateway MOSAIC can't write to shows it.
    const unwritable = (await ctx.api.gateways()).filter((candidate) => candidate.id !== ctx.gateway.id && !candidate.access.canWrite)
    if (unwritable.length === 0) note('A7', 'Every other gateway grants MOSAIC write access, so the refusal is covered only by unit tests.')
    for (const other of unwritable) {
      const page = await GatewayPage.open(personas, targets, other)
      if (other.managementMode === 'observe') {
        await expect.soft(page.mode('Manage')).toBeDisabled()
        await expect.soft(page.cannotManage).toBeVisible()
      } else {
        await expect.soft(page.refusesToPublish).toBeVisible()
      }
    }
  })

  test('A8 every model the manifest keeps is published on the suite gateway', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const all = await ctx.api.publications()
    const models = await ModelsPage.open(personas, targets)
    const gateway = await GatewayPage.open(personas, targets, ctx.gateway)
    for (const model of keptModels(targets)) {
      const label = modelLabel(model)
      const publication = await ctx.api.publicationOf(model, ctx.gateway.id, all)
      expect.soft(publication?.status, `${label}'s publication on ${ctx.gateway.name}`).toBe('published')
      if (!publication) continue
      await expect.soft(models.publishedRow(publication, ctx.gateway.name)).toContainText('Published')
      await expect.soft(gateway.publishedRow(publication.displayName, publication.apiPath)).toHaveCount(1)
    }
  })

  test('A9 catalog visibility decides who can find each published model', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const catalog = await (await MosaicApi.as(personas, targets, targets.roles.user)).portalCatalog()
    const listed = new Set(catalog.map((entry) => entry.id))
    const models = await ModelsPage.open(personas, targets)
    const portal = await CatalogPage.open(personas, targets)
    for (const model of keptModels(targets)) {
      const label = modelLabel(model)
      const publication = await ctx.api.publicationOf(model, ctx.gateway.id)
      if (publication?.status !== 'published') continue
      const modelApi = await ctx.api.modelApiOf(publication)
      expect.soft(modelApi, `a model API for ${label}`).toBeDefined()
      if (!modelApi) continue
      await expect.soft(models.visibility(modelApi.displayName)).toHaveValue(modelApi.visibility)
      if (modelApi.visibility === 'catalog') {
        expect.soft(listed.has(modelApi.id), `${targets.roles.user}'s catalog to list ${label}`).toBe(true)
        await expect.soft(portal.card(modelApi.displayName).first()).toBeVisible()
      } else {
        expect.soft(listed.has(modelApi.id), `${targets.roles.user}'s catalog to leave out ${label}, which is private`).toBe(false)
        if (!catalog.some((entry) => entry.displayName === modelApi.displayName)) {
          await expect.soft(portal.card(modelApi.displayName)).toHaveCount(0)
        }
      }
    }
  })

  test.describe('A8 publishing the disposable model', { tag: '@writes' }, () => {
    requireWrites()
    test.describe.configure({ mode: 'serial', timeout: applyTimeoutMs })

    test('A8 publishing the disposable model applies every step', async ({ personas, targets }) => {
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      // Start from nothing, so that the first plan can only create.
      for (const line of await cleanupDisposable(ctx, disposable)) note('reset', line)
      const publication = await publishDisposable(ctx, disposable)
      const gateway = await GatewayPage.open(personas, targets, ctx.gateway)
      await expect(gateway.publishedRow(publication.displayName, publication.apiPath)).toHaveCount(1)
    })

    test('A8 re-planning the unchanged disposable publication creates and deletes nothing', async ({ personas, targets }) => {
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      const label = modelLabel(disposable)
      const before = await ctx.api.publicationOf(disposable, ctx.gateway.id)
      if (!before || before.status !== 'published' || !isDisposableName(before.displayName)) {
        throw new Error(`The suite's publication of ${label} isn't published, so there is nothing to re-plan.`)
      }
      const models = await ModelsPage.open(personas, targets)
      const { dialog, plan: response } = await models.replan(models.publishedRow(before, ctx.gateway.name))
      const plan = await jsonOf<ApiPlan>(response, `Re-planning ${label}`)
      const what = `the re-plan of ${label}`
      await expectPlanInScope(dialog, plan, await scopeOf(ctx, before.id), replanProblems(plan), what)
      if (plan.steps.length > 0 && plan.steps.every((step) => step.action === 'noChange')) {
        await expect(dialog.nothingToApply).toBeVisible()
        await dialog.close()
      } else {
        await applyReviewed(ctx, dialog, before.id, what, false)
      }
      const after = await ctx.api.publication(before.id)
      expect(after.status, `${label}'s status after the re-plan`).toBe('published')
      expect(resourceIds(after), `the API Management resources ${label} owns after the re-plan`).toEqual(resourceIds(before))
    })
  })

  test.describe('A9 hiding the disposable model', { tag: '@writes' }, () => {
    requireWrites()

    test('A9 a private model leaves the catalog and comes back when it is discoverable again', async ({ personas, targets }) => {
      test.setTimeout(applyTimeoutMs)
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      const label = modelLabel(disposable)
      const { modelApi } = await publishedDisposable(ctx, disposable)
      const requester = await MosaicApi.as(personas, targets, disposable.requester)
      const listed = async () => (await requester.portalCatalog()).some((entry) => entry.id === modelApi.id)
      const models = await ModelsPage.open(personas, targets)
      if (modelApi.visibility === 'private') {
        await expectOk(await models.setVisibility(modelApi.displayName, 'catalog'), `Listing ${label} in the catalog`)
        note('reset', `${label} was private, so the suite made it discoverable before hiding it.`)
      }
      await expect.poll(listed, { message: `${disposable.requester}'s catalog to list ${label}`, timeout: 60_000 }).toBe(true)

      await restoringAfter(
        async () => {
          await expectOk(await models.setVisibility(modelApi.displayName, 'private'), `Hiding ${label} from the catalog`)
          await expect.poll(listed, { message: `${disposable.requester}'s catalog to leave out ${label}`, timeout: 60_000 }).toBe(false)
          const catalog = await CatalogPage.open(personas, targets, disposable.requester)
          await expect(catalog.card(modelApi.displayName)).toHaveCount(0)
        },
        async () => {
          await models.reload()
          if ((await models.visibility(modelApi.displayName).inputValue()) !== 'catalog') {
            await expectOk(await models.setVisibility(modelApi.displayName, 'catalog'), `Listing ${label} in the catalog again`)
          }
        },
        `Listing ${label} in the catalog again`,
      )

      await expect.poll(listed, { message: `${disposable.requester}'s catalog to list ${label} again`, timeout: 60_000 }).toBe(true)
      const catalog = await CatalogPage.open(personas, targets, disposable.requester)
      await expect(catalog.card(modelApi.displayName)).toHaveCount(1)
    })
  })
})
