import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { TargetsError, mcpCallLimitCalls, mcpCallLimitSeconds, mcpPoolCallCeiling, mcpPoolCallFloor, parseTargets } from '../src/config.ts'
import { e2eRoot } from '../src/paths.ts'
import {
  checkForwarded,
  controlTokenProblem,
  controlTokenVariables,
  forwardedEnvironment,
  parseSignInPrompt,
  publicLine,
  verifierLaunch,
} from '../src/verify.ts'
import {
  mcpControlTokenNeeds,
  mcpDefaults,
  mcpForwardedVariables,
  mcpSecretVariables,
  mcpSignInSubjects,
  mcpVerifierFlags,
  mcpVerifierScript,
  mcpVerifierTimeoutMs,
  mcpVerifyPersonas,
  planMcpVerification,
} from '../src/verify-mcp.ts'

const example = () => JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')) as Record<string, any>
const targets = parseTargets(example())
const { api, gateway } = targets.origins
const user = ['--user-entitlement', 'ent_user']
const onBehalf = ['--on-behalf-entitlement', 'ent_agent', '--send-model-requests']
const plan = (...args: string[]) => planMcpVerification(targets, args)

function jwt(claims: Record<string, unknown>): string {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${part({ alg: 'RS256', typ: 'JWT' })}.${part(claims)}.c2lnbmF0dXJlLXZhbHVl`
}

test('a minimal run gets the manifest origins and the verifier defaults', () => {
  const result = plan(...user)
  assert.deepEqual(result.argv, ['--api-base-url', api, '--gateway-origin', gateway, '--user-entitlement', 'ent_user'])
  assert.deepEqual(result.userEntitlements, ['ent_user'])
  assert.deepEqual(result.applicationEntitlements, [])
  assert.equal(result.onBehalfEntitlement, undefined)
  assert.equal(result.userTokenSource, 'env')
  assert.equal(result.applicationTokenSource, 'env')
  assert.equal(result.checkUngrantedUser, false)
  assert.equal(result.checkMissingScope, false)
  assert.equal(result.awaitAttribution, false)
  assert.equal(result.awaitModelGrant, false)
  assert.deepEqual(
    {
      revocationTimeoutSeconds: result.revocationTimeoutSeconds,
      revocationIntervalSeconds: result.revocationIntervalSeconds,
      attributionTimeoutSeconds: result.attributionTimeoutSeconds,
      attributionIntervalSeconds: result.attributionIntervalSeconds,
      modelGrantTimeoutSeconds: result.modelGrantTimeoutSeconds,
      modelGrantIntervalSeconds: result.modelGrantIntervalSeconds,
    },
    mcpDefaults,
  )
})

test('every flag passes through, in either --flag value or --flag=value form', () => {
  const result = plan(
    '--user-entitlement=ent_a',
    '--user-entitlement',
    'ent_b',
    '--application-entitlement',
    'ent_app',
    '--on-behalf-entitlement=ent_agent',
    '--model-caller-entitlement',
    'ent_model',
    '--user-token-source',
    'device-code',
    '--application-token-source=client-credentials',
    '--check-ungranted-user',
    '--check-missing-scope',
    '--prove-call-limit',
    'ent_b',
    '--await-attribution',
    '--await-model-grant',
    '--attribution-timeout',
    '900',
    '--attribution-interval=45',
    '--model-grant-timeout',
    '300',
    '--model-grant-interval=20',
    '--send-model-requests',
  )
  assert.deepEqual(result.argv, [
    '--api-base-url', api, '--gateway-origin', gateway,
    '--user-entitlement', 'ent_a', '--user-entitlement', 'ent_b',
    '--application-entitlement', 'ent_app',
    '--on-behalf-entitlement', 'ent_agent',
    '--model-caller-entitlement', 'ent_model',
    '--user-token-source', 'device-code',
    '--application-token-source', 'client-credentials',
    '--prove-call-limit', 'ent_b',
    '--attribution-timeout', '900',
    '--attribution-interval', '45',
    '--model-grant-timeout', '300',
    '--model-grant-interval', '20',
    '--check-ungranted-user', '--check-missing-scope', '--await-attribution', '--await-model-grant', '--send-model-requests',
  ])
  assert.equal(result.onBehalfEntitlement, 'ent_agent')
  assert.equal(result.modelCallerEntitlement, 'ent_model')
  assert.equal(result.proveCallLimit, 'ent_b')
  assert.equal(result.userTokenSource, 'device-code')
  assert.equal(result.applicationTokenSource, 'client-credentials')
  assert.equal(result.attributionTimeoutSeconds, 900)
  assert.equal(result.attributionIntervalSeconds, 45)
  assert.equal(result.awaitModelGrant, true)
  assert.equal(result.modelGrantTimeoutSeconds, 300)
  assert.equal(result.modelGrantIntervalSeconds, 20)
  const pooled = plan(...user, '--prove-pooled-quota', 'ent_user', '--watch-revocation', 'ent_user', '--revocation-timeout=600', '--revocation-interval', '15')
  assert.equal(pooled.provePooledQuota, 'ent_user')
  assert.equal(pooled.watchRevocation, 'ent_user')
  assert.equal(pooled.revocationTimeoutSeconds, 600)
  assert.equal(pooled.revocationIntervalSeconds, 15)
})

test('grant IDs can come from the manifest, and are checked like any other', () => {
  const result = plan(
    '--user-entitlement', '@target:mcp.grants.tools-user.id',
    '--prove-call-limit=@target:mcp.grants.tools-user.id',
  )
  assert.deepEqual(result.userEntitlements, ['ent_00000000000000000000000000000001'])
  assert.equal(result.proveCallLimit, 'ent_00000000000000000000000000000001')
  assert.ok(result.argv.includes('ent_00000000000000000000000000000001'))
  assert.ok(!result.argv.some((value) => value.startsWith('@target:')))
  assert.throws(() => plan('--user-entitlement', '@target:mcp.grants.missing.id'), TargetsError)
  // A reference must still resolve to a grant ID.
  assert.throws(() => plan('--user-entitlement', '@target:mcp.servers.m-tools.url'), /--user-entitlement needs a grant ID/)
  assert.throws(() => plan('--user-entitlement', '@target:mcp.grants.tools-user'), TargetsError)
  // The runbook's M9 command names the agent's model grant from its server, and waits for it to be applied.
  const agent = plan(
    '--on-behalf-entitlement', '@target:mcp.grants.agent-user.id', '--send-model-requests',
    '--await-attribution', '--model-caller-entitlement', '@target:mcp.servers.m-agent.modelCaller.modelGrant',
    '--await-model-grant',
  )
  assert.equal(agent.onBehalfEntitlement, 'ent_00000000000000000000000000000006')
  assert.equal(agent.modelCallerEntitlement, 'ent_00000000000000000000000000000009')
  assert.equal(agent.awaitModelGrant, true)
})

test('only known, full flag names reach the verifier, with checked values', () => {
  const cases: [string[], RegExp][] = [
    [['ent_user', ...user], /Unexpected argument "ent_user"/],
    [['--user', 'ent_user'], /Unknown verifier flag "--user"/],
    [['--prove-shared-budget', ...user], /Unknown verifier flag "--prove-shared-budget"/],
    [['--api-base-url', 'https://evil.example', ...user], /--api-base-url comes from the targets manifest/],
    [['--gateway-origin=https://evil.example', ...user], /--gateway-origin comes from the targets manifest/],
    [[...user, '--check-missing-scope=yes'], /--check-missing-scope takes no value/],
    [['--user-entitlement', 'https://evil.example/x'], /--user-entitlement needs a grant ID/],
    [[...user, '--on-behalf-entitlement', 'a', '--on-behalf-entitlement', 'b', '--send-model-requests'], /Give --on-behalf-entitlement only once/],
    [[...user, '--user-token-source', 'browser'], /env or device-code/],
    [[...user, '--application-token-source', 'device-code'], /env or client-credentials/],
    [[...user, '--revocation-timeout', '59'], /from 60 to 3600/],
    [[...user, '--revocation-interval', '301'], /from 10 to 300/],
    [[...user, '--attribution-timeout', '3601'], /from 60 to 3600/],
    [[...user, '--attribution-interval', '29'], /from 30 to 600/],
    [[...user, '--attribution-interval', '6e1'], /from 30 to 600/],
    [[...user, '--model-grant-timeout', '59'], /--model-grant-timeout needs a whole number of seconds from 60 to 1800/],
    [[...user, '--model-grant-timeout', '1801'], /from 60 to 1800/],
    [[...user, '--model-grant-interval', '9'], /--model-grant-interval needs a whole number of seconds from 10 to 120/],
    [[...user, '--model-grant-interval=121'], /from 10 to 120/],
    [[...user, '--await-model-grant=yes'], /--await-model-grant takes no value/],
  ]
  for (const [args, message] of cases) assert.throws(() => plan(...args), message, args.join(' '))
})

test('a run mirrors the verifier: acknowledged, with distinct grants, one proof and one wait', () => {
  const cases: [string[], RegExp][] = [
    [[], /Name at least one --user-entitlement, --application-entitlement or --on-behalf-entitlement/],
    [['--send-model-requests'], /Name at least one/],
    [[...user, '--user-entitlement', 'ent_user'], /List each grant only once/],
    [[...user, '--on-behalf-entitlement', 'ent_user', '--send-model-requests'], /List each grant only once/],
    [[...onBehalf, '--model-caller-entitlement', 'ent_agent', '--await-attribution'], /List each grant only once/],
    [['--on-behalf-entitlement', 'ent_agent'], /Add --send-model-requests to acknowledge that ask_model sends a real, billed model request/],
    [[...user, '--await-attribution'], /need an --on-behalf-entitlement/],
    [[...user, '--model-caller-entitlement', 'ent_model'], /need an --on-behalf-entitlement/],
    [[...onBehalf, '--model-caller-entitlement', 'ent_model'], /--model-caller-entitlement is only used with --await-attribution/],
    [[...user, '--await-model-grant'], /--await-model-grant needs a --model-caller-entitlement/],
    [[...onBehalf, '--await-attribution', '--await-model-grant'], /--await-model-grant needs a --model-caller-entitlement/],
    [[...onBehalf, '--model-caller-entitlement', 'ent_model', '--await-model-grant'], /--model-caller-entitlement is only used with --await-attribution/],
    [['--application-entitlement', 'ent_app', '--check-ungranted-user'], /need a --user-entitlement/],
    [[...onBehalf, '--check-missing-scope'], /need a --user-entitlement/],
    [[...user, '--prove-call-limit', 'ent_user', '--prove-pooled-quota', 'ent_user'], /Choose one proof per run/],
    [[...user, '--prove-call-limit', 'ent_other'], /--prove-call-limit must name a --user-entitlement or --application-entitlement/],
    [[...user, ...onBehalf, '--prove-pooled-quota', 'ent_agent'], /--prove-pooled-quota must name a --user-entitlement/],
    [[...user, ...onBehalf, '--watch-revocation', 'ent_agent'], /--watch-revocation must name a --user-entitlement/],
    [[...user, ...onBehalf, '--watch-revocation', 'ent_user', '--await-attribution'], /Wait for one thing per run/],
    [
      [...user, ...onBehalf, '--watch-revocation', 'ent_user', '--await-attribution', '--model-caller-entitlement', 'ent_model', '--await-model-grant'],
      /Wait for one thing per run/,
    ],
  ]
  for (const [args, message] of cases) assert.throws(() => plan(...args), message, args.join(' '))
})

test('the people in a run: the user, the admin where MOSAIC needs one, and a stranger to refuse', () => {
  assert.deepEqual(mcpVerifyPersonas(targets, plan(...user)), { user: 'user-a', admin: undefined, stranger: undefined })
  for (const args of [
    ['--application-entitlement', 'ent_app'],
    [...user, '--prove-pooled-quota', 'ent_user'],
    onBehalf,
  ]) {
    assert.equal(mcpVerifyPersonas(targets, plan(...args)).admin, 'admin', args.join(' '))
  }
  // An application's grant has no key for the user to reach, so one person may play both parts.
  assert.equal(mcpVerifyPersonas(targets, plan('--application-entitlement', 'ent_app'), { admin: 'user-a' }).admin, 'user-a')
  assert.throws(
    () => mcpVerifyPersonas(targets, plan(...user), { admin: 'admin' }),
    /--admin is only used with --application-entitlement, --prove-pooled-quota or --on-behalf-entitlement/,
  )
  assert.throws(() => mcpVerifyPersonas(targets, plan(...user), { user: 'nobody' }), TargetsError)

  const stranger = plan(...user, '--user-token-source', 'device-code', '--check-ungranted-user')
  assert.equal(mcpVerifyPersonas(targets, stranger).stranger, 'outsider')
  assert.equal(mcpVerifyPersonas(targets, stranger, { stranger: 'user-b' }).stranger, 'user-b')
  assert.throws(() => mcpVerifyPersonas(targets, stranger, { stranger: 'user-a' }), /someone other than user-a/)
  const noOutsider = example()
  delete noOutsider.roles.outsider
  delete noOutsider.roles.noRole
  assert.throws(() => mcpVerifyPersonas(parseTargets(noOutsider), stranger), /no grant for these MCP servers\. Name them with --stranger/)
  const fromEnv = plan(...user, '--check-ungranted-user')
  assert.equal(mcpVerifyPersonas(targets, fromEnv).stranger, undefined)
  assert.throws(() => mcpVerifyPersonas(targets, fromEnv, { stranger: 'outsider' }), /--stranger is only used with/)
})

test('tokens and the verifier get time for sign-ins, checks, and the waits', () => {
  assert.deepEqual(mcpControlTokenNeeds(plan(...user)), { seconds: 1_200 })
  const watch = plan(...user, '--watch-revocation', 'ent_user')
  assert.deepEqual(mcpControlTokenNeeds(watch), { seconds: 1_200 + 900 + 30 + 60, shorten: 'Lower --revocation-timeout and run it again.' })
  const attribution = plan(...onBehalf, '--await-attribution', '--attribution-timeout', '1200', '--attribution-interval', '120')
  assert.deepEqual(mcpControlTokenNeeds(attribution), {
    seconds: 1_200 + 1_200 + 120 + 60,
    shorten: 'Lower --attribution-timeout and run it again.',
  })
  // M9 can wait for the model caller's grant before its call, then for the call's attribution.
  const grantWait = [...onBehalf, '--await-attribution', '--model-caller-entitlement', 'ent_model', '--await-model-grant']
  const both = plan(...grantWait, '--model-grant-timeout', '300', '--model-grant-interval', '20')
  const shortenBoth = 'Lower --model-grant-timeout or --attribution-timeout and run it again.'
  assert.deepEqual(mcpControlTokenNeeds(both), { seconds: 1_200 + 300 + 20 + 1_800 + 60 + 60, shorten: shortenBoth })
  assert.deepEqual(mcpControlTokenNeeds(plan(...grantWait)), { seconds: 1_200 + 600 + 15 + 1_800 + 60 + 60, shorten: shortenBoth })
  assert.equal(mcpVerifierTimeoutMs(plan(...user)), 45 * 60_000)
  assert.equal(mcpVerifierTimeoutMs(watch), (45 * 60 + 930) * 1_000)
  assert.equal(mcpVerifierTimeoutMs(attribution), (45 * 60 + 1_320) * 1_000)
  assert.equal(mcpVerifierTimeoutMs(both), (45 * 60 + 320 + 1_860) * 1_000)

  // The MOSAIC API token check takes the MCP run's needs, and says what would shorten them.
  const now = 1_800_000_000
  const holder = { personaKey: 'user-a', upn: 'user-a@contoso.example', tenantId: targets.tenantId }
  const token = jwt({ tid: targets.tenantId, preferred_username: holder.upn, exp: now + 2_000 })
  assert.equal(controlTokenProblem(token, holder, now, mcpControlTokenNeeds(plan(...user))), undefined)
  assert.match(
    controlTokenProblem(token, holder, now, mcpControlTokenNeeds(attribution)) ?? '',
    /expires in 2000 seconds, and this run needs it for 2580\. Lower --attribution-timeout and run it again\.$/,
  )
  const longer = jwt({ tid: targets.tenantId, preferred_username: holder.upn, exp: now + 3_000 })
  assert.equal(controlTokenProblem(longer, holder, now, mcpControlTokenNeeds(attribution)), undefined)
  assert.match(
    controlTokenProblem(longer, holder, now, mcpControlTokenNeeds(both)) ?? '',
    /expires in 3000 seconds, and this run needs it for 3440\. Lower --model-grant-timeout or --attribution-timeout and run it again\.$/,
  )
})

test('drive.ts forwards, and the daemon accepts, only the MCP verifier variables', () => {
  const env = {
    MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN: 'mcp-user-token',
    MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'model-token',
    MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN: 'model-verifier-only',
    MOSAIC_SMOKE_PAYLOAD: '{}',
    [controlTokenVariables.user]: 'stale-control-token',
    PATH: 'C:\\Windows',
  }
  assert.deepEqual(forwardedEnvironment(env, mcpForwardedVariables), {
    MOSAIC_SMOKE_MCP_USER_RUNTIME_TOKEN: 'mcp-user-token',
    MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'model-token',
  })
  assert.deepEqual(checkForwarded({ MOSAIC_SMOKE_MCP_APPLICATION_RUNTIME_TOKEN: 'token' }, mcpForwardedVariables), {
    MOSAIC_SMOKE_MCP_APPLICATION_RUNTIME_TOKEN: 'token',
  })
  for (const name of ['MOSAIC_SMOKE_AGENT_RUNTIME_TOKEN', 'MOSAIC_SMOKE_PAYLOAD', controlTokenVariables.admin, 'NODE_OPTIONS']) {
    assert.throws(() => checkForwarded({ [name]: 'value' }, mcpForwardedVariables), /can't be passed to the verifier/, name)
  }
})

test('every credential the MCP verifier gets is redacted from its output; the client ID is not one', () => {
  const forwarded = Object.fromEntries(mcpForwardedVariables.map((name) => [name, `value-of-${name}`]))
  const { env, secrets } = verifierLaunch({ PATH: 'C:\\Windows' }, forwarded, { user: 'user-api-token', admin: 'admin-api-token' }, mcpSecretVariables)
  assert.equal(Object.keys(env).filter((name) => name.startsWith('MOSAIC_SMOKE_')).length, mcpForwardedVariables.length + 2)
  for (const name of mcpForwardedVariables) {
    const echoed = publicLine(`FAIL ${forwarded[name]} was rejected`, secrets)
    const expected = name === 'MOSAIC_SMOKE_APPLICATION_CLIENT_ID' ? `FAIL ${forwarded[name]} was rejected` : 'FAIL [redacted-secret] was rejected'
    assert.equal(echoed, expected, name)
  }
  for (const control of ['user-api-token', 'admin-api-token']) assert.equal(publicLine(control, secrets), '[redacted-secret]')
})

test('reads the MCP verifier device sign-in prompts and whose they are', () => {
  assert.deepEqual(
    parseSignInPrompt('SIGN IN as the user who holds these grants: open https://microsoft.com/devicelogin and enter the code ABCD1234', mcpSignInSubjects),
    { who: 'the user who holds these grants', subject: 'user', uri: 'https://microsoft.com/devicelogin', code: 'ABCD1234' },
  )
  const stranger = 'SIGN IN as a different user, one with no grant for these MCP servers: open https://microsoft.com/devicelogin and enter the code EFGH5678'
  assert.equal(parseSignInPrompt(stranger, mcpSignInSubjects)?.subject, 'stranger')
  // The model verifier's names for the stranger aren't the MCP verifier's, so neither maps the other's.
  assert.equal(parseSignInPrompt(stranger)?.subject, undefined)
})

test('MOSAIC grant IDs in the verifier output stay readable, so the coordinator can find the attribution', () => {
  const trace = `INFO: On-behalf grant (M-agent)'s MCP call trace reads: mosaic-attribution v=1 g=${'ab'.repeat(32)} m= a=33333333-3333-4333-8333-333333333333`
  assert.equal(publicLine(trace, []), trace)
  const window = 'INFO: On-behalf grant (M-agent) called ask_model between 2026-10-05T18:01:02Z and 2026-10-05T18:01:09Z UTC'
  assert.equal(publicLine(window, []), window)
})

test('the harness matches the MCP verifier it drives', () => {
  const source = readFileSync(mcpVerifierScript, 'utf8')
  for (const who of Object.keys(mcpSignInSubjects)) assert.ok(source.includes(`"${who}"`), who)
  const declared = [...source.matchAll(/add_argument\(\s*"(--[a-z-]+)"/g)].map((match) => match[1]).sort()
  assert.deepEqual(declared, [...mcpVerifierFlags].sort(), 'the harness knows every verifier flag')
  const variables = [...source.matchAll(/"(MOSAIC_SMOKE_[A-Z_]+)"/g)].map((match) => match[1])
  assert.deepEqual([...new Set(variables)].sort(), [...Object.values(controlTokenVariables), ...mcpForwardedVariables].sort())
  for (const range of [
    'if not 60 <= args.revocation_timeout <= 3600:',
    'if not 10 <= args.revocation_interval <= 300:',
    'if not 60 <= args.attribution_timeout <= 3600:',
    'if not 30 <= args.attribution_interval <= 600:',
    'if not 60 <= args.model_grant_timeout <= 1800:',
    'if not 10 <= args.model_grant_interval <= 120:',
  ]) {
    assert.ok(source.includes(range), range)
  }
  // The harness refuses a run as the verifier would, in the verifier's words.
  assert.throws(() => plan(...user, '--await-model-grant'), /: --await-model-grant needs a --model-caller-entitlement\.$/)
  assert.ok(source.includes('"--await-model-grant needs a --model-caller-entitlement"'), 'the switch needs a model caller')
  for (const constant of [
    `DEFAULT_REVOCATION_TIMEOUT = ${mcpDefaults.revocationTimeoutSeconds}`,
    `DEFAULT_REVOCATION_INTERVAL = ${mcpDefaults.revocationIntervalSeconds}`,
    `DEFAULT_ATTRIBUTION_TIMEOUT = ${mcpDefaults.attributionTimeoutSeconds}`,
    `DEFAULT_ATTRIBUTION_INTERVAL = ${mcpDefaults.attributionIntervalSeconds}`,
    `DEFAULT_MODEL_GRANT_TIMEOUT = ${mcpDefaults.modelGrantTimeoutSeconds}`,
    `DEFAULT_MODEL_GRANT_INTERVAL = ${mcpDefaults.modelGrantIntervalSeconds}`,
    // The manifest checks a call-limit or pooled grant against the proofs' bounds.
    `FLOW_CALLS = ${mcpCallLimitCalls[0] - 1}`,
    `CALL_LIMIT_CEILING = ${mcpCallLimitCalls[1]}`,
    `CALL_LIMIT_PERIODS = (${mcpCallLimitSeconds[0]}, ${mcpCallLimitSeconds[1]})`,
    `POOL_CALL_CEILING = ${mcpPoolCallCeiling}`,
    `POOL_CALL_FLOOR = ${mcpPoolCallFloor}`,
  ]) {
    assert.ok(source.includes(constant), constant)
  }
})
