import { expect, test } from '../src/fixtures.ts'
import { persona } from '../src/config.ts'
import {
  consoleUserOnlyTitle,
  noConsoleAccessTitle,
  noPortalAccessTitle,
  primaryNavigation,
} from '../src/personas.ts'

/**
 * Phase 1 smoke journeys: the deployment is healthy and each persona reaches the surface its role allows.
 * These are read-only and safe to run at any time.
 */
test.describe('00 smoke', { tag: '@smoke' }, () => {
  test('S1 API liveness and readiness', async ({ request, targets }) => {
    const health = await request.get(new URL('/healthz', targets.origins.api).href)
    expect(health.status()).toBe(200)
    expect(await health.json()).toMatchObject({ status: 'ok' })

    const ready = await request.get(new URL('/readyz', targets.origins.api).href)
    expect(ready.status()).toBe(200)
    expect(await ready.json()).toMatchObject({ status: 'ready' })
  })

  test('S2 API rejects anonymous and malformed-token requests', async ({ request, targets }) => {
    const protectedPaths = ['/api/v1/model-endpoints', '/api/v1/principals', '/api/v1/gateways', '/api/v1/me/entitlements']
    for (const path of protectedPaths) {
      const url = new URL(path, targets.origins.api).href
      const anonymous = await request.get(url)
      expect(anonymous.status(), `anonymous GET ${path}`).toBe(401)
      const malformed = await request.get(url, { headers: { Authorization: 'Bearer not-a-token' } })
      expect(malformed.status(), `malformed-token GET ${path}`).toBe(401)
    }
  })

  test('A0 admin reaches the MOSAIC console', async ({ personas, targets }) => {
    const page = await personas.page(targets.roles.admin, 'web', '/models')
    await expect(page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })).toBeVisible()
    await expect(page.getByRole('heading', { name: 'Model endpoints' })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Add model endpoint' }).first()).toBeVisible()
    await expect(page.getByText('MOSAIC Admin', { exact: true })).toBeVisible()
    await expect(page.getByText('Global Admin')).toHaveCount(0)
    await expect(page.getByText('Unable to load data')).toHaveCount(0)
  })

  test('A1 console turns a User-only persona away with one explanation', async ({ personas, targets }) => {
    const userKey = targets.roles.guest ?? targets.roles.user
    test.skip(targets.personas[userKey].expectedRole !== 'User', `${userKey} is not expected to hold only the User role`)
    const page = await personas.page(userKey, 'web', '/dashboard')
    await expect(page.getByText(consoleUserOnlyTitle)).toBeVisible()
    await expect(page.getByText('an administrator must grant you the Admin role')).toBeVisible()
    await expect(page.getByText(`Signed in as ${persona(targets, userKey).upn}`)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
    await expect(page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })).toHaveCount(0)
    await expect(page.getByText('Unable to load data')).toHaveCount(0)
  })

  test('A1 console tells a persona without a MOSAIC role that it has no access', async ({ personas, targets }) => {
    const noRoleKey = targets.roles.noRole
    test.skip(!noRoleKey, 'No persona is mapped to roles.noRole')
    test.skip(targets.personas[noRoleKey as string].expectedRole !== 'None', `${noRoleKey} is expected to hold a role`)
    const page = await personas.page(noRoleKey as string, 'web', '/dashboard')
    await expect(page.getByText(noConsoleAccessTitle)).toBeVisible()
    await expect(page.getByText(consoleUserOnlyTitle)).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
    await expect(page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })).toHaveCount(0)
  })

  test('P0 portal opens for a User persona', async ({ personas, targets }) => {
    const userKey = targets.roles.guest ?? targets.roles.user
    test.skip(targets.personas[userKey].expectedRole !== 'User', `${userKey} is not expected to hold the User role`)
    const page = await personas.page(userKey, 'portal', '/access')
    await expect(page.getByRole(primaryNavigation.role, { name: primaryNavigation.name })).toBeVisible()
    await expect(page.getByRole('link', { name: 'Catalog' })).toBeVisible()
    await expect(page.getByText(noPortalAccessTitle)).toHaveCount(0)
  })

  test('P1 portal explains missing access to a persona without a MOSAIC role', async ({ personas, targets }) => {
    const noRoleKey = targets.roles.noRole
    test.skip(!noRoleKey, 'No persona is mapped to roles.noRole')
    test.skip(targets.personas[noRoleKey as string].expectedRole !== 'None', `${noRoleKey} is expected to hold a role`)
    const page = await personas.page(noRoleKey as string, 'portal', '/access')
    await expect(page.getByText(noPortalAccessTitle)).toBeVisible()
    await expect(page.getByRole('button', { name: 'Sign out' })).toBeVisible()
  })
})
