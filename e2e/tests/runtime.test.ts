import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { parseTargets } from '../src/config.ts'
import { e2eRoot } from '../src/paths.ts'
import { redact } from '../src/redact.ts'
import {
  applicationTokenSource,
  grantPassed,
  maskDeviceCode,
  rejectedChecks,
  revocationPromptLabel,
  runtimeForwarded,
  skippedChecks,
  tokenIsFor,
  ungrantedRuntimeTokenVariable,
  unreportedChecks,
  userRuntimeTokenVariable,
  userTokenPlan,
  verifierArgs,
  verifierProblems,
  verifierSummary,
} from '../src/runtime.ts'
import { planVerification } from '../src/verify.ts'

const targets = parseTargets(JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')))

function jwt(claims: Record<string, unknown>): string {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${part({ alg: 'RS256', typ: 'JWT' })}.${part(claims)}.c2lnbmF0dXJlLXZhbHVl`
}

// Built rather than written out, so the fictional IDs don't read as real ones.
const guid = (last: string) => ['0'.repeat(8), '0000', '4000', '8000', last.padStart(12, '0')].join('-')
const holderId = guid('a')
const otherId = guid('b')
const holder = { personaKey: 'user-a', upn: 'user-a@contoso.example' }
const holderToken = jwt({ oid: holderId, preferred_username: 'User-A@contoso.example' })
const otherToken = jwt({ oid: otherId, preferred_username: 'user-b@contoso.example' })

test('verifier args name each grant, acknowledge billed calls only when models are called, and pass the planner', () => {
  const args = verifierArgs({
    userGrants: ['ent_a', 'ent_b'],
    applicationGrants: ['ent_app'],
    foreignGrants: ['ent_other'],
    userTokenSource: 'device-code',
    applicationTokenSource: 'client-credentials',
    checkUngrantedUser: true,
    runtime: { apiVersion: '2024-10-21', chatTokenParameter: 'max_completion_tokens' },
  })
  assert.deepEqual(args, [
    '--user-entitlement', 'ent_a',
    '--user-entitlement', 'ent_b',
    '--application-entitlement', 'ent_app',
    '--foreign-user-entitlement', 'ent_other',
    '--send-model-requests',
    '--user-token-source', 'device-code',
    '--application-token-source', 'client-credentials',
    '--check-ungranted-user',
    '--api-version', '2024-10-21',
    '--chat-token-parameter', 'max_completion_tokens',
  ])
  const plan = planVerification(targets, args)
  assert.deepEqual(plan.userEntitlements, ['ent_a', 'ent_b'])
  assert.equal(plan.userTokenSource, 'device-code')

  const isolationOnly = verifierArgs({ foreignGrants: ['ent_other'] })
  assert.deepEqual(isolationOnly, ['--foreign-user-entitlement', 'ent_other'])
  assert.doesNotThrow(() => planVerification(targets, isolationOnly))
})

test('verifier args carry a proof or a revocation watch the planner accepts', () => {
  const budget = verifierArgs({ userGrants: ['ent_budget'], proof: 'shared-budget' })
  assert.ok(budget.includes('--prove-shared-budget'))
  assert.doesNotThrow(() => planVerification(targets, budget))

  const tokens = verifierArgs({ userGrants: ['ent_tokens'], proof: 'token-limit' })
  assert.ok(tokens.includes('--prove-token-limit'))
  assert.doesNotThrow(() => planVerification(targets, tokens))

  const watch = verifierArgs({ userGrants: ['ent_watch'], watchRevocation: 'ent_watch', revocationTimeoutSeconds: 1200, revocationIntervalSeconds: 20 })
  const plan = planVerification(targets, watch)
  assert.equal(plan.watchRevocation, 'ent_watch')
  assert.equal(plan.revocationTimeoutSeconds, 1200)
  assert.equal(plan.revocationIntervalSeconds, 20)
})

test('a token belongs to someone by object ID when the manifest has it, else by sign-in name', () => {
  assert.equal(tokenIsFor(holderToken, { upn: 'nobody@contoso.example', objectId: holderId.toUpperCase() }), true)
  assert.equal(tokenIsFor(holderToken, { upn: 'user-a@contoso.example', objectId: otherId }), false)
  assert.equal(tokenIsFor(holderToken, holder), true)
  assert.equal(tokenIsFor(jwt({ upn: 'USER-A@contoso.example' }), holder), true)
  assert.equal(tokenIsFor(otherToken, holder), false)
  assert.equal(tokenIsFor('not-a-token', holder), false)
})

test("the holder's token in the environment is used as it is, with the ungranted check only for someone else's token", () => {
  const plan = userTokenPlan({
    env: { [userRuntimeTokenVariable]: holderToken, [ungrantedRuntimeTokenVariable]: otherToken },
    interactive: false,
    entra: true,
    holder,
    wantUngranted: true,
  })
  assert.deepEqual(plan, { source: 'env', checkUngrantedUser: true, notes: [] })

  const sameAgain = userTokenPlan({
    env: { [userRuntimeTokenVariable]: holderToken, [ungrantedRuntimeTokenVariable]: holderToken },
    interactive: true,
    entra: true,
    holder,
    stranger: 'outsider',
    wantUngranted: true,
  })
  assert.ok('source' in sameAgain)
  assert.equal(sameAgain.source, 'env')
  assert.equal(sameAgain.checkUngrantedUser, false)
  assert.match(sameAgain.notes.join(' '), /ungranted-user check needs MOSAIC_SMOKE_UNGRANTED_USER_RUNTIME_TOKEN/)
})

test('an interactive run signs the holder in with a device code when the environment has no token of theirs', () => {
  const plan = userTokenPlan({ env: { [userRuntimeTokenVariable]: otherToken }, interactive: true, entra: true, holder, stranger: 'outsider', wantUngranted: true })
  assert.ok('source' in plan)
  assert.equal(plan.source, 'device-code')
  assert.equal(plan.checkUngrantedUser, true)
  assert.match(plan.notes.join(' '), /isn't user-a's token/)

  const noStranger = userTokenPlan({ env: {}, interactive: true, entra: true, holder, wantUngranted: true })
  assert.ok('source' in noStranger)
  assert.equal(noStranger.checkUngrantedUser, false)
  assert.match(noStranger.notes.join(' '), /roles\.outsider or roles\.noRole/)
})

test('key-only grants need no model token, and Entra grants without one are skipped with the way to provide it', () => {
  const keysOnly = userTokenPlan({ env: {}, interactive: false, entra: false, holder, wantUngranted: false })
  assert.deepEqual(keysOnly, { source: 'env', checkUngrantedUser: false, notes: [] })

  const skipped = userTokenPlan({ env: {}, interactive: false, entra: true, holder, wantUngranted: true })
  assert.ok('skip' in skipped)
  assert.match(skipped.skip, /MOSAIC_E2E_INTERACTIVE=1/)
  assert.match(skipped.skip, /MOSAIC_SMOKE_USER_RUNTIME_TOKEN/)
})

test("the workload's token comes from its secret, else from the environment", () => {
  assert.equal(
    applicationTokenSource({ MOSAIC_SMOKE_APPLICATION_CLIENT_ID: 'app', MOSAIC_SMOKE_APPLICATION_CLIENT_SECRET: 'secret', MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN: 'token' }),
    'client-credentials',
  )
  assert.equal(applicationTokenSource({ MOSAIC_SMOKE_APPLICATION_CLIENT_ID: 'app', MOSAIC_SMOKE_APPLICATION_RUNTIME_TOKEN: 'token' }), 'env')
  assert.equal(applicationTokenSource({ MOSAIC_SMOKE_APPLICATION_CLIENT_ID: '  ' }), undefined)
})

test("only the holder's own model token and someone else's ungranted token are passed on", () => {
  const env = {
    [userRuntimeTokenVariable]: otherToken,
    [ungrantedRuntimeTokenVariable]: holderToken,
    MOSAIC_SMOKE_APPLICATION_CLIENT_ID: 'app',
    PATH: '/usr/bin',
  }
  assert.deepEqual(runtimeForwarded(env, holder), { MOSAIC_SMOKE_APPLICATION_CLIENT_ID: 'app' })
  assert.deepEqual(runtimeForwarded({ [userRuntimeTokenVariable]: holderToken, [ungrantedRuntimeTokenVariable]: otherToken }, holder), {
    [userRuntimeTokenVariable]: holderToken,
    [ungrantedRuntimeTokenVariable]: otherToken,
  })
})

const passing = [
  "PASS: the user can't list, read or retrieve the key of 1 grant(s) held by someone else",
  'PASS: User grant 1 (gpt-4.1-mini) reached the model with its key',
  'PASS: User grant 1 (gpt-4.1-mini) reached the model with its Entra token',
  'PASS: User grant 2 rejected a missing key, a wrong key and a missing token',
  'PASS: Application grant 1 (gpt-4.1-mini) reached the model with its key',
  'SKIP: the user\'s usage report: this MOSAIC has none',
  'Live checks passed for 3 grant(s). Rerun after each method toggle, and check key rotation and revocation separately.',
]

test('the summary sorts the verifier output and reads its closing counts', () => {
  const summary = verifierSummary(passing)
  assert.equal(summary.passes.length, 5)
  assert.equal(summary.skips.length, 1)
  assert.equal(summary.checkedGrants, 3)
  assert.equal(summary.failure, undefined)
  assert.equal(summary.stopped, false)
  assert.equal(verifierSummary(['Isolation checks passed for 2 grant(s) held by someone else.']).isolatedGrants, 2)
})

test('a run passes only if it exits cleanly and says its checks passed', () => {
  assert.deepEqual(verifierProblems({ exitCode: 0, timedOut: false, lines: passing }), [])
  assert.deepEqual(verifierProblems({ exitCode: 0, timedOut: false, lines: ['PASS: something'] }), [
    'The verifier exited without reporting that its checks passed.',
  ])
  assert.deepEqual(verifierProblems({ exitCode: 1, timedOut: false, lines: ['FAIL: User grant 1 returned 200 without a key'] }), [
    'The verifier failed: User grant 1 returned 200 without a key',
  ])
  assert.deepEqual(verifierProblems({ exitCode: 130, timedOut: false, lines: ['STOPPED: interrupted'] }), [
    'The verifier was stopped before its checks finished.',
  ])
  assert.deepEqual(verifierProblems({ exitCode: 2, timedOut: false, lines: [] }), ['The verifier exited with code 2.'])
  assert.deepEqual(verifierProblems({ exitCode: null, timedOut: true, lines: [] }), ['The verifier ran out of time and was stopped.'])
})

test('a grant passed an outcome only if its own numbered label reported it', () => {
  const summary = verifierSummary(passing)
  assert.equal(grantPassed(summary, 'user', 1, 'reached the model with its Entra token'), true)
  assert.equal(grantPassed(summary, 'user', 2, 'rejected'), true)
  assert.equal(grantPassed(summary, 'user', 2, 'reached the model with its key'), false)
  assert.equal(grantPassed(summary, 'application', 1, 'reached the model with its key'), true)
  assert.equal(grantPassed(summary, 'user', 11, 'rejected'), false)
})

test("a grant's rejected calls are read from its own line, in the verifier's order", () => {
  const summary = verifierSummary([
    "PASS: User grant 1 (gpt-4.1-mini) rejected an anonymous call, an invalid key, a MOSAIC control-plane token, the ungranted user's token",
    'PASS: User grant 11 rejected an anonymous call',
    "PASS: Application grant 1 rejected an anonymous call, an invalid key, a MOSAIC control-plane token, the user's token with this grant's key",
  ])
  assert.deepEqual(rejectedChecks(summary, 'user', 1), [
    'an anonymous call',
    'an invalid key',
    'a MOSAIC control-plane token',
    "the ungranted user's token",
  ])
  assert.deepEqual(rejectedChecks(summary, 'application', 1)?.at(-1), "the user's token with this grant's key")
  assert.deepEqual(rejectedChecks(summary, 'user', 11), ['an anonymous call'])
  assert.equal(rejectedChecks(summary, 'user', 2), undefined)
})

test("a grant's skipped checks are read from its own lines, by name, whatever the reason says", () => {
  const summary = verifierSummary([
    "SKIP: User grant 1 (gpt-4.1-mini) the application's token with this grant's key: set MOSAIC_SMOKE_APPLICATION_CLIENT_ID: or a token",
    "SKIP: User grant 1 (gpt-4.1-mini) the ungranted user's token: it's for a different audience",
    'SKIP: User grant 11 a token while Entra tokens are off: no token',
    "SKIP: the user's usage report: this MOSAIC has none",
    "SKIP: Application grant 1 the user's token with this grant's key: no token",
  ])
  assert.deepEqual(skippedChecks(summary, 'user', 1), ["the application's token with this grant's key", "the ungranted user's token"])
  assert.deepEqual(skippedChecks(summary, 'user', 11), ['a token while Entra tokens are off'])
  assert.deepEqual(skippedChecks(summary, 'application', 1), ["the user's token with this grant's key"])
  assert.deepEqual(skippedChecks(summary, 'user', 2), [])
})

test('each applied method must reach the model, and each refusal be reported, or skipped when a credential was missing', () => {
  const both = { keysEnabled: true, entraEnabled: true }
  const keysOnly = { keysEnabled: true, entraEnabled: false }
  const summary = verifierSummary([
    "PASS: User grant 1 (gpt-4.1-mini) rejected an anonymous call, an invalid key, a MOSAIC control-plane token, an invalid token with a valid key, the ungranted user's token",
    "SKIP: User grant 1 (gpt-4.1-mini) the application's token with this grant's key: no application token in this run",
    'PASS: User grant 1 (gpt-4.1-mini) reached the model with its key',
    'PASS: User grant 1 (gpt-4.1-mini) reached the model with its Entra token',
    "PASS: User grant 2 rejected an anonymous call, an invalid key, a MOSAIC control-plane token, an invalid token with a valid key, the application's token with this grant's key",
    'SKIP: User grant 2 a token while Entra tokens are off: no user token in this run',
    'PASS: User grant 2 reached the model with its key',
    'PASS: Application grant 1 rejected an anonymous call, a MOSAIC control-plane token',
    'PASS: Application grant 1 reached the model with its Entra token',
  ])
  assert.deepEqual(unreportedChecks(summary, { kind: 'user', index: 1, methods: both, ungranted: true }), [])
  assert.deepEqual(unreportedChecks(summary, { kind: 'user', index: 2, methods: keysOnly }), [])
  assert.deepEqual(unreportedChecks(summary, { kind: 'user', index: 2, methods: keysOnly, ungranted: true }), [
    "the ungranted user's token, refused or skipped with a reason",
  ])
  assert.deepEqual(unreportedChecks(summary, { kind: 'user', index: 1, methods: keysOnly }), [
    'a token while Entra tokens are off, refused or skipped with a reason',
  ])
  assert.deepEqual(unreportedChecks(summary, { kind: 'application', index: 1, methods: { keysEnabled: false, entraEnabled: true } }), [
    'an invalid key, refused',
  ])
  assert.deepEqual(unreportedChecks(summary, { kind: 'user', index: 3, methods: both }), [
    'a call with its key reaching the model',
    'a call with its Entra token reaching the model',
    'the calls the gateway refused',
  ])
})

test("the revocation prompt names the grant to revoke, and device codes stay out of kept output", () => {
  assert.equal(
    revocationPromptLabel("WAIT: revoke User grant 1 (gpt-4.1-mini) in MOSAIC's console, which disables it, and apply its model's access plan. Checking every 30 seconds for up to 900 seconds"),
    'User grant 1 (gpt-4.1-mini)',
  )
  assert.equal(revocationPromptLabel('WAIT: MOSAIC reports User grant 1 as revoked'), undefined)
  // M9's lines about opening and closing the model caller's grant around its call aren't revocation prompts.
  for (const line of [
    "WAIT: re-enable the model caller's grant in MOSAIC's console and apply its model's access plan. Checking every 15 seconds for up to 600 seconds",
    "WAIT: MOSAIC reports the model caller's grant as pending",
    "INFO: On-behalf grant (M-agent) made its model call. Revoke the model caller's grant now and apply its model's access plan; the attribution wait goes on",
  ]) {
    assert.equal(revocationPromptLabel(line), undefined, line)
  }
  assert.equal(
    maskDeviceCode('SIGN IN as the user who holds these grants: open https://microsoft.com/devicelogin and enter the code ABCD-1234'),
    'SIGN IN as the user who holds these grants: open https://microsoft.com/devicelogin and enter the code [device code]',
  )
  assert.equal(maskDeviceCode('PASS: nothing to hide'), 'PASS: nothing to hide')
})

test('redaction leaves a device code readable for the person at the keyboard', () => {
  for (const code of ['ABCD-1234', 'HX7K2M9QP']) {
    const line = `Enter the code ${code} yourself at https://microsoft.com/devicelogin, signed in as the user who holds these grants.`
    assert.equal(redact(line), line)
  }
})
