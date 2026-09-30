import { flags } from '../src/config.ts'
import { expect, requireModelRequests, requireWrites, test } from '../src/fixtures.ts'
import {
  type HeldGrant,
  type ThrowawaySpec,
  appliedMethods,
  applyTimeoutMs,
  heldGrant,
  holderOf,
  note,
  skip,
  suiteContext,
  throwawayModel,
  withThrowawayGrant,
  workloadGrant,
} from '../src/journeys.ts'
import type { AccessMethods } from '../src/mosaic-api.ts'
import {
  applicationKeyWithheld,
  applicationTokenSource,
  grantPassed,
  runVerifier,
  runtimeForwarded,
  unreportedChecks,
  userTokenPlan,
  verifierArgs,
  verifierOutcomes,
  verifierProblems,
} from '../src/runtime.ts'
import { missingSuite } from '../src/suite.ts'

/**
 * Real calls through API Management, journeys R1 to R6, made by scripts/verify_model_access.py as src/runtime.ts
 * drives it. Every run sends billed model requests, so the spec needs MOSAIC_E2E_SEND_MODEL_REQUESTS=1. R1 to R4
 * use grants the manifest says already exist, and change nothing. R5 and R6 need a throwaway grant with the proof's
 * exact limits, so they also need writes, and put the grant back to rest afterwards. R7 runs in 60-lifecycle,
 * where the grant is revoked; R8 is checked by hand.
 */

const noMethods: AccessMethods = { keysEnabled: false, entraEnabled: false }

/** Notes each check the verifier skipped, with its reason, so a skipped refusal is never taken for a tested one. */
function noteSkips(journey: string, skips: readonly string[]): void {
  for (const line of skips) note(journey, line)
}

test.describe('50 runtime', { tag: '@runtime' }, () => {
  requireModelRequests()

  test('R1 R2 R4 the user reaches each granted model by every applied method, and the gateway refuses the rest', async ({ personas, targets }, testInfo) => {
    const ctx = await suiteContext(personas, targets)
    const runtime = ctx.suite.runtime ?? skip(missingSuite('runtime'))
    const user = targets.roles.user
    const held: HeldGrant[] = []
    for (const model of runtime.userGrants) held.push(await heldGrant(ctx, model, user))
    // The verifier refuses a grant with anything pending, so check that first, before any model is called.
    for (const { label, grant } of held) expect(grant.runtime?.status, `${user}'s grant on ${label}`).toBe('applied')
    const methods = held.map(({ grant, publication }) => appliedMethods(grant, publication) ?? noMethods)

    const holder = holderOf(targets, user)
    const plan = userTokenPlan({
      env: process.env,
      interactive: flags.interactive(),
      entra: methods.some((applied) => applied.entraEnabled),
      holder,
      stranger: targets.roles.outsider ?? targets.roles.noRole,
      wantUngranted: true,
    })
    if ('skip' in plan) skip(plan.skip)
    for (const text of plan.notes) note('R2', text)

    const { outcome, summary } = await runVerifier({
      personas,
      targets,
      args: verifierArgs({
        userGrants: held.map(({ grant }) => grant.id),
        userTokenSource: plan.source,
        checkUngrantedUser: plan.checkUngrantedUser,
        runtime,
      }),
      forwarded: runtimeForwarded(process.env, holder),
      testInfo,
      name: 'R1-R2-R4',
    })
    expect(verifierProblems(outcome), 'what went wrong in the verifier run').toEqual([])
    expect(summary.checkedGrants, 'grants the verifier checked').toBe(held.length)
    held.forEach(({ label }, index) => {
      const grant = { kind: 'user' as const, index: index + 1, methods: methods[index], ungranted: plan.checkUngrantedUser }
      expect.soft(unreportedChecks(summary, grant), `checks the verifier didn't report for ${label}`).toEqual([])
    })
    noteSkips('R4', summary.skips)
  })

  test("R3 R4 the workload reaches its model with its own token and its handed-off key, and the gateway refuses the rest", async ({ personas, targets }, testInfo) => {
    const ctx = await suiteContext(personas, targets)
    const runtime = ctx.suite.runtime ?? skip(missingSuite('runtime'))
    const model = runtime.applicationGrant ?? skip(missingSuite('runtime.applicationGrant'))
    const held = await workloadGrant(ctx, model)
    expect(held.grant.runtime?.status, `the workload's grant on ${held.label}`).toBe('applied')
    const methods = appliedMethods(held.grant, held.publication) ?? noMethods
    const source = applicationTokenSource(process.env)
    if (methods.entraEnabled && !source) {
      skip(
        `The workload's grant on ${held.label} accepts Microsoft Entra tokens, so the verifier needs the workload's token. ` +
          'Set MOSAIC_SMOKE_APPLICATION_CLIENT_ID and MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET, or MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN.',
      )
    }

    // The admin reads the workload's key, as in the handoff, and the user's MOSAIC token must not be able to.
    const { outcome, summary } = await runVerifier({
      personas,
      targets,
      args: verifierArgs({ applicationGrants: [held.grant.id], applicationTokenSource: source, runtime }),
      testInfo,
      name: 'R3-R4',
    })
    expect(verifierProblems(outcome), 'what went wrong in the verifier run').toEqual([])
    expect(summary.checkedGrants, 'grants the verifier checked').toBe(1)
    expect.soft(unreportedChecks(summary, { kind: 'application', index: 1, methods }), "checks the verifier didn't report for the workload's grant").toEqual([])
    expect.soft(summary.passes, "the verifier's check that the user can't retrieve the workload's key").toContain(applicationKeyWithheld)
    noteSkips('R4', summary.skips)
  })

  test.describe('with a throwaway grant', { tag: '@writes' }, () => {
    requireWrites()

    test('R5 a budget of 2 calls per 300 seconds, spent by the primary key and a token, refuses the secondary key', async ({ personas, targets }, testInfo) => {
      test.setTimeout(applyTimeoutMs)
      const ctx = await suiteContext(personas, targets)
      const budget = ctx.suite.disposable?.sharedBudget ?? skip(missingSuite('disposable.sharedBudget'))
      const spec: ThrowawaySpec = {
        journey: 'R5',
        model: budget,
        persona: budget.persona,
        limits: { calls: budget.calls, perSeconds: budget.perSeconds },
        needs: { keys: true, entra: true },
      }
      // Anything that would stop the proof skips it before the grant is touched.
      await throwawayModel(ctx, spec)
      const holder = holderOf(targets, budget.persona)
      const plan = userTokenPlan({ env: process.env, interactive: flags.interactive(), entra: true, holder, wantUngranted: false })
      if ('skip' in plan) skip(plan.skip)
      for (const text of plan.notes) note('R5', text)

      await withThrowawayGrant(ctx, spec, async ({ grant, label }) => {
        const { outcome, summary } = await runVerifier({
          personas,
          targets,
          args: verifierArgs({ userGrants: [grant.id], userTokenSource: plan.source, proof: 'shared-budget', runtime: ctx.suite.runtime }),
          choice: { user: budget.persona },
          forwarded: runtimeForwarded(process.env, holder),
          testInfo,
          name: 'R5-shared-budget',
        })
        expect(verifierProblems(outcome), 'what went wrong in the verifier run').toEqual([])
        expect(grantPassed(summary, 'user', 1, verifierOutcomes.sharedBudget), `the shared budget on ${label}`).toBe(true)
      })
    })

    test('R6 a tokens-per-minute limit returns 429 with Retry-After', async ({ personas, targets }, testInfo) => {
      test.setTimeout(applyTimeoutMs)
      const ctx = await suiteContext(personas, targets)
      const limit = ctx.suite.disposable?.tokenLimit ?? skip(missingSuite('disposable.tokenLimit'))
      const spec: ThrowawaySpec = {
        journey: 'R6',
        model: limit,
        persona: limit.persona,
        limits: { tokensPerMinute: limit.tokensPerMinute },
        needs: { keys: false, entra: false },
      }
      const { label, publication } = await throwawayModel(ctx, spec)
      const methods = publication.appliedAccess?.settings ?? noMethods
      if (!methods.keysEnabled && !methods.entraEnabled) skip(`${label} accepts neither keys nor tokens, so nothing can spend its token limit.`)
      const holder = holderOf(targets, limit.persona)
      const plan = userTokenPlan({ env: process.env, interactive: flags.interactive(), entra: methods.entraEnabled, holder, wantUngranted: false })
      if ('skip' in plan) skip(plan.skip)
      for (const text of plan.notes) note('R6', text)

      await withThrowawayGrant(ctx, spec, async ({ grant }) => {
        const { outcome, summary } = await runVerifier({
          personas,
          targets,
          args: verifierArgs({ userGrants: [grant.id], userTokenSource: plan.source, proof: 'token-limit', runtime: ctx.suite.runtime }),
          choice: { user: limit.persona },
          forwarded: runtimeForwarded(process.env, holder),
          testInfo,
          name: 'R6-token-limit',
        })
        expect(verifierProblems(outcome), 'what went wrong in the verifier run').toEqual([])
        expect(grantPassed(summary, 'user', 1, verifierOutcomes.tokenLimit), `the token limit on ${label}`).toBe(true)
      })
    })
  })
})
