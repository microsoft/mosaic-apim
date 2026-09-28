import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { parseTargets } from '../src/config.ts'
import { e2eRoot } from '../src/paths.ts'
import { SignInError, appDestination, closePersona, isAppUrl } from '../src/personas.ts'

const targets = parseTargets(JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')))
const { web, portal, api } = targets.origins

test('sign-in destinations resolve on the app origin', () => {
  assert.equal(appDestination(targets, 'web').href, `${web}/`)
  assert.equal(appDestination(targets, 'web', '/models?tab=aoai').href, `${web}/models?tab=aoai`)
  assert.equal(appDestination(targets, 'web', 'models').href, `${web}/models`)
  assert.equal(appDestination(targets, 'portal', `${portal}/access`).href, `${portal}/access`)
})

test('sign-in destinations refuse other origins and script or file URLs', () => {
  const paths = [
    '//evil.example/x',
    'https://evil.example/',
    'javascript:void(document.title=1)',
    'file:///C:/Users/x/.azure/msal_token_cache.json',
    portal,
    'http://[',
  ]
  for (const path of paths) {
    assert.throws(() => appDestination(targets, 'web', path), SignInError, path)
  }
})

test('only web and portal pages count as MOSAIC pages', () => {
  assert.ok(isAppUrl(targets, `${web}/models`))
  assert.ok(isAppUrl(targets, `${portal}/access`))
  assert.ok(!isAppUrl(targets, 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'))
  assert.ok(!isAppUrl(targets, `${api}/healthz`))
  assert.ok(!isAppUrl(targets, 'about:blank'))
})

test('closing a persona browser stops waiting for a browser that never finishes exiting', async () => {
  assert.equal(await closePersona({ close: async () => {} }, 1_000), true)
  assert.equal(await closePersona({ close: () => Promise.reject(new Error('Target closed')) }, 1_000), true)
  const started = Date.now()
  assert.equal(await closePersona({ close: () => new Promise<void>(() => {}) }, 50), false)
  assert.ok(Date.now() - started < 1_000)
})
