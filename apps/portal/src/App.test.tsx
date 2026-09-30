import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from './api'
import App from './App'
import type { PortalProfile } from './types'

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
  usePortalApi: () => mocks.api,
}))

function renderApp() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/access']}>
        <App />
      </MemoryRouter>
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
