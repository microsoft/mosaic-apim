import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'
import { IdentityPage } from './IdentityPage'

vi.mock('../api', () => ({
  useMosaicApi: () => ({
    listPrincipals: async () => [
      {
        id: 'user',
        tenantId: 'tenant',
        objectId: 'entra-user',
        kind: 'user',
        label: 'Alex User',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
      {
        id: 'workload',
        tenantId: 'tenant',
        objectId: 'entra-workload',
        kind: 'managedIdentity',
        label: 'Build agent',
        createdAt: '2026-01-01T00:00:00Z',
        updatedAt: '2026-01-01T00:00:00Z',
      },
    ],
    listGroups: async () => [],
    listMemberships: async () => [],
    createPrincipal: vi.fn(),
    updatePrincipal: vi.fn(),
    deletePrincipal: vi.fn(),
    createGroup: vi.fn(),
    updateGroup: vi.fn(),
    deleteGroup: vi.fn(),
    addMembership: vi.fn(),
    removeMembership: vi.fn(),
  }),
}))

describe('IdentityPage', () => {
  it('separates users from workload identities using URL-addressable tabs', async () => {
    const user = userEvent.setup()
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <MemoryRouter initialEntries={['/identity?tab=users']}>
          <IdentityPage />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect((await screen.findAllByText('Alex User')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Build agent')).not.toBeInTheDocument()
    expect(screen.getByText('Live data')).toBeVisible()

    await user.click(screen.getByRole('tab', { name: 'Workload identities' }))

    expect((await screen.findAllByText('Build agent')).length).toBeGreaterThan(0)
    expect(screen.queryByText('Alex User')).not.toBeInTheDocument()
  })
})
