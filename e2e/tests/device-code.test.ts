import assert from 'node:assert/strict'
import { test } from 'node:test'
import type { Request } from '@playwright/test'
import { carriesApiToken, deviceCodePersona, deviceSignInGraceMs, endsDeviceSignIn } from '../src/device-code.ts'
import type { SignInPrompt } from '../src/verify.ts'

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
