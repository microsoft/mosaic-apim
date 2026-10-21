import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, Link, RouterProvider } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from '../api'
import type { NotificationPreferencesInput } from '../notification-types'
import { notificationCheck, notificationPreferences } from '../test/notification-fixtures'
import { NotificationPreferencesPage } from './NotificationPreferencesPage'

const mocks = vi.hoisted(() => ({ api: {} as PortalApi }))
vi.mock('../api', () => ({ ApiError: class ApiError extends Error { status = 500 }, usePortalApi: () => mocks.api }))

function renderPage(api: Partial<PortalApi> = {}, client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })) {
  mocks.api = {
    getNotificationPreferences: async () => notificationPreferences(),
    getNotificationHealth: async () => [notificationCheck()],
    getPersonalTeamsStatus: async () => ({ enabled: false, state: 'notInstalled', checks: [] }),
    getNotificationPreferencePreview: async () => ({ preferences: notificationPreferences(), channels: [], checks: [] }),
    saveNotificationPreferences: async (input: NotificationPreferencesInput) => notificationPreferences(input),
    ...api,
  } as PortalApi
  const router = createMemoryRouter([
    { path: '/notification-preferences', element: <><NotificationPreferencesPage /><Link to="/usage">Usage page</Link></> },
    { path: '/usage', element: <h1>Usage destination</h1> },
  ], { initialEntries: ['/notification-preferences'] })
  render(<QueryClientProvider client={client}><RouterProvider router={router} /></QueryClientProvider>)
  return router
}

describe('NotificationPreferencesPage', () => {
  it('defaults in-app on and all external channels/digests off; shows actionable private Teams readiness', async () => {
    renderPage()
    const category = await screen.findByRole('group', { name: 'Access requests' })
    expect(within(category).getByRole('checkbox', { name: 'In-app — Access requests' })).toBeChecked()
    expect(within(category).getByLabelText('Email — Access requests')).toHaveValue('off')
    expect(within(category).getByLabelText('Private Teams — Access requests')).toHaveValue('off')
    expect(screen.getByText('No digests are enabled.', { exact: false })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled()
    expect(await screen.findByText('Action needed')).toBeVisible()
    expect(screen.getByText('Teams administrator')).toBeVisible()
    expect(screen.getByRole('link', { name: 'Open remediation guidance' })).toHaveAttribute('href', 'https://teams.microsoft.com/l/app/example')
    expect(screen.getByText(/Shared administrator channels are never a fallback/)).toBeVisible()
    expect(screen.queryByLabelText(/conversation|object id|mailbox/i)).not.toBeInTheDocument()
  })

  it('persists explicit category/channel cadence, three digest schedules, timezone, thresholds and cooldown', async () => {
    const user = userEvent.setup()
    const save = vi.fn(async (input: NotificationPreferencesInput) => notificationPreferences(input))
    renderPage({ saveNotificationPreferences: save })
    await user.selectOptions(await screen.findByLabelText('Email — Access requests'), 'digest')
    const periods = screen.getByRole('group', { name: 'Email — Access requests periods' })
    await user.click(within(periods).getByRole('checkbox', { name: 'Weekly' }))
    await user.click(within(periods).getByRole('checkbox', { name: 'Monthly' }))
    await user.selectOptions(screen.getByLabelText('Private Teams — My access'), 'immediate')
    await user.clear(screen.getByLabelText('Time zone'))
    await user.type(screen.getByLabelText('Time zone'), 'America/New_York')
    await user.selectOptions(screen.getByLabelText('Weekly delivery day'), '4')
    await user.clear(screen.getByLabelText('Monthly delivery day'))
    await user.type(screen.getByLabelText('Monthly delivery day'), '15')
    await user.clear(screen.getByLabelText('Threshold percentages'))
    await user.type(screen.getByLabelText('Threshold percentages'), '70, 90, 100')
    await user.clear(screen.getByLabelText('Repeat cooldown (seconds)'))
    await user.type(screen.getByLabelText('Repeat cooldown (seconds)'), '3600')
    await user.click(screen.getByRole('button', { name: 'Save preferences' }))
    await waitFor(() => expect(save).toHaveBeenCalledOnce())
    const input = save.mock.calls[0][0]
    expect(input).toMatchObject({ timeZone: 'America/New_York', thresholds: [70, 90, 100], repeatCooldownSeconds: 3600 })
    expect(input.categories[0].email).toEqual({ mode: 'digest', digestPeriods: ['daily', 'weekly', 'monthly'] })
    expect(input.categories[1].teams).toEqual({ mode: 'immediate', digestPeriods: [] })
    expect(input.digestSchedules).toEqual([
      { period: 'daily', deliveryTime: '09:00', dayOfWeek: 0, dayOfMonth: 1 },
      { period: 'weekly', deliveryTime: '09:00', dayOfWeek: 4, dayOfMonth: 1 },
      { period: 'monthly', deliveryTime: '09:00', dayOfWeek: 0, dayOfMonth: 15 },
    ])
    expect(Object.keys(input)).not.toContain('userObjectId')
    expect(save).toHaveBeenCalledWith(expect.anything(), 1)
    expect(await screen.findByRole('status')).toHaveTextContent('saved settings do not prove delivery')
    expect(screen.getByRole('button', { name: 'Save preferences' })).toBeDisabled()
    expect(screen.queryByText('Verified')).not.toBeInTheDocument()
  })

  it('retains a failed-save draft and validates invalid timezone and empty digest selection', async () => {
    const user = userEvent.setup()
    const save = vi.fn(async () => { throw new Error('Preferences service unavailable') })
    renderPage({ saveNotificationPreferences: save })
    await user.clear(await screen.findByLabelText('Time zone'))
    await user.type(screen.getByLabelText('Time zone'), 'Invalid/Zone')
    await user.click(screen.getByRole('button', { name: 'Save preferences' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('IANA time zone')
    expect(save).not.toHaveBeenCalled()
    await user.clear(screen.getByLabelText('Time zone'))
    await user.type(screen.getByLabelText('Time zone'), 'Europe/London')
    await user.selectOptions(screen.getByLabelText('Email — Access requests'), 'digest')
    const periods = screen.getByRole('group', { name: 'Email — Access requests periods' })
    await user.click(within(periods).getByRole('checkbox', { name: 'Daily' }))
    await user.click(screen.getByRole('button', { name: 'Save preferences' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Choose at least one digest period')
    expect(save).not.toHaveBeenCalled()
    await user.click(within(periods).getByRole('checkbox', { name: 'Daily' }))
    await user.click(screen.getByRole('button', { name: 'Save preferences' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Your draft is retained')
    expect(screen.getByLabelText('Time zone')).toHaveValue('Europe/London')
    expect(screen.queryByText(/Preferences saved/)).not.toBeInTheDocument()
  })

  it('protects drafts on navigation and unload, keeps edits after cancellation, and resets explicitly', async () => {
    const user = userEvent.setup()
    const router = renderPage()
    const timezone = await screen.findByLabelText('Time zone')
    await user.clear(timezone)
    await user.type(timezone, 'Europe/London')
    const beforeUnload = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(beforeUnload)
    expect(beforeUnload.defaultPrevented).toBe(true)
    await user.click(screen.getByRole('link', { name: 'Usage page' }))
    expect(await screen.findByRole('dialog')).toHaveTextContent('Discard unsaved notification preferences?')
    await user.click(screen.getByRole('button', { name: 'Keep editing' }))
    expect(timezone).toHaveValue('Europe/London')
    expect(router.state.location.pathname).toBe('/notification-preferences')
    await user.click(screen.getByRole('button', { name: 'Reset changes' }))
    const resetDialog = await screen.findByRole('dialog', { name: 'Reset unsaved changes?' })
    await user.click(within(resetDialog).getByRole('button', { name: 'Keep editing' }))
    expect(timezone).toHaveValue('Europe/London')
    await user.click(screen.getByRole('button', { name: 'Reset changes' }))
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Reset changes' }))
    expect(timezone).toHaveValue('UTC')
    await user.click(screen.getByRole('link', { name: 'Usage page' }))
    expect(await screen.findByRole('heading', { name: 'Usage destination' })).toBeVisible()
  })

  it('allows explicit draft discard when navigating away', async () => {
    const user = userEvent.setup()
    renderPage()
    await user.click(await screen.findByRole('checkbox', { name: 'In-app — Access requests' }))
    await user.click(screen.getByRole('link', { name: 'Usage page' }))
    await user.click(await screen.findByRole('button', { name: 'Discard and leave' }))
    expect(await screen.findByRole('heading', { name: 'Usage destination' })).toBeVisible()
  })

  it('does not invent default preferences when the API fails, and refreshes readiness without sending tests', async () => {
    const user = userEvent.setup()
    const getNotificationHealth = vi.fn(async () => [notificationCheck({ state: 'notConfigured', remediation: null })])
    renderPage({
      getNotificationPreferences: async () => { throw new Error('Preferences unavailable') },
      getNotificationHealth,
    })
    expect(await screen.findByText('Preferences unavailable')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Save preferences' })).not.toBeInTheDocument()
    expect(await screen.findByText('Not configured')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Refresh readiness' }))
    await waitFor(() => expect(getNotificationHealth).toHaveBeenCalledTimes(2))
    expect(screen.getByRole('button', { name: 'Send a test to my private Teams' })).toBeDisabled()
  })

  it('tests only the authenticated private installation and distinguishes acceptance from delivery', async () => {
    const user = userEvent.setup()
    const testPersonalTeams = vi.fn(async () => ({
      outcome: 'providerAccepted' as const, message: 'Check your Teams chat for receipt.',
      testedAt: '2026-10-16T12:00:00Z', confirmedDelivered: false,
    }))
    renderPage({
      getPersonalTeamsStatus: async () => ({ enabled: true, state: 'installed', checks: [] }),
      testPersonalTeams,
    })
    expect(testPersonalTeams).not.toHaveBeenCalled()
    const testButton = await screen.findByRole('button', { name: 'Send a test to my private Teams' })
    expect(testButton).toBeEnabled()
    await user.click(testButton)
    expect(await screen.findByRole('status')).toHaveTextContent('Teams accepted the test request; delivery is not confirmed.')
    expect(testPersonalTeams).toHaveBeenCalledOnce()
    expect(screen.queryByText('Delivery was confirmed.')).not.toBeInTheDocument()
  })

  it('retries exhausted private Teams status requests without checking access or sending', async () => {
    const user = userEvent.setup()
    const getPersonalTeamsStatus = vi.fn<PortalApi['getPersonalTeamsStatus']>()
      .mockRejectedValueOnce(new Error('Teams status unavailable'))
      .mockRejectedValueOnce(new Error('Teams status unavailable'))
      .mockResolvedValue({ enabled: false, state: 'notInstalled', checks: [] })
    const checkPersonalTeamsAccess = vi.fn<PortalApi['checkPersonalTeamsAccess']>().mockResolvedValue([])
    const testPersonalTeams = vi.fn<PortalApi['testPersonalTeams']>()
    const client = new QueryClient({ defaultOptions: { queries: { retry: 1, retryDelay: 0 }, mutations: { retry: false } } })
    renderPage({ getPersonalTeamsStatus, checkPersonalTeamsAccess, testPersonalTeams }, client)
    expect(await screen.findByText('Teams status unavailable')).toBeVisible()
    expect(getPersonalTeamsStatus).toHaveBeenCalledTimes(2)
    const retry = screen.getByRole('button', { name: 'Retry Teams status' })
    expect(retry).toBeEnabled()
    await user.click(retry)
    expect(await screen.findByText('Not installed')).toBeVisible()
    expect(getPersonalTeamsStatus).toHaveBeenCalledTimes(3)
    expect(checkPersonalTeamsAccess).not.toHaveBeenCalled()
    expect(testPersonalTeams).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: 'Check my Teams access' }))
    await waitFor(() => expect(checkPersonalTeamsAccess).toHaveBeenCalledOnce())
    expect(testPersonalTeams).not.toHaveBeenCalled()
  })

  it('keeps normal access checks available when a status refresh fails with cached data', async () => {
    const user = userEvent.setup()
    const status = { enabled: true, state: 'installed' as const, checks: [] }
    const getPersonalTeamsStatus = vi.fn<PortalApi['getPersonalTeamsStatus']>()
      .mockResolvedValueOnce(status)
      .mockRejectedValueOnce(new Error('Teams status refresh unavailable'))
      .mockResolvedValue(status)
    const checkPersonalTeamsAccess = vi.fn<PortalApi['checkPersonalTeamsAccess']>().mockResolvedValue([])
    const testPersonalTeams = vi.fn<PortalApi['testPersonalTeams']>()
    renderPage({ getPersonalTeamsStatus, checkPersonalTeamsAccess, testPersonalTeams })
    expect(await screen.findByText('Installed')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Check my Teams access' }))
    expect(await screen.findByText('Teams status refresh unavailable')).toBeVisible()
    expect(checkPersonalTeamsAccess).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: 'Retry Teams status' })).not.toBeInTheDocument()
    const checkAccess = screen.getByRole('button', { name: 'Check my Teams access' })
    expect(checkAccess).toBeEnabled()
    await user.click(checkAccess)
    expect(await screen.findByText('Installed')).toBeVisible()
    expect(getPersonalTeamsStatus).toHaveBeenCalledTimes(3)
    expect(checkPersonalTeamsAccess).toHaveBeenCalledTimes(2)
    expect(testPersonalTeams).not.toHaveBeenCalled()
  })

  it('shows saved effective cadence and next schedule without using a shared Teams fallback', async () => {
    renderPage({
      getNotificationPreferencePreview: async () => ({
        preferences: notificationPreferences(),
        channels: [
          { category: 'requestLifecycle', channel: 'email', mode: 'digest', digestPeriods: ['weekly'],
            nextDeliveryAt: '2026-10-19T09:00:00Z', available: false, reasons: ['Your directory contact could not be resolved.'] },
          { category: 'requestLifecycle', channel: 'teamsChannel', mode: 'immediate', digestPeriods: [],
            nextDeliveryAt: null, available: true, reasons: ['Shared administrator channel'] },
        ], checks: [],
      }),
    })
    expect(await screen.findByText('Your directory contact could not be resolved.')).toBeVisible()
    expect(screen.getByText('Digests: weekly')).toBeVisible()
    expect(screen.getByText('Next scheduled delivery:', { exact: false })).toBeVisible()
    expect(screen.queryByText('Shared administrator channel')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send a test to my private Teams' })).toBeDisabled()
  })
})
