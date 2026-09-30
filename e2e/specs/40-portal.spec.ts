import { type SuiteModel, type Targets, modelLabel } from '../src/config.ts'
import { describeResponses, ResponseRecorder } from '../src/denials.ts'
import { expect, requireWrites, test } from '../src/fixtures.ts'
import {
  type SuiteContext,
  appliedMethods,
  applyTimeoutMs,
  cleanupDisposable,
  ensureDisposable,
  heldGrant,
  note,
  requireDisposable,
  reviewAndApply,
  skip,
  suiteContext,
} from '../src/journeys.ts'
import { type ApiAccessRequest, type ApiEntitlement, MosaicApi, MosaicApiError } from '../src/mosaic-api.ts'
import { expectOk, jsonOf } from '../src/pages/common.ts'
import { EntitlementsPage } from '../src/pages/console/entitlements.ts'
import { CatalogPage } from '../src/pages/portal/catalog.ts'
import { MyAccessPage } from '../src/pages/portal/my-access.ts'
import { MyRequestsPage } from '../src/pages/portal/my-requests.ts'
import { UsagePage } from '../src/pages/portal/usage.ts'
import { noPortalAccessTitle, primaryNavigation } from '../src/personas.ts'
import { runVerifier, verifierArgs, verifierProblems } from '../src/runtime.ts'
import {
  cleanupHint,
  describeLimits,
  grantOf,
  limitLines,
  limitsMatch,
  missingSuite,
  personaPrincipal,
  portalCardTitle,
  resolveModel,
  runTag,
  suiteJustification,
  suiteNote,
} from '../src/suite.ts'

/**
 * The end-user portal, journeys P0 to P9 and A13: who may see what, the user's grants, limits and keys, isolation
 * between people, and usage. The @writes group has the disposable model's requester ask for access, withdraw and
 * ask again; the admin approves, reviews and applies; and the requester sees the grant. Everything else only reads.
 */

/** People with the User role other than roles.user, the guest persona first. */
function otherUsers(targets: Targets): string[] {
  const keys = Object.keys(targets.personas).filter((key) => key !== targets.roles.user && targets.personas[key].expectedRole === 'User')
  return keys.sort((left, right) => Number(right === targets.roles.guest) - Number(left === targets.roles.guest))
}

/** The first of a persona's grants on these models whose subscription keys are applied, read without skipping. */
async function keyedGrant(ctx: SuiteContext, models: readonly SuiteModel[], personaKey: string): Promise<{ label: string; grant: ApiEntitlement } | undefined> {
  const principal = await personaPrincipal(ctx.api, ctx.personas, ctx.targets, personaKey)
  if (!principal) return undefined
  for (const model of models) {
    const { label, publication, modelApi } = await resolveModel(ctx.api, model, ctx.gateway.id)
    if (!publication || !modelApi) continue
    const [grant] = await ctx.api.grantsOf(principal.id, modelApi.id)
    if (grant?.enabled && grant.runtime?.status === 'applied' && appliedMethods(grant, publication)?.keysEnabled) return { label, grant }
  }
  return undefined
}

test.describe('40 portal', { tag: '@portal' }, () => {
  test('P0 the user persona reaches My access and a catalog of every listed model', async ({ personas, targets }) => {
    const user = targets.roles.user
    const admin = await MosaicApi.as(personas, targets, targets.roles.admin)
    const gateways = new Set((await admin.gateways()).map((gateway) => gateway.id))
    const modelApis = await admin.modelApis()
    const listed = modelApis.filter((modelApi) => modelApi.visibility === 'catalog' && gateways.has(modelApi.gatewayId))
    const hidden = new Set(modelApis.filter((modelApi) => modelApi.visibility === 'private').map((modelApi) => modelApi.id))
    const entries = await (await MosaicApi.as(personas, targets, user)).portalCatalog()
    const shown = new Set(entries.filter((entry) => entry.kind === 'modelApi').map((entry) => entry.id))
    expect.soft(
      listed.filter((modelApi) => !shown.has(modelApi.id)).map((modelApi) => modelApi.displayName),
      `listed models missing from ${user}'s catalog`,
    ).toEqual([])
    expect.soft(entries.filter((entry) => hidden.has(entry.id)).map((entry) => entry.displayName), `private models in ${user}'s catalog`).toEqual([])

    await MyAccessPage.open(personas, targets, user)
    const catalog = await CatalogPage.open(personas, targets, user)
    await expect(catalog.cards).toHaveCount(entries.length)
    const named = new Map<string, number>()
    for (const entry of entries) named.set(entry.displayName, (named.get(entry.displayName) ?? 0) + 1)
    for (const [name, count] of named) await expect.soft(catalog.card(name)).toHaveCount(count)
    for (const entry of entries.filter((item) => item.entitled)) {
      await expect.soft(catalog.entitled(catalog.card(entry.displayName).first())).toBeVisible()
    }
  })

  test('P1 a persona without a MOSAIC role is turned away, and MOSAIC serves it no data', async ({ personas, targets }) => {
    const key = targets.roles.noRole ?? skip('No persona is mapped to roles.noRole.')
    if (targets.personas[key].expectedRole !== 'None') skip(`${key} is expected to hold a role.`)
    const recorder = new ResponseRecorder(await personas.context(key), targets.origins.api)
    try {
      for (const path of ['/access', '/catalog', '/requests', '/usage']) {
        const page = await personas.page(key, 'portal', path)
        await expect(page.getByText(noPortalAccessTitle)).toBeVisible()
        await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
        await expect(page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })).toHaveCount(0)
      }
    } finally {
      recorder.stop()
    }
    expect(recorder.refusals.length, 'MOSAIC API calls the portal made and MOSAIC refused').toBeGreaterThan(0)
    const served = recorder.unexpectedSuccesses()
    expect(served, `MOSAIC API calls that succeeded for ${key}: ${describeResponses(served)}`).toEqual([])
  })

  test('P2 a User persona with no grants sees the empty My access and the catalog', async ({ personas, targets }) => {
    for (const key of otherUsers(targets)) {
      const api = await MosaicApi.as(personas, targets, key)
      if ((await api.portalEntitlements()).some((resolved) => resolved.effective !== false)) continue
      const access = await MyAccessPage.open(personas, targets, key)
      await expect(access.empty).toBeVisible()
      await expect(access.cards).toHaveCount(0)
      const entries = await api.portalCatalog()
      const catalog = await CatalogPage.open(personas, targets, key)
      await expect(catalog.cards).toHaveCount(entries.length)
      for (const entry of entries.filter((item) => item.kind === 'modelApi' && item.requestState !== 'pending')) {
        await expect.soft(catalog.requestButton(catalog.card(entry.displayName).first())).toBeVisible()
      }
      return
    }
    skip(
      'Every persona with the User role, other than roles.user, holds a grant. P2 needs one who holds none; 40-portal\'s P4 checks the same state for the disposable model before its requester asks for it.',
    )
  })

  test("P3 My access shows the user's applied grants with their limits and attribution", async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const runtime = ctx.suite.runtime ?? skip(missingSuite('runtime'))
    const user = targets.roles.user
    const resolved = await (await MosaicApi.as(personas, targets, user)).portalEntitlements()
    const access = await MyAccessPage.open(personas, targets, user)
    for (const model of runtime.userGrants) {
      const held = await heldGrant(ctx, model, user)
      const entry = resolved.find((item) => item.entitlement.id === held.grant.id)
      expect.soft(entry, `${user}'s grant on ${held.label} in their My access`).toBeDefined()
      if (!entry) continue
      expect.soft(entry.effective, `${user}'s grant on ${held.label} to be in effect`).not.toBe(false)
      const status = held.grant.runtime?.status ?? 'unknown'
      expect.soft(status, `${user}'s grant on ${held.label}`).toBe('applied')
      const card = access.card(portalCardTitle(entry))
      await expect.soft(card).toHaveCount(1)
      await expect.soft(access.runtime(card.first(), status)).toBeVisible()
      await expect.soft(access.limits(card.first())).toHaveText(limitLines(held.grant.enforcement))
      await expect.soft(access.attribution(card.first())).toBeVisible()
    }
  })

  test('P6 connection details show, and a key reveal is masked, brief and never cached', async ({ personas, targets }) => {
    test.slow()
    const ctx = await suiteContext(personas, targets)
    const runtime = ctx.suite.runtime ?? skip(missingSuite('runtime'))
    const user = targets.roles.user
    const keyed =
      (await keyedGrant(ctx, runtime.userGrants, user)) ??
      skip(`None of ${user}'s grants in suite.runtime.userGrants is applied with subscription keys, so there is no key to reveal.`)
    const entry = (await (await MosaicApi.as(personas, targets, user)).portalEntitlements()).find(
      (item) => item.entitlement.id === keyed.grant.id,
    )
    if (!entry) throw new Error(`${user}'s My access doesn't list their grant on ${keyed.label}.`)
    const title = portalCardTitle(entry)

    const access = await MyAccessPage.open(personas, targets, user)
    const panel = await access.connectionDetails(access.card(title).first())
    await expect(panel.section('Endpoint')).toBeVisible()
    await expect(panel.section('Authentication')).toBeVisible()
    await expect(panel.keys.secret).toHaveCount(0)
    for (const slot of ['primary', 'secondary'] as const) {
      const shown = await panel.keys.show(slot)
      expect(shown.response.status(), `the reveal of the ${slot} key`).toBe(200)
      expect(shown.noStore, `the reveal of the ${slot} key to forbid caching`).toBe(true)
      expect(shown.shownLength, `the characters of the ${slot} key the page shows`).toBeGreaterThan(0)
      expect(shown.keptIn, `where else the page keeps the ${slot} key`).toEqual([])
      if (slot === 'primary') await panel.keys.hidesByItself()
      else await panel.keys.hide()
    }

    // A reload shows no key: the page kept none.
    await access.reload()
    const again = await access.connectionDetails(access.card(title).first())
    await expect(again.keys.secret).toHaveCount(0)
  })

  test("P7 the user can't list, read or use another person's grant", async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const foreign = ctx.suite.runtime?.foreignGrant ?? skip(missingSuite('runtime.foreignGrant'))
    const held = await heldGrant(ctx, foreign, foreign.persona)
    const id = held.grant.id
    const user = targets.roles.user
    const whose = `${foreign.persona}'s grant on ${held.label}`
    const api = await MosaicApi.as(personas, targets, user)
    expect((await api.portalEntitlements()).some((resolved) => resolved.entitlement.id === id), `${whose} in ${user}'s My access`).toBe(false)
    expect((await api.get<ApiEntitlement[]>('/api/v1/me/entitlements')).some((grant) => grant.id === id), `${whose} in ${user}'s grants`).toBe(false)
    expect((await api.myUsage()).byResource.some((row) => row.entitlementId === id), `${whose} in ${user}'s usage report`).toBe(false)
    const refusal = await api.get(`/api/v1/me/entitlements/${encodeURIComponent(id)}/connection`).then(
      () => undefined,
      (error: unknown) => error,
    )
    // What MOSAIC sent back isn't printed: it would be someone else's connection details.
    if (refusal === undefined) throw new Error(`MOSAIC gave ${user} the connection details of ${whose}.`)
    if (!(refusal instanceof MosaicApiError)) throw refusal
    expect([403, 404], `MOSAIC's answer when ${user} asks for the connection details of ${whose}`).toContain(refusal.status)
  })

  test("P7 the verifier finds another person's grant withheld from the user", { tag: '@runtime' }, async ({ personas, targets }, testInfo) => {
    const ctx = await suiteContext(personas, targets)
    const runtime = ctx.suite.runtime ?? skip(missingSuite('runtime'))
    const foreign = runtime.foreignGrant ?? skip(missingSuite('runtime.foreignGrant'))
    const held = await heldGrant(ctx, foreign, foreign.persona)
    // An isolation-only run sends no model requests: it lists, reads and asks for a key, and each must be refused.
    const { outcome, summary } = await runVerifier({
      personas,
      targets,
      args: verifierArgs({ foreignGrants: [held.grant.id], runtime }),
      testInfo,
      name: 'P7-isolation',
    })
    expect(verifierProblems(outcome), 'what went wrong in the verifier run').toEqual([])
    expect(summary.isolatedGrants, 'grants the verifier found withheld from the user').toBe(1)
  })

  test('P8 the portal shows the admin as allowed, and only the admin', async ({ personas, targets }) => {
    const admin = targets.roles.admin
    const user = targets.roles.user
    const profile = (key: string) => MosaicApi.as(personas, targets, key).then((api) => api.get<{ isAdmin?: boolean }>('/api/v1/portal/me'))
    expect((await profile(admin)).isAdmin, `whether the portal treats ${admin} as an admin`).toBe(true)
    expect((await profile(user)).isAdmin ?? false, `whether the portal treats ${user} as an admin`).toBe(false)

    const adminAccess = await MyAccessPage.open(personas, targets, admin)
    await expect(adminAccess.page.getByText('Admin allowed', { exact: true })).toBeVisible()
    await CatalogPage.open(personas, targets, admin)
    await MyRequestsPage.open(personas, targets, admin)

    const userAccess = await MyAccessPage.open(personas, targets, user)
    await expect(userAccess.page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })).toBeVisible()
    await expect(userAccess.page.getByText('Checking access', { exact: true })).toHaveCount(0)
    await expect(userAccess.page.getByText('Admin allowed', { exact: true })).toHaveCount(0)
  })

  test("P9 Usage & cost lists only the user's own grants and labels sample figures", async ({ personas, targets }) => {
    const user = targets.roles.user
    const admin = await MosaicApi.as(personas, targets, targets.roles.admin)
    const api = await MosaicApi.as(personas, targets, user)
    const report = await api.myUsage()
    const own = new Set((await api.portalEntitlements()).map((resolved) => resolved.entitlement.id))
    const principal = await personaPrincipal(admin, personas, targets, user)
    let others = 0
    for (const row of report.byResource) {
      // As the admin reads it, each grant is the user's own or one of their groups'.
      const grant = await admin.entitlement(row.entitlementId)
      const subject = grant.subject
      const theirs = subject.kind === 'user' ? subject.id === principal?.id : subject.kind === 'group' || subject.kind === 'securityGroup'
      if (!theirs || !own.has(row.entitlementId)) others += 1
    }
    expect(others, `grants in ${user}'s usage report that aren't theirs`).toBe(0)

    const usage = await UsagePage.open(personas, targets, user)
    if (report.byResource.length === 0) await expect(usage.empty).toBeVisible()
    else await expect(usage.resources).toHaveCount(report.byResource.length)
    if (report.dataSource === 'simulated') await expect(usage.sampleData).toBeVisible()
    else await expect(usage.page.getByText('Sample data', { exact: true })).toHaveCount(0)
  })

  test.describe('request, approve and apply', { tag: ['@writes', '@console'] }, () => {
    requireWrites()
    test.describe.configure({ mode: 'serial', timeout: applyTimeoutMs })

    // Unique to this run, so each step finds its own request among earlier runs'.
    const tag = runTag()
    const first = suiteJustification('P4', tag)
    const second = suiteJustification('A13', tag)

    test('P4 the requester asks for the disposable model, withdraws, and asks again', async ({ personas, targets }) => {
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      const requester = disposable.requester
      const label = modelLabel(disposable)
      let state = await ensureDisposable(ctx, disposable)
      if (await grantOf(ctx.api, personas, targets, requester, state.modelApi.id)) {
        // MOSAIC keeps an applied grant until its model is unpublished, so the only way back is to publish it again.
        note('reset', `${requester} already held a grant on ${label} from an earlier run, so the suite published it again.`)
        for (const line of await cleanupDisposable(ctx, disposable)) note('reset', line)
        state = await ensureDisposable(ctx, disposable)
      }
      const name = state.modelApi.displayName
      const catalog = await CatalogPage.open(personas, targets, requester)
      const card = catalog.card(name)
      await expect(card).toHaveCount(1)
      const requesterApi = await MosaicApi.as(personas, targets, requester)
      const open = (await requesterApi.portalAccessRequests()).some(
        (request) => request.state === 'pending' && request.resource.id === state.modelApi.id,
      )
      if (open) {
        await expectOk(await catalog.withdraw(name), `Withdrawing ${requester}'s open request for ${label} from an earlier run`)
        note('reset', `Withdrew ${requester}'s open request for ${label} from an earlier run.`)
        await catalog.reload()
      }

      // P2's state for this model: no card on My access, and the catalog offers a request.
      const access = await MyAccessPage.open(personas, targets, requester)
      await expect(access.card(name)).toHaveCount(0)
      await expect(catalog.requestButton(card)).toBeVisible()

      await expectOk(await catalog.request(name, first), `Requesting ${label}`)
      await expect(catalog.requestOpen(card)).toBeVisible()
      const requests = await MyRequestsPage.open(personas, targets, requester)
      const opened = requests.request(first)
      await expect(requests.state(opened, 'Pending')).toBeVisible()
      await expectOk(await requests.withdraw(opened), `Withdrawing the request for ${label}`)
      await expect(requests.state(opened, 'Withdrawn')).toBeVisible()
      await expect(opened.getByRole('button', { name: 'Withdraw', exact: true })).toHaveCount(0)

      await catalog.reload()
      await expect(catalog.requestButton(card)).toBeVisible()
      await expectOk(await catalog.request(name, second), `Requesting ${label} again`)
      await expect(catalog.requestOpen(card)).toBeVisible()
      await requests.reload()
      await expect(requests.state(requests.request(second), 'Pending')).toBeVisible()
      await expect(requests.state(requests.request(first), 'Withdrawn')).toBeVisible()
    })

    test('A13 approving the request creates grant intent, which is then reviewed and applied', async ({ personas, targets }) => {
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      const requester = disposable.requester
      const label = modelLabel(disposable)
      const publication = await ctx.api.publicationOf(disposable, ctx.gateway.id)
      const modelApi = publication && (await ctx.api.modelApiOf(publication))
      if (!publication || !modelApi) throw new Error(`${label} isn't published, as P4 should have left it. ${cleanupHint}`)

      const entitlements = await EntitlementsPage.open(personas, targets)
      const row = entitlements.requestRow(second)
      await expect(row).toHaveCount(1)
      const dialog = await entitlements.approve(row)
      const approved = await jsonOf<ApiAccessRequest>(
        await dialog.approve(disposable.limits, suiteNote('A13')),
        `Approving ${requester}'s request for ${label}`,
      )
      expect(approved.state, `${requester}'s request after its approval`).toBe('approved')
      const grantId = approved.grantedEntitlementId ?? (await grantOf(ctx.api, personas, targets, requester, modelApi.id))?.id
      if (!grantId) throw new Error(`Approving ${requester}'s request for ${label} created no grant.`)

      // Approval saves intent only: API Management is unchanged until the model's plan is applied.
      const intent = await ctx.api.entitlement(grantId)
      expect.soft(intent.enabled, 'the approved grant to be enabled').toBe(true)
      expect.soft(limitsMatch(intent.enforcement, disposable.limits), `the approved grant to allow ${describeLimits(disposable.limits)}`).toBe(true)
      expect.soft(intent.runtime?.status, 'the approved grant before its review').toBe('pending')
      await entitlements.reload()
      await expect.soft(entitlements.grantState(grantId, 'pending')).toBeVisible()

      const requests = await MyRequestsPage.open(personas, targets, requester)
      const card = requests.request(second)
      await expect(requests.state(card, 'Approved')).toBeVisible()
      await expect(requests.approvedNote(card)).toBeVisible()

      await reviewAndApply(ctx, entitlements, publication, `${requester}'s approved grant on ${label}`, { settle: [grantId] })
      await entitlements.reload()
      await expect(entitlements.grantState(grantId, 'applied')).toBeVisible()
    })

    test('P5 after the apply, the requester sees the grant applied with its limits', async ({ personas, targets }) => {
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      const requester = disposable.requester
      const label = modelLabel(disposable)
      const publication = await ctx.api.publicationOf(disposable, ctx.gateway.id)
      const modelApi = publication && (await ctx.api.modelApiOf(publication))
      const grant = modelApi && (await grantOf(ctx.api, personas, targets, requester, modelApi.id))
      if (!grant) throw new Error(`${requester} holds no grant on ${label} after A13.`)
      const entry = (await (await MosaicApi.as(personas, targets, requester)).portalEntitlements()).find(
        (item) => item.entitlement.id === grant.id,
      )
      if (!entry) throw new Error(`${requester}'s My access doesn't list their grant on ${label}.`)

      const access = await MyAccessPage.open(personas, targets, requester)
      const card = access.card(portalCardTitle(entry))
      await expect(card).toHaveCount(1)
      await expect(access.runtime(card, 'applied')).toBeVisible()
      await expect(access.limits(card)).toHaveText(limitLines(grant.enforcement))
      const requests = await MyRequestsPage.open(personas, targets, requester)
      await expect(requests.state(requests.request(second), 'Approved')).toBeVisible()
    })
  })
})
