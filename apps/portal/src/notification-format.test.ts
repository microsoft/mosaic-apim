import { describe, expect, it } from 'vitest'
import {
  capabilityStatus, defaultCategoryPreference, notificationLink, preferenceInput, preferenceValidation, safeRemediationPath,
} from './notification-format'
import { notificationCheck, notificationItem, notificationPreferences } from './test/notification-fixtures'
import type { CategoryNotificationPreference } from './notification-types'

describe('notification contracts', () => {
  it('defaults only in-app on and excludes identity and record metadata from preference writes', () => {
    expect(defaultCategoryPreference('requestLifecycle')).toEqual({
      category: 'requestLifecycle', inApp: true,
      email: { mode: 'off', digestPeriods: [] }, teams: { mode: 'off', digestPeriods: [] },
    })
    const input = preferenceInput(notificationPreferences({ categories: [] }))
    expect(input.categories).toHaveLength(5)
    expect(input.categories.every((item) => item.inApp && item.email.mode === 'off' && item.teams.mode === 'off')).toBe(true)
    expect(input).not.toHaveProperty('userObjectId')
    expect(input).not.toHaveProperty('tenantId')
    expect(input).not.toHaveProperty('persona')
  })

  it('requires explicit digest periods, schedules, valid timezone, thresholds and cooldown', () => {
    const input = preferenceInput(notificationPreferences())
    expect(preferenceValidation(input)).toBeNull()
    expect(preferenceValidation({ ...input, timeZone: 'Not/A_Zone' })).toMatch(/IANA/)
    expect(preferenceValidation({ ...input, thresholds: [0, 1001] })).toMatch(/thresholds/)
    expect(preferenceValidation({ ...input, thresholds: [80, 80] })).toMatch(/different/)
    expect(preferenceValidation({ ...input, repeatCooldownSeconds: 299 })).toMatch(/cooldown/)
    const category: CategoryNotificationPreference = { ...input.categories[0], email: { mode: 'digest', digestPeriods: [] } }
    expect(preferenceValidation({ ...input, categories: [category] })).toMatch(/at least one/)
    category.email.digestPeriods = ['weekly']
    expect(preferenceValidation({ ...input, categories: [category] })).toMatch(/delivery schedule/)
  })

  it('uses fixed portal routes and encoded subject identifiers rather than arbitrary links', () => {
    expect(notificationLink(notificationItem())).toEqual({ to: '/requests?request=request%2F1', label: 'View request' })
    expect(notificationLink(notificationItem({ subject: { entitlementId: '../other?x=1' } }))?.to).toBe('/access?grant=..%2Fother%3Fx%3D1')
    expect(notificationLink(notificationItem({ category: 'usageAndBudgets', subject: { costCenterId: 'shared' } }))?.to).toBe('/usage')
    expect(notificationLink(notificationItem({ category: 'operationalIssues', subject: {} }))).toBeNull()
  })

  it('never treats configuration, stale evidence, or missing evidence as verified delivery', () => {
    expect(capabilityStatus(notificationCheck({ state: 'notConfigured' }))).toBe('Not configured')
    expect(capabilityStatus(notificationCheck({ state: 'verified' }))).toBe('Verification unavailable')
    expect(capabilityStatus(notificationCheck({
      state: 'verified', evidence: ['Observed personal installation'], freshUntil: '2026-10-02T00:00:00Z',
    }), Date.parse('2026-10-03T00:00:00Z'))).toBe('Verification expired')
    expect(capabilityStatus(notificationCheck({
      state: 'verified', evidence: ['Observed personal installation'], freshUntil: '2026-10-03T00:00:00Z',
    }), Date.parse('2026-10-02T00:00:00Z'))).toBe('Verified')
  })

  it.each(['javascript:alert(1)', '//evil.example', '/\\evil.example', 'https://evil.example', 'https://teams.microsoft.com@evil.example'])(
    'does not expose an unsupported remediation URL: %s', (path) => expect(safeRemediationPath(path)).toBeNull(),
  )
  it('allows same-origin guidance and official Teams installation or Microsoft documentation', () => {
    expect(safeRemediationPath('/notification-preferences')).toBe('/notification-preferences')
    expect(safeRemediationPath('https://teams.microsoft.com/l/app/example')).toBe('https://teams.microsoft.com/l/app/example')
    expect(safeRemediationPath('https://learn.microsoft.com/example')).toBe('https://learn.microsoft.com/example')
  })
})
