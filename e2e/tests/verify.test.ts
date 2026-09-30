import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { TargetsError, parseTargets } from '../src/config.ts'
import { e2eRoot } from '../src/paths.ts'
import {
  VerifyError,
  bearerToken,
  checkForwarded,
  controlTokenProblem,
  controlTokenSeconds,
  controlTokenVariables,
  forwardedEnvironment,
  forwardedVariables,
  isDeviceLoginUrl,
  parseSignInPrompt,
  planVerification,
  publicLine,
  signInSubjects,
  tokenClaims,
  verifierFlags,
  verifierLaunch,
  verifierScript,
  verifierTimeoutMs,
  verifyPersonas,
} from '../src/verify.ts'

const example = () => JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')) as Record<string, any>
const targets = parseTargets(example())
const { api, gateway } = targets.origins
const minimal = ['--user-entitlement', 'ent_user', '--send-model-requests']
const plan = (...args: string[]) => planVerification(targets, args)

function jwt(claims: Record<string, unknown>): string {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString('base64url')
  return `${part({ alg: 'RS256', typ: 'JWT' })}.${part(claims)}.c2lnbmF0dXJlLXZhbHVl`
}

test('a minimal run gets the manifest origins and the verifier defaults', () => {
  const result = plan(...minimal)
  assert.deepEqual(result.argv, ['--api-base-url', api, '--gateway-origin', gateway, '--user-entitlement', 'ent_user', '--send-model-requests'])
  assert.deepEqual(result.userEntitlements, ['ent_user'])
  assert.deepEqual(result.applicationEntitlements, [])
  assert.deepEqual(result.foreignUserEntitlements, [])
  assert.equal(result.userTokenSource, 'env')
  assert.equal(result.applicationTokenSource, 'env')
  assert.equal(result.checkUngrantedUser, false)
  assert.equal(result.watchRevocation, undefined)
  assert.equal(result.revocationTimeoutSeconds, 900)
  assert.equal(result.revocationIntervalSeconds, 30)
})

test('every flag passes through, in either --flag value or --flag=value form', () => {
  const result = plan(
    '--user-entitlement=ent_a',
    '--user-entitlement',
    'ent_b',
    '--application-entitlement',
    'ent_app',
    '--foreign-user-entitlement=ent_other',
    '--api-version',
    '2024-10-21',
    '--models-api-version=2024-05-01-preview',
    '--chat-token-parameter',
    'max_tokens',
    '--user-token-source',
    'device-code',
    '--application-token-source=client-credentials',
    '--check-ungranted-user',
    '--prove-token-limit',
    '--watch-revocation',
    'ent_b',
    '--revocation-timeout',
    '600',
    '--revocation-interval=15',
    '--send-model-requests',
  )
  assert.deepEqual(result.argv, [
    '--api-base-url', api, '--gateway-origin', gateway,
    '--user-entitlement', 'ent_a', '--user-entitlement', 'ent_b',
    '--application-entitlement', 'ent_app',
    '--foreign-user-entitlement', 'ent_other',
    '--api-version', '2024-10-21',
    '--models-api-version', '2024-05-01-preview',
    '--chat-token-parameter', 'max_tokens',
    '--user-token-source', 'device-code',
    '--application-token-source', 'client-credentials',
    '--watch-revocation', 'ent_b',
    '--revocation-timeout', '600',
    '--revocation-interval', '15',
    '--check-ungranted-user', '--prove-token-limit', '--send-model-requests',
  ])
  assert.deepEqual(result.userEntitlements, ['ent_a', 'ent_b'])
  assert.deepEqual(result.applicationEntitlements, ['ent_app'])
  assert.deepEqual(result.foreignUserEntitlements, ['ent_other'])
  assert.equal(result.userTokenSource, 'device-code')
  assert.equal(result.applicationTokenSource, 'client-credentials')
  assert.equal(result.checkUngrantedUser, true)
  assert.equal(result.watchRevocation, 'ent_b')
  assert.equal(result.revocationTimeoutSeconds, 600)
  assert.equal(result.revocationIntervalSeconds, 15)
})

test('only known, full flag names reach the verifier', () => {
  const cases: [string[], RegExp][] = [
    [['ent_user', ...minimal], /Unexpected argument "ent_user"/],
    [['-v', ...minimal], /Unexpected argument "-v"/],
    [['--send-model', '--user-entitlement', 'ent_user'], /Unknown verifier flag "--send-model"/],
    [['--help', ...minimal], /Unknown verifier flag "--help"/],
    [['--', ...minimal], /Unknown verifier flag "--"/],
    [['--__proto__', 'x', ...minimal], /Unknown verifier flag/],
    [['--api-base-url', 'https://evil.example', ...minimal], /--api-base-url comes from the targets manifest/],
    [['--gateway-origin=https://evil.example', ...minimal], /--gateway-origin comes from the targets manifest/],
    [['--send-model-requests=yes', '--user-entitlement', 'ent_user'], /--send-model-requests takes no value/],
  ]
  for (const [args, message] of cases) assert.throws(() => plan(...args), message, args.join(' '))
})

test('flag values are checked before anything runs', () => {
  const cases: [string[], RegExp][] = [
    [['--send-model-requests', '--user-entitlement'], /--user-entitlement needs a grant ID/],
    [['--send-model-requests', '--user-entitlement='], /--user-entitlement needs a grant ID/],
    [['--user-entitlement', '--send-model-requests'], /--user-entitlement needs a grant ID, not "--send-model-requests"/],
    [['--send-model-requests', '--user-entitlement', 'https://evil.example/x'], /needs a grant ID/],
    [['--send-model-requests', '--user-entitlement', 'ent x'], /needs a grant ID/],
    [['--send-model-requests', '--user-entitlement', 'ent?x=1'], /needs a grant ID/],
    [['--send-model-requests', '--user-entitlement', 'x'.repeat(201)], /needs a grant ID/],
    [[...minimal, '--api-version', '2024-10-21&x=1'], /--api-version needs an API version/],
    [[...minimal, '--chat-token-parameter', 'max_output_tokens'], /max_tokens or max_completion_tokens/],
    [[...minimal, '--user-token-source', 'browser'], /env or device-code/],
    [[...minimal, '--application-token-source', 'device-code'], /env or client-credentials/],
    [[...minimal, '--revocation-timeout', '59'], /from 60 to 3600/],
    [[...minimal, '--revocation-timeout', '3601'], /from 60 to 3600/],
    [[...minimal, '--revocation-timeout', '9e2'], /from 60 to 3600/],
    [[...minimal, '--revocation-timeout', '900.5'], /from 60 to 3600/],
    [[...minimal, '--revocation-interval', '9'], /from 10 to 300/],
    [[...minimal, '--revocation-interval', '301'], /from 10 to 300/],
    [[...minimal, '--api-version', '2024-10-21', '--api-version', '2025-01-01'], /Give --api-version only once/],
  ]
  for (const [args, message] of cases) assert.throws(() => plan(...args), message, args.join(' '))
})

test('a run mirrors the verifier: acknowledged, with distinct grants and a consistent watch', () => {
  const foreign = ['--foreign-user-entitlement', 'ent_other']
  const cases: [string[], RegExp][] = [
    [['--user-entitlement', 'ent_user'], /Add --send-model-requests/],
    [[...foreign, '--user-entitlement', 'ent_user'], /Add --send-model-requests/],
    [[], /Name at least one --user-entitlement, --application-entitlement or --foreign-user-entitlement/],
    [['--send-model-requests'], /Name at least one --user-entitlement, --application-entitlement or --foreign-user-entitlement/],
    [[...minimal, '--user-entitlement', 'ent_user'], /List each grant only once/],
    [[...minimal, '--application-entitlement', 'ent_user'], /List each grant only once/],
    [[...minimal, '--foreign-user-entitlement', 'ent_user'], /List each grant only once/],
    [['--application-entitlement', 'ent_app', '--send-model-requests', '--check-ungranted-user'], /needs a --user-entitlement/],
    [[...foreign, '--check-ungranted-user'], /needs a --user-entitlement/],
    [[...minimal, '--prove-shared-budget', '--prove-token-limit'], /Choose one proof per run/],
    [[...foreign, '--prove-token-limit'], /A proof needs a --user-entitlement or --application-entitlement/],
    [[...minimal, '--watch-revocation', 'ent_other'], /must name a --user-entitlement or --application-entitlement in this run/],
    [[...minimal, ...foreign, '--watch-revocation', 'ent_other'], /must name a --user-entitlement or --application-entitlement/],
  ]
  for (const [args, message] of cases) assert.throws(() => plan(...args), message, args.join(' '))
  assert.throws(() => plan('--send-model-requests'), VerifyError)
})

test('grants held by someone else need no model requests, and the admin confirms whose they are', () => {
  const foreign = plan('--foreign-user-entitlement', 'ent_other', '--foreign-user-entitlement', 'ent_more')
  assert.deepEqual(foreign.argv, [
    '--api-base-url', api, '--gateway-origin', gateway,
    '--foreign-user-entitlement', 'ent_other', '--foreign-user-entitlement', 'ent_more',
  ])
  assert.deepEqual(foreign.foreignUserEntitlements, ['ent_other', 'ent_more'])
  assert.deepEqual(foreign.userEntitlements, [])
  assert.deepEqual(foreign.applicationEntitlements, [])
  assert.deepEqual(verifyPersonas(targets, foreign), { user: 'user-a', admin: 'admin', stranger: undefined })
  // Nothing here reads a key the user may not see, so the admin can play the user too.
  assert.deepEqual(verifyPersonas(targets, foreign, { user: 'admin' }), { user: 'admin', admin: 'admin', stranger: undefined })
  assert.equal(verifyPersonas(targets, foreign, { admin: 'user-b' }).admin, 'user-b')
  const withApplication = plan('--application-entitlement', 'ent_app', '--foreign-user-entitlement', 'ent_other', '--send-model-requests')
  assert.throws(() => verifyPersonas(targets, withApplication, { user: 'admin' }), /must be different people/)
})

test('user grants use the manifest user and need no admin', () => {
  assert.deepEqual(verifyPersonas(targets, plan(...minimal)), { user: 'user-a', admin: undefined, stranger: undefined })
  assert.equal(verifyPersonas(targets, plan(...minimal), { user: 'guest' }).user, 'guest')
  assert.throws(
    () => verifyPersonas(targets, plan(...minimal), { admin: 'admin' }),
    /--admin is only used with --application-entitlement or --foreign-user-entitlement/,
  )
  assert.throws(() => verifyPersonas(targets, plan(...minimal), { user: 'nobody' }), TargetsError)
})

test('application grants add the manifest admin, who must be someone other than the user', () => {
  const application = plan('--application-entitlement', 'ent_app', '--send-model-requests')
  assert.deepEqual(verifyPersonas(targets, application), { user: 'user-a', admin: 'admin', stranger: undefined })
  assert.throws(() => verifyPersonas(targets, application, { admin: 'user-a' }), /must be different people/)
  const twin = example()
  twin.personas['admin-alias'] = { upn: 'USER-A@contoso.example', expectedRole: 'Admin' }
  assert.throws(() => verifyPersonas(parseTargets(twin), application, { admin: 'admin-alias' }), /must be different people/)
})

test('a device-code check of an ungranted user signs in the outsider, or the no-role persona', () => {
  const stranger = plan(...minimal, '--user-token-source', 'device-code', '--check-ungranted-user')
  assert.equal(verifyPersonas(targets, stranger).stranger, 'outsider')
  assert.equal(verifyPersonas(targets, stranger, { stranger: 'user-b' }).stranger, 'user-b')
  const noOutsider = example()
  delete noOutsider.roles.outsider
  assert.equal(verifyPersonas(parseTargets(noOutsider), stranger).stranger, 'user-b')
  delete noOutsider.roles.noRole
  assert.throws(() => verifyPersonas(parseTargets(noOutsider), stranger), /Name them with --stranger <persona>/)
  assert.throws(() => verifyPersonas(targets, stranger, { stranger: 'user-a' }), /someone other than user-a/)
  const twin = example()
  twin.personas.twin = { upn: 'user-a@CONTOSO.example', expectedRole: 'None' }
  assert.throws(() => verifyPersonas(parseTargets(twin), stranger, { stranger: 'twin' }), /someone other than user-a/)
})

test('--stranger is refused when the verifier reads the ungranted token from the environment', () => {
  const fromEnv = plan(...minimal, '--check-ungranted-user')
  assert.equal(verifyPersonas(targets, fromEnv).stranger, undefined)
  assert.throws(() => verifyPersonas(targets, fromEnv, { stranger: 'outsider' }), /--stranger is only used with/)
  assert.throws(() => verifyPersonas(targets, plan(...minimal), { stranger: 'outsider' }), /--stranger is only used with/)
})

test('reads the verifier device sign-in prompts', () => {
  assert.deepEqual(
    parseSignInPrompt('SIGN IN as the user who holds these grants: open https://microsoft.com/devicelogin and enter the code GH7KX2PQL\r\n'),
    { who: 'the user who holds these grants', subject: 'user', uri: 'https://microsoft.com/devicelogin', code: 'GH7KX2PQL' },
  )
  assert.equal(
    parseSignInPrompt(
      'SIGN IN as a different user, one with no grant for these models: open https://login.microsoft.com/device and enter the code AB-12',
    )?.subject,
    'stranger',
  )
  assert.equal(parseSignInPrompt('SIGN IN as the application: open https://microsoft.com/devicelogin and enter the code ABCD')?.subject, undefined)
  for (const line of [
    'PASS grant ent_x: key reaches the model',
    'SIGN IN as x: open https://microsoft.com/devicelogin and enter the code ABC',
    'SIGN IN as x: open https://microsoft.com/devicelogin and enter the code ABCD; then run evil',
    'note: SIGN IN as x: open https://microsoft.com/devicelogin and enter the code ABCD',
  ]) {
    assert.equal(parseSignInPrompt(line), undefined, line)
  }
})

test('only Microsoft device sign-in pages are opened from verifier output', () => {
  for (const url of [
    'https://microsoft.com/devicelogin',
    'https://www.microsoft.com/devicelogin',
    'https://login.microsoft.com/device',
    'https://login.microsoftonline.com/common/oauth2/deviceauth',
  ]) {
    assert.ok(isDeviceLoginUrl(url), url)
  }
  for (const url of [
    'http://microsoft.com/devicelogin',
    'https://microsoft.com.evil.example/devicelogin',
    'https://evil.example/?https://microsoft.com/devicelogin',
    'https://microsoft.com:8443/devicelogin',
    'https://user@microsoft.com/devicelogin',
    'javascript:alert(1)',
    'not a url',
  ]) {
    assert.ok(!isDeviceLoginUrl(url), url)
  }
})

test('takes the JWT from an Authorization header, and nothing else', () => {
  const token = jwt({ tid: targets.tenantId })
  assert.equal(bearerToken(`Bearer ${token}`), token)
  assert.equal(bearerToken(`bearer ${token}`), token)
  for (const header of [null, undefined, '', token, `Basic ${token}`, 'Bearer opaque-token', `Bearer ${token} extra`]) {
    assert.equal(bearerToken(header), undefined, String(header))
  }
})

test('decodes JWT claims, or returns undefined', () => {
  assert.deepEqual(tokenClaims(jwt({ tid: 'x', exp: 1 })), { tid: 'x', exp: 1 })
  for (const token of ['', 'a.b', 'a.b.c.d', `x.${Buffer.from('[1]').toString('base64url')}.y`, 'x.!!!.y']) {
    assert.equal(tokenClaims(token), undefined, token)
  }
})

test('a MOSAIC API token must be the persona own, from the tenant, and last the run', () => {
  const now = 1_800_000_000
  const run = plan(...minimal)
  const holder = { personaKey: 'user-a', upn: 'user-a@contoso.example', tenantId: targets.tenantId }
  const claims = { tid: targets.tenantId, preferred_username: 'User-A@Contoso.example', exp: now + 3_600 }
  assert.equal(controlTokenProblem(jwt(claims), holder, now, run), undefined)
  assert.equal(controlTokenProblem(jwt({ ...claims, preferred_username: undefined, upn: holder.upn }), holder, now, run), undefined)
  const withObject = { ...holder, objectId: '11111111-2222-3333-4444-555555555555' }
  const renamed = { ...claims, preferred_username: 'renamed@contoso.example', oid: withObject.objectId.toUpperCase() }
  assert.equal(controlTokenProblem(jwt(renamed), withObject, now, run), undefined)

  const problems: [string, RegExp][] = [
    ['not-a-token', /isn't a readable JWT\.$/],
    [jwt({ ...claims, tid: '99999999-9999-9999-9999-999999999999' }), /different tenant than the manifest's tenantId\.$/],
    [jwt({ ...claims, tid: undefined }), /different tenant/],
    [jwt({ ...claims, preferred_username: 'someone@contoso.example' }), /different account than persona user-a/],
    [jwt({ ...claims, exp: undefined }), /has no expiry\.$/],
    [jwt({ ...claims, exp: now + 600 }), /expires in 600 seconds, and this run needs it for 1200\.$/],
    [jwt({ ...claims, exp: now - 30 }), /expires in 0 seconds/],
  ]
  for (const [token, message] of problems) {
    const problem = controlTokenProblem(token, holder, now, run)
    assert.match(problem ?? '', message, token)
    assert.ok(!(problem ?? '').includes(token), 'the message must not quote the token')
  }

  // A revocation watch needs the token for longer, and only a short lifetime is solved by shortening the watch.
  const watch = plan(...minimal, '--watch-revocation', 'ent_user')
  assert.equal(controlTokenProblem(jwt(claims), holder, now, watch), undefined)
  assert.match(
    controlTokenProblem(jwt({ ...claims, exp: now + 2_000 }), holder, now, watch) ?? '',
    /expires in 2000 seconds, and this run needs it for 2190\. Lower --revocation-timeout and run it again\.$/,
  )
  assert.doesNotMatch(controlTokenProblem('not-a-token', holder, now, watch) ?? '', /revocation-timeout/)
  assert.doesNotMatch(controlTokenProblem(jwt({ ...claims, exp: now + 600 }), holder, now, run) ?? '', /revocation-timeout/)
})

test('tokens and the verifier get enough time for sign-ins, checks and the revocation watch', () => {
  assert.equal(controlTokenSeconds(plan(...minimal)), 1_200)
  assert.equal(controlTokenSeconds(plan(...minimal, '--watch-revocation', 'ent_user')), 1_200 + 900 + 30 + 60)
  assert.equal(verifierTimeoutMs(plan(...minimal)), 45 * 60_000)
  const watch = plan(...minimal, '--watch-revocation', 'ent_user', '--revocation-timeout', '3600', '--revocation-interval', '300')
  assert.equal(verifierTimeoutMs(watch), (45 * 60 + 3_600 + 300) * 1_000)
})

test('drive.ts forwards only the allowlisted, non-empty variables', () => {
  const env = {
    MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'runtime-token',
    MOSAIC_SMOKE_APPLICATION_CLIENT_ID: 'client-id',
    MOSAIC_SMOKE_PAYLOAD: '  ',
    [controlTokenVariables.user]: 'stale-control-token',
    [controlTokenVariables.admin]: 'stale-admin-token',
    PATH: 'C:\\Windows',
  }
  assert.deepEqual(forwardedEnvironment(env), {
    MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'runtime-token',
    MOSAIC_SMOKE_APPLICATION_CLIENT_ID: 'client-id',
  })
})

test('the daemon accepts forwarded variables only from the allowlist', () => {
  assert.deepEqual(checkForwarded(undefined), {})
  assert.deepEqual(checkForwarded(null), {})
  assert.deepEqual(checkForwarded({ MOSAIC_SMOKE_PAYLOAD: '{}' }), { MOSAIC_SMOKE_PAYLOAD: '{}' })
  const refused: [unknown, RegExp][] = [
    [[], /must be an object/],
    ['MOSAIC_SMOKE_PAYLOAD={}', /must be an object/],
    [{ [controlTokenVariables.user]: 'token' }, /can't be passed to the verifier/],
    [{ [controlTokenVariables.admin]: 'token' }, /can't be passed to the verifier/],
    [{ PATH: 'C:\\evil' }, /can't be passed to the verifier/],
    [{ NODE_OPTIONS: '--require evil' }, /can't be passed to the verifier/],
    [{ MOSAIC_SMOKE_PAYLOAD: 42 }, /non-empty string/],
    [{ MOSAIC_SMOKE_PAYLOAD: ' ' }, /non-empty string/],
    [{ MOSAIC_SMOKE_PAYLOAD: 'x'.repeat(16_385) }, /at most 16384 characters/],
  ]
  for (const [value, message] of refused) assert.throws(() => checkForwarded(value), message, JSON.stringify(value))
})

test('every credential the verifier gets is redacted from its output; client IDs and payloads are not credentials', () => {
  const forwarded = Object.fromEntries(forwardedVariables.map((name) => [name, `value-of-${name}`]))
  const control = { user: 'user-mosaic-api-token', admin: 'admin-mosaic-api-token' }
  const { env, secrets } = verifierLaunch({ PATH: 'C:\\Windows' }, forwarded, control)
  const passed = Object.entries(env).filter(([name]) => name.startsWith('MOSAIC_SMOKE_'))
  assert.equal(passed.length, forwardedVariables.length + 2)
  const notCredentials = new Set(['MOSAIC_SMOKE_APPLICATION_CLIENT_ID', 'MOSAIC_SMOKE_PAYLOAD'])
  for (const [name, value] of passed) {
    const echoed = publicLine(`FAIL ${value} was rejected`, secrets)
    assert.equal(echoed, notCredentials.has(name) ? `FAIL ${value} was rejected` : 'FAIL [redacted-secret] was rejected', name)
  }

  const userOnly = verifierLaunch({}, {}, { user: control.user })
  assert.deepEqual(userOnly.secrets, [control.user])
  assert.equal(userOnly.env[controlTokenVariables.admin], undefined)
})

test('the verifier environment drops inherited MOSAIC_SMOKE_ variables and adds this run', () => {
  const { env } = verifierLaunch(
    {
      PATH: 'C:\\Windows',
      MOSAIC_SMOKE_USER_CONTROL_TOKEN: 'inherited',
      MOSAIC_SMOKE_ADMIN_CONTROL_TOKEN: 'inherited-admin',
      mosaic_smoke_application_client_secret: 'inherited-lowercase',
      MOSAIC_SMOKE_PAYLOAD: 'inherited-payload',
      EMPTY: undefined,
    },
    { MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'runtime' },
    { user: 'user-control' },
  )
  assert.deepEqual(env, {
    PATH: 'C:\\Windows',
    MOSAIC_SMOKE_USER_RUNTIME_TOKEN: 'runtime',
    MOSAIC_SMOKE_USER_CONTROL_TOKEN: 'user-control',
    PYTHONUNBUFFERED: '1',
    PYTHONIOENCODING: 'utf-8',
  })
})

test('verifier output is redacted and bounded before anyone sees it', () => {
  const token = jwt({ tid: targets.tenantId })
  const line = publicLine(`FAIL secret-runtime-value and ${token} rejected   \r`, ['secret-runtime-value'])
  assert.equal(line, 'FAIL [redacted-secret] and [redacted-jwt] rejected')
  assert.match(publicLine('PASS grant ent_0123456789abcdef0123456789abcdef', []), /ent_0123456789abcdef0123456789abcdef/)
  assert.ok(publicLine('x'.repeat(5_000), []).length < 2_100)
})

test('the harness matches the verifier it drives', () => {
  const source = readFileSync(verifierScript, 'utf8')
  assert.ok(source.includes('f"SIGN IN as {who}: open {verification_uri} and enter the code {user_code}"'), 'device sign-in prompt')
  for (const who of Object.keys(signInSubjects)) assert.ok(source.includes(`"${who}"`), who)
  assert.ok(source.includes('USER_CODE = re.compile(r"[A-Za-z0-9-]{4,32}")'), 'user code pattern')
  for (const flag of verifierFlags) assert.ok(source.includes(`"${flag}"`), flag)
  const declared = [...source.matchAll(/add_argument\(\s*"(--[a-z-]+)"/g)].map((match) => match[1]).sort()
  assert.deepEqual(declared, [...verifierFlags].sort(), 'the harness knows every verifier flag')
  for (const name of [...Object.values(controlTokenVariables), ...forwardedVariables]) assert.ok(source.includes(`"${name}"`), name)
  const variables = [...source.matchAll(/"(MOSAIC_SMOKE_[A-Z_]+)"/g)].map((match) => match[1])
  assert.deepEqual([...new Set(variables)].sort(), [...Object.values(controlTokenVariables), ...forwardedVariables].sort())
  assert.ok(source.includes('if not 60 <= args.revocation_timeout <= 3600:'), 'revocation timeout range')
  assert.ok(source.includes('if not 10 <= args.revocation_interval <= 300:'), 'revocation interval range')
  assert.ok(source.includes('"--revocation-timeout", type=int, default=900'), 'revocation timeout default')
  assert.ok(source.includes('"--revocation-interval", type=int, default=30'), 'revocation interval default')
})
