import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Request } from '@playwright/test'
import { carriesApiToken, deviceCodePersona, deviceSignInGraceMs, endsDeviceSignIn, followDeviceSignIns } from '../src/device-code.ts'
import { mcpSignInSubjects } from '../src/verify-mcp.ts'
import { type SignInPrompt, type VerifyPersonas, parseSignInPrompt } from '../src/verify.ts'

const people = { user: 'user-a', stranger: 'outsider' }
const prompt = (overrides: Partial<SignInPrompt> = {}): SignInPrompt => ({
  who: 'the granted user',
  subject: 'user',
  uri: 'https://microsoft.com/devicelogin',
  code: 'ABCD-1234',
  ...overrides,
})

test('device codes go to the persona the verifier asked for', () => {
  assert.deepEqual(deviceCodePersona(prompt(), people), { personaKey: 'user-a' })
  assert.deepEqual(deviceCodePersona(prompt({ subject: 'stranger' }), people), { personaKey: 'outsider' })
})

test('a device code is refused for any other address, before anyone is chosen', () => {
  assert.deepEqual(deviceCodePersona(prompt({ uri: 'https://contoso.example/devicelogin' }), people), { refused: 'not-device-login' })
  assert.deepEqual(deviceCodePersona(prompt({ uri: 'https://contoso.example/devicelogin', subject: undefined }), people), {
    refused: 'not-device-login',
  })
})

test('a device code is refused when the run has no persona for that sign-in', () => {
  assert.deepEqual(deviceCodePersona(prompt({ subject: 'stranger' }), { user: 'user-a' }), { refused: 'no-persona' })
  assert.deepEqual(deviceCodePersona(prompt({ subject: undefined }), people), { refused: 'no-persona' })
})

test("a device sign-in ends at the verifier's next line, but not at its note that it's still waiting", () => {
  const later = 480_000
  const waiting = 'INFO: The sign-in service answered HTTP 502; still waiting for the user who holds these grants to sign in'
  assert.equal(endsDeviceSignIn(waiting, later), false)
  assert.equal(endsDeviceSignIn(`${waiting}\r`, later), false)
  const stranger = 'a different user, one with no grant for these MCP servers'
  assert.equal(endsDeviceSignIn(`INFO: The sign-in service answered HTTP 503; still waiting for ${stranger} to sign in`, later), false)
  assert.equal(endsDeviceSignIn('PASS: User grant 1 (gpt-4.1-mini) reached the model with its Entra token', later), true)
  const failed = 'FAIL: Signing in the user who holds these grants failed: HTTP 502. See docs/connect-to-mcp-servers.md#troubleshooting'
  assert.equal(endsDeviceSignIn(failed, later), true)
  assert.equal(endsDeviceSignIn('INFO: User grant 1 (gpt-4.1-mini) had no key, so the check created one', later), true)
  assert.equal(endsDeviceSignIn(`${waiting}. PASS: anything`, later), true)
  // Output this soon after the prompt was written before it, on the verifier's other stream.
  assert.equal(endsDeviceSignIn('PASS: something', deviceSignInGraceMs - 1), false)
  assert.equal(endsDeviceSignIn('PASS: something', deviceSignInGraceMs), true)
})

const holder = 'the user who holds these grants'
const devicePrompt = (who: string, code: string) => `SIGN IN as ${who}: open https://microsoft.com/devicelogin and enter the code ${code}`

/**
 * Follows an MCP verifier run's output as the live driver does, logging each line shown and each sign-in entered,
 * refused or ended. Like the driver, it doesn't enter a code for a persona who is already signing in.
 */
function followRun(people: VerifyPersonas) {
  let clock = 0
  const log: string[] = []
  const signingIn = new Set<string>()
  const signIns = followDeviceSignIns(
    (line) => parseSignInPrompt(line, mcpSignInSubjects),
    (prompt) => {
      const target = deviceCodePersona(prompt, people)
      if ('refused' in target || signingIn.has(target.personaKey)) {
        log.push(`refused ${prompt.code}`)
        return undefined
      }
      const { personaKey } = target
      signingIn.add(personaKey)
      log.push(`entered ${prompt.code} for ${personaKey}`)
      return {
        startedAt: clock,
        finish() {
          signingIn.delete(personaKey)
          log.push(`ended ${prompt.code}`)
        },
      }
    },
    () => clock,
  )
  return {
    log,
    read(line: string, afterMs: number) {
      clock += afterMs
      signIns.line(line, () => log.push(`shown ${line}`))
    },
    end: () => signIns.end(),
  }
}

test('every device-code prompt is entered, even a second one for the same person, once the one before ends', () => {
  // M5 with --check-ungranted-user and --missing-scope-client-id: the user, the stranger, then the user again.
  const first = devicePrompt(holder, 'CODE0001')
  const stranger = devicePrompt('a different user, one with no grant for these MCP servers', 'CODE0002')
  const again = devicePrompt(holder, 'CODE0003')
  const passed = 'PASS: User grant 1 (M-tools) answers an anonymous call with 401 and its resource metadata'
  const run = followRun({ user: 'user-a', stranger: 'outsider' })
  run.read(first, 0)
  run.read(stranger, 60_000)
  run.read(again, 60_000)
  run.read(passed, 60_000)
  run.end()
  assert.deepEqual(run.log, [
    `shown ${first}`,
    'entered CODE0001 for user-a',
    'ended CODE0001',
    `shown ${stranger}`,
    'entered CODE0002 for outsider',
    'ended CODE0002',
    `shown ${again}`,
    'entered CODE0003 for user-a',
    'ended CODE0003',
    `shown ${passed}`,
  ])
})

test("the user's second prompt is entered when it straight follows the first, even within the grace window", () => {
  // M5 with --missing-scope-client-id alone: the user's two sign-ins, with only a note that the first is waiting between.
  const first = devicePrompt(holder, 'CODE0001')
  const again = devicePrompt(holder, 'CODE0002')
  const waiting = `INFO: The sign-in service answered HTTP 502; still waiting for ${holder} to sign in`
  const run = followRun({ user: 'user-a' })
  run.read(first, 0)
  run.read(waiting, 30_000)
  run.read(again, 30_000)
  run.end()
  assert.deepEqual(run.log, [
    `shown ${first}`,
    'entered CODE0001 for user-a',
    `shown ${waiting}`,
    'ended CODE0001',
    `shown ${again}`,
    'entered CODE0002 for user-a',
    'ended CODE0002',
  ])
  // A prompt ends the sign-in before it even when it seems to have been written first, on the other stream.
  const quick = followRun({ user: 'user-a' })
  quick.read(first, 0)
  quick.read(again, deviceSignInGraceMs - 1)
  quick.end()
  assert.deepEqual(quick.log, [
    `shown ${first}`,
    'entered CODE0001 for user-a',
    `shown ${again}`,
    'ended CODE0001',
    'entered CODE0002 for user-a',
    'ended CODE0002',
  ])
})

test('a MOSAIC API token is taken only from API calls with a bearer JWT', async () => {
  const api = 'https://mosaic-api.contoso.example'
  const jwt = ['eyJhbGciOiJub25lIn0', 'eyJzdWIiOiJ4In0', 'c2ln'].join('.')
  const request = (url: string, authorization: string | null) =>
    ({ url: () => url, headerValue: async () => authorization }) as unknown as Request
  assert.equal(await carriesApiToken(request(`${api}/api/v1/portal/me`, `Bearer ${jwt}`), api), true)
  assert.equal(await carriesApiToken(request(`${api}/api/v1/portal/me`, null), api), false)
  assert.equal(await carriesApiToken(request(`${api}/api/v1/portal/me`, 'Basic abc'), api), false)
  assert.equal(await carriesApiToken(request(`${api}/healthz`, `Bearer ${jwt}`), api), false)
  assert.equal(await carriesApiToken(request(`https://fabrikam.example/api/v1`, `Bearer ${jwt}`), api), false)
})
