import { test as fixtureTest, expect } from '../src/fixtures.ts'
import { type ModelBudgetView, budgetProof } from '../src/model-budget.ts'
import { approvedModelScope, budgetReady, prepareModelRun } from '../src/model-live.ts'
import { pooledTokenProof, selectionProof } from '../src/model-runtime.ts'
import { CostCenterBudgetPage } from '../src/pages/console/cost-centers.ts'
import { MyAccessPage } from '../src/pages/portal/my-access.ts'
import { redactErrors } from '../src/redact.ts'

// No live screenshots or page snapshots for these proofs. Credentials stay in RAM.
const test = fixtureTest.extend<{ personaPages: void }>({
  personaPages: async ({ personas }, use, info) => {
    await use()
    redactErrors(info.errors)
    await personas.closePages()
  },
})

test.describe('55 isolated model cost centers and budgets', { tag: ['@model-budgets', '@runtime'] }, () => {
  // A default run skips BEFORE manifest/profile fixtures. Generic write/inference flags do not open this gate.
  test.skip(!process.env.MOSAIC_E2E_MODEL_SCOPE, 'NOT RUN: exact isolated model scope and owner approval required')

  test('R10 runtime/UI leg: token header choices and existing key cost-center matrix (not analytics/backend proof)', async ({ personas, targets }, info) => {
    approvedModelScope(targets, 'R10')
    const run = await prepareModelRun(personas, targets, 'R10', info)
    let key: string | undefined
    try {
      const grant = run.scope.selection.default
      const connection = run.connections.get(grant.id)!
      test.skip(!connection.keyExists, 'NOT RUN: create an isolated key through UI only after separate owner approval')
      const access = await MyAccessPage.open(personas, targets, grant.persona)
      const center = run.centers.get(grant.costCenterId)!
      const card = access.cards.filter({ hasText: `${center.name} · ${center.code}` })
      const panel = await access.connectionDetails(card)
      const shown = await panel.keys.show('primary')
      if (!shown.response.ok() || !shown.noStore || shown.keptIn.length || shown.shownLength < 8) throw new Error('Key reveal failed its secret-hygiene checks')
      key = (await panel.keys.secret.textContent())?.trim()
      if (!key) throw new Error('UI did not reveal the approved existing key')
      await panel.keys.hide()
      await selectionProof(run.scope, run.call, key)
      info.annotations.push({ type: 'NOT PROVEN', description: 'R10 still needs request-correlated Analytics grant attribution, denial traces and real backend header stripping' })
    } finally {
      key = undefined
      await run.finish()
    }
  })

  test('R11 fresh shared monthly MODEL token counter across two distinct grants, independent own counters and other-center control', async ({ personas, targets }, info) => {
    approvedModelScope(targets, 'R11')
    const run = await prepareModelRun(personas, targets, 'R11', info)
    test.setTimeout((run.scope.bounds.timeoutSeconds + 120) * 1000)
    try {
      await pooledTokenProof(run.scope, run.connections, run.call, run.pause)
      info.annotations.push({ type: 'boundary', description: 'Fresh model counter proof only; MCP pooled calls are not R11, and missing required headers fail rather than infer pool throttling' })
    } finally { await run.finish() }
  })

  for (const journey of ['R12', 'R14'] as const) {
    test(`${journey} no-email budget UI/runtime leg: real priced spend, selective block, portal banner and raise (independent audit/delivery still required)`, { tag: '@writes' }, async ({ personas, targets }, info) => {
      const scope = approvedModelScope(targets, journey)
      test.setTimeout((scope.bounds.timeoutSeconds + 300) * 1000)
      const run = await prepareModelRun(personas, targets, journey, info)
      let details: object = {}
      try {
        await budgetReady(run.api, scope)
        const fixture = scope.budget!
        const center = run.centers.get(fixture.grant.costCenterId)!
        const page = new CostCenterBudgetPage(await personas.page(targets.roles.admin, 'web', `/cost-centers/${center.id}`), targets, center.id)
        await page.loaded(center.name)
        const result = await budgetProof(scope, journey, {
          read: () => run.api.get<ModelBudgetView | null>(`/api/v1/cost-centers/${center.id}/budget`),
          save: (amount) => page.save(amount),
          call: run.call,
          pause: run.pause,
          banner: async (blocked) => {
            const access = await MyAccessPage.open(personas, targets, fixture.grant.persona)
            const region = access.page.getByRole('region', { name: 'Cost center budgets' })
            const title = region.getByText(`${center.name} has used its monthly budget`, { exact: true })
            if (blocked) await expect(title).toBeVisible()
            else await expect(title).toHaveCount(0)
          },
        })
        details = {
          timing: { blockSave: result.blockSave, raiseSave: result.raiseSave, first403At: result.first403.at, firstSuccessAt: result.firstSuccess.at },
          rollup: { blockedSpend: result.blocked.status.monthToDate, raisedSpend: result.raised.status.monthToDate, through: result.blocked.status.through },
          gateways: result.blocked.status.gateways.map((g) => ({ id: g.gatewayId, syncedAt: g.syncedAt })),
          notificationEvidence: 'NOT RUN: zero recipients; skipped/ACS-accepted records are not mailbox delivery or dedupe',
        }
        info.annotations.push({ type: 'NOT PROVEN', description: `${journey}: need actual named-value snapshots/write audits and budget denial trace; R14 email/dedupe NOT RUN` })
      } finally {
        await run.finish(details)
        info.annotations.push({ type: 'cleanup', description: 'No automatic cleanup, grant edits or deletes. A failed blocking run may leave its isolated budget blocked; owner must approve UI recovery. The shared lists must never be edited directly.' })
      }
    })
  }

  test('R13 Government ACS managed-identity delivery and repeated Operation-Id', async () => {
    test.skip(true, 'NOT RUN: human-assisted Government ACS/domain/MI/recipient fixture and exact send approval required; no provisioning, email settings writes or sends implemented')
  })
})
