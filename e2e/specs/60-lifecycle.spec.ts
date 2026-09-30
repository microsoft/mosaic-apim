import { flags, modelLabel } from '../src/config.ts'
import { expect, requireModelRequests, requireWrites, test } from '../src/fixtures.ts'
import {
  type ThrowawaySpec,
  applyTimeoutMs,
  ensureDisposable,
  ensureThrowawayGrant,
  expectGrantBadge,
  holderOf,
  note,
  requireDisposable,
  reviewAndApply,
  setMethods,
  skip,
  suiteContext,
  throwawayModel,
  withThrowawayGrant,
} from '../src/journeys.ts'
import type { AccessMethods } from '../src/mosaic-api.ts'
import { expectOk } from '../src/pages/common.ts'
import { EntitlementsPage } from '../src/pages/console/entitlements.ts'
import {
  attachVerifierLog,
  grantPassed,
  revocationPromptLabel,
  runtimeForwarded,
  startVerifier,
  userTokenPlan,
  verifierArgs,
  verifierOutcomes,
  verifierProblems,
  verifierSummary,
} from '../src/runtime.ts'
import { methodsLabel } from '../src/suite.ts'

/**
 * Changing access after it was applied, journeys A14 and R7, on the disposable publication only: turning a method
 * off and on again, revoking a grant and enabling it again, and the gateway refusing a revoked grant. Every change
 * is saved in the console first, and reaches API Management only through "Review model changes" and "Apply plan".
 * Each test starts from ensureDisposable, which resets anything an earlier run left part done.
 */

const both: AccessMethods = { keysEnabled: true, entraEnabled: true }
const keysOnly: AccessMethods = { keysEnabled: true, entraEnabled: false }

/** How long the verifier may take to sign in and check the grant before it asks for the revocation. */
const revocationPromptMs = 20 * 60_000

test.describe('60 lifecycle', { tag: ['@console', '@writes'] }, () => {
  requireWrites()
  test.describe.configure({ mode: 'serial', timeout: applyTimeoutMs })

  test('A14 a method turned off and on again changes nothing until each change is reviewed and applied', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const disposable = requireDisposable(targets)
    const label = modelLabel(disposable)
    const { publication } = await ensureDisposable(ctx, disposable)
    const entitlements = await EntitlementsPage.open(personas, targets)

    for (const [methods, change] of [
      [keysOnly, `turning Microsoft Entra tokens off for ${label}`],
      [both, `turning Microsoft Entra tokens back on for ${label}`],
    ] as const) {
      const before = (await ctx.api.publication(publication.id)).appliedAccess?.settings
      await entitlements.reload()
      await entitlements.selectPublication(publication.id, publication.displayName)
      await setMethods(entitlements, methods)
      // Saved, not applied: the gateway keeps the methods it last applied until the plan is applied.
      const saved = await ctx.api.publication(publication.id)
      expect(saved.governedAccess, `${label}'s saved methods`).toMatchObject({ ...methods })
      expect(saved.appliedAccess?.settings, `${label}'s applied methods before the apply`).toEqual(before)
      await expect(entitlements.appliedMethods).toHaveText(`Last applied methods: ${methodsLabel(before)}`)

      const applied = await reviewAndApply(ctx, entitlements, publication, change)
      expect(applied.appliedAccess?.settings, `${label}'s applied methods after the apply`).toMatchObject({ ...methods })
      await entitlements.reload()
      await entitlements.selectPublication(publication.id, publication.displayName)
      await expect(entitlements.accessBadge).toHaveText('Access: applied')
      await expect(entitlements.appliedMethods).toHaveText(`Last applied methods: ${methodsLabel(methods)}`)
    }
  })

  test('A14 a revoked grant stays pending until the apply revokes it, and enabling it again is applied the same way', async ({ personas, targets }) => {
    const ctx = await suiteContext(personas, targets)
    const disposable = requireDisposable(targets)
    await ensureDisposable(ctx, disposable)
    const spec: ThrowawaySpec = {
      journey: 'A14',
      model: disposable,
      persona: disposable.requester,
      limits: disposable.limits,
      needs: { keys: false, entra: false },
      owned: true,
    }
    const { label, publication, grant } = await ensureThrowawayGrant(ctx, spec)
    const what = `${spec.persona}'s grant on ${label}`
    const entitlements = await EntitlementsPage.open(personas, targets)
    expect((await expectGrantBadge(ctx, entitlements, grant.id)).runtime?.status, `${what} before the revocation`).toBe('applied')

    await expectOk(await entitlements.disable(grant.id), `Revoking ${what}`)
    const saved = await expectGrantBadge(ctx, entitlements, grant.id)
    expect(saved.enabled, `${what} after Revoke`).toBe(false)
    expect(saved.runtime?.status, `${what} before the apply`).toBe('revocationPending')
    await reviewAndApply(ctx, entitlements, publication, `revoking ${what}`, { settle: [grant.id] })
    expect((await expectGrantBadge(ctx, entitlements, grant.id)).runtime?.status, `${what} after the apply`).toBe('revoked')

    await expectOk(await entitlements.reEnable(grant.id), `Enabling ${what} again`)
    expect((await expectGrantBadge(ctx, entitlements, grant.id)).runtime?.status, `${what} enabled again, before the apply`).toBe('pending')
    await reviewAndApply(ctx, entitlements, publication, `enabling ${what} again`, { settle: [grant.id] })
    expect((await expectGrantBadge(ctx, entitlements, grant.id)).runtime?.status, `${what} after the apply`).toBe('applied')
  })

  test.describe('with model requests', { tag: '@runtime' }, () => {
    requireModelRequests()

    test('R7 once a revocation is applied, the gateway refuses the grant', async ({ personas, targets }, testInfo) => {
      const ctx = await suiteContext(personas, targets)
      const disposable = requireDisposable(targets)
      await ensureDisposable(ctx, disposable)
      const spec: ThrowawaySpec = {
        journey: 'R7',
        model: disposable,
        persona: disposable.requester,
        limits: disposable.limits,
        needs: { keys: true, entra: false },
        owned: true,
      }
      const { publication } = await throwawayModel(ctx, spec)
      const entra = publication.appliedAccess?.settings.entraEnabled === true
      const holder = holderOf(targets, spec.persona)
      const plan = userTokenPlan({ env: process.env, interactive: flags.interactive(), entra, holder, wantUngranted: false })
      if ('skip' in plan) skip(plan.skip)
      for (const text of plan.notes) note('R7', text)

      await withThrowawayGrant(ctx, spec, async ({ grant, label }) => {
        const what = `${spec.persona}'s grant on ${label}`
        const running = await startVerifier({
          personas,
          targets,
          args: verifierArgs({ userGrants: [grant.id], userTokenSource: plan.source, watchRevocation: grant.id, runtime: ctx.suite.runtime }),
          choice: { user: spec.persona },
          forwarded: runtimeForwarded(process.env, holder),
          testInfo,
        })
        try {
          // The verifier checks the grant works, then asks for the revocation and watches the gateway refuse it.
          await running.waitForLine((line) => revocationPromptLabel(line) !== undefined, revocationPromptMs, 'ask for the revocation')
          const entitlements = await EntitlementsPage.open(personas, targets)
          await expectOk(await entitlements.disable(grant.id), `Revoking ${what}`)
          await reviewAndApply(ctx, entitlements, publication, `revoking ${what}`, { settle: [grant.id] })
          const outcome = await running.finished
          expect(verifierProblems(outcome), 'what went wrong in the verifier run').toEqual([])
          const summary = verifierSummary(outcome.lines)
          expect.soft(grantPassed(summary, 'user', 1, verifierOutcomes.keyRevoked), `the gateway refusing ${what}'s key`).toBe(true)
          if (entra) expect.soft(grantPassed(summary, 'user', 1, verifierOutcomes.tokenRevoked), `the gateway refusing ${what}'s token`).toBe(true)
        } finally {
          running.stop()
          await attachVerifierLog(testInfo, 'R7-revocation', running.lines)
        }
      })
    })
  })
})
