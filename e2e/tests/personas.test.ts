import type { Page } from '@playwright/test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from 'node:test'
import { parseTargets } from '../src/config.ts'
import { e2eRoot } from '../src/paths.ts'
import { SignInError, appDestination, closePersona, completeEntraSignIn, isAppUrl } from '../src/personas.ts'

const targets = parseTargets(JSON.parse(readFileSync(join(e2eRoot, 'targets.example.json'), 'utf8')))
const { web, portal, api } = targets.origins
const loginUrl = 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'

interface LoginFrame {
  url: string
  body?: string
}

/** A page that shows one frame per poll and stays on the last. Nothing on it is visible to automate. */
function scriptedPage(frames: LoginFrame[]): { page: Page; polls: () => number } {
  let poll = 0
  const frame = (): LoginFrame => frames[Math.min(poll, frames.length - 1)]
  const locator = (selector: string) => {
    const handle = {
      first: () => handle,
      isVisible: async () => false,
      innerText: async () => (selector === 'body' ? (frame().body ?? '') : ''),
    }
    return handle
  }
  const page = {
    url: () => frame().url,
    locator,
    waitForTimeout: async () => {
      poll += 1
      await new Promise((resolve) => setTimeout(resolve, 1))
    },
  }
  return { page: page as unknown as Page, polls: () => poll }
}

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

test('non-interactive sign-in waits out a login page that silent SSO passes through', async () => {
  const { page, polls } = scriptedPage([{ url: loginUrl }, { url: loginUrl }, { url: `${web}/models` }])
  await completeEntraSignIn(page, web, 'admin@example.com', false, 5_000)
  assert.equal(polls(), 2)
})

test('non-interactive sign-in gives up once a login page settles with nothing to automate', async () => {
  const { page } = scriptedPage([{ url: loginUrl }])
  const started = Date.now()
  await assert.rejects(
    completeEntraSignIn(page, web, 'admin@example.com', false, 5_000, 20),
    (error: unknown) => error instanceof SignInError && /needs a password, MFA, or consent/.test(error.message),
  )
  assert.ok(Date.now() - started < 2_000)
})

test('an Entra error code fails sign-in at once, without waiting for the page to settle', async () => {
  const { page, polls } = scriptedPage([{ url: loginUrl, body: 'AADSTS50105: The signed in user is not assigned to a role' }])
  await assert.rejects(completeEntraSignIn(page, web, 'admin@example.com', false, 5_000), /AADSTS50105/)
  assert.equal(polls(), 0)
})
