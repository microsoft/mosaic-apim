import { expect, requireWrites, test } from '../src/fixtures.ts'
import { note, skip, suiteContext } from '../src/journeys.ts'
import { type ApiEndpoint, type ApiSuggestions, MosaicApi } from '../src/mosaic-api.ts'
import { expectOk, jsonOf, responseTo } from '../src/pages/common.ts'
import { ModelsPage, endpointStatusLabels } from '../src/pages/console/models.ts'
import { disposablePrefix } from '../src/suite.ts'

/**
 * Model endpoints, journeys A2 to A6: what MOSAIC found in Azure, the manifest's endpoints and their models, and
 * whether the suite's gateway can call them. The first group only reads. The @writes group asks MOSAIC to check
 * access and sync models again, and tries a duplicate registration that MOSAIC must refuse. Nothing in Azure
 * changes.
 */

const lower = (value: string | null | undefined) => (value ?? '').toLowerCase()

/** The MOSAIC endpoint registered for a manifest endpoint. Anything but exactly one fails softly. */
async function registeredEndpoint(api: MosaicApi, key: string): Promise<ApiEndpoint | undefined> {
  const registered = await api.endpointsFor(key)
  expect.soft(registered.length, `MOSAIC to register ${key} exactly once`).toBe(1)
  return registered.length === 1 ? registered[0] : undefined
}

test.describe('10 endpoints', { tag: '@console' }, () => {
  test('A2 discovery lists the manifest endpoints and shows remediation', async ({ personas, targets }) => {
    const models = await ModelsPage.open(personas, targets)
    const found = await jsonOf<ApiSuggestions>(
      await responseTo(models.page, targets.origins.api, 'GET', /^\/api\/v1\/model-endpoints\/suggested$/, () => models.reload(), 120_000),
      'Listing the endpoints MOSAIC found',
    )

    const scanned = found.scanStatus === 'scanned'
    const suggested = new Map(found.suggestions.filter((item) => item.azureResourceId).map((item) => [lower(item.azureResourceId), item]))
    const fromSuggestions = Object.entries(targets.endpoints).filter(([, endpoint]) => endpoint.registerVia === 'suggestion')
    if (!scanned) {
      note('discovery', `MOSAIC's subscription scan reports ${found.scanStatus}.`)
      expect
        .soft(fromSuggestions.map(([key]) => key), `endpoints the manifest registers from a suggestion, while the scan reports ${found.scanStatus}`)
        .toEqual([])
    }
    for (const [key, endpoint] of Object.entries(targets.endpoints)) {
      const suggestion = [endpoint.resourceId, endpoint.projectResourceId]
        .map((id) => suggested.get(lower(id)))
        .find((item) => item !== undefined)
      if (endpoint.registerVia === 'suggestion' && scanned) {
        expect.soft(suggestion, `MOSAIC's scan to find ${key}`).toBeDefined()
        expect.soft(suggestion?.alreadyRegistered, `MOSAIC's scan to show ${key} as registered`).toBe(true)
      } else if (endpoint.registerVia === 'paste' && !suggestion) {
        note('discovery', `MOSAIC's scan didn't find ${key}, which the manifest registers by pasting its ID.`)
      }
    }

    // Each part of the discovery card shows exactly when MOSAIC's answer calls for it.
    const pending = found.suggestions.filter((item) => !item.alreadyRegistered).length
    await expect.soft(models.discovery).toHaveCount(pending > 0 || (scanned && found.subscriptionsScanned > 0) ? 1 : 0)
    await expect.soft(models.partlyReadable).toHaveCount(found.partialScans.length > 0 ? 1 : 0)
    await expect.soft(models.unscannable).toHaveCount(found.scanIssues.length > 0 ? 1 : 0)
    const cannotScan = found.scanStatus === 'noVisibleSubscriptions' || found.scanStatus === 'listFailed'
    await expect.soft(models.cannotScan).toHaveCount(cannotScan ? 1 : 0)
    const commands =
      [...found.partialScans, ...found.scanIssues].filter((scan) => scan.remediation).length +
      (cannotScan ? (found.scanRemediation ?? []).length : 0)
    expect.soft(await models.copyCommand.count(), 'a Copy command button for each fix MOSAIC offers').toBeGreaterThanOrEqual(commands)
  })

  test('A3 A4 the manifest endpoints are registered and MOSAIC can read them', async ({ personas, targets }) => {
    const api = await MosaicApi.as(personas, targets, targets.roles.admin)
    const models = await ModelsPage.open(personas, targets)
    for (const key of Object.keys(targets.endpoints)) {
      const endpoint = await registeredEndpoint(api, key)
      if (!endpoint) continue
      expect.soft(['connected', 'degraded'], `${key}'s status`).toContain(endpoint.status)
      await expect.soft(models.endpointRow(endpoint.name)).toContainText(endpointStatusLabels[endpoint.status] ?? endpoint.status)
      expect.soft(endpoint.access.canRead, `MOSAIC to read ${key}`).toBe(true)
      await models.selectEndpoint(endpoint.name)
      await expect.soft(models.readVerdict(endpoint.access.canRead)).toBeVisible()
    }

    // Any endpoint MOSAIC can't read says so, with the command that grants it (A4).
    for (const endpoint of (await api.endpoints()).filter((item) => !item.access.canRead)) {
      await models.selectEndpoint(endpoint.name)
      await expect.soft(models.readVerdict(false)).toBeVisible()
      if (endpoint.access.remediation) await expect.soft(models.copyCommand.first()).toBeVisible()
    }
  })

  test('A5 synced deployments include every model the manifest publishes', async ({ personas, targets }) => {
    const api = await MosaicApi.as(personas, targets, targets.roles.admin)
    const models = await ModelsPage.open(personas, targets)
    const disposable = targets.suite?.disposable?.publication
    for (const [key, target] of Object.entries(targets.endpoints)) {
      const endpoint = await registeredEndpoint(api, key)
      if (!endpoint) continue
      const synced = (await api.deployments(endpoint.id)).map((deployment) => deployment.deploymentName)
      const wanted = [...target.publish, ...(disposable?.endpoint === key ? [disposable.deployment] : [])]
      expect.soft(wanted.filter((name) => !synced.includes(name)), `deployments MOSAIC hasn't synced from ${key}`).toEqual([])
      await models.selectEndpoint(endpoint.name)
      // The header row, then one row for each deployment.
      if (synced.length > 0) await expect.soft(models.deployments.getByRole('row')).toHaveCount(synced.length + 1)
      for (const name of wanted.filter((deployment) => synced.includes(deployment))) {
        await expect.soft(models.deploymentRow(name).first()).toBeVisible()
      }
    }
  })

  test('A6 the suite gateway can invoke every manifest endpoint and none of the negatives', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const models = await ModelsPage.open(personas, targets)
    for (const key of Object.keys(targets.endpoints)) {
      const endpoint = await registeredEndpoint(ctx.api, key)
      if (!endpoint) continue
      const entry = endpoint.runtimeAccess.find((access) => access.gatewayId === ctx.gateway.id)
      expect.soft(entry, `MOSAIC to evaluate ${ctx.gateway.name}'s access to ${key}`).toBeDefined()
      if (!entry) continue
      expect.soft(entry.canInvoke, `${ctx.gateway.name} to be able to invoke ${key}`).toBe(true)
      await models.selectEndpoint(endpoint.name)
      await expect.soft(models.gatewayVerdict(entry.gatewayName)).toHaveText(`${entry.gatewayName}: can invoke`)
    }

    const registered = await ctx.api.endpoints()
    for (const [key, negative] of Object.entries(targets.negatives ?? {})) {
      const endpoint = registered.find((item) => lower(item.azureResourceId) === lower(negative.resourceId))
      if (!endpoint) {
        note('negative', `${key} isn't registered in MOSAIC, so its runtime verdict wasn't checked.`)
        continue
      }
      const entry = endpoint.runtimeAccess.find((access) => access.gatewayId === ctx.gateway.id)
      if (!entry) {
        note('negative', `MOSAIC has no verdict on whether ${ctx.gateway.name} can invoke ${key}.`)
        continue
      }
      expect.soft(entry.canInvoke, `${ctx.gateway.name} to be unable to invoke ${key}: ${negative.reason}`).toBe(false)
      await models.selectEndpoint(endpoint.name)
      await expect.soft(models.gatewayVerdict(entry.gatewayName)).toHaveText(/: (cannot invoke|not confirmed)$/)
    }
  })

  test.describe('with writes', { tag: '@writes' }, () => {
    requireWrites()

    test('A3 MOSAIC refuses to register an endpoint twice', async ({ personas, targets }) => {
      const api = await MosaicApi.as(personas, targets, targets.roles.admin)
      let chosen: { key: string; resourceId: string } | undefined
      for (const key of Object.keys(targets.endpoints)) {
        const registered = await api.endpointsFor(key)
        const resourceId = registered[0]?.azureResourceId
        if (registered.length === 1 && resourceId) {
          chosen = { key, resourceId }
          break
        }
      }
      if (!chosen) skip('No manifest endpoint is registered exactly once, so there is nothing to register twice.')
      const { key, resourceId } = chosen
      const models = await ModelsPage.open(personas, targets)
      const dialog = await models.openRegisterDialog()
      const name = `${disposablePrefix}duplicate ${key}`
      const response = await dialog.registerAzureResource(resourceId, name)
      if (response.ok()) {
        await models.reload()
        await expectOk(await models.removeEndpoint(name), 'Removing the second registration')
        throw new Error(`MOSAIC registered ${key} a second time (${response.status()}). The suite removed the copy.`)
      }
      expect(response.status(), 'MOSAIC to refuse a second registration').toBe(409)
      await expect(dialog.refusal).toBeVisible()
      await expect.soft(dialog.root).toContainText(/already registered/i)
      await dialog.cancel()
    })

    test('A5 Sync models finds the same deployments again', async ({ personas, targets }) => {
      const keys = Object.keys(targets.endpoints)
      test.setTimeout((keys.length * 4 + 1) * 60_000)
      const api = await MosaicApi.as(personas, targets, targets.roles.admin)
      const models = await ModelsPage.open(personas, targets)
      for (const key of keys) {
        const endpoint = await registeredEndpoint(api, key)
        if (!endpoint?.access.canRead) continue
        const before = (await api.deployments(endpoint.id)).map((deployment) => deployment.deploymentName)
        await expectOk(await models.sync(endpoint.name), `Syncing ${key}'s models`)
        const after = (await api.deployments(endpoint.id)).map((deployment) => deployment.deploymentName)
        expect.soft(targets.endpoints[key].publish.filter((name) => !after.includes(name)), `published deployments missing from ${key} after a sync`).toEqual([])
        expect.soft(before.filter((name) => !after.includes(name)), `deployments a sync of ${key} dropped`).toEqual([])
      }
    })

    test('A4 A6 Check access confirms MOSAIC can read each endpoint and the gateway can call it', async ({ personas, targets }) => {
      const keys = Object.keys(targets.endpoints)
      test.setTimeout((keys.length * 2 + 1) * 60_000)
      const ctx = await suiteContext(personas, targets)
      const models = await ModelsPage.open(personas, targets)
      for (const key of keys) {
        const endpoint = await registeredEndpoint(ctx.api, key)
        if (!endpoint) continue
        await expectOk(await models.checkAccess(endpoint.name), `Checking access to ${key}`)
        const checked = (await ctx.api.endpointsFor(key))[0]
        expect.soft(checked?.access.canRead, `MOSAIC to read ${key} after Check access`).toBe(true)
        const entry = checked?.runtimeAccess.find((access) => access.gatewayId === ctx.gateway.id)
        expect.soft(entry?.canInvoke, `${ctx.gateway.name} to be able to invoke ${key} after Check access`).toBe(true)
      }
    })
  })
})
