import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import App from './App'
import type { RuntimeConfig } from './runtime-config'

const mocks = vi.hoisted(() => ({
  config: {
    apiBaseUrl: 'http://localhost:8000',
    authMode: 'local',
    entraTenantId: 'organizations',
    entraClientId: 'local-development',
    entraApiScope: 'api://local-development/access_as_user',
  } as RuntimeConfig,
  accounts: [
    { name: 'Name (Team)', username: 'name@example.com', homeAccountId: 'home-account' },
  ],
  getConsoleAccess: vi.fn(),
  loginRedirect: vi.fn(),
  logoutRedirect: vi.fn(),
}))

vi.mock('./runtime-config', () => ({ runtimeConfig: mocks.config }))

vi.mock('@azure/msal-react', () => ({
  useIsAuthenticated: () => true,
  useMsal: () => ({
    accounts: mocks.accounts,
    inProgress: 'none',
    instance: {
      loginRedirect: mocks.loginRedirect,
      logoutRedirect: mocks.logoutRedirect,
    },
  }),
}))

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>()
  const api = {
    getConsoleAccess: mocks.getConsoleAccess,
    listPrincipals: async () => [],
    listGroups: async () => [],
  }
  return { ...actual, useMosaicApi: () => api }
})

const primaryLinks = [
  'Dashboard',
  'Models',
  'Pools',
  'MCPs',
  'Identity',
  'Entitlements',
  'Cost centers',
  'Policies',
  'Analytics',
  'Pricing',
  'Settings',
  'Support',
]

const roleChangeHint = 'If an administrator has just granted you a role, sign out and sign in again.'

function renderApp() {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <MemoryRouter initialEntries={['/dashboard']}>
        <App />
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

function expectNoAdminShell() {
  expect(screen.queryByRole('navigation', { name: 'Primary navigation' })).not.toBeInTheDocument()
  expect(screen.queryByRole('link', { name: 'Dashboard' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Add model endpoint' })).not.toBeInTheDocument()
  expect(screen.queryByText('MOSAIC Admin')).not.toBeInTheDocument()
  expect(screen.queryByText('Global Admin')).not.toBeInTheDocument()
}

beforeEach(() => {
  mocks.config.authMode = 'entra'
  mocks.getConsoleAccess.mockReset()
  mocks.loginRedirect.mockReset()
  mocks.logoutRedirect.mockReset()
})

describe('App shell', () => {
  it('uses MOSAIC branding and the requested navigation', () => {
    mocks.config.authMode = 'local'

    renderApp()

    expect(screen.getAllByText('MOSAIC').length).toBeGreaterThan(0)
    for (const label of primaryLinks) {
      expect(screen.getByRole('link', { name: label })).toBeVisible()
    }
    expect(screen.getByRole('button', { name: 'Add model endpoint' })).toBeVisible()
    expect(screen.queryByText('AzureLite')).not.toBeInTheDocument()
    // Cost centers sits next to Entitlements; Pricing sits right after Analytics, whose figures it prices.
    const primary = within(screen.getByRole('navigation', { name: 'Primary navigation' }))
    const labels = primary.getAllByRole('link').map((link) => link.textContent)
    expect(labels.indexOf('Cost centers')).toBe(labels.indexOf('Entitlements') + 1)
    expect(labels.indexOf('Pricing')).toBe(labels.indexOf('Analytics') + 1)
  })

  it('leaves local mode unchanged: no role check and a Local mode label', () => {
    mocks.config.authMode = 'local'

    renderApp()

    expect(screen.getByRole('link', { name: 'Dashboard' })).toBeVisible()
    expect(screen.getByText('Local mode')).toBeVisible()
    expect(screen.queryByText('MOSAIC Admin')).not.toBeInTheDocument()
    expect(mocks.getConsoleAccess).not.toHaveBeenCalled()
  })
})

describe('Console access in Entra mode', () => {
  it('shows a neutral loading state, not the admin shell, while it checks the role', async () => {
    mocks.getConsoleAccess.mockReturnValue(new Promise(() => {}))

    renderApp()

    expect(await screen.findByText('Checking your access to the MOSAIC console')).toBeVisible()
    expectNoAdminShell()
  })

  it('renders the admin shell with the MOSAIC role for an administrator', async () => {
    mocks.getConsoleAccess.mockResolvedValue({ roles: ['Admin', 'User'], isAdmin: true })

    renderApp()

    expect(await screen.findByRole('link', { name: 'Dashboard' })).toBeVisible()
    for (const label of primaryLinks) {
      expect(screen.getByRole('link', { name: label })).toBeVisible()
    }
    expect(screen.getByRole('button', { name: 'Add model endpoint' })).toBeVisible()
    expect(screen.getByText('MOSAIC Admin')).toBeVisible()
    expect(screen.queryByText('Global Admin')).not.toBeInTheDocument()
    expect(screen.getByText('Name (Team)')).toBeVisible()
    expect(screen.getAllByText('NT')).toHaveLength(2)
    expect(screen.queryByText('N(')).not.toBeInTheDocument()
    expect(mocks.getConsoleAccess).toHaveBeenCalledTimes(1)
  })

  it('tells a User-only caller the console is for administrators', async () => {
    mocks.getConsoleAccess.mockResolvedValue({ roles: ['User'], isAdmin: false })

    renderApp()

    expect(await screen.findByText('This console is for MOSAIC administrators')).toBeVisible()
    expect(
      screen.getByText(
        'Your account has the User role, which opens the MOSAIC end-user portal. To use this console, an administrator must grant you the Admin role.',
      ),
    ).toBeVisible()
    expect(screen.getByText('Signed in as name@example.com')).toBeVisible()
    expect(screen.getByText(roleChangeHint)).toBeVisible()
    expectNoAdminShell()

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(mocks.logoutRedirect).toHaveBeenCalledTimes(1)
  })

  it('tells a caller with no MOSAIC role that they do not have access yet', async () => {
    mocks.getConsoleAccess.mockRejectedValue(
      new ApiError('A MOSAIC app role is required: Admin, User', 403),
    )

    renderApp()

    expect(await screen.findByText('You do not have access to MOSAIC yet')).toBeVisible()
    expect(
      screen.getByText(
        'An administrator must grant you a MOSAIC role. The Admin role opens this console; the User role opens the MOSAIC end-user portal.',
      ),
    ).toBeVisible()
    expect(screen.getByText('Signed in as name@example.com')).toBeVisible()
    expect(screen.getByText(roleChangeHint)).toBeVisible()
    expectNoAdminShell()
    expect(screen.queryByText('This console is for MOSAIC administrators')).not.toBeInTheDocument()
    expect(mocks.getConsoleAccess).toHaveBeenCalledTimes(1)

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(mocks.logoutRedirect).toHaveBeenCalledTimes(1)
  })

  it('offers a retry and sign-out, not the admin shell, when the check fails', async () => {
    mocks.getConsoleAccess.mockRejectedValue(new ApiError('Request failed with status 503', 503))

    renderApp()

    expect(
      await screen.findByText('Unable to check your access', undefined, { timeout: 5000 }),
    ).toBeVisible()
    expect(
      screen.getByText(
        'The console could not confirm your MOSAIC role: Request failed with status 503',
      ),
    ).toBeVisible()
    expectNoAdminShell()
    expect(screen.queryByText(roleChangeHint)).not.toBeInTheDocument()
    // A server error is retried once before the page gives up.
    expect(mocks.getConsoleAccess).toHaveBeenCalledTimes(2)

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    expect(mocks.logoutRedirect).toHaveBeenCalledTimes(1)

    mocks.getConsoleAccess.mockReset()
    mocks.getConsoleAccess.mockResolvedValue({ roles: ['Admin'], isAdmin: true })
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))

    expect(await screen.findByRole('link', { name: 'Dashboard' })).toBeVisible()
    expect(screen.getByText('MOSAIC Admin')).toBeVisible()
    await waitFor(() => expect(mocks.getConsoleAccess).toHaveBeenCalledTimes(1))
  })

  it('shows a network failure as an error rather than as a missing role', async () => {
    mocks.getConsoleAccess.mockRejectedValue(new TypeError('Failed to fetch'))

    renderApp()

    expect(
      await screen.findByText('Unable to check your access', undefined, { timeout: 5000 }),
    ).toBeVisible()
    expect(
      screen.getByText('The console could not confirm your MOSAIC role: Failed to fetch'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible()
    expect(screen.queryByText('You do not have access to MOSAIC yet')).not.toBeInTheDocument()
    expectNoAdminShell()
  })
})
