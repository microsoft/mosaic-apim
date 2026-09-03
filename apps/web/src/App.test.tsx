import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import App from './App'

vi.mock('@azure/msal-react', () => ({
  useIsAuthenticated: () => true,
  useMsal: () => ({
    accounts: [],
    inProgress: 'none',
    instance: {
      loginRedirect: vi.fn(),
      logoutRedirect: vi.fn(),
    },
  }),
}))

vi.mock('./api', () => ({
  useMosaicApi: () => ({
    listPrincipals: async () => [],
    listGroups: async () => [],
  }),
}))

describe('App shell', () => {
  it('uses MOSAIC branding and the requested navigation', async () => {
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

    expect(screen.getAllByText('MOSAIC').length).toBeGreaterThan(0)
    for (const label of [
      'Dashboard',
      'Models',
      'MCPs',
      'Identity',
      'Entitlements',
      'Policies',
      'Analytics',
      'Settings',
      'Support',
    ]) {
      expect(screen.getByRole('link', { name: label })).toBeVisible()
    }
    expect(screen.getByRole('button', { name: 'Deploy Model' })).toBeVisible()
    expect(screen.queryByText('AzureLite')).not.toBeInTheDocument()
  })
})
