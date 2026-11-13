import { describe, expect, it } from 'vitest'
import { resolvePortalDestination } from './runtime-config'

const consoleOrigin = 'https://console.example.test'

describe('portal destination', () => {
  it('normalizes an explicitly configured HTTPS origin', () => {
    expect(resolvePortalDestination(' HTTPS://PORTAL.EXAMPLE.TEST:443/ ', consoleOrigin))
      .toEqual({ status: 'ready', href: 'https://portal.example.test' })
  })

  it.each([undefined, null, '', ' \t '])('reports missing configuration: %s', (value) => {
    expect(resolvePortalDestination(value, consoleOrigin)).toEqual({ status: 'missing' })
  })

  it.each([
    42,
    {},
    'not a URL',
    '${MOSAIC_PORTAL_URL}',
    '/portal',
    '//portal.example.test',
    'javascript:alert(1)',
    'data:text/html,portal',
    'https://user:password@portal.example.test',
    'https://portal.example.test/access',
    'https://portal.example.test?returnTo=https://other.example.test',
    'https://portal.example.test#access',
    consoleOrigin,
  ])('refuses unsafe or misconfigured destinations: %s', (value) => {
    expect(resolvePortalDestination(value, consoleOrigin)).toEqual({ status: 'invalid' })
  })

  it.each([
    'http://localhost:5174',
    'http://127.0.0.1:15174',
    'http://[::1]:5174',
  ])('allows explicit HTTP loopback from a loopback console: %s', (value) => {
    expect(resolvePortalDestination(value, 'http://localhost:5173'))
      .toEqual({ status: 'ready', href: value })
  })

  it.each([
    ['http://portal.example.test', 'http://localhost:5173'],
    ['http://localhost:5174', consoleOrigin],
    ['http://127.0.0.1:5174', consoleOrigin],
    ['http://localhost.example.test:5174', 'http://localhost:5173'],
    ['http://localhost:5174', 'http://localhost.example.test:5173'],
    ['http://localhost:5173', 'http://localhost:5173'],
  ])('refuses HTTP outside the local exception: %s from %s', (value, origin) => {
    expect(resolvePortalDestination(value, origin)).toEqual({ status: 'invalid' })
  })
})
