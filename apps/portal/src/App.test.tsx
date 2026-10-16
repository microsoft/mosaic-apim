import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from './api'
import App from './App'
import type { PortalProfile } from './types'
import { notificationPreferences } from './test/notification-fixtures'

const mocks = vi.hoisted(() => ({
  api: {} as PortalApi,
}))

vi.mock('@azure/msal-react', () => ({
  useIsAuthenticated: () => true,
  useMsal: () => ({
    accounts: [{ name: 'Ada Lovelace' }],
    inProgress: 'none',
    instance: {
      loginRedirect: vi.fn(),
      logoutRedirect: vi.fn(),
      acquireTokenSilent: vi.fn(),
    },
  }),
}))

vi.mock('./api', () => ({
  ApiError: class ApiError extends Error {
    status = 403
  },
  usePortalApi: () => Object.assign({ listNotifications: async () => ({ items: [], unreadCount: 0, continuationToken: null }) }, mocks.api),
}))

function renderApp(path = '/access') {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={createMemoryRouter([{ path: '*', element: <App /> }], { initialEntries: [path] })} />
    </QueryClientProvider>,
  )
}

function portalProfile(overrides: Partial<PortalProfile> = {}): PortalProfile {
  return {
    objectId: 'object-id',
    tenantId: 'tenant-id',
    roles: ['User'],
    isAdmin: false,
    principalId: null,
    displayLabel: 'Ada Lovelace',
    entitlementCount: 0,
    pendingRequestCount: 0,
    groupsOverage: false,
    ...overrides,
  }
}

describe('App', () => {
  it('exposes inbox through navigation and bell, and personal preferences through the account menu', async () => {
    const user = userEvent.setup()
    mocks.api = {
      getProfile: async () => portalProfile(),
      listNotifications: async () => ({ items: [], unreadCount: 3, continuationToken: null }),
      getNotificationPreferences: async () => notificationPreferences(),
      getNotificationHealth: async () => [],
      getNotificationPreferencePreview: async () => ({ preferences: notificationPreferences(), channels: [], checks: [] }),
      getPersonalTeamsStatus: async () => ({ enabled: false, state: 'notInstalled', checks: [] }),
    } as unknown as PortalApi
    renderApp('/notifications')
    expect(await screen.findByRole('heading', { name: 'Notifications' })).toBeVisible()
    expect(await screen.findByRole('link', { name: 'Notifications — 3 unread' })).toHaveAttribute('href', '/notifications')
    await user.click(screen.getByRole('button', { name: 'Account menu for Ada Lovelace' }))
    await user.click(await screen.findByRole('menuitem', { name: 'Notification preferences' }))
    expect(await screen.findByRole('heading', { name: 'Notification preferences' })).toBeVisible()
    expect(await screen.findByLabelText('Email — Access requests')).toHaveValue('off')
  })
  it.each(['/', '/unknown-page'])('lands %s on usage and orders end-user navigation', async (path) => {
    mocks.api = {
      getProfile: async () => portalProfile(),
      getMyUsage: async () => ({ byResource: [] }),
      getMyBudgets: async () => [],
      listEnvironments: async () => [],
    } as unknown as PortalApi
    renderApp(path)
    expect(await screen.findByRole('heading', { name: 'Usage & cost' })).toBeVisible()
    const links = within(screen.getByRole('navigation', { name: 'Primary navigation' })).getAllByRole('link')
    expect(links.map((link) => link.textContent)).toEqual(['Usage & cost', 'Catalog', 'My access', 'My requests', 'Notifications'])
    expect(links[0]).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('group', { name: 'Appearance' })).toBeVisible()
    expect(screen.queryByText('Personal view')).not.toBeInTheDocument()
  })
  it('renders the portal no-role explanation on a 403 profile response', async () => {
    mocks.api = {
      getProfile: async () => {
        throw Object.assign(new Error('Forbidden'), { status: 403 })
      },
      listEntitlements: async () => [],
      listEnvironments: async () => [],
    } as unknown as PortalApi

    renderApp()

    expect(await screen.findByText('You do not have access to the portal yet')).toBeVisible()
    expect(
      screen.getByText(/An administrator must grant you the MOSAIC User role/),
    ).toBeVisible()
  })

  it('builds the avatar initials from letters and digits only', async () => {
    mocks.api = {
      getProfile: async () => portalProfile({ displayLabel: 'Name (Team)' }),
      listEntitlements: async () => [],
    } as unknown as PortalApi

    renderApp()

    expect(await screen.findByText('Name (Team)')).toBeVisible()
    expect(screen.getByText('NT')).toBeVisible()
    expect(screen.queryByText('N(')).not.toBeInTheDocument()
  })

  it.each([
    { entitlementCount: 0, pendingRequestCount: 0, summary: '0 grants · 0 pending requests' },
    { entitlementCount: 1, pendingRequestCount: 1, summary: '1 grant · 1 pending request' },
    { entitlementCount: 2, pendingRequestCount: 2, summary: '2 grants · 2 pending requests' },
    { entitlementCount: 1, pendingRequestCount: 2, summary: '1 grant · 2 pending requests' },
    { entitlementCount: 2, pendingRequestCount: 1, summary: '2 grants · 1 pending request' },
  ])(
    'summarizes access in the header as $summary',
    async ({ entitlementCount, pendingRequestCount, summary }) => {
      mocks.api = {
        getProfile: async () => portalProfile({ entitlementCount, pendingRequestCount }),
        listEntitlements: async () => [],
        listEnvironments: async () => [],
      } as unknown as PortalApi

      renderApp()

      const summaryText = await screen.findByText(summary)
      expect(summaryText).toBeVisible()
      const header = summaryText.closest('header')
      expect(header).toBeInTheDocument()
      expect(header).not.toHaveTextContent(/entitlement/i)
    },
  )
})
