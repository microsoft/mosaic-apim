import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import type { PortalApi } from './api'
import App from './App'

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

describe('App', () => {
  it('renders the portal no-role explanation on a 403 profile response', async () => {
    mocks.api = {
      getProfile: async () => {
        throw Object.assign(new Error('Forbidden'), { status: 403 })
      },
      listEntitlements: async () => [],
    } as unknown as PortalApi

    renderApp()

    expect(await screen.findByText('You do not have access to the portal yet')).toBeVisible()
    expect(
      screen.getByText(/An administrator must grant you the MOSAIC User role/),
    ).toBeVisible()
  })
})
